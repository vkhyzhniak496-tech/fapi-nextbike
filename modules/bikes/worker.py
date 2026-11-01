import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List
from zoneinfo import ZoneInfo
import httpx

from config import HTTP_HEADERS
from core.database import get_pool
from modules.bikes.storage import set_cached_stations

logger = logging.getLogger(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")
CITYBIKES_WARSAW_URL = "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"

# Pamięć podręczna w RAM filtrująca zbędne snapshoty historyczne (zapisujemy do historii tylko delty)
LAST_KNOWN_BIKES: Dict[str, int] = {}


async def save_snapshots_to_postgres(
    stations_live_batch: List[tuple], snapshots_delta_batch: List[tuple]
) -> None:
    """
    Transakcyjny zapis:
    1. Aktualizuje bieżący stan i geometrię stacji (veturilo_stations).
    2. Zapisuje delty do historii czasowej (station_snapshots).
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. SŁOWNIK I BIEŻĄCY STAN (7 parametrów: id, name, lat, lng, capacity, free_bikes, empty_slots)
                if stations_live_batch:
                    await conn.executemany(
                        """
                        INSERT INTO veturilo_stations (
                            station_id, name, lat, lng, capacity, 
                            free_bikes, empty_slots,
                            geom, geom_2180, updated_at
                        )
                        VALUES (
                            $1, $2, $3, $4, $5, $6, $7,
                            ST_SetSRID(ST_MakePoint($4, $3), 4326),
                            ST_Transform(ST_SetSRID(ST_MakePoint($4, $3), 4326), 2180),
                            CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (station_id) DO UPDATE SET 
                            name = EXCLUDED.name,
                            lat = COALESCE(EXCLUDED.lat, veturilo_stations.lat),
                            lng = COALESCE(EXCLUDED.lng, veturilo_stations.lng),
                            geom = EXCLUDED.geom,
                            geom_2180 = EXCLUDED.geom_2180,
                            capacity = CASE 
                                WHEN EXCLUDED.capacity > 0 THEN EXCLUDED.capacity 
                                ELSE veturilo_stations.capacity 
                            END,
                            free_bikes = EXCLUDED.free_bikes,
                            empty_slots = EXCLUDED.empty_slots,
                            updated_at = CURRENT_TIMESTAMP;
                        """,
                        stations_live_batch,
                    )

                # 2. HISTORIA ZMIAN
                if snapshots_delta_batch:
                    await conn.executemany(
                        """
                        INSERT INTO station_snapshots (station_id, timestamp, bikes)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (station_id, timestamp) DO NOTHING;
                        """,
                        snapshots_delta_batch,
                    )
    except Exception as e:
        logger.error(f"[BIKES] Błąd transakcji zapisu bazy: {e}")

async def bike_history_poller_worker() -> None:
    """Worker odpytujący CityBikes, zasilający PostgreSQL i odświeżający GeoJSON w RAM."""
    backoff_delay = 45
    LAST_KNOWN_BIKES.clear()

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
    ) as client:
        logger.info("[BIKES] Uruchomiono worker telemetrii Veturilo...")
        while True:
            try:
                res = await client.get(CITYBIKES_WARSAW_URL)

                if res.status_code == 200:
                    backoff_delay = 45
                    network_data = res.json().get("network", {})
                    stations = network_data.get("stations", [])

                    now_warsaw_dt = datetime.now(WARSAW_TZ)
                    now_warsaw_iso = now_warsaw_dt.isoformat()

                    features = []
                    stations_live_batch = []
                    snapshots_delta_batch = []

                    for st in stations:
                        s_id = str(st.get("id"))
                        s_name = str(st.get("name", "Stacja"))
                        current_bikes = int(st.get("free_bikes") or 0)
                        empty_slots = int(st.get("empty_slots") or 0)
                        lat = st.get("latitude")
                        lng = st.get("longitude")

                        lat_val = float(lat) if lat is not None else 0.0
                        lon_val = float(lng) if lng is not None else 0.0
                        capacity_val = current_bikes + empty_slots

                        # Każda poprawna stacja trafia do aktualizacji stanu bieżącego dla leaderboardu
                        stations_live_batch.append((
                            s_id, s_name, lat_val, lon_val, capacity_val, current_bikes, empty_slots
                        ))

                        # Tylko zmiana stanu generuje wpis historyczny do tabeli snapshotów
                        if s_id not in LAST_KNOWN_BIKES or LAST_KNOWN_BIKES[s_id] != current_bikes:
                            snapshots_delta_batch.append((s_id, now_warsaw_dt, current_bikes))
                            LAST_KNOWN_BIKES[s_id] = current_bikes

                        # Budowanie GeoJSON dla warstwy mapy
                        if lat is not None and lng is not None:
                            total = current_bikes + empty_slots
                            pct = round((current_bikes / total) * 100, 1) if total > 0 else 0.0

                            features.append({
                                "type": "Feature",
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [lon_val, lat_val],
                                },
                                "properties": {
                                    "id": s_id,
                                    "name": s_name,
                                    "free_bikes": current_bikes,
                                    "empty_slots": empty_slots,
                                    "occupancy_pct": pct,
                                    "updated_at": now_warsaw_iso,
                                },
                            })

                    # Zapis do PostgreSQL
                    if stations_live_batch:
                        await save_snapshots_to_postgres(stations_live_batch, snapshots_delta_batch)

                    # Aktualizacja cache GeoJSON dla endpointa /bikes/citybikes/warsaw
                    geojson_payload = {
                        "type": "FeatureCollection",
                        "system_name": network_data.get("name", "VETURILO 3.0"),
                        "total_stations": len(features),
                        "last_update": now_warsaw_iso,
                        "features": features,
                    }
                    set_cached_stations(geojson_payload)

                elif res.status_code == 429:
                    retry_after = res.headers.get("Retry-After")
                    wait_time = int(retry_after) if retry_after and retry_after.isdigit() else 120
                    logger.warning(f"[BIKES] 429 Rate Limit z CityBikes. Pauza na {wait_time}s...")
                    backoff_delay = wait_time
                else:
                    logger.warning(f"[BIKES] CityBikes API status: {res.status_code}. Ponawiam za 60s.")
                    backoff_delay = 60

            except asyncio.CancelledError:
                logger.info("[BIKES] Worker Veturilo zatrzymany pomyślnie.")
                break
            except Exception as e:
                logger.warning(f"[BIKES] Błąd w pętli Veturilo: {e}")
                backoff_delay = 60

            await asyncio.sleep(backoff_delay)