from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class LogRecord:
    log_id: str | None
    logged_at: str | None
    log_type: str | None
    finder: str | None
    text: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WaypointRecord:
    wpt_code: str | None
    name: str | None
    latitude: float | None
    longitude: float | None
    wpt_type: str | None
    comment: str | None
    gc_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AttributeRecord:
    attr_id: int | None
    name: str | None
    included: bool | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CacheRecord:
    gc_code: str
    name: str
    latitude: float | None = None
    longitude: float | None = None
    cache_type: str | None = None
    container: str | None = None
    difficulty: float | None = None
    terrain: float | None = None
    owner: str | None = None
    placed_at: str | None = None
    country: str | None = None
    state: str | None = None
    available: bool | None = None
    archived: bool | None = None
    short_description: str | None = None
    long_description: str | None = None
    encoded_hints: str | None = None
    url: str | None = None
    favorited: int | None = None
    source: str | None = None
    imported_at: str | None = None
    distance_m: float | None = None
    logs: list[LogRecord] = field(default_factory=list)
    waypoints: list[WaypointRecord] = field(default_factory=list)
    attributes: list[AttributeRecord] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.archived:
            return "archived"
        if self.available is False:
            return "disabled"
        if self.available:
            return "available"
        return "unknown"

    def to_dict(self, *, include_children: bool = True) -> dict[str, Any]:
        data = {
            "gc_code": self.gc_code,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "cache_type": self.cache_type,
            "container": self.container,
            "difficulty": self.difficulty,
            "terrain": self.terrain,
            "owner": self.owner,
            "placed_at": self.placed_at,
            "country": self.country,
            "state": self.state,
            "available": self.available,
            "archived": self.archived,
            "status": self.status,
            "short_description": self.short_description,
            "long_description": self.long_description,
            "encoded_hints": self.encoded_hints,
            "url": self.url,
            "favorited": self.favorited,
            "source": self.source,
            "imported_at": self.imported_at,
        }
        if self.distance_m is not None:
            data["distance_m"] = self.distance_m
            data["distance_km"] = round(self.distance_m / 1000.0, 3)
        if include_children:
            data["logs"] = [item.to_dict() for item in self.logs]
            data["waypoints"] = [item.to_dict() for item in self.waypoints]
            data["attributes"] = [item.to_dict() for item in self.attributes]
        return data


@dataclass
class ImportResult:
    path: str
    caches_new: int = 0
    caches_updated: int = 0
    waypoints: int = 0
    logs: int = 0
    attributes: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def caches_total(self) -> int:
        return self.caches_new + self.caches_updated
