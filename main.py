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

@app.get("/bikes/systems")
async def get_all_systems_sorted():
    """
    Zwraca wszystkie systemy rowerowe (miasta) z API Nextbike, 
    priorytetowo sortując Polskę na samej górze.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(NEXTBIKE_API_URL)
        data = response.json()

    systems = []

    # Iterujemy po wszystkich krajach i miastach w JSON-ie
    for country in data.get("countries", []):
        country_name = country.get("country_name", "Unknown")
        country_code = country.get("country", "")

        for city in country.get("cities", []):
            systems.append({
                "city_id": city.get("uid"),
                "city_name": city.get("name"),
                "country_name": country_name,
                "country_code": country_code,
                "total_bikes": city.get("set_point_bikes", 0),
                "total_places": len(city.get("places", [])),
                "lat": city.get("lat"),
                "lng": city.get("lng")
            })

    # Kluczowe sortowanie:
    # 1. Sprawdzamy czy country_code to "PL" (False/0 dla PL, True/1 dla reszty -> PL ląduje na górze)
    # 2. Resztę sortujemy alfabetycznie po nazwie kraju, a potem po nazwie miasta.
    systems.sort(key=lambda x: (x["country_code"] != "PL", x["country_name"], x["city_name"]))

    return {
        "total_systems": len(systems),
        "systems": systems
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)