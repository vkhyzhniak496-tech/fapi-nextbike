from contextlib import asynccontextmanager
import logging
from pathlib import Path

from av_service import router as av_router
import database
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from network_service import router as network_router
from storage import get_cached_stations

logger = logging.getLogger(__name__)
RESOURCES_DIR = Path(__file__).parent / "resources"


@asynccontextmanager
async def lifespan(app: FastAPI):
  database.init_db()
  yield


app = FastAPI(
    title="Nextbike & Safe Cycleways GIS", lifespan=lifespan
)  # Cykl życia z inicjalizacją SQLite
app.include_router(network_router)  
app.include_router(av_router)  

app.mount(
    "/static", StaticFiles(directory=RESOURCES_DIR), name="static"
)  


@app.get("/health/check") 
def health_check():
  return {"SayHelloTo": "ctor2", "status": "running"}  


@app.get("/")  
def root():
  return RedirectResponse(url="/map")  


@app.get("/bikes/citybikes/warsaw")
async def get_warsaw_bikes():
  """Błyskawicznie serwuje stacje z pamięci RAM/dysku. Zero zapytań HTTP na zewnątrz!"""
  cached = get_cached_stations()
  if cached:
    return cached

  return {
      "type": "FeatureCollection",
      "system_name": "VETURILO 3.0",
      "total_stations": 0,
      "features": [],
  }


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


@app.get("/stations")
def list_stations():
  """Zwraca listę wszystkich zarejestrowanych stacji z bazy SQLite."""
  with database.get_db_cursor() as cur:
    cur.execute("SELECT station_id, name FROM stations ORDER BY name ASC;")
    return [dict(r) for r in cur.fetchall()]


@app.post("/admin/sync-json")
def sync_latest_json():
  """Wymusza ponowne zczytanie danych z history_cache.json do SQLite."""
  try:
    database.migrate_json_to_db(database.JSON_CACHE_PATH)
    return {
        "status": "success",
        "message": "Zsynchronizowano dane z pliku JSON do bazy SQLite.",
    }
  except Exception as e:
    raise HTTPException(
        status_code=500, detail=f"Błąd synchronizacji: {str(e)}"
    )


if __name__ == "__main__":
  import uvicorn

  uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)