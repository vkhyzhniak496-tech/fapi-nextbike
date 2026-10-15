from collections import defaultdict
from datetime import datetime, timedelta
import heapq
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from core.database import get_pool
from core.models import TramDwellEvent
from modules.telemetry.worker import LAST_TRAM_POSITIONS

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry & Analytics"])

_TRAVEL_TIME_CACHE: Dict[
    Tuple[str, str, Optional[str]], Tuple[float, Dict[str, Any]]
] = {}
_CACHE_TTL_SEC = 30


# ==============================================================================
# 1. Pozycje tramwajów na żywo i trajektorie wozów
# ==============================================================================

@router.get("/live")
async def get_live_tram_positions(line: Optional[str] = None) -> Dict[str, Any]:
    """Zwraca bieżące pozycje składów z pamięci RAM w formacie GeoJSON FeatureCollection."""
    features = []
    target_line = line.strip() if line else None
    cutoff = datetime.now() - timedelta(minutes=10)

    for v_num, telemetry in list(LAST_TRAM_POSITIONS.items()):
        if telemetry.gps_time < cutoff:
            continue
        if target_line and telemetry.line != target_line:
            continue

        features.append({
            "type": "Feature",
            "id": v_num,
            "properties": {
                "vehicle_number": telemetry.vehicle_number,
                "line": telemetry.line,
                "brigade": telemetry.brigade,
                "speed_kmh": telemetry.speed_kmh,
                "time": telemetry.gps_time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [telemetry.lon, telemetry.lat],
            },
        })

    return {
        "type": "FeatureCollection",
        "total_active_trams": len(features),
        "features": features,
    }


@router.get("/vehicles/{vehicle_number}/track")
async def get_vehicle_track(
    vehicle_number: str, limit: int = Query(default=300, ge=10, le=2000)
) -> Dict[str, Any]:
    """Zwraca zarejestrowany ślad GPS dla wybranego wozu prosto z PostgreSQL."""
    v_num = vehicle_number.strip()
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT line, brigade, lat, lon, speed_kmh, gps_time
            FROM tram_telemetry_history
            WHERE vehicle_number = $1
            ORDER BY gps_time DESC
            LIMIT $2;
            """,
            v_num, limit
        )

    if not rows:
        return {
            "type": "FeatureCollection",
            "vehicle_number": v_num,
            "samples_count": 0,
            "features": [],
        }

    valid_rows = list(reversed(rows))
    coordinates = [[r["lon"], r["lat"]] for r in valid_rows]
    latest = valid_rows[-1]

    track_line = {
        "type": "Feature",
        "properties": {
            "type": "track_line",
            "vehicle_number": v_num,
            "line": latest["line"],
            "brigade": latest["brigade"],
        },
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }

    sample_points = [
        {
            "type": "Feature",
            "properties": {
                "type": "sample_point",
                "speed_kmh": float(r["speed_kmh"]),
                "time": r["gps_time"].strftime("%Y-%m-%d %H:%M:%S"),
                "line": r["line"],
                "brigade": r["brigade"],
            },
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
        }
        for r in valid_rows
    ]

    return {
        "type": "FeatureCollection",
        "vehicle_number": v_num,
        "current_line": latest["line"],
        "current_brigade": latest["brigade"],
        "samples_count": len(valid_rows),
        "features": [track_line] + sample_points,
    }


# =@router.get("/dwells/lines")
async def get_available_lines() -> Dict[str, List[str]]:
    """Zwraca unikalną listę linii z bazy zdarzeń postojowych."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT line 
            FROM tram_dwell_events 
            WHERE line IS NOT NULL AND line != ''
            ORDER BY line ASC;
        """)
    lines = [r["line"] for r in rows]
    lines.sort(key=lambda x: int(x) if x.isdigit() else 999)
    return {"lines": lines}


@router.get("/dwells/vehicle/{vehicle_number}", response_model=List[TramDwellEvent])
async def get_vehicle_dwell_events(
    vehicle_number: str, limit: int = Query(default=100, ge=1, le=500)
):
    """Zwraca listę zarejestrowanych postojów konkretnego wozu."""
    v_num = vehicle_number.strip()
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT vehicle_number, line, brigade, stop_name, cluster_name,
                   arrival_time, departure_time, duration_sec, min_speed_kmh,
                   min_dist_m, pings_count
            FROM tram_dwell_events
            WHERE vehicle_number = $1
            ORDER BY arrival_time DESC
            LIMIT $2;
        """, v_num, limit)
    return [TramDwellEvent(**dict(r)) for r in rows]


