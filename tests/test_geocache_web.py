"""Geocache Web dispatch UX: third tab, field mapping, fail-closed backend."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from muteki.models.solve_graph import Challenge


ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "apps" / "web" / "ui"


def _read(*parts: str) -> str:
    return (UI_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


# ── source contract: third tab / dispatch mapping / result label ──────────────


def test_dispatch_opts_include_geocache_fields():
    convo = _read("components", "Conversation.tsx")
    assert "gcCode?: string" in convo
    assert "coordSkeleton?: string" in convo
    assert "digitChecksum?: number" in convo
    assert "geocheckUrl?: string" in convo
    assert "anchorRadiusM?: number" in convo
    assert 'mode: "ctf" | "pentest" | "geocache"' in convo


def test_composer_has_third_geocache_tab_and_fields():
    convo = _read("components", "Conversation.tsx")
    i18n = _read("lib", "i18n.tsx")
    assert 'role="tab"' in convo
    assert 't("composer.modeGeocache")' in convo
    assert 'pickMode("geocache")' in convo or "pickMode(" in convo
    assert '"composer.modeGeocache"' in i18n
    assert "Geocache" in i18n
    assert "gcCode" in convo
    assert "coordSkeleton" in convo
    assert "digitChecksum" in convo
    assert "geocheckUrl" in convo
    assert "anchorRadius" in convo or "anchorRadiusM" in convo
    assert "GC[A-Z0-9]+" in convo
    assert "3200" in convo


def test_geocache_dispatch_sends_snake_case_fields_not_ctf_or_pentest():
    page = _read("app", "page.tsx")
    convo = _read("components", "Conversation.tsx")
    assert 'opts?.mode === "geocache"' in page
    assert "challenge.gc_code" in page
    assert "challenge.coord_skeleton" in page
    assert "challenge.digit_checksum" in page
    assert "challenge.geocheck_url" in page
    assert "challenge.anchor_radius_m" in page
    assert "verifier_rate_limited" in page
    # geocache branch must not attach CTF collect / pentest goal
    geocache_block = page.split('opts?.mode === "geocache"')[1].split("} else {")[0]
    assert "multi_flag" not in geocache_block
    assert "flag_format" not in geocache_block
    assert "challenge.goal" not in geocache_block
    assert "challenge.scope" not in geocache_block
    assert "swarm_class" not in page
    assert "swarm_class" not in convo
    assert "解答 Geocaching Mystery" in convo or "解答 Geocaching Mystery" in page


def test_ctf_and_pentest_dispatch_bodies_unchanged():
    page = _read("app", "page.tsx")
    pentest_block = page.split('opts?.mode === "pentest"')[1].split(
        'else if (opts?.mode === "geocache")')[0]
    assert "challenge.mode = \"pentest\"" in pentest_block
    assert "challenge.goal" in pentest_block
    assert "challenge.scope" in pentest_block
    assert "gc_code" not in pentest_block
    after_gc = page.split('} else if (opts?.mode === "geocache")')[1]
    ctf_block = after_gc.split("} else {")[1].split("const worker_backend")[0]
    assert "challenge.multi_flag = true" in ctf_block
    assert "challenge.flag_format" in ctf_block
    assert "gc_code" not in ctf_block
    assert "swarm_class" not in ctf_block
    assert 'opts?.mode === "pentest"' in page


def test_result_list_label_is_verified_coord_for_geocache():
    convo = _read("components", "Conversation.tsx")
    inspector = _read("components", "RunInspector.tsx")
    i18n = _read("lib", "i18n.tsx")
    assert "已验证坐标" in i18n
    assert "hero.results.coordTitle" in i18n or "已验证坐标" in i18n
    assert 'digest.mode === "geocache"' in convo
    assert "insp.run.coord" in inspector or 'deck.mode === "geocache"' in inspector
    # underlying flags projection is not renamed
    events = _read("lib", "events.ts")
    assert "flags:" in events
    assert "s.flags" in events or "flags =" in events


def test_slash_verify_coord_uses_control_api_not_local_flag():
    convo = _read("components", "Conversation.tsx")
    use_run = _read("lib", "useRun.ts")
    assert 'raw.startsWith("/")' in convo
    assert "onCommand(cmdTarget, a, payload)" in convo
    assert "`/api/runs/${runId}/control`" in use_run
    assert "{ target, action, text }" in use_run
    assert "flag_found" not in use_run
    assert "add_evidence" not in use_run


def test_resolve_swarm_class_default_unchanged():
    from apps.web.drivers import _resolve_swarm_class
    from muteki.swarm.swarm import Swarm

    src = inspect.getsource(_resolve_swarm_class)
    assert "return Swarm" in src
    assert _resolve_swarm_class(None) is Swarm
    assert _resolve_swarm_class("") is Swarm
    assert _resolve_swarm_class("   ") is Swarm
    assert "SwarmChainForce" not in src
    assert "swarm_pex" not in src.lower()


# ── backend fail-closed validation ───────────────────────────────────────────


def test_infer_normalizes_gc_code_and_rejects_invalid_geocache_fields():
    from apps.web.drivers import _infer_challenge

    ok = _infer_challenge({
        "prompt": "解答 Geocaching Mystery GC8ABCD",
        "challenge": {
            "mode": "geocache",
            "gc_code": "gc8abcd",
            "geocheck_url": "https://geocheck.org/geo_check.php?gid=1",
            "anchor_radius_m": 1500,
            "digit_checksum": 17,
            "posted_lat": 51.5,
            "posted_lon": 0.0,
        },
    })
    ch = ok["challenge"]
    assert ch["mode"] == "geocache"
    assert ch["gc_code"] == "GC8ABCD"
    assert ch["verifier_rate_limited"] is True
    assert ch["anchor_radius_m"] == 1500

    invalid_bodies = [
        {"mode": "geocache"},
        {"mode": "geocache", "gc_code": ""},
        {"mode": "geocache", "gc_code": "   "},
        {"mode": "geocache", "gc_code": "not-a-code"},
        {"mode": "geocache", "gc_code": "GC8ABCD",
         "geocheck_url": "https://geocheck.org.evil.com/x"},
        {"mode": "geocache", "gc_code": "GC8ABCD",
         "geocheck_url": "http://geocheck.org/geo_check.php?gid=1"},
        {"mode": "geocache", "gc_code": "GC8ABCD", "anchor_radius_m": 0},
        {"mode": "geocache", "gc_code": "GC8ABCD", "anchor_radius_m": 3201},
        {"mode": "geocache", "gc_code": "GC8ABCD", "anchor_radius_m": float("nan")},
        {"mode": "geocache", "gc_code": "GC8ABCD", "digit_checksum": -1},
        {"mode": "geocache", "gc_code": "GC8ABCD", "digit_checksum": 1.5},
        {"mode": "geocache", "gc_code": "GC8ABCD",
         "posted_lat": float("inf"), "posted_lon": 0},
        {"mode": "geocache", "gc_code": "GC8ABCD",
         "posted_lat": 91.0, "posted_lon": 0.0},
    ]
    for challenge in invalid_bodies:
        with pytest.raises((ValueError, RuntimeError)) as ei:
            _infer_challenge({"prompt": "x", "challenge": challenge})
        msg = str(ei.value).lower()
        assert "geocache" in msg or "gc" in msg or "invalid" in msg
        # must not silently become CTF
        assert "downgrade" not in msg


def test_infer_does_not_silent_ctf_on_bad_geocache():
    from apps.web.drivers import _infer_challenge

    with pytest.raises((ValueError, RuntimeError)):
        body = _infer_challenge({
            "prompt": "hello",
            "challenge": {"mode": "geocache", "gc_code": "XXX"},
        })
        # if it didn't raise, mode must still not be ctf
        assert body["challenge"]["mode"] != "ctf"


def test_geocache_kwargs_and_standby_preserve_ui_fields():
    from apps.web.drivers import _geocache_challenge_kwargs, _effective_verifier_rate_limited

    ch = {
        "gc_code": "GC8ABCD",
        "posted_lat": 51.4,
        "posted_lon": -0.1,
        "coord_skeleton": "N 51 24.??? W 000 06.???",
        "digit_checksum": 8,
        "geocheck_url": "https://geocheck.org/geo_check.php?gid=1",
        "anchor_radius_m": 2000.0,
    }
    kwargs = _geocache_challenge_kwargs(ch)
    built = Challenge(id="t", name="t", category="misc", mode="geocache", **kwargs)
    assert built.gc_code == "GC8ABCD"
    assert built.posted_lat == 51.4
    assert built.coord_skeleton.startswith("N 51")
    assert built.digit_checksum == 8
    assert built.geocheck_url.endswith("gid=1")
    assert built.anchor_radius_m == 2000.0
    assert _effective_verifier_rate_limited("geocache", ch) is True


def test_driver_construction_rejects_invalid_geocache_radius():
    from apps.web.drivers import _geocache_challenge_kwargs

    with pytest.raises((ValueError, RuntimeError)):
        _geocache_challenge_kwargs({
            "gc_code": "GC8ABCD",
            "anchor_radius_m": -5,
        })


def test_infer_geocache_requires_nonempty_valid_gc_code():
    from apps.web.drivers import _infer_challenge
    from muteki.solver.gc_urls import GeocacheFieldError

    for challenge in (
        {"mode": "geocache"},
        {"mode": "geocache", "gc_code": ""},
        {"mode": "geocache", "gc_code": "   "},
        {"mode": "geocache", "gc_code": "XXX"},
    ):
        with pytest.raises((GeocacheFieldError, ValueError)):
            _infer_challenge({"prompt": "x", "challenge": challenge})


def test_start_rejects_missing_and_invalid_gc_code_before_run_creation(
    tmp_path, monkeypatch,
):
    from fastapi.testclient import TestClient

    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    monkeypatch.delenv("MUTEKI_WEB_PASSWORD", raising=False)
    mgr = RunManager(sessions_root=tmp_path / "sessions")
    client = TestClient(create_app(mgr))
    cases = [
        ("gc-start-missing", {"mode": "geocache"}),
        ("gc-start-empty", {"mode": "geocache", "gc_code": ""}),
        ("gc-start-invalid", {"mode": "geocache", "gc_code": "XXX"}),
    ]
    for run_id, challenge in cases:
        resp = client.post(
            f"/api/runs/{run_id}/start",
            json={"kind": "swarm", "prompt": "x", "challenge": challenge},
        )
        assert resp.status_code == 400, (run_id, resp.status_code, resp.text)
        assert mgr.get(run_id) is None
