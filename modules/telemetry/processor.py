from datetime import datetime
import logging
from typing import Any, Dict, List
from core.database import get_pool

logger = logging.getLogger(__name__)

SQL_ANALYTICS_PIPELINE = """
-- KROK 1: Przestrzenne dopasowanie najbliższego peronu w buforze 40m (k-NN na indeksie GiST)
CREATE TEMP TABLE matched_pings ON COMMIT DROP AS
SELECT 
    p.v_num,
    p.line,
    p.brigade,
    p.speed,
    p.gps_dt,
    plat.name AS stop_name,
    plat.cluster_name,
    ROUND(ST_Distance(p.geom_2180, plat.geom_2180)::numeric, 1) AS dist_m
FROM current_pings p
LEFT JOIN LATERAL (
    SELECT name, cluster_name, geom_2180
    FROM tram_platforms
    WHERE ST_DWithin(p.geom_2180, geom_2180, 40.0)
    ORDER BY p.geom_2180 <-> geom_2180
    LIMIT 1
) plat ON TRUE;

-- KROK 2: Wykrycie zdarzeń do sfinalizowania (tramwaj opuścił słupek lub zmienił zespół)
CREATE TEMP TABLE events_to_finalize ON COMMIT DROP AS
SELECT 
    a.vehicle_number,
    a.line,
    a.brigade,
    a.stop_name,
    a.cluster_name,
    a.start_time,
    a.last_time,
    ROUND((EXTRACT(EPOCH FROM (a.last_time - a.start_time)))::numeric, 1) AS duration_sec,
    a.min_speed_kmh,
    a.min_dist_m,
    a.pings_count
FROM tram_active_dwells a
LEFT JOIN matched_pings m ON a.vehicle_number = m.v_num
WHERE m.v_num IS NULL 
   OR m.cluster_name IS NULL 
   OR m.cluster_name != a.cluster_name;

-- KROK 3: Wykrycie zakończonych przelotów międzyzespołowych (A -> B)
CREATE TEMP TABLE completed_segments ON COMMIT DROP AS
SELECT 
    m.line,
    d.cluster_name AS from_cluster,
    m.cluster_name AS to_cluster,
    ROUND((EXTRACT(EPOCH FROM (m.gps_dt - d.departure_time)))::numeric, 1) AS flight_sec
FROM matched_pings m
JOIN tram_last_departures d ON m.v_num = d.vehicle_number
WHERE m.cluster_name IS NOT NULL
  AND d.cluster_name != m.cluster_name
  AND d.line = m.line
  AND EXTRACT(EPOCH FROM (m.gps_dt - d.departure_time)) BETWEEN 8.0 AND 900.0
  AND NOT EXISTS (
      SELECT 1 FROM tram_active_dwells a 
      WHERE a.vehicle_number = m.v_num AND a.cluster_name = m.cluster_name
  );

-- KROK 4: Zapis zakwalifikowanych postojów do tram_dwell_events
INSERT INTO tram_dwell_events (
    vehicle_number, line, brigade, stop_name, cluster_name,
    arrival_time, departure_time, duration_sec, min_speed_kmh,
    min_dist_m, pings_count
)
SELECT 
    vehicle_number, line, brigade, stop_name, cluster_name,
    start_time, last_time, duration_sec, min_speed_kmh,
    min_dist_m, pings_count
FROM events_to_finalize
WHERE (min_speed_kmh <= 6.0 OR duration_sec >= 8.0) 
  AND duration_sec BETWEEN 4.0 AND 900.0
ON CONFLICT (vehicle_number, stop_name, arrival_time) DO NOTHING;

-- KROK 5: Aktualizacja ostatnich odjazdów
INSERT INTO tram_last_departures (vehicle_number, cluster_name, departure_time, line)
SELECT vehicle_number, cluster_name, last_time, line
FROM events_to_finalize
WHERE cluster_name IS NOT NULL
ON CONFLICT (vehicle_number) DO UPDATE SET
    cluster_name = EXCLUDED.cluster_name,
    departure_time = EXCLUDED.departure_time,
    line = EXCLUDED.line;

-- KROK 6: Usunięcie zakończonych postojów ze stanu aktywnego
DELETE FROM tram_active_dwells
WHERE vehicle_number IN (SELECT vehicle_number FROM events_to_finalize);

-- KROK 7: Upsert aktywnych postojów wozów
INSERT INTO tram_active_dwells (
    vehicle_number, line, brigade, stop_name, cluster_name,
    start_time, last_time, min_speed_kmh, min_dist_m, pings_count
)
SELECT 
    v_num, line, brigade, stop_name, cluster_name,
    gps_dt, gps_dt, speed, dist_m, 1
FROM matched_pings
WHERE stop_name IS NOT NULL AND cluster_name IS NOT NULL
ON CONFLICT (vehicle_number) DO UPDATE SET
    stop_name = EXCLUDED.stop_name,
    cluster_name = EXCLUDED.cluster_name,
    last_time = EXCLUDED.last_time,
    min_speed_kmh = LEAST(tram_active_dwells.min_speed_kmh, EXCLUDED.min_speed_kmh),
    min_dist_m = LEAST(tram_active_dwells.min_dist_m, EXCLUDED.min_dist_m),
    pings_count = tram_active_dwells.pings_count + 1;

-- KROK 8: Aktualizacja korytarzy w tram_direct_segments
INSERT INTO tram_direct_segments (
    line, from_cluster, to_cluster, avg_duration_sec, samples_count, last_seen
)
SELECT 
    line, from_cluster, to_cluster, flight_sec, 1, CURRENT_TIMESTAMP
FROM completed_segments
ON CONFLICT (line, from_cluster, to_cluster) DO UPDATE SET
    avg_duration_sec = ROUND(
        ((tram_direct_segments.avg_duration_sec * tram_direct_segments.samples_count + EXCLUDED.avg_duration_sec) 
        / (tram_direct_segments.samples_count + 1))::numeric, 
        1
    ),
    samples_count = tram_direct_segments.samples_count + 1,
    last_seen = CURRENT_TIMESTAMP;
"""


