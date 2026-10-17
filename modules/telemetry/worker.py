import asyncio
from datetime import datetime
import json
import logging
from typing import Any, Dict, List
import httpx

from config import (
    UM_WARSZAWA_RESOURCE_ID,
    UM_WARSZAWA_VEHICLES_URL,
    settings,
)
from core.database import get_pool

logger = logging.getLogger(__name__)


async def ingest_tram_batch(items: List[Dict[str, Any]]) -> int:
    """
    Przyjmuje surową paczkę z ZTM, rzutuje punkty na torowisko tram_edges
    oraz oblicza metryczną prędkość i aktualizuje stan w PostGIS.
    """
    prepared = []
    for item in items:
        try:
            v_num = str(item.get("VehicleNumber", "")).strip()
            line = str(item.get("Lines", "")).strip()
            brigade = str(item.get("Brigade", "")).strip()
            lat = float(item.get("Lat"))
            lon = float(item.get("Lon"))
            gps_time = str(item.get("Time")).strip()

            if not (v_num and line and gps_time):
                continue

            prepared.append({
                "v_num": v_num,
                "line": line,
                "brigade": brigade,
                "lat": lat,
                "lon": lon,
                "gps_time": gps_time
            })
        except (KeyError, ValueError, TypeError):
            continue

    if not prepared:
        return 0

    pool = get_pool()
    async with pool.acquire() as conn:
        # Jedno atomowe zapytanie realizujące Map-Matching i różnicową prędkość 1D
        await conn.execute("""
            WITH raw_batch AS (
                SELECT 
                    u.v_num, u.line, u.brigade,
                    ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326) AS raw_geom,
                    ST_Transform(ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326), 2180) AS geom_2180,
                    u.gps_time::timestamptz AS gps_time
                FROM json_to_recordset($1::json) AS u(
                    v_num TEXT, line TEXT, brigade TEXT, 
                    lat DOUBLE PRECISION, lon DOUBLE PRECISION, gps_time TEXT
                )
            ),
            snapped AS (
                SELECT 
                    r.*,
                    e.way_id AS track_id,
                    ST_ClosestPoint(ST_Transform(e.geom, 2180), r.geom_2180) AS snapped_geom,
                    ST_Distance(ST_Transform(e.geom, 2180), r.geom_2180) AS track_dist
                FROM raw_batch r
                CROSS JOIN LATERAL (
                    SELECT way_id, geom 
                    FROM tram_edges 
                    ORDER BY ST_Transform(geom, 2180) <-> r.geom_2180 
                    LIMIT 1
                ) e
                WHERE ST_DWithin(ST_Transform(e.geom, 2180), r.geom_2180, 25.0)
            ),
            upserted AS (
                INSERT INTO tram_live_state AS tls (
                    vehicle_number, line, brigade, raw_geom, 
                    snapped_geom_2180, dist_to_track, nearest_way_id, 
                    speed_kmh, gps_time
                )
                SELECT 
                    s.v_num, s.line, s.brigade, s.raw_geom, 
                    s.snapped_geom, s.track_dist, s.track_id, 
                    0.0, s.gps_time
                FROM snapped s
                ON CONFLICT (vehicle_number) DO UPDATE SET
                    line = EXCLUDED.line,
                    brigade = EXCLUDED.brigade,
                    raw_geom = EXCLUDED.raw_geom,
                    snapped_geom_2180 = EXCLUDED.snapped_geom_2180,
                    dist_to_track = EXCLUDED.dist_to_track,
                    nearest_way_id = EXCLUDED.nearest_way_id,
                    -- Różnicowa prędkość w metrach na sekundę przeliczona na km/h:
                    speed_kmh = CASE 
                        WHEN EXTRACT(EPOCH FROM (EXCLUDED.gps_time - tls.gps_time)) BETWEEN 1.0 AND 120.0 
                             AND ST_Distance(EXCLUDED.snapped_geom_2180, tls.snapped_geom_2180) >= 4.0 THEN
                            LEAST(
                                75.0, 
                                ROUND((ST_Distance(EXCLUDED.snapped_geom_2180, tls.snapped_geom_2180) 
                                       / EXTRACT(EPOCH FROM (EXCLUDED.gps_time - tls.gps_time)) * 3.6)::numeric, 1)
                            )
                        ELSE 0.0
                    END,
                    gps_time = EXCLUDED.gps_time,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING tls.vehicle_number, tls.line, tls.speed_kmh, tls.gps_time, tls.snapped_geom_2180
            )
            -- Opcjonalne zrzucenie do historii telemetrii:
            INSERT INTO tram_telemetry_history (vehicle_number, line, speed_kmh, gps_time, geom_2180)
            SELECT vehicle_number, line, speed_kmh, gps_time, snapped_geom_2180
            FROM upserted;
        """, json.dumps(prepared))

    return len(prepared)


async def tram_telemetry_poller_task() -> None:
    """Główna pętla pobierająca pozycje z ZTM i przekazująca je do PostGIS."""
    if not settings.WAW_API_KEY:
        logger.warning("[TRAM_TELEMETRY] Brak WAW_API_KEY w konfiguracji.")
        return

    params = {
        "apikey": settings.WAW_API_KEY,
        "type": 2,
        "resource_id": UM_WARSZAWA_RESOURCE_ID,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        logger.info("[TRAM_TELEMETRY] Uruchomiono poller telemetrii PostGIS...")
        while True:
            try:
                res = await client.get(UM_WARSZAWA_VEHICLES_URL, params=params)
                if res.status_code == 200:
                    data = res.json()
                    raw_items = data.get("result", [])
                    if isinstance(raw_items, list) and raw_items:
                        count = await ingest_tram_batch(raw_items)
                        logger.debug(f"[TRAM_TELEMETRY] Zaktualizowano pozycje dla {count} składów.")
                elif res.status_code == 429:
                    logger.warning("[TRAM_TELEMETRY] Rate limit ZTM (429). Czekam 30s...")
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                logger.info("[TRAM_TELEMETRY] Zatrzymano poller telemetrii.")
                break
            except Exception as e:
                logger.warning(f"[TRAM_TELEMETRY] Błąd w pętli telemetrii: {e}")

            await asyncio.sleep(10)


async def tram_analytics_cleaner_task() -> None:
    """Czyszczenie historii starszej niż 4 godziny."""
    logger.info("[TRAM_CLEANER] Uruchomiono harmonogram czyszczenia telemetrii...")
    await asyncio.sleep(60)

    while True:
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                res = await conn.execute(
                    "DELETE FROM tram_telemetry_history WHERE gps_time < NOW() - INTERVAL '4 hours';"
                )
                logger.info(f"[TRAM_CLEANER] Wyczyszczono przeterminowaną telemetrię: {res}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[TRAM_CLEANER] Błąd cleanera: {e}")

        await asyncio.sleep(1800)