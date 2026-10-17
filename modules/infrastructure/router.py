import json
from typing import Any, Dict, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel

from config import DEFAULT_WARSAW_BBOX
from core.database import get_pool
from modules.infrastructure.overpass import (
    sync_cycleways_task,
    sync_tram_platforms_task,
    sync_tram_tracks_task,
)

router = APIRouter(prefix="/network", tags=["Infrastructure Network GIS"])


class SyncCustomRequest(BaseModel):
    bbox: Optional[str] = None


# ==============================================================================
# 1. TRASY ROWEROWE
# ==============================================================================

@router.get("/safe-cycleways")
async def get_safe_cycleways(response: Response) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                way_id,
                properties_json,
                CASE 
                    -- 1. DDR / CPR
                    WHEN (properties_json->>'highway' = 'cycleway')
                         OR (properties_json->>'highway' IN ('path', 'footway') AND properties_json->>'bicycle' = 'designated')
                    THEN 'ddr'
                    
                    -- 2. Pas rowerowy / kontrapas / buspas
                    WHEN (properties_json->>'cycleway' IN ('lane', 'opposite_lane', 'share_busway', 'track', 'opposite_track', 'yes'))
                         OR (properties_json->>'cycleway:left' IN ('lane', 'opposite_lane', 'share_busway', 'track', 'yes'))
                         OR (properties_json->>'cycleway:right' IN ('lane', 'opposite_lane', 'share_busway', 'track', 'yes'))
                         OR (properties_json->>'cycleway:both' IN ('lane', 'opposite_lane', 'share_busway', 'track', 'yes'))
                         OR (properties_json->>'oneway:bicycle' = 'no')
                         OR (properties_json->>'cycleway:opposite' = 'yes')
                         OR (properties_json ? 'cycleway')
                         OR (properties_json ? 'cycleway:right')
                         OR (properties_json ? 'cycleway:left')
                    THEN 'lane'
                    
                    -- 3. Woonerf / Strefa zamieszkania
                    WHEN properties_json->>'highway' = 'living_street'
                    THEN 'woonerf'
                    
                    -- 4. Tempo 30 / Ulica uspokojona
                    ELSE 'tempo30'
                END AS infra_type,
                ST_AsGeoJSON(geom)::json AS geometry
            FROM cycleway_edges
            WHERE geom IS NOT NULL;
        """)

    if not rows:
        raise HTTPException(status_code=404, detail="Baza tras rowerowych jest pusta.")

    features = []
    for r in rows:
        props = r["properties_json"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = {}
        elif not isinstance(props, dict):
            props = {}

        # Wstrzykujemy pewną, przeliczoną w bazie kategorię
        props["infra_type"] = r["infra_type"]

        geom = r["geometry"]
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                geom = None

        if geom:
            features.append({
                "type": "Feature",
                "id": r["way_id"],
                "properties": props,
                "geometry": geom,
            })

    response.headers["Cache-Control"] = "no-cache"
    return {
        "type": "FeatureCollection",
        "generator": "postgis-cycleways-engine",
        "total_ways": len(features),
        "features": features,
    }


@router.post("/safe-cycleways/sync")
async def trigger_cycleways_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
    """Pobiera trasy rowerowe z Overpass API i zapisuje bezpośrednio do PostGIS."""
    bbox = payload.bbox if payload and payload.bbox else None
    background_tasks.add_task(sync_cycleways_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja dróg rowerowych do PostGIS uruchomiona w tle.",
        "target": bbox if bbox else "Cała Warszawa (4 kafelki)",
    }


# ==============================================================================
# 2. TOROWISKA TRAMWAJOWE
# ==============================================================================

@router.get("/tram")
@router.get("/tram/tracks")
async def get_tram_network(response: Response) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                way_id,
                length_m,
                properties_json,
                COALESCE((properties_json->>'maxspeed')::integer, 50) AS maxspeed,
                ST_AsGeoJSON(geom)::json AS geometry
            FROM tram_edges;
        """)

    if not rows:
        raise HTTPException(status_code=404, detail="Baza torowisk jest pusta.")

    features = []
    for r in rows:
        props = r["properties_json"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                props = {}
        elif not isinstance(props, dict):
            props = {}

        props["length_m"] = r["length_m"]
        props["maxspeed"] = r["maxspeed"]

        geom = r["geometry"]
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                geom = None

        if geom:
            features.append({
                "type": "Feature",
                "id": r["way_id"],
                "properties": props,
                "geometry": geom,
            })

    response.headers["Cache-Control"] = "no-cache"
    return {
        "type": "FeatureCollection",
        "generator": "postgis-tram-engine",
        "total_tracks": len(features),
        "features": features,
    }


@router.post("/tram/sync")
async def trigger_tram_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
    """Pobiera sieć torowisk z Overpass API i aktualizuje PostGIS."""
    bbox = (
        payload.bbox.strip()
        if payload and payload.bbox and payload.bbox.strip() not in ("", "string")
        else DEFAULT_WARSAW_BBOX
    )
    background_tasks.add_task(sync_tram_tracks_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja torowisk tramwajowych do PostGIS uruchomiona w tle.",
        "target": bbox,
    }


# ==============================================================================
# 3. PERONY I SŁUPKI TRAMWAJOWE
# ==============================================================================

@router.get("/tram/platforms")
async def get_tram_platforms(response: Response) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT 
                osm_id,
                name,
                cluster_name,
                ST_AsGeoJSON(geom)::json AS geometry
            FROM tram_platforms
            WHERE geom IS NOT NULL;
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
                "id": r["osm_id"],
                "properties": {
                    "name": r["name"],
                    "cluster_name": r["cluster_name"],
                },
                "geometry": geom,
            })

    response.headers["Cache-Control"] = "no-cache"
    return {
        "type": "FeatureCollection",
        "generator": "postgis-platforms-engine",
        "total_platforms": len(features),
        "features": features,
    }


@router.post("/tram/platforms/sync")
async def trigger_platforms_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
    """Pobiera słupki tramwajowe z OSM i aktualizuje PostGIS."""
    bbox = (
        payload.bbox.strip()
        if payload and payload.bbox and payload.bbox.strip() not in ("", "string")
        else DEFAULT_WARSAW_BBOX
    )
    background_tasks.add_task(sync_tram_platforms_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja peronów tramwajowych do PostGIS uruchomiona w tle.",
        "target": bbox,
    }