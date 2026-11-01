import asyncio
import json
import logging
from typing import Any, Dict, List
import httpx

from config import UM_WARSZAWA_RESOURCE_ID, UM_WARSZAWA_VEHICLES_URL, settings
from core.database import get_pool

logger = logging.getLogger(__name__)

INGEST_TRAMS_SQL = """
WITH raw_batch AS (
    SELECT 
        u.v_num, 
        u.line, 
        u.brigade,
        ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326) AS raw_geom,
        ST_Transform(ST_SetSRID(ST_MakePoint(u.lon, u.lat), 4326), 2180) AS geom_2180,
        to_timestamp(u.gps_time, 'YYYY-MM-DD HH24:MI:SS') AS gps_time
    FROM json_to_recordset($1::json) AS u(
        v_num TEXT, line TEXT, brigade TEXT, 
        lat DOUBLE PRECISION, lon DOUBLE PRECISION, gps_time TEXT
    )
),
snapped AS (
    -- 1. Zrzucenie na tor z tolerancją GPS do 25 metrów
    SELECT 
        r.*,
        e.way_id AS track_id,
        ST_ClosestPoint(e.geom_2180, r.geom_2180) AS snapped_geom,
        ST_Distance(e.geom_2180, r.geom_2180) AS track_dist
    FROM raw_batch r
    CROSS JOIN LATERAL (
        SELECT way_id, COALESCE(geom_2180, ST_Transform(geom, 2180)) AS geom_2180
        FROM tram_edges 
        ORDER BY COALESCE(geom_2180, ST_Transform(geom, 2180)) <-> r.geom_2180 
        LIMIT 1
    ) e
    WHERE ST_DWithin(e.geom_2180, r.geom_2180, 25.0)
),
with_platform AS (
    -- 2. Pobranie poprzedniego stanu i odrzucenie powtórzonych pingów
    SELECT 
        s.*,
        p.osm_id AS plat_osm_id,
        p.name AS plat_name,
        p.cluster_name AS plat_cluster,
        prev.current_stop_osm_id AS prev_stop_id,
        prev.dwell_start_time AS prev_dwell_start,
        prev.gps_time AS prev_gps_time,
        ST_Distance(s.snapped_geom, prev.snapped_geom_2180) AS dist_from_prev,
        EXTRACT(EPOCH FROM (s.gps_time - prev.gps_time)) AS delta_t_sec
    FROM snapped s
    LEFT JOIN tram_live_state prev ON prev.vehicle_number = s.v_num
    LEFT JOIN LATERAL (
        SELECT osm_id, name, cluster_name
        FROM tram_platforms
        WHERE ST_DWithin(s.snapped_geom, COALESCE(geom_2180, ST_Transform(geom, 2180)), 35.0)
        ORDER BY s.snapped_geom <-> COALESCE(geom_2180, ST_Transform(geom, 2180))
        LIMIT 1
    ) p ON TRUE
    WHERE prev.gps_time IS NULL OR s.gps_time > prev.gps_time
),
evaluated_dwells AS (
    -- 3. Estymacja czasu trwania postoju przy odjeździe
    SELECT
        w.v_num,
        w.line,
        w.brigade,
        plat.name AS stop_name,
        plat.cluster_name,
        w.prev_stop_id AS stop_osm_id,
        w.prev_dwell_start AS arrival_time,
        w.gps_time AS departure_time,
        CASE
            -- A. Postój wielopingowy (>= 2 kolejne odczyty przy peronie)
            WHEN w.prev_gps_time > w.prev_dwell_start THEN
                ROUND((
                    EXTRACT(EPOCH FROM (w.prev_gps_time - w.prev_dwell_start))
                    + LEAST(6.0, COALESCE(w.delta_t_sec, 10.0) * 0.4)
                )::numeric, 1)

            -- B. Postój jednopingowy (wymiana pasażerska w oknie jednego interwału)
            WHEN w.prev_gps_time = w.prev_dwell_start
                 AND w.delta_t_sec BETWEEN 9.0 AND 40.0
                 AND COALESCE(w.dist_from_prev, 0.0) / GREATEST(w.delta_t_sec, 1.0) < 4.5 THEN
                ROUND(GREATEST(8.0, LEAST(24.0, w.delta_t_sec - (COALESCE(w.dist_from_prev, 0.0) / 7.0)))::numeric, 1)
            ELSE NULL
        END AS calc_duration_sec
    FROM with_platform w
    JOIN tram_platforms plat ON plat.osm_id = w.prev_stop_id
    WHERE w.prev_stop_id IS NOT NULL
      AND w.prev_dwell_start IS NOT NULL
      AND (w.plat_osm_id IS NULL OR w.plat_osm_id != w.prev_stop_id OR COALESCE(w.dist_from_prev, 0.0) >= 12.0)
),
completed_dwells AS (
    -- 4. Zapis zweryfikowanych postojów (8 - 180s)
    INSERT INTO tram_dwell_events (
        vehicle_number, line, brigade, stop_name, cluster_name, 
        stop_osm_id, arrival_time, departure_time, duration_sec
    )
    SELECT 
        v_num, line, brigade, stop_name, cluster_name,
        stop_osm_id, arrival_time, departure_time, calc_duration_sec
    FROM evaluated_dwells
    WHERE calc_duration_sec BETWEEN 8.0 AND 180.0
    ON CONFLICT (vehicle_number, stop_osm_id, arrival_time) DO NOTHING
)
-- 5. Aktualizacja wektora bieżącego w tram_live_state
INSERT INTO tram_live_state AS tls (
    vehicle_number, line, brigade, raw_geom, 
    snapped_geom_2180, dist_to_track, nearest_way_id, 
    speed_kmh, gps_time, current_stop_osm_id, dwell_start_time
)
SELECT 
    w.v_num, w.line, w.brigade, w.raw_geom, 
    w.snapped_geom, w.track_dist, w.track_id,
    CASE 
        WHEN w.delta_t_sec > 0 AND w.dist_from_prev IS NOT NULL 
        THEN ROUND(LEAST(90.0, (w.dist_from_prev / w.delta_t_sec) * 3.6)::numeric, 1)
        ELSE 0.0
    END AS speed_kmh,
    w.gps_time,
    CASE 
        WHEN w.plat_osm_id IS NOT NULL AND (w.prev_stop_id IS NULL OR w.prev_stop_id != w.plat_osm_id)
        THEN w.plat_osm_id
        WHEN w.plat_osm_id IS NOT NULL AND w.prev_stop_id = w.plat_osm_id AND COALESCE(w.dist_from_prev, 0.0) < 12.0
        THEN w.plat_osm_id
        ELSE NULL 
    END,
    CASE 
        WHEN w.plat_osm_id IS NOT NULL AND (w.prev_stop_id IS NULL OR w.prev_stop_id != w.plat_osm_id)
        THEN w.gps_time
        WHEN w.plat_osm_id IS NOT NULL AND w.prev_stop_id = w.plat_osm_id AND COALESCE(w.dist_from_prev, 0.0) < 12.0
        THEN COALESCE(w.prev_dwell_start, w.gps_time)
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
    speed_kmh = EXCLUDED.speed_kmh,
    current_stop_osm_id = EXCLUDED.current_stop_osm_id,
    dwell_start_time = EXCLUDED.dwell_start_time,
    gps_time = EXCLUDED.gps_time,
    updated_at = CURRENT_TIMESTAMP;
"""


