from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Generator
from config import RESOURCES_DIR
import json, math
from core.geo import clean_stop_cluster_name, wgs84_to_epsg2180
# Domyślne ścieżki do baz SQLite w projekcie
BIKES_DB_PATH = RESOURCES_DIR / "veturilo_history.db"
TRAM_DB_PATH = RESOURCES_DIR / "tram_network.db"
TRAM_LIVE_DB_PATH = RESOURCES_DIR / "tram_live.db"
CYCLEWAYS_DB_PATH = RESOURCES_DIR / "cycleways_network.db"
TRAM_ANALYTICS_DB_PATH = RESOURCES_DIR / "tram_analytics.db"

def apply_wal_pragmas(cursor: sqlite3.Cursor) -> None:
  """Konfiguruje silnik SQLite do pracy z dużą częstotliwością zapisu i odczytu."""
  # 1. Zapisy nie blokują odczytów (Write-Ahead Logging)
  cursor.execute("PRAGMA journal_mode=WAL;")
  # 2. Bezpieczna synchronizacja bez spowalniających operacji fsync na dysku
  cursor.execute("PRAGMA synchronous=NORMAL;")
  # 3. Trzymanie operacji tymczasowych (sortowania, indeksy) w pamięci RAM
  cursor.execute("PRAGMA temp_store=MEMORY;")
  # 4. Alokacja 64MB cache w RAM (-64000 = 64000 KiB)
  cursor.execute("PRAGMA cache_size=-64000;")
  # 5. Czas oczekiwania na zwolnienie blokady zanim rzuci 'database is locked'
  cursor.execute("PRAGMA busy_timeout=5000;")


@contextmanager
def get_db_cursor(
    db_path: Path, autocommit: bool = True
) -> Generator[sqlite3.Cursor, None, None]:
  """Uniwersalny generator bezpiecznego kursora SQLite.

  Udostępnia wiersze jako słowniki (sqlite3.Row) i automatycznie zamyka zasoby.
  """
  RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

  # isolation_level=None włącza tryb autocommit (niezbędne do ręcznego BEGIN IMMEDIATE)
  isolation = None if autocommit else ""
  conn = sqlite3.connect(
      db_path, timeout=10.0, check_same_thread=False, isolation_level=isolation
  )
  conn.row_factory = sqlite3.Row
  cursor = conn.cursor()

  try:
    yield cursor
  finally:
    cursor.close()
    conn.close()


@contextmanager
def transaction(cursor: sqlite3.Cursor) -> Generator[None, None, None]:
  """Blokuje bazę do natychmiastowego zapisu batcha (BEGIN IMMEDIATE)

  i dba o atomowy COMMIT lub automatyczny ROLLBACK w razie błędu.
  """
  cursor.execute("BEGIN IMMEDIATE;")
  try:
    yield
    cursor.execute("COMMIT;")
  except Exception:
    cursor.execute("ROLLBACK;")
    raise

