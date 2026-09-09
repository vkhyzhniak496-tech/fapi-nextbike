import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
import httpx

from models import SyncCustomRequest
from config import OVERPASS_HEADERS 
router = APIRouter(prefix="/network/tram", tags=["Tram Network GIS"])

RESOURCES_DIR = Path(__file__).parent / "resources"
TRAM_DB_PATH = RESOURCES_DIR / "tram_network.db"
DEFAULT_WARSAW_BBOX = "52.09,20.85,52.37,21.27"

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

HEADERS = {
    "User-Agent": "WarsawTramGIS/1.0 (Urban transit research)",
    "Accept": "application/json",
}


@contextmanager
def get_tram_db_cursor() -> Generator[sqlite3.Cursor, None, None]:
  """Zarządza dedykowanym połączeniem i transakcją do bazy torowisk tramwajowych."""
  conn = sqlite3.connect(TRAM_DB_PATH, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  try:
    with conn:
      yield conn.cursor()
  finally:
    conn.close()

def init_tram_db():
    """Inicjalizuje schemat tabel w resources/tram_network.db."""
    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    with get_tram_db_cursor() as cur:
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tram_edges (
                way_id TEXT PRIMARY KEY,
                properties_json TEXT NOT NULL,
                coordinates_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Jeśli w tabeli wciąż wisi stara kolumna geom_type, dropujemy tabelę
        cur.execute("PRAGMA table_info(tram_platforms);")
        cols = [r["name"] for r in cur.fetchall()]
        if "geom_type" in cols:
            cur.execute("DROP TABLE tram_platforms;")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tram_platforms (
                osm_id TEXT PRIMARY KEY,
                name TEXT,
                coordinates_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

init_tram_db()


def build_tram_overpass_query(bbox: str) -> str:
  return f"""
    [out:json][timeout:120];
    (
      way["railway"="tram"]({bbox});
    );
    out body geom;
    """


async def sync_tram_network_task(bbox: str):
  query = build_tram_overpass_query(bbox)
  print(f"[TRAM_GIS] Pobieranie sieci szynowej dla BBOX: {bbox}...")

  raw_elements: List[Dict[str, Any]] = []
  async with httpx.AsyncClient(timeout=150.0, headers=HEADERS) as client:
    for server in OVERPASS_SERVERS:
      try:
        res = await client.post(server, data={"data": query})
        if res.status_code == 200:
          raw_elements = res.json().get("elements", [])
          print(
              f"[TRAM_GIS] Sukces z serwera {server}. Segmentów:"
              f" {len(raw_elements)}"
          )
          break
        elif res.status_code == 429:
          await asyncio.sleep(5)
      except Exception as e:
        print(f"[TRAM_GIS] Błąd połączenia z {server}: {e}")

  if not raw_elements:
    print("[TRAM_GIS] Nie udało się pobrać torowisk z żadnego serwera.")
    return

  records = []
  for el in raw_elements:
    if el.get("type") == "way" and "geometry" in el:
      way_id = f"way/{el['id']}"
      props = el.get("tags", {})
      coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
      records.append((
          way_id,
          json.dumps(props, ensure_ascii=False),
          json.dumps(coords),
      ))

  with get_tram_db_cursor() as cursor:
    cursor.executemany(
        """
            INSERT INTO tram_edges (way_id, properties_json, coordinates_json)
            VALUES (?, ?, ?)
            ON CONFLICT(way_id) DO UPDATE SET
                properties_json=excluded.properties_json,
                coordinates_json=excluded.coordinates_json,
                updated_at=CURRENT_TIMESTAMP;
        """,
        records,
    )

  print(f"[TRAM_GIS] Zapisano do bazy SQLite: {len(records)} odcinków torowisk.")


@router.get("")
async def get_tram_network(response: Response):
  """Zwraca geometrię torowisk z bazy SQLite z nagłówkiem cache na 7 dni."""
  with get_tram_db_cursor() as cursor:
    cursor.execute(
        "SELECT way_id, properties_json, coordinates_json FROM tram_edges;"
    )
    rows = cursor.fetchall()

  if not rows:
    raise HTTPException(
        status_code=404,
        detail=(
            "Baza torowisk jest pusta. Uruchom POST /network/tram/sync, aby"
            " pobrać dane z OSM."
        ),
    )

  features = [
      {
          "type": "Feature",
          "id": r["way_id"],
          "properties": json.loads(r["properties_json"]),
          "geometry": {
              "type": "LineString",
              "coordinates": json.loads(r["coordinates_json"]),
          },
      }
      for r in rows
  ]

  response.headers["Cache-Control"] = "public, max-age=604800, immutable"

  return {
      "type": "FeatureCollection",
      "generator": "sqlite-tram-engine",
      "total_ways": len(features),
      "features": features,
  }


@router.post("/sync")
async def trigger_tram_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
  """Zleca asynchroniczną synchronizację szyn tramwajowych w tle."""
  bbox = DEFAULT_WARSAW_BBOX
  if payload and payload.bbox and payload.bbox.strip() not in ("", "string"):
    bbox = payload.bbox.strip()

  background_tasks.add_task(sync_tram_network_task, bbox)
  return {
      "status": "accepted",
      "message": "Synchronizacja torowisk tramwajowych uruchomiona w tle.",
      "target": bbox,
  }


# --- PRZYSTANKI TRAMWAJOWE (TYLKO PUNKTY) ---
def build_tram_platforms_query(bbox: str) -> str:
  # Tylko i wyłącznie punkty słupków tramwajowych
  return f"""
    [out:json][timeout:60];
    (
      node["railway"="tram_stop"]({bbox});
    );
    out body;
    """


async def sync_tram_platforms_task(bbox: str):
  query = build_tram_platforms_query(bbox)
  print(f"[PLATFORMS_GIS] Pobieranie słupków tramwajowych...", flush=True)

  raw_elements = []
  async with httpx.AsyncClient(timeout=90.0, headers=HEADERS) as client:
    for server in OVERPASS_SERVERS:
      try:
        res = await client.post(server, data={"data": query})
        if res.status_code == 200:
          raw_elements = res.json().get("elements", [])
          break
        elif res.status_code == 429:
          await asyncio.sleep(5)
      except Exception:
        continue

  if not raw_elements:
    print("[PLATFORMS_GIS] Brak danych z Overpass.", flush=True)
    return

  records = []
  for el in raw_elements:
    if el.get("type") == "node" and "lon" in el and "lat" in el:
      name = el.get("tags", {}).get("name") or "Przystanek"
      records.append((
          f"node/{el['id']}",
          name,
          json.dumps([el["lon"], el["lat"]]),  # Czysty punkt [lon, lat]
      ))

  with get_tram_db_cursor() as cur:
    cur.execute("DELETE FROM tram_platforms;")
    cur.executemany(
        """
            INSERT INTO tram_platforms (osm_id, name, coordinates_json)
            VALUES (?, ?, ?)
            ON CONFLICT(osm_id) DO UPDATE SET
                name=excluded.name,
                coordinates_json=excluded.coordinates_json,
                updated_at=CURRENT_TIMESTAMP;
        """,
        records,
    )

  print(
      f"[PLATFORMS_GIS] Zapisano w SQLite {len(records)} unikalnych słupków.",
      flush=True,
  )


@router.get("/platforms")
async def get_tram_platforms():
  """Zwraca wyłącznie punkty słupków jako lekki GeoJSON."""
  with get_tram_db_cursor() as cur:
    cur.execute("SELECT osm_id, name, coordinates_json FROM tram_platforms;")
    rows = cur.fetchall()

  if not rows:
    raise HTTPException(
        status_code=404, detail="Baza przystanków pusta. Uruchom sync."
    )

  features = [
      {
          "type": "Feature",
          "id": r["osm_id"],
          "properties": {"name": r["name"]},
          "geometry": {
              "type": "Point",
              "coordinates": json.loads(r["coordinates_json"]),
          },
      }
      for r in rows
  ]

  return {
      "type": "FeatureCollection",
      "total": len(features),
      "features": features,
  }

@router.post("/platforms/sync")
async def trigger_platforms_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None
):
    """Pobiera wyłącznie węzły przystanków tramwajowych z OSM i zapisuje do SQLite."""
    bbox = DEFAULT_WARSAW_BBOX
    if payload and payload.bbox and payload.bbox.strip() not in ("", "string"):
        bbox = payload.bbox.strip()

    background_tasks.add_task(sync_tram_platforms_task, bbox)
    return {
        "status": "accepted",
        "message": "Synchronizacja przystanków tramwajowych uruchomiona w tle.",
        "target": bbox
    }