class PostgresTelemetryAnalyticsEngine:

    async def process_live_batch(
        self, telemetry_rows: List[Dict[str, Any]]
    ) -> None:
        if not telemetry_rows:
            return

        batch = [
            (
                str(r.get("VehicleNumber") or r.get("vehicle_number") or "").strip(),
                str(r.get("Lines") or r.get("line") or "").strip(),
                str(r.get("Brigade") or r.get("brigade") or "").strip(),
                float(r.get("Speed") or r.get("speed_kmh") or r.get("speed") or 0.0),
                float(r.get("Lon") if "Lon" in r else r.get("lon", 0.0)),
                float(r.get("Lat") if "Lat" in r else r.get("lat", 0.0)),
                r.get("Time") if isinstance(r.get("Time"), datetime) else datetime.now(),
            )
            for r in telemetry_rows
            if (r.get("VehicleNumber") or r.get("vehicle_number"))
            and (r.get("Lines") or r.get("line"))
            and (r.get("Lat") or r.get("lat"))
            and (r.get("Lon") or r.get("lon"))
        ]

        if not batch:
            return

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    CREATE TEMP TABLE current_pings (
                        v_num TEXT,
                        line TEXT,
                        brigade TEXT,
                        speed NUMERIC(4, 1),
                        geom_2180 GEOMETRY(Point, 2180),
                        gps_dt TIMESTAMPTZ
                    ) ON COMMIT DROP;
                """)

                await conn.executemany(
                    """
                    INSERT INTO current_pings (v_num, line, brigade, speed, geom_2180, gps_dt)
                    VALUES ($1, $2, $3, $4, ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), 4326), 2180), $7);
                    """,
                    batch,
                )

                await conn.execute(SQL_ANALYTICS_PIPELINE)


analytics_engine = PostgresTelemetryAnalyticsEngine()