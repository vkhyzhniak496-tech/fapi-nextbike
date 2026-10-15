import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
import httpx

from core.database import get_pool

logger = logging.getLogger(__name__)

# Tylko zweryfikowane serwery akceptujące publiczne kwerendy GIS
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Polityka OSM wymaga formatu: NazwaAplikacji/Wersja (kontakt@domena.com)
OVERPASS_HEADERS = {
    "User-Agent": "WarsawTransitGIS/2.0 (urban.gis.research.warsaw@gmail.com)",
    "Accept": "application/json",
}

CYCLEWAY_TILES = [
    {"name": "SW (Mokotów, Ochota, Ursynów)", "bbox": "52.09,20.85,52.23,21.05"},
    {"name": "SE (Praga Płd, Wilanów, Wawer)", "bbox": "52.09,21.05,52.23,21.27"},
    {"name": "NW (Wola, Bemowo, Bielany)",    "bbox": "52.23,20.85,52.37,21.05"},
    {"name": "NE (Śródmieście, Praga Płn)",   "bbox": "52.23,21.05,52.37,21.27"},
]

DEFAULT_WARSAW_BBOX = "52.09,20.85,52.37,21.27"


def _coords_to_linestring_wkt(coords: List[Dict[str, Any]]) -> str:
    points = [f"{pt['lon']} {pt['lat']}" for pt in coords]
    return f"LINESTRING({', '.join(points)})"


def _sanitize_bbox(bbox: Optional[str]) -> str:
    if not bbox or not isinstance(bbox, str):
        return DEFAULT_WARSAW_BBOX
    val = bbox.strip()
    if val in ("", "string") or len(val.split(",")) != 4:
        return DEFAULT_WARSAW_BBOX
    return val


def build_cycleways_query(bbox: str) -> str:
    return f"""
    [out:json][timeout:90][maxsize:536870912];
    (
      way["highway"="cycleway"]({bbox});
      way["highway"~"^(path|footway|pedestrian)$"]["bicycle"~"^(designated|yes)$"]({bbox});
      way["cycleway"]({bbox});
      way["cycleway:left"]({bbox});
      way["cycleway:right"]({bbox});
      way["cycleway:both"]({bbox});
      way["oneway:bicycle"="no"]({bbox});
      way["highway"="living_street"]({bbox});
      // Nowo otwierane trasy i przejezdne budowy (np. al. Jana Pawła II):
      way["highway"="construction"]["construction"="cycleway"]({bbox});
    );
    out body geom;
    """


def build_tram_tracks_query(bbox: str) -> str:
    return f"""
    [out:json][timeout:90][maxsize:536870912];
    (
      way["railway"="tram"]({bbox});
    );
    out body geom;
    """


def build_tram_platforms_query(bbox: str) -> str:
    return f"""
    [out:json][timeout:60][maxsize:268435456];
    (
      node["railway"="tram_stop"]({bbox});
    );
    out body;
    """


async def execute_overpass_query(
    query: str, client: httpx.AsyncClient, max_retries: int = 2
) -> List[Dict[str, Any]]:
    for server in OVERPASS_SERVERS:
        for attempt in range(1, max_retries + 1):
            try:
                res = await client.post(
                    server,
                    data={"data": query},
                    headers=OVERPASS_HEADERS,
                    timeout=120.0
                )

                if res.status_code == 200:
                    try:
                        payload = res.json()
                    except Exception:
                        break

                    if "remark" in payload and "error" in payload["remark"].lower():
                        logger.warning(f"[OVERPASS] Błąd składni na {server}: {payload['remark']}")
                        break

                    elements = payload.get("elements", [])
                    if elements:
                        logger.info(f"[OVERPASS] Sukces z {server}. Pobrano elementów: {len(elements)}")
                        return elements
                    break

                elif res.status_code == 429:
                    wait_time = attempt * 20
                    logger.warning(f"[OVERPASS] Rate Limit (429) na {server}. Pauza na {wait_time}s...")
                    await asyncio.sleep(wait_time)

                elif res.status_code in (403, 500, 502, 504):
                    logger.warning(f"[OVERPASS] Status {res.status_code} na {server}. Zmiana mirrora...")
                    break

            except Exception as e:
                logger.warning(f"[OVERPASS] Błąd sieciowy z {server}: {e}")
                await asyncio.sleep(3)

    return []


async def save_cycleways_to_postgis(elements: List[Dict[str, Any]]) -> int:
    records = []
    for el in elements:
        if el.get("type") == "way" and "geometry" in el:
            coords = el["geometry"]
            if len(coords) < 2:
                continue

            way_id = f"way/{el['id']}"
            tags = el.get("tags", {})
            maxspeed_val = tags.get("maxspeed", "")
            maxspeed_int = int(maxspeed_val) if maxspeed_val.isdigit() else None
            linestring_wkt = _coords_to_linestring_wkt(coords)

            records.append((
                way_id,
                tags.get("highway"),
                tags.get("cycleway"),
                tags.get("name"),
                maxspeed_int,
                json.dumps(tags, ensure_ascii=False),
                linestring_wkt,
            ))

    if not records:
        return 0

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.executemany("""
            INSERT INTO cycleway_edges (
                way_id, highway, cycleway, name, maxspeed, properties_json, geom
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, ST_SetSRID(ST_GeomFromText($7), 4326)
            )
            ON CONFLICT (way_id) DO UPDATE SET
                highway = EXCLUDED.highway,
                cycleway = EXCLUDED.cycleway,
                name = EXCLUDED.name,
                maxspeed = EXCLUDED.maxspeed,
                properties_json = EXCLUDED.properties_json,
                geom = EXCLUDED.geom,
                updated_at = CURRENT_TIMESTAMP;
        """, records)

    return len(records)


