import os
import httpx
import logging
from core.database import get_pool

logger = logging.getLogger(__name__)

API_KEY = os.getenv("ZTM_API_KEY") or os.getenv("W  AW_API_KEY")

DICT_URL = "https://api.um.warszawa.pl/api/action/public_transport_dictionary/"
ROUTES_URL = "https://api.um.warszawa.pl/api/action/public_transport_routes/"
STOPS_URL = "https://api.um.warszawa.pl/api/action/dbtimetable_get"
STOPS_RESOURCE_ID = "ab75c33d-3a26-4342-b36a-6e5fef0a3ac3"


async def sync_all_ztm_data() -> dict:
    """Pobiera i aktualizuje w bazie komplet oficjalnych słowników ZTM."""
    if not API_KEY:
        raise ValueError("Brak klucza ZTM_API_KEY!")

    pool = get_pool()
    summary = {}

    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        # 1. Słownik pojęć (ulice, typy, dzielnice)
        logger.info("Pobieranie public_transport_dictionary...")
        resp_dict = await client.get(DICT_URL, params={"apikey": API_KEY})
        if resp_dict.status_code == 200:
            dict_data = resp_dict.json().get("result", {})
            dict_records = []
            
            # Parsowanie struktury słownika (zbiory pojęć)
            for category, items in dict_data.items():
                if isinstance(items, dict):
                    for item_id, item_val in items.items():
                        name = item_val if isinstance(item_val, str) else str(item_val.get("nazwa", ""))
                        dict_records.append((category, str(item_id).strip(), name.strip()))

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
                """, dict_records)
            summary["dictionaries"] = len(dict_records)

        # 2. Słownik tras (tram_route_patterns)
        logger.info("Pobieranie public_transport_routes...")
        resp_routes = await client.get(ROUTES_URL, params={"apikey": API_KEY})
        if resp_routes.status_code == 200:
            routes_data = resp_routes.json().get("result", {})
            route_records = []
            for line, variants in routes_data.items():
                if not line.isdigit() or int(line) >= 100:
                    continue
                for var_name, stops in variants.items():
                    for seq, sinfo in stops.items():
                        c_id = str(sinfo.get("nr_zespolu", "")).strip()
                        s_nr = str(sinfo.get("nr_przystanku", "")).strip()
                        route_records.append((
                            line, var_name, int(seq), c_id, s_nr, f"{c_id}{s_nr}",
                            str(sinfo.get("ulica_id", "")).strip(),
                            int(sinfo.get("odleglosc", 0)),
                            int(sinfo.get("typ", 0))
                        ))
            
            async with pool.acquire() as conn:
                await conn.executemany("""
                    INSERT INTO tram_route_patterns (
                        line, route_variant, stop_sequence, cluster_id, stop_nr, stop_tag, street_id, distance_m, stop_type, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
                    ON CONFLICT (line, route_variant, stop_sequence) DO UPDATE SET
                        distance_m = EXCLUDED.distance_m,
                        stop_type = EXCLUDED.stop_type,
                        street_id = EXCLUDED.street_id,
                        updated_at = CURRENT_TIMESTAMP;
                """, route_records)
            summary["route_points"] = len(route_records)

        # 3. Słownik słupków i współrzędnych (tram_stops_dictionary)
        logger.info("Pobieranie tram_stops_dictionary...")
        resp_stops = await client.get(STOPS_URL, params={"id": STOPS_RESOURCE_ID, "apikey": API_KEY})
        if resp_stops.status_code == 200:
            raw_stops = resp_stops.json().get("result", [])
            stop_records = []
            for item in raw_stops:
                vm = {entry["key"]: entry["value"] for entry in item.get("values", [])}
                cid, snr, name = vm.get("zespol"), vm.get("slupek"), vm.get("nazwa_zespolu")
                lat, lon = vm.get("szer_geo"), vm.get("dlug_geo")
                if cid and snr and lat and lon:
                    try:
                        stop_records.append((cid, snr, f"{cid}{snr}", name, float(lon), float(lat)))
                    except ValueError:
                        continue

            async with pool.acquire() as conn:
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
                """, stop_records)
            summary["stops"] = len(stop_records)

    return summary