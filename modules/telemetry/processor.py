import sqlite3
from datetime import datetime, timedelta
from typing import Optional
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from core.database import (
    TRAM_ANALYTICS_DB_PATH,
    TRAM_DB_PATH,
    TRAM_LIVE_DB_PATH,
    get_db_cursor,
    transaction,
)
from core.geo import wgs84_to_epsg2180
from core.models import TramDwellEvent


class TelemetryAnalyticsEngine:
    def __init__(self):
        self.plat_tree: Optional[cKDTree] = None
        self.plat_names: Optional[np.ndarray] = None
        self.plat_clusters: Optional[np.ndarray] = None
        self._load_platforms()

    def _load_platforms(self) -> None:
        """Ładuje infrastrukturę przystankową do pamięci RAM raz przy starcie."""
        with get_db_cursor(TRAM_DB_PATH) as cur:
            cur.execute("""
                SELECT name, cluster_name, x_2180, y_2180 
                FROM tram_platforms 
                WHERE x_2180 IS NOT NULL AND y_2180 IS NOT NULL;
            """)
            plat_rows = cur.fetchall()

        if not plat_rows:
            return

        self.plat_names = np.array([r["name"] for r in plat_rows])
        self.plat_clusters = np.array([r["cluster_name"] for r in plat_rows])
        coords = np.column_stack([[r["x_2180"] for r in plat_rows], [r["y_2180"] for r in plat_rows]])
        self.plat_tree = cKDTree(coords)



    def analyze_recent_telemetry(self, lookback_minutes: int = 45) -> int:
        """Przelicza ostatnie trajektorie i uzupełnia tabelę tram_dwell_events."""
        if self.plat_tree is None:
            self._load_platforms()
            if self.plat_tree is None:
                return 0

        since_time = (datetime.now() - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d %H:%M:%S")

        # Pobieramy pingi ze wszystkich wozów z zadanego okna czasowego
        with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
            cur.execute("""
                SELECT vehicle_number, line, brigade, lat, lon, speed_kmh, gps_time
                FROM tram_telemetry_history
                WHERE gps_time >= ?
                ORDER BY vehicle_number, gps_time ASC;
            """, (since_time,))
            rows = cur.fetchall()

        if not rows:
            return 0

        df_all = pd.DataFrame([dict(r) for r in rows])
        df_all["gps_time"] = pd.to_datetime(df_all["gps_time"])
        df_all = df_all.drop_duplicates(subset=["vehicle_number", "gps_time"]).sort_values(["vehicle_number", "gps_time"]).reset_index(drop=True)

        events_to_db = []

        # Przeliczanie per wóz
        for v_num, df in df_all.groupby("vehicle_number"):
            if len(df) < 5:
                continue

            tr_xy = [wgs84_to_epsg2180(lon, lat) for lon, lat in zip(df["lon"], df["lat"])]
            tr_x, tr_y = zip(*tr_xy)

            dists, indices = self.plat_tree.query(np.column_stack([tr_x, tr_y]), distance_upper_bound=80.0)

            df = df.copy()
            df["dist_to_stop_m"] = dists
            df["candidate_stop"] = [self.plat_names[idx] if idx < len(self.plat_names) else None for idx in indices]
            df["candidate_cluster"] = [self.plat_clusters[idx] if idx < len(self.plat_clusters) else None for idx in indices]
            df["time_gap_prev"] = df["gps_time"].diff().dt.total_seconds().fillna(0)

            df["in_stop_zone"] = df["candidate_cluster"].notna()
            zone_boundary = (
                (~df["in_stop_zone"])
                | (df["candidate_cluster"] != df["candidate_cluster"].shift(1))
                | (df["time_gap_prev"] > 120.0)
            )
            df["zone_id"] = zone_boundary.cumsum()

            def process_zone(g: pd.DataFrame) -> pd.Series:
                c_idx = g["dist_to_stop_m"].idxmin()
                return pd.Series({
                    "cluster_name": g["candidate_cluster"].iloc[0],
                    "stop_name": g.loc[c_idx, "candidate_stop"],
                    "line": g["line"].iloc[0],
                    "brigade": g["brigade"].iloc[0],
                    "vehicle_number": g["vehicle_number"].iloc[0],
                    "start_time": g["gps_time"].min(),
                    "end_time": g["gps_time"].max(),
                    "min_speed": g["speed_kmh"].min(),
                    "avg_speed": g["speed_kmh"].mean(),
                    "min_dist": g["dist_to_stop_m"].min(),
                    "pings": len(g)
                })

            zones = df[df["in_stop_zone"]].groupby("zone_id", as_index=False).apply(process_zone, include_groups=False)
            if zones.empty:
                continue

            zones["zone_duration_sec"] = (zones["end_time"] - zones["start_time"]).dt.total_seconds()

            # Kryteria Speed-Dip
            is_serviced = (
                (zones["min_speed"] < 3.0)
                | ((zones["min_speed"] < 14.0) & (zones["zone_duration_sec"] >= 12.0))
                | ((zones["min_speed"] < 5.0) & (zones["min_dist"] < 35.0))
            )
            serviced = zones[is_serviced & (zones["zone_duration_sec"] < 900.0)].copy()
            if serviced.empty:
                continue

            # Scalanie sąsiednich klastrów (< 45s)
            serviced["gap"] = (serviced["start_time"] - serviced["end_time"].shift(1)).dt.total_seconds()
            serviced["same_cluster"] = serviced["cluster_name"] == serviced["cluster_name"].shift(1)
            merge_id = ((~serviced["same_cluster"]) | (serviced["gap"] > 45.0)).cumsum()

            final_stops = serviced.groupby(merge_id).agg(
                vehicle_number=("vehicle_number", "first"),
                line=("line", "first"),
                brigade=("brigade", "first"),
                stop_name=("stop_name", "first"),
                cluster_name=("cluster_name", "first"),
                arrival_time=("start_time", "min"),
                departure_time=("end_time", "max"),
                min_speed_kmh=("min_speed", "min"),
                min_dist_m=("min_dist", "min"),
                duration_sec=("zone_duration_sec", "sum"),
                pings_count=("pings", "sum")
            )

            for _, r in final_stops.iterrows():
                events_to_db.append((
                    str(r["vehicle_number"]),
                    str(r["line"]),
                    str(r["brigade"]),
                    str(r["stop_name"]),
                    str(r["cluster_name"]),
                    r["arrival_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    r["departure_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    float(round(r["duration_sec"], 1)),
                    float(round(r["min_speed_kmh"], 1)),
                    float(round(r["min_dist_m"], 1)),
                    int(r["pings_count"])
                ))

        if not events_to_db:
            return 0

        with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
            with transaction(cur):
                cur.executemany("""
                    INSERT INTO tram_dwell_events (
                        vehicle_number, line, brigade, stop_name, cluster_name,
                        arrival_time, departure_time, duration_sec, min_speed_kmh,
                        min_dist_m, pings_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vehicle_number, stop_name, arrival_time) DO NOTHING;
                """, events_to_db)

        return len(events_to_db)

    def backfill_all_history(self) -> int:
        """
        Jednorazowo przelicza całą zebraną historię z tram_live.db 
        i zapisuje brakujące postoje do tram_analytics.db.
        """
        if self.plat_tree is None:
            self._load_platforms()
            if self.plat_tree is None:
                return 0

        # Pobieramy unikalne wozy, które mają jakiekolwiek wpisy w historii
        with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
            cur.execute("SELECT DISTINCT vehicle_number FROM tram_telemetry_history;")
            vehicles = [r["vehicle_number"] for r in cur.fetchall()]

        total_inserted = 0
        print(f"[BACKFILL] Rozpoczynam analitykę dla {len(vehicles)} wozów...")

        for v_num in vehicles:
            with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
                cur.execute("""
                    SELECT vehicle_number, line, brigade, lat, lon, speed_kmh, gps_time
                    FROM tram_telemetry_history
                    WHERE vehicle_number = ?
                    ORDER BY gps_time ASC;
                """, (v_num,))
                rows = cur.fetchall()

            if len(rows) < 5:
                continue

            df = pd.DataFrame([dict(r) for r in rows])
            df["gps_time"] = pd.to_datetime(df["gps_time"])
            df = df.drop_duplicates(subset=["gps_time"]).sort_values("gps_time").reset_index(drop=True)

            # Rzutowanie i dopasowanie peronów
            tr_xy = [wgs84_to_epsg2180(lon, lat) for lon, lat in zip(df["lon"], df["lat"])]
            tr_x, tr_y = zip(*tr_xy)

            # Zamiast 80.0 m, użyj 40.0 m - odcina przeciwległe jezdnie i perony sąsiednich ciągów
            dists, indices = self.plat_tree.query(
                np.column_stack([tr_x, tr_y]), distance_upper_bound=40.0
)
            df["dist_to_stop_m"] = dists
            df["candidate_stop"] = [self.plat_names[idx] if idx < len(self.plat_names) else None for idx in indices]
            df["candidate_cluster"] = [self.plat_clusters[idx] if idx < len(self.plat_clusters) else None for idx in indices]
            df["time_gap_prev"] = df["gps_time"].diff().dt.total_seconds().fillna(0)

            df["in_stop_zone"] = df["candidate_cluster"].notna()
            zone_boundary = (
                (~df["in_stop_zone"])
                | (df["candidate_cluster"] != df["candidate_cluster"].shift(1))
                | (df["time_gap_prev"] > 120.0)
            )
            df["zone_id"] = zone_boundary.cumsum()

            def process_zone(g: pd.DataFrame) -> pd.Series:
                c_idx = g["dist_to_stop_m"].idxmin()
                return pd.Series({
                    "cluster_name": g["candidate_cluster"].iloc[0],
                    "stop_name": g.loc[c_idx, "candidate_stop"],
                    "line": g["line"].iloc[0],
                    "brigade": g["brigade"].iloc[0],
                    "vehicle_number": g["vehicle_number"].iloc[0],
                    "start_time": g["gps_time"].min(),
                    "end_time": g["gps_time"].max(),
                    "min_speed": g["speed_kmh"].min(),
                    "avg_speed": g["speed_kmh"].mean(),
                    "min_dist": g["dist_to_stop_m"].min(),
                    "pings": len(g)
                })

            zones = df[df["in_stop_zone"]].groupby("zone_id", as_index=False).apply(process_zone, include_groups=False)
            if zones.empty:
                continue

            zones["zone_duration_sec"] = (zones["end_time"] - zones["start_time"]).dt.total_seconds()

            # Speed-Dip
            is_serviced = (
                (zones["min_speed"] < 3.0)
                | ((zones["min_speed"] < 14.0) & (zones["zone_duration_sec"] >= 12.0))
                | ((zones["min_speed"] < 5.0) & (zones["min_dist"] < 35.0))
            )
            serviced = zones[is_serviced & (zones["zone_duration_sec"] < 900.0)].copy()
            if serviced.empty:
                continue

            # Scalanie klastrów
            serviced["gap"] = (serviced["start_time"] - serviced["end_time"].shift(1)).dt.total_seconds()
            serviced["same_cluster"] = serviced["cluster_name"] == serviced["cluster_name"].shift(1)
            merge_id = ((~serviced["same_cluster"]) | (serviced["gap"] > 45.0)).cumsum()

            final_stops = serviced.groupby(merge_id).agg(
                vehicle_number=("vehicle_number", "first"),
                line=("line", "first"),
                brigade=("brigade", "first"),
                stop_name=("stop_name", "first"),
                cluster_name=("cluster_name", "first"),
                arrival_time=("start_time", "min"),
                departure_time=("end_time", "max"),
                min_speed_kmh=("min_speed", "min"),
                min_dist_m=("min_dist", "min"),
                duration_sec=("zone_duration_sec", "sum"),
                pings_count=("pings", "sum")
            )

            records = []
            for _, r in final_stops.iterrows():
                records.append((
                    str(r["vehicle_number"]),
                    str(r["line"]),
                    str(r["brigade"]),
                    str(r["stop_name"]),
                    str(r["cluster_name"]),
                    r["arrival_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    r["departure_time"].strftime("%Y-%m-%d %H:%M:%S"),
                    float(round(r["duration_sec"], 1)),
                    float(round(r["min_speed_kmh"], 1)),
                    float(round(r["min_dist_m"], 1)),
                    int(r["pings_count"])
                ))

            with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
                with transaction(cur):
                    cur.executemany("""
                        INSERT INTO tram_dwell_events (
                            vehicle_number, line, brigade, stop_name, cluster_name,
                            arrival_time, departure_time, duration_sec, min_speed_kmh,
                            min_dist_m, pings_count
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(vehicle_number, stop_name, arrival_time) DO NOTHING;
                    """, records)

            total_inserted += len(records)

        print(f"[BACKFILL] Zakończono. Przetworzono łącznie {total_inserted} postojów.")
        return total_inserted


analytics_engine = TelemetryAnalyticsEngine()

# Jednorazowe zasilenie tram_analytics.db całą dotychczasową zawartością tram_live.db:
