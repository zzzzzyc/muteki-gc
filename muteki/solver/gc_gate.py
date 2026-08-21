"""Geocache coordinate acceptance gate.

Local shape, distance, and provenance checks produce an accepted candidate.
Only an exact ``digit_checksum`` match upgrades ``verified=True``. External
GeoCheck / Certitude receipts are out of scope. This module does not call
or wrap the CTF ``flag_ok`` gate.
"""

from __future__ import annotations

import math
import re
from typing import NamedTuple

from geocaching_cli.coord import (
    LatLon,
    digit_checksum,
    format_dmm,
    haversine_m,
    parse_coord,
)
from geocaching_cli.errors import CoordError

from muteki.models.solve_graph import Challenge

_SLOT_RE = re.compile(r"^(\d{3})(?:\s+|-)(\d{3})$")
_OFFSET_RE = re.compile(r"^([+-]\d{3})\s*([+-]\d{3})$")


class CoordVerdict(NamedTuple):
    accepted: bool
    verified: bool
    coord_text: str
    reason: str


def _rejected(reason: str) -> CoordVerdict:
    return CoordVerdict(False, False, "", reason)


def coord_ok(candidate: str, challenge: Challenge, raw_output: str) -> CoordVerdict:
    """Return a stable verdict for one coordinate candidate. Never raises."""
    try:
        return _coord_ok(candidate, challenge, raw_output)
    except CoordError:
        return _rejected("坐标无法解析或超出范围")
    except (TypeError, ValueError, OverflowError):
        return _rejected("坐标无法解析或超出范围")


def _coord_ok(candidate: str, challenge: Challenge, raw_output: str) -> CoordVerdict:
    text = (candidate or "").strip()
    if getattr(challenge, "mode", "") != "geocache":
        return _rejected("仅 geocache 模式可提交坐标")
    if not text:
        return _rejected("候选坐标为空")
    if text not in (raw_output or ""):
        return _rejected("候选未出现在真实命令输出中")

    posted_lat = getattr(challenge, "posted_lat", None)
    posted_lon = getattr(challenge, "posted_lon", None)
    if posted_lat is None or posted_lon is None:
        return _rejected("缺少 posted 坐标")
    posted = LatLon(float(posted_lat), float(posted_lon))
    posted.validate()

    slot = _SLOT_RE.fullmatch(text)
    offset = _OFFSET_RE.fullmatch(text)
    if slot is not None:
        final = _apply_slot(slot.group(1), slot.group(2), challenge.coord_skeleton or "")
    elif offset is not None:
        final = LatLon(
            posted.latitude + int(offset.group(1)) / 1000.0 / 60.0,
            posted.longitude + int(offset.group(2)) / 1000.0 / 60.0,
        )
    else:
        final = parse_coord(text)
    final.validate()
    if not math.isfinite(final.latitude) or not math.isfinite(final.longitude):
        return _rejected("坐标包含非有限数值")

    radius = float(getattr(challenge, "anchor_radius_m", 3200.0) or 3200.0)
    distance = haversine_m(posted, final)
    if distance > radius:
        return _rejected(
            f"距离 posted 约 {distance:.0f} 米，超出锚点半径 {radius:.0f} 米")

    coord_text = format_dmm(final)
    reason = f"本地形状与距离校验通过（距 posted {distance:.0f} 米）"
    verified = False
    expected = getattr(challenge, "digit_checksum", None)
    if expected is not None:
        actual = int(digit_checksum(coord_text)["digits_sum"])
        if actual == int(expected):
            verified = True
            reason = f"数字校验和匹配（{actual}）"
        else:
            reason = (
                f"数字校验和不匹配（得到 {actual}，期望 {int(expected)}），仍为候选"
            )
    return CoordVerdict(True, verified, coord_text, reason)


def _apply_slot(aaa: str, bbb: str, skeleton: str) -> LatLon:
    if skeleton.count("???") != 2:
        raise CoordError("coord_skeleton must contain exactly two ??? groups")
    filled = skeleton.replace("???", aaa, 1).replace("???", bbb, 1)
    return parse_coord(filled)
