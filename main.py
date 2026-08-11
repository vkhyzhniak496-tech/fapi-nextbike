from fastapi import FastAPI
import httpx

app = FastAPI(
    title="Nextbike 2",
    description="No description provided"
)

# Publiczne API Nextbike z żywymi danymi o stacjach
NEXTBIKE_API_URL = "https://maps.nextbike.net/maps/nextbike-live.json"

@app.get("/")
def home():
    return {
        "status": "ok", 
        "message": "Czysto, płasko i bez powiadomień MS 🚴"
    }

@app.get("/health/check")
def health_check():
    return{
        "SayHelloTo": "ctor2"
    }


@app.get("/bikes/live")
async def get_live_bikes(city_id: str = "210"):
    """
    Pobiera aktualne stacje i liczbę rowerów.
    Domyślnie city_id='210' to Warszawa (Veturilo), 
    ale możesz wpisać dowolne ID z sieci Nextbike.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{NEXTBIKE_API_URL}?city={city_id}")
        data = response.json()

    places = []
    try:
        # Przetwarzanie drzewa JSON z Nextbike
        countries = data.get("countries", [])
        if countries and "cities" in countries[0]:
            city_data = countries[0]["cities"][0]
            for place in city_data.get("places", []):
                places.append({
                    "id": place.get("uid"),
                    "name": place.get("name"),
                    "lat": place.get("lat"),
                    "lng": place.get("lng"),
                    "bikes_count": place.get("bikes_count"),
                })
            return {
                "city_name": city_data.get("name"),
                "total_stations": len(places),
                "stations": places
            }
    except (IndexError, KeyError):
        return {"error": "Nie udało się sparsować struktury danych Nextbike"}

    return {"message": "Brak danych dla podanego city_id"}


if __name__ == "__main__":
    import uvicorn
    # 0.0.0.0 oznacza: "słuchaj na wszystkich interfejsach sieciowych"
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)