@router.get("/dwells/all-stops")
async def get_all_stops_dwell_stats() -> Dict[str, Any]:
  """Zwraca postoje peronów z poprawnym, unikalnym rozbiciem na linie (brak duplikatów)."""
  pool = get_pool()
  async with pool.acquire() as conn:
    rows = await conn.fetch("""
            WITH per_line AS (
                -- 1. Unikalna agregacja per słupek i linia
                SELECT 
                    stop_name,
                    line,
                    COUNT(*) AS line_samples,
                    ROUND(AVG(duration_sec)::numeric, 1) AS line_avg_dwell
                FROM tram_dwell_events
                WHERE duration_sec BETWEEN 4 AND 300 AND stop_name IS NOT NULL
                GROUP BY stop_name, line
            ),
            lines_aggregated AS (
                -- 2. Złożenie unikalnej tablicy JSON dla każdego słupka
                SELECT 
                    stop_name,
                    json_agg(json_build_object(
                        'line', line,
                        'avg_dwell_sec', line_avg_dwell,
                        'samples', line_samples
                    ) ORDER BY line ASC) AS lines_json
                FROM per_line
                GROUP BY stop_name
            ),
            overall_dwells AS (
                -- 3. Łączny czas i próba dla słupka
                SELECT 
                    stop_name,
                    MAX(cluster_name) AS cluster_name,
                    ROUND(AVG(duration_sec)::numeric, 1) AS avg_dwell,
                    COUNT(*) AS total_samples
                FROM tram_dwell_events
                WHERE duration_sec BETWEEN 4 AND 300 AND stop_name IS NOT NULL
                GROUP BY stop_name
            )
            SELECT 
                p.osm_id,
                o.stop_name,
                COALESCE(o.cluster_name, p.cluster_name, o.stop_name) AS cluster_name,
                o.avg_dwell,
                o.total_samples,
                la.lines_json,
                ST_AsGeoJSON(p.geom)::json AS geometry
            FROM overall_dwells o
            JOIN lines_aggregated la ON o.stop_name = la.stop_name
            JOIN tram_platforms p ON p.name = o.stop_name
            WHERE p.geom IS NOT NULL
            ORDER BY o.total_samples DESC;
        """)

  features = []
  for r in rows:
    geom = r["geometry"]
    if isinstance(geom, str):
      try:
        geom = json.loads(geom)
      except Exception:
        geom = None

    if geom:
      lines_val = r["lines_json"]
      if not isinstance(lines_val, str):
        lines_val = json.dumps(lines_val)

      features.append({
          "type": "Feature",
          "id": r["osm_id"],
          "geometry": geom,
          "properties": {
              "stop_name": r["stop_name"],
              "cluster_name": r["cluster_name"],
              "avg_dwell_sec": float(r["avg_dwell"]),
              "samples_count": r["total_samples"],
              "lines_json": lines_val,
          },
      })

  return {"type": "FeatureCollection", "features": features}

