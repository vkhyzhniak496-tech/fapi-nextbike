from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import httpx

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


@app.get("/health/check")
def health_check():
  return {"SayHelloTo": "ctor2", "status": "running"}


@app.get("/")
def root():
  return RedirectResponse(url="/map")


LATEST_STATIONS_CACHE = None


@app.get("/bikes/citybikes/warsaw")
async def get_warsaw_bikes():
    global LATEST_STATIONS_CACHE
    server_time_iso = datetime.now(timezone.utc).isoformat()

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
            res = await client.get(CITYBIKES_WARSAW_URL)
            if res.status_code == 200:
                data = res.json().get("network", {})
                stations = data.get("stations", [])
                
                features = []
                for st in stations:
                    lat, lng = st.get("latitude"), st.get("longitude")
                    if lat is not None and lng is not None:
                        features.append({
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
                            "properties": {
                                "id": str(st["id"]),
                                "name": str(st.get("name", "Stacja")),
                                "free_bikes": int(st.get("free_bikes") or 0),
                                "empty_slots": int(st.get("empty_slots") or 0),
                                "updated_at": st.get("timestamp") or server_time_iso
                            }
                        })
                
                response_payload = {
                    "type": "FeatureCollection",
                    "system_name": data.get("name", "VETURILO 3.0"),
                    "total_stations": len(features),
                    "last_update": server_time_iso,
                    "features": features
                }
                LATEST_STATIONS_CACHE = response_payload
                return response_payload

    except Exception:
        pass  # W razie timeoutu nie wywalamy błędu 504

    # Jeśli CityBikes złapie timeout, serwujemy ostatnie znane stacje
    if LATEST_STATIONS_CACHE is not None:
        return LATEST_STATIONS_CACHE

    raise HTTPException(status_code=504, detail="CityBikes API nie odpowiada, brak danych w cache.")

@app.get("/map", response_class=HTMLResponse)
async def get_map_view():
  html_file = RESOURCES_DIR / "index.html"
  return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/leaderboard", response_class=HTMLResponse)
async def get_leaderboard_view():
  html_file = RESOURCES_DIR / "leaderboard.html"
  return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
  return Response(status_code=204)


if __name__ == "__main__":
  import uvicorn

  uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)