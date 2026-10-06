import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from config import BASE_DIR, RESOURCES_DIR
from core.database import (
    BIKES_DB_PATH,
    TRAM_DB_PATH,
    get_db_cursor,
    init_all_databases,
    migrate_tram_edges,
    migrate_tram_platforms,
)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from modules.bikes.router import router as bikes_router
from modules.bikes.storage import get_cached_stations
from modules.bikes.worker import bike_history_poller_worker
from modules.infrastructure.router import router as infra_router
from modules.telemetry.processor import analytics_engine
from modules.telemetry.router import router as telemetry_router
from modules.telemetry.worker import (
    cleanup_old_telemetry_task,
    tram_analytics_worker_task,
    tram_telemetry_poller_task,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
  # Inicjalizacja indeksu przestrzennego peronów
  analytics_engine.ensure_initialized()

  # Uruchomienie zadań asynchronicznych w tle
  ingest_task = asyncio.create_task(tram_telemetry_poller_task())
  analytics_cleaner_task = asyncio.create_task(tram_analytics_worker_task())
  cleanup_task = asyncio.create_task(cleanup_old_telemetry_task())
  bikes_task = asyncio.create_task(bike_history_poller_worker())

  all_tasks = [ingest_task, analytics_cleaner_task, cleanup_task, bikes_task]

  try:
    yield
  finally:
    for t in all_tasks:
      if not t.done():
        t.cancel()
    await asyncio.gather(*all_tasks, return_exceptions=True)


app = FastAPI(
    title="Warsaw Transit & Safe Cycleways GIS",
    lifespan=lifespan,
)

# Rejestracja modułów
app.include_router(bikes_router)
app.include_router(infra_router)
app.include_router(telemetry_router)

# Zasoby statyczne
app.mount(
    "/static", StaticFiles(directory=RESOURCES_DIR / "static"), name="static"
)


# --- Endpointy Główne & Widoki ---


@app.get("/health/check")
def health_check():
  return {"status": "running", "service": "Warsaw GIS Engine"}


@app.get("/")
def root():
  return RedirectResponse(url="/map")


@app.get("/map", response_class=HTMLResponse)
async def get_map_view():
  html_file = RESOURCES_DIR / "templates" / "index.html"
  if not html_file.exists():
    raise HTTPException(
        status_code=404, detail="Brak pliku index.html w resources/templates"
    )
  return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/analytics/tram", response_class=HTMLResponse)
async def tram_analytics_page():
  template_path = RESOURCES_DIR / "templates" / "tram_analytics.html"
  with open(template_path, encoding="utf-8") as f:
    return HTMLResponse(f.read())


@app.get("/analytics/tram-corridor", response_class=HTMLResponse)
async def tram_corridor_view():
  html_file = RESOURCES_DIR / "templates" / "tram_corridor.html"
  return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/stations")
def list_stations():
  """Zwraca listę zarejestrowanych stacji Veturilo z bazy SQLite."""
  with get_db_cursor(BIKES_DB_PATH) as cur:
    cur.execute(
        "SELECT station_id, name, capacity FROM stations ORDER BY name ASC;"
    )
    return [dict(r) for r in cur.fetchall()]


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
  return Response(status_code=204)


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(
    request: Request, exc: StarletteHTTPException
):
  if exc.status_code == 404:
    accept_header = request.headers.get("accept", "")
    if "text/html" in accept_header:
      template_path = RESOURCES_DIR / "templates" / "404.html"
      if template_path.exists():
        return HTMLResponse(
            content=template_path.read_text(encoding="utf-8"), status_code=404
        )
    return JSONResponse(
        status_code=404,
        content={"detail": exc.detail or "Nie znaleziono zasobu"},
    )
  return JSONResponse(
      status_code=exc.status_code, content={"detail": exc.detail}
  )


if __name__ == "__main__":
  import uvicorn

  uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)