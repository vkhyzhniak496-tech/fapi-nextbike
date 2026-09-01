from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

WARSAW_TZ = ZoneInfo("Europe/Warsaw")
RESOURCES_DIR = Path(__file__).parent / "resources"
HISTORY_FILE = RESOURCES_DIR / "history_cache.json"
STATIONS_FILE = RESOURCES_DIR / "stations_cache.json"


def load_history() -> tuple[Dict[str, str], Dict[str, List[Dict[str, Any]]]]:
    """Wczytuje metadane i bufor historii z dysku, parsując znaczniki czasu ISO do obiektów datetime."""
    metadata: Dict[str, str] = {}
    history_buffer: Dict[str, List[Dict[str, Any]]] = {}

    if not HISTORY_FILE.exists():
        logger.info("Brak pliku history_cache.json – start z pustym buforem.")
        return metadata, history_buffer

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        metadata = data.get("metadata", {})
        raw_buffer = data.get("buffer", {})

        for s_id, entries in raw_buffer.items():
            parsed_entries = []
            for entry in entries:
                try:
                    dt = datetime.fromisoformat(entry["datetime"])
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=WARSAW_TZ)
                    parsed_entries.append({
                        "datetime": dt,
                        "bikes": int(entry["bikes"])
                    })
                except Exception:
                    continue
            if parsed_entries:
                history_buffer[s_id] = parsed_entries

        logger.info(f"Wczytano historię dla {len(history_buffer)} stacji.")
    except Exception as e:
        logger.error(f"Błąd podczas wczytywania history_cache.json: {e}")

    return metadata, history_buffer


def save_history(metadata: Dict[str, str], history_buffer: Dict[str, List[Dict[str, Any]]]) -> None:
    """Serializuje obiekty datetime do ISO i zrzuca bufor do pliku JSON."""
    try:
        RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "metadata": metadata,
            "buffer": {
                s_id: [
                    {
                        "datetime": entry["datetime"].isoformat() 
                        if isinstance(entry["datetime"], datetime) 
                        else str(entry["datetime"]),
                        "bikes": entry["bikes"]
                    }
                    for entry in entries
                ]
                for s_id, entries in history_buffer.items()
            }
        }
        HISTORY_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.info(f"Zapisano historię {len(history_buffer)} stacji na dysku.")
    except Exception as e:
        logger.error(f"Błąd zapisu do history_cache.json: {e}")


def load_stations_cache() -> Dict[str, Any] | None:
    """Wczytuje ostatnio zapisaną kolekcję stacji."""
    if not STATIONS_FILE.exists():
        return None
    try:
        return json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Błąd odczytu stations_cache.json: {e}")
        return None


def save_stations_cache(data: Dict[str, Any]) -> None:
    """Zapisuje bieżący stan stacji do pliku."""
    try:
        RESOURCES_DIR.mkdir(parents=True, exist_ok=True)
        STATIONS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"Błąd zapisu do stations_cache.json: {e}")