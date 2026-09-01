from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from typing import Any, Dict, List, Tuple
import httpx


from models import Station
from av_service import router as av_router
from network_service import router as network_router

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

LATEST_STATIONS_CACHE: Dict[str, Any] = None


async def fetch_network_metadata(client: httpx.AsyncClient) -> Dict[str, Any]:
    """Pobiera surowy obiekt sieci z zewnętrznego API CityBikes."""
    res = await client.get(CITYBIKES_WARSAW_URL)
    if res.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"CityBikes API zwróciło błąd statusu {res.status_code}",
        )
    return res.json().get("network", {})


def parse_stations_to_features(
    stations_raw: List[Dict[str, Any]], server_time_iso: str
) -> Tuple[List[Dict[str, Any]], List[Station]]:
    """Dokonuje konwersji i walidacji stacji na obiekty domenowe Station oraz GeoJSON Features."""
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
    """Główny endpoint GeoJSON orkiestrujący pobranie sieci i stacji."""
    global LATEST_STATIONS_CACHE
    server_time_iso = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(
            timeout=15.0, follow_redirects=True, headers=HTTP_HEADERS
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
            return response_payload

    except Exception:
        pass

    # Fallback: serwowanie danych z cache przy awarii API
    if LATEST_STATIONS_CACHE is not None:
        return LATEST_STATIONS_CACHE

    raise HTTPException(
        status_code=504,
        detail="CityBikes API nie odpowiada, brak danych w cache.",
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)