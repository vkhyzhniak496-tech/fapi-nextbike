from typing import Any, Dict
from fastapi import APIRouter, HTTPException, Response
import httpx
import json

from config import HTTP_HEADERS
from core.models import Station
from modules.bikes.storage import get_cached_stations
from modules.bikes.worker import CITYBIKES_WARSAW_URL
# Import dedykowanej funkcji rysującej z Twojego modułu wykresów:
from modules.bikes.charts import generate_station_chart
from core.database import get_pool
router = APIRouter(tags=["Veturilo Bikeshare & Analytics"])


# --- 1. GEOJSON DLA MAPY ---

@router.get("/bikes/citybikes/warsaw")
@router.get("/bikes/warsaw", include_in_schema=False)
async def get_warsaw_bikes_live():
    """Zwraca stan wszystkich stacji z pamięci podręcznej RAM pod warstwę MapLibre."""
    cached = get_cached_stations()
    if cached:
        return cached
    return {
        "type": "FeatureCollection",
        "system_name": "VETURILO 3.0",
        "total_stations": 0,
        "features": []
    }


# --- 2. RANKING HUBÓW ---



@router.get("/bikes/leaderboard")
async def get_stations_leaderboard(top_n: int = 36):
    """
    Aktualizuje stan stacji w PostGIS i wylicza ranking najlepszych
    węzłów przesiadkowych (Veturilo + bliskość peronu + natężenie postojów tramwajów).
    """
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Błąd komunikacji z CityBikes")
        stations_raw = res.json().get("network", {}).get("stations", [])

    prepared = []
    for st in stations_raw:
        lat = st.get("latitude")
        lng = st.get("longitude")
        if not (lat and lng):
            continue
        prepared.append({
            "station_id": str(st.get("id")),
            "name": str(st.get("name", "Stacja")).strip(),
            "lat": float(lat),
            "lng": float(lng),
            "free_bikes": int(st.get("free_bikes") or 0),
            "empty_slots": int(st.get("empty_slots") or 0),
        })

    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Hurtowy Upsert stacji do PostGIS z transformacją do EPSG:2180
        if prepared:
            await conn.execute("""
                INSERT INTO veturilo_stations (
                    station_id, name, lat, lng, free_bikes, empty_slots, geom_2180, updated_at
                )
                SELECT 
                    u.station_id, u.name, u.lat, u.lng, u.free_bikes, u.empty_slots,
                    ST_Transform(ST_SetSRID(ST_MakePoint(u.lng, u.lat), 4326), 2180),
                    CURRENT_TIMESTAMP
                FROM json_to_recordset($1::json) AS u(
                    station_id TEXT, name TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
                    free_bikes INTEGER, empty_slots INTEGER
                )
                ON CONFLICT (station_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    free_bikes = EXCLUDED.free_bikes,
                    empty_slots = EXCLUDED.empty_slots,
                    geom_2180 = EXCLUDED.geom_2180,
                    updated_at = CURRENT_TIMESTAMP;
            """, json.dumps(prepared))

        # 2. Zapytanie przestrzenne łączące stacje z peronami i postojami tramwajowymi
        rows = await conn.fetch("""
            WITH cluster_activity AS (
                SELECT 
                    cluster_name,
                    COUNT(*) AS dwells_count,
                    ROUND(AVG(duration_sec)::numeric, 1) AS avg_dwell_sec
                FROM tram_dwell_events
                GROUP BY cluster_name
            ),
            infra_match AS (
                -- Wyliczamy dostępność bezpiecznej infrastruktury w promieniu 150m od stacji
                SELECT 
                    vs.station_id,
                    -- Dystans do najbliższego bezpiecznego zjazdu
                    MIN(ST_Distance(vs.geom_2180, ST_Transform(ce.geom, 2180))) AS dist_to_infra_m,
                    -- Suma ważonej długości tras w buforze 150m
                    COALESCE(SUM(
                        CASE 
                            WHEN ce.properties_json->>'highway' = 'cycleway' THEN 1.0
                            WHEN ce.properties_json->>'highway' = 'living_street' THEN 0.8
                            WHEN ce.properties_json->>'cycleway' IN ('lane', 'opposite_lane') THEN 0.7
                            ELSE 0.5
                        END * ST_Length(ST_Intersection(ST_Transform(ce.geom, 2180), ST_Buffer(vs.geom_2180, 150.0)))
                    ), 0.0) AS safe_network_meters
                FROM veturilo_stations vs
                LEFT JOIN cycleway_edges ce 
                    ON ST_DWithin(vs.geom_2180, ST_Transform(ce.geom, 2180), 150.0)
                    -- Wykluczamy arterie i węzły, które już wcześniej odcięliśmy
                    AND ce.properties_json->>'highway' NOT IN ('motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary_link')
                GROUP BY vs.station_id
            ),
            spatial_match AS (
                SELECT 
                    vs.station_id AS id,
                    vs.name,
                    vs.lat,
                    vs.lng,
                    vs.free_bikes,
                    vs.empty_slots,
                    (vs.free_bikes + vs.empty_slots) AS total_docks,
                    CASE 
                        WHEN (vs.free_bikes + vs.empty_slots) > 0 
                        THEN ROUND((vs.free_bikes::numeric / (vs.free_bikes + vs.empty_slots)) * 100, 1)
                        ELSE 0.0
                    END AS occupancy_pct,
                    tp.name AS nearest_stop_name,
                    tp.cluster_name AS nearest_cluster,
                    ROUND(ST_Distance(vs.geom_2180, tp.geom_2180)::numeric, 0) AS dist_to_tram_m,
                    COALESCE(ca.dwells_count, 0) AS tram_dwells_count,
                    COALESCE(ca.avg_dwell_sec, 0.0) AS tram_avg_dwell_sec,
                    ROUND(im.dist_to_infra_m::numeric, 0) AS dist_to_cycleway_m,
                    ROUND(im.safe_network_meters::numeric, 0) AS safe_network_meters
                FROM veturilo_stations vs
                LEFT JOIN LATERAL (
                    SELECT name, cluster_name, geom_2180
                    FROM tram_platforms
                    WHERE ST_DWithin(vs.geom_2180, geom_2180, 250.0)
                    ORDER BY vs.geom_2180 <-> geom_2180
                    LIMIT 1
                ) tp ON TRUE
                LEFT JOIN cluster_activity ca ON ca.cluster_name = tp.cluster_name
                LEFT JOIN infra_match im ON im.station_id = vs.station_id
                WHERE vs.free_bikes > 0
            ),
            scored AS (
                SELECT 
                    *,
                    ROUND((
                        -- 1. Rowery: max 35 pkt (zmniejszona waga samej liczby rowerów)
                        LEAST(free_bikes, 25) * 1.4
                        +
                        -- 2. Bliskość peronu tramwajowego (0-40 pkt)
                        CASE 
                            WHEN dist_to_tram_m IS NOT NULL 
                            THEN GREATEST(0.0, (250.0 - dist_to_tram_m) / 6.25)
                            ELSE 0.0
                        END
                        +
                        -- 3. Aktywność tramwajów (0-25 pkt)
                        LEAST(tram_dwells_count * 1.2, 25.0)
                        +
                        -- 4. Bezpieczna infrastruktura rowerowa (0-30 pkt):
                        -- Gęstość sieci w promieniu 150m (np. 300m tras DDR = 30 pkt)
                        LEAST(safe_network_meters / 10.0, 30.0)
                        -
                        -- 5. Kara za odcięcie od drogi rowerowej (>30m bez bezpiecznego wjazdu):
                        CASE 
                            WHEN dist_to_cycleway_m IS NULL OR dist_to_cycleway_m > 30.0 
                            THEN LEAST(25.0, COALESCE(dist_to_cycleway_m - 30.0, 25.0))
                            ELSE 0.0
                        END
                    )::numeric, 1) AS hub_score
                FROM spatial_match
            )
            SELECT * FROM scored
            ORDER BY hub_score DESC, free_bikes DESC
            LIMIT $1;
        """, top_n)

    top_multimodal_hubs = [dict(r) for r in rows]

    return {
        "total_active_stations": len(prepared),
        "top_hubs": top_multimodal_hubs,
    }