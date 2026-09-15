from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, computed_field, Field


# ==========================================
# 1. MODELE VETURILO
# ==========================================

class Station(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    free_bikes: int = 0
    empty_slots: int = 0
    updated_at: Optional[str] = None

    @computed_field
    @property
    def total_docks(self) -> int:
        return self.free_bikes + self.empty_slots

    @computed_field
    @property
    def occupancy_pct(self) -> float:
        if self.total_docks > 0:
            return round((self.free_bikes / self.total_docks) * 100, 1)
        return 100.0 if self.free_bikes > 0 else 0.0

    def to_geojson_feature(self) -> Dict[str, Any]:
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lng, self.lat]},
            "properties": {
                "id": self.id,
                "name": self.name,
                "free_bikes": self.free_bikes,
                "empty_slots": self.empty_slots,
                "occupancy_pct": self.occupancy_pct,
                "updated_at": self.updated_at,
            },
        }


class StationHistoryEntry(BaseModel):
    datetime: datetime
    bikes: int


class SyncCustomRequest(BaseModel):
    bbox: Optional[str] = None


# ==========================================
# 2. MODELE TELEMETRII I POSTOJÓW TRAMWAJOWYCH
# ==========================================

class TramRawTelemetry(BaseModel):
    """Surowy odczyt pozycji tramwaju z API ZTM."""
    vehicle_number: str
    line: str
    brigade: str
    lat: float
    lon: float
    speed_kmh: float
    gps_time: datetime


class TramDwellEvent(BaseModel):
    """Pojedyncze zdarzenie postojowe lub przejazdu przez przystanek."""
    vehicle_number: str
    line: str
    brigade: Optional[str] = None
    stop_name: str
    cluster_name: str
    arrival_time: datetime
    departure_time: datetime
    duration_sec: float = Field(
        ge=0.0, description="Czas przebywania / wymiany pasażerskiej w sekundach"
    )
    min_speed_kmh: float
    min_dist_m: float
    pings_count: int


class StopDwellStats(BaseModel):
    """Statystyka postoju na przystanku pod API i mapę."""
    stop_name: str
    cluster_name: str
    lat: float
    lon: float
    median_dwell_sec: float
    avg_dwell_sec: float
    samples_count: int
    slow_passes_count: int = 0  # Przeloty na biegu (min_speed > 3 km/h)

    def to_geojson_feature(self) -> Dict[str, Any]:
        """Gotowy format pod warstwę punktową MapLibre / Leaflet."""
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": {
                "stop_name": self.stop_name,
                "cluster_name": self.cluster_name,
                "median_dwell_sec": self.median_dwell_sec,
                "avg_dwell_sec": self.avg_dwell_sec,
                "samples_count": self.samples_count,
                "slow_passes_count": self.slow_passes_count,
            },
        }


class LineDwellSummary(BaseModel):
    """Podsumowanie kursowania całej linii tramwajowej."""
    line: str
    total_dwells_recorded: int
    avg_line_dwell_sec: float
    stops: List[StopDwellStats]