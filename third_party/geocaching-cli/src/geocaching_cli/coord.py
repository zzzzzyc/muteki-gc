"""Coordinate parsing and puzzle-oriented helpers.

Supported input families:
- DD:  ``40.6892,-74.0445`` / ``N 40.6892 W 74.0445``
- DMM: ``N 40 41.352 W 074 02.670`` (common geocaching form)
- DMS: ``N 40 41 21.12 W 074 02 40.20`` / ``40°41'21.12"N, 74°02'40.20"W``
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from geocaching_cli.errors import CoordError

EARTH_RADIUS_M = 6371008.8
TOKEN_RE = re.compile(r"[NSEW]|[+-]?\d+(?:\.\d+)?", re.IGNORECASE)

_PUNCT_TRANSLATION = str.maketrans(
    {
        "°": " ",
        "º": " ",
        "'": " ",
        "′": " ",
        "’": " ",
        "`": " ",
        '"': " ",
        "″": " ",
        "”": " ",
        "“": " ",
        ":": " ",
        ",": " ",
        ";": " ",
        "/": " ",
        "\t": " ",
        "\n": " ",
    }
)


@dataclass(frozen=True)
class LatLon:
    latitude: float
    longitude: float

    def validate(self) -> None:
        if not math.isfinite(self.latitude) or not math.isfinite(self.longitude):
            raise CoordError("坐标包含非有限数值")
        if not -90.0 <= self.latitude <= 90.0:
            raise CoordError(f"纬度超出范围: {self.latitude}")
        if not -180.0 <= self.longitude <= 180.0:
            raise CoordError(f"经度超出范围: {self.longitude}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "dd": format_dd(self),
            "dmm": format_dmm(self),
            "dms": format_dms(self),
        }


def normalize_coord_text(text: str) -> str:
    cleaned = text.strip().translate(_PUNCT_TRANSLATION)
    return re.sub(r"\s+", " ", cleaned).strip()


def _numbers_to_decimal(nums: list[float]) -> float:
    if not nums:
        raise CoordError("坐标缺少数值")
    sign = -1.0 if nums[0] < 0 else 1.0
    values = [abs(n) for n in nums]
    if len(values) == 1:
        value = values[0]
    elif len(values) == 2:
        degrees, minutes = values
        if minutes >= 60:
            raise CoordError(f"分超出范围: {minutes}")
        value = degrees + minutes / 60.0
    elif len(values) == 3:
        degrees, minutes, seconds = values
        if minutes >= 60:
            raise CoordError(f"分超出范围: {minutes}")
        if seconds >= 60:
            raise CoordError(f"秒超出范围: {seconds}")
        value = degrees + minutes / 60.0 + seconds / 3600.0
    else:
        raise CoordError(f"单个坐标的数值过多: {nums}")
    return sign * value


def _parse_signed_pair(tokens: list[str]) -> LatLon:
    nums = [float(tok) for tok in tokens]
    if len(nums) == 2:
        lat, lon = nums
    elif len(nums) == 4:
        lat = _numbers_to_decimal(nums[0:2])
        lon = _numbers_to_decimal(nums[2:4])
    elif len(nums) == 6:
        lat = _numbers_to_decimal(nums[0:3])
        lon = _numbers_to_decimal(nums[3:6])
    else:
        raise CoordError(f"无法将 {len(nums)} 个数字拆成一对坐标")
    point = LatLon(lat, lon)
    point.validate()
    return point


def _parse_with_hemisphere(tokens: list[str]) -> LatLon:
    pairs: list[tuple[str, list[float]]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.upper() in "NSEW":
            index += 1
            nums: list[float] = []
            while index < len(tokens) and tokens[index].upper() not in "NSEW":
                nums.append(float(tokens[index]))
                index += 1
            if not nums:
                raise CoordError(f"半球标记 {token} 后面没有数值")
            pairs.append((token.upper(), nums))
            continue
        nums = []
        while index < len(tokens) and tokens[index].upper() not in "NSEW":
            nums.append(float(tokens[index]))
            index += 1
        if index >= len(tokens) or tokens[index].upper() not in "NSEW":
            raise CoordError("带半球坐标的格式无法识别")
        pairs.append((tokens[index].upper(), nums))
        index += 1

    if len(pairs) != 2:
        raise CoordError(f"需要恰好两个半球坐标，实际得到 {len(pairs)} 个")

    latitude: float | None = None
    longitude: float | None = None
    for hem, nums in pairs:
        value = abs(_numbers_to_decimal(nums))
        if hem in "NS":
            latitude = value if hem == "N" else -value
        elif hem in "EW":
            longitude = value if hem == "E" else -value
    if latitude is None or longitude is None:
        raise CoordError("缺少纬度或经度半球（需要 N/S 与 E/W）")
    point = LatLon(latitude, longitude)
    point.validate()
    return point


def parse_coord(text: str) -> LatLon:
    if not text or not str(text).strip():
        raise CoordError("坐标字符串为空")
    tokens = TOKEN_RE.findall(normalize_coord_text(text))
    if len(tokens) < 2:
        raise CoordError(f"无法从输入中解析坐标: {text!r}")
    if any(tok.upper() in "NSEW" for tok in tokens):
        return _parse_with_hemisphere(tokens)
    return _parse_signed_pair(tokens)


def format_dd(point: LatLon, precision: int = 6) -> str:
    return f"{point.latitude:.{precision}f},{point.longitude:.{precision}f}"


def format_dmm(point: LatLon) -> str:
    def one(value: float, north_south: bool) -> str:
        positive = "N" if north_south else "E"
        negative = "S" if north_south else "W"
        hem = positive if value >= 0 else negative
        absolute = abs(value)
        degrees = int(absolute)
        minutes = (absolute - degrees) * 60.0
        if round(minutes, 3) >= 60.0:
            degrees += 1
            minutes = 0.0
        return f"{hem} {degrees} {minutes:06.3f}"

    return f"{one(point.latitude, True)} {one(point.longitude, False)}"


def format_dms(point: LatLon) -> str:
    def one(value: float, north_south: bool) -> str:
        positive = "N" if north_south else "E"
        negative = "S" if north_south else "W"
        hem = positive if value >= 0 else negative
        absolute = abs(value)
        degrees = int(absolute)
        minutes_full = (absolute - degrees) * 60.0
        minutes = int(minutes_full)
        seconds = (minutes_full - minutes) * 60.0
        if round(seconds, 2) >= 60.0:
            seconds = 0.0
            minutes += 1
        if minutes >= 60:
            minutes = 0
            degrees += 1
        return f"{hem} {degrees} {minutes} {seconds:05.2f}"

    return f"{one(point.latitude, True)} {one(point.longitude, False)}"


def format_coord(point: LatLon, fmt: str = "dd") -> str:
    key = fmt.lower().strip()
    if key in {"dd", "decimal"}:
        return format_dd(point)
    if key in {"dmm", "min", "minutes"}:
        return format_dmm(point)
    if key in {"dms", "sec", "seconds"}:
        return format_dms(point)
    raise CoordError(f"未知坐标格式: {fmt}（支持 dd / dmm / dms）")


def haversine_m(a: LatLon, b: LatLon) -> float:
    phi1, phi2 = math.radians(a.latitude), math.radians(b.latitude)
    d_phi = phi2 - phi1
    d_lambda = math.radians(b.longitude - a.longitude)
    hav = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.atan2(math.sqrt(hav), math.sqrt(max(0.0, 1.0 - hav)))


def project(origin: LatLon, bearing_deg: float, distance_m: float) -> LatLon:
    if distance_m < 0:
        raise CoordError("投影距离必须 >= 0")
    if not math.isfinite(bearing_deg) or not math.isfinite(distance_m):
        raise CoordError("方位或距离不是有限数值")
    phi1 = math.radians(origin.latitude)
    lambda1 = math.radians(origin.longitude)
    theta = math.radians(bearing_deg)
    delta = distance_m / EARTH_RADIUS_M
    phi2 = math.asin(
        math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    )
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lon = (math.degrees(lambda2) + 540.0) % 360.0 - 180.0
    point = LatLon(math.degrees(phi2), lon)
    point.validate()
    return point


def midpoint(a: LatLon, b: LatLon) -> LatLon:
    phi1, lambda1 = math.radians(a.latitude), math.radians(a.longitude)
    phi2 = math.radians(b.latitude)
    d_lambda = math.radians(b.longitude - a.longitude)
    bx = math.cos(phi2) * math.cos(d_lambda)
    by = math.cos(phi2) * math.sin(d_lambda)
    phi3 = math.atan2(
        math.sin(phi1) + math.sin(phi2),
        math.sqrt((math.cos(phi1) + bx) ** 2 + by**2),
    )
    lambda3 = lambda1 + math.atan2(by, math.cos(phi1) + bx)
    lon = (math.degrees(lambda3) + 540.0) % 360.0 - 180.0
    point = LatLon(math.degrees(phi3), lon)
    point.validate()
    return point


def initial_bearing_deg(a: LatLon, b: LatLon) -> float:
    phi1, phi2 = math.radians(a.latitude), math.radians(b.latitude)
    d_lambda = math.radians(b.longitude - a.longitude)
    y = math.sin(d_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360.0) % 360.0


def digit_checksum(text: str) -> dict[str, Any]:
    digits = [int(ch) for ch in text if ch.isdigit()]
    letters = [ch for ch in text if ch.isalpha()]
    total = sum(digits)
    if total == 0:
        digital_root = 0
    else:
        digital_root = total % 9 or 9
    return {
        "text": text,
        "digits": digits,
        "digit_count": len(digits),
        "digits_sum": total,
        "digital_root": digital_root,
        "letter_count": len(letters),
    }
