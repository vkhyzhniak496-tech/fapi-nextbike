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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)