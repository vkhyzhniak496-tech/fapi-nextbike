from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Response
import httpx

from config import HTTP_HEADERS
from core.database import BIKES_DB_PATH, get_db_cursor
from core.models import Station
from modules.bikes.charts import generate_step_chart_bytes
from modules.bikes.worker import CITYBIKES_WARSAW_URL
from modules.bikes.storage import get_cached_stations

router = APIRouter(tags=["Veturilo Bikeshare & Analytics"])


# --- 1. GEOJSON DLA MAPY ---

@router.get("/bikes/citybikes/warsaw")
@router.get("/bikes/warsaw", include_in_schema=False)
async def get_warsaw_bikes_live():
    """Zwraca stan wszystkich stacji z pamięci podręcznej RAM."""
    cached = get_cached_stations()
    if cached:
        return cached
    return {
        "type": "FeatureCollection",
        "system_name": "VETURILO 3.0",
        "total_stations": 0,
        "features": []
    }


# --- 2. RANKING HUBÓW ---

@router.get("/bikes/leaderboard")
@router.get("/analytics/leaderboard", include_in_schema=False)
async def get_stations_leaderboard(top_n: int = 50):
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Błąd komunikacji z CityBikes")
        stations_raw = res.json().get("network", {}).get("stations", [])

    stations = [
        Station(
            id=str(st.get("id")),
            name=str(st.get("name", "Stacja")),
            lat=float(st.get("latitude") or 0.0),
            lng=float(st.get("longitude") or 0.0),
            free_bikes=int(st.get("free_bikes") or 0),
            empty_slots=int(st.get("empty_slots") or 0)
        )
        for st in stations_raw
    ]

    return {
        "total_active_stations": len(stations),
        "top_hubs": [s.model_dump() for s in sorted(stations, key=lambda x: x.free_bikes, reverse=True)[:top_n]],
        "top_overflow": [s.model_dump() for s in sorted(stations, key=lambda x: (x.occupancy_pct, x.free_bikes), reverse=True)[:top_n]]
    }


# --- 3. SERIE CZASOWE I DELTY Z ORYGINALNEJ TABELI ---

@router.get("/bikes/station/{station_id}/series")
@router.get("/analytics/series/{station_id}", include_in_schema=False)
def get_station_time_series(station_id: str, limit: Optional[int] = None):
    """Zwraca historię pomiarów z tabeli station_snapshots."""
    query = "SELECT timestamp, bikes FROM station_snapshots WHERE station_id = ? ORDER BY timestamp ASC"
    if limit:
        query += f" LIMIT {int(limit)}"

    with get_db_cursor(BIKES_DB_PATH) as cur:
        cur.execute(query, (station_id,))
        rows = cur.fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="Brak danych w bazie dla podanej stacji")

    return {
        "station_id": station_id,
        "points": len(rows),
        "data": [{"datetime": r["timestamp"], "bikes": r["bikes"]} for r in rows]
    }


@router.get("/analytics/events/{station_id}")
def get_station_deltas(station_id: str, start_time: str, end_time: str) -> List[Dict[str, Any]]:
    """Wyciąga wyłącznie punkty zmian liczby rowerów (+/-)."""
    query = """
        WITH calculated_deltas AS (
            SELECT 
                timestamp,
                bikes,
                bikes - LAG(bikes) OVER (ORDER BY timestamp) AS delta
            FROM station_snapshots
            WHERE station_id = ? AND timestamp BETWEEN ? AND ?
        )
        SELECT timestamp, bikes, delta
        FROM calculated_deltas
        WHERE delta IS NOT NULL AND delta != 0
        ORDER BY timestamp ASC;
    """
    with get_db_cursor(BIKES_DB_PATH) as cur:
        cur.execute(query, (station_id, start_time, end_time))
        return [dict(row) for row in cur.fetchall()]


# --- 4. WYKRES PNG (MATPLOTLIB) ---

@router.get("/bikes/station/{station_id}/chart.png")
@router.get("/analytics/station/{station_id}/chart.png", include_in_schema=False)
def get_station_chart_image(
    station_id: str,
    name: Optional[str] = None,
    limit: Optional[int] = None
):
    """Generuje wykres schodkowy na żywo z bazy SQLite."""
    query = "SELECT timestamp, bikes FROM station_snapshots WHERE station_id = ? ORDER BY timestamp ASC"
    if limit:
        query += f" LIMIT {int(limit)}"

    with get_db_cursor(BIKES_DB_PATH) as cur:
        cur.execute(query, (station_id,))
        rows = cur.fetchall()

    if not rows or len(rows) < 2:
        raise HTTPException(status_code=404, detail="Brak wystarczającej liczby danych do wykresu")

    series = [{"timestamp": r["timestamp"], "bikes": r["bikes"]} for r in rows]
    img_bytes = generate_step_chart_bytes(series, name or f"Stacja {station_id}")
    return Response(content=img_bytes, media_type="image/png")


