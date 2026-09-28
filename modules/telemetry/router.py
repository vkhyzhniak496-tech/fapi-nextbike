import asyncio
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from core.database import (
    TRAM_ANALYTICS_DB_PATH,
    TRAM_DB_PATH,
    TRAM_LIVE_DB_PATH,
    get_db_cursor,
)
from core.models import TramDwellEvent
from modules.telemetry.worker import LAST_TRAM_POSITIONS

router = APIRouter(prefix="/network/tram", tags=["Tram Telemetry & Analytics"])

# Pamięć podręczna w RAM (0 ms narzutu)
# Klucz: (from_stop_lower, to_stop_lower, line) -> (expire_timestamp, payload)
_TRAVEL_TIME_CACHE: Dict[
    Tuple[str, str, Optional[str]], Tuple[float, Dict[str, Any]]
] = {}
_CACHE_TTL_SEC = 600  # 10 minut


# ==============================================================================
# 1. Pozycje tramwajów na żywo i trajektorie wozów
# ==============================================================================


@router.get("/live")
async def get_live_tram_positions(line: Optional[str] = None) -> Dict[str, Any]:
  """Zwraca bieżące pozycje składów z pamięci RAM w formacie GeoJSON FeatureCollection."""
  features = []
  target_line = line.strip() if line else None

  for v_num, telemetry in LAST_TRAM_POSITIONS.items():
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
  """Zwraca zarejestrowany ślad GPS dla wybranego wozu."""
  v_num = vehicle_number.strip()

  def _read_track():
    with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
      cur.execute(
          """
                SELECT line, brigade, lat, lon, speed_kmh, gps_time
                FROM tram_telemetry_history
                WHERE vehicle_number = ?
                ORDER BY gps_time ASC
                LIMIT ?;
                """,
          (v_num, limit),
      )
      return cur.fetchall()

  rows = await asyncio.to_thread(_read_track)
  if not rows:
    return {
        "type": "FeatureCollection",
        "vehicle_number": v_num,
        "samples_count": 0,
        "features": [],
    }

  coordinates = [[r["lon"], r["lat"]] for r in rows]
  track_line = {
      "type": "Feature",
      "properties": {
          "type": "track_line",
          "vehicle_number": v_num,
          "line": rows[-1]["line"],
          "brigade": rows[-1]["brigade"],
      },
      "geometry": {"type": "LineString", "coordinates": coordinates},
  }

  sample_points = [
      {
          "type": "Feature",
          "properties": {
              "type": "sample_point",
              "speed_kmh": r["speed_kmh"],
              "time": r["gps_time"],
              "line": r["line"],
              "brigade": r["brigade"],
          },
          "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
      }
      for r in rows
  ]

  return {
      "type": "FeatureCollection",
      "vehicle_number": v_num,
      "current_line": rows[-1]["line"],
      "current_brigade": rows[-1]["brigade"],
      "samples_count": len(rows),
      "features": [track_line] + sample_points,
  }


# ==============================================================================
# 2. Statystyki postojów na peronach (Widok analityczny peronów)
# ==============================================================================


@router.get("/dwells/lines")
def get_available_lines():
  """Zwraca unikalną listę linii obecnych w zdarzeniach postojowych."""
  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute("""
            SELECT DISTINCT line 
            FROM tram_dwell_events 
            WHERE line IS NOT NULL AND line != ''
            ORDER BY CAST(line AS INTEGER), line ASC;
        """)
    return {"lines": [r["line"] for r in cur.fetchall()]}


@router.get(
    "/dwells/vehicle/{vehicle_number}", response_model=List[TramDwellEvent]
)
def get_vehicle_dwell_events(
    vehicle_number: str, limit: int = Query(default=100, ge=1, le=500)
):
  """Zwraca postoje konkretnego wozu."""
  v_num = vehicle_number.strip()
  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute(
        """
            SELECT vehicle_number, line, brigade, stop_name, cluster_name,
                   arrival_time, departure_time, duration_sec, min_speed_kmh,
                   min_dist_m, pings_count
            FROM tram_dwell_events
            WHERE vehicle_number = ?
            ORDER BY arrival_time DESC
            LIMIT ?;
            """,
        (v_num, limit),
    )
    return [TramDwellEvent(**dict(r)) for r in cur.fetchall()]