async def save_tram_tracks_to_postgis(elements: List[Dict[str, Any]]) -> int:
    records = []
    seen_ways = set()

    for el in elements:
        if el.get("type") == "way" and "geometry" in el:
            way_id = f"way/{el['id']}"
            if way_id in seen_ways:
                continue
            seen_ways.add(way_id)

            tags = el.get("tags", {})
            if tags.get("railway") != "tram" or tags.get("service") or tags.get("construction"):
                continue

            coords = el["geometry"]
            if len(coords) < 2:
                continue

            linestring_wkt = _coords_to_linestring_wkt(coords)
            records.append((
                way_id,
                json.dumps(tags, ensure_ascii=False),
                linestring_wkt,
            ))

    if not records:
        return 0

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM tram_edges;")
            await conn.executemany("""
                INSERT INTO tram_edges (way_id, properties_json, geom, length_m)
                VALUES (
                    $1, 
                    $2::jsonb, 
                    ST_SetSRID(ST_GeomFromText($3), 4326),
                    ROUND(ST_Length(ST_Transform(ST_SetSRID(ST_GeomFromText($3), 4326), 2180))::numeric, 1)
                );
            """, records)

    return len(records)


async def save_tram_platforms_to_postgis(elements: List[Dict[str, Any]]) -> int:
    records = []
    for el in elements:
        if el.get("type") == "node" and "lon" in el and "lat" in el:
            osm_id = f"node/{el['id']}"
            name = el.get("tags", {}).get("name") or "Przystanek"
            lon = float(el["lon"])
            lat = float(el["lat"])
            point_wkt = f"POINT({lon} {lat})"
            cluster = name.rsplit(" ", 1)[0] if name else name

            records.append((osm_id, name, cluster, point_wkt))

    if not records:
        return 0

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM tram_platforms;")
            await conn.executemany("""
                INSERT INTO tram_platforms (osm_id, name, cluster_name, geom, geom_2180)
                VALUES (
                    $1, 
                    $2, 
                    $3, 
                    ST_SetSRID(ST_GeomFromText($4), 4326),
                    ST_Transform(ST_SetSRID(ST_GeomFromText($4), 4326), 2180)
                )
                ON CONFLICT (osm_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    cluster_name = EXCLUDED.cluster_name,
                    geom = EXCLUDED.geom,
                    geom_2180 = EXCLUDED.geom_2180,
                    updated_at = CURRENT_TIMESTAMP;
            """, records)

    return len(records)


async def sync_cycleways_task(custom_bbox: Optional[str] = None):
    logger.info("[INFRA_GIS] Synchronizacja dróg rowerowych z OSM do PostGIS...")
    sanitized = _sanitize_bbox(custom_bbox) if custom_bbox else None
    targets = [sanitized] if sanitized else [t["bbox"] for t in CYCLEWAY_TILES]

    total = 0
    async with httpx.AsyncClient(timeout=120.0) as client:
        for bbox in targets:
            elements = await execute_overpass_query(build_cycleways_query(bbox), client)
            added = await save_cycleways_to_postgis(elements)
            total += added
            # Kluczowy odstęp zapobiegający blokadzie 502/429 na kolejnym kafelku
            await asyncio.sleep(6)

    logger.info(f"[INFRA_GIS] Zakończono trasy rowerowe. Zapisano/zaktualizowano łącznie: {total} segmentów.")


async def sync_tram_tracks_task(bbox: Optional[str] = None):
    clean_bbox = _sanitize_bbox(bbox)
    logger.info(f"[INFRA_GIS] Synchronizacja torowisk dla BBOX: {clean_bbox} do PostGIS...")
    async with httpx.AsyncClient(timeout=120.0) as client:
        elements = await execute_overpass_query(build_tram_tracks_query(clean_bbox), client)
        added = await save_tram_tracks_to_postgis(elements)
    logger.info(f"[INFRA_GIS] Zapisano torowisk do PostGIS: {added}")


async def sync_tram_platforms_task(bbox: Optional[str] = None):
    clean_bbox = _sanitize_bbox(bbox)
    logger.info("[INFRA_GIS] Synchronizacja peronów tramwajowych do PostGIS...")
    async with httpx.AsyncClient(timeout=90.0) as client:
        elements = await execute_overpass_query(build_tram_platforms_query(clean_bbox), client)
        added = await save_tram_platforms_to_postgis(elements)
    logger.info(f"[INFRA_GIS] Zapisano peronów do PostGIS: {added}")