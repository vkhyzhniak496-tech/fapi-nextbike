import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI(
    title="Nextbike & CityBikes GIS Service",
    description="Mikroserwis do serwowania danych rowerowych dla Warszawy i innych miast"
)

CITYBIKES_WARSAW_URL = "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"
#NEXTBIKE_LIVE_URL = "https://maps.nextbike.net/maps/nextbike-live.json"


@app.get("/health/check")
def health_check():
    return {"SayHelloTo": "ctor2", "status": "running"}


@app.get("/bikes/citybikes/warsaw")
async def get_warsaw_citybikes():
    """Citybikes API parse"""
    async with httpx.AsyncClient() as client:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Błąd połączenia z CityBikes API")
        data = res.json().get("network", {})

    features = []
    for station in data.get("stations", []):
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [station["longitude"], station["latitude"]]
            },
            "properties": {
                "id": station["id"],
                "name": station["name"],
                "free_bikes": station.get("free_bikes", 0),
                "empty_slots": station.get("empty_slots", 0),
                "has_ebikes": station.get("extra", {}).get("has_ebikes", False),
                "ebikes_count": station.get("extra", {}).get("ebikes", 0),
                "updated_at": station.get("timestamp")
            }
        })

    return {
        "type": "FeatureCollection",
        "system_name": data.get("name"),
        "total_stations": len(features),
        "features": features
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)