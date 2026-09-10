from datetime import datetime
import math


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
  return 6371000 * 2 * math.asin(math.sqrt(a))


def calculate_speed_kmh(
    prev_lat: float,
    prev_lon: float,
    prev_time: datetime,
    new_lat: float,
    new_lon: float,
    new_time: datetime,
) -> float:
  """Oblicza prędkość w km/h z odfiltrowaniem anomalii GPS."""
  dt = (new_time - prev_time).total_seconds()
  if dt <= 0 or dt > 180:
    return 0.0

  distance = haversine_distance_meters(prev_lat, prev_lon, new_lat, new_lon)
  speed = (distance / dt) * 3.6

  # Szum GPS lub błędy pomiaru (> 90 km/h) zerujemy
  return round(speed, 1) if speed < 90.0 else 0.0