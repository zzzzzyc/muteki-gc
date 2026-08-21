from __future__ import annotations

from pathlib import Path

from geocaching_cli.gpx import export_gpx, parse_gpx_bytes, parse_gpx_file


def test_parse_sample_groundspeak(sample_gpx: Path) -> None:
    caches, extras = parse_gpx_file(sample_gpx)
    codes = {c.gc_code for c in caches}
    assert codes == {"GC1A2B3", "GC2C3D4", "GC3E4F5", "GC4G5H6"}
    forbidden = next(c for c in caches if c.gc_code == "GC1A2B3")
    assert forbidden.name == "Forbidden City Cache"
    assert forbidden.cache_type == "Traditional Cache"
    assert forbidden.difficulty == 2
    assert forbidden.terrain == 2
    assert forbidden.available is True
    assert forbidden.archived is False
    assert forbidden.encoded_hints == "Look under the bench"
    assert len(forbidden.logs) == 2
    assert forbidden.logs[0].finder == "FinderOne"
    assert any(a.name == "Recommended for kids" for a in forbidden.attributes)

    archived = next(c for c in caches if c.gc_code == "GC4G5H6")
    assert archived.archived is True
    assert archived.available is False
    assert extras == []


def test_parse_additional_waypoints(sample_wpts: Path) -> None:
    caches, extras = parse_gpx_file(sample_wpts)
    assert caches == []
    assert {w.wpt_code for w in extras} == {"GC1A2B3-P", "GC3E4F5-1"}
    parking = next(w for w in extras if w.wpt_code == "GC1A2B3-P")
    assert parking.gc_code == "GC1A2B3"
    assert parking.wpt_type == "Parking Area"


def test_parse_gpx11_extensions(gpx11: Path) -> None:
    caches, extras = parse_gpx_file(gpx11)
    assert extras == []
    assert len(caches) == 1
    cache = caches[0]
    assert cache.gc_code == "GC9TEST"
    assert cache.name == "Berlin Test"
    assert cache.owner == "OwnerB"
    assert cache.container == "Micro"
    assert cache.difficulty == 1.5
    assert cache.country == "Germany"


def test_export_roundtrip(sample_gpx: Path) -> None:
    caches, _ = parse_gpx_file(sample_gpx)
    xml = export_gpx(caches, name="roundtrip")
    again, extras = parse_gpx_bytes(xml.encode("utf-8"), source="roundtrip")
    assert extras == []
    assert {c.gc_code for c in again} == {c.gc_code for c in caches}
    mystery = next(c for c in again if c.gc_code == "GC2C3D4")
    assert "Puzzle" in mystery.name
    assert mystery.difficulty == 3.5
