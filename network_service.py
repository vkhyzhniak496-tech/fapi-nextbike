import json
from pathlib import Path
from typing import Optional
import time
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
import httpx

router = APIRouter(prefix="/network", tags=["Network GIS"])

RESOURCES_DIR = Path(__file__).parent / "resources"
GEOJSON_PATH = RESOURCES_DIR / "cycleways.geojson"

# Stabilne podziały na ćwiartki Warszawy (zapobiega błędom 429 i 504)
TILES = [
    {"name": "SW (Mokotów, Ochota, Ursynów)", "bbox": "52.09,20.85,52.23,21.05"},
    {"name": "SE (Praga Płd, Wilanów, Wawer)", "bbox": "52.09,21.05,52.23,21.27"},
    {"name": "NW (Wola, Bemowo, Bielany)",    "bbox": "52.23,20.85,52.37,21.05"},
    {"name": "NE (Śródmieście, Praga Płn)",   "bbox": "52.23,21.05,52.37,21.27"}
]

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter"
]

HEADERS = {
    "User-Agent": "WarsawBikeRouter/1.0 (GIS research project)",
    "Accept": "application/json"
}


class SyncCustomRequest(BaseModel):
    bbox: Optional[str] = None  # Opcjonalny własny wycinek (np. "52.18,21.02,52.22,21.08")


def build_tile_query(bbox: str) -> str:
    """Zoptymalizowane zapytanie wyciągające pełną sieć: DDR, pasy na jezdni, woonerfy i Tempo 30."""
    return f"""
    [out:json][timeout:90];
    (
      way["highway"="cycleway"]({bbox});
      way["highway"="path"]["bicycle"="designated"]({bbox});
      way["cycleway"~"^(lane|opposite_lane|track|share_busway)$"]({bbox});
      way["cycleway:both"~"^(lane|opposite_lane|track|share_busway)$"]({bbox});
      way["cycleway:left"~"^(lane|opposite_lane|track|share_busway)$"]({bbox});
      way["cycleway:right"~"^(lane|opposite_lane|track|share_busway)$"]({bbox});
      way["oneway:bicycle"="no"]({bbox});
      way["highway"="living_street"]({bbox});
      way["highway"="residential"]["maxspeed"="30"]({bbox});
      way["highway"="residential"]["zone:traffic"="PL:zone30"]({bbox});
    );
    out body geom;
    """


async def fetch_elements_from_overpass(bbox: str, client: httpx.AsyncClient) -> list:
    """Odpytuje mirrory Overpass z obsługą prób i fallbacku."""
    query = build_tile_query(bbox)
    for server in OVERPASS_SERVERS:
        for attempt in range(1, 3):
            try:
                res = await client.post(server, data={"data": query})
                if res.status_code == 200:
                    return res.json().get("elements", [])
                elif res.status_code == 429:
                    time.sleep(attempt * 4)
            except Exception:
                pass
    return []


async def sync_overpass_to_disk_task(custom_bbox: Optional[str] = None):
    """Zadanie w tle: pobiera żywe dane z Overpass, deduplikuje segmenty i trwale zapisuje GeoJSON na dysk."""
    print("Rozpoczęto synchronizację sieci rowerowej z Overpass API...")
    seen_ids = set()
    all_features = []

    async with httpx.AsyncClient(timeout=120.0, headers=HEADERS) as client:
        # Jeśli podano własny mały wycinek (np. dzielnicę), pobieramy tylko jego
        tasks_bboxes = [custom_bbox] if custom_bbox else [t["bbox"] for t in TILES]

        for bbox in tasks_bboxes:
            elements = await fetch_elements_from_overpass(bbox, client)
            for el in elements:
                way_id = el.get("id")
                if el.get("type") == "way" and "geometry" in el and way_id not in seen_ids:
                    seen_ids.add(way_id)
                    coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
                    all_features.append({
                        "type": "Feature",
                        "id": f"way/{way_id}",
                        "properties": el.get("tags", {}),
                        "geometry": {
                            "type": "LineString",
                            "coordinates": coords
                        }
                    })

    if all_features:
        geojson_output = {
            "type": "FeatureCollection",
            "generator": "overpass-live-sync",
            "total_ways": len(all_features),
            "features": all_features
        }

        RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
        GEOJSON_PATH.write_text(json.dumps(geojson_output, ensure_ascii=False), encoding="utf-8")
        print(f"Trwale zaktualizowano graf: {len(all_features)} segmentów zapisano do {GEOJSON_PATH}")
    else:
        print("Nie udało się pobrać żadnych nowych segmentów z Overpass.")


@router.get("/safe-cycleways")
async def get_safe_cycleways():
    """Błyskawicznie zwraca zaktualizowany plik z dysku serwera."""
    if not GEOJSON_PATH.exists():
        raise HTTPException(
            status_code=404,
            detail="Brak pliku cycleways.geojson. Wywołaj najpierw POST /network/safe-cycleways/sync"
        )
    return json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))


@router.post("/safe-cycleways/sync")
async def trigger_cycleways_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None
):
    """
    Wywolaj ten POST, aby pobrać żywy graf z OSM Overpass i trwale nadpisać plik na dysku.
    Działa w tle bez blokowania API.
    """
    custom_bbox = payload.bbox if payload else None
    background_tasks.add_task(sync_overpass_to_disk_task, custom_bbox)
    return {
        "status": "accepted",
        "message": "Pobieranie grafu z Overpass API uruchomione w tle. Po zakończeniu plik na dysku zostanie nadpisany.",
        "target": custom_bbox if custom_bbox else "Cała Warszawa (4 kafelki)"
    }