@router.get("/dwells/line/{line}/stats")
def get_line_dwell_stats(line: str) -> Dict[str, Any]:
  """Zwraca statystyki postojów dla pojedynczej linii (natychmiastowy odczyt z koordynatami)."""
  line_clean = line.strip()

  coords_by_stop = {}
  coords_by_cluster = {}

  with get_db_cursor(TRAM_DB_PATH) as cur:
    cur.execute(
        "SELECT name, cluster_name, lat, lon, coordinates_json FROM"
        " tram_platforms;"
    )
    for r in cur.fetchall():
      lat, lon = r["lat"], r["lon"]
      if (lat is None or lon is None) and r["coordinates_json"]:
        try:
          c = json.loads(r["coordinates_json"])
          lon, lat = float(c[0]), float(c[1])
        except Exception:
          continue
      if lat is not None and lon is not None:
        if r["name"]:
          coords_by_stop[r["name"]] = (lon, lat)
        if r["cluster_name"] and r["cluster_name"] not in coords_by_cluster:
          coords_by_cluster[r["cluster_name"]] = (lon, lat)

  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute(
        """
            SELECT 
                d.stop_name,
                COALESCE(d.cluster_name, '') AS cluster_name,
                d.line,
                ROUND(AVG(d.duration_sec), 1) AS avg_dwell,
                COUNT(*) AS samples
            FROM tram_dwell_events d
            WHERE d.line = ?
            GROUP BY d.stop_name, d.line
            ORDER BY samples DESC;
        """,
        (line_clean,),
    )
    rows = cur.fetchall()

  features = []
  for r in rows:
    name = r["stop_name"]
    cluster = r["cluster_name"]
    coords = coords_by_stop.get(name) or coords_by_cluster.get(cluster)
    if not coords:
      continue

    features.append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [coords[0], coords[1]]},
        "properties": {
            "stop_name": name,
            "cluster_name": cluster or name,
            "avg_dwell_sec": r["avg_dwell"],
            "samples_count": r["samples"],
            "lines_json": json.dumps([{
                "line": r["line"],
                "avg_dwell_sec": r["avg_dwell"],
                "samples": r["samples"],
            }]),
        },
    })

  return {
      "type": "FeatureCollection",
      "line": line_clean,
      "stops_count": len(features),
      "features": features,
  }


