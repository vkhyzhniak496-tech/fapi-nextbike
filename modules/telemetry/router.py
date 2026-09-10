from typing import Optional
from fastapi import APIRouter

from core.database import TRAM_LIVE_DB_PATH, get_db_cursor
from modules.telemetry.worker import LAST_TRAM_POSITIONS

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])


router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry"])

@router.get("/live")
async def get_live_tram_positions(line: Optional[str] = None):
    """Błyskawicznie zwraca bieżące pozycje składów z pamięci RAM."""
    features = []
    target_line = line.strip() if line else None

    for v_num, state in LAST_TRAM_POSITIONS.items():
        lat, lon, gps_dt, speed, v_line, brigade = state

        if target_line and v_line != target_line:
            continue

        features.append({
            "type": "Feature",
            "id": v_num,
            "properties": {
                "vehicle_number": v_num,
                "line": v_line,
                "brigade": brigade,
                "speed_kmh": speed,
                "time": gps_dt.strftime("%Y-%m-%d %H:%M:%S")
            },
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat]
            }
        })

    return {
        "type": "FeatureCollection",
        "total_active_trams": len(features),
        "features": features
    }

@router.get("/live")
async def get_live_tram_positions(line: Optional[str] = None):
    """Błyskawicznie zwraca bieżące pozycje wszystkich składów z pamięci RAM."""
    features = []
    target_line = line.strip() if line else None

    for v_num, state in LAST_TRAM_POSITIONS.items():
        lat, lon, gps_dt, speed, v_line, brigade = state

        if target_line and v_line != target_line:
            continue

        features.append({
            "type": "Feature",
            "id": v_num,
            "properties": {
                "vehicle_number": v_num,
                "line": v_line,
                "brigade": brigade,
                "speed_kmh": speed,
                "time": gps_dt.strftime("%Y-%m-%d %H:%M:%S")
            },
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat]
            }
        })

    return {
        "type": "FeatureCollection",
        "total_active_trams": len(features),
        "features": features
    }


@router.get("/vehicles/{vehicle_number}/track")
async def get_vehicle_track(
    vehicle_number: str,
    limit: int = 250
):
    """
    Zwraca zarejestrowany ślad (LineString) oraz wszystkie punkty pomiarowe (Point)
    dla konkretnego numeru taborowego z bazy historii SQLite.
    """
    v_num = vehicle_number.strip()
    with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
        cur.execute("""
            SELECT line, brigade, lat, lon, speed_kmh, gps_time
            FROM tram_telemetry_history
            WHERE vehicle_number = ?
            ORDER BY id ASC
            LIMIT ?;
        """, (v_num, limit))
        rows = cur.fetchall()

    if not rows:
        return {
            "vehicle_number": v_num,
            "samples_count": 0,
            "features": []
        }

    # 1. Punkty trajektorii trasy
    coordinates = [[r["lon"], r["lat"]] for r in rows]

    track_line_feature = {
        "type": "Feature",
        "properties": {
            "type": "track_line",
            "vehicle_number": v_num,
            "line": rows[-1]["line"],
            "brigade": rows[-1]["brigade"]
        },
        "geometry": {
            "type": "LineString",
            "coordinates": coordinates
        }
    }

    # 2. Poszczególne punkty pomiarowe z prędkościami
    sample_points = [
        {
            "type": "Feature",
            "properties": {
                "type": "sample_point",
                "speed_kmh": r["speed_kmh"],
                "time": r["gps_time"],
                "line": r["line"],
                "brigade": r["brigade"]
            },
            "geometry": {
                "type": "Point",
                "coordinates": [r["lon"], r["lat"]]
            }
        }
        for r in rows
    ]

    return {
        "type": "FeatureCollection",
        "vehicle_number": v_num,
        "current_line": rows[-1]["line"],
        "current_brigade": rows[-1]["brigade"],
        "samples_count": len(rows),
        "features": [track_line_feature] + sample_points
    }