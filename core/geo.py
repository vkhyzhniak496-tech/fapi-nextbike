from datetime import datetime
import math
import re
from typing import Optional


def haversine_distance_meters(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Oblicza odległość w metrach po sferoidzie między dwoma punktami GPS."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


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
    Oblicza prędkość różnicową w km/h z odfiltrowaniem anomalii GPS.
    Wartości powyżej 75 km/h oraz luki > 120s są zerowane.
    """
    dt = (new_time - prev_time).total_seconds()
    if dt <= 0.5 or dt > max_valid_gap_sec:
        return 0.0

    distance = haversine_distance_meters(prev_lat, prev_lon, new_lat, new_lon)
    speed = (distance / dt) * 3.6

    if speed > speed_cap_kmh:
        return 0.0

    return round(speed, 1)


def clean_stop_cluster_name(stop_name: Optional[str]) -> Optional[str]:
    """Odcina numer słupka peronowego, zwracając nazwę całego zespołu przystankowego."""
    if not isinstance(stop_name, str) or not stop_name.strip():
        return None
    return re.sub(r"\s+\d+$", "", stop_name).strip()