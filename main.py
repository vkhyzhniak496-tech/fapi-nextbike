from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
import httpx

app = FastAPI(title="Nextbike & CityBikes GIS")
RESOURCES_DIR = Path(__file__).parent / "resources"
CITYBIKES_WARSAW_URL = "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"


@app.get("/health/check")
def health_check():
    return {"SayHelloTo": "ctor2", "status": "running"}


@app.get("/bikes/citybikes/warsaw")
async def get_warsaw_bikes():
    """Pobiera stacje Veturilo 3.0 z gwarantowanym czasem aktualizacji serwera."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Błąd połączenia z CityBikes API")
        data = res.json().get("network", {})

    server_time_iso = datetime.now(timezone.utc).isoformat()
    stations = data.get("stations", [])
    features = []

    for st in stations:
        timestamp = st.get("timestamp") or server_time_iso
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [st["longitude"], st["latitude"]]
            },
            "properties": {
                "id": st["id"],
                "name": st["name"],
                "free_bikes": st.get("free_bikes", 0),
                "empty_slots": st.get("empty_slots", 0),
                "updated_at": timestamp
            }
        })

    return {
        "type": "FeatureCollection",
        "system_name": data.get("name", "VETURILO 3.0"),
        "total_stations": len(features),
        "last_update": server_time_iso,
        "features": features
    }


@app.get("/map", response_class=HTMLResponse)
async def get_map_view():
    html_file = RESOURCES_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>Brak pliku resources/index.html</h1>", status_code=404)
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)