@router.get("/dwells/all-stops")
def get_all_stops_dwell_stats() -> Dict[str, Any]:
  """Pobiera zagregowane statystyki postojów dla wszystkich peronów bez blokowania Event Loopa."""
  coords_by_stop = {}
  cluster_by_stop = {}

  with get_db_cursor(TRAM_DB_PATH) as cur:
    cur.execute(
        "SELECT name, cluster_name, lat, lon, coordinates_json FROM"
        " tram_platforms;"
    )
    for r in cur.fetchall():
      lat, lon = r["lat"], r["lon"]
      if (lat is None or lon is None) and r["coordinates_json"]:
        try:
          c = json.loads(r["coordinates_json"])
          lon, lat = float(c[0]), float(c[1])
        except Exception:
          continue
      if lat is not None and lon is not None and r["name"]:
        coords_by_stop[r["name"]] = (lon, lat)
        if r["cluster_name"]:
          cluster_by_stop[r["name"]] = r["cluster_name"]

  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute("""
            SELECT 
                stop_name,
                COALESCE(cluster_name, '') AS cluster_name,
                line,
                ROUND(AVG(duration_sec), 1) AS avg_dwell,
                COUNT(*) AS samples
            FROM tram_dwell_events
            GROUP BY stop_name, line
            ORDER BY stop_name, samples DESC;
        """)
    rows = cur.fetchall()

  stops = {}
  for r in rows:
    name = r["stop_name"]
    cluster = cluster_by_stop.get(name) or r["cluster_name"] or name
    if name not in stops:
      stops[name] = {
          "stop_name": name,
          "cluster_name": cluster,
          "total_samples": 0,
          "weighted_sum": 0.0,
          "lines": [],
      }
    stops[name]["lines"].append({
        "line": r["line"],
        "avg_dwell_sec": r["avg_dwell"],
        "samples": r["samples"],
    })
    stops[name]["total_samples"] += r["samples"]
    stops[name]["weighted_sum"] += r["avg_dwell"] * r["samples"]

  features = []
  for name, s in stops.items():
    coords = coords_by_stop.get(name)
    if not coords:
      continue
    overall_avg = (
        round(s["weighted_sum"] / s["total_samples"], 1)
        if s["total_samples"]
        else 0.0
    )
    features.append({
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [coords[0], coords[1]]},
        "properties": {
            "stop_name": s["stop_name"],
            "cluster_name": s["cluster_name"],
            "avg_dwell_sec": overall_avg,
            "samples_count": s["total_samples"],
            "lines_json": json.dumps(s["lines"]),
        },
    })

  return {"type": "FeatureCollection", "features": features}


@router.get("/dwells/clusters")
def get_available_clusters():
  """Zwraca unikalne zespoły przystankowe pod autouzupełnianie w polach tekstowych."""
  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute("""
            SELECT DISTINCT cluster_name 
            FROM tram_dwell_events 
            WHERE cluster_name IS NOT NULL AND cluster_name != ''
            ORDER BY cluster_name COLLATE NOCASE ASC;
        """)
    return {"clusters": [r["cluster_name"] for r in cur.fetchall()]}


# ==============================================================================
# 3. Korytarze Tramwajowe: Odczyt z gotowej tabeli agregatów (< 2 ms)
# ==============================================================================


def _read_corridor_stats(
    from_stop: str, to_stop: str, line: Optional[str]
) -> List[Dict[str, Any]]:
  """Wyznacza czas przejazdu (zarówno dla odcinków bezpośrednich, jak i wieloskokowych)."""

  # 1. Sprawdź relację bezpośrednią
  query_direct = """
        SELECT line, samples, avg_time_min, min_time_min, max_time_min
        FROM tram_corridor_stats
        WHERE from_stop = ? COLLATE NOCASE 
          AND to_stop = ? COLLATE NOCASE
          AND (? IS NULL OR line = ?)
        ORDER BY CAST(line AS INTEGER), line ASC;
    """
  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute(query_direct, (from_stop, to_stop, line, line))
    direct_rows = [dict(r) for r in cur.fetchall()]
    if direct_rows:
      return direct_rows

  # 2. Jeśli brak bezpośredniego wpisu: zsumuj kolejne przeloty wzdłuż trasy linii (Graf w SQL)
  query_route = """
        WITH RECURSIVE journey(curr_stop, line, total_time, hops, min_samples) AS (
            SELECT to_stop, line, avg_time_min, 1, samples
            FROM tram_corridor_stats
            WHERE from_stop = ? COLLATE NOCASE
              AND (? IS NULL OR line = ?)
            
            UNION ALL
            
            SELECT s.to_stop, s.line, j.total_time + s.avg_time_min, j.hops + 1, MIN(j.min_samples, s.samples)
            FROM tram_corridor_stats s
            JOIN journey j ON s.from_stop = j.curr_stop AND s.line = j.line
            WHERE j.hops < 30 AND j.curr_stop != ? COLLATE NOCASE
        )
        SELECT 
            line,
            min_samples AS samples,
            ROUND(total_time, 1) AS avg_time_min,
            ROUND(total_time * 0.85, 1) AS min_time_min,
            ROUND(total_time * 1.25, 1) AS max_time_min
        FROM journey
        WHERE curr_stop = ? COLLATE NOCASE
        GROUP BY line;
    """
  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute(query_route, (from_stop, line, line, to_stop, to_stop))
    return [dict(r) for r in cur.fetchall()]


