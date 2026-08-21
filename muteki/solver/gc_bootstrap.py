"""Host-side Geocaching listing bootstrap via trusted ``gc show --json``."""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Awaitable, Callable
from subprocess import CompletedProcess
from typing import Any

from muteki.models.solve_graph import Challenge
from muteki.solver.gc_checker import _checker_env, _default_runner, _resolve_executable
from muteki.solver.gc_urls import (
    extract_checker_url,
    require_gc_code,
    validate_checker_url,
)
from muteki.swarm.shared_graph import SharedGraph
from muteki.vendor.geocaching_cli.coord import LatLon, format_dmm
from muteki.vendor.geocaching_cli.errors import CoordError


LISTING_CAP_BYTES = 64 * 1024
STDOUT_CAP_BYTES = 1024 * 1024
_LISTING_HEAD = "Geocaching listing ({code}):"
_SKELETON_FRAC_RE = re.compile(r"(\d+)\.(\d{3})\b")


class GcBootstrapError(RuntimeError):
    """Stable, secret-free failure to load a geocache listing."""


def _cap_bytes(text: str, limit: int) -> str:
    raw = (text or "").encode("utf-8")
    if len(raw) <= limit:
        return text or ""
    clipped = raw[:limit]
    return clipped.decode("utf-8", errors="ignore")


def _coord_skeleton(dmm: str) -> str:
    return _SKELETON_FRAC_RE.sub(r"\1.???", dmm)


def _require_finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GcBootstrapError(f"invalid geocache {label}") from exc
    if not math.isfinite(number):
        raise GcBootstrapError(f"invalid geocache {label}")
    return number


