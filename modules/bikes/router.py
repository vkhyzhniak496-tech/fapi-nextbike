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

@router.get("/analytics/station/{station_id}/chart.png")
async def get_station_chart_endpoint(station_id: str, name: str = "Stacja"):
    """
    Pobiera wyrenderowany w oddzielnym module wykres PNG i zwraca go do popupu MapLibre.
    """
    img_bytes = await generate_station_chart(station_id=station_id, station_name=name)
    if not img_bytes:
        raise HTTPException(status_code=404, detail="Brak danych do wygenerowania wykresu dla podanej stacji")

    return Response(
        content=img_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=30"}
    )

# --- 2. RANKING HUBÓW ---
@router.get("/bikes/leaderboard")
async def get_stations_leaderboard(top_n: int = 36):
    """
    Błyskawiczny ranking multimodalny oparty na zmaterializowanej 
    tabeli veturilo_station_infrastructure.
    """
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=HTTP_HEADERS) as client:
        res = await client.get(CITYBIKES_WARSAW_URL)
        if res.status_code != 200:
            raise HTTPException(status_code=502, detail="Błąd komunikacji z CityBikes")
        stations_raw = res.json().get("network", {}).get("stations", [])

    prepared = [
        {
            "station_id": str(st.get("id")),
            "free_bikes": int(st.get("free_bikes") or 0),
            "empty_slots": int(st.get("empty_slots") or 0),
        }
        for st in stations_raw
        if st.get("id")
    ]

    pool = get_pool()
    async with pool.acquire() as conn:
        # 1. Lekka aktualizacja wyłącznie liczby rowerów i wolnych stojaków
        if prepared:
            await conn.execute("""
                UPDATE veturilo_stations vs
                SET 
                    free_bikes = u.free_bikes,
                    empty_slots = u.empty_slots,
                    updated_at = CURRENT_TIMESTAMP
                FROM json_to_recordset($1::json) AS u(
                    station_id TEXT, free_bikes INTEGER, empty_slots INTEGER
                )
                WHERE vs.station_id = u.station_id;
            """, json.dumps(prepared))

        # 2. Błyskawiczny SELECT ze statycznego szkieletu i zdarzeń tramwajowych
        rows = await conn.fetch("""
            WITH live_dwell_stats AS (
                -- Zliczamy postoje zarejestrowane dla każdego peronu
                SELECT 
                    stop_osm_id,
                    COUNT(*) AS dwells_count,
                    ROUND(AVG(duration_sec)::numeric, 1) AS avg_dwell_sec
                FROM tram_dwell_events
                GROUP BY stop_osm_id
            ),
            combined AS (
                SELECT 
                    inf.station_id AS id,
                    inf.name,
                    inf.lat,
                    inf.lng,
                    vs.free_bikes,
                    vs.empty_slots,
                    (vs.free_bikes + vs.empty_slots) AS total_docks,
                    CASE 
                        WHEN (vs.free_bikes + vs.empty_slots) > 0 
                        THEN ROUND((vs.free_bikes::numeric / (vs.free_bikes + vs.empty_slots)) * 100, 1)
                        ELSE 0.0
                    END AS occupancy_pct,
                    inf.nearest_stop_name,
                    inf.nearest_cluster,
                    inf.dist_to_tram_m,
                    COALESCE(dw.dwells_count, 0) AS tram_dwells_count,
                    COALESCE(dw.avg_dwell_sec, 0.0) AS tram_avg_dwell_sec,
                    inf.dist_to_cycleway_m,
                    inf.safe_network_meters
                FROM veturilo_station_infrastructure inf
                JOIN veturilo_stations vs ON vs.station_id = inf.station_id
                LEFT JOIN live_dwell_stats dw ON dw.stop_osm_id = inf.nearest_stop_osm_id
                WHERE (vs.free_bikes + vs.empty_slots) > 0
            ),
            scored AS (
                SELECT 
                    *,
                    ROUND((
                        -- 1. Dostępność rowerów: max 35 pkt
                        LEAST(free_bikes, 25) * 1.4
                        +
                        -- 2. Tramwaj w pobliżu: max 40 pkt
                        CASE 
                            WHEN dist_to_tram_m IS NOT NULL 
                            THEN GREATEST(0.0, (250.0 - dist_to_tram_m) / 6.25)
                            ELSE 0.0
                        END
                        +
                        -- 3. Aktywność wymiany pasażerskiej tramwajów: max 25 pkt
                        LEAST(tram_dwells_count * 1.5, 25.0)
                        +
                        -- 4. Sieć tras rowerowych w promieniu 150m: max 30 pkt
                        LEAST(safe_network_meters / 15.0, 30.0)
                        -
                        -- 5. Kara za odcięcie od trasy (>25m do DDR):
                        CASE 
                            WHEN dist_to_cycleway_m > 25.0 
                            THEN LEAST(25.0, (dist_to_cycleway_m - 25.0))
                            ELSE 0.0
                        END
                    )::numeric, 1) AS hub_score
                FROM combined
            )
            SELECT * FROM scored
            ORDER BY hub_score DESC, free_bikes DESC
            LIMIT $1;
        """, top_n)

    return {
        "total_active_stations": len(prepared),
        "top_hubs": [dict(r) for r in rows],
    }