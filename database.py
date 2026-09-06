from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional

BASE_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = BASE_DIR / "resources"
DB_PATH = RESOURCES_DIR / "veturilo_history.db"
JSON_CACHE_PATH = RESOURCES_DIR / "history_cache.json"


@contextmanager
def get_db_cursor() -> Generator[sqlite3.Cursor, None, None]:
  """Zapewnia automatyczny commit oraz pewne zamknięcie połączenia (brak wycieków FD)."""
  conn = sqlite3.connect(DB_PATH, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  try:
    with conn:  # Zarządza transakcją (BEGIN / COMMIT / ROLLBACK)
      yield conn.cursor()
  finally:
    conn.close()


def init_db():
  with get_db_cursor() as cur:
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")

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

  with get_db_cursor() as cur:
    cur.execute("SELECT COUNT(*) FROM station_snapshots;")
    if cur.fetchone()[0] == 0 and JSON_CACHE_PATH.exists():
      migrate_json_to_db(JSON_CACHE_PATH)


def migrate_json_to_db(json_path: Path):
  with open(json_path, "r", encoding="utf-8") as f:
    data = json.load(f)

  metadata: Dict[str, str] = data.get("metadata", {})
  buffer: Dict[str, List[Dict[str, Any]]] = data.get("buffer", {})

  with get_db_cursor() as cur:
    cur.executemany(
        "INSERT OR IGNORE INTO stations (station_id, name) VALUES (?, ?);",
        list(metadata.items()),
    )

    snapshots = [
        (s_id, entry["datetime"], entry["bikes"])
        for s_id, entries in buffer.items()
        for entry in entries
    ]
    cur.executemany(
        """INSERT OR IGNORE INTO station_snapshots (station_id, timestamp, bikes) 
           VALUES (?, ?, ?);""",
        snapshots,
    )


def log_snapshot(station_id: str, name: str, timestamp: str, bikes: int):
  with get_db_cursor() as cur:
    cur.execute(
        "INSERT OR IGNORE INTO stations (station_id, name) VALUES (?, ?);",
        (station_id, name),
    )
    cur.execute(
        """INSERT OR IGNORE INTO station_snapshots (station_id, timestamp, bikes) 
           VALUES (?, ?, ?);""",
        (station_id, timestamp, bikes),
    )


def get_series_for_chart(
    station_id: str, limit: Optional[int] = None
) -> List[Dict[str, Any]]:
  """Zwraca dane stacji zoptymalizowane pod wykresy."""
  query = """
        SELECT timestamp, bikes 
        FROM station_snapshots 
        WHERE station_id = ? 
        ORDER BY timestamp ASC
    """
  if limit:
    query += f" LIMIT {int(limit)}"

  with get_db_cursor() as cur:
    cur.execute(query, (station_id,))
    return [
        {"datetime": row["timestamp"], "bikes": row["bikes"]}
        for row in cur.fetchall()
    ]


def get_station_deltas(
    station_id: str, start_time: str, end_time: str
) -> List[Dict[str, Any]]:
  """Wyciąga tylko punkty zmian (+1 / -1) dla wybranej stacji w oknie czasowym."""
  query = """
        WITH calculated_deltas AS (
            SELECT 
                timestamp,
                bikes,
                bikes - LAG(bikes) OVER (ORDER BY timestamp) AS delta
            FROM station_snapshots
            WHERE station_id = ? AND timestamp BETWEEN ? AND ?
        )
        SELECT timestamp, bikes, delta
        FROM calculated_deltas
        WHERE delta IS NOT NULL AND delta != 0
        ORDER BY timestamp ASC;
    """
  with get_db_cursor() as cur:
    cur.execute(query, (station_id, start_time, end_time))
    return [dict(row) for row in cur.fetchall()]