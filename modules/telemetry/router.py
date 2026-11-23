import logging
from typing import Any, Dict
from fastapi import APIRouter, Response
from core.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])

@router.get("/live")
async def get_live_trams(response: Response) -> Response:
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
              AND snapped_geom_2180 IS NOT NULL
        ) f;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return Response(
        content=geojson_str or '{"type":"FeatureCollection","features":[]}', 
        media_type="application/json"
    )


@router.get("/dwells/all-stops")
async def get_all_stops_dwell_stats(response: Response) -> Response:
    """
    Zwraca zagregowane statystyki postojów tramwajowych z ostatniej godziny (okno 1h)
    per peron wraz z rozbiciem na linie oraz czasem aktualizacji updated_at.
    """
    query = """
        WITH per_stop AS (
            SELECT
                s.stop_osm_id,
                MAX(s.stop_name) AS stop_name,
                MAX(s.cluster_name) AS cluster_name,
                SUM(s.samples_count)::integer AS total_samples,
                ROUND(
                    (SUM(s.avg_dwell_sec * s.samples_count) / NULLIF(SUM(s.samples_count), 0))::numeric,
                    1
                )::double precision AS avg_dwell_sec,
                MAX(s.updated_at) AS updated_at,
                json_agg(
                    json_build_object(
                        'line', s.line,
                        'avg_dwell_sec', s.avg_dwell_sec,
                        'samples', s.samples_count
                    )
                    ORDER BY s.line ASC
                ) AS lines_json
            FROM tram_stop_dwell_stats s
            WHERE s.samples_count > 0
            GROUP BY s.stop_osm_id
        )
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'window_minutes', 60,
            'updated_at', to_char(MAX(ps.updated_at), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'features', COALESCE(json_agg(f.feature), '[]'::json)
        )::text AS geojson
        FROM per_stop ps
        JOIN tram_platforms p
          ON NULLIF(regexp_replace(p.osm_id, '\D', '', 'g'), '')::bigint = ps.stop_osm_id
        CROSS JOIN LATERAL (
            SELECT json_build_object(
                'type', 'Feature',
                'id', p.osm_id,
                'geometry', ST_AsGeoJSON(ST_Transform(p.geom_2180, 4326))::json,
                'properties', json_build_object(
                    'stop_name', ps.stop_name,
                    'cluster_name', COALESCE(NULLIF(TRIM(ps.cluster_name), ''), p.cluster_name, ps.stop_name),
                    'avg_dwell_sec', ps.avg_dwell_sec,
                    'samples_count', ps.total_samples,
                    'window_minutes', 60,
                    'updated_at', to_char(ps.updated_at, 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                    'lines', ps.lines_json
                )
            ) AS feature
        ) f
        WHERE p.geom_2180 IS NOT NULL;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return Response(
        content=geojson_str or '{"type":"FeatureCollection","features":[]}',
        media_type="application/json"
    )