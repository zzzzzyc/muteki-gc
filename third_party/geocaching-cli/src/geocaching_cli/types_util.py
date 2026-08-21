"""Cache-type aliases used by search filters."""

from __future__ import annotations

_RULES: list[tuple[str, str]] = [
    ("traditional", "traditional"),
    ("unknown", "mystery"),
    ("mystery", "mystery"),
    ("puzzle", "mystery"),
    ("multi", "multi"),
    ("letterbox", "letterbox"),
    ("earth", "earth"),
    ("virtual", "virtual"),
    ("webcam", "webcam"),
    ("cito", "cito"),
    ("wherigo", "wherigo"),
    ("event", "event"),
]


def canonical_cache_type(value: str) -> str:
    lowered = value.lower().strip()
    aliases = {
        "trad": "traditional",
        "traditional": "traditional",
        "mystery": "mystery",
        "unknown": "mystery",
        "puzzle": "mystery",
        "multi": "multi",
        "multicache": "multi",
        "letterbox": "letterbox",
        "earth": "earth",
        "earthcache": "earth",
        "virtual": "virtual",
        "webcam": "webcam",
        "event": "event",
        "cito": "cito",
        "wherigo": "wherigo",
    }
    if lowered in aliases:
        return aliases[lowered]
    for needle, canon in _RULES:
        if needle in lowered:
            return canon
    return lowered
