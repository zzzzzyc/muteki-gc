"""Geocache coordinate gate, submit-coord, and CliSolver verified-only completion.

CTF ``flag_ok`` / ``submit-flag`` / ``_accept_flag`` semantics stay unchanged.
A locally accepted coordinate is only a candidate until digit-checksum verification;
unverified candidates must not enter the result/completion projection.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from muteki.core.events import EventType
from muteki.models.solve_graph import Challenge
from muteki.solver.cli_solver import CliSolver
from muteki.solver.gc_gate import CoordVerdict, coord_ok
from muteki.swarm.shared_graph import SQLiteSharedGraph

# Clean binary-exact posted point: N 51 30.000 E 0 00.000
_POSTED_LAT = 51.5
_POSTED_LON = 0.0
_EARTH_RADIUS_M = 6371008.8
_SLOT_SKELETON = "N 51 30.??? E 000 00.???"
_SLOT_CANDIDATE = "123 456"
_SLOT_HYPHEN = "123-456"
_SLOT_DMM = "N 51 30.123 E 0 00.456"
_SLOT_CHECKSUM = 30  # digits of _SLOT_DMM
_OFFSET_PLUS_MINUS = "+123-456"
_OFFSET_PLUS_PLUS = "+123+456"
_OFFSET_MINUS_PLUS = "-123+456"
_OFFSET_MINUS_MINUS = "-123-456"
_BLACKBOARD = Path(__file__).resolve().parents[1] / "skills" / "muteki-blackboard" / "blackboard.py"


def _gc(**kw) -> Challenge:
    fields = dict(
        id="gc-gate",
        name="cache",
        category="misc",
        mode="geocache",
        posted_lat=_POSTED_LAT,
        posted_lon=_POSTED_LON,
        coord_skeleton=_SLOT_SKELETON,
        digit_checksum=None,
        geocheck_url="https://geocheck.org/geo_check.php?gid=should-never-fetch",
        anchor_radius_m=3200.0,
    )
    fields.update(kw)
    return Challenge(**fields)


def _ctf(**kw) -> Challenge:
    fields = dict(
        id="ctf-gate",
        name="web",
        category="web",
        mode="ctf",
        flag_format=r"flag\{.*?\}",
    )
    fields.update(kw)
    return Challenge(**fields)


def _dmm_to_dd(text: str) -> tuple[float, float]:
    hem_lat, deg_lat, min_lat, hem_lon, deg_lon, min_lon = text.split()
    lat = (int(deg_lat) + float(min_lat) / 60.0) * (1.0 if hem_lat == "N" else -1.0)
    lon = (int(deg_lon) + float(min_lon) / 60.0) * (1.0 if hem_lon == "E" else -1.0)
    return lat, lon


def _cli(challenge: Challenge, tmp_path: Path, **kw) -> CliSolver:
    graph = kw.pop("shared_graph", None)
    if graph is None:
        graph = SQLiteSharedGraph(str(tmp_path / "sg.db"), challenge)
    spec = type("S", (), {"solver_id": "cli-1"})()
    solver = CliSolver(spec, challenge, kb=False, shared_graph=graph, **kw)
    tmp_path.mkdir(parents=True, exist_ok=True)
    solver._flag_submission_dir = Path(tempfile.mkdtemp(
        prefix="muteki-coord-submission-test-", dir=tmp_path))
    return solver


def _seed_output(solver: CliSolver, text: str) -> None:
    solver._raw_tool_outputs = [text]
    solver._raw_tool_commands = ["python puzzle.py"]
    solver._raw_tool_attributed = [True]


def _run_blackboard(solver: CliSolver, *argv: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "MUTEKI_BLACKBOARD_DB": str(solver.shared_graph.db_path),
        "MUTEKI_WORKER_ID": solver.solver_id,
        "MUTEKI_FLAG_SUBMISSION_DIR": str(solver._flag_submission_dir),
        "MUTEKI_INTENT_ID": getattr(solver, "_intent_id", "") or "",
    }
    return subprocess.run(
        [sys.executable, str(_BLACKBOARD), *argv],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _request_files(solver: CliSolver) -> list[Path]:
    return sorted(solver._flag_submission_dir.glob("fs-*.json"))


class _CaptureBus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, ev) -> None:
        self.events.append(ev)

    def bb_kinds(self) -> list[str]:
        return [
            ev.payload.get("kind")
            for ev in self.events
            if ev.event_type is EventType.BLACKBOARD_DELTA
        ]


# ── gate: mode / empty / posted ──────────────────────────────────────────────

def test_coord_ok_rejects_non_geocache_mode():
    verdict = coord_ok(_SLOT_CANDIDATE, _ctf(), _SLOT_CANDIDATE)
    assert verdict == CoordVerdict(False, False, "", verdict.reason)
    assert verdict.reason


def test_coord_ok_rejects_empty_candidate():
    verdict = coord_ok("   ", _gc(), "   ")
    assert verdict.accepted is False and verdict.verified is False
    assert verdict.coord_text == ""


def test_coord_ok_rejects_missing_posted_lat():
    ch = _gc(posted_lat=None, posted_lon=0.0)
    verdict = coord_ok(_SLOT_CANDIDATE, ch, _SLOT_CANDIDATE)
    assert verdict.accepted is False
    assert verdict.verified is False


def test_coord_ok_rejects_missing_posted_lon():
    ch = _gc(posted_lat=51.5, posted_lon=None)
    verdict = coord_ok("N 51 30.000 E 0 00.000", ch, "N 51 30.000 E 0 00.000")
    assert verdict.accepted is False


# ── gate: provenance ─────────────────────────────────────────────────────────

def test_coord_ok_rejects_candidate_absent_from_raw_output():
    verdict = coord_ok(_SLOT_CANDIDATE, _gc(), "puzzle printed something else")
    assert verdict.accepted is False
    assert "输出" in verdict.reason or "溯源" in verdict.reason or "原文" in verdict.reason


def test_coord_ok_requires_exact_trimmed_verbatim():
    # hyphen form is a different string; whitespace slot must appear verbatim
    raw = f"decoded {_SLOT_HYPHEN} only"
    assert coord_ok(_SLOT_CANDIDATE, _gc(), raw).accepted is False
    assert coord_ok(_SLOT_HYPHEN, _gc(), raw).accepted is True


# ── gate: slot family ────────────────────────────────────────────────────────

def test_coord_ok_slot_whitespace():
    verdict = coord_ok(_SLOT_CANDIDATE, _gc(), f"decoded {_SLOT_CANDIDATE}")
    assert verdict.accepted is True
    assert verdict.verified is False
    assert verdict.coord_text == _SLOT_DMM


def test_coord_ok_slot_hyphen():
    verdict = coord_ok(_SLOT_HYPHEN, _gc(), f"decoded {_SLOT_HYPHEN}")
    assert verdict.accepted is True
    assert verdict.coord_text == _SLOT_DMM


def test_coord_ok_slot_without_skeleton_rejected():
    ch = _gc(coord_skeleton="")
    verdict = coord_ok(_SLOT_CANDIDATE, ch, _SLOT_CANDIDATE)
    assert verdict.accepted is False
    assert verdict.coord_text == ""


def test_coord_ok_slot_malformed_skeleton_rejected():
    for skeleton in (
        "N 51 30.___ E 000 00.___",
        "N 51 30.??? E 000 00.000",
        "N 51 30.??? E 000 00.??? extra ???",
        "N 51 30.?? E 000 00.??",
    ):
        verdict = coord_ok(_SLOT_CANDIDATE, _gc(coord_skeleton=skeleton), _SLOT_CANDIDATE)
        assert verdict.accepted is False, skeleton


# ── gate: offset family ──────────────────────────────────────────────────────

def test_coord_ok_offset_four_sign_combinations_numeric():
    raw_plus_minus = f"offset {_OFFSET_PLUS_MINUS}"
    plus_minus = coord_ok(_OFFSET_PLUS_MINUS, _gc(), raw_plus_minus)
    assert plus_minus.accepted is True
    lat, lon = _dmm_to_dd(plus_minus.coord_text)
    assert lat == pytest_approx(_POSTED_LAT + 123 / 1000 / 60)
    assert lon == pytest_approx(_POSTED_LON - 456 / 1000 / 60)

    plus_plus = coord_ok(_OFFSET_PLUS_PLUS, _gc(), _OFFSET_PLUS_PLUS)
    lat, lon = _dmm_to_dd(plus_plus.coord_text)
    assert lat == pytest_approx(_POSTED_LAT + 123 / 1000 / 60)
    assert lon == pytest_approx(_POSTED_LON + 456 / 1000 / 60)

    minus_plus = coord_ok(_OFFSET_MINUS_PLUS, _gc(), _OFFSET_MINUS_PLUS)
    lat, lon = _dmm_to_dd(minus_plus.coord_text)
    assert lat == pytest_approx(_POSTED_LAT - 123 / 1000 / 60)
    assert lon == pytest_approx(_POSTED_LON + 456 / 1000 / 60)

    minus_minus = coord_ok(_OFFSET_MINUS_MINUS, _gc(), _OFFSET_MINUS_MINUS)
    lat, lon = _dmm_to_dd(minus_minus.coord_text)
    assert lat == pytest_approx(_POSTED_LAT - 123 / 1000 / 60)
    assert lon == pytest_approx(_POSTED_LON - 456 / 1000 / 60)


def test_coord_ok_offset_allows_whitespace_between_signed_groups():
    candidate = "+123 -456"
    verdict = coord_ok(candidate, _gc(), f"found {candidate}")
    assert verdict.accepted is True
    lat, lon = _dmm_to_dd(verdict.coord_text)
    assert lat == pytest_approx(_POSTED_LAT + 123 / 1000 / 60)
    assert lon == pytest_approx(_POSTED_LON - 456 / 1000 / 60)


def pytest_approx(value: float, abs_tol: float = 1e-9):
    class _Approx:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, (int, float)) and math.isclose(
                float(other), value, rel_tol=0.0, abs_tol=abs_tol)

        def __repr__(self) -> str:
            return f"approx({value})"
    return _Approx()


# ── gate: full coordinate ────────────────────────────────────────────────────

def test_coord_ok_full_dmm_accepted():
    candidate = "N 51 30.000 E 0 00.000"
    verdict = coord_ok(candidate, _gc(), f"listing {candidate}")
    assert verdict.accepted is True
    assert verdict.verified is False
    assert verdict.coord_text == "N 51 30.000 E 0 00.000"


def test_coord_ok_full_dd_accepted():
    candidate = "51.5,0"
    verdict = coord_ok(candidate, _gc(), f"dd {candidate}")
    assert verdict.accepted is True
    assert verdict.coord_text == "N 51 30.000 E 0 00.000"


def test_coord_ok_invalid_coordinate_rejected():
    candidate = "not-a-coordinate"
    verdict = coord_ok(candidate, _gc(), candidate)
    assert verdict.accepted is False
    assert verdict.verified is False
    assert verdict.coord_text == ""


# ── gate: distance ───────────────────────────────────────────────────────────

def test_coord_ok_over_radius_rejected():
    # ~1° north ≫ 3200 m
    candidate = "N 52 30.000 E 0 00.000"
    verdict = coord_ok(candidate, _gc(), candidate)
    assert verdict.accepted is False
    assert "半径" in verdict.reason or "距离" in verdict.reason


def test_coord_ok_exact_boundary_accepted():
    # Inclusive <= radius, checked on the parsed point (before DMM normalize).
    # Due-north spherical offset of exactly 3200 m; float slop is << 1 mm.
    final_lat = _POSTED_LAT + math.degrees(3200.0 / _EARTH_RADIUS_M)
    candidate = f"{final_lat:.12f},0"
    verdict = coord_ok(candidate, _gc(), candidate)
    assert verdict.accepted is True
    assert verdict.verified is False
    assert verdict.coord_text.startswith("N ")


def test_coord_ok_just_over_boundary_rejected():
    final_lat = _POSTED_LAT + math.degrees(3201.0 / _EARTH_RADIUS_M)
    candidate = f"{final_lat:.12f},0"
    verdict = coord_ok(candidate, _gc(), candidate)
    assert verdict.accepted is False


# ── gate: checksum upgrades verified; mismatch stays candidate ───────────────

def test_coord_ok_checksum_match_verified():
    ch = _gc(digit_checksum=_SLOT_CHECKSUM)
    verdict = coord_ok(_SLOT_CANDIDATE, ch, _SLOT_CANDIDATE)
    assert verdict.accepted is True
    assert verdict.verified is True
    assert verdict.coord_text == _SLOT_DMM


def test_coord_ok_checksum_mismatch_is_candidate_not_rejection():
    ch = _gc(digit_checksum=1)
    verdict = coord_ok(_SLOT_CANDIDATE, ch, _SLOT_CANDIDATE)
    assert verdict.accepted is True
    assert verdict.verified is False
    assert verdict.coord_text == _SLOT_DMM


def test_coord_ok_ignores_geocheck_url():
    ch = _gc(digit_checksum=_SLOT_CHECKSUM, geocheck_url="https://example.invalid/never")
    verdict = coord_ok(_SLOT_CANDIDATE, ch, _SLOT_CANDIDATE)
    assert verdict.accepted is True
    assert verdict.verified is True


# ── blackboard submit-coord / submit-flag ────────────────────────────────────

def test_submit_coord_atomic_payload_and_no_echo(tmp_path):
    solver = _cli(_gc(), tmp_path)
    result = _run_blackboard(solver, "submit-coord", _SLOT_CANDIDATE)
    assert result.returncode == 0, result.stderr
    assert _SLOT_CANDIDATE not in result.stdout
    assert _SLOT_CANDIDATE not in result.stderr
    assert re.fullmatch(r"SUBMITTED fs-[0-9a-f]{16}; awaiting provenance validation\n",
                        result.stdout)
    files = _request_files(solver)
    assert len(files) == 1
    raw = files[0].read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert raw == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert payload["submission_id"] == files[0].stem
    assert re.fullmatch(r"fs-[0-9a-f]{16}", payload["submission_id"])
    assert payload["protocol"] == "blackboard-api-v1"
    assert payload["actor"] == solver.solver_id
    assert payload["coord"] == _SLOT_CANDIDATE
    assert payload["submission_kind"] == "coord"
    assert "flag" not in payload
    assert payload["intent_id"] == ""
    assert isinstance(payload["created_at"], float)


def test_submit_coord_validation_matches_submit_flag(tmp_path):
    solver = _cli(_gc(), tmp_path)
    too_long = "1" * 1025
    for bad in ("", too_long, "N 51\n30.000 E 0 00.000"):
        result = _run_blackboard(solver, "submit-coord", bad)
        assert result.returncode == 2
        assert "ERROR:" in result.stderr
        if bad.strip():
            assert bad.strip() not in result.stdout
        assert _request_files(solver) == []


def test_submit_flag_payload_unchanged_byte_keys(tmp_path):
    flag = "flag{api_compatible}"
    solver = _cli(_ctf(), tmp_path)
    result = _run_blackboard(solver, "submit-flag", flag)
    assert result.returncode == 0, result.stderr
    assert flag not in result.stdout
    files = _request_files(solver)
    assert len(files) == 1
    raw = files[0].read_bytes()
    payload = json.loads(raw)
    assert set(payload) == {
        "submission_id", "flag", "intent_id", "actor", "protocol", "created_at",
    }
    assert payload["flag"] == flag
    assert payload["protocol"] == "blackboard-api-v1"
    assert payload["actor"] == solver.solver_id
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    assert raw.decode("utf-8") == compact


def test_flag_submission_graph_api_omits_default_kind(tmp_path):
    ch = _ctf()
    graph = SQLiteSharedGraph(str(tmp_path / "sg.db"), ch)
    seq = graph.flag_submission(
        actor="cli-1", submission_id="fs-0123456789abcdef", flag="flag{x}")
    assert seq > 0
    payload = graph.events()[-1]["payload"]
    assert payload == {
        "submission_id": "fs-0123456789abcdef",
        "flag": "flag{x}",
        "intent_id": "",
        "protocol": "blackboard-api-v1",
    }
    assert "submission_kind" not in payload


def test_flag_submission_graph_api_coord_kind_omits_flag(tmp_path):
    ch = _gc()
    graph = SQLiteSharedGraph(str(tmp_path / "sg.db"), ch)
    graph.flag_submission(
        actor="cli-1", submission_id="fs-fedcba9876543210",
        coord=_SLOT_CANDIDATE, submission_kind="coord")
    payload = graph.events()[-1]["payload"]
    assert payload["submission_kind"] == "coord"
    assert payload["coord"] == _SLOT_CANDIDATE
    assert "flag" not in payload
    assert payload["protocol"] == "blackboard-api-v1"


# ── CliSolver integration ────────────────────────────────────────────────────

def test_cli_solver_candidate_writes_fact_not_graph_flag(tmp_path):
    bus = _CaptureBus()
    ch = _gc(digit_checksum=1, geocheck_url="")
    solver = _cli(ch, tmp_path, bus=bus)
    _seed_output(solver, f"worksheet {_SLOT_CANDIDATE}")
    submitted = _run_blackboard(solver, "submit-coord", _SLOT_CANDIDATE)
    assert submitted.returncode == 0
    asyncio.run(solver._drain_blackboard_flag_submissions())

    snap = solver.shared_graph.snapshot()
    assert snap.flags == []
    assert solver.graph.flags == []
    assert solver._already_found == set()
    assert _SLOT_DMM not in solver._validated_flag_submissions
    assert _SLOT_CANDIDATE not in solver._validated_flag_submissions
    candidates = [ev for ev in snap.evidence if not ev.verified]
    assert any(_SLOT_DMM in ev.fact for ev in candidates)
    decisions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission_decision"
    ]
    assert decisions[-1]["payload"]["accepted"] is True
    assert decisions[-1]["payload"]["code"] == "coord_candidate"
    assert "coord_candidate" in bus.bb_kinds()
    assert "coord_found" not in bus.bb_kinds()
    assert "flag_found" not in bus.bb_kinds()


def test_cli_solver_verified_coord_reaches_graph_flags(tmp_path):
    bus = _CaptureBus()
    ch = _gc(digit_checksum=_SLOT_CHECKSUM)
    solver = _cli(ch, tmp_path, bus=bus)
    _seed_output(solver, f"worksheet {_SLOT_CANDIDATE}")
    submitted = _run_blackboard(solver, "submit-coord", _SLOT_CANDIDATE)
    assert submitted.returncode == 0
    asyncio.run(solver._drain_blackboard_flag_submissions())

    assert solver.graph.flags == [_SLOT_DMM]
    assert solver.shared_graph.snapshot().flags == [_SLOT_DMM]
    assert solver._already_found == {_SLOT_DMM}
    assert _SLOT_DMM in solver._validated_coord_submissions
    assert _SLOT_DMM not in solver._validated_flag_submissions
    decisions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission_decision"
    ]
    assert decisions[-1]["payload"]["accepted"] is True
    assert decisions[-1]["payload"]["code"] == "coord_verified"
    kinds = bus.bb_kinds()
    assert "coord_found" in kinds
    assert "flag_found" in kinds


def test_cli_solver_geocache_rejects_submit_flag(tmp_path):
    bus = _CaptureBus()
    flag = "flag{not_a_coord}"
    solver = _cli(_gc(), tmp_path, bus=bus)
    _seed_output(solver, f"stdout had {flag}")
    submitted = _run_blackboard(solver, "submit-flag", flag)
    assert submitted.returncode == 0
    asyncio.run(solver._drain_blackboard_flag_submissions())

    assert solver.graph.flags == []
    assert solver.shared_graph.snapshot().flags == []
    assert flag not in solver._validated_flag_submissions
    decisions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission_decision"
    ]
    assert decisions[-1]["payload"]["accepted"] is False
    assert decisions[-1]["payload"]["code"] == "coord_rejected"


def test_cli_solver_ctf_submit_flag_still_uses_flag_ok(tmp_path):
    bus = _CaptureBus()
    flag = "flag{seen_in_output}"
    solver = _cli(_ctf(), tmp_path, bus=bus)
    _seed_output(solver, f"decoder printed {flag}")
    submitted = _run_blackboard(solver, "submit-flag", flag)
    assert submitted.returncode == 0
    assert flag not in submitted.stdout
    asyncio.run(solver._drain_blackboard_flag_submissions())

    assert solver.graph.flags == [flag]
    assert solver.shared_graph.snapshot().flags == [flag]
    assert flag in solver._validated_flag_submissions
    decisions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission_decision"
    ]
    assert decisions[-1]["payload"]["accepted"] is True
    assert decisions[-1]["payload"]["code"] == "accepted"
    submissions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission"
    ]
    assert "submission_kind" not in submissions[-1]["payload"]
    assert submissions[-1]["payload"]["flag"] == flag


def test_cli_solver_rejected_coord_does_not_write_flag(tmp_path):
    solver = _cli(_gc(), tmp_path, bus=_CaptureBus())
    _seed_output(solver, "no candidate here")
    _run_blackboard(solver, "submit-coord", _SLOT_CANDIDATE)
    asyncio.run(solver._drain_blackboard_flag_submissions())
    decisions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission_decision"
    ]
    assert decisions[-1]["payload"]["accepted"] is False
    assert decisions[-1]["payload"]["code"] == "coord_rejected"
    assert solver.graph.flags == []
    assert solver.shared_graph.snapshot().flags == []
    assert solver.shared_graph.snapshot().evidence == []


def test_accept_coordinate_is_separate_from_accept_flag(tmp_path):
    ch = _gc(digit_checksum=_SLOT_CHECKSUM)
    solver = _cli(ch, tmp_path, bus=_CaptureBus())
    assert hasattr(solver, "_accept_coordinate")
    assert solver._accept_coordinate is not solver._accept_flag
    _seed_output(solver, _SLOT_CANDIDATE)
    _run_blackboard(solver, "submit-coord", _SLOT_CANDIDATE)
    asyncio.run(solver._drain_blackboard_flag_submissions())
    assert solver.graph.flags == [_SLOT_DMM]
    # A second verified accept is a no-op (dedup as strict as _accept_flag).
    solver._validated_coord_submissions.add(_SLOT_DMM)
    assert asyncio.run(solver._accept_coordinate(_SLOT_DMM)) is False
    assert solver.graph.flags == [_SLOT_DMM]


def _last_decision(solver: CliSolver) -> dict:
    decisions = [
        ev for ev in solver.shared_graph.events()
        if ev["kind"] == "flag_submission_decision"
    ]
    assert decisions
    return decisions[-1]["payload"]


def _submit_and_drain(solver: CliSolver, candidate: str = _SLOT_CANDIDATE) -> None:
    submitted = _run_blackboard(solver, "submit-coord", candidate)
    assert submitted.returncode == 0
    asyncio.run(solver._drain_blackboard_flag_submissions())


# ── review: provenance hardening ─────────────────────────────────────────────

def test_cli_solver_rejects_operator_laundered_raw_candidate(tmp_path):
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    solver._remember_operator_context(f"hint: try {_SLOT_CANDIDATE}")
    _seed_output(solver, f"echoed {_SLOT_CANDIDATE}")
    _submit_and_drain(solver)
    assert solver.graph.flags == []
    assert solver.shared_graph.snapshot().flags == []
    assert _last_decision(solver)["accepted"] is False
    assert _last_decision(solver)["code"] == "coord_rejected"


def test_cli_solver_rejects_operator_laundered_normalized_coord(tmp_path):
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    solver._remember_operator_context(_SLOT_DMM)
    _seed_output(solver, f"worksheet {_SLOT_CANDIDATE}")
    _submit_and_drain(solver)
    assert solver.graph.flags == []
    assert solver.shared_graph.snapshot().flags == []
    assert _last_decision(solver)["code"] == "coord_rejected"


def test_cli_solver_rejects_internal_storage_launder(tmp_path):
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    _seed_output(
        solver,
        f"found it in ~/.codex/sessions/abc {_SLOT_CANDIDATE}",
    )
    _submit_and_drain(solver)
    assert solver.graph.flags == []
    assert _last_decision(solver)["code"] == "coord_rejected"


def test_cli_solver_rejects_file_token_launder_only_with_read_action(tmp_path):
    stolen = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path / "steal", bus=_CaptureBus())
    _seed_output(
        stolen,
        "$ grep -r coord /workspace/eval_runs/run-11550/winner.json\n"
        f"...later...\nI recovered {_SLOT_CANDIDATE}\n",
    )
    _submit_and_drain(stolen)
    assert stolen.graph.flags == []
    assert _last_decision(stolen)["code"] == "coord_rejected"

    mention = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path / "ok", bus=_CaptureBus())
    _seed_output(
        mention,
        "GET /winner.json HTTP/1.1 -> 200\n"
        f"body contained {_SLOT_CANDIDATE}\n",
    )
    _submit_and_drain(mention)
    assert mention.graph.flags == [_SLOT_DMM]
    assert _last_decision(mention)["code"] == "coord_verified"


def test_cli_solver_rejects_foreknowledge_origin_on_verified(tmp_path):
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    solver._persist_raw_tool_output("", command=f"echo '{_SLOT_CANDIDATE}' > planted.txt")
    solver._persist_raw_tool_output(_SLOT_CANDIDATE, command="cat planted.txt")
    _submit_and_drain(solver)
    assert solver.graph.flags == []
    assert solver.shared_graph.snapshot().flags == []
    assert _last_decision(solver)["code"] == "coord_rejected"


def test_cli_solver_rejects_unsanctioned_origin_on_verified(tmp_path):
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    solver._persist_raw_tool_output(
        f"solution: the coord is {_SLOT_CANDIDATE}",
        command="curl -sL https://writeups.example/gc.html",
    )
    _submit_and_drain(solver)
    assert solver.graph.flags == []
    assert _last_decision(solver)["code"] == "coord_rejected"


def test_cli_solver_unsanctioned_origin_still_writes_unverified_candidate(tmp_path):
    solver = _cli(_gc(digit_checksum=1), tmp_path, bus=_CaptureBus())
    solver._persist_raw_tool_output(
        f"solution: the coord is {_SLOT_CANDIDATE}",
        command="curl -sL https://writeups.example/gc.html",
    )
    _submit_and_drain(solver)
    assert solver.graph.flags == []
    assert _last_decision(solver)["accepted"] is True
    assert _last_decision(solver)["code"] == "coord_candidate"
    assert any(_SLOT_DMM in ev.fact for ev in solver.shared_graph.snapshot().evidence)


def test_cli_solver_legitimate_computed_coord_still_verified(tmp_path):
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    solver._persist_raw_tool_output(
        f"python puzzle.py\n{_SLOT_CANDIDATE}",
        command="python puzzle.py",
    )
    _submit_and_drain(solver)
    assert solver.graph.flags == [_SLOT_DMM]
    assert solver.shared_graph.snapshot().flags == [_SLOT_DMM]
    assert _last_decision(solver)["code"] == "coord_verified"


# ── review minors ────────────────────────────────────────────────────────────

def test_coord_ok_zero_radius_stays_zero():
    ch = _gc(anchor_radius_m=0)
    posted = "N 51 30.000 E 0 00.000"
    assert coord_ok(posted, ch, posted).accepted is True
    assert coord_ok(_SLOT_CANDIDATE, ch, _SLOT_CANDIDATE).accepted is False


def test_coord_ok_slot_and_offset_ascii_digits_only():
    fullwidth_slot = "１２３ ４５６"
    assert coord_ok(fullwidth_slot, _gc(), fullwidth_slot).accepted is False
    fullwidth_offset = "+１２３-４５６"
    assert coord_ok(fullwidth_offset, _gc(), fullwidth_offset).accepted is False


def test_cli_solver_operator_invalidated_normalized_coord_not_reaccepted(tmp_path):
    graph = SQLiteSharedGraph(str(tmp_path / "sg.db"), _gc(digit_checksum=_SLOT_CHECKSUM))
    first = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path / "w1",
                 shared_graph=graph, bus=_CaptureBus())
    _seed_output(first, f"worksheet {_SLOT_CANDIDATE}")
    _submit_and_drain(first)
    assert first.graph.flags == [_SLOT_DMM]
    graph.reopen_after_false_positive(actor="operator", flag=_SLOT_DMM)
    assert _SLOT_DMM in graph.invalidated_flags()
    assert _SLOT_DMM not in graph.snapshot().flags

    second = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path / "w2",
                  shared_graph=graph, bus=_CaptureBus())
    _seed_output(second, f"worksheet {_SLOT_CANDIDATE}")
    _submit_and_drain(second)
    assert second.graph.flags == []
    assert _SLOT_DMM not in graph.snapshot().flags
    assert _SLOT_DMM not in second._already_found


def test_vendored_coord_attribution_and_gate_import():
    from muteki.vendor.geocaching_cli import SOURCE_COMMIT, SOURCE_URL
    from muteki.solver import gc_gate

    assert SOURCE_COMMIT == "4273699aa1dfc7da09e75eb4211ad923e4fd0bd3"
    assert "zzzzzyc/geocaching-cli" in SOURCE_URL
    coord_path = (
        Path(__file__).resolve().parents[1]
        / "muteki" / "vendor" / "geocaching_cli" / "coord.py"
    )
    header = coord_path.read_text(encoding="utf-8")[:800]
    assert SOURCE_COMMIT in header
    assert "zzzzzyc/geocaching-cli" in header
    assert gc_gate.LatLon.__module__ == "muteki.vendor.geocaching_cli.coord"
    assert gc_gate.parse_coord.__module__ == "muteki.vendor.geocaching_cli.coord"
