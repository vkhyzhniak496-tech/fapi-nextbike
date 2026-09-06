import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import io
import logging
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response
import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

import database
from models import Station
from storage import set_cached_stations

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

LAST_KNOWN_BIKES: Dict[str, int] = {}


async def _history_poller_worker():
  """Worker odpytujący CityBikes, zasilający bazę SQLite oraz odświeżający cache GeoJSON dla mapy."""
  backoff_delay = 45

  async with httpx.AsyncClient(
      timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
  ) as client:
    while True:
      try:
        res = await client.get(CITYBIKES_WARSAW_URL)

        if res.status_code == 200:
          backoff_delay = 45
          network_data = res.json().get("network", {})
          stations = network_data.get("stations", [])
          now_warsaw = datetime.now(WARSAW_TZ)
          now_warsaw_iso = now_warsaw.isoformat()

          features = []
          for st in stations:
            s_id = str(st.get("id"))
            s_name = str(st.get("name", "Stacja"))
            current_bikes = int(st.get("free_bikes") or 0)
            empty_slots = int(st.get("empty_slots") or 0)
            lat = st.get("latitude")
            lng = st.get("longitude")

            # 1. Zapis delty do bazy SQLite
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

            # 2. Budowanie cech GeoJSON pod mapę
            if lat is not None and lng is not None:
              features.append({
                  "type": "Feature",
                  "geometry": {
                      "type": "Point",
                      "coordinates": [float(lng), float(lat)],
                  },
                  "properties": {
                      "id": s_id,
                      "name": s_name,
                      "free_bikes": current_bikes,
                      "empty_slots": empty_slots,
                      "occupancy_pct": (
                          round(
                              (current_bikes / (current_bikes + empty_slots))
                              * 100,
                              1,
                          )
                          if (current_bikes + empty_slots) > 0
                          else 0.0
                      ),
                      "updated_at": now_warsaw_iso,
                  },
              })

          # 3. Zapis do cache (RAM + dysk) poprzez moduł storage
          geojson_payload = {
              "type": "FeatureCollection",
              "system_name": network_data.get("name", "VETURILO 3.0"),
              "total_stations": len(features),
              "last_update": now_warsaw_iso,
              "features": features,
          }
          set_cached_stations(geojson_payload)

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
              f"CityBikes API zwróciło status: {res.status_code}. Ponawiam za"
              " 60s..."
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
  return database.get_station_deltas(station_id, start_time, end_time)


@router.get("/series/{station_id}")
def get_station_time_series(station_id: str, limit: Optional[int] = None):
  series = database.get_series_for_chart(station_id, limit=limit)
  if not series:
    raise HTTPException(
        status_code=404, detail="Brak danych w bazie dla podanej stacji"
    )
  return {"station_id": station_id, "points": len(series), "data": series}

@router.get("/station/{station_id}/chart.png")
def get_station_chart_image(
    station_id: str,
    name: Optional[str] = None,
    limit: Optional[int] = None,  # Domyślnie brak limitu — pobiera całą historię
):
  """Rysuje pełny wykres schodkowy na podstawie danych historycznych z bazy SQLite."""
  series = database.get_series_for_chart(station_id, limit=limit)
  if not series or len(series) < 2:
    raise HTTPException(
        status_code=404,
        detail="Brak wystarczającej liczby danych do wygenerowania wykresu",
    )

  resolved_name = name or "Stacja Veturilo"
  dates = [datetime.fromisoformat(row["datetime"]) for row in series]
  values = [row["bikes"] for row in series]

  fig, ax = plt.subplots(figsize=(6.5, 3.0), dpi=100)

  ax.step(
      dates,
      values,
      where="post",
      color="#007cbf",
      marker="o",
      markersize=3.0,
      linewidth=1.5,
  )

  # Dynamiczne dopasowanie etykiet: gdy mamy > 1 dzień, pokazujemy dzień i godzinę (np. 05.09 14:00)
  delta_time = dates[-1] - dates[0]
  if delta_time.days >= 1:
    time_fmt = mdates.DateFormatter("%d.%m %H:%M", tz=WARSAW_TZ)
  else:
    time_fmt = mdates.DateFormatter("%H:%M", tz=WARSAW_TZ)

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