"""Geocache mode contract: Challenge fields and transport whitelist.

Covers model defaults/round-trip, infer/standby/dispatch mode allow-lists,
and field preservation. Does not cover gate, prompt, UI tabs, or completion.
"""

from __future__ import annotations

from muteki.models.solve_graph import Challenge
from muteki.solver import cli_solver as _cli_solver
from muteki.solver.cli_solver import CliSolver


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


# ── Worker prompt isolation (Task 4) ─────────────────────────────────────────

def _solver(ch, **kw):
    spec = type("S", (), {"solver_id": "cli-1"})()
    return CliSolver(spec, ch, kb=False, **kw)


def _gc_challenge(**extra):
    fields = dict(_GC_FIELDS)
    fields.update(extra)
    return Challenge(
        id="gc1", name="Greenwich Mystery", category="misc",
        mode="geocache", description="解 GC8ABCD mystery",
        **fields,
    )


_CTF_IDENTIFIERS = (
    "You are an expert CTF solver working a BLACK-BOX challenge with a FULL shell",
    "python3 \"$MUTEKI_BLACKBOARD_SCRIPT\" submit-flag '<the flag>'",
    "Ordinary reply text and FOUND_FLAG= do not submit a result.",
)
_PENTEST_IDENTIFIERS = (
    "You are an expert penetration tester with a FULL shell",
    "SUBMIT_REPORT=",
)
_GC_FORBIDDEN = (
    "FOUND_FLAG=<the flag>",
    "SUBMIT_REPORT",
    "submit-flag",
)


def test_ctf_and_pentest_prompt_constants_unchanged():
    for needle in _CTF_IDENTIFIERS:
        assert needle in _cli_solver._EXEC_PROMPT
    assert "You are an expert CTF solver with a FULL shell" in _cli_solver._EXPLORE_PROMPT
    assert "submit-flag" in _cli_solver._EXPLORE_PROMPT
    assert "CONCLUDE: stop exploring now. If you already saw a correctly-formatted flag" in _cli_solver._RESUME_PROMPT
    assert "submit-flag" in _cli_solver._RESUME_PROMPT
    assert "You are the Review-Arbiter for a CTF/pentest-solving swarm" in _cli_solver._REVIEW_PROMPT
    assert "Never submit a flag." in _cli_solver._REVIEW_PROMPT
    for needle in _PENTEST_IDENTIFIERS:
        assert needle in _cli_solver._PENTEST_EXEC_PROMPT
    assert "SUBMIT_REPORT=" in _cli_solver._PENTEST_EXPLORE_PROMPT
    assert "Do not submit a flag." in _cli_solver._PENTEST_EXPLORE_PROMPT
    assert "SUBMIT_REPORT=" in _cli_solver._PENTEST_RESUME_PROMPT
    assert "Do not submit a flag." in _cli_solver._PENTEST_RESUME_PROMPT


def test_infer_geocache_checker_url_sets_verifier_rate_limited():
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
    assert body["challenge"]["verifier_rate_limited"] is True
    assert body["challenge"]["geocheck_url"] == _GC_FIELDS["geocheck_url"]


def test_build_prompt_geocache_includes_listing_and_submit_coord():
    p = _solver(_gc_challenge())._build_prompt()
    assert "GC8ABCD" in p
    assert "51.4769" in p
    assert "-0.0005" in p
    assert "N 51° 2_.___ W 000° 0_.___" in p
    assert "17" in p
    assert "https://geocheck.org/geo_check.php?gid=1" in p
    assert "submit-coord" in p
    assert "D3" in p and "D4" in p
    assert "3.2" in p or "3200" in p or "1500" in p
    assert "cipher" in p.lower()
    assert "projection" in p.lower()
    assert "lateral" in p.lower()
    assert "read-deadends" in p
    assert "coord_calc.py" in p
    for needle in _GC_FORBIDDEN:
        assert needle not in p
    assert "offline" not in p.lower()
    assert "writeup" in p.lower()
    assert "READY_TO_SUBMIT=" in p
    assert "verifier:geocheck@" in p
    assert "不要运行" in p or "do not run" in p.lower()
    assert "before `gc check`" not in p


def test_build_prompt_ctf_and_pentest_stay_isolated_from_gc():
    ctf = _solver(Challenge(id="t", name="t", category="web", target="http://x"))._build_prompt()
    pentest = _solver(Challenge(
        id="t", name="acme", category="web", target="http://x",
        mode="pentest", goal="find SQLi in /login", scope="only acme.test",
    ))._build_prompt()
    for needle in _CTF_IDENTIFIERS:
        assert needle in ctf
    assert "FOUND_FLAG=" in ctf
    assert "submit-coord" not in ctf
    assert "GC8ABCD" not in ctf
    assert "cipher / projection / lateral" not in ctf.lower()
    assert "penetration tester" in pentest
    assert "SUBMIT_REPORT=" in pentest
    assert "submit-coord" not in pentest
    assert "FOUND_FLAG=<the flag>" not in pentest


