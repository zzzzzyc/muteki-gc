"""Host-side geocache listing bootstrap: trusted ``gc show --json`` only.

No test in this module talks to the network or launches Playwright. Fake
executables and an injected runner stand in for the CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from muteki.core.event_bus import EventBus
from muteki.core.events import EventType
from muteki.core.llm import ModelSpec
from muteki.models.solve_graph import Challenge
from muteki.sandbox.manager import SandboxManager
from muteki.solver.result import ArtifactStore
from muteki.swarm.shared_graph import SQLiteSharedGraph
from muteki.swarm.swarm import Swarm
from muteki.vendor.geocaching_cli.coord import LatLon, format_dmm


_GC = "GC8ABCD"
_LISTING_HTML = (
    '<p>Look at the sundial.</p>'
    '<a href="https://geocheck.org/geo_check.php?gid=42">checker</a>'
)
_HINT = "under the bench"
_LAT = 51.476852
_LON = -0.000500


def _record(**extra: Any) -> dict[str, Any]:
    body = {
        "gc_code": _GC,
        "name": "Greenwich Mystery",
        "cache_type": "Unknown Cache",
        "latitude": _LAT,
        "longitude": _LON,
        "difficulty": 3.5,
        "terrain": 2.0,
        "long_description": _LISTING_HTML,
        "encoded_hints": _HINT,
    }
    body.update(extra)
    return body


def _challenge(**extra: Any) -> Challenge:
    fields = {
        "id": "gc-boot",
        "name": "boot",
        "category": "misc",
        "mode": "geocache",
        "description": "解答 Geocaching Mystery GC8ABCD",
        "gc_code": _GC,
    }
    fields.update(extra)
    return Challenge(**fields)


def _graph(tmp_path: Path, challenge: Challenge | None = None) -> SQLiteSharedGraph:
    ch = challenge or _challenge()
    return SQLiteSharedGraph.open(db_path=tmp_path / "shared_graph.db", challenge=ch)


def _run(coro):
    return asyncio.run(coro)


def _facts(graph: SQLiteSharedGraph) -> list[dict[str, Any]]:
    snap = graph.snapshot()
    return [
        {
            "fact": ev.fact,
            "source": ev.source,
            "witness": ev.witness,
            "verified": ev.verified,
            "actor": getattr(ev, "actor", "") or "",
        }
        for ev in snap.evidence
    ]


def _fact_texts(graph: SQLiteSharedGraph) -> list[str]:
    return [row["fact"] for row in _facts(graph)]


async def _scripted_runner(
    record: dict[str, Any] | str,
    *,
    returncode: int = 0,
    stderr: str = "",
    capture: dict[str, Any] | None = None,
):
    stdout = record if isinstance(record, str) else json.dumps(record)

    async def runner(argv, *, cwd, env, timeout):
        if capture is not None:
            capture["argv"] = list(argv)
            capture["cwd"] = cwd
            capture["env"] = dict(env)
            capture["timeout"] = timeout
            capture["shell"] = False
        return CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return runner


# ── no-op / preconditions ────────────────────────────────────────────────────


def test_bootstrap_is_noop_unless_geocache(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    async def boom(*_a, **_k):
        raise AssertionError("runner must not start")

    graph = _graph(tmp_path)
    for mode in ("ctf", "pentest"):
        ch = Challenge(id=mode, name=mode, category="web", mode=mode, gc_code=_GC)
        out = _run(bootstrap_gc_challenge(ch, graph, runner=boom))
        assert out == {}
    assert _facts(graph) == []


def test_bootstrap_requires_and_normalizes_gc_code(tmp_path):
    from muteki.solver.gc_bootstrap import GcBootstrapError, bootstrap_gc_challenge

    async def boom(*_a, **_k):
        raise AssertionError("runner must not start")

    graph = _graph(tmp_path)
    for bad in ("", "gc", "FLAG{no}", "GC-1", "1GC8ABCD"):
        ch = _challenge(gc_code=bad)
        with pytest.raises(GcBootstrapError) as ei:
            _run(bootstrap_gc_challenge(ch, graph, runner=boom))
        assert "cookie" not in str(ei.value).lower()
        assert "GEOCACHING" not in str(ei.value)

    capture: dict[str, Any] = {}
    runner = _run(_scripted_runner(_record(), capture=capture))
    ch = _challenge(gc_code="gc8abcd")
    _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    assert ch.gc_code == "GC8ABCD"
    assert capture["argv"][2] == "GC8ABCD"


def test_bootstrap_command_env_cwd_no_shell(tmp_path, monkeypatch):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    capture: dict[str, Any] = {}
    secret = "sk-secret-must-not-leak"
    cookie = "GEO_COOKIE_DUMMY"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("GEOCACHING_COOKIE", cookie)
    monkeypatch.setenv("GEOCACHING_USERNAME", "alice")
    runner = _run(_scripted_runner(_record(), capture=capture))
    previous = os.getcwd()
    worker = tmp_path / "worker-cwd"
    worker.mkdir()
    try:
        os.chdir(worker)
        _run(bootstrap_gc_challenge(_challenge(), _graph(tmp_path), runner=runner))
    finally:
        os.chdir(previous)
    argv = capture["argv"]
    assert argv[1:] == ["show", "GC8ABCD", "--json"]
    assert capture["shell"] is False
    cwd = Path(capture["cwd"])
    assert cwd != worker
    assert cwd != tmp_path
    env = capture["env"]
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["GEOCACHING_COOKIE"] == cookie
    assert env["GEOCACHING_USERNAME"] == "alice"
    assert env.get("PATH")
    assert env.get("HOME")


def test_bootstrap_rejects_malformed_and_hides_secrets(tmp_path):
    from muteki.solver.gc_bootstrap import GcBootstrapError, bootstrap_gc_challenge

    cookie = "GEO_COOKIE_DUMMY"
    graph = _graph(tmp_path)
    cases = [
        (1, json.dumps(_record()), "auth failed cookie=" + cookie),
        (0, json.dumps(_record()) + "\ntrailing", ""),
        (0, json.dumps([_record()]), ""),
        (0, "not-json", cookie),
        (0, "", ""),
        (0, json.dumps(_record())[:-1], ""),
        (0, "x" * (1024 * 1024 + 8), ""),
    ]
    for rc, stdout, stderr in cases:
        runner = _run(_scripted_runner(stdout, returncode=rc, stderr=stderr))
        with pytest.raises(GcBootstrapError) as ei:
            _run(bootstrap_gc_challenge(_challenge(), graph, runner=runner))
        msg = str(ei.value)
        assert cookie not in msg
        assert "trailing" not in msg
        assert stdout[:40] not in msg or not stdout.startswith("{")


def test_bootstrap_rejects_mismatched_code_and_bad_coords(tmp_path):
    from muteki.solver.gc_bootstrap import GcBootstrapError, bootstrap_gc_challenge

    graph = _graph(tmp_path)
    bad_records = [
        _record(gc_code="GC9ZZZZ"),
        _record(latitude=float("nan"), longitude=0.0),
        _record(latitude=91.0, longitude=0.0),
        _record(latitude=0.0, longitude=float("inf")),
        _record(difficulty=float("nan")),
        _record(name="", long_description="", encoded_hints=""),
        _record(name=12),
        _record(long_description=None),
    ]
    for rec in bad_records:
        runner = _run(_scripted_runner(rec))
        with pytest.raises(GcBootstrapError):
            _run(bootstrap_gc_challenge(_challenge(), graph, runner=runner))


def test_bootstrap_enriches_posted_skeleton_checker_and_description(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    ch = _challenge()
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record()))
    summary = _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    assert ch.posted_lat == pytest.approx(_LAT)
    assert ch.posted_lon == pytest.approx(_LON)
    posted = LatLon(_LAT, _LON)
    posted.validate()
    dmm = format_dmm(posted)
    assert ch.coord_skeleton == dmm.replace(".852", ".???").replace(".030", ".???") \
        or ch.coord_skeleton.count("???") == 2
    assert ch.coord_skeleton.count("???") == 2
    assert "???" in ch.coord_skeleton
    assert ch.geocheck_url == "https://geocheck.org/geo_check.php?gid=42"
    assert "Geocaching listing (GC8ABCD):" in ch.description
    assert _LISTING_HTML in ch.description
    assert _HINT in ch.description
    assert summary["code"] == "GC8ABCD"
    assert summary["name"] == "Greenwich Mystery"
    assert "cookie" not in json.dumps(summary).lower()
    assert "long_description" not in summary
    assert summary["checker_url"] == ch.geocheck_url
    assert summary["facts_added"] == 6


def test_bootstrap_does_not_overwrite_explicit_posted_skeleton_checker(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    ch = _challenge(
        posted_lat=40.0,
        posted_lon=-74.0,
        coord_skeleton="N 40 41.??? W 074 02.???",
        geocheck_url="https://www.geotjek.dk/geo_check.php?gid=99",
        digit_checksum=17,
    )
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record(
        long_description="<p>no checker here</p>",
    )))
    _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    assert ch.posted_lat == 40.0
    assert ch.posted_lon == -74.0
    assert ch.coord_skeleton == "N 40 41.??? W 074 02.???"
    assert ch.geocheck_url == "https://www.geotjek.dk/geo_check.php?gid=99"
    assert ch.digit_checksum == 17


def test_bootstrap_never_infers_digit_checksum_from_prose(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    ch = _challenge()
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record(
        long_description="<p>the digit checksum is 42 and the total is 17</p>",
        encoded_hints="checksum 9",
    )))
    _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    assert ch.digit_checksum is None


def test_bootstrap_checker_url_allowlist_and_entity_decoding(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    html = (
        '<a href="javascript:alert(1)">x</a>'
        '<a href="data:text/html,hi">x</a>'
        '<a href="https://geocheck.org.evil.com/geo_check.php?gid=1">x</a>'
        '<a href="http://geocheck.org/geo_check.php?gid=1">x</a>'
        '<a href="https://evil.com/https://geocheck.org/geo_check.php">x</a>'
        '<a href="https://certitude.geocaching.com/cert?id=1&amp;wp=GC8ABCD">ok</a>'
    )
    ch = _challenge()
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record(long_description=html)))
    _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    assert ch.geocheck_url == "https://certitude.geocaching.com/cert?id=1&wp=GC8ABCD"


def test_bootstrap_writes_seven_verified_fact_families_with_witness(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    ch = _challenge(digit_checksum=17)
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record()))
    _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    rows = _facts(graph)
    assert len(rows) == 7
    joined = "\n".join(r["fact"] for r in rows)
    assert format_dmm(LatLon(_LAT, _LON)) in joined
    assert "3.5" in joined and "2.0" in joined
    assert "Greenwich Mystery" in joined
    assert _LISTING_HTML in joined
    assert _HINT in joined
    assert "???" in joined
    assert "17" in joined
    assert "https://geocheck.org/geo_check.php?gid=42" in joined
    source = f"gc show {_GC} --json"
    for row in rows:
        assert row["verified"] is True
        assert row["source"] == source
        assert row["witness"]
        assert "cookie" not in (row["witness"] or "").lower()
        assert row["actor"] in ("", "gc-bootstrap") or "gc-bootstrap" in joined
    events = graph.events()
    actors = {e.get("actor") for e in events if e.get("kind") == "fact_added"}
    assert "gc-bootstrap" in actors


def test_bootstrap_is_idempotent_on_facts_and_description(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    ch = _challenge(digit_checksum=17)
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record()))
    first = _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    desc = ch.description
    second = _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    assert ch.description == desc
    assert ch.description.count("Geocaching listing (GC8ABCD):") == 1
    assert len(_facts(graph)) == 7
    assert first["facts_added"] == 7
    assert second["facts_added"] == 0


def test_bootstrap_description_append_is_capped(tmp_path):
    from muteki.solver.gc_bootstrap import bootstrap_gc_challenge

    huge = "<p>" + ("字" * (70 * 1024)) + "</p>"
    ch = _challenge()
    graph = _graph(tmp_path, ch)
    runner = _run(_scripted_runner(_record(long_description=huge, encoded_hints="h" * 100)))
    _run(bootstrap_gc_challenge(ch, graph, runner=runner))
    section = ch.description.split("Geocaching listing (GC8ABCD):", 1)[1]
    assert len(section.encode("utf-8")) <= 64 * 1024 + 32
    listing_facts = [r for r in _facts(graph) if "<p>" in r["fact"] or "字" in r["fact"]]
    assert listing_facts
    assert len(listing_facts[0]["fact"].encode("utf-8")) <= 64 * 1024 + 64


# ── Coordinator integration ──────────────────────────────────────────────────


def _swarm(challenge: Challenge, tmp_path: Path, **kw: Any) -> Swarm:
    kw.setdefault("race_scout", False)
    return Swarm(
        challenge,
        [ModelSpec(solver_id="seat", model="mock")],
        llm=None,
        sandbox=SandboxManager(root=tmp_path / "sbx"),
        artifacts=ArtifactStore(root=tmp_path / "arts"),
        executor="cli",
        coordinator=True,
        graph_dir=tmp_path / "graph",
        **kw,
    )


def test_coordinator_bootstraps_before_health_and_workers(tmp_path, monkeypatch):
    from muteki.solver import gc_bootstrap as gb

    order: list[str] = []
    ch = _challenge()

    async def fake_boot(challenge, graph, **_k):
        order.append("bootstrap")
        assert graph is not None
        assert challenge.mode == "geocache"
        return {
            "code": "GC8ABCD",
            "name": "Greenwich Mystery",
            "posted": "N 51 28.611 W 000 00.030",
            "difficulty": 3.5,
            "terrain": 2.0,
            "skeleton": "N 51 28.??? W 000 00.???",
            "checker_url": "https://geocheck.org/geo_check.php?gid=42",
            "facts_added": 7,
        }

    monkeypatch.setattr(gb, "bootstrap_gc_challenge", fake_boot)
    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.bootstrap_gc_challenge", fake_boot,
        raising=False,
    )
    bus = EventBus()
    sw = _swarm(ch, tmp_path, bus=bus)

    async def health() -> list[str]:
        order.append("health")
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    outcome = _run(asyncio.wait_for(sw._run_coordinator(), timeout=3))
    assert order == ["bootstrap", "health"]
    assert outcome.solved is False


@pytest.mark.asyncio
async def test_coordinator_bootstrap_success_emits_sanitized_delta(
    tmp_path, monkeypatch,
):
    from muteki.solver import gc_bootstrap as gb

    captured: list[dict[str, Any]] = []

    async def fake_boot(challenge, graph, **_k):
        return {
            "code": "GC8ABCD",
            "name": "Greenwich Mystery",
            "posted": "N 51 28.611 W 000 00.030",
            "difficulty": 3.5,
            "terrain": 2.0,
            "skeleton": "N 51 28.??? W 000 00.???",
            "checker_url": "https://geocheck.org/geo_check.php?gid=42",
            "facts_added": 3,
            "long_description": "<secret listing>",
        }

    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.bootstrap_gc_challenge", fake_boot,
        raising=False,
    )
    monkeypatch.setattr(gb, "bootstrap_gc_challenge", fake_boot, raising=False)
    bus = EventBus()

    async def recorder(ev):
        captured.append({"type": ev.event_type, "payload": dict(ev.payload or {})})

    bus.add_sink(recorder)
    sw = _swarm(_challenge(), tmp_path, bus=bus)

    async def health() -> list[str]:
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    deltas = [
        row["payload"] for row in captured
        if row["type"] is EventType.BLACKBOARD_DELTA
        and row["payload"].get("kind") == "gc_bootstrap_complete"
    ]
    assert deltas
    payload = deltas[0]
    assert payload.get("code") == "GC8ABCD"
    assert payload.get("name") == "Greenwich Mystery"
    assert "<secret listing>" not in json.dumps(payload)
    assert "long_description" not in payload
    dumped = json.dumps(captured)
    assert "<secret listing>" not in dumped


@pytest.mark.asyncio
async def test_coordinator_bootstrap_failure_is_blocker_not_solved(
    tmp_path, monkeypatch,
):
    from muteki.solver.gc_bootstrap import GcBootstrapError

    async def boom(*_a, **_k):
        raise GcBootstrapError("无法加载 Geocaching listing")

    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.bootstrap_gc_challenge", boom,
        raising=False,
    )
    bus = EventBus()
    captured: list[Any] = []

    async def recorder(ev):
        captured.append(ev)

    bus.add_sink(recorder)
    sw = _swarm(_challenge(), tmp_path, bus=bus)

    async def health() -> list[str]:
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    outcome = await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert outcome.solved is False
    assert "solved" not in (outcome.reason or "").lower() or "NoEligibleEngine" in outcome.reason
    reqs = [ev for ev in captured if ev.event_type is EventType.HITL_REQUEST]
    assert reqs
    need = str((reqs[0].payload or {}).get("need") or "")
    assert "gc auth login" in need or "GC" in need
    assert (reqs[0].payload or {}).get("need_kind") == "external_blocker"
    assert any(
        h.get("need_kind") == "external_blocker" for h in sw._pending_help
    )
    dumped = json.dumps([(ev.payload or {}) for ev in captured], ensure_ascii=False)
    assert "cookie" not in dumped.lower()


@pytest.mark.asyncio
async def test_coordinator_skips_bootstrap_for_ctf(tmp_path, monkeypatch):
    called = {"n": 0}

    async def fake_boot(*_a, **_k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.bootstrap_gc_challenge", fake_boot,
        raising=False,
    )
    bus = EventBus()
    sw = _swarm(
        Challenge(id="c", name="c", category="web", mode="ctf"),
        tmp_path, bus=bus,
    )

    async def health() -> list[str]:
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert called["n"] == 0
