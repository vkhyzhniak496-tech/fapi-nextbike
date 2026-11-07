import logging
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel

from config import DEFAULT_WARSAW_BBOX
from core.database import get_pool
from modules.infrastructure.overpass import (
    sync_cycleways_task,
    sync_tram_platforms_task,
    sync_tram_tracks_task,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/network", tags=["Infrastructure Network GIS"])


class SyncCustomRequest(BaseModel):
    bbox: Optional[str] = None


@router.get("/safe-cycleways")
async def get_safe_cycleways(response: Response) -> Response:
    """Zwraca sklasyfikowane trasy rowerowe w natywnym GeoJSON prosto z PostGIS."""
    query = """
    WITH classified AS (
        SELECT 
            way_id,
            properties_json,
            CASE 
                WHEN (properties_json->>'highway' = 'cycleway')
                     OR (properties_json->>'highway' IN ('path', 'footway', 'pedestrian') 
                         AND properties_json->>'bicycle' = 'designated')
                THEN 'ddr'

                WHEN (
                        properties_json->>'cycleway' IN ('lane', 'opposite_lane', 'track', 'opposite_track')
                        OR properties_json->>'cycleway:left' IN ('lane', 'opposite_lane', 'track')
                        OR properties_json->>'cycleway:right' IN ('lane', 'opposite_lane', 'track')
                        OR properties_json->>'cycleway:both' IN ('lane', 'opposite_lane', 'track')
                        OR properties_json->>'oneway:bicycle' = 'no'
                        OR properties_json->>'cycleway:opposite' = 'yes'
                     )
                     AND COALESCE((properties_json->>'maxspeed')::integer, 50) <= 50
                     AND properties_json->>'highway' NOT IN ('motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary_link')
                     AND COALESCE(properties_json->>'busway', '') NOT LIKE '%lane%'
                     AND COALESCE(properties_json->>'cycleway', '') != 'share_busway'
                THEN 'lane'

                WHEN properties_json->>'highway' = 'living_street'
                THEN 'woonerf'

                WHEN (
                        COALESCE((properties_json->>'maxspeed')::integer, 50) <= 30
                        OR properties_json->>'zone:maxspeed' = 'PL:zone30'
                        OR properties_json->>'highway' = 'residential'
                     )
                     AND properties_json->>'highway' NOT IN ('motorway', 'trunk', 'primary', 'primary_link', 'secondary_link')
                THEN 'tempo30'

                ELSE NULL
            END AS infra_type,
            geom
        FROM cycleway_edges
        WHERE geom IS NOT NULL
          AND properties_json->>'highway' NOT IN ('motorway', 'motorway_link')
    )
    SELECT json_build_object(
        'type', 'FeatureCollection',
        'generator', 'postgis-cycleways-engine',
        'features', COALESCE(json_agg(
            json_build_object(
                'type', 'Feature',
                'id', way_id,
                'properties', properties_json || jsonb_build_object('infra_type', infra_type),
                'geometry', ST_AsGeoJSON(geom)::json
            )
        ), '[]'::json)
    )::text
    FROM classified
    WHERE infra_type IS NOT NULL;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    if not geojson_str:
        raise HTTPException(status_code=404, detail="Baza tras rowerowych jest pusta.")

    response.headers["Cache-Control"] = "public, max-age=3600"
    return Response(content=geojson_str, media_type="application/json")


@router.post("/safe-cycleways/sync")
async def trigger_cycleways_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
    bbox = payload.bbox if payload and payload.bbox else None
    background_tasks.add_task(sync_cycleways_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja dróg rowerowych uruchomiona w tle.",
        "target": bbox if bbox else "Cała Warszawa (4 kafelki)",
    }


@router.get("/tram")
@router.get("/tram/tracks")
async def get_tram_network(response: Response) -> Response:
    """Zwraca sieć torowisk w formacie GeoJSON wygenerowaną bezpośrednio w PostGIS."""
    query = """
    SELECT json_build_object(
        'type', 'FeatureCollection',
        'generator', 'postgis-tram-engine',
        'features', COALESCE(json_agg(
            json_build_object(
                'type', 'Feature',
                'id', way_id,
                'properties', properties_json || jsonb_build_object(
                    'length_m', length_m,
                    'maxspeed', COALESCE((properties_json->>'maxspeed')::integer, 50)
                ),
                'geometry', ST_AsGeoJSON(geom)::json
            )
        ), '[]'::json)
    )::text
    FROM tram_edges;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    response.headers["Cache-Control"] = "public, max-age=3600"
    return Response(content=geojson_str or '{"type":"FeatureCollection","features":[]}', media_type="application/json")


@router.post("/tram/sync")
async def trigger_tram_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
    bbox = payload.bbox.strip() if payload and payload.bbox else DEFAULT_WARSAW_BBOX
    background_tasks.add_task(sync_tram_tracks_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja torowisk uruchomiona w tle.",
        "target": bbox,
    }


@router.get("/tram/platforms")
async def get_tram_platforms(response: Response) -> Response:
    """Zwraca perony tramwajowe w GeoJSON bezpośrednio z PostGIS."""
    query = """
    SELECT json_build_object(
        'type', 'FeatureCollection',
        'generator', 'postgis-platforms-engine',
        'features', COALESCE(json_agg(
            json_build_object(
                'type', 'Feature',
                'id', osm_id,
                'properties', json_build_object(
                    'name', name,
                    'cluster_name', cluster_name
                ),
                'geometry', ST_AsGeoJSON(geom)::json
            )
        ), '[]'::json)
    )::text
    FROM tram_platforms
    WHERE geom IS NOT NULL;
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        geojson_str = await conn.fetchval(query)

    response.headers["Cache-Control"] = "public, max-age=3600"
    return Response(content=geojson_str or '{"type":"FeatureCollection","features":[]}', media_type="application/json")


@router.post("/tram/platforms/sync")
async def trigger_platforms_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
    bbox = payload.bbox.strip() if payload and payload.bbox else DEFAULT_WARSAW_BBOX
    background_tasks.add_task(sync_tram_platforms_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja peronów tramwajowych uruchomiona w tle.",
        "target": bbox,
    }