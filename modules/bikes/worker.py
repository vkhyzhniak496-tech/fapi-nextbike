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
CITYBIKES_WARSAW_URL = (
    "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"
)

# Słownik w RAM filtrujący zbędne wpisy (zapisujemy do bazy tylko delty)
LAST_KNOWN_BIKES: Dict[str, int] = {}


async def save_snapshots_to_postgres(
    stations_meta: List[tuple], snapshots: List[tuple]
) -> None:
    """Zapis do tabeli słownikowej stacji oraz historii snapshotów."""
    if not snapshots and not stations_meta:
        return

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            # 1. SŁOWNIK STACJI (5 parametrów: id, name, lat, lon, capacity)
            if stations_meta:
                await conn.executemany(
                    """
                    INSERT INTO stations (station_id, name, lat, lon, capacity, updated_at)
                    VALUES ($1, $2, $3, $4, $5, CURRENT_TIMESTAMP)
                    ON CONFLICT (station_id) DO UPDATE SET 
                        name = EXCLUDED.name,
                        lat = COALESCE(EXCLUDED.lat, stations.lat),
                        lon = COALESCE(EXCLUDED.lon, stations.lon),
                        capacity = GREATEST(EXCLUDED.capacity, stations.capacity),
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    stations_meta,
                )

            # 2. SNAPSHOTY (3 parametry: id, timestamp, bikes)
            if snapshots:
                await conn.executemany(
                    """
                    INSERT INTO station_snapshots (station_id, timestamp, bikes)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (station_id, timestamp) DO NOTHING;
                    """,
                    snapshots,
                )
    except Exception as e:
        logger.error(f"[BIKES] Błąd zapisu snapshotów: {e}")


async def bike_history_poller_worker():
    """Worker odpytujący CityBikes, zasilający PostgreSQL i odświeżający GeoJSON."""
    backoff_delay = 45

    # Jeśli zrestartujesz aplikację, wyczyść pamięć podręczną w RAM,
    # aby przy pierwszym obiegu zaktualizować współrzędne wszystkich stacji:
    LAST_KNOWN_BIKES.clear()

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
    ) as client:
        while True:
            try:
                res = await client.get(CITYBIKES_WARSAW_URL)

                if res.status_code == 200:
                    backoff_delay = 45
                    network_data = res.json().get("network", {})
                    stations = network_data.get("stations", [])

                    # Obiekt datetime dla PostgreSQL i string ISO dla frontendu
                    now_warsaw_dt = datetime.now(WARSAW_TZ)
                    now_warsaw_iso = now_warsaw_dt.isoformat()

                    features = []
                    stations_meta_batch = []
                    snapshots_batch = []

                    for st in stations:
                        s_id = str(st.get("id"))
                        s_name = str(st.get("name", "Stacja"))
                        current_bikes = int(st.get("free_bikes") or 0)
                        empty_slots = int(st.get("empty_slots") or 0)
                        lat = st.get("latitude")
                        lng = st.get("longitude")

                        # Konwersja współrzędnych i wyliczenie pojemności stojaka
                        lat_val = float(lat) if lat is not None else 0.0
                        lon_val = float(lng) if lng is not None else 0.0
                        capacity_val = current_bikes + empty_slots

                        # 1. Wykrycie zmiany (delty) lub nowo napotkanej stacji
                        if (
                            s_id not in LAST_KNOWN_BIKES
                            or LAST_KNOWN_BIKES[s_id] != current_bikes
                        ):
                            # Krotka 5-elementowa ($1: id, $2: name, $3: lat, $4: lon, $5: capacity)
                            stations_meta_batch.append(
                                (s_id, s_name, lat_val, lon_val, capacity_val)
                            )
                            # Krotka 3-elementowa ($1: id, $2: timestamp, $3: bikes)
                            snapshots_batch.append((s_id, now_warsaw_dt, current_bikes))
                            LAST_KNOWN_BIKES[s_id] = current_bikes

                        # 2. Budowanie GeoJSON na potrzeby warstwy mapy
                        if lat is not None and lng is not None:
                            total = current_bikes + empty_slots
                            pct = round((current_bikes / total) * 100, 1) if total > 0 else 0.0

                            features.append({
                                "type": "Feature",
                                "geometry": {
                                    "type": "Point",
                                    "coordinates": [float(lng), float(lat)],
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
                    if snapshots_batch:
                        await save_snapshots_to_postgres(stations_meta_batch, snapshots_batch)

                    # Aktualizacja cache GeoJSON dla frontendu
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
                    wait_time = (
                        int(retry_after)
                        if retry_after and retry_after.isdigit()
                        else 120
                    )
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