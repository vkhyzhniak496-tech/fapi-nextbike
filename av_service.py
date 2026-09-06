import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import io
import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Response
import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

import database
from models import Station

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

# Podręczny stan w pamięci wyłącznie do detekcji zmian (delta-check bez SELECT-ów)
LAST_KNOWN_BIKES: Dict[str, int] = {}


async def _history_poller_worker():
  """Worker odpytujący CityBikes i zapisujący zmiany wprost do SQLite."""
  backoff_delay = 45

  async with httpx.AsyncClient(
      timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
  ) as client:
    while True:
      try:
        res = await client.get(CITYBIKES_WARSAW_URL)

        if res.status_code == 200:
          backoff_delay = 45
          stations = res.json().get("network", {}).get("stations", [])
          now_warsaw_iso = datetime.now(WARSAW_TZ).isoformat()

          for st in stations:
            s_id = str(st.get("id"))
            s_name = str(st.get("name", "Stacja"))
            current_bikes = int(st.get("free_bikes") or 0)

            # Logujemy tylko pierwszy pomiar stacji lub realną zmianę stanu
            if (
                s_id not in LAST_KNOWN_BIKES
                or LAST_KNOWN_BIKES[s_id] != current_bikes
            ):
              database.log_snapshot(
                  station_id=s_id,
                  name=s_name,
                  timestamp=now_warsaw_iso,
                  bikes=current_bikes,
              )
              LAST_KNOWN_BIKES[s_id] = current_bikes

        elif res.status_code == 429:
          retry_after = res.headers.get("Retry-After")
          wait_time = (
              int(retry_after)
              if retry_after and retry_after.isdigit()
              else 120
          )
          logger.warning(
              f"Otrzymano 429 Rate Limit. Wstrzymuję odpytywanie na {wait_time}s..."
          )
          backoff_delay = wait_time
        else:
          logger.warning(
              f"CityBikes API zwróciło nieoczekiwany status: {res.status_code}"
          )
          backoff_delay = 60

      except asyncio.CancelledError:
        logger.info("Worker analityczny został bezpiecznie zatrzymany.")
        break
      except Exception as e:
        logger.warning(f"Wyjątek w workerze historii: {e}")
        backoff_delay = 60

      await asyncio.sleep(backoff_delay)


@asynccontextmanager
async def analytics_lifespan(router: APIRouter):
  """Zarządzanie tłem workera na poziomie routera."""
  task = asyncio.create_task(_history_poller_worker())
  yield
  task.cancel()
  try:
    await task
  except asyncio.CancelledError:
    pass


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
      stations, key=lambda x: (x.occupancy_pct, x.free_bikes), reverse=True
  )[:top_n]

  return {
      "total_active_stations": len(stations),
      "top_hubs": [s.model_dump() for s in top_hubs],
      "top_overflow": [s.model_dump() for s in top_overflow],
  }


@router.get("/events/{station_id}")
def get_station_deltas(
    station_id: str, start_time: str, end_time: str
) -> List[Dict[str, Any]]:
  """Wyciąga punkty zmian (+1 / -1) bezpośrednio z SQLite."""
  return database.get_station_deltas(station_id, start_time, end_time)


@router.get("/series/{station_id}")
def get_station_time_series(station_id: str, limit: Optional[int] = None):
  """Seria czasowa z bazy danych pod wykresy."""
  series = database.get_series_for_chart(station_id, limit=limit)
  if not series:
    raise HTTPException(
        status_code=404, detail="Brak danych w bazie dla podanej stacji"
    )
  return {"station_id": station_id, "points": len(series), "data": series}


@router.get("/station/{station_id}/chart.png")
def get_station_chart_image(station_id: str, name: Optional[str] = None):
  """Rysuje wykres schodkowy na podstawie danych historycznych z bazy SQLite."""
  series = database.get_series_for_chart(station_id, limit=100)
  if not series:
    raise HTTPException(
        status_code=404, detail="Brak zarejestrowanych danych dla tej stacji"
    )

  resolved_name = name or "Stacja Veturilo"
  dates = [datetime.fromisoformat(row["datetime"]) for row in series]
  values = [row["bikes"] for row in series]

  fig, ax = plt.subplots(figsize=(6, 2.8), dpi=100)

  if len(values) == 1:
    ax.scatter(dates, values, color="#007cbf", s=40)
    ax.axhline(y=values[0], color="#007cbf", linestyle=":", alpha=0.6)
  else:
    ax.step(
        dates,
        values,
        where="post",
        color="#007cbf",
        marker="o",
        markersize=3.5,
        linewidth=1.8,
    )

  ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=WARSAW_TZ))
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