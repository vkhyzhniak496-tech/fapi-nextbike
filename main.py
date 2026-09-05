from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple,Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import httpx

from av_service import router as av_router
from models import Station
from network_service import router as network_router
from storage import load_stations_cache, save_stations_cache
import database

logger = logging.getLogger(__name__)

database.init_db()
app = FastAPI(title="Nextbike & Safe Cycleways GIS")
app.include_router(network_router)
app.include_router(av_router)

RESOURCES_DIR = Path(__file__).parent / "resources"
app.mount("/static", StaticFiles(directory=RESOURCES_DIR), name="static")

CITYBIKES_WARSAW_URL = (
    "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"
)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
    "Accept": "application/json",
}

# Inicjalizacja cache danymi z dysku przy starcie modułu
LATEST_STATIONS_CACHE: Dict[str, Any] = load_stations_cache()


@app.get("/health/check")
def health_check():
    return {"SayHelloTo": "ctor2", "status": "running"}


@app.get("/")
def root():
    return RedirectResponse(url="/map")


async def fetch_network_metadata(client: httpx.AsyncClient) -> Dict[str, Any]:
    """Pobiera surowy stan sieci z CityBikes API."""
    res = await client.get(CITYBIKES_WARSAW_URL)
    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"CityBikes API zwróciło status błędu {res.status_code}",
        )
    return res.json().get("network", {})


def parse_stations_to_features(
    stations_raw: List[Dict[str, Any]], server_time_iso: str
) -> Tuple[List[Dict[str, Any]], List[Station]]:
    """Konwertuje surowe słowniki ze stacjami na obiekty domenowe Station oraz GeoJSON Features."""
    features = []
    domain_stations = []

    for st in stations_raw:
        lat, lng = st.get("latitude"), st.get("longitude")
        if lat is not None and lng is not None:
            station = Station(
                id=str(st.get("id")),
                name=str(st.get("name", "Stacja")),
                lat=float(lat),
                lng=float(lng),
                free_bikes=int(st.get("free_bikes") or 0),
                empty_slots=int(st.get("empty_slots") or 0),
                updated_at=st.get("timestamp") or server_time_iso,
            )
            domain_stations.append(station)
            features.append(station.to_geojson_feature())

    return features, domain_stations


@app.get("/bikes/citybikes/warsaw")
async def get_warsaw_bikes():
    """Główny endpoint GeoJSON serwujący stacje na mapę z automatycznym fallbackiem."""
    global LATEST_STATIONS_CACHE
    server_time_iso = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
        ) as client:
            network_data = await fetch_network_metadata(client)
            stations_raw = network_data.get("stations", [])
            features, _ = parse_stations_to_features(
                stations_raw, server_time_iso
            )

            response_payload = {
                "type": "FeatureCollection",
                "system_name": network_data.get("name", "VETURILO 3.0"),
                "total_stations": len(features),
                "last_update": server_time_iso,
                "features": features,
            }

            LATEST_STATIONS_CACHE = response_payload
            save_stations_cache(response_payload)
            return response_payload

    except Exception as e:
        logger.warning(f"Błąd CityBikes API, używam cache: {e}")

    # Fallback 1: Dane z pamięci RAM
    if LATEST_STATIONS_CACHE is not None:
        return LATEST_STATIONS_CACHE

    # Fallback 2: Odczyt świeżo z dysku
    disk_cache = load_stations_cache()
    if disk_cache is not None:
        LATEST_STATIONS_CACHE = disk_cache
        return disk_cache

    # Fallback 3: Bezpieczny pusty GeoJSON (zapobiega wyłożeniu frontendu Leaflet)
    return {
        "type": "FeatureCollection",
        "system_name": "VETURILO 3.0",
        "total_stations": 0,
        "last_update": server_time_iso,
        "features": [],
    }


@app.get("/map", response_class=HTMLResponse)
async def get_map_view():
    """Serwuje główny widok interaktywnej mapy."""
    html_file = RESOURCES_DIR / "index.html"
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/leaderboard", response_class=HTMLResponse)
async def get_leaderboard_view():
    """Serwuje widok tabeli/rankingu obciążenia stacji."""
    html_file = RESOURCES_DIR / "leaderboard.html"
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


@app.get("/stations")
def list_stations():
    """Zwraca listę wszystkich zaimportowanych stacji."""
    with database.get_connection() as conn:
        rows = conn.execute("SELECT station_id, name FROM stations ORDER BY name ASC;").fetchall()
        return [dict(r) for r in rows]


@app.get("/analytics/events/{station_id}")
def get_station_deltas(station_id: str, start_time: str, end_time: str) -> List[Dict[str, Any]]:
    """Wyciąga punkty zmian (+1 / -1) dopasowując daty jako prefikse lub zakres tekstowy."""
    query = """
        WITH calculated_deltas AS (
            SELECT 
                timestamp,
                bikes,
                bikes - LAG(bikes) OVER (ORDER BY timestamp) AS delta
            FROM station_snapshots
            WHERE station_id = ? 
              AND timestamp >= ? 
              AND timestamp <= ?
        )
        SELECT timestamp, bikes, delta
        FROM calculated_deltas
        WHERE delta IS NOT NULL AND delta != 0
        ORDER BY timestamp ASC;
    """
    with database.get_connection() as conn:
        # Doklejamy wildcardy lub pełne dopasowanie, albo po prostu przekazujemy parametry
        rows = conn.execute(query, (station_id, start_time, end_time)).fetchall()
        return [dict(row) for row in rows]


@app.get("/analytics/series/{station_id}")
def get_station_time_series(station_id: str, limit: Optional[int] = None):
    """Seria czasowa pod wykresy."""
    series = database.get_series_for_chart(station_id, limit=limit)
    if not series:
        raise HTTPException(status_code=404, detail="Brak danych dla stacji")
    return {"station_id": station_id, "points": len(series), "data": series}


@app.get("/analytics/debug/{station_id}")
def debug_station(station_id: str):
    """Zwraca pierwsze 5 surowych wpisów dla stacji z bazy."""
    with database.get_connection() as conn:
        rows = conn.execute(
            "SELECT timestamp, bikes FROM station_snapshots WHERE station_id = ? ORDER BY timestamp ASC LIMIT 5;",
            (station_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/admin/sync-json")
def sync_latest_json():
    """Wymusza ponowne zczytanie danych z history_cache.json do SQLite (np. po pullu)."""
    try:
        database.migrate_json_to_db(database.JSON_CACHE_PATH)
        return {"status": "success", "message": "Zsynchronizowano dane z pliku JSON do bazy SQLite."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd synchronizacji: {str(e)}")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)