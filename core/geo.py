from datetime import datetime
import math
import re
from typing import Optional, Tuple
import numpy as np
import pandas as pd
from pyproj import Transformer


# Transformatory CRS (WGS84 <-> PUWG 1992 / EPSG:2180)
# always_xy=True oznacza kolejność: (lon, lat) -> (x, y)
_TRANS_TO_2180 = Transformer.from_crs("EPSG:4326", "EPSG:2180", always_xy=True)
_TRANS_TO_4326 = Transformer.from_crs("EPSG:2180", "EPSG:4326", always_xy=True)


# --- 1. Rzutowanie współrzędnych ---

def wgs84_to_epsg2180(lon: float, lat: float) -> Tuple[float, float]:
    """Konwertuje współrzędne geograficzne WGS84 na metryczny układ PUWG 1992 (EPSG:2180)."""
    x, y = _TRANS_TO_2180.transform(lon, lat)
    return float(x), float(y)


def epsg2180_to_wgs84(x: float, y: float) -> Tuple[float, float]:
    """Konwertuje metryczne współrzędne PUWG 1992 na WGS84 (lon, lat)."""
    lon, lat = _TRANS_TO_4326.transform(x, y)
    return float(lon), float(lat)


# --- 2. Dystans i Azymut (Geodezja / GIS) ---

def haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Oblicza odległość w metrach po wielkim kole między dwoma punktami."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


def haversine_vectorized(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    """Wektorowe wyliczanie odległości Haversine w metrach dla tablic NumPy / serii Pandas."""
    lat1_rad, lon1_rad = np.radians(lat1), np.radians(lon1)
    lat2_rad, lon2_rad = np.radians(lat2), np.radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = np.sin(dlat / 2.0) ** 2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2.0) ** 2
    return 6371000.0 * 2 * np.arcsin(np.sqrt(a))


def calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Oblicza azymut ruchu (kierunek geograficzny) w stopniach [0, 360).
    0° = Północ, 90° = Wschód, 180° = Południe, 270° = Zachód.
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)

    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return round(bearing, 1)


# --- 3. Telemetria i prędkości ---

def calculate_speed_kmh(
    prev_lat: float,
    prev_lon: float,
    prev_time: datetime,
    new_lat: float,
    new_lon: float,
    new_time: datetime,
    max_valid_gap_sec: float = 120.0,
    speed_cap_kmh: float = 75.0,
) -> float:
    """
    Oblicza prędkość w km/h z odfiltrowaniem anomalii GPS.
    Domyślny cap prędkości to 75 km/h (maksimum fizyczne warszawskich tramwajów).
    Luki czasowe > 120s oznaczają brak możliwości ciągłego wyliczenia prędkości.
    """
    dt = (new_time - prev_time).total_seconds()
    if dt <= 0.5 or dt > max_valid_gap_sec:
        return 0.0

    distance = haversine_distance_meters(prev_lat, prev_lon, new_lat, new_lon)
    speed = (distance / dt) * 3.6

    # Filtr szumu stacjonarnego GPS i anomalii nierealnych prędkości
    if speed > speed_cap_kmh:
        return 0.0

    return round(speed, 1)


# --- 4. Przetwarzanie przystanków i klastrów ---

def clean_stop_cluster_name(stop_name: Optional[str]) -> Optional[str]:
  """Odcina numer słupka peronowego, zwracając nazwę całego zespołu przystankowego.

  Przykład: 'Spacerowa 01' -> 'Spacerowa', 'Centrum 08' -> 'Centrum'.
  """
  if not isinstance(stop_name, str) or not stop_name.strip():
    return None
  return re.sub(r"\s+\d+$", "", stop_name).strip()