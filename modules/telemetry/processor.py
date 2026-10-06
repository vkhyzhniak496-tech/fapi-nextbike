from datetime import datetime
from typing import Any, Dict, List
import numpy as np
from scipy.spatial import cKDTree

from core.database import (
    TRAM_ANALYTICS_DB_PATH,
    TRAM_CORRIDORS_DB_PATH,
    TRAM_DB_PATH,
    get_db_cursor,
    transaction,
)
from core.geo import wgs84_to_epsg2180


class TelemetryAnalyticsEngine:

  def __init__(self):
    self.plat_names: List[str] = []
    self.plat_clusters: List[str] = []
    self.plat_tree: cKDTree = None
    self.initialized: bool = False
    # vehicle_number -> {"stop_name", "cluster_name", "line", "brigade", "start_time", "min_speed", "pings", "min_dist"}
    self.active_dwells: Dict[str, Dict[str, Any]] = {}
    # vehicle_number -> {"cluster_name": str, "departure_time": datetime, "line": str}
    self.last_departures: Dict[str, Dict[str, Any]] = {}

  def ensure_initialized(self):
    """Ładuje perony do cKDTree (tylko raz, zajmuje ~1 MB RAM)."""
    if self.initialized:
      return

    with get_db_cursor(TRAM_DB_PATH) as cur:
      cur.execute("""
                SELECT name, cluster_name, x_2180, y_2180 
                FROM tram_platforms 
                WHERE x_2180 IS NOT NULL AND y_2180 IS NOT NULL;
            """)
      rows = cur.fetchall()

    if rows:
      self.plat_names = [r["name"] for r in rows]
      self.plat_clusters = [r["cluster_name"] for r in rows]
      coords = np.array(
          [[r["x_2180"], r["y_2180"]] for r in rows], dtype=np.float64
      )
      self.plat_tree = cKDTree(coords)
      self.initialized = True
      print(
          f"[PROCESSOR] Załadowano {len(rows)} peronów do indeksu"
          " przestrzennego."
      )

  def process_live_batch(self, telemetry_rows: List[Dict[str, Any]]):
    self.ensure_initialized()
    if not self.plat_tree or not telemetry_rows:
      return

    completed_events = []
    completed_segments = []

    try:
      coords_2180 = [
          wgs84_to_epsg2180(float(r["Lon"]), float(r["Lat"]))
          for r in telemetry_rows
      ]
      dists, indices = self.plat_tree.query(
          coords_2180, distance_upper_bound=50.0
      )
    except Exception as e:
      print(f"[PROCESSOR ERROR] Konwersja współrzędnych / cKDTree: {e}")
      return

    for i, r in enumerate(telemetry_rows):
      v_num = str(r.get("VehicleNumber", "")).strip()
      line = str(r.get("Lines", "")).strip()
      brigade = str(r.get("Brigade", "")).strip()
      speed = float(r.get("Speed", 0.0) or 0.0)
      time_str = r.get("Time")

      if not v_num or not line:
        continue

      try:
        curr_time = datetime.strptime(
            str(time_str)[:19], "%Y-%m-%d %H:%M:%S"
        )
      except Exception:
        curr_time = datetime.now()

      idx = indices[i]
      in_zone = idx < len(self.plat_names)
      stop_name = self.plat_names[idx] if in_zone else None
      cluster_name = self.plat_clusters[idx] if in_zone else None

      tracked = self.active_dwells.get(v_num)

      if in_zone:
        if tracked:
          if tracked["cluster_name"] == cluster_name:
            tracked["min_speed"] = min(tracked["min_speed"], speed)
            tracked["last_time"] = curr_time
            tracked["pings"] += 1
            continue
          else:
            # Płynny przeskok bezpośrednio ze słupka na inny słupek
            self._finalize_event(tracked, curr_time, completed_events)
        else:
          # Nowy wjazd na przystanek: sprawdzamy przelot z poprzedniego zespołu
          last_dep = self.last_departures.get(v_num)
          if (
              last_dep
              and last_dep["line"] == line
              and last_dep["cluster_name"] != cluster_name
          ):
            flight_sec = (
                curr_time - last_dep["departure_time"]
            ).total_seconds()
            if 10.0 <= flight_sec <= 600.0:
              completed_segments.append(
                  (line, last_dep["cluster_name"], cluster_name, flight_sec)
              )

        # Inicjalizacja śledzenia postoju na peronie
        self.active_dwells[v_num] = {
            "vehicle_number": v_num,
            "line": line,
            "brigade": brigade,
            "stop_name": stop_name,
            "cluster_name": cluster_name,
            "start_time": curr_time,
            "last_time": curr_time,
            "min_speed": speed,
            "min_dist": float(round(dists[i], 1)),
            "pings": 1,
        }
      else:
        # Wyjazd poza strefę przystanku
        if tracked:
          self._finalize_event(tracked, curr_time, completed_events)
          del self.active_dwells[v_num]

    # 1. Zapis postojów do tram_analytics.db
    if completed_events:
      try:
        with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
          with transaction(cur):
            cur.executemany(
                """
                            INSERT INTO tram_dwell_events (
                                vehicle_number, line, brigade, stop_name, cluster_name,
                                arrival_time, departure_time, duration_sec, min_speed_kmh,
                                min_dist_m, pings_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(vehicle_number, stop_name, arrival_time) DO NOTHING;
                        """,
                completed_events,
            )
      except Exception as e:
        print(f"[PROCESSOR ERROR] Błąd zapisu postojów: {e}")

    # 2. Zapis segmentów przelotu do tram_corridors.db (Średnia krocząca na żywo)
    if completed_segments:
      try:
        with get_db_cursor(TRAM_CORRIDORS_DB_PATH) as cur:
          with transaction(cur):
            for line_val, from_c, to_c, dur in completed_segments:
              cur.execute(
                  """
                                INSERT INTO tram_direct_segments (
                                    line, from_cluster, to_cluster, avg_duration_sec, samples_count, last_seen
                                ) VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                                ON CONFLICT(line, from_cluster, to_cluster) DO UPDATE SET
                                    avg_duration_sec = ROUND((avg_duration_sec * samples_count + excluded.avg_duration_sec) / (samples_count + 1), 1),
                                    samples_count = samples_count + 1,
                                    last_seen = CURRENT_TIMESTAMP;
                            """,
                  (line_val, from_c, to_c, round(dur, 1)),
              )
        print(
            f"[PROCESSOR] Zarejestrowano {len(completed_segments)} przelotów"
            f" segmentowych! (np. {completed_segments[0][0]}:"
            f" {completed_segments[0][1]} -> {completed_segments[0][2]})"
        )
      except Exception as e:
        print(f"[PROCESSOR ERROR] Błąd zapisu segmentów korytarza: {e}")

  def _finalize_event(
      self,
      tracked: dict,
      end_time: datetime,
      events_list: list,
  ):
    duration = (end_time - tracked["start_time"]).total_seconds()

    # Rejestrujemy postój i odjazd TYLKO, gdy tramwaj rzeczywiście obsłużył przystanek
    if (
        tracked["min_speed"] <= 3.5 or duration >= 12.0
    ) and 8.0 <= duration <= 900.0:
      events_list.append((
          tracked["vehicle_number"],
          tracked["line"],
          tracked["brigade"],
          tracked["stop_name"],
          tracked["cluster_name"],
          tracked["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
          end_time.strftime("%Y-%m-%d %H:%M:%S"),
          float(round(duration, 1)),
          float(round(tracked["min_speed"], 1)),
          tracked["min_dist"],
          tracked["pings"],
      ))

      # Punkt początkowy do segmentu zostaje ustawiony TYLKO przy zaliczonym postoju
      self.last_departures[tracked["vehicle_number"]] = {
          "cluster_name": tracked["cluster_name"],
          "departure_time": end_time,
          "line": tracked["line"],
      }

  def analyze_recent_telemetry(self) -> int:
    return 0

  def backfill_all_history(self) -> int:
    self.ensure_initialized()
    return 0


analytics_engine = TelemetryAnalyticsEngine()