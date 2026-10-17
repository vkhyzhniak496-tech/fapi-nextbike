import asyncio
import json
import logging
from typing import Any, Dict, List
import httpx

from config import UM_WARSZAWA_RESOURCE_ID, UM_WARSZAWA_VEHICLES_URL, settings
from core.database import get_pool

logger = logging.getLogger(__name__)


async def ingest_tram_batch(items: List[Dict[str, Any]]) -> int:
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
                -- 1. Zrzucenie na tor z odcięciem błędów GPS > 25m
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
            with_platform AS (
                -- 2. Sprawdzenie, czy rzut na szynę jest w promieniu 25m od peronu
                SELECT 
                    s.*,
                    p.osm_id AS plat_osm_id,
                    p.name AS plat_name,
                    p.cluster_name AS plat_cluster
                FROM snapped s
                LEFT JOIN LATERAL (
                    SELECT osm_id, name, cluster_name
                    FROM tram_platforms
                    WHERE ST_DWithin(s.snapped_geom, geom_2180, 25.0)
                    ORDER BY s.snapped_geom <-> geom_2180
                    LIMIT 1
                ) p ON TRUE
            ),
            upserted AS (
                -- 3. Aktualizacja stanu wozów i detekcja opuszczenia przystanku
                INSERT INTO tram_live_state AS tls (
                    vehicle_number, line, brigade, raw_geom, 
                    snapped_geom_2180, dist_to_track, nearest_way_id, 
                    gps_time, current_stop_osm_id, dwell_start_time
                )
                SELECT 
                    w.v_num, w.line, w.brigade, w.raw_geom, 
                    w.snapped_geom, w.track_dist, w.track_id, 
                    w.gps_time,
                    CASE 
                        WHEN w.plat_osm_id IS NOT NULL THEN w.plat_osm_id
                        ELSE NULL 
                    END,
                    CASE 
                        WHEN w.plat_osm_id IS NOT NULL THEN w.gps_time
                        ELSE NULL 
                    END
                FROM with_platform w
                ON CONFLICT (vehicle_number) DO UPDATE SET
                    line = EXCLUDED.line,
                    brigade = EXCLUDED.brigade,
                    raw_geom = EXCLUDED.raw_geom,
                    snapped_geom_2180 = EXCLUDED.snapped_geom_2180,
                    dist_to_track = EXCLUDED.dist_to_track,
                    nearest_way_id = EXCLUDED.nearest_way_id,
                    
                    -- Jeśli wóz ruszył lub opuścił strefę peronu, zerujemy stan
                    current_stop_osm_id = CASE
                        WHEN EXCLUDED.current_stop_osm_id IS NOT NULL 
                             AND ST_Distance(EXCLUDED.snapped_geom_2180, tls.snapped_geom_2180) < 4.0 
                        THEN COALESCE(tls.current_stop_osm_id, EXCLUDED.current_stop_osm_id)
                        WHEN EXCLUDED.current_stop_osm_id IS NOT NULL 
                             AND tls.current_stop_osm_id IS NULL
                        THEN EXCLUDED.current_stop_osm_id
                        ELSE NULL
                    END,
                    
                    dwell_start_time = CASE
                        WHEN EXCLUDED.current_stop_osm_id IS NOT NULL 
                             AND ST_Distance(EXCLUDED.snapped_geom_2180, tls.snapped_geom_2180) < 4.0 
                        THEN COALESCE(tls.dwell_start_time, EXCLUDED.dwell_start_time)
                        WHEN EXCLUDED.current_stop_osm_id IS NOT NULL 
                             AND tls.current_stop_osm_id IS NULL
                        THEN EXCLUDED.dwell_start_time
                        ELSE NULL
                    END,
                    
                    gps_time = EXCLUDED.gps_time,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING 
                    tls.vehicle_number, 
                    tls.line, 
                    tls.brigade,
                    OLD.current_stop_osm_id AS prev_stop,
                    OLD.dwell_start_time AS prev_start,
                    EXCLUDED.gps_time AS curr_time,
                    tls.current_stop_osm_id AS curr_stop
            )
            -- 4. Jeśli wóz właśnie opuścił peron, a postój trwał 8-180s, zapisujemy zdarzenie
            INSERT INTO tram_dwell_events (
                vehicle_number, line, brigade, stop_name, cluster_name, 
                stop_osm_id, arrival_time, departure_time, duration_sec
            )
            SELECT 
                u.vehicle_number,
                u.line,
                u.brigade,
                p.name,
                p.cluster_name,
                u.prev_stop,
                u.prev_start,
                u.curr_time,
                ROUND(EXTRACT(EPOCH FROM (u.curr_time - u.prev_start))::numeric, 1)
            FROM upserted u
            JOIN tram_platforms p ON p.osm_id = u.prev_stop
            WHERE u.prev_stop IS NOT NULL 
              AND (u.curr_stop IS NULL OR u.curr_stop != u.prev_stop)
              AND EXTRACT(EPOCH FROM (u.curr_time - u.prev_start)) BETWEEN 8.0 AND 180.0
            ON CONFLICT (vehicle_number, stop_osm_id, arrival_time) DO NOTHING;
        """, json.dumps(prepared))

    return len(prepared)


async def tram_telemetry_poller_task() -> None:
    if not settings.WAW_API_KEY:
        logger.warning("[TRAM_TELEMETRY] Brak WAW_API_KEY w konfiguracji.")
        return

    params = {
        "apikey": settings.WAW_API_KEY,
        "type": 2,
        "resource_id": UM_WARSZAWA_RESOURCE_ID,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        logger.info("[TRAM_TELEMETRY] Uruchomiono poller telemetrii z detekcją postojów (Opcja B)...")
        while True:
            try:
                res = await client.get(UM_WARSZAWA_VEHICLES_URL, params=params)
                if res.status_code == 200:
                    data = res.json()
                    raw_items = data.get("result", [])
                    if isinstance(raw_items, list) and raw_items:
                        count = await ingest_tram_batch(raw_items)
                        logger.debug(f"[TRAM_TELEMETRY] Zaktualizowano pozycje: {count} wozów.")
                elif res.status_code == 429:
                    logger.warning("[TRAM_TELEMETRY] Rate limit ZTM (429). Czekam 30s...")
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
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