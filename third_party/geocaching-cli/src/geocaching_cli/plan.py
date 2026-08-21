"""Simple nearest-neighbor route planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from geocaching_cli.coord import LatLon, haversine_m, initial_bearing_deg
from geocaching_cli.errors import GeoCLIError
from geocaching_cli.models import CacheRecord


@dataclass
class PlanLeg:
    gc_code: str
    name: str
    latitude: float
    longitude: float
    distance_m: float
    bearing_deg: float
    cumulative_m: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "gc_code": self.gc_code,
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "distance_m": round(self.distance_m, 1),
            "bearing_deg": round(self.bearing_deg, 1),
            "cumulative_m": round(self.cumulative_m, 1),
            "distance_km": round(self.distance_m / 1000.0, 3),
            "cumulative_km": round(self.cumulative_m / 1000.0, 3),
        }


@dataclass
class PlanResult:
    start: LatLon
    legs: list[PlanLeg]
    leftover: int = 0

    @property
    def total_m(self) -> float:
        return self.legs[-1].cumulative_m if self.legs else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.to_dict(),
            "stops": len(self.legs),
            "total_m": round(self.total_m, 1),
            "total_km": round(self.total_m / 1000.0, 3),
            "leftover": self.leftover,
            "legs": [leg.to_dict() for leg in self.legs],
        }


def nearest_neighbor(caches: list[CacheRecord], start: LatLon, *, limit: int | None = None) -> PlanResult:
    remaining: list[CacheRecord] = [
        cache
        for cache in caches
        if cache.latitude is not None and cache.longitude is not None
    ]
    leftover = len(caches) - len(remaining)
    current = start
    legs: list[PlanLeg] = []
    cumulative = 0.0
    max_stops = len(remaining) if limit is None else max(0, limit)

    while remaining and len(legs) < max_stops:
        best_index = 0
        best_distance = float("inf")
        for index, cache in enumerate(remaining):
            point = LatLon(cache.latitude or 0.0, cache.longitude or 0.0)
            distance = haversine_m(current, point)
            if distance < best_distance:
                best_distance = distance
                best_index = index
        chosen = remaining.pop(best_index)
        dest = LatLon(chosen.latitude or 0.0, chosen.longitude or 0.0)
        cumulative += best_distance
        legs.append(
            PlanLeg(
                gc_code=chosen.gc_code,
                name=chosen.name,
                latitude=dest.latitude,
                longitude=dest.longitude,
                distance_m=best_distance,
                bearing_deg=initial_bearing_deg(current, dest),
                cumulative_m=cumulative,
            )
        )
        current = dest

    leftover += len(remaining)
    return PlanResult(start=start, legs=legs, leftover=leftover)


def require_start(start: LatLon | None, caches: list[CacheRecord]) -> LatLon:
    if start is not None:
        return start
    for cache in caches:
        if cache.latitude is not None and cache.longitude is not None:
            return LatLon(cache.latitude, cache.longitude)
    raise GeoCLIError("没有可用起点：请传入 --start 或先导入带坐标的缓存")
