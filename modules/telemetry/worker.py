import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List
import httpx

from config import (
    UM_WARSZAWA_RESOURCE_ID,
    UM_WARSZAWA_VEHICLES_URL,
    settings,
)
from core.database import TRAM_LIVE_DB_PATH, get_db_cursor, transaction
from core.geo import calculate_speed_kmh

logger = logging.getLogger(__name__)

# Cache w RAM do natychmiastowego liczenia prędkości i serwowania /live:
# {vehicle_number: (lat, lon, datetime, speed_kmh, line, brigade)}
LAST_TRAM_POSITIONS: Dict[str, tuple[float, float, datetime, float, str, str]] = {}


def process_and_append_batch(items: List[Dict[str, Any]]):
  records = []

  for item in items:
    v_num = str(item.get("VehicleNumber", "")).strip()
    line = str(item.get("Lines", "")).strip()
    brigade = str(item.get("Brigade", "")).strip()
    lat = item.get("Lat")
    lon = item.get("Lon")
    gps_time_str = item.get("Time")

    if not (v_num and line and brigade and lat and lon and gps_time_str):
      continue

    try:
      lat_f, lon_f = float(lat), float(lon)
      gps_dt = datetime.strptime(gps_time_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
      continue

    speed = 0.0
    if v_num in LAST_TRAM_POSITIONS:
      prev_lat, prev_lon, prev_dt, _, _, _ = LAST_TRAM_POSITIONS[v_num]
      if gps_dt <= prev_dt:
        continue
      speed = calculate_speed_kmh(
          prev_lat, prev_lon, prev_dt, lat_f, lon_f, gps_dt
      )

    LAST_TRAM_POSITIONS[v_num] = (
        lat_f,
        lon_f,
        gps_dt,
        speed,
        line,
        brigade,
    )
    records.append((v_num, line, brigade, lat_f, lon_f, speed, gps_time_str))

  if not records:
    return

  with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
    with transaction(cur):
      cur.executemany(
          """
                INSERT INTO tram_telemetry_history (
                    vehicle_number, line, brigade, lat, lon, speed_kmh, gps_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
          records,
      )


async def tram_telemetry_poller_task():
  """Worker cykliczny rejestrujący telemetrię składów w trybie Append-Only."""
  if not settings.WAW_API_KEY:
    logger.warning("[TRAM_TELEMETRY] Brak WAW_API_KEY w .env. Worker zatrzymany.")
    return

  params = {
      "apikey": settings.WAW_API_KEY,
      "type": 2,
      "resource_id": UM_WARSZAWA_RESOURCE_ID,
  }

  async with httpx.AsyncClient(timeout=15.0) as client:
    logger.info("[TRAM_TELEMETRY] Start pętli ingestu telemetrii...")
    while True:
      try:
        res = await client.get(UM_WARSZAWA_VEHICLES_URL, params=params)
        if res.status_code == 200:
          items = res.json().get("result", [])
          if isinstance(items, list) and items:
            process_and_append_batch(items)
      except asyncio.CancelledError:
        logger.info("[TRAM_TELEMETRY] Zatrzymano workera telemetrii.")
        break
      except Exception as e:
        logger.warning(f"[TRAM_TELEMETRY] Błąd pobierania: {e}")

      await asyncio.sleep(10)