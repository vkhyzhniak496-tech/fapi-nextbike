import asyncio
import json
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
import httpx

router = APIRouter(prefix="/network", tags=["Network GIS"])

RESOURCES_DIR = Path(__file__).parent / "resources"
GEOJSON_PATH = RESOURCES_DIR / "cycleways.geojson"

TILES = [
    {"name": "SW (Mokotów, Ochota, Ursynów)", "bbox": "52.09,20.85,52.23,21.05"},
    {"name": "SE (Praga Płd, Wilanów, Wawer)", "bbox": "52.09,21.05,52.23,21.27"},
    {"name": "NW (Wola, Bemowo, Bielany)",    "bbox": "52.23,20.85,52.37,21.05"},
    {"name": "NE (Śródmieście, Praga Płn, Białołęka)", "bbox": "52.23,21.05,52.37,21.27"}
]

OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter"
]

HEADERS = {
    "User-Agent": "WarsawBikeRouter/1.0 (GIS research)",
    "Accept": "application/json"
}


class SyncCustomRequest(BaseModel):
    bbox: Optional[str] = None


def build_tile_query(bbox: str) -> str:
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


async def sync_overpass_to_disk_task(custom_bbox: Optional[str] = None):
    print("Rozpoczęto synchronizację sieci tras...")
    seen_ids = set()
    all_features = []

    # 1. Zachowaj trasy, które już mamy na dysku
    if GEOJSON_PATH.exists():
        try:
            cached = json.loads(GEOJSON_PATH.read_text(encoding="utf-8"))
            for f in cached.get("features", []):
                fid = f.get("id")
                if fid:
                    seen_ids.add(fid)
                    all_features.append(f)
            print(f"Załadowano {len(all_features)} istniejących tras z pliku.")
        except Exception:
            pass

    # 2. Pobierz kafelki i dołącz brakujące segmenty
    async with httpx.AsyncClient(timeout=120.0, headers=HEADERS) as client:
        targets = [custom_bbox] if custom_bbox else [t["bbox"] for t in TILES]
        
        for bbox in targets:
            elements = await fetch_tile_with_retry(bbox, client)
            added_count = 0
            for el in elements:
                way_id = f"way/{el.get('id')}"
                if el.get("type") == "way" and "geometry" in el and way_id not in seen_ids:
                    seen_ids.add(way_id)
                    coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
                    all_features.append({
                        "type": "Feature",
                        "id": way_id,
                        "properties": el.get("tags", {}),
                        "geometry": {"type": "LineString", "coordinates": coords}
                    })
                    added_count += 1
            print(f"Pobrano kafalek: dodano {added_count} nowych tras.")
            await asyncio.sleep(3)  # Odstęp zapobiegający blokadzie 429

    # 3. Trwały zapis scalonej bazy
    geojson_output = {
        "type": "FeatureCollection",
        "generator": "overpass-live-sync",
        "total_ways": len(all_features),
        "features": all_features
    }

    RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
    GEOJSON_PATH.write_text(json.dumps(geojson_output, ensure_ascii=False), encoding="utf-8")
    print(f"Zakończono synchronizację. Łącznie na dysku: {len(all_features)} tras.")


@router.get("/safe-cycleways")
async def get_safe_cycleways():
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
    custom_bbox = payload.bbox if payload else None
    background_tasks.add_task(sync_overpass_to_disk_task, custom_bbox)
    return {
        "status": "accepted",
        "message": "Trwa pobieranie i dołączanie brakujących tras do pliku."
    }