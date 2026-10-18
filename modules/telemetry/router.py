import os
import logging
from typing import Any, Dict
from fastapi import APIRouter, BackgroundTasks, Response, HTTPException, status
import httpx

from core.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])

WAW_API_KEY = os.getenv("WAW_API_KEY") or os.getenv("API_KEY")
ROUTES_URL = "https://api.um.warszawa.pl/api/action/public_transport_routes/"
STOPS_URL = "https://api.um.warszawa.pl/api/action/dbtimetable_get"
DICT_URL = "https://api.um.warszawa.pl/api/action/public_transport_dictionary/"
STOPS_RESOURCE_ID = "ab75c33d-3a26-4342-b36a-6e5fef0a3ac3"


async def run_ztm_dictionary_sync():
    """Zadanie w tle: pobiera słowniki ZTM i aktualizuje tabele w PostgreSQL."""
    if not WAW_API_KEY:
        logger.error("Brak klucza WAW_API_KEY w środowisku (.env)!")
        return

    logger.info("Rozpoczynanie synchronizacji słowników ZTM w tle...")
    pool = get_pool()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True, headers=headers) as client:
        # 1. Słownik pojęć i ulic
        try:
            resp_dict = await client.get(DICT_URL, params={"apikey": WAW_API_KEY})
            if resp_dict.status_code == 200:
                dict_payload = resp_dict.json().get("result", {})
                dict_rows = []
                for cat, items in dict_payload.items():
                    if isinstance(items, dict):
                        for item_id, item_val in items.items():
                            name = item_val if isinstance(item_val, str) else str(item_val.get("nazwa", ""))
                            dict_rows.append((cat, str(item_id).strip(), name.strip()))

                if dict_rows:
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            CREATE TABLE IF NOT EXISTS ztm_dictionary_entries (
                                category TEXT NOT NULL,
                                item_id TEXT NOT NULL,
                                name TEXT NOT NULL,
                                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                                PRIMARY KEY (category, item_id)
                            );
                        """)
                        await conn.executemany("""
                            INSERT INTO ztm_dictionary_entries (category, item_id, name, updated_at)
                            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                            ON CONFLICT (category, item_id) DO UPDATE SET
                                name = EXCLUDED.name,
                                updated_at = CURRENT_TIMESTAMP;
                        """, dict_rows)
                    logger.info(f"Zsynchronizowano {len(dict_rows)} wpisów w ztm_dictionary_entries.")
        except Exception as e:
            logger.error(f"Błąd podczas synchronizacji słownika pojęć: {e}")

        # 2. Oficjalne trasy (tram_route_patterns)
        try:
            resp_routes = await client.get(ROUTES_URL, params={"apikey": WAW_API_KEY})
            if resp_routes.status_code == 200:
                routes_data = resp_routes.json().get("result", {})
                route_rows = []
                for line, variants in routes_data.items():
                    if not line.isdigit() or int(line) >= 100:
                        continue
                    for var_name, stops in variants.items():
                        for seq, sinfo in stops.items():
                            cid = str(sinfo.get("nr_zespolu", "")).strip()
                            snr = str(sinfo.get("nr_przystanku", "")).strip()
                            route_rows.append((
                                line, var_name, int(seq), cid, snr, f"{cid}{snr}",
                                str(sinfo.get("ulica_id", "")).strip(),
                                int(sinfo.get("odleglosc", 0)),
                                int(sinfo.get("typ", 0))
                            ))

                if route_rows:
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            CREATE TABLE IF NOT EXISTS tram_route_patterns (
                                line TEXT NOT NULL,
                                route_variant TEXT NOT NULL,
                                stop_sequence INT NOT NULL,
                                cluster_id TEXT NOT NULL,
                                stop_nr TEXT NOT NULL,
                                stop_tag TEXT NOT NULL,
                                street_id TEXT,
                                distance_m INT NOT NULL,
                                stop_type INT NOT NULL,
                                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                                PRIMARY KEY (line, route_variant, stop_sequence)
                            );
                        """)
                        await conn.executemany("""
                            INSERT INTO tram_route_patterns (
                                line, route_variant, stop_sequence, cluster_id, stop_nr, stop_tag, street_id, distance_m, stop_type, updated_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
                            ON CONFLICT (line, route_variant, stop_sequence) DO UPDATE SET
                                distance_m = EXCLUDED.distance_m,
                                stop_type = EXCLUDED.stop_type,
                                street_id = EXCLUDED.street_id,
                                updated_at = CURRENT_TIMESTAMP;
                        """, route_rows)
                    logger.info(f"Zsynchronizowano {len(route_rows)} punktów w tram_route_patterns.")
        except Exception as e:
            logger.error(f"Błąd podczas synchronizacji tras: {e}")

        # 3. Słownik przystanków (tram_stops_dictionary)
        try:
            resp_stops = await client.get(STOPS_URL, params={"id": STOPS_RESOURCE_ID, "apikey": WAW_API_KEY})
            if resp_stops.status_code == 200:
                raw_stops = resp_stops.json().get("result", [])
                if isinstance(raw_stops, list):
                    stop_rows = []
                    for item in raw_stops:
                        vm = {entry["key"]: entry["value"] for entry in item.get("values", [])}
                        cid, snr, name = vm.get("zespol"), vm.get("slupek"), vm.get("nazwa_zespolu")
                        lat, lon = vm.get("szer_geo"), vm.get("dlug_geo")
                        if cid and snr and lat and lon:
                            try:
                                stop_rows.append((cid, snr, f"{cid}{snr}", name, float(lon), float(lat)))
                            except ValueError:
                                continue

                    if stop_rows:
                        async with pool.acquire() as conn:
                            await conn.execute("""
                                CREATE TABLE IF NOT EXISTS tram_stops_dictionary (
                                    cluster_id TEXT NOT NULL,
                                    stop_nr TEXT NOT NULL,
                                    stop_tag TEXT PRIMARY KEY,
                                    stop_name TEXT NOT NULL,
                                    lon DOUBLE PRECISION NOT NULL,
                                    lat DOUBLE PRECISION NOT NULL,
                                    geom_2180 GEOMETRY(Point, 2180),
                                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                                );
                            """)
                            await conn.executemany("""
                                INSERT INTO tram_stops_dictionary (
                                    cluster_id, stop_nr, stop_tag, stop_name, lon, lat, geom_2180, updated_at
                                ) VALUES (
                                    $1, $2, $3, $4, $5, $6,
                                    ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), 4326), 2180),
                                    CURRENT_TIMESTAMP
                                )
                                ON CONFLICT (stop_tag) DO UPDATE SET
                                    stop_name = EXCLUDED.stop_name,
                                    geom_2180 = EXCLUDED.geom_2180,
                                    updated_at = CURRENT_TIMESTAMP;
                            """, stop_rows)
                        logger.info(f"Zsynchronizowano {len(stop_rows)} słupków w tram_stops_dictionary.")
        except Exception as e:
            logger.error(f"Błąd podczas synchronizacji przystanków: {e}")

    logger.info("Synchronizacja słowników ZTM zakończona pomyślnie.")


@router.post("/sync/dictionaries", status_code=status.HTTP_202_ACCEPTED)
async def trigger_ztm_sync(background_tasks: BackgroundTasks) -> Dict[str, str]:
    """
    Ręczne lub cykliczne wywołanie aktualizacji oficjalnych tras i słowników ZTM.
    Zadanie uruchamia się asynchronicznie w tle (nie blokuje endpointu).
    """
    background_tasks.add_task(run_ztm_dictionary_sync)
    return {
        "status": "accepted",
        "message": "Zadanie synchronizacji oficjalnych tras, słowników i przystanków ZTM zostało uruchomione w tle."
    }