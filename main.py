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

@app.get("/bikes/live")
async def get_live_bikes(city_name: str = "Częstochowa"):
    async with httpx.AsyncClient() as client:
        response = await client.get(NEXTBIKE_API_URL)
        data = response.json()

    extracted_places = []
    
    # Przeszukujemy kraje i miasta
    for country in data.get("countries", []):
        for city in country.get("cities", []):
            # Dopasowujemy miasto po nazwie (ignorując wielkość liter)
            if city_name.lower() in city.get("name", "").lower():
                
                # Właściwe stacje i rowery leżą w kluczu 'places' wewnątrz miasta!
                for place in city.get("places", []):
                    extracted_places.append({
                        "id": place.get("uid"),
                        "name": place.get("name"),
                        "lat": place.get("lat"),
                        "lng": place.get("lng"),
                        "bikes_count": place.get("bikes_count"),
                        "is_station": place.get("spot", True)
                    })
                
                return {
                    "matched_city": city.get("name"),
                    "city_center": {"lat": city.get("lat"), "lng": city.get("lng")},
                    "total_places": len(extracted_places),
                    "places": extracted_places
                }

    return {"message": f"Nie znaleziono miasta zawierającego frazę '{city_name}'"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)