def _require_str(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GcBootstrapError(f"invalid geocache {label}")
    return value


def _parse_show_object(stdout: str) -> dict[str, Any]:
    text = (stdout or "").strip()
    if len((stdout or "").encode("utf-8")) > STDOUT_CAP_BYTES:
        raise GcBootstrapError("geocache listing output exceeded size cap")
    if not text.startswith("{") or not text.endswith("}"):
        raise GcBootstrapError("geocache listing is malformed")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GcBootstrapError("geocache listing is malformed") from exc
    if type(obj) is not dict:
        raise GcBootstrapError("geocache listing is malformed")
    return obj


def _validate_record(obj: dict[str, Any], requested: str) -> dict[str, Any]:
    returned = str(obj.get("gc_code") or "").strip().upper()
    if returned != requested:
        raise GcBootstrapError("geocache listing code mismatch")
    def _field(key: str) -> str:
        if key in obj and not isinstance(obj.get(key), str):
            raise GcBootstrapError(f"invalid geocache {key}")
        return _require_str(obj.get(key), key)

    name = _field("name")
    listing = _field("long_description")
    hint = _field("encoded_hints")
    if not name.strip() and not listing.strip() and not hint.strip():
        raise GcBootstrapError("geocache listing is empty")
    try:
        lat = _require_finite(obj.get("latitude"), "latitude")
        lon = _require_finite(obj.get("longitude"), "longitude")
        point = LatLon(lat, lon)
        point.validate()
    except (CoordError, GcBootstrapError) as exc:
        raise GcBootstrapError("invalid geocache coordinates") from exc
    difficulty = obj.get("difficulty")
    terrain = obj.get("terrain")
    if difficulty is not None:
        difficulty = _require_finite(difficulty, "difficulty")
    if terrain is not None:
        terrain = _require_finite(terrain, "terrain")
    cache_type = _require_str(obj.get("cache_type") or obj.get("type"), "cache_type")
    return {
        "gc_code": requested,
        "name": name,
        "cache_type": cache_type,
        "latitude": lat,
        "longitude": lon,
        "difficulty": difficulty,
        "terrain": terrain,
        "long_description": listing,
        "encoded_hints": hint,
        "point": point,
    }


def _source(code: str) -> str:
    return f"gc show {code} --json"


def _witness(code: str, stdout: str) -> str:
    body = _cap_bytes((stdout or "").strip(), 2048)
    return f"{_source(code)}\n{body}"


def _append_listing_once(challenge: Challenge, code: str, listing: str, hint: str) -> None:
    head = _LISTING_HEAD.format(code=code)
    if head in (challenge.description or ""):
        return
    blob = listing
    if hint.strip():
        blob = f"{listing}\nHint: {hint}" if listing else f"Hint: {hint}"
    blob = _cap_bytes(blob, LISTING_CAP_BYTES)
    prefix = (challenge.description or "").rstrip()
    challenge.description = f"{prefix}\n\n{head}\n{blob}".strip() if prefix else f"{head}\n{blob}"


def _write_facts(
    graph: SharedGraph,
    *,
    code: str,
    record: dict[str, Any],
    challenge: Challenge,
    stdout: str,
) -> int:
    source = _source(code)
    witness = _witness(code, stdout)
    point: LatLon = record["point"]
    dmm = format_dmm(point)
    families: list[str] = [
        f"posted coordinate: {dmm} ({point.latitude}, {point.longitude})",
    ]
    dt_bits = []
    if record["name"]:
        dt_bits.append(record["name"])
    if record["cache_type"]:
        dt_bits.append(f"({record['cache_type']})")
    if record["difficulty"] is not None or record["terrain"] is not None:
        d_val = record["difficulty"] if record["difficulty"] is not None else "?"
        t_val = record["terrain"] if record["terrain"] is not None else "?"
        dt_bits.append(f"D/T {d_val}/{t_val}")
    if dt_bits:
        families.append("cache " + " ".join(dt_bits))
    listing = _cap_bytes(record["long_description"], LISTING_CAP_BYTES)
    families.append(f"listing: {listing}")
    hint = record["encoded_hints"].strip()
    if hint:
        families.append(f"encoded hint: {hint}")
    if challenge.coord_skeleton:
        families.append(f"coord skeleton: {challenge.coord_skeleton}")
    if challenge.digit_checksum is not None:
        families.append(f"digit checksum: {challenge.digit_checksum}")
    if challenge.geocheck_url:
        families.append(f"checker url: {challenge.geocheck_url}")
    added = 0
    for fact in families:
        seq = graph.add_evidence(
            actor="gc-bootstrap",
            source=source,
            fact=fact,
            verified=True,
            witness=witness,
        )
        if seq and seq > 0:
            added += 1
    return added


def _enrich_challenge(challenge: Challenge, record: dict[str, Any]) -> None:
    challenge.gc_code = record["gc_code"]
    if challenge.posted_lat is None or challenge.posted_lon is None:
        challenge.posted_lat = record["latitude"]
        challenge.posted_lon = record["longitude"]
    if not str(challenge.coord_skeleton or "").strip():
        challenge.coord_skeleton = _coord_skeleton(format_dmm(record["point"]))
    if not str(challenge.geocheck_url or "").strip():
        extracted = extract_checker_url(record["long_description"])
        if extracted:
            challenge.geocheck_url = validate_checker_url(extracted)
    # never infer digit_checksum from prose
    _append_listing_once(
        challenge,
        record["gc_code"],
        record["long_description"],
        record["encoded_hints"],
    )


def _summary(challenge: Challenge, record: dict[str, Any], facts_added: int) -> dict[str, Any]:
    posted = ""
    if challenge.posted_lat is not None and challenge.posted_lon is not None:
        try:
            posted = format_dmm(LatLon(float(challenge.posted_lat), float(challenge.posted_lon)))
        except Exception:
            posted = ""
    return {
        "code": challenge.gc_code,
        "name": record.get("name") or challenge.name,
        "posted": posted,
        "difficulty": record.get("difficulty"),
        "terrain": record.get("terrain"),
        "skeleton": challenge.coord_skeleton,
        "checker_url": challenge.geocheck_url,
        "facts_added": facts_added,
    }


async def bootstrap_gc_challenge(
    challenge: Challenge,
    graph: SharedGraph,
    *,
    executable: str | None = None,
    timeout_s: float = 60.0,
    runner: Callable[..., Awaitable[CompletedProcess[str]]] | None = None,
) -> dict[str, Any]:
    if getattr(challenge, "mode", "") != "geocache":
        return {}
    try:
        code = require_gc_code(getattr(challenge, "gc_code", ""))
    except Exception as exc:
        raise GcBootstrapError("invalid geocache gc_code") from exc
    challenge.gc_code = code

    resolved = _resolve_executable(executable)
    if resolved is None:
        if runner is None:
            raise GcBootstrapError("geocache listing program is unavailable")
        resolved = executable or "gc"
    argv = [resolved, "show", code, "--json"]
    env = _checker_env()
    run = runner if runner is not None else _default_runner
    try:
        with tempfile.TemporaryDirectory(prefix="muteki-gc-show-") as tmp:
            result = await run(argv, cwd=tmp, env=env, timeout=timeout_s)
    except GcBootstrapError:
        raise
    except Exception as exc:
        raise GcBootstrapError("geocache listing command failed") from exc
    if int(result.returncode) != 0:
        raise GcBootstrapError("geocache listing command failed")
    obj = _parse_show_object(result.stdout or "")
    record = _validate_record(obj, code)
    _enrich_challenge(challenge, record)
    facts_added = 0
    if graph is not None:
        facts_added = _write_facts(
            graph, code=code, record=record, challenge=challenge,
            stdout=result.stdout or "",
        )
    return _summary(challenge, record, facts_added)
