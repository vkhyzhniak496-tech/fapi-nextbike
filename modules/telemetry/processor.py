import logging
from core.database import get_pool

logger = logging.getLogger(__name__)

ANALYTICS_SQL = """
-- 1. Dopasowanie aktualnych wozów do najbliższych peronów (promień 25m od osi toru)
CREATE TEMP TABLE matched_live ON COMMIT DROP AS
SELECT 
    ls.vehicle_number,
    ls.line,
    ls.brigade,
    ls.speed_kmh,
    ls.gps_time,
    plat.name AS stop_name,
    plat.cluster_name
FROM tram_live_state ls
CROSS JOIN LATERAL (
    SELECT name, cluster_name
    FROM tram_platforms
    WHERE ST_DWithin(ls.snapped_geom_2180, geom_2180, 25.0)
    ORDER BY ls.snapped_geom_2180 <-> geom_2180
    LIMIT 1
) plat
WHERE ls.updated_at > NOW() - INTERVAL '30 seconds';

-- 2. Wykrycie postojów, które właśnie się zakończyły (tramwaj opuścił strefę peronu)
CREATE TEMP TABLE dwells_to_close ON COMMIT DROP AS
SELECT 
    a.vehicle_number,
    a.line,
    a.brigade,
    a.stop_name,
    a.cluster_name,
    a.start_time,
    a.last_time,
    ROUND(EXTRACT(EPOCH FROM (a.last_time - a.start_time))::numeric, 1) AS duration_sec,
    a.min_speed_kmh,
    a.pings_count
FROM tram_active_dwells a
LEFT JOIN matched_live m ON a.vehicle_number = m.vehicle_number AND a.stop_name = m.stop_name
WHERE m.vehicle_number IS NULL;

-- 3. Zapis kwalifikowanych postojów do tram_dwell_events (8s - 900s, prędkość minimalna <= 4.0 km/h)
INSERT INTO tram_dwell_events (
    vehicle_number, line, brigade, stop_name, cluster_name,
    arrival_time, departure_time, duration_sec, min_speed_kmh, pings_count
)
SELECT 
    vehicle_number, line, brigade, stop_name, cluster_name,
    start_time, last_time, duration_sec, min_speed_kmh, pings_count
FROM dwells_to_close
WHERE duration_sec BETWEEN 8.0 AND 900.0
  AND min_speed_kmh <= 4.0
ON CONFLICT (vehicle_number, stop_name, arrival_time) DO NOTHING;

-- 4. Rejestracja zakończonego przelotu między przystankami (do korytarzy grafowych)
INSERT INTO tram_direct_segments (line, from_cluster, to_cluster, avg_duration_sec, samples_count, last_seen)
SELECT 
    m.line,
    d.cluster_name AS from_cluster,
    m.cluster_name AS to_cluster,
    ROUND(EXTRACT(EPOCH FROM (m.gps_time - d.departure_time))::numeric, 1) AS flight_sec,
    1,
    CURRENT_TIMESTAMP
FROM matched_live m
JOIN tram_last_departures d ON m.vehicle_number = d.vehicle_number
WHERE m.cluster_name != d.cluster_name
  AND m.line = d.line
  AND EXTRACT(EPOCH FROM (m.gps_time - d.departure_time)) BETWEEN 15.0 AND 720.0
ON CONFLICT (line, from_cluster, to_cluster) DO UPDATE SET
    avg_duration_sec = ROUND(
        ((tram_direct_segments.avg_duration_sec * tram_direct_segments.samples_count + EXCLUDED.avg_duration_sec) 
        / (tram_direct_segments.samples_count + 1))::numeric, 1
    ),
    samples_count = tram_direct_segments.samples_count + 1,
    last_seen = CURRENT_TIMESTAMP;

-- 5. Aktualizacja punktu ostatniego odjazdu wozu
INSERT INTO tram_last_departures (vehicle_number, cluster_name, departure_time, line)
SELECT vehicle_number, cluster_name, last_time, line
FROM dwells_to_close
WHERE cluster_name IS NOT NULL
ON CONFLICT (vehicle_number) DO UPDATE SET
    cluster_name = EXCLUDED.cluster_name,
    departure_time = EXCLUDED.departure_time,
    line = EXCLUDED.line;

-- 6. Usunięcie sfinalizowanych ze stanu aktywnego
DELETE FROM tram_active_dwells
WHERE vehicle_number IN (SELECT vehicle_number FROM dwells_to_close);

-- 7. Upsert aktywnych postojów (dla wozów stojących lub toczących się przy peronie <= 4 km/h)
INSERT INTO tram_active_dwells (
    vehicle_number, line, brigade, stop_name, cluster_name,
    start_time, last_time, min_speed_kmh, pings_count
)
SELECT 
    vehicle_number, line, brigade, stop_name, cluster_name,
    gps_time, gps_time, speed_kmh, 1
FROM matched_live
WHERE speed_kmh <= 4.0
ON CONFLICT (vehicle_number) DO UPDATE SET
    stop_name = EXCLUDED.stop_name,
    cluster_name = EXCLUDED.cluster_name,
    last_time = EXCLUDED.last_time,
    min_speed_kmh = LEAST(tram_active_dwells.min_speed_kmh, EXCLUDED.min_speed_kmh),
    pings_count = tram_active_dwells.pings_count + 1;
"""


async def run_dwell_analytics() -> None:
    """Wyzwalane w cyklu telemetrycznym po zrzutowaniu punktów na tory."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(ANALYTICS_SQL)
    except Exception as e:
        logger.error(f"[ANALYTICS] Błąd potoku postojów: {e}")