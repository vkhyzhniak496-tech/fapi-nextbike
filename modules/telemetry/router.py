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