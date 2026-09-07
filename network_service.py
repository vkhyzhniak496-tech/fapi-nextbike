import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Generator, Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException
import httpx

from models import SyncCustomRequest

router = APIRouter(prefix="/network", tags=["Network GIS"])

RESOURCES_DIR = Path(__file__).parent / "resources"
NETWORK_DB_PATH = RESOURCES_DIR / "cycleways_network.db"

TILES = [
    {
        "name": "SW (Mokotów, Ochota, Ursynów)",
        "bbox": "52.09,20.85,52.23,21.05",
    },
    {"name": "SE (Praga Płd, Wilanów, Wawer)", "bbox": "52.09,21.05,52.23,21.27"},
    {"name": "NW (Wola, Bemowo, Bielany)", "bbox": "52.23,20.85,52.37,21.05"},
    {
        "name": "NE (Śródmieście, Praga Płn, Białołęka)",
        "bbox": "52.23,21.05,52.37,21.27",
    },
]

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

HEADERS = {
    "User-Agent": "WarsawBikeRouter/1.0 (GIS research)",
    "Accept": "application/json",
}


# --- 1. ZARZĄDZANIE BAZĄ DANYCH SIECI TRAS ---


@contextmanager
def get_network_db_cursor() -> Generator[sqlite3.Cursor, None, None]:
  """Zarządza dedykowanym połączeniem i transakcją do bazy sieci tras."""
  conn = sqlite3.connect(NETWORK_DB_PATH, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  try:
    with conn:
      yield conn.cursor()
  finally:
    conn.close()


def init_network_db():
  """Tworzy schemat tabeli i indeksy w cycleways_network.db."""
  RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
  with get_network_db_cursor() as cur:
    cur.execute("PRAGMA journal_mode=WAL;")
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


# Inicjalizacja tabeli przy starcie aplikacji podczas importu routera:
init_network_db()


# --- 2. ZAPYTANIA DO OVERPASS API I ZAPIS SQL ---


def build_tile_query(bbox: str) -> str:
  return f"""
    [out:json][timeout:120];
    (
      way["highway"="cycleway"]({bbox});
      way["highway"~"^(path|footway|pedestrian)$"]["bicycle"~"^(designated|yes)$"]({bbox});
      way["cycleway"~"^(lane|opposite_lane|track|share_busway|opposite)$"]({bbox});
      way["cycleway:both"~"^(lane|opposite_lane|track|share_busway|opposite)$"]({bbox});
      way["cycleway:left"~"^(lane|opposite_lane|track|share_busway|opposite)$"]({bbox});
      way["cycleway:right"~"^(lane|opposite_lane|track|share_busway|opposite)$"]({bbox});
      way["busway:right"="share_busway"]({bbox});
      way["busway:left"="share_busway"]({bbox});
      way["oneway:bicycle"="no"]({bbox});
      way["highway"="living_street"]({bbox});
      way["highway"="residential"]["maxspeed"="30"]({bbox});
    );
    out body geom;
    """


async def fetch_tile_with_retry(bbox: str, client: httpx.AsyncClient) -> list:
  query = build_tile_query(bbox)
  for server in OVERPASS_SERVERS:
    for attempt in range(1, 4):
      try:
        res = await client.post(server, data={"data": query})
        if res.status_code == 200:
          return res.json().get("elements", [])
        elif res.status_code == 429:
          await asyncio.sleep(attempt * 6)
        else:
          await asyncio.sleep(2)
      except Exception:
        await asyncio.sleep(2)
  return []


def save_osm_elements_to_db(elements: list) -> int:
  """Zapisuje kafelki Overpass bezpośrednio do SQLite w pojedynczej transakcji."""
  rows = []
  for el in elements:
    if el.get("type") == "way" and "geometry" in el:
      way_id = f"way/{el['id']}"
      tags = el.get("tags", {})
      coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]

      maxspeed_val = tags.get("maxspeed", "")
      maxspeed_int = int(maxspeed_val) if maxspeed_val.isdigit() else None

      rows.append((
          way_id,
          tags.get("highway"),
          tags.get("cycleway"),
          tags.get("name"),
          maxspeed_int,
          json.dumps(tags, ensure_ascii=False),
          json.dumps(coords),
      ))

  if not rows:
    return 0

  with get_network_db_cursor() as cursor:
    cursor.executemany(
        """
            INSERT INTO cycleway_edges (
                way_id, highway, cycleway, name, maxspeed, properties_json, coordinates_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(way_id) DO UPDATE SET
                highway = excluded.highway,
                cycleway = excluded.cycleway,
                name = excluded.name,
                maxspeed = excluded.maxspeed,
                properties_json = excluded.properties_json,
                coordinates_json = excluded.coordinates_json,
                updated_at = CURRENT_TIMESTAMP;
        """,
        rows,
    )
  return len(rows)


async def sync_overpass_to_db_task(custom_bbox: Optional[str] = None):
  print("Rozpoczęto synchronizację sieci tras do bazy SQLite...")
  total_saved = 0

  async with httpx.AsyncClient(timeout=120.0, headers=HEADERS) as client:
    targets = [custom_bbox] if custom_bbox else [t["bbox"] for t in TILES]
    for bbox in targets:
      elements = await fetch_tile_with_retry(bbox, client)
      added = save_osm_elements_to_db(elements)
      total_saved += added
      print(f"Pobrano kafelek: zapisano/zaktualizowano {added} odcinków.")
      await asyncio.sleep(3)

  print(f"Zakończono synchronizację. Łącznie w SQLite: {total_saved} tras.")


# --- 3. ENDPOINTY FASTAPI DLA SIECI TRAS ---


@router.get("/safe-cycleways")
async def get_safe_cycleways():
  """Zwraca całą sieć dróg bezpośrednio z bazy SQLite."""
  with get_network_db_cursor() as cursor:
    cursor.execute("""
            SELECT way_id, properties_json, coordinates_json 
            FROM cycleway_edges;
        """)
    rows = cursor.fetchall()

  if not rows:
    raise HTTPException(
        status_code=404,
        detail=(
            "Baza tras jest pusta. Uruchom synchronizację POST"
            " /network/safe-cycleways/sync"
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

  return {
      "type": "FeatureCollection",
      "generator": "sqlite-network-engine",
      "total_ways": len(features),
      "features": features,
  }


@router.post("/safe-cycleways/sync")
async def trigger_cycleways_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
  """Pobiera trasy z Overpass i zapisuje bezpośrednio do bazy SQLite w tle."""
  custom_bbox = payload.bbox if payload else None
  background_tasks.add_task(sync_overpass_to_db_task, custom_bbox)
  return {
      "status": "accepted",
      "message": "Trwa synchronizacja sieci z Overpass do bazy SQLite.",
  }