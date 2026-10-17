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
            way_id = el["id"]
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
            line_name = tags.get("name") or tags.get("ref") or f"Way/{way_id}"
            records.append((line_name, linestring_wkt))

    if not records:
        return 0

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("TRUNCATE TABLE tram_tracks RESTART IDENTITY CASCADE;")
            await conn.executemany("""
                INSERT INTO tram_tracks (line_name, geom)
                VALUES (
                    $1, 
                    ST_Transform(ST_SetSRID(ST_GeomFromText($2), 4326), 2180)
                );
            """, records)

    return len(records)

async def save_tram_platforms_to_postgis(elements: List[Dict[str, Any]]) -> int:
    raw_nodes = []
    for el in elements:
        if el.get("type") == "node" and "lon" in el and "lat" in el:
            platform_id = str(el["id"])
            full_name = el.get("tags", {}).get("name") or "Przystanek"
            parts = full_name.rsplit(" ", 1)
            stop_name = parts[0]
            stop_nr = parts[1] if len(parts) > 1 and parts[1].isdigit() else "01"
            
            lon = float(el["lon"])
            lat = float(el["lat"])
            point_wkt = f"POINT({lon} {lat})"
            raw_nodes.append((platform_id, stop_name, stop_nr, point_wkt))

    if not raw_nodes:
        return 0

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("TRUNCATE TABLE tram_platforms CASCADE;")
            # Zuordnung zum nächstgelegenen Gleis und Berechnung des Bahnsteigintervalls (~40m Länge)
            await conn.executemany("""
                WITH point_geom AS (
                    SELECT 
                        $1::varchar AS p_id,
                        $2::text AS s_name,
                        $3::varchar AS s_nr,
                        ST_Transform(ST_SetSRID(ST_GeomFromText($4), 4326), 2180) AS geom_2180
                ),
                matched_track AS (
                    SELECT 
                        p.p_id,
                        p.s_name,
                        p.s_nr,
                        p.geom_2180,
                        t.track_id,
                        ST_LineLocatePoint(t.geom, p.geom_2180) AS frac,
                        (40.0 / GREATEST(ST_Length(t.geom), 1.0)) AS delta_frac
                    FROM point_geom p
                    CROSS JOIN LATERAL (
                        SELECT track_id, geom 
                        FROM tram_tracks 
                        ORDER BY geom <-> p.geom_2180 
                        LIMIT 1
                    ) t
                    WHERE ST_DWithin(t.geom, p.geom_2180, 25.0)
                )
                INSERT INTO tram_platforms (
                    platform_id, stop_name, stop_nr, track_id, 
                    track_fraction_start, track_fraction_end, geom
                )
                SELECT 
                    p_id,
                    s_name,
                    s_nr,
                    track_id,
                    GREATEST(0.0, frac - (delta_frac / 2.0)),
                    LEAST(1.0, frac + (delta_frac / 2.0)),
                    geom_2180
                FROM matched_track
                ON CONFLICT (platform_id) DO NOTHING;
            """, raw_nodes)

    return len(raw_nodes)


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