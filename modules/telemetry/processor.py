from datetime import datetime
import numpy as np
from scipy.spatial import cKDTree

from core.database import (
    get_db_cursor,
    transaction,
    TRAM_DB_PATH,
    TRAM_ANALYTICS_DB_PATH,
)
from core.geo import wgs84_to_epsg2180


class TelemetryAnalyticsEngine:

    def __init__(self):
        self.plat_names = []
        self.plat_clusters = []
        self.plat_tree = None
        self.initialized = False
        # Stan aktywnych wozów trzymany lekko w pamięci RAM:
        # vehicle_number -> {"stop_name", "cluster_name", "line", "brigade", "start_time", "min_speed", "pings"}
        self.active_dwells = {}

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

    def process_live_batch(self, telemetry_rows: list[dict]):
        """Błyskawiczna analiza bieżącej paczki danych (wywoływana wprost z workera).

        Zamiast rzeźbić w bazie, przetwarza listę w RAM w ułamku sekundy.
        """
        self.ensure_initialized()
        if not self.plat_tree or not telemetry_rows:
            return

        completed_events = []

        # 1. Transformacja współrzędnych i zapytanie do cKDTree (bufor 40 m)
        coords_2180 = [
            wgs84_to_epsg2180(float(r["Lon"]), float(r["Lat"]))
            for r in telemetry_rows
        ]
        dists, indices = self.plat_tree.query(
            coords_2180, distance_upper_bound=40.0
        )

        for i, r in enumerate(telemetry_rows):
            v_num = str(r.get("VehicleNumber"))
            line = str(r.get("Lines", "")).strip()
            brigade = str(r.get("Brigade", "")).strip()
            speed = float(r.get("Speed", 0.0) or 0.0)
            time_str = r.get("Time")  # np. '2026-09-16 15:40:00'

            try:
                curr_time = datetime.strptime(
                    time_str[:19], "%Y-%m-%d %H:%M:%S"
                )
            except Exception:
                curr_time = datetime.now()

            idx = indices[i]
            in_zone = idx < len(self.plat_names)
            stop_name = self.plat_names[idx] if in_zone else None
            cluster_name = self.plat_clusters[idx] if in_zone else None

            # Czy wóz był już śledzony w strefie przystanku?
            tracked = self.active_dwells.get(v_num)

            if in_zone:
                if tracked:
                    # Tramwaj nadal w tym samym zespole przystankowym
                    if tracked["cluster_name"] == cluster_name:
                        tracked["min_speed"] = min(tracked["min_speed"], speed)
                        tracked["last_time"] = curr_time
                        tracked["pings"] += 1
                        continue
                    else:
                        # Przeskoczył do innego przystanku – domykamy stary
                        self._finalize_event(
                            tracked, curr_time, completed_events
                        )

                # Nowy wjazd w strefę przystanku
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
                # Wóz poza strefą przystanku – jeśli wcześniej stał na przystanku, finalizujemy postój
                if tracked:
                    self._finalize_event(tracked, curr_time, completed_events)
                    del self.active_dwells[v_num]

        # 2. Zapis wykrytych postojów do tram_analytics.db
        if completed_events:
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

    def _finalize_event(self, tracked: dict, end_time: datetime, events_list: list):
        """Weryfikuje regułę Speed-Dip i kwalifikuje zdarzenie do zapisu."""
        duration = (end_time - tracked["start_time"]).total_seconds()

        # Filtr Speed-Dip: postój trwał >= 10s lub skład wyraźnie zwolnił (<3.5 km/h)
        if (
            (tracked["min_speed"] <= 3.5 or duration >= 12.0)
            and 8.0 <= duration <= 900.0
        ):
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

    def analyze_recent_telemetry(self, *args, **kwargs) -> int:
        """Pusta atrapa na potrzeby pętli workera – analiza odbywa się teraz w locie."""
        return 0

    def backfill_all_history(self) -> int:
        """Atrapa wyłączająca mielenie 11M rekordów."""
        self.ensure_initialized()
        return 0


analytics_engine = TelemetryAnalyticsEngine()