from datetime import datetime
from typing import Any, Dict, List
import numpy as np
from scipy.spatial import cKDTree
import re
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
    # vehicle_number -> dict
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
      # Zabezpieczenie: jeśli cluster_name jest puste, bierzemy nazwę przystanku
      clusters = []
      for r in rows:
        raw = (r["cluster_name"] or r["name"] or "").strip()
        # Usuwa końcówkę ze spacją i 2 cyframi (np. 'Mangalia 03' -> 'Mangalia')
        cleaned = re.sub(r"\s+\d{2}$", "", raw)
        clusters.append(cleaned)
      self.plat_clusters = clusters
      coords = np.array(
          [[r["x_2180"], r["y_2180"]] for r in rows], dtype=np.float64
      )
      self.plat_tree = cKDTree(coords)
      self.initialized = True
      print(
          f"[PROCESSOR] Załadowano {len(rows)} peronów do indeksu"
          " przestrzennego.",
          flush=True,
      )

  def process_live_batch(self, telemetry_rows: List[Any]):
    self.ensure_initialized()
    if not self.plat_tree or not telemetry_rows:
      return

    completed_events = []
    completed_segments = []

    # 1. Elastyczne wyciąganie współrzędnych
    coords_2180 = []
    for r in telemetry_rows:
      try:
        if isinstance(r, dict):
          lon = float(r.get("Lon") if "Lon" in r else r.get("lon", 0.0))
          lat = float(r.get("Lat") if "Lat" in r else r.get("lat", 0.0))
        else:
          lon = float(getattr(r, "lon", getattr(r, "Lon", 0.0)))
          lat = float(getattr(r, "lat", getattr(r, "Lat", 0.0)))
        coords_2180.append(wgs84_to_epsg2180(lon, lat))
      except Exception:
        coords_2180.append((0.0, 0.0))

    try:
      dists, indices = self.plat_tree.query(
          coords_2180, distance_upper_bound=50.0
      )
    except Exception as e:
      print(
          f"[PROCESSOR ERROR] Konwersja współrzędnych / cKDTree: {e}", flush=True
      )
      return

    # 2. Główna pętla analizy
    for i, r in enumerate(telemetry_rows):
      if isinstance(r, dict):
        v_num = str(
            r.get("VehicleNumber") or r.get("vehicle_number") or ""
        ).strip()
        line = str(r.get("Lines") or r.get("line") or "").strip()
        brigade = str(r.get("Brigade") or r.get("brigade") or "").strip()
        speed = float(
            r.get("Speed") or r.get("speed_kmh") or r.get("speed") or 0.0
        )
        time_val = (
            r.get("Time")
            or r.get("time")
            or r.get("gps_time")
            or r.get("timestamp")
        )
      else:
        v_num = str(
            getattr(r, "vehicle_number", getattr(r, "VehicleNumber", ""))
        ).strip()
        line = str(getattr(r, "line", getattr(r, "Lines", ""))).strip()
        brigade = str(getattr(r, "brigade", getattr(r, "Brigade", ""))).strip()
        speed = float(
            getattr(
                r, "speed_kmh", getattr(r, "speed", getattr(r, "Speed", 0.0))
            )
        )
        time_val = getattr(
            r, "gps_time", getattr(r, "time", getattr(r, "Time", None))
        )

      if not v_num or not line:
        continue

      if isinstance(time_val, datetime):
        curr_time = time_val
      elif isinstance(time_val, str):
        try:
          curr_time = datetime.strptime(
              str(time_val)[:19], "%Y-%m-%d %H:%M:%S"
          )
        except Exception:
          curr_time = datetime.now()
      else:
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
            # Płynny przeskok bezpośrednio na kolejny zespół peronowy
            self._finalize_event(tracked, curr_time, completed_events)
            last_dep = self.last_departures.get(v_num)
            if (
                last_dep
                and last_dep["line"] == line
                and last_dep["cluster_name"] != cluster_name
            ):
              flight_sec = (
                  curr_time - last_dep["departure_time"]
              ).total_seconds()
              if 8.0 <= flight_sec <= 900.0:
                completed_segments.append(
                    (line, last_dep["cluster_name"], cluster_name, flight_sec)
                )
        else:
          # Nowy wjazd na przystanek: rejestrujemy przelot segmentowy z poprzedniego peronu
          last_dep = self.last_departures.get(v_num)
          if (
              last_dep
              and last_dep["line"] == line
              and last_dep["cluster_name"] != cluster_name
          ):
            flight_sec = (
                curr_time - last_dep["departure_time"]
            ).total_seconds()
            if 8.0 <= flight_sec <= 900.0:
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
        print(f"[PROCESSOR ERROR] Błąd zapisu postojów: {e}", flush=True)

    # 2. Zapis segmentów przelotu do tram_corridors.db (Ważona średnia krocząca na żywo)
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
            f" {completed_segments[0][1]} -> {completed_segments[0][2]})",
            flush=True,
        )
      except Exception as e:
        print(
            f"[PROCESSOR ERROR] Błąd zapisu segmentów korytarza: {e}", flush=True
        )

  def _finalize_event(
      self,
      tracked: dict,
      end_time: datetime,
      events_list: list,
  ):
    # Czas postoju liczymy do ostatniego pingu wewnątrz strefy peronu
    last_ping_in_zone = tracked.get("last_time", end_time)
    duration = (last_ping_in_zone - tracked["start_time"]).total_seconds()

    # Kwalifikacja postoju do tram_analytics
    if (
        tracked["min_speed"] <= 6.0 or duration >= 8.0
    ) and 4.0 <= duration <= 900.0:
      events_list.append((
          tracked["vehicle_number"],
          tracked["line"],
          tracked["brigade"],
          tracked["stop_name"],
          tracked["cluster_name"],
          tracked["start_time"].strftime("%Y-%m-%d %H:%M:%S"),
          last_ping_in_zone.strftime("%Y-%m-%d %H:%M:%S"),
          float(round(duration, 1)),
          float(round(tracked["min_speed"], 1)),
          tracked["min_dist"],
          tracked["pings"],
      ))

    # Do segmentu rejestrujemy odjazd z momentu ostatniego kontaktu z peronem
    if tracked["cluster_name"]:
      self.last_departures[tracked["vehicle_number"]] = {
          "cluster_name": tracked["cluster_name"],
          "departure_time": last_ping_in_zone,
          "line": tracked["line"],
      }

  def analyze_recent_telemetry(self) -> int:
    return 0

  def backfill_all_history(self) -> int:
    self.ensure_initialized()
    return 0


analytics_engine = TelemetryAnalyticsEngine()