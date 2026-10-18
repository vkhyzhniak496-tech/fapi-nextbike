import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import RESOURCES_DIR
from core.database import close_db_pool, get_pool, init_db_pool
from modules.bikes.router import router as bikes_router
from modules.bikes.worker import bike_history_poller_worker
from modules.infrastructure.router import router as infra_router
from modules.telemetry.router import (
    router as telemetry_router,
    run_ztm_dictionary_sync,  # Import funkcji synchronizującej słowniki
)
from modules.telemetry.worker import (
    tram_analytics_cleaner_task,
    tram_telemetry_poller_task,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("main")


async def ztm_dictionary_daily_sync_worker():
    """
    Worker odpalany raz na dobę w nocy (np. o 03:30), gdy ZTM publikuje świeże rozkłady,
    trasy i słowniki ulic/przystanków.
    """
    TARGET_HOUR = 3
    TARGET_MINUTE = 30

    while True:
        try:
            now = datetime.now()
            target_time = time(TARGET_HOUR, TARGET_MINUTE)
            target_dt = datetime.combine(now.date(), target_time)

            # Jeśli dzisiejsza godzina 03:30 już minęła, planujemy na jutro
            if now >= target_dt:
                target_dt += timedelta(days=1)

            sleep_seconds = (target_dt - now).total_seconds()
            logger.info(
                f"[ZTM Sync Worker] Następna synchronizacja słowników za "
                f"{sleep_seconds / 3600.0:.2f} h (o {target_dt.strftime('%Y-%m-%d %H:%M:%S')})."
            )

            await asyncio.sleep(sleep_seconds)

            logger.info("[ZTM Sync Worker] Rozpoczynam planową nocną aktualizację słowników i tras ZTM...")
            await run_ztm_dictionary_sync()
            logger.info("[ZTM Sync Worker] Planowa aktualizacja słowników zakończona sukcesem.")

        except asyncio.CancelledError:
            logger.info("[ZTM Sync Worker] Worker dobowy został zatrzymany.")
            break
        except Exception as e:
            logger.error(f"[ZTM Sync Worker] Błąd podczas synchronizacji ZTM: {e}", exc_info=True)
            # W razie awarii API odczekaj 15 minut przed ewentualną ponowną próbą
            await asyncio.sleep(900)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Start połączenia z bazą danych PostgreSQL / PostGIS
    logger.info("Łączenie z bazą PostgreSQL / PostGIS...")
    await init_db_pool()

    # 2. Start workerów telemetrycznych, analitycznych, rowerowych i słownikowych
    ingest_task = asyncio.create_task(tram_telemetry_poller_task())
    cleaner_task = asyncio.create_task(tram_analytics_cleaner_task())
    bikes_task = asyncio.create_task(bike_history_poller_worker())
    ztm_dict_task = asyncio.create_task(ztm_dictionary_daily_sync_worker())

    all_tasks = [ingest_task, cleaner_task, bikes_task, ztm_dict_task]

    try:
        yield
    finally:
        logger.info("Zamykanie zadań w tle i puli PostgreSQL...")
        for t in all_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        await close_db_pool()
        logger.info("System zamknięty pomyślnie.")


app = FastAPI(
    title="Warsaw Transit & Safe Cycleways GIS",
    lifespan=lifespan,
)

# Rejestracja routerów
app.include_router(bikes_router)
app.include_router(infra_router)
app.include_router(telemetry_router)

# Zasoby statyczne
app.mount("/static", StaticFiles(directory=RESOURCES_DIR / "static"), name="static")


# --- Endpointy Główne & Widoki HTML ---

@app.get("/health/check")
async def health_check():
    pool = get_pool()
    async with pool.acquire() as conn:
        version = await conn.fetchval("SELECT postgis_full_version();")
    return {
        "status": "running",
        "service": "Warsaw GIS Engine (PostgreSQL + PostGIS)",
        "postgis": version.split(" ")[0] if version else "OK"
    }


@app.get("/")
def root():
    return RedirectResponse(url="/map")


@app.get("/map", response_class=HTMLResponse)
async def get_map_view():
    html_file = RESOURCES_DIR / "templates" / "index.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="Brak pliku index.html w templates")
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


@app.get("/analytics/tram", response_class=HTMLResponse)
async def tram_analytics_page():
    template_path = RESOURCES_DIR / "templates" / "tram_analytics.html"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Brak pliku tram_analytics.html")
    return HTMLResponse(template_path.read_text(encoding="utf-8"))


# --- WIDOK LEADERBOARD ---
@app.get("/analytics/leaderboard", response_class=HTMLResponse)
@app.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page():
    template_path = RESOURCES_DIR / "templates" / "leaderboard.html"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Brak pliku leaderboard.html w katalogu templates")
    return HTMLResponse(template_path.read_text(encoding="utf-8"))


@app.get("/stations")
async def list_stations():
    """Zwraca listę zarejestrowanych stacji Veturilo bezpośrednio z PostgreSQL."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT station_id, name, lat, lng 
            FROM veturilo_stations 
            ORDER BY name ASC;
        """)
        return [dict(r) for r in rows]


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/static/", "/network/", "/map")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        accept_header = request.headers.get("accept", "")
        if "text/html" in accept_header:
            template_path = RESOURCES_DIR / "templates" / "404.html"
            if template_path.exists():
                return HTMLResponse(
                    content=template_path.read_text(encoding="utf-8"),
                    status_code=404
                )
        return JSONResponse(status_code=404, content={"detail": exc.detail or "Nie znaleziono zasobu"})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)