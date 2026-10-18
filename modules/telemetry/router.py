import os
import json
import logging
from pathlib import Path
from typing import Any, Dict
from fastapi import APIRouter, BackgroundTasks, Response, HTTPException, status
import httpx
from dotenv import load_dotenv

from core.database import get_pool

# Pewne ładowanie .env niezależnie od katalogu uruchomienia procesu
load_dotenv()

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])

WAW_API_KEY = os.getenv("WAW_API_KEY") or os.getenv("API_KEY")
ROUTES_URL = "https://api.um.warszawa.pl/api/action/public_transport_routes/"
STOPS_URL = "https://api.um.warszawa.pl/api/action/dbtimetable_get"
DICT_URL = "https://api.um.warszawa.pl/api/action/public_transport_dictionary/"
STOPS_RESOURCE_ID = "ab75c33d-3a26-4342-b36a-6e5fef0a3ac3"


# --- 1. ENDPOINTY OPERACYJNE / TELEMETRIA ---

@router.get("/live")
async def get_live_trams(response: Response) -> Dict[str, Any]:
    """
    Zwraca bieżącą pozycję wszystkich tramwajów zrzutowanych na tory (PostGIS)
    w standardzie GeoJSON FeatureCollection.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                vehicle_number,
                line,
                brigade,
                speed_kmh,
                dist_to_track,
                gps_time,
                ST_AsGeoJSON(ST_Transform(snapped_geom_2180, 4326))::json AS geometry
            FROM tram_live_state
            WHERE updated_at > NOW() - INTERVAL '5 minutes';
        """)

    features = []
    for r in rows:
        geom = r["geometry"]
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                geom = None

        if geom:
            features.append({
                "type": "Feature",
                "id": r["vehicle_number"],
                "properties": {
                    "vehicle_number": r["vehicle_number"],
                    "line": r["line"],
                    "brigade": r["brigade"],
                    "speed_kmh": r["speed_kmh"],
                    "dist_to_track": r["dist_to_track"],
                    "gps_time": r["gps_time"].isoformat() if r["gps_time"] else None,
                },
                "geometry": geom,
            })

    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return {
        "type": "FeatureCollection",
        "generator": "postgis-tram-snapper",
        "total_active_trams": len(features),
        "features": features,
    }


@router.get("/dwells/all-stops")
async def get_all_stops_dwell_stats(response: Response) -> Dict[str, Any]:
    """
    Agreguje sfinalizowane postoje z tram_dwell_events i łączy je
    z geometrią peronów tram_platforms pod interaktywny widok mapy.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH per_line AS (
                -- 1. Średnia i liczba prób per peron oraz linia
                SELECT 
                    stop_osm_id,
                    line,
                    COUNT(*) AS line_samples,
                    ROUND(AVG(duration_sec)::numeric, 1) AS line_avg_dwell
                FROM tram_dwell_events
                GROUP BY stop_osm_id, line
            ),
            lines_agg AS (
                -- 2. Złożenie tablicy obiektów linii pod popup w przeglądarce
                SELECT 
                    stop_osm_id,
                    json_agg(json_build_object(
                        'line', line,
                        'avg_dwell_sec', line_avg_dwell,
                        'samples', line_samples
                    ) ORDER BY line ASC) AS lines_json
                FROM per_line
                GROUP BY stop_osm_id
            ),
            stop_totals AS (
                -- 3. Łączna średnia i suma prób dla całego peronu
                SELECT 
                    stop_osm_id,
                    stop_name,
                    cluster_name,
                    ROUND(AVG(duration_sec)::numeric, 1) AS avg_dwell,
                    COUNT(*) AS total_samples
                FROM tram_dwell_events
                GROUP BY stop_osm_id, stop_name, cluster_name
            )
            SELECT 
                p.osm_id,
                st.stop_name,
                COALESCE(st.cluster_name, p.cluster_name, st.stop_name) AS cluster_name,
                st.avg_dwell,
                st.total_samples,
                la.lines_json,
                ST_AsGeoJSON(ST_Transform(p.geom_2180, 4326))::json AS geometry
            FROM stop_totals st
            JOIN lines_agg la ON la.stop_osm_id = st.stop_osm_id
            JOIN tram_platforms p ON p.osm_id = st.stop_osm_id
            WHERE p.geom_2180 IS NOT NULL
            ORDER BY st.total_samples DESC;
        """)

    features = []
    for r in rows:
        geom = r["geometry"]
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                geom = None

        if geom:
            lines_val = r["lines_json"]
            if isinstance(lines_val, str):
                try:
                    lines_val = json.loads(lines_val)
                except Exception:
                    lines_val = []

            features.append({
                "type": "Feature",
                "id": r["osm_id"],
                "geometry": geom,
                "properties": {
                    "stop_name": r["stop_name"],
                    "cluster_name": r["cluster_name"],
                    "avg_dwell_sec": float(r["avg_dwell"]),
                    "samples_count": int(r["total_samples"]),
                    "lines_json": lines_val,
                },
            })

    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return {
        "type": "FeatureCollection",
        "total_stops": len(features),
        "features": features,
    }


# --- 2. ZADANIA SYNCHRONIZACJI SŁOWNIKÓW ZTM ---

async def run_ztm_dictionary_sync():
    """Zadanie w tle: pobiera słowniki ZTM i aktualizuje tabele w PostgreSQL."""
    key = os.getenv("WAW_API_KEY") or os.getenv("API_KEY")
    if not key:
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
            resp_dict = await client.get(DICT_URL, params={"apikey": key})
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
            resp_routes = await client.get(ROUTES_URL, params={"apikey": key})
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
            resp_stops = await client.get(STOPS_URL, params={"id": STOPS_RESOURCE_ID, "apikey": key})
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
    Ręczne lub wywołane webhookiem odświeżenie tras i słowników ZTM.
    Zadanie wykonuje się asynchronicznie w tle.
    """
    background_tasks.add_task(run_ztm_dictionary_sync)
    return {
        "status": "accepted",
        "message": "Zadanie synchronizacji oficjalnych tras, słowników i przystanków ZTM zostało uruchomione w tle."
    }