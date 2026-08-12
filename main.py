from fastapi import FastAPI
import httpx

app = FastAPI(
    title="Nextbike 2",
    description="No description provided"
)

NEXTBIKE_API_URL = "https://maps.nextbike.net/maps/nextbike-live.json"


@app.get("/health/check")
def health_check():
    return{
        "SayHelloTo": "ctor2"
    }

@app.get("/bikes/systems/poland")
async def get_polish_systems():
    """
    Zwraca tylko i wyłącznie polskie systemy rowerowe z API Nextbike.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(NEXTBIKE_API_URL)
        data = response.json()

    polish_systems = []

    for country in data.get("countries", []):
        # Filtrujemy tylko Polskę (kod 'PL' lub nazwa 'Poland')
        if country.get("country") == "PL" or country.get("country_name") == "Poland":
            for city in country.get("cities", []):
                polish_systems.append({
                    "city_id": city.get("uid"),
                    "city_name": city.get("name"),
                    "country_name": "Poland",
                    "country_code": "PL",
                    "total_bikes": city.get("set_point_bikes", 0),
                    "total_places": len(city.get("places", [])),
                    "lat": city.get("lat"),
                    "lng": city.get("lng")
                })

    # Sortujemy polskie miasta alfabetycznie według nazwy
    polish_systems.sort(key=lambda x: x["city_name"])

    return {
        "total_polish_systems": len(polish_systems),
        "systems": polish_systems
    }



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)