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
async def get_tram_network(response: Response):
  """Zwraca geometrię torowisk tramwajowych."""
  with get_db_cursor(TRAM_DB_PATH) as cur:
    cur.execute(
        "SELECT way_id, properties_json, coordinates_json FROM tram_edges;"
    )
    rows = cur.fetchall()

  if not rows:
    raise HTTPException(
        status_code=404,
        detail="Baza torowisk jest pusta. Uruchom POST /network/tram/sync.",
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