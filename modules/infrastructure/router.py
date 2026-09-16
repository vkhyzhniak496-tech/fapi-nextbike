import json
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, HTTPException, Response

from config import DEFAULT_WARSAW_BBOX
from core.database import CYCLEWAYS_DB_PATH, TRAM_DB_PATH, get_db_cursor
from core.models import SyncCustomRequest
from modules.infrastructure.overpass import (
    sync_cycleways_task,
    sync_tram_platforms_task,
    sync_tram_tracks_task,
)

router = APIRouter(prefix="/network", tags=["Infrastructure Network GIS"])

# --- TRASY ROWEROWE ---


@router.get("/safe-cycleways")
async def get_safe_cycleways(response: Response):
  """Zwraca sieć dróg rowerowych z SQLite z nagłówkiem Cache-Control na 7 dni."""
  with get_db_cursor(CYCLEWAYS_DB_PATH) as cur:
    cur.execute(
        "SELECT way_id, properties_json, coordinates_json FROM cycleway_edges;"
    )
    rows = cur.fetchall()

  if not rows:
    raise HTTPException(
        status_code=404,
        detail="Baza tras rowerowych jest pusta. Uruchom synchronizację POST.",
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
      "generator": "sqlite-cycleways-engine",
      "total_ways": len(features),
      "features": features,
  }


@router.post("/safe-cycleways/sync")
async def trigger_cycleways_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
  bbox = payload.bbox if payload and payload.bbox else None
  background_tasks.add_task(sync_cycleways_task, bbox)
  return {
      "status": "accepted",
      "message": "Synchronizacja dróg rowerowych rozpoczęta w tle.",
  }


# --- TOROWISKA I PRZYSTANKI TRAMWAJOWE ---


@router.get("/tram")
async def get_tram_network():
  """Zwraca wyłącznie eksploatowane torowiska tramwajowe."""
  with get_db_cursor(TRAM_DB_PATH) as cur:
    # 1. Musi być tramwajem ("railway": "tram")
    # 2. Wykluczamy budowy, plany, nieużywane tory i bocznice
    cur.execute("""
            SELECT way_id, coordinates_json, properties_json 
            FROM tram_edges 
            WHERE (properties_json LIKE '%"railway": "tram"%' OR properties_json LIKE '%"railway":"tram"%')
              AND properties_json NOT LIKE '%construction%'
              AND properties_json NOT LIKE '%proposed%'
              AND properties_json NOT LIKE '%abandoned%'
              AND properties_json NOT LIKE '%disused%'
              AND properties_json NOT LIKE '%"service": "yard"%'
              AND properties_json NOT LIKE '%"service":"yard"%'
              AND properties_json NOT LIKE '%"service": "siding"%'
              AND properties_json NOT LIKE '%"service":"siding"%';
        """)
    rows = cur.fetchall()

  features = []
  for r in rows:
    try:
      coords = json.loads(r["coordinates_json"])
      features.append({
          "type": "Feature",
          "id": r["way_id"],
          "properties": {},
          "geometry": {"type": "LineString", "coordinates": coords},
      })
    except Exception:
      continue

  return {"type": "FeatureCollection", "features": features}




@router.post("/tram/sync")
async def trigger_tram_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
  bbox = (
      payload.bbox.strip()
      if payload and payload.bbox and payload.bbox.strip() not in ("", "string")
      else DEFAULT_WARSAW_BBOX
  )
  background_tasks.add_task(sync_tram_tracks_task, bbox)
  return {
      "status": "accepted",
      "message": "Synchronizacja torowisk uruchomiona w tle.",
      "target": bbox,
  }


@router.get("/tram/platforms")
async def get_tram_platforms():
  """Zwraca punkty słupków tramwajowych."""
  with get_db_cursor(TRAM_DB_PATH) as cur:
    cur.execute("SELECT osm_id, name, coordinates_json FROM tram_platforms;")
    rows = cur.fetchall()

  if not rows:
    raise HTTPException(
        status_code=404,
        detail="Baza peronów jest pusta. Uruchom POST"
        " /network/tram/platforms/sync.",
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


@router.post("/tram/platforms/sync")
async def trigger_platforms_sync(
    background_tasks: BackgroundTasks,
    payload: Optional[SyncCustomRequest] = None,
):
  bbox = (
      payload.bbox.strip()
      if payload and payload.bbox and payload.bbox.strip() not in ("", "string")
      else DEFAULT_WARSAW_BBOX
  )
  background_tasks.add_task(sync_tram_platforms_task, bbox)
  return {
      "status": "accepted",
      "message": "Synchronizacja przystanków uruchomiona w tle.",
      "target": bbox,
  }