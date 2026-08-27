import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone
import io
from typing import Dict

from fastapi import APIRouter, HTTPException, Response
import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

router = APIRouter(prefix="/analytics", tags=["Bikeshare Analytics & Charts"])

CITYBIKES_WARSAW_URL = (
    "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"
)
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    ),
    "Accept": "application/json",
}

# Zwiększamy bufor do 100 ostatnich zmian (zdarzeń)
STATION_HISTORY_BUFFER: Dict[str, deque] = defaultdict(
    lambda: deque(maxlen=100)
)


async def _history_poller_worker():
  """Worker rejestrujący wyłącznie realne zmiany (zdarzenia) na stacjach."""
  async with httpx.AsyncClient(
      timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
  ) as client:
    while True:
      try:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code == 200:
          stations = res.json().get("network", {}).get("stations", [])
          now_hhmmss = datetime.now(timezone.utc).strftime("%H:%M:%S")

          for st in stations:
            s_id = str(st.get("id"))
            current_bikes = int(st.get("free_bikes") or 0)
            buf = STATION_HISTORY_BUFFER[s_id]

            # Zapisujemy tylko przy pierwszym odczycie LUB gdy zmieniła się liczba rowerów
            if not buf or buf[-1]["bikes"] != current_bikes:
              buf.append({"time": now_hhmmss, "bikes": current_bikes})
      except Exception:
        pass
      await asyncio.sleep(30)


@router.on_event("startup")
async def startup_analytics_poller():
  asyncio.create_task(_history_poller_worker())


@router.get("/leaderboard")
async def get_stations_leaderboard(top_n: int = 50):
  """Zwraca ranking hubów oraz przepełnionych stacji."""
  async with httpx.AsyncClient(
      timeout=20.0, follow_redirects=True, headers=HTTP_HEADERS
  ) as client:
    res = await client.get(CITYBIKES_WARSAW_URL)
    if res.status_code != 200:
      raise HTTPException(
          status_code=502, detail="Błąd pobierania danych CityBikes"
      )
    stations = res.json().get("network", {}).get("stations", [])

  parsed = []
  for st in stations:
    free_bikes = int(st.get("free_bikes") or 0)
    empty_slots = int(st.get("empty_slots") or 0)
    total_docks = free_bikes + empty_slots
    occupancy = (
        (free_bikes / total_docks * 100)
        if total_docks > 0
        else (100.0 if free_bikes > 0 else 0.0)
    )

    parsed.append({
        "id": str(st.get("id")),
        "name": str(st.get("name", "Stacja")),
        "lat": st.get("latitude"),
        "lng": st.get("longitude"),
        "free_bikes": free_bikes,
        "empty_slots": empty_slots,
        "occupancy_pct": round(occupancy, 1),
    })

  top_hubs = sorted(parsed, key=lambda x: x["free_bikes"], reverse=True)[:top_n]
  top_overflow = sorted(
      parsed, key=lambda x: (x["occupancy_pct"], x["free_bikes"]), reverse=True
  )[:top_n]

  return {
      "total_active_stations": len(parsed),
      "top_hubs": top_hubs,
      "top_overflow": top_overflow,
  }


@router.get("/station/{station_id}/chart.png")
def get_station_chart_image(station_id: str, name: str = "Stacja"):
  """Generuje wykres zdarzeń dla stacji."""
  history = STATION_HISTORY_BUFFER.get(station_id)
  if not history:
    raise HTTPException(
        status_code=404, detail="Brak zarejestrowanych danych dla stacji"
    )

  labels = [entry["time"] for entry in history]
  values = [entry["bikes"] for entry in history]

  fig, ax = plt.subplots(figsize=(6, 2.8), dpi=100)

  if len(values) == 1:
    # Pojedynczy punkt (stacja od startu nie zmieniła stanu)
    ax.scatter(labels, values, color="#007cbf", s=50, zorder=3)
    ax.axhline(y=values[0], color="#007cbf", linestyle=":", alpha=0.6)
  else:
    # Wykres schodkowy (steps-post) - idealny dla dyskretnych wypożyczeń/zwrotów
    ax.step(
        labels,
        values,
        where="post",
        color="#007cbf",
        marker="o",
        markersize=4,
        linewidth=2,
    )

  ax.set_title(f"Historia zmian: {name}", fontsize=10, fontweight="bold", pad=8)
  ax.set_ylabel("Liczba rowerów", fontsize=8)
  ax.grid(True, linestyle="--", alpha=0.4)

  # Wymuszenie liczb całkowitych na osi Y
  ax.yaxis.set_major_locator(MaxNLocator(integer=True))
  min_v, max_v = min(values), max(values)
  ax.set_ylim(max(0, min_v - 1), max_v + 2)

  if len(labels) > 6:
    ax.set_xticks(range(0, len(labels), max(1, len(labels) // 4)))
  plt.xticks(rotation=20, fontsize=8)
  plt.yticks(fontsize=8)
  plt.tight_layout()

  buf = io.BytesIO()
  plt.savefig(buf, format="png")
  plt.close(fig)
  buf.seek(0)

  return Response(content=buf.getvalue(), media_type="image/png")