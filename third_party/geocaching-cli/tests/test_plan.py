from __future__ import annotations

from geocaching_cli.coord import LatLon
from geocaching_cli.models import CacheRecord
from geocaching_cli.plan import nearest_neighbor


def test_nearest_neighbor_order() -> None:
    start = LatLon(0.0, 0.0)
    caches = [
        CacheRecord("GCFAR", "Far", latitude=0.0, longitude=3.0),
        CacheRecord("GCNR", "Near", latitude=0.0, longitude=0.2),
        CacheRecord("GCMID", "Mid", latitude=0.0, longitude=1.0),
    ]
    result = nearest_neighbor(caches, start)
    assert [leg.gc_code for leg in result.legs] == ["GCNR", "GCMID", "GCFAR"]
    assert result.total_m > 0
    assert result.legs[0].distance_m < result.legs[1].distance_m
    assert result.leftover == 0


def test_plan_limit_and_skip_missing_coords() -> None:
    start = LatLon(10.0, 10.0)
    caches = [
        CacheRecord("GC1", "One", latitude=10.1, longitude=10.0),
        CacheRecord("GC2", "Two", latitude=10.2, longitude=10.0),
        CacheRecord("GCX", "NoCoords"),
    ]
    result = nearest_neighbor(caches, start, limit=1)
    assert len(result.legs) == 1
    assert result.legs[0].gc_code == "GC1"
    assert result.leftover == 2