def test_explore_prompt_geocache_mode_isolation():
    gc = _solver(_gc_challenge(), intent_goal="decode the clock face cipher")._build_explore_prompt()
    ctf = _solver(
        Challenge(id="t", name="t", category="web"),
        intent_goal="probe /login",
    )._build_explore_prompt()
    pentest = _solver(
        Challenge(id="t", name="acme", category="web", mode="pentest",
                  goal="find RCE", scope="acme.test"),
        intent_goal="try default creds",
    )._build_explore_prompt()

    assert "decode the clock face cipher" in gc
    assert "GC8ABCD" in gc
    assert "submit-coord" in gc
    assert "N 51° 2_.___ W 000° 0_.___" in gc
    for needle in _GC_FORBIDDEN:
        assert needle not in gc

    assert "probe /login" in ctf
    assert "submit-flag" in ctf
    assert "submit-coord" not in ctf
    assert "FOUND_FLAG=" in ctf or "submit-flag" in ctf

    assert "try default creds" in pentest
    assert "SUBMIT_REPORT=" in pentest
    assert "submit-coord" not in pentest


def test_review_prompt_geocache_mode_isolation():
    gc = _solver(_gc_challenge(), intent_goal="audit cipher loops")._build_review_prompt()
    ctf = _solver(Challenge(id="t", name="t", category="web"))._build_review_prompt()
    pentest = _solver(Challenge(
        id="t", name="acme", category="web", mode="pentest",
        goal="find RCE", scope="only acme.test",
    ))._build_review_prompt()

    assert "audit cipher loops" in gc
    assert "GC8ABCD" in gc
    assert "submit-coord" in gc
    assert "cipher" in gc.lower()
    for needle in _GC_FORBIDDEN:
        assert needle not in gc

    assert "You are the Review-Arbiter for a CTF/pentest-solving swarm" in ctf
    assert "Never submit a flag." in ctf
    assert "submit-coord" not in ctf
    assert "GC8ABCD" not in ctf
    assert "Engagement goal" in pentest
    assert "only acme.test" in pentest
    assert "submit-coord" not in pentest


def test_resume_prompt_geocache_mode_isolation():
    gc = _solver(_gc_challenge())._resume_text()
    ctf = _solver(Challenge(id="t", name="t", category="web"))._resume_text()
    pentest = _solver(Challenge(
        id="t", name="acme", category="web", mode="pentest", goal="find RCE",
    ))._resume_text()

    assert gc == _cli_solver._GC_RESUME_PROMPT
    assert "submit-coord" in gc
    for needle in _GC_FORBIDDEN:
        assert needle not in gc
    assert ctf == _cli_solver._RESUME_PROMPT
    assert "submit-flag" in ctf
    assert pentest == _cli_solver._PENTEST_RESUME_PROMPT
    assert "SUBMIT_REPORT=" in pentest


def test_explore_conclude_text_three_modes():
    gc = _solver(_gc_challenge())._explore_conclude_text()
    ctf = _solver(Challenge(id="t", name="t", category="web"))._explore_conclude_text()
    pentest = _solver(Challenge(
        id="t", name="acme", category="web", mode="pentest", goal="find RCE",
    ))._explore_conclude_text()

    assert gc == _cli_solver._GC_EXPLORE_CONCLUDE_PROMPT
    assert "submit-coord" in gc
    for needle in _GC_FORBIDDEN:
        assert needle not in gc
    assert ctf == _cli_solver._EXPLORE_CONCLUDE_PROMPT
    assert "submit-flag" in ctf
    assert pentest == _cli_solver._PENTEST_EXPLORE_CONCLUDE_PROMPT
    assert "SUBMIT_REPORT=" in pentest


def test_geocache_omits_flag_team_and_rejected_blocks_even_if_multiflag():
    class _Graph:
        def invalidated_flags(self):
            return {"flag{false_positive}"}

        def snapshot(self):
            return type("S", (), {"flags": ["flag{one}"]})()

    ch = _gc_challenge(expected_flags=3)
    s = _solver(ch, shared_graph=_Graph())
    assert s._team_context_block() == ""
    assert s._rejected_flags_block() == ""
    bootstrap = s._build_prompt()
    explore = s._build_explore_prompt()
    for prompt in (bootstrap, explore):
        assert "submit-flag" not in prompt
        assert "This challenge has 3 flags" not in prompt
        assert "Known-BAD flags" not in prompt
        assert "submit-coord" in prompt


def test_geocache_listing_uses_effective_anchor_radius():
    custom = _solver(_gc_challenge(anchor_radius_m=1500.0))._geocache_listing_block()
    assert "1500" in custom
    assert "1.5 km" in custom
    assert "3.2" not in custom
    assert "3200" not in custom

    default = _solver(_gc_challenge(anchor_radius_m=3200.0))._geocache_listing_block()
    assert "3200" in default
    assert "3.2 km" in default
    assert "1.5 km" not in default