@router.get("/dwells/line/{line}/stats")
async def get_line_dwell_stats(line: str) -> Dict[str, Any]:
    """Zwraca statystyki postojów dla wybranej linii z dokładnym położeniem słupków."""
    line_clean = line.strip()
    pool = get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            WITH line_dwells AS (
                SELECT 
                    stop_name,
                    line,
                    COUNT(*) AS samples,
                    ROUND(AVG(duration_sec)::numeric, 1) AS avg_dwell
                FROM tram_dwell_events
                WHERE line = $1 AND duration_sec BETWEEN 4 AND 300 AND stop_name IS NOT NULL
                GROUP BY stop_name, line
            )
            SELECT 
                p.osm_id,
                p.name AS stop_name,
                p.cluster_name,
                ld.avg_dwell,
                ld.samples,
                json_build_array(json_build_object(
                    'line', ld.line,
                    'avg_dwell_sec', ld.avg_dwell,
                    'samples', ld.samples
                )) AS lines_json,
                ST_AsGeoJSON(p.geom)::json AS geometry
            FROM line_dwells ld
            JOIN tram_platforms p ON p.name = ld.stop_name
            WHERE p.geom IS NOT NULL
            ORDER BY ld.samples DESC;
        """, line_clean)

    features = []
    for r in rows:
        geom = r["geometry"]
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                geom = None

        if geom:
            lines_val = r["lines_json"]
            if not isinstance(lines_val, str):
                lines_val = json.dumps(lines_val)

            features.append({
                "type": "Feature",
                "id": r["osm_id"],
                "geometry": geom,
                "properties": {
                    "stop_name": r["stop_name"],
                    "cluster_name": r["cluster_name"],
                    "avg_dwell_sec": float(r["avg_dwell"]),
                    "samples_count": r["samples"],
                    "lines_json": lines_val,
                },
            })

    return {
        "type": "FeatureCollection",
        "line": line_clean,
        "stops_count": len(features),
        "features": features,
    }


@router.get("/dwells/clusters")
async def get_available_clusters() -> Dict[str, List[str]]:
    """Zwraca unikalne zespoły przystankowe pod autouzupełnianie pól tekstowych."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT cluster_name 
            FROM tram_platforms 
            WHERE cluster_name IS NOT NULL AND cluster_name != ''
            ORDER BY cluster_name ASC;
        """)
    return {"clusters": [r["cluster_name"] for r in rows]}
# ==============================================================================
# 3. Korytarze Tramwajowe: Wyszukiwanie ścieżek grafowych (Dijkstra)
# ==============================================================================

async def _fetch_corridor_stats(
    from_stop: str, to_stop: str, line: Optional[str]
) -> List[Dict[str, Any]]:
    from_clean = from_stop.strip().lower()
    to_clean = to_stop.strip().lower()

    if from_clean == to_clean:
        return []

    pool = get_pool()
    async with pool.acquire() as conn:
        if line:
            segments = await conn.fetch(
                """
                SELECT line, LOWER(from_cluster) AS from_c, LOWER(to_cluster) AS to_c, 
                       avg_duration_sec, samples_count
                FROM tram_direct_segments
                WHERE line = $1 AND samples_count >= 2;
                """,
                line.strip(),
            )
        else:
            segments = await conn.fetch(
                """
                SELECT line, LOWER(from_cluster) AS from_c, LOWER(to_cluster) AS to_c, 
                       avg_duration_sec, samples_count
                FROM tram_direct_segments
                WHERE samples_count >= 2;
                """
            )

    if not segments:
        return []

    lines_graph: Dict[str, Dict[str, List[tuple]]] = defaultdict(lambda: defaultdict(list))
    for s in segments:
        lines_graph[s["line"]][s["from_c"]].append(
            (s["to_c"], float(s["avg_duration_sec"]), int(s["samples_count"]))
        )

    results = []

    for l, edges in lines_graph.items():
        if from_clean not in edges:
            continue

        pq = [(0.0, from_clean, float("inf"))]
        best_durations = {from_clean: 0.0}
        found_time = None
        found_samples = 0

        while pq:
            curr_time, curr_stop, min_s = heapq.heappop(pq)

            if curr_stop == to_clean and curr_time > 0.0:
                found_time = curr_time
                found_samples = int(min_s)
                break

            if curr_time > best_durations.get(curr_stop, float("inf")):
                continue

            for nxt, dur, cnt in edges.get(curr_stop, []):
                dwell_sec = 22.0 if nxt != to_clean else 0.0
                new_time = curr_time + dur + dwell_sec

                if new_time < best_durations.get(nxt, float("inf")):
                    best_durations[nxt] = new_time
                    heapq.heappush(pq, (new_time, nxt, min(min_s, cnt)))

        if found_time is not None:
            avg_m = round(found_time / 60.0, 1)
            results.append({
                "line": l,
                "samples": found_samples if found_samples != float("inf") else 1,
                "avg_time_min": avg_m,
                "min_time_min": round(avg_m * 0.85, 1),
                "max_time_min": round(avg_m * 1.25, 1),
            })

    results.sort(key=lambda x: int(x["line"]) if x["line"].isdigit() else x["line"])
    return results


@router.get("/travel-time")
async def get_tram_travel_time(
    from_stop: str = Query(..., description="Nazwa zespołu, np. 'Mangalia'"),
    to_stop: str = Query(..., description="Nazwa zespołu, np. 'Centrum'"),
    line: Optional[str] = Query(None, description="Opcjonalna linia, np. '16'"),
):
    from_clean = from_stop.strip()
    to_clean = to_stop.strip()
    line_clean = line.strip() if line and line.strip() else None

    if from_clean.lower() == to_clean.lower():
        return {
            "from_stop": from_clean,
            "to_stop": to_clean,
            "total_samples": 0,
            "overall_avg_min": None,
            "lines": [],
        }

    cache_key = (from_clean.lower(), to_clean.lower(), line_clean)
    now = time.time()

    if cache_key in _TRAVEL_TIME_CACHE:
        expire_at, cached_payload = _TRAVEL_TIME_CACHE[cache_key]
        if now < expire_at:
            return cached_payload

    rows = await _fetch_corridor_stats(from_clean, to_clean, line_clean)

    if not rows:
        payload = {
            "from_stop": from_clean,
            "to_stop": to_clean,
            "total_samples": 0,
            "overall_avg_min": None,
            "lines": [],
        }
        _TRAVEL_TIME_CACHE[cache_key] = (now + 30, payload)
        return payload

    total_samples = sum(r["samples"] for r in rows)
    weighted_sum = sum(r["avg_time_min"] * r["samples"] for r in rows)

    response_data = {
        "from_stop": from_clean,
        "to_stop": to_clean,
        "total_samples": total_samples,
        "overall_avg_min": round(weighted_sum / total_samples, 1) if total_samples else 0.0,
        "lines": rows,
    }

    _TRAVEL_TIME_CACHE[cache_key] = (now + _CACHE_TTL_SEC, response_data)
    return response_data


@router.get("/corridor/lines")
async def get_corridor_lines(
    from_stop: str = Query(..., description="Nazwa zespołu startowego"),
    to_stop: Optional[str] = Query(None, description="Opcjonalna nazwa zespołu docelowego"),
):
    """Zwraca dostępne linie bezpośrednio z tabeli tram_direct_segments."""
    from_clean = from_stop.strip()
    to_clean = to_stop.strip() if to_stop else None
    pool = get_pool()

    if to_clean:
        stats = await _fetch_corridor_stats(from_clean, to_clean, None)
        return {"lines": [r["line"] for r in stats]}

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT line 
            FROM tram_direct_segments 
            WHERE LOWER(from_cluster) = LOWER($1)
            ORDER BY line ASC;
            """,
            from_clean,
        )
    return {"lines": [r["line"] for r in rows]}