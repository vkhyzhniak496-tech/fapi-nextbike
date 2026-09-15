from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Query
from pydantic import BaseModel

from core.database import (
    TRAM_ANALYTICS_DB_PATH,
    TRAM_LIVE_DB_PATH,
    get_db_cursor,
)
from core.models import StopDwellStats, TramDwellEvent
from modules.telemetry.worker import LAST_TRAM_POSITIONS

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])


@router.get("/live")
async def get_live_tram_positions(line: Optional[str] = None) -> Dict[str, Any]:
    """Zwraca bieżące pozycje składów z pamięci RAM w formacie GeoJSON FeatureCollection."""
    features = []
    target_line = line.strip() if line else None

    for v_num, telemetry in LAST_TRAM_POSITIONS.items():
        if target_line and telemetry.line != target_line:
            continue

        features.append({
            "type": "Feature",
            "id": v_num,
            "properties": {
                "vehicle_number": telemetry.vehicle_number,
                "line": telemetry.line,
                "brigade": telemetry.brigade,
                "speed_kmh": telemetry.speed_kmh,
                "time": telemetry.gps_time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [telemetry.lon, telemetry.lat],
            },
        })

    return {
        "type": "FeatureCollection",
        "total_active_trams": len(features),
        "features": features,
    }


@router.get("/vehicles/{vehicle_number}/track")
async def get_vehicle_track(
    vehicle_number: str, limit: int = Query(default=300, ge=10, le=2000)
) -> Dict[str, Any]:
    """Zwraca trajektorię przejazdu (LineString) oraz próbki punktowe dla danego wozu."""
    v_num = vehicle_number.strip()

    with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
        cur.execute(
            """
            SELECT line, brigade, lat, lon, speed_kmh, gps_time
            FROM tram_telemetry_history
            WHERE vehicle_number = ?
            ORDER BY gps_time ASC
            LIMIT ?;
            """,
            (v_num, limit),
        )
        rows = cur.fetchall()

    if not rows:
        return {
            "type": "FeatureCollection",
            "vehicle_number": v_num,
            "samples_count": 0,
            "features": [],
        }

    coordinates = [[r["lon"], r["lat"]] for r in rows]

    track_line_feature = {
        "type": "Feature",
        "properties": {
            "type": "track_line",
            "vehicle_number": v_num,
            "line": rows[-1]["line"],
            "brigade": rows[-1]["brigade"],
        },
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }

    sample_points = [
        {
            "type": "Feature",
            "properties": {
                "type": "sample_point",
                "speed_kmh": r["speed_kmh"],
                "time": r["gps_time"],
                "line": r["line"],
                "brigade": r["brigade"],
            },
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
        }
        for r in rows
    ]

    return {
        "type": "FeatureCollection",
        "vehicle_number": v_num,
        "current_line": rows[-1]["line"],
        "current_brigade": rows[-1]["brigade"],
        "samples_count": len(rows),
        "features": [track_line_feature] + sample_points,
    }


@router.get("/dwells/vehicle/{vehicle_number}", response_model=List[TramDwellEvent])
async def get_vehicle_dwell_events(
    vehicle_number: str, limit: int = Query(default=100, ge=1, le=500)
):
    """Zwraca listę zarejestrowanych postojów na peronach dla wskazanego wozu."""
    v_num = vehicle_number.strip()
    with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
        cur.execute(
            """
            SELECT vehicle_number, line, brigade, stop_name, cluster_name,
                   arrival_time, departure_time, duration_sec, min_speed_kmh,
                   min_dist_m, pings_count
            FROM tram_dwell_events
            WHERE vehicle_number = ?
            ORDER BY arrival_time DESC
            LIMIT ?;
            """,
            (v_num, limit),
        )
        rows = cur.fetchall()

    return [TramDwellEvent(**dict(r)) for r in rows]


@router.get("/dwells/line/{line}/stats")
async def get_line_dwell_stats(line: str) -> Dict[str, Any]:
    """Zwraca statystyki czasów wymiany pasażerskiej dla przystanków na danej linii w formacie GeoJSON."""
    line_clean = line.strip()

    with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
        cur.execute(
            """
            SELECT 
                d.stop_name,
                d.cluster_name,
                ROUND(AVG(d.duration_sec), 1) AS avg_dwell_sec,
                COUNT(*) AS samples_count,
                SUM(CASE WHEN d.min_speed_kmh > 3.0 THEN 1 ELSE 0 END) AS slow_passes_count
            FROM tram_dwell_events d
            WHERE d.line = ?
            GROUP BY d.stop_name, d.cluster_name
            ORDER BY samples_count DESC;
            """,
            (line_clean,),
        )
        dwell_rows = cur.fetchall()

    features = []
    for r in dwell_rows:
        features.append({
            "type": "Feature",
            "properties": {
                "stop_name": r["stop_name"],
                "cluster_name": r["cluster_name"],
                "avg_dwell_sec": r["avg_dwell_sec"],
                "samples_count": r["samples_count"],
                "slow_passes_count": r["slow_passes_count"],
            },
            # Współrzędne peronu można pobrać przez JOIN z tram_platforms
            "geometry": None,
        })

    return {
        "type": "FeatureCollection",
        "line": line_clean,
        "stops_count": len(features),
        "features": features,
    }