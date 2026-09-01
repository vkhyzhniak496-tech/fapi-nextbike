import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
import io
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

# Bufor zdarzeń i słownik metadanych stacji
STATION_HISTORY_BUFFER: Dict[str, deque] = defaultdict(
    lambda: deque(maxlen=1000)
)
STATION_METADATA: Dict[str, str] = {}


async def _history_poller_worker():
    """Worker rejestrujący zmiany stanu stacji w czasie lokalnym."""
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
    ) as client:
        while True:
            try:
                res = await client.get(CITYBIKES_WARSAW_URL)
                if res.status_code == 200:
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
            except Exception:
                pass
            await asyncio.sleep(30)


@asynccontextmanager
async def analytics_lifespan(app_router: APIRouter):
    """Nowoczesna obsługa cyklu życia workera w tle (zastępuje on_event)."""
    task = asyncio.create_task(_history_poller_worker())
    yield
    task.cancel()


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
    """Generuje czysty wykres schodkowy. Nazwę stacji pobiera z pamięci."""
    history = STATION_HISTORY_BUFFER.get(station_id)
    if not history or len(history) < 2:
        raise HTTPException(
            status_code=404,
            detail="Brak wystarczającej liczby zmian do wygenerowania wykresu",
        )

    resolved_name = name or STATION_METADATA.get(station_id, "Stacja Veturilo")
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