import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
import io
import logging
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response
import httpx
import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

from models import Station
from storage import load_history, save_history

logger = logging.getLogger(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")
CITYBIKES_WARSAW_URL = (
    "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"
)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
    "Accept": "application/json",
}

# 1. Wczytanie danych z dysku przy imporcie modułu
_loaded_meta, _loaded_buf = load_history()
STATION_METADATA: Dict[str, str] = _loaded_meta

# Inicjalizacja bufora jako defaultdict(deque) i zasilenie go danymi z pliku
STATION_HISTORY_BUFFER: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
for s_id, records in _loaded_buf.items():
    STATION_HISTORY_BUFFER[s_id].extend(records)


async def _history_poller_worker():
    save_counter = 0
    backoff_delay = 30  # bazowy czas oczekiwania

    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
    ) as client:
        while True:
            try:
                res = await client.get(CITYBIKES_WARSAW_URL)
                
                if res.status_code == 200:
                    backoff_delay = 45  # resetujemy do bezpiecznego bazowego interwału
                    stations = res.json().get("network", {}).get("stations", [])
                    now_warsaw = datetime.now(WARSAW_TZ)

                    for st in stations:
                        s_id = str(st.get("id"))
                        s_name = str(st.get("name", "Stacja"))
                        current_bikes = int(st.get("free_bikes") or 0)

                        STATION_METADATA[s_id] = s_name
                        buf = STATION_HISTORY_BUFFER[s_id]

                        if not buf or buf[-1]["bikes"] != current_bikes:
                            buf.append({
                                "datetime": now_warsaw,
                                "bikes": current_bikes,
                            })

                    save_counter += 1
                    if save_counter >= 10:
                        save_history(STATION_METADATA, dict(STATION_HISTORY_BUFFER))
                        save_counter = 0

                elif res.status_code == 429:
                    # Sprawdzamy, czy CityBikes przysłało nagłówek Retry-After
                    retry_after = res.headers.get("Retry-After")
                    wait_time = int(retry_after) if retry_after and retry_after.isdigit() else 120
                    logger.warning(f"Otrzymano 429 Rate Limit. Wstrzymuję odpytywanie na {wait_time}s...")
                    backoff_delay = wait_time

                else:
                    logger.warning(f"CityBikes API zwróciło status: {res.status_code}")
                    backoff_delay = 60

            except asyncio.CancelledError:
                save_history(STATION_METADATA, dict(STATION_HISTORY_BUFFER))
                break
            except Exception as e:
                logger.warning(f"Wyjątek w workerze: {e}")
                backoff_delay = 60

            await asyncio.sleep(backoff_delay)


@asynccontextmanager
async def analytics_lifespan(app_router: APIRouter):
    """Zarządzanie cyklem życia workera i persystencją danych."""
    task = asyncio.create_task(_history_poller_worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Bezpieczny zrzut pamięci RAM do pliku przy wyłączaniu serwera
    save_history(STATION_METADATA, dict(STATION_HISTORY_BUFFER))


router = APIRouter(
    prefix="/analytics",
    tags=["Bikeshare Analytics & Charts"],
    lifespan=analytics_lifespan,
)


@router.get("/leaderboard")
async def get_stations_leaderboard(top_n: int = 50):
    """Zwraca ranking hubów oraz stacji o wysokim obłożeniu."""
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
    ) as client:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code != 200:
            raise HTTPException(
                status_code=502, detail="Błąd pobierania danych CityBikes"
            )
        stations_raw = res.json().get("network", {}).get("stations", [])

    stations = [
        Station(
            id=str(st.get("id")),
            name=str(st.get("name", "Stacja")),
            lat=float(st.get("latitude") or 0.0),
            lng=float(st.get("longitude") or 0.0),
            free_bikes=int(st.get("free_bikes") or 0),
            empty_slots=int(st.get("empty_slots") or 0),
        )
        for st in stations_raw
    ]

    top_hubs = sorted(stations, key=lambda x: x.free_bikes, reverse=True)[:top_n]
    top_overflow = sorted(
        stations,
        key=lambda x: (x.occupancy_pct, x.free_bikes),
        reverse=True,
    )[:top_n]

    return {
        "total_active_stations": len(stations),
        "top_hubs": [s.model_dump() for s in top_hubs],
        "top_overflow": [s.model_dump() for s in top_overflow],
    }


@router.get("/station/{station_id}/chart.png")
def get_station_chart_image(station_id: str, name: Optional[str] = None):
    history = list(STATION_HISTORY_BUFFER.get(station_id, []))
    if not history:
        raise HTTPException(
            status_code=404,
            detail="Brak zarejestrowanych danych dla tej stacji",
        )

    resolved_name = name or STATION_METADATA.get(station_id, "Stacja Veturilo")
    now_warsaw = datetime.now(WARSAW_TZ)

    # Jeśli mamy tylko 1 punkt (brak zmian), dodajemy punkt wirtualny "teraz",
    # aby Matplotlib miał poprawny przedział czasu na osi X
    if len(history) == 1:
        dates = [history[0]["datetime"], now_warsaw]
        values = [history[0]["bikes"], history[0]["bikes"]]
    else:
        dates = [entry["datetime"] for entry in history]
        values = [entry["bikes"] for entry in history]

    fig, ax = plt.subplots(figsize=(6, 2.8), dpi=100)
    ax.step(
        dates,
        values,
        where="post",
        color="#007cbf",
        marker="o",
        markersize=3.5,
        linewidth=1.8,
    )

    time_fmt = mdates.DateFormatter("%H:%M:%S", tz=WARSAW_TZ)
    ax.xaxis.set_major_formatter(time_fmt)
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=WARSAW_TZ))

    ax.set_title(
        f"Historia zmian: {resolved_name}",
        fontsize=10,
        fontweight="bold",
        pad=8,
    )
    ax.set_ylabel("Liczba rowerów", fontsize=8)
    ax.grid(True, linestyle="--", alpha=0.4)

    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    min_v, max_v = min(values), max(values)
    ax.set_ylim(max(0, min_v - 1), max_v + 2)

    plt.xticks(rotation=20, fontsize=8)
    plt.yticks(fontsize=8)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)

    return Response(content=buf.getvalue(), media_type="image/png")