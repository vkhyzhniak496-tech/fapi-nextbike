from typing import Any, Dict
from fastapi import APIRouter, HTTPException
import httpx

from config import HTTP_HEADERS
from core.models import Station
from modules.bikes.storage import get_cached_stations
from modules.bikes.worker import CITYBIKES_WARSAW_URL

router = APIRouter(tags=["Veturilo Bikeshare & Analytics"])


# --- 1. GEOJSON DLA MAPY ---

@router.get("/bikes/citybikes/warsaw")
@router.get("/bikes/warsaw", include_in_schema=False)
async def get_warsaw_bikes_live():
    """Zwraca stan wszystkich stacji z pamięci podręcznej RAM pod warstwę MapLibre."""
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
    """Pobiera bieżące dane z CityBikes i zwraca ranking najbardziej obciążonych stacji."""
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
        "top_hubs": [
            s.model_dump() for s in sorted(stations, key=lambda x: x.free_bikes, reverse=True)[:top_n]
        ],
        "top_overflow": [
            s.model_dump() for s in sorted(stations, key=lambda x: (x.occupancy_pct, x.free_bikes), reverse=True)[:top_n]
        ]
    }   