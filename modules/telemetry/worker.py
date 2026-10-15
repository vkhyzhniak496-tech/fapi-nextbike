import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List, Optional
import httpx
from pydantic import ValidationError

from config import (
    UM_WARSZAWA_RESOURCE_ID,
    UM_WARSZAWA_VEHICLES_URL,
    settings,
)
from core.database import get_pool
from core.geo import calculate_speed_kmh
from core.models import TramRawTelemetry
from modules.telemetry.processor import analytics_engine

logger = logging.getLogger(__name__)

# Podręczny cache w RAM na bieżący stan wozów (do natychmiastowego serwowania /live i różnicowej prędkości)
LAST_TRAM_POSITIONS: Dict[str, TramRawTelemetry] = {}


async def process_and_append_batch(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Waliduje telemetrię, wylicza prędkość różnicową, aktualizuje stan w RAM
    oraz zapisuje bufor wierszy asynchronicznie do bazy PostgreSQL.
    """
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

        # Weryfikacja chronologii i odrzucanie duplikatów pingu
        prev_telemetry = LAST_TRAM_POSITIONS.get(v_num)
        if prev_telemetry and gps_dt <= prev_telemetry.gps_time:
            continue

        # Obliczenie prędkości różnicowej na bazie odległości i czasu
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

        # Rekord pod asynchroniczny INSERT do tabeli tram_telemetry_history
        records_to_insert.append((
            telemetry.vehicle_number,
            telemetry.line,
            telemetry.brigade,
            telemetry.lat,
            telemetry.lon,
            telemetry.speed_kmh,
            telemetry.gps_time,
        ))

        # Przygotowanie zserializowanego słownika dla silnika cKDTree / postojów
        enriched_live_items.append({
            "VehicleNumber": telemetry.vehicle_number,
            "Lines": telemetry.line,
            "Brigade": telemetry.brigade,
            "Lat": telemetry.lat,
            "Lon": telemetry.lon,
            "Speed": telemetry.speed_kmh,
            "Time": telemetry.gps_time,
        })

    # Hurtowy, nieblokujący zapis bufora telemetrii do PostgreSQL
    if records_to_insert:
        pool = get_pool()
        try:
            async with pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO tram_telemetry_history (
                        vehicle_number, line, brigade, lat, lon, speed_kmh, gps_time
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7);
                    """,
                    records_to_insert,
                )
        except Exception as e:
            logger.error(f"[TRAM_TELEMETRY] Błąd zapisu telemetrii do PostgreSQL: {e}")

    return enriched_live_items


async def tram_telemetry_poller_task() -> None:
    """Cykliczny worker zbierający telemetrię składów tramwajowych z miejskiego API."""
    if not settings.WAW_API_KEY:
        logger.warning("[TRAM_TELEMETRY] Brak WAW_API_KEY w konfiguracji. Worker zatrzymany.")
        return

    params = {
        "apikey": settings.WAW_API_KEY,
        "type": 2,
        "resource_id": UM_WARSZAWA_RESOURCE_ID,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        logger.info("[TRAM_TELEMETRY] Uruchomiono pętlę pobierania telemetrii (PostgreSQL)...")
        while True:
            try:
                res = await client.get(UM_WARSZAWA_VEHICLES_URL, params=params)
                if res.status_code == 200:
                    data = res.json()
                    raw_items = data.get("result", [])
                    if isinstance(raw_items, list) and raw_items:
                        # 1. Zapis do tram_telemetry_history i aktualizacja RAM
                        enriched = await process_and_append_batch(raw_items)
                        # 2. Wyznaczenie postojów na peronach i segmentów przelotowych
                        if enriched:
                            await analytics_engine.process_live_batch(enriched)
                elif res.status_code == 429:
                    logger.warning("[TRAM_TELEMETRY] Rate limit API UM Warszawa. Pauza 30s...")
                    await asyncio.sleep(30)
                else:
                    logger.warning(f"[TRAM_TELEMETRY] API ZTM zwróciło status: {res.status_code}")
            except asyncio.CancelledError:
                logger.info("[TRAM_TELEMETRY] Zatrzymano zadanie workera telemetrii.")
                break
            except Exception as e:
                logger.warning(f"[TRAM_TELEMETRY] Błąd w pętli telemetrii: {e}")

            await asyncio.sleep(10)


async def tram_analytics_cleaner_task() -> None:
    """
    Cykliczne sprzątanie retencyjne w PostgreSQL:
    - Usuwa telemetrię surową starszą niż 4 godziny (bufor trajektorii /track).
    - Usuwa zarejestrowane postoje starsze niż 14 dni.
    """
    logger.info("[TRAM_CLEANER] Inicjalizacja harmonogramu czyszczenia bazy PostgreSQL...")
    await asyncio.sleep(60)

    while True:
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                # 1. Czyszczenie bufora telemetrii
                res_hist = await conn.execute(
                    "DELETE FROM tram_telemetry_history WHERE gps_time < NOW() - INTERVAL '4 hours';"
                )
                # 2. Czyszczenie starych postojów
                res_dwell = await conn.execute(
                    "DELETE FROM tram_dwell_events WHERE arrival_time < NOW() - INTERVAL '14 days';"
                )
                logger.info(
                    f"[TRAM_CLEANER] Usunięto przeterminowane dane: telemetria ({res_hist}), postoje ({res_dwell})."
                )
        except asyncio.CancelledError:
            logger.info("[TRAM_CLEANER] Zatrzymano zadanie sprzątające.")
            break
        except Exception as e:
            logger.error(f"[TRAM_CLEANER] Błąd procedury czyszczenia: {e}")

        # Powtórzenie co 30 minut
        await asyncio.sleep(1800)