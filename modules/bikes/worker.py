import asyncio
from datetime import datetime
import logging
from typing import Dict
from zoneinfo import ZoneInfo
import httpx

from config import HTTP_HEADERS
from core.database import BIKES_DB_PATH, get_db_cursor, transaction
from modules.bikes.storage import set_cached_stations

logger = logging.getLogger(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")
CITYBIKES_WARSAW_URL = (
    "http://api.citybik.es/v2/networks/veturilo-nextbike-warsaw"
)
LAST_KNOWN_BIKES: Dict[str, int] = {}


def log_snapshot(
    station_id: str, name: str, timestamp: str, bikes: int, docks: int
):
  with get_db_cursor(BIKES_DB_PATH) as cur:
    with transaction(cur):
      cur.execute(
          """
                INSERT OR IGNORE INTO stations (station_id, name)
                VALUES (?, ?);
            """,
          (station_id, name),
      )
      cur.execute(
          """
                INSERT OR IGNORE INTO station_snapshots (station_id, timestamp, bikes)
                VALUES (?, ?, ?);
            """,
          (station_id, timestamp, bikes),
      )


async def bike_history_poller_worker():
  """Worker odpytujący CityBikes, rejestrujący delty i odświeżający cache RAM pod mapę."""
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
          now_warsaw_iso = datetime.now(WARSAW_TZ).isoformat()

          features = []
          for st in stations:
            s_id = str(st.get("id"))
            s_name = str(st.get("name", "Stacja"))
            current_bikes = int(st.get("free_bikes") or 0)
            empty_slots = int(st.get("empty_slots") or 0)
            lat = st.get("latitude")
            lng = st.get("longitude")

            # 1. Wykrywanie różnicy i zapis do SQLite
            if (
                s_id not in LAST_KNOWN_BIKES
                or LAST_KNOWN_BIKES[s_id] != current_bikes
            ):
              log_snapshot(
                  s_id, s_name, now_warsaw_iso, current_bikes, empty_slots
              )
              LAST_KNOWN_BIKES[s_id] = current_bikes

            # 2. GeoJSON dla MapLibre
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

          # 3. Cache w pamięci pod natychmiastowe serwowanie na mapę
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
              f"429 Rate Limit z CityBikes. Pauza na {wait_time} sekund."
          )
          backoff_delay = wait_time
        else:
          logger.warning(
              f"CityBikes API status: {res.status_code}. Ponawiam za 60s."
          )
          backoff_delay = 60

      except asyncio.CancelledError:
        logger.info("Worker Veturilo zatrzymany pomyślnie.")
        break
      except Exception as e:
        logger.warning(f"Błąd w pętli Veturilo: {e}")
        backoff_delay = 60

      await asyncio.sleep(backoff_delay)