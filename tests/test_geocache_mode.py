"""Geocache mode contract: Challenge fields and transport whitelist.

Covers model defaults/round-trip, infer/standby/dispatch mode allow-lists,
and field preservation. Does not cover gate, prompt, UI tabs, or completion.
"""

from __future__ import annotations

from muteki.models.solve_graph import Challenge


_GC_FIELDS = {
    "gc_code": "GC8ABCD",
    "posted_lat": 51.4769,
    "posted_lon": -0.0005,
    "coord_skeleton": "N 51° 2_.___ W 000° 0_.___",
    "digit_checksum": 17,
    "geocheck_url": "https://geocheck.org/geo_check.php?gid=1",
    "anchor_radius_m": 1500.0,
}


def test_challenge_defaults_remain_ctf():
    ch = Challenge(id="t", name="t", category="web")
    assert ch.mode == "ctf"
    assert ch.goal == "" and ch.scope == ""
    assert ch.gc_code == ""
    assert ch.posted_lat is None
    assert ch.posted_lon is None
    assert ch.coord_skeleton == ""
    assert ch.digit_checksum is None
    assert ch.geocheck_url == ""
    assert ch.anchor_radius_m == 3200.0


def test_challenge_geocache_construct_and_roundtrip():
    ch = Challenge(
        id="gc1", name="cache", category="misc", mode="geocache", **_GC_FIELDS,
    )
    assert ch.mode == "geocache"
    dumped = ch.model_dump()
    assert dumped["mode"] == "geocache"
    for key, value in _GC_FIELDS.items():
        assert dumped[key] == value
    restored = Challenge.model_validate(dumped)
    assert restored == ch
    assert Challenge.model_validate_json(ch.model_dump_json()) == ch


def test_normalize_challenge_mode_whitelist():
    from muteki.models.solve_graph import normalize_challenge_mode

    assert normalize_challenge_mode("geocache") == "geocache"
    assert normalize_challenge_mode("ctf") == "ctf"
    assert normalize_challenge_mode("pentest") == "pentest"
    assert normalize_challenge_mode("alien") == "ctf"
    assert normalize_challenge_mode("") == "ctf"
    assert normalize_challenge_mode(None) == "ctf"


def test_infer_challenge_keeps_explicit_geocache_and_fields():
    from apps.web.drivers import _infer_challenge

    body = _infer_challenge({
        "kind": "swarm",
        "prompt": "解 GC8ABCD",
        "challenge": {
            "mode": "geocache",
            "description": "解 GC8ABCD",
            **_GC_FIELDS,
        },
    })
    ch = body["challenge"]
    assert ch["mode"] == "geocache"
    for key, value in _GC_FIELDS.items():
        assert ch[key] == value
    from muteki.models.solve_graph import normalize_challenge_mode

    built = Challenge(
        id="t", name="t", category="misc",
        mode=normalize_challenge_mode(ch["mode"]),
        description=ch.get("description") or "",
        gc_code=ch.get("gc_code") or "",
        posted_lat=ch.get("posted_lat"),
        posted_lon=ch.get("posted_lon"),
        coord_skeleton=ch.get("coord_skeleton") or "",
        digit_checksum=ch.get("digit_checksum"),
        geocheck_url=ch.get("geocheck_url") or "",
        anchor_radius_m=ch.get("anchor_radius_m", 3200.0),
    )
    assert built.mode == "geocache"
    assert built.gc_code == "GC8ABCD"
    assert built.posted_lat == 51.4769
    assert built.anchor_radius_m == 1500.0


def test_infer_challenge_body_level_geocache_is_not_downgraded():
    from apps.web.drivers import _infer_challenge

    body = _infer_challenge({
        "prompt": "GC8ABCD mystery cache near Greenwich",
        "mode": "geocache",
        "challenge": {**_GC_FIELDS},
    })
    assert body["challenge"]["mode"] == "geocache"
    assert body["challenge"]["gc_code"] == "GC8ABCD"


def test_infer_challenge_unknown_mode_falls_back_to_ctf():
    from apps.web.drivers import _infer_challenge

    body = _infer_challenge({
        "prompt": "hello",
        "challenge": {"mode": "space-pirates", "gc_code": "GC1"},
    })
    assert body["challenge"]["mode"] == "ctf"
    assert body["challenge"]["gc_code"] == "GC1"


def test_infer_challenge_does_not_guess_geocache_from_prompt():
    from apps.web.drivers import _infer_challenge

    body = _infer_challenge({
        "prompt": "Please solve GC8ABCD with geocheck.org checksum 17",
    })
    assert body["challenge"].get("mode") in (None, "ctf")
    assert body["challenge"].get("gc_code") in (None, "")


def test_dispatch_parse_whitelist_keeps_geocache(tmp_path, monkeypatch):
    """POST /api/dispatch/parse must pass explicit geocache through to the helper."""
    from fastapi.testclient import TestClient

    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    captured: dict[str, str] = {}

    async def fake_parse(prompt, goal, mode, **_kw):
        captured["mode"] = mode
        return {"name": "gc"}

    class _FakeLLM:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    monkeypatch.delenv("MUTEKI_WEB_PASSWORD", raising=False)
    monkeypatch.setattr("apps.web.dispatch_parse.parse_dispatch", fake_parse)
    monkeypatch.setattr("muteki.core.llm.LLMClient", lambda **_k: _FakeLLM())

    client = TestClient(create_app(
        RunManager(sessions_root=tmp_path / "sessions")))
    resp = client.post("/api/dispatch/parse", json={
        "prompt": "解 GC8ABCD",
        "mode": "geocache",
    })
    assert resp.status_code == 200
    assert captured.get("mode") == "geocache"

    captured.clear()
    resp = client.post("/api/dispatch/parse", json={
        "prompt": "hello",
        "mode": "space-pirates",
    })
    assert resp.status_code == 200
    assert captured.get("mode") == "ctf"
