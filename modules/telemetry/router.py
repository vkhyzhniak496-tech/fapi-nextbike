import json
from typing import Any, Dict
from fastapi import APIRouter, Response

from core.database import get_pool

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])


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
            # Frontend obsługuje zarówno string jak i tablicę obiektów
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