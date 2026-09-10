from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, computed_field


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