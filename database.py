import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path("veturilo_history.db")
JSON_CACHE_PATH = Path("history_cache.json")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Inicjalizuje schemat bazy, indeksy i odpala tryb WAL."""
    with get_connection() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stations (
                station_id TEXT PRIMARY KEY,
                name TEXT NOT NULL
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS station_snapshots (
                station_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                bikes INTEGER NOT NULL,
                FOREIGN KEY (station_id) REFERENCES stations (station_id)
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_station_time 
            ON station_snapshots (station_id, timestamp);
        """)

    # Sprawdzenie czy baza jest świeża i wymaga zaimportowania istniejącego JSON-a
    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM station_snapshots;").fetchone()[0]
        if count == 0 and JSON_CACHE_PATH.exists():
            migrate_json_to_db(JSON_CACHE_PATH)


def migrate_json_to_db(json_path: Path):
    """Jednorazowa migracja z pliku history_cache.json do SQLite."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    metadata: Dict[str, str] = data.get("metadata", {})
    buffer: Dict[str, List[Dict[str, Any]]] = data.get("buffer", {})

    with get_connection() as conn:
        # 1. Zapis słownika stacji
        stations = [(s_id, name) for s_id, name in metadata.items()]
        conn.executemany(
            "INSERT OR IGNORE INTO stations (station_id, name) VALUES (?, ?);",
            stations,
        )

        # 2. Zapis logów historycznych
        snapshots = []
        for s_id, entries in buffer.items():
            for entry in entries:
                snapshots.append((s_id, entry["datetime"], entry["bikes"]))

        conn.executemany(
            "INSERT INTO station_snapshots (station_id, timestamp, bikes) VALUES (?, ?, ?);",
            snapshots,
        )


def log_snapshot(station_id: str, name: str, timestamp: str, bikes: int):
    """Zapis pojedynczej próbki ze skryptu zbierającego dane GBFS."""
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO stations (station_id, name) VALUES (?, ?);",
            (station_id, name),
        )
        conn.execute(
            "INSERT INTO station_snapshots (station_id, timestamp, bikes) VALUES (?, ?, ?);",
            (station_id, timestamp, bikes),
        )


def get_series_for_chart(station_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Zwraca dane stacji w formacie zgodnym z dotychczasowym skryptem Matplotlib."""
    query = """
        SELECT timestamp, bikes 
        FROM station_snapshots 
        WHERE station_id = ? 
        ORDER BY timestamp ASC
    """
    if limit:
        query += f" LIMIT {int(limit)}"

    with get_connection() as conn:
        rows = conn.execute(query, (station_id,)).fetchall()
        return [{"datetime": row["timestamp"], "bikes": row["bikes"]} for row in rows]


def get_station_deltas(station_id: str, start_time: str, end_time: str) -> List[Dict[str, Any]]:
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
    with get_connection() as conn:
        rows = conn.execute(query, (station_id, start_time, end_time)).fetchall()
        return [dict(row) for row in rows]