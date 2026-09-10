from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Generator
from config import RESOURCES_DIR

# Domyślne ścieżki do baz SQLite w projekcie
BIKES_DB_PATH = RESOURCES_DIR / "veturilo_history.db"
TRAM_DB_PATH = RESOURCES_DIR / "tram_network.db"
TRAM_LIVE_DB_PATH = RESOURCES_DIR / "tram_live.db"
CYCLEWAYS_DB_PATH = RESOURCES_DIR / "cycleways_network.db"

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
  """Tworzy katalog resources i inicjalizuje schematy wszystkich 4 baz SQLite."""
  RESOURCES_DIR.mkdir(parents=True, exist_ok=True)

  # 1. Baza dróg rowerowych OSM (statyczna)
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

  # 2. Baza torowisk i peronów OSM (statyczna)
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
                name TEXT,
                coordinates_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

  # Baza stacji Veturilo (Twoja oryginalna struktura)
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

  # 4. Baza telemetrii tramwajów na żywo (Time-Series, wysoka częstotliwość)
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