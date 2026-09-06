import json
from pathlib import Path
from typing import Any, Dict, Optional

RESOURCES_DIR = Path(__file__).resolve().parent / "resources"
STATIONS_CACHE_PATH = RESOURCES_DIR / "stations_cache.json"

# Bufor RAM żyjący w storage
_MEM_STATIONS_CACHE: Optional[Dict[str, Any]] = None


def load_stations_cache() -> Optional[Dict[str, Any]]:
  if STATIONS_CACHE_PATH.exists():
    try:
      return json.loads(STATIONS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
      return None
  return None


def save_stations_cache(data: Dict[str, Any]) -> None:
  try:
    STATIONS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATIONS_CACHE_PATH.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
  except Exception:
    pass


def set_cached_stations(data: Dict[str, Any]) -> None:
  """Zapisuje dane do pamięci RAM serwera i zrzuca kopię na dysk."""
  global _MEM_STATIONS_CACHE
  _MEM_STATIONS_CACHE = data
  save_stations_cache(data)


def get_cached_stations() -> Optional[Dict[str, Any]]:
  """Zwraca dane z RAM-u, a jeśli puste (po starcie) - sięga po plik z dysku."""
  global _MEM_STATIONS_CACHE
  if _MEM_STATIONS_CACHE is not None:
    return _MEM_STATIONS_CACHE
  _MEM_STATIONS_CACHE = load_stations_cache()
  return _MEM_STATIONS_CACHE