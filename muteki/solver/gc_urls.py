"""Exact-host HTTPS checker URL + GC-code helpers.

No import from the external geocaching-cli package. Host matching is exact
(no suffix tricks). Used by listing bootstrap and Web start validation.
"""

from __future__ import annotations

import html
import math
import re
from urllib.parse import urlparse

from muteki.vendor.geocaching_cli.coord import LatLon
from muteki.vendor.geocaching_cli.errors import CoordError


GC_CODE_RE = re.compile(r"^GC[A-Z0-9]+$")

CHECKER_HOSTS = frozenset({
    "geocheck.org",
    "www.geocheck.org",
    "geotjek.dk",
    "www.geotjek.dk",
    "certitude.geocaching.com",
    "www.certitude.geocaching.com",
})

_HREF_RE = re.compile(r"""href\s*=\s*(['"])(.*?)\1""", re.IGNORECASE)
_BARE_URL_RE = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)

_MAX_CHECKSUM = 9999
_MAX_RADIUS_M = 3200.0


class GeocacheFieldError(ValueError):
    """Invalid geocache fields on a start/parse body. Fail closed, never CTF."""


def normalize_gc_code(raw: object) -> str:
    return str(raw or "").strip().upper()


def require_gc_code(raw: object) -> str:
    code = normalize_gc_code(raw)
    if not GC_CODE_RE.fullmatch(code):
        raise GeocacheFieldError("invalid geocache gc_code")
    return code


def validate_checker_url(raw: object, *, required: bool = False) -> str:
    text = str(raw or "").strip()
    if not text:
        if required:
            raise GeocacheFieldError("invalid geocache geocheck_url")
        return ""
    if _looks_like_checker_url(text):
        return text
    raise GeocacheFieldError("invalid geocache geocheck_url")


def _looks_like_checker_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower()
    if host not in CHECKER_HOSTS:
        return False
    if parsed.path.lower().startswith("javascript:") or parsed.path.lower().startswith("data:"):
        return False
    return True


def extract_checker_url(html_text: str) -> str:
    """First exact HTTPS GeoCheck / GeoTjek / Certitude URL in listing HTML."""
    if not html_text:
        return ""
    candidates: list[str] = []
    for _, href in _HREF_RE.findall(html_text):
        candidates.append(href)
    candidates.extend(_BARE_URL_RE.findall(html_text))
    for raw in candidates:
        decoded = html.unescape(raw).strip()
        lower = decoded.lower()
        if lower.startswith("javascript:") or lower.startswith("data:"):
            continue
        if _looks_like_checker_url(decoded):
            return decoded
    return ""


def validate_posted(lat: object, lon: object) -> tuple[float | None, float | None]:
    if lat is None and lon is None:
        return None, None
    if lat is None or lon is None:
        raise GeocacheFieldError("invalid geocache posted coordinates")
    try:
        plat = float(lat)
        plon = float(lon)
    except (TypeError, ValueError) as exc:
        raise GeocacheFieldError("invalid geocache posted coordinates") from exc
    if not math.isfinite(plat) or not math.isfinite(plon):
        raise GeocacheFieldError("invalid geocache posted coordinates")
    try:
        LatLon(plat, plon).validate()
    except CoordError as exc:
        raise GeocacheFieldError("invalid geocache posted coordinates") from exc
    return plat, plon


def validate_radius_m(raw: object, *, default: float = 3200.0) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise GeocacheFieldError("invalid geocache anchor_radius_m") from exc
    if not math.isfinite(value) or value <= 0 or value > _MAX_RADIUS_M:
        raise GeocacheFieldError("invalid geocache anchor_radius_m")
    return value


def validate_digit_checksum(raw: object) -> int | None:
    if raw is None or raw == "":
        return None
    if type(raw) is bool:
        raise GeocacheFieldError("invalid geocache digit_checksum")
    if type(raw) is float:
        raise GeocacheFieldError("invalid geocache digit_checksum")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise GeocacheFieldError("invalid geocache digit_checksum") from exc
    if type(raw) is str and (not raw.strip().lstrip("-").isdigit() or "." in raw):
        raise GeocacheFieldError("invalid geocache digit_checksum")
    if value < 0 or value > _MAX_CHECKSUM:
        raise GeocacheFieldError("invalid geocache digit_checksum")
    return value


def normalize_geocache_fields(
    ch: dict, *, require_code: bool = False,
) -> dict[str, object]:
    """Return normalized GC fields or raise GeocacheFieldError.

    ``require_code=True`` is the Web infer / geocache start contract: mode
    geocache must carry a non-empty ``GC[A-Z0-9]+``. Direct Challenge
    construction and CTF pass-through keep the default so empty fields stay
    valid there.
    """
    updates: dict[str, object] = {}
    raw_code = ch.get("gc_code")
    if require_code or raw_code not in (None, ""):
        updates["gc_code"] = require_gc_code(raw_code)
    if ch.get("geocheck_url") not in (None, ""):
        updates["geocheck_url"] = validate_checker_url(ch.get("geocheck_url"))
    lat, lon = validate_posted(ch.get("posted_lat"), ch.get("posted_lon"))
    if lat is not None:
        updates["posted_lat"] = lat
        updates["posted_lon"] = lon
    if "anchor_radius_m" in ch:
        updates["anchor_radius_m"] = validate_radius_m(ch.get("anchor_radius_m"))
    if "digit_checksum" in ch:
        updates["digit_checksum"] = validate_digit_checksum(ch.get("digit_checksum"))
    return updates
