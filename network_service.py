import json
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
import httpx

router = APIRouter(prefix="/network", tags=["Network GIS"])

RESOURCES_DIR = Path(__file__).parent / "resources"
# Stabilne źródło zrzutu sieci tras
WARSAW_BIKE_NETWORK_URL = "https://raw.githubusercontent.com/vkhyzhniak496-tech/fapi-nextbike/main/resources/cycleways.geojson"


async def sync_cycleways_task():
    """Pobiera i nadpisuje plik bazy tras w tle bez obciążania pamięci."""
    output_path = RESOURCES_DIR / "cycleways.geojson"
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            res = await client.get(WARSAW_BIKE_NETWORK_URL)
            if res.status_code == 200:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(res.text, encoding="utf-8")
                print("Siatka tras zsynchronizowana pomyślnie.")
        except Exception as e:
            print(f"Błąd synchronizacji: {e}")


@router.get("/safe-cycleways")
async def get_safe_cycleways():
    """Błyskawiczne serwowanie lokalnego pliku GeoJSON."""
    geojson_file = RESOURCES_DIR / "cycleways.geojson"
    if not geojson_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Brak pliku cycleways.geojson. Wywołaj POST /network/safe-cycleways/sync"
        )
    return json.loads(geojson_file.read_text(encoding="utf-8"))


@router.post("/safe-cycleways/sync")
async def trigger_cycleways_sync(background_tasks: BackgroundTasks):
    """Odświeża plik tras w tle na żądanie."""
    background_tasks.add_task(sync_cycleways_task)
    return {"status": "accepted", "message": "Synchronizacja uruchomiona w tle."}