from __future__ import annotations

import math

import pytest

from geocaching_cli.coord import (
    LatLon,
    digit_checksum,
    format_dd,
    format_dmm,
    format_dms,
    haversine_m,
    midpoint,
    parse_coord,
    project,
)
from geocaching_cli.errors import CoordError

LIBERTY = LatLon(40.6892, -74.0445)


@pytest.mark.parametrize(
    "text",
    [
        "40.6892,-74.0445",
        "40.6892, -74.0445",
        "N 40.6892 W 74.0445",
        "40.6892 N 74.0445 W",
        "N 40 41.352 W 074 02.670",
        "N 40° 41.352' W 074° 02.670'",
        "N 40 41 21.12 W 074 02 40.20",
        "40°41'21.12\"N, 74°02'40.20\"W",
    ],
)
def test_parse_statue_of_liberty(text: str) -> None:
    point = parse_coord(text)
    assert point.latitude == pytest.approx(LIBERTY.latitude, abs=1e-4)
    assert point.longitude == pytest.approx(LIBERTY.longitude, abs=1e-4)


def test_parse_beijing_dd() -> None:
    point = parse_coord("39.9042,116.4074")
    assert point.latitude == pytest.approx(39.9042)
    assert point.longitude == pytest.approx(116.4074)


def test_roundtrip_dmm_dms() -> None:
    original = parse_coord("39.9042,116.4074")
    again_dmm = parse_coord(format_dmm(original))
    again_dms = parse_coord(format_dms(original))
    assert again_dmm.latitude == pytest.approx(original.latitude, abs=1e-4)
    assert again_dms.longitude == pytest.approx(original.longitude, abs=1e-4)
    assert "," in format_dd(original)


def test_invalid_empty() -> None:
    with pytest.raises(CoordError):
        parse_coord("   ")


def test_invalid_latitude() -> None:
    with pytest.raises(CoordError):
        parse_coord("95.0,10.0")


def test_project_north_from_equator() -> None:
    dest = project(LatLon(0.0, 0.0), 0.0, 1000.0)
    assert dest.latitude == pytest.approx(1000.0 / 6371008.8 * 180 / math.pi, abs=1e-6)
    assert dest.longitude == pytest.approx(0.0, abs=1e-6)


def test_project_east_from_equator() -> None:
    dest = project(LatLon(0.0, 0.0), 90.0, 1000.0)
    assert dest.latitude == pytest.approx(0.0, abs=1e-6)
    assert dest.longitude > 0


def test_midpoint_and_distance() -> None:
    a = LatLon(0.0, 0.0)
    b = LatLon(0.0, 2.0)
    mid = midpoint(a, b)
    assert mid.latitude == pytest.approx(0.0, abs=1e-6)
    assert mid.longitude == pytest.approx(1.0, abs=1e-3)
    meters = haversine_m(a, b)
    assert meters == pytest.approx(222390.0, rel=0.02)


def test_digit_checksum() -> None:
    result = digit_checksum("N 39 54.252 E 116 24.444")
    assert result["digits_sum"] == 3 + 9 + 5 + 4 + 2 + 5 + 2 + 1 + 1 + 6 + 2 + 4 + 4 + 4 + 4
    assert result["digital_root"] == (result["digits_sum"] % 9 or 9)
    assert result["letter_count"] == 2
    assert result["digit_count"] == 15
