import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional
import httpx
from pydantic import ValidationError

from config import (
    UM_WARSZAWA_RESOURCE_ID,
    UM_WARSZAWA_VEHICLES_URL,
    settings,
)
from core.database import TRAM_LIVE_DB_PATH, get_db_cursor, transaction
from core.geo import calculate_speed_kmh
from core.models import TramRawTelemetry
from modules.telemetry.processor import analytics_engine

logger = logging.getLogger(__name__)

# Cache w RAM do natychmiastowego serwowania /live i różnicowego liczenia prędkości
# {vehicle_number: TramRawTelemetry}
LAST_TRAM_POSITIONS: Dict[str, TramRawTelemetry] = {}


def process_and_append_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records_to_insert = []
    enriched_live_items = []

    for item in items:
        v_num = str(item.get("VehicleNumber", "")).strip()
        line = str(item.get("Lines", "")).strip()
        brigade = str(item.get("Brigade", "")).strip()
        lat = item.get("Lat")
        lon = item.get("Lon")
        gps_time_raw = item.get("Time")

        if not (v_num and line and brigade and lat and lon and gps_time_raw):
            continue

        try:
            gps_dt = datetime.strptime(str(gps_time_raw), "%Y-%m-%d %H:%M:%S")
            lat_f = float(lat)
            lon_f = float(lon)
        except (ValueError, TypeError):
            continue

        # Weryfikacja chronologii
        prev_telemetry = LAST_TRAM_POSITIONS.get(v_num)
        if prev_telemetry and gps_dt <= prev_telemetry.gps_time:
            continue

        # Obliczenie prędkości różnicowej
        speed = 0.0
        if prev_telemetry:
            speed = calculate_speed_kmh(
                prev_lat=prev_telemetry.lat,
                prev_lon=prev_telemetry.lon,
                prev_time=prev_telemetry.gps_time,
                new_lat=lat_f,
                new_lon=lon_f,
                new_time=gps_dt,
            )

        try:
            telemetry = TramRawTelemetry(
                vehicle_number=v_num,
                line=line,
                brigade=brigade,
                lat=lat_f,
                lon=lon_f,
                speed_kmh=speed,
                gps_time=gps_dt,
            )
        except ValidationError:
            continue

        LAST_TRAM_POSITIONS[v_num] = telemetry
        records_to_insert.append((
            telemetry.vehicle_number,
            telemetry.line,
            telemetry.brigade,
            telemetry.lat,
            telemetry.lon,
            telemetry.speed_kmh,
            telemetry.gps_time.strftime("%Y-%m-%d %H:%M:%S"),
        ))

        # Przygotowanie zserializowanego słownika ze zliczoną prędkością dla analityki w locie
        enriched_live_items.append({
            "VehicleNumber": telemetry.vehicle_number,
            "Lines": telemetry.line,
            "Brigade": telemetry.brigade,
            "Lat": telemetry.lat,
            "Lon": telemetry.lon,
            "Speed": telemetry.speed_kmh,
            "Time": telemetry.gps_time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    if records_to_insert:
        with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
            with transaction(cur):
                cur.executemany(
                    """
                    INSERT INTO tram_telemetry_history (
                        vehicle_number, line, brigade, lat, lon, speed_kmh, gps_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    records_to_insert,
                )

    return enriched_live_items


async def tram_telemetry_poller_task() -> None:
    """Cykliczny worker zbierający telemetrię składów w trybie Append-Only."""
    if not settings.WAW_API_KEY:
        logger.warning("[TRAM_TELEMETRY] Brak WAW_API_KEY w konfiguracji. Worker zatrzymany.")
        return

    params = {
        "apikey": settings.WAW_API_KEY,
        "type": 2,
        "resource_id": UM_WARSZAWA_RESOURCE_ID,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        logger.info("[TRAM_TELEMETRY] Uruchomiono pętlę pobierania telemetrii...")
        while True:
            try:
                res = await client.get(UM_WARSZAWA_VEHICLES_URL, params=params)
                if res.status_code == 200:
                    data = res.json()
                    raw_items = data.get("result", [])
                    if isinstance(raw_items, list) and raw_items:
                        # 1. Zapis do historii i obliczenie prędkości
                        enriched = process_and_append_batch(raw_items)
                        # 2. Błyskawiczna analiza postojów w pamięci RAM
                        if enriched:
                            analytics_engine.process_live_batch(enriched)
            except asyncio.CancelledError:
                logger.info("[TRAM_TELEMETRY] Zatrzymano zadanie workera.")
                break
            except Exception as e:
                logger.warning(f"[TRAM_TELEMETRY] Błąd odpytywania API: {e}")

            await asyncio.sleep(10)


async def tram_analytics_worker_task() -> None:
    """Asynchroniczny worker periodycznie przeliczający postoje tramwajów w tle."""
    logger.info("[TRAM_ANALYTICS] Uruchomiono periodyczny proces analizy postojów...")
    await asyncio.sleep(15)

    while True:
        try:
            inserted_count = await asyncio.to_thread(
                analytics_engine.analyze_recent_telemetry, lookback_minutes=60
            )
            if inserted_count > 0:
                logger.info(f"[TRAM_ANALYTICS] Zsynchronizowano {inserted_count} nowych postojów.")
        except asyncio.CancelledError:
            logger.info("[TRAM_ANALYTICS] Zatrzymano proces analityki.")
            break
        except Exception as e:
            logger.error(f"[TRAM_ANALYTICS] Błąd podczas przeliczania postojów: {e}")

        await asyncio.sleep(60)
async def cleanup_old_telemetry_task():
  """Uruchamia się raz na dobę i usuwa dane starsze niż 48h."""
  while True:
    await asyncio.sleep(86400)  # 24 godziny
    try:
      with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
        with transaction(cur):
          cur.execute("""
                        DELETE FROM tram_telemetry_history 
                        WHERE gps_time < datetime('now', '-2 days');
                    """)
      logger.info(
          "[CLEANUP] Wyczyszczono stara telemetrie z tram_telemetry_history."
      )
    except Exception as e:
      logger.error(f"[CLEANUP] Blad podczas usuwania historii: {e}")