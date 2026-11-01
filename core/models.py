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
    lon: float
    capacity: int = 0
    free_bikes: int = 0
    empty_slots: int = 0
    updated_at: Optional[str] = None

    @computed_field
    @property
    def total_docks(self) -> int:
        return self.capacity if self.capacity > 0 else (self.free_bikes + self.empty_slots)

    @computed_field
    @property
    def occupancy_pct(self) -> float:
        if self.total_docks > 0:
            return round((self.free_bikes / self.total_docks) * 100, 1)
        return 100.0 if self.free_bikes > 0 else 0.0

    def to_geojson_feature(self) -> Dict[str, Any]:
        return {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": {
                "id": self.id,
                "name": self.name,
                "free_bikes": self.free_bikes,
                "empty_slots": self.empty_slots,
                "total_docks": self.total_docks,
                "occupancy_pct": self.occupancy_pct,
                "updated_at": self.updated_at,
            },
        }


class StationHistoryEntry(BaseModel):
    timestamp: datetime
    bikes: int


class StationLeaderboardHub(BaseModel):
    id: str
    name: str
    lat: float
    lon: float
    free_bikes: int
    empty_slots: int
    total_docks: int
    occupancy_pct: float
    nearest_stop_name: Optional[str] = None
    nearest_cluster: Optional[str] = None
    dist_to_tram_m: Optional[float] = None
    tram_dwells_count: int = 0
    tram_avg_dwell_sec: float = 0.0
    dist_to_cycleway_m: Optional[float] = None
    safe_network_meters: Optional[float] = None
    hub_score: float


# ==========================================
# 2. MODELE TELEMETRII I POSTOJÓW TRAMWAJOWYCH
# ==========================================

class TramLiveVehicle(BaseModel):
    """Zmapowany tramwaj na torowisku (PostGIS EPSG:2180 -> WGS84)."""
    vehicle_number: str
    line: str
    brigade: str
    speed_kmh: float
    dist_to_track: float
    gps_time: datetime
    current_stop_osm_id: Optional[int] = None
    dwell_start_time: Optional[datetime] = None


class TramDwellEvent(BaseModel):
    """Zarejestrowane i zweryfikowane zdarzenie wymiany pasażerskiej."""
    vehicle_number: str
    line: str
    brigade: Optional[str] = None
    stop_name: str
    cluster_name: Optional[str] = None
    stop_osm_id: int
    arrival_time: datetime
    departure_time: datetime
    duration_sec: float = Field(ge=8.0, le=900.0)


class StopDwellStats(BaseModel):
    """Zagregowane statystyki postojowe per peron pod warstwę mapy."""
    stop_osm_id: int
    stop_name: str
    cluster_name: str
    lat: float
    lon: float
    avg_dwell_sec: float
    samples_count: int
    lines: List[Dict[str, Any]] = Field(default_factory=list)

    def to_geojson_feature(self) -> Dict[str, Any]:
        return {
            "type": "Feature",
            "id": self.stop_osm_id,
            "geometry": {"type": "Point", "coordinates": [self.lon, self.lat]},
            "properties": {
                "stop_name": self.stop_name,
                "cluster_name": self.cluster_name,
                "avg_dwell_sec": self.avg_dwell_sec,
                "samples_count": self.samples_count,
                "lines": self.lines,
            },
        }