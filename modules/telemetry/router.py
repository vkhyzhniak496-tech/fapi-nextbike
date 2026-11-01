import logging
from typing import Any, Dict
from fastapi import APIRouter, Response
from core.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])


@router.get("/live")
async def get_live_trams(response: Response) -> Dict[str, Any]:
    """
    Zwraca bieżącą pozycję tramwajów na torach w formacie GeoJSON FeatureCollection.
    Generowane bezpośrednio przez silnik PostGIS.
    """
    query = """
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'generator', 'postgis-tram-snapper',
            'total_active_trams', COUNT(f),
            'features', COALESCE(json_agg(f.feature), '[]'::json)
        )::text AS geojson
        FROM (
            SELECT json_build_object(
                'type', 'Feature',
                'id', vehicle_number,
                'geometry', ST_AsGeoJSON(ST_Transform(snapped_geom_2180, 4326))::json,
                'properties', json_build_object(
                    'vehicle_number', vehicle_number,
                    'line', line,
                    'brigade', brigade,
                    'speed_kmh', speed_kmh,
                    'dist_to_track', dist_to_track,
                    'gps_time', to_char(gps_time, 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                )
            ) AS feature
            FROM tram_live_state
            WHERE updated_at > NOW() - INTERVAL '5 minutes'
        ) f;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Content-Type"] = "application/json"
    return Response(content=geojson_str, media_type="application/json")


@router.get("/dwells/all-stops")
async def get_all_stops_dwell_stats(response: Response) -> Response:
    """
    Agreguje postoje tramwajowe per peron pod warstwę analityczną mapy (GeoJSON).
    """
    query = """
        WITH per_line AS (
            SELECT 
                stop_osm_id,
                line,
                COUNT(*) AS line_samples,
                ROUND(AVG(duration_sec)::numeric, 1) AS line_avg_dwell
            FROM tram_dwell_events
            GROUP BY stop_osm_id, line
        ),
        lines_agg AS (
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
            SELECT 
                stop_osm_id,
                stop_name,
                cluster_name,
                ROUND(AVG(duration_sec)::numeric, 1) AS avg_dwell,
                COUNT(*) AS total_samples
            FROM tram_dwell_events
            GROUP BY stop_osm_id, stop_name, cluster_name
        )
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(json_agg(f.feature), '[]'::json)
        )::text AS geojson
        FROM (
            SELECT json_build_object(
                'type', 'Feature',
                'id', p.osm_id,
                'geometry', ST_AsGeoJSON(ST_Transform(p.geom_2180, 4326))::json,
                'properties', json_build_object(
                    'stop_name', st.stop_name,
                    'cluster_name', COALESCE(st.cluster_name, p.cluster_name, st.stop_name),
                    'avg_dwell_sec', st.avg_dwell,
                    'samples_count', st.total_samples,
                    'lines', la.lines_json
                )
            ) AS feature
            FROM stop_totals st
            JOIN lines_agg la ON la.stop_osm_id = st.stop_osm_id
            JOIN tram_platforms p ON p.osm_id = st.stop_osm_id
            WHERE p.geom_2180 IS NOT NULL
        ) f;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return Response(content=geojson_str or '{"type":"FeatureCollection","features":[]}', media_type="application/json")