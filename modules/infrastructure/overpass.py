import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
import httpx

from config import OVERPASS_HEADERS, OVERPASS_SERVERS
from core.database import (
    CYCLEWAYS_DB_PATH,
    TRAM_DB_PATH,
    get_db_cursor,
    transaction,
)

logger = logging.getLogger(__name__)

CYCLEWAY_TILES = [
    {
        "name": "SW (Mokotów, Ochota, Ursynów)",
        "bbox": "52.09,20.85,52.23,21.05",
    },
    {
        "name": "SE (Praga Płd, Wilanów, Wawer)",
        "bbox": "52.09,21.05,52.23,21.27",
    },
    {
        "name": "NW (Wola, Bemowo, Bielany)",
        "bbox": "52.23,20.85,52.37,21.05",
    },
    {
        "name": "NE (Śródmieście, Praga Płn, Białołęka)",
        "bbox": "52.23,21.05,52.37,21.27",
    },
]

# --- 1. ZAPYTANIA OVERPASS QL ---


def build_cycleways_query(bbox: str) -> str:
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


def build_tram_tracks_query(bbox: str) -> str:
    return f"""
    [out:json][timeout:120];
    (
      way["railway"="tram"]({bbox});
      way["railway"="construction"]["construction"="tram"]({bbox});
      way["railway"="disused"]({bbox});
    );
    out body geom;
    """


def build_tram_platforms_query(bbox: str) -> str:
  return f"""
    [out:json][timeout:60];
    (
      node["railway"="tram_stop"]({bbox});
    );
    out body;
    """


# --- 2. SILNIK POBIERANIA ---


async def execute_overpass_query(
    query: str, client: httpx.AsyncClient, max_retries: int = 3
) -> List[Dict[str, Any]]:
    for server in OVERPASS_SERVERS:
        for attempt in range(1, max_retries + 1):
            try:
                res = await client.post(server, data={"data": query})
                if res.status_code == 200:
                    payload = res.json()
                    if "remark" in payload:
                        logger.error(f"[INFRA_GIS] Błąd składni Overpass na {server}: {payload['remark']}")
                    return payload.get("elements", [])
                if res.status_code == 429:
                    await asyncio.sleep(attempt * 5)
                else:
                    await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"[INFRA_GIS] Błąd połączenia z {server}: {e}")
                await asyncio.sleep(2)
    return []


# --- 3. ZAPIS DO BAZ SQLITE ---


def save_cycleways_to_db(elements: List[Dict[str, Any]]) -> int:
  rows = []
  for el in elements:
    if el.get("type") == "way" and "geometry" in el:
      tags = el.get("tags", {})
      maxspeed_val = tags.get("maxspeed", "")
      maxspeed_int = int(maxspeed_val) if maxspeed_val.isdigit() else None
      coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]

      rows.append((
          f"way/{el['id']}",
          tags.get("highway"),
          tags.get("cycleway"),
          tags.get("name"),
          maxspeed_int,
          json.dumps(tags, ensure_ascii=False),
          json.dumps(coords),
      ))

  if not rows:
    return 0

  with get_db_cursor(CYCLEWAYS_DB_PATH) as cur:
    with transaction(cur):
      cur.executemany(
          """
                INSERT INTO cycleway_edges (
                    way_id, highway, cycleway, name, maxspeed, properties_json, coordinates_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(way_id) DO UPDATE SET
                    highway=excluded.highway,
                    cycleway=excluded.cycleway,
                    name=excluded.name,
                    maxspeed=excluded.maxspeed,
                    properties_json=excluded.properties_json,
                    coordinates_json=excluded.coordinates_json,
                    updated_at=CURRENT_TIMESTAMP;
            """,
          rows,
      )
  return len(rows)


def save_tram_tracks_to_db(elements: List[Dict[str, Any]]) -> int:
    records = []
    seen_ways = set()

    for el in elements:
        if el.get("type") == "way" and "geometry" in el:
            way_key = f"way/{el['id']}"
            # Zabezpieczenie przed dublami w ramach jednej odpowiedzi Overpassa
            if way_key in seen_ways:
                continue
            seen_ways.add(way_key)

            coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
            records.append((
                way_key,
                json.dumps(el.get("tags", {}), ensure_ascii=False),
                json.dumps(coords),
            ))

    if not records:
        return 0

    with get_db_cursor(TRAM_DB_PATH) as cur:
        with transaction(cur):
            # 1. Czyścimy tabelę przed zasileniem świeżymi danymi
            cur.execute("DELETE FROM tram_edges;")

            # 2. Wstawiamy czyste, aktualne odcinki
            cur.executemany(
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
    return len(records)


def save_tram_platforms_to_db(elements: List[Dict[str, Any]]) -> int:
  records = []
  for el in elements:
    if el.get("type") == "node" and "lon" in el and "lat" in el:
      name = el.get("tags", {}).get("name") or "Przystanek"
      records.append(
          (f"node/{el['id']}", name, json.dumps([el["lon"], el["lat"]]))
      )

  if not records:
    return 0

  with get_db_cursor(TRAM_DB_PATH) as cur:
    with transaction(cur):
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
  return len(records)


# --- 4. ZADANIA TŁOWE DLA FASTAPI ---


async def sync_cycleways_task(custom_bbox: Optional[str] = None):
  logger.info("[INFRA_GIS] Synchronizacja dróg rowerowych z OSM...")
  total = 0
  async with httpx.AsyncClient(
      timeout=120.0, headers=OVERPASS_HEADERS
  ) as client:
    targets = (
        [custom_bbox] if custom_bbox else [t["bbox"] for t in CYCLEWAY_TILES]
    )
    for bbox in targets:
      elements = await execute_overpass_query(
          build_cycleways_query(bbox), client
      )
      added = save_cycleways_to_db(elements)
      total += added
      await asyncio.sleep(2)
  logger.info(f"[INFRA_GIS] Zakończono trasy rowerowe. Zapisano: {total}")


async def sync_tram_tracks_task(bbox: str):
  logger.info(f"[INFRA_GIS] Synchronizacja torowisk dla BBOX: {bbox}...")
  async with httpx.AsyncClient(
      timeout=150.0, headers=OVERPASS_HEADERS
  ) as client:
    elements = await execute_overpass_query(build_tram_tracks_query(bbox), client)
    added = save_tram_tracks_to_db(elements)
  logger.info(f"[INFRA_GIS] Zapisano torowisk: {added}")


async def sync_tram_platforms_task(bbox: str):
  logger.info(f"[INFRA_GIS] Synchronizacja peronów tramwajowych...")
  async with httpx.AsyncClient(timeout=90.0, headers=OVERPASS_HEADERS) as client:
    elements = await execute_overpass_query(
        build_tram_platforms_query(bbox), client
    )
    added = save_tram_platforms_to_db(elements)
  logger.info(f"[INFRA_GIS] Zapisano peronów: {added}")