@router.get("/travel-time")
async def get_tram_travel_time(
    from_stop: str = Query(..., description="Nazwa zespołu, np. 'Mangalia'"),
    to_stop: str = Query(..., description="Nazwa zespołu, np. 'Centrum'"),
    line: Optional[str] = Query(None, description="Opcjonalna linia, np. '16'"),
):
  """Zwraca średni czas przelotu i rozbicie na linie w czasie < 2 ms."""
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

  # 1. Odczyt z pamięci podręcznej RAM (0 ms)
  if cache_key in _TRAVEL_TIME_CACHE:
    expire_at, cached_payload = _TRAVEL_TIME_CACHE[cache_key]
    if now < expire_at:
      return cached_payload

  # 2. Główny odczyt z tabeli agregatów (< 2 ms)
  rows = await asyncio.to_thread(
      _read_corridor_stats, from_clean, to_clean, line_clean
  )

  if not rows:
    payload = {
        "from_stop": from_clean,
        "to_stop": to_clean,
        "total_samples": 0,
        "overall_avg_min": None,
        "lines": [],
    }
    _TRAVEL_TIME_CACHE[cache_key] = (now + 60, payload)
    return payload

  total_samples = sum(r["samples"] for r in rows)
  weighted_sum = sum(r["avg_time_min"] * r["samples"] for r in rows)

  response_data = {
      "from_stop": from_clean,
      "to_stop": to_clean,
      "total_samples": total_samples,
      "overall_avg_min": (
          round(weighted_sum / total_samples, 1) if total_samples else 0.0
      ),
      "lines": rows,
  }

  _TRAVEL_TIME_CACHE[cache_key] = (now + _CACHE_TTL_SEC, response_data)
  return response_data


def _execute_corridor_lines_query(
    from_stop: str, to_stop: Optional[str]
) -> List[str]:
  """Pobiera dostępne linie prosto z tabeli agregatów zamiast łączyć historię."""
  if to_stop and to_stop.strip():
    query = """
            SELECT DISTINCT line 
            FROM tram_corridor_stats 
            WHERE from_stop = ? COLLATE NOCASE 
              AND to_stop = ? COLLATE NOCASE
            ORDER BY CAST(line AS INTEGER), line ASC;
        """
    params = (from_stop.strip(), to_stop.strip())
  else:
    query = """
            SELECT DISTINCT line 
            FROM tram_corridor_stats 
            WHERE from_stop = ? COLLATE NOCASE
            ORDER BY CAST(line AS INTEGER), line ASC;
        """
    params = (from_stop.strip(),)

  with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
    cur.execute(query, params)
    lines = [r["line"] for r in cur.fetchall()]

  # Fallback na wypadek podania tylko punktu startowego bez zdefiniowanej mety
  if not lines and not to_stop:
    with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
      cur.execute(
          "SELECT DISTINCT line FROM tram_dwell_events WHERE cluster_name = ?"
          " COLLATE NOCASE ORDER BY CAST(line AS INTEGER);",
          (from_stop.strip(),),
      )
      lines = [r["line"] for r in cur.fetchall()]

  return lines


@router.get("/corridor/lines")
async def get_corridor_lines(
    from_stop: str = Query(..., description="Nazwa zespołu startowego"),
    to_stop: Optional[str] = Query(
        None, description="Opcjonalna nazwa zespołu docelowego"
    ),
):
  """Błyskawiczne serwowanie linii dla korytarza bez obciążania procesora."""
  lines = await asyncio.to_thread(
      _execute_corridor_lines_query, from_stop, to_stop
  )
  return {"lines": lines}