async def ingest_tram_batch(items: List[Dict[str, Any]]) -> int:
    prepared = []
    for item in items:
        try:
            v_num = str(item.get("VehicleNumber", "")).strip()
            line = str(item.get("Lines", "")).strip()
            brigade = str(item.get("Brigade", "")).strip()
            lat = float(item.get("Lat"))
            lon = float(item.get("Lon"))
            gps_time = str(item.get("Time", "")).strip()

            if not (v_num and line and gps_time and len(gps_time) >= 19):
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
        await conn.execute(INGEST_TRAMS_SQL, json.dumps(prepared))

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
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TransitGIS/1.0",
        "Accept": "application/json",
    }
    timeout_config = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)

    async with httpx.AsyncClient(headers=headers, timeout=timeout_config) as client:
        logger.info("[TRAM_TELEMETRY] Uruchomiono poller telemetrii ZTM...")
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
            except (httpx.ReadError, httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadTimeout) as net_err:
                logger.warning(f"[TRAM_TELEMETRY] Chwilowy błąd sieciowy ZTM ({type(net_err).__name__}). Ponawiam...")
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TRAM_TELEMETRY] Błąd w pętli: {e}", exc_info=True)

            await asyncio.sleep(10)


async def tram_analytics_cleaner_task() -> None:
    """Czyszczenie starych danych telemetrii co 30 minut."""
    logger.info("[TRAM_CLEANER] Uruchomiono harmonogram czyszczenia telemetrii...")
    await asyncio.sleep(60)

    while True:
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                res = await conn.execute(
                    "DELETE FROM tram_live_state WHERE gps_time < NOW() - INTERVAL '1 hour';"
                )
                logger.info(f"[TRAM_CLEANER] Wyczyszczono nieaktywne wozy: {res}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[TRAM_CLEANER] Błąd cleanera: {e}")

        await asyncio.sleep(1800)