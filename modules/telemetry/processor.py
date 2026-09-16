import math
import sqlite3
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree

from core.database import (
    get_db_cursor,
    transaction,
    TRAM_DB_PATH,
    TRAM_LIVE_DB_PATH,
    TRAM_ANALYTICS_DB_PATH,
)


from core.geo import wgs84_to_epsg2180


class TelemetryAnalyticsEngine:

    def __init__(self):
        self.plat_names = []
        self.plat_clusters = []
        self.plat_tree = None
        self.initialized = False

    def ensure_initialized(self):
        """Ładuje perony i buduje cKDTree (EPSG:2180) z bazy tram_network.db."""
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
    def analyze_recent_telemetry(self, lookback_minutes: int = 15, *args, **kwargs) -> int:
        """Analizuje telemetrię z ostatnich `lookback_minutes` minut."""
        self.ensure_initialized()
        if not self.plat_tree:
            return 0

        with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
            cur.execute(f"""
                SELECT DISTINCT vehicle_number 
                FROM tram_telemetry_history 
                WHERE gps_time >= datetime('now', '-{int(lookback_minutes)} minutes');
            """)
            vehicles = [r["vehicle_number"] for r in cur.fetchall()]

        if not vehicles:
            return 0

        return self.backfill_all_history()

    
    def backfill_all_history(self) -> int:
        """Przetwarza całą historię telemetrii algorytmem Speed-Dip (bufor 40 m)."""
        self.ensure_initialized()
        if not self.plat_tree or len(self.plat_names) == 0:
            print("[BACKFILL] Brak zainicjalizowanego cKDTree peronów.")
            return 0

        with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
            cur.execute(
                "SELECT DISTINCT vehicle_number FROM tram_telemetry_history;"
            )
            vehicles = [r["vehicle_number"] for r in cur.fetchall()]

        total_inserted = 0
        print(
            f"[BACKFILL] Rozpoczynam analitykę dla {len(vehicles)} wozów..."
        )

        for v_num in vehicles:
            with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
                cur.execute(
                    """
                    SELECT vehicle_number, line, brigade, lat, lon, speed_kmh, gps_time
                    FROM tram_telemetry_history
                    WHERE vehicle_number = ?
                    ORDER BY gps_time ASC;
                """,
                    (v_num,),
                )
                rows = cur.fetchall()

            if len(rows) < 5:
                continue

            df = pd.DataFrame([dict(r) for r in rows])
            df["gps_time"] = pd.to_datetime(df["gps_time"])
            df = (
                df.drop_duplicates(subset=["gps_time"])
                .sort_values("gps_time")
                .reset_index(drop=True)
            )

            # Rzutowanie współrzędnych i dopasowanie peronów
            tr_xy = [
                wgs84_to_epsg2180(lon, lat)
                for lon, lat in zip(df["lon"], df["lat"])
            ]
            tr_x, tr_y = zip(*tr_xy)

            # Bufor 40 m – eliminuje przeciwległe jezdnie i fałszywe perony
            dists, indices = self.plat_tree.query(
                np.column_stack([tr_x, tr_y]), distance_upper_bound=40.0
            )

            df["dist_to_stop_m"] = dists
            df["candidate_stop"] = [
                self.plat_names[idx] if idx < len(self.plat_names) else None
                for idx in indices
            ]
            df["candidate_cluster"] = [
                self.plat_clusters[idx]
                if idx < len(self.plat_clusters)
                else None
                for idx in indices
            ]
            df["time_gap_prev"] = (
                df["gps_time"].diff().dt.total_seconds().fillna(0)
            )

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
                    "pings": len(g),
                })

            valid_zones = df[df["in_stop_zone"]]
            if valid_zones.empty:
                continue

            zones = valid_zones.groupby("zone_id", as_index=False).apply(
                process_zone, include_groups=False
            )
            if zones.empty:
                continue

            zones["zone_duration_sec"] = (
                zones["end_time"] - zones["start_time"]
            ).dt.total_seconds()

            # Warunki Speed-Dip
            is_serviced = (
                (zones["min_speed"] < 3.0)
                | (
                    (zones["min_speed"] < 14.0)
                    & (zones["zone_duration_sec"] >= 12.0)
                )
                | ((zones["min_speed"] < 5.0) & (zones["min_dist"] < 35.0))
            )
            serviced = zones[
                is_serviced & (zones["zone_duration_sec"] < 900.0)
            ].copy()
            if serviced.empty:
                continue

            # Scalanie sąsiadujących stref tego samego zespołu przystankowego
            serviced["gap"] = (
                serviced["start_time"] - serviced["end_time"].shift(1)
            ).dt.total_seconds()
            serviced["same_cluster"] = serviced["cluster_name"] == serviced[
                "cluster_name"
            ].shift(1)
            merge_id = (
                (~serviced["same_cluster"]) | (serviced["gap"] > 45.0)
            ).cumsum()

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
                pings_count=("pings", "sum"),
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
                    int(r["pings_count"]),
                ))

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
                        records,
                    )

            total_inserted += len(records)

        print(
            f"[BACKFILL] Zakończono. Przetworzono łącznie {total_inserted}"
            " postojów."
        )
        return total_inserted


analytics_engine = TelemetryAnalyticsEngine()