def init_all_databases() -> None:
    """Inicjalizuje schematy wszystkich baz danych w systemie."""
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

    # --- 1. Drogi rowerowe OSM (statyczna) ---
    with get_db_cursor(CYCLEWAYS_DB_PATH) as cur:
        apply_wal_pragmas(cur)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cycleway_edges (
                way_id TEXT PRIMARY KEY,
                highway TEXT,
                cycleway TEXT,
                name TEXT,
                maxspeed INTEGER,
                properties_json TEXT,
                coordinates_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_cycleway_highway 
            ON cycleway_edges (highway);
        """)

    # --- 2. Infrastruktura tramwajowa OSM (statyczna, jawne koordynaty WGS84 + EPSG:2180) ---
    with get_db_cursor(TRAM_DB_PATH) as cur:
        apply_wal_pragmas(cur)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tram_edges (
                way_id TEXT PRIMARY KEY,
                properties_json TEXT NOT NULL,
                coordinates_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tram_platforms (
                osm_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cluster_name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                x_2180 REAL,
                y_2180 REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_platform_cluster 
            ON tram_platforms (cluster_name);
        """)

    # --- 3. Historia Veturilo ---
    with get_db_cursor(BIKES_DB_PATH) as cur:
        apply_wal_pragmas(cur)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                station_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS station_snapshots (
                station_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                bikes INTEGER NOT NULL,
                PRIMARY KEY (station_id, timestamp),
                FOREIGN KEY (station_id) REFERENCES stations (station_id)
            );
        """)

    # --- 4. Surowa telemetria tramwajów na żywo (Time-Series) ---
    with get_db_cursor(TRAM_LIVE_DB_PATH) as cur:
        apply_wal_pragmas(cur)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tram_telemetry_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_number TEXT NOT NULL,
                line TEXT NOT NULL,
                brigade TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                speed_kmh REAL NOT NULL,
                gps_time TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_tram_hist_line_brigade 
            ON tram_telemetry_history(line, brigade, gps_time);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_tram_hist_vehicle_time 
            ON tram_telemetry_history(vehicle_number, gps_time);
        """)

    # --- 5. Wyniki analityczne: postoje i czasy wymiany pasażerskiej ---
    with get_db_cursor(TRAM_ANALYTICS_DB_PATH) as cur:
        apply_wal_pragmas(cur)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tram_dwell_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_number TEXT NOT NULL,
                line TEXT NOT NULL,
                brigade TEXT,
                stop_name TEXT NOT NULL,
                cluster_name TEXT NOT NULL,
                arrival_time TIMESTAMP NOT NULL,
                departure_time TIMESTAMP NOT NULL,
                duration_sec REAL NOT NULL,
                min_speed_kmh REAL NOT NULL,
                min_dist_m REAL NOT NULL,
                pings_count INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(vehicle_number, stop_name, arrival_time) ON CONFLICT IGNORE
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dwell_line_cluster 
            ON tram_dwell_events(line, cluster_name);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_dwell_arrival 
            ON tram_dwell_events(arrival_time);
        """)

def migrate_tram_platforms() -> None:
  """Rozszerza istniejącą tabelę tram_platforms o jawne współrzędne bez utraty danych."""
  conn = sqlite3.connect(TRAM_DB_PATH)
  cur = conn.cursor()

  # 1. Sprawdzenie, czy kolumny już istnieją
  cur.execute("PRAGMA table_info(tram_platforms);")
  existing_cols = {row[1] for row in cur.fetchall()}

  new_cols = [
      ("cluster_name", "TEXT"),
      ("lat", "REAL"),
      ("lon", "REAL"),
      ("x_2180", "REAL"),
      ("y_2180", "REAL"),
  ]

  for col_name, col_type in new_cols:
    if col_name not in existing_cols:
      cur.execute(f"ALTER TABLE tram_platforms ADD COLUMN {col_name} {col_type};")

  # 2. Uzupełnienie danych na podstawie istniejącego coordinates_json
  cur.execute(
      "SELECT osm_id, name, coordinates_json FROM tram_platforms WHERE lat IS"
      " NULL;"
  )
  rows = cur.fetchall()

  updates = []
  for osm_id, name, raw_json in rows:
    try:
      coords = json.loads(raw_json)
      lon, lat = float(coords[0]), float(coords[1])
      x_2180, y_2180 = wgs84_to_epsg2180(lon, lat)
      cluster = clean_stop_cluster_name(name)
      updates.append((cluster, lat, lon, x_2180, y_2180, osm_id))
    except Exception:
      continue

  cur.executemany(
      """
        UPDATE tram_platforms 
        SET cluster_name = ?, lat = ?, lon = ?, x_2180 = ?, y_2180 = ?
        WHERE osm_id = ?;
    """,
      updates,
  )

  conn.commit()
  conn.close()
  print(f"Pomyślnie zmigrowano {len(updates)} peronów w tram_network.db.")

def migrate_tram_edges() -> None:
    """Rozszerza tram_edges o długość i metryczny Bounding Box w EPSG:2180."""
    conn = sqlite3.connect(TRAM_DB_PATH)
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(tram_edges);")
    existing_cols = {row[1] for row in cur.fetchall()}

    new_cols = [
        ("length_m", "REAL"),
        ("min_x", "REAL"),
        ("min_y", "REAL"),
        ("max_x", "REAL"),
        ("max_y", "REAL"),
        ("coords_2180_json", "TEXT")
    ]
    for col_name, col_type in new_cols:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE tram_edges ADD COLUMN {col_name} {col_type};")

    cur.execute("SELECT way_id, coordinates_json FROM tram_edges WHERE length_m IS NULL;")
    rows = cur.fetchall()

    updates = []
    for way_id, raw_json in rows:
        try:
            coords = json.loads(raw_json)
            if not coords or len(coords) < 2:
                continue
            
            # Konwersja całej linii na metry
            pts_2180 = [wgs84_to_epsg2180(float(c[0]), float(c[1])) for c in coords]
            xs = [p[0] for p in pts_2180]
            ys = [p[1] for p in pts_2180]

            # Długość w metrach (Pitagoras na układzie kartezjańskim 1992)
            total_len = sum(
                math.hypot(pts_2180[i][0] - pts_2180[i-1][0], pts_2180[i][1] - pts_2180[i-1][1])
                for i in range(1, len(pts_2180))
            )

            updates.append((
                round(total_len, 2),
                min(xs), min(ys), max(xs), max(ys),
                json.dumps(pts_2180),
                way_id
            ))
        except Exception:
            continue

    cur.executemany("""
        UPDATE tram_edges 
        SET length_m = ?, min_x = ?, min_y = ?, max_x = ?, max_y = ?, coords_2180_json = ?
        WHERE way_id = ?;
    """, updates)

    # Indeks pod zapytania przestrzenne w bounding boxie
    cur.execute("CREATE INDEX IF NOT EXISTS idx_edges_bbox ON tram_edges(min_x, max_x, min_y, max_y);")
    conn.commit()
    conn.close()
    print(f"Pomyślnie zmigrowano {len(updates)} odcinków torowisk w tram_network.db.")