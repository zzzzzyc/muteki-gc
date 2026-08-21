"""Host-side geocache checker: only Muteki-started ``gc check`` JSON can verify.

No test in this module talks to the network or launches Playwright. Fake
executables and an injected runner stand in for the CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import textwrap
import time
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from muteki.models.solve_graph import Challenge
from muteki.solver.cli_solver import CliSolver
from muteki.solver.gc_checker import (
    ExternalCoordVerdict,
    _reset_checker_state_for_tests,
    verify_external_coordinate,
)
from muteki.vendor.geocaching_cli.coord import format_dmm, parse_coord

from tests.test_gc_gate import (
    _SLOT_CANDIDATE,
    _SLOT_CHECKSUM,
    _SLOT_DMM,
    _CaptureBus,
    _cli,
    _gc,
    _last_decision,
    _run_blackboard,
    _seed_output,
    _submit_and_drain,
)

_CHECK_URL = "https://geocheck.org/geo_check.php?gid=should-never-fetch"


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _payload(
    coord: str = _SLOT_DMM,
    *,
    ok: bool = True,
    definitive: bool = True,
    site: str = "geocheck",
    message: str = "Looks good",
    attempts: int = 1,
    extra: dict | None = None,
) -> dict:
    body = {
        "ok": ok,
        "definitive": definitive,
        "coord_text": coord,
        "site": site,
        "message": message,
        "attempts": attempts,
    }
    if extra:
        body.update(extra)
    return body


def _stdout(coord: str = _SLOT_DMM, **kw) -> str:
    return json.dumps(_payload(coord, **kw))


def _write_exec(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _echo_gc(tmp_path: Path, *, returncode: int = 0, stdout: str = "", stderr: str = "") -> Path:
    """Fake ``gc`` that records argv/env/cwd and prints a scripted result."""
    out_path = tmp_path / "gc-stdout.txt"
    err_path = tmp_path / "gc-stderr.txt"
    out_path.write_text(stdout, encoding="utf-8")
    err_path.write_text(stderr, encoding="utf-8")
    script = tmp_path / "gc"
    _write_exec(
        script,
        f"""\
        #!/usr/bin/env python3
        import json, os, sys
        from pathlib import Path
        root = Path({str(tmp_path)!r})
        (root / "gc-argv.json").write_text(json.dumps(sys.argv), encoding="utf-8")
        (root / "gc-cwd.txt").write_text(os.getcwd(), encoding="utf-8")
        env = {{
            key: os.environ.get(key, "")
            for key in os.environ
            if key.startswith((
                "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "MUTEKI_DEEPSEEK_",
                "CURSOR_API_KEY", "XAI_", "GROK_", "GEOCACHING_",
                "HOME", "PATH",
            )) or key in ("CURSOR_API_KEY",)
        }}
        (root / "gc-env.json").write_text(json.dumps(env), encoding="utf-8")
        sys.stdout.write(Path({str(out_path)!r}).read_text(encoding="utf-8"))
        sys.stderr.write(Path({str(err_path)!r}).read_text(encoding="utf-8"))
        raise SystemExit({returncode})
        """,
    )
    return script


@pytest.fixture(autouse=True)
def _isolate_checker(monkeypatch):
    monkeypatch.setenv("MUTEKI_GC_CLI", "/nonexistent/muteki-gc-checker-sentinel")
    _reset_checker_state_for_tests()
    yield
    _reset_checker_state_for_tests()


def _run(coro):
    return asyncio.run(coro)


# ── executable resolution / no shell ─────────────────────────────────────────


def test_skips_when_not_geocache_or_url_empty():
    async def boom(*_a, **_k):
        raise AssertionError("runner must not start")

    ctf = Challenge(id="ctf", name="t", category="web", mode="ctf",
                    geocheck_url=_CHECK_URL)
    empty = _gc(geocheck_url="")
    for ch in (ctf, empty):
        verdict = _run(verify_external_coordinate(
            ch, _SLOT_DMM, executable="/bin/true", runner=boom))
        assert verdict.verified is False
        assert verdict.definitive is False
        assert _has_cjk(verdict.message)


def test_executable_precedence_explicit_then_env_then_which(tmp_path, monkeypatch):
    seen: list[str] = []

    def make_bin(name: str) -> Path:
        path = tmp_path / name
        _write_exec(
            path,
            f"""\
            #!/usr/bin/env python3
            import json, sys
            from pathlib import Path
            Path({str(tmp_path / (name + '.ran'))!r}).write_text({name!r})
            sys.stdout.write({_stdout()!r})
            """,
        )
        return path

    explicit = make_bin("explicit-gc")
    env_bin = make_bin("env-gc")
    which_bin = make_bin("which-gc")
    monkeypatch.setenv("MUTEKI_GC_CLI", str(env_bin))
    monkeypatch.setattr(
        "muteki.solver.gc_checker.shutil.which",
        lambda name: str(which_bin) if name == "gc" else None,
    )

    async def _check(executable=None):
        return await verify_external_coordinate(
            _gc(), _SLOT_DMM, executable=executable)

    assert _run(_check(str(explicit))).verified is True
    assert (tmp_path / "explicit-gc.ran").read_text() == "explicit-gc"
    assert not (tmp_path / "env-gc.ran").exists()

    (tmp_path / "explicit-gc.ran").unlink(missing_ok=True)
    _reset_checker_state_for_tests()
    assert _run(_check(None)).verified is True
    assert (tmp_path / "env-gc.ran").read_text() == "env-gc"
    assert not (tmp_path / "which-gc.ran").exists()

    monkeypatch.delenv("MUTEKI_GC_CLI")
    _reset_checker_state_for_tests()
    assert _run(_check(None)).verified is True
    assert (tmp_path / "which-gc.ran").read_text() == "which-gc"
    seen.append("ok")
    assert seen == ["ok"]


def test_rejects_relative_or_non_file_executable(tmp_path):
    rel = Path("not-abs-gc")
    directory = tmp_path / "gc-dir"
    directory.mkdir()
    for executable in (str(rel), str(directory), str(tmp_path / "missing-gc")):
        verdict = _run(verify_external_coordinate(
            _gc(), _SLOT_DMM, executable=executable))
        assert verdict.verified is False
        assert verdict.definitive is False
        assert _has_cjk(verdict.message)


def test_runs_without_shell_and_uses_fresh_cwd(tmp_path):
    script = _echo_gc(tmp_path, stdout=_stdout())
    worker_cwd = tmp_path / "worker-cwd"
    worker_cwd.mkdir()
    previous = os.getcwd()
    try:
        os.chdir(worker_cwd)
        verdict = _run(verify_external_coordinate(
            _gc(), _SLOT_DMM, executable=str(script)))
    finally:
        os.chdir(previous)
    assert verdict.verified is True
    argv = json.loads((tmp_path / "gc-argv.json").read_text(encoding="utf-8"))
    assert argv[0] == str(script.resolve())
    assert argv[1:] == [
        "check", "--url", _CHECK_URL, _SLOT_DMM, "--headless", "--json",
    ]
    cwd = Path((tmp_path / "gc-cwd.txt").read_text(encoding="utf-8"))
    assert cwd != worker_cwd
    assert cwd != tmp_path
    assert not str(cwd).startswith(str(worker_cwd))


# ── env secret stripping ─────────────────────────────────────────────────────


def test_strips_model_secrets_but_keeps_geocaching_and_path(tmp_path, monkeypatch):
    secret = "sk-secret-must-not-leak"
    cookie = "GEO_COOKIE_DUMMY"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    monkeypatch.setenv("MUTEKI_DEEPSEEK_KEY", secret)
    monkeypatch.setenv("CURSOR_API_KEY", secret)
    monkeypatch.setenv("XAI_API_KEY", secret)
    monkeypatch.setenv("GROK_API_KEY", secret)
    monkeypatch.setenv("GEOCACHING_COOKIE", cookie)
    monkeypatch.setenv("GEOCACHING_USERNAME", "alice")
    home = os.environ.get("HOME") or str(Path.home())
    script = _echo_gc(
        tmp_path,
        stdout=_stdout(),
        stderr=f"Authorization: Bearer {secret}\n",
    )
    verdict = _run(verify_external_coordinate(
        _gc(), _SLOT_DMM, executable=str(script)))
    assert verdict.verified is True
    env = json.loads((tmp_path / "gc-env.json").read_text(encoding="utf-8"))
    for key in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
        "MUTEKI_DEEPSEEK_KEY", "CURSOR_API_KEY", "XAI_API_KEY", "GROK_API_KEY",
    ):
        assert key not in env or env[key] == ""
    assert env["GEOCACHING_COOKIE"] == cookie
    assert env["GEOCACHING_USERNAME"] == "alice"
    assert env["HOME"] == home
    assert env["PATH"]
    assert secret not in verdict.message
    assert cookie not in verdict.message
    assert "Authorization" not in verdict.message


# ── JSON object + return-code matrix ─────────────────────────────────────────


def test_stdout_must_be_exactly_one_json_object(tmp_path):
    script = _echo_gc(tmp_path, stdout=_stdout() + "\ntrailing prose\n")
    verdict = _run(verify_external_coordinate(
        _gc(), _SLOT_DMM, executable=str(script)))
    assert verdict == ExternalCoordVerdict(
        False, False, verdict.message)
    assert _has_cjk(verdict.message)
    assert "trailing prose" not in verdict.message
    assert _stdout() not in verdict.message


@pytest.mark.parametrize(
    "stdout",
    [
        json.dumps([_payload()]),
        "not-json",
        "",
        _stdout()[:-1],
        json.dumps({k: v for k, v in _payload().items() if k != "attempts"}),
        json.dumps({**_payload(), "ok": "true"}),
        json.dumps({**_payload(), "attempts": True}),
        json.dumps({**_payload(), "attempts": 1.5}),
    ],
)
def test_malformed_json_is_non_definitive(tmp_path, stdout):
    script = _echo_gc(tmp_path, stdout=str(stdout))
    verdict = _run(verify_external_coordinate(
        _gc(), _SLOT_DMM, executable=str(script)))
    assert verdict.verified is False
    assert verdict.definitive is False
    assert _has_cjk(verdict.message)
    leaked = str(stdout).strip()
    if leaked:
        assert leaked not in verdict.message


def test_returncode_result_matrix(tmp_path):
    cases = [
        (0, True, True, True, True),
        (1, False, True, False, True),
        (3, True, True, False, False),
        (0, True, False, False, False),
        (1, False, False, False, False),
        (2, False, True, False, False),
        (0, False, True, False, False),
    ]
    for idx, (rc, ok, definitive, expect_v, expect_d) in enumerate(cases):
        _reset_checker_state_for_tests()
        case_dir = tmp_path / f"case-{idx}"
        case_dir.mkdir()
        script = _echo_gc(
            case_dir,
            returncode=rc,
            stdout=_stdout(ok=ok, definitive=definitive),
        )
        verdict = _run(verify_external_coordinate(
            _gc(), _SLOT_DMM, executable=str(script)))
        assert verdict.verified is expect_v, (rc, ok, definitive, verdict)
        assert verdict.definitive is expect_d, (rc, ok, definitive, verdict)
        assert _has_cjk(verdict.message)


def test_coordinate_mismatch_cannot_verify(tmp_path):
    other = format_dmm(parse_coord("N 51 30.999 E 0 00.001"))
    script = _echo_gc(tmp_path, stdout=_stdout(other))
    verdict = _run(verify_external_coordinate(
        _gc(), _SLOT_DMM, executable=str(script)))
    assert verdict.verified is False
    assert verdict.definitive is False
    assert other not in verdict.message
    assert _SLOT_DMM not in verdict.message or _has_cjk(verdict.message)


def test_normalized_coord_text_is_accepted(tmp_path):
    raw = "N 51° 30.123 E 000° 00.456"
    script = _echo_gc(tmp_path, stdout=_stdout(raw))
    verdict = _run(verify_external_coordinate(
        _gc(), _SLOT_DMM, executable=str(script)))
    assert verdict.verified is True
    assert verdict.definitive is True


def test_timeout_is_non_definitive(tmp_path):
    sleeper = tmp_path / "sleep-gc"
    _write_exec(
        sleeper,
        """\
        #!/usr/bin/env python3
        import time
        time.sleep(30)
        """,
    )
    started = time.monotonic()
    verdict = _run(verify_external_coordinate(
        _gc(), _SLOT_DMM, executable=str(sleeper), timeout_s=0.35))
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    assert verdict.verified is False
    assert verdict.definitive is False
    assert _has_cjk(verdict.message)


def test_missing_binary_is_non_definitive(monkeypatch):
    monkeypatch.delenv("MUTEKI_GC_CLI", raising=False)
    monkeypatch.setattr("muteki.solver.gc_checker.shutil.which", lambda _n: None)
    verdict = _run(verify_external_coordinate(_gc(), _SLOT_DMM))
    assert verdict.verified is False
    assert verdict.definitive is False
    assert _has_cjk(verdict.message)


# ── serialization / cache ────────────────────────────────────────────────────


def test_concurrent_duplicates_run_once():
    calls = 0

    async def runner(argv, **_kw):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return CompletedProcess(argv, 0, stdout=_stdout(), stderr="")

    async def both():
        ch = _gc()
        return await asyncio.gather(
            verify_external_coordinate(ch, _SLOT_DMM, runner=runner),
            verify_external_coordinate(ch, _SLOT_DMM, runner=runner),
        )

    first, second = _run(both())
    assert calls == 1
    assert first.verified is True and second.verified is True


def test_definitive_cache_hit_and_non_definitive_retries():
    calls = 0
    mode = {"ok": True}

    async def runner(argv, **_kw):
        nonlocal calls
        calls += 1
        if mode["ok"]:
            return CompletedProcess(argv, 0, stdout=_stdout(), stderr="")
        return CompletedProcess(argv, 3, stdout=_stdout(ok=False, definitive=False), stderr="")

    ch = _gc()
    assert _run(verify_external_coordinate(ch, _SLOT_DMM, runner=runner)).verified
    assert _run(verify_external_coordinate(ch, _SLOT_DMM, runner=runner)).verified
    assert calls == 1

    _reset_checker_state_for_tests()
    calls = 0
    mode["ok"] = False
    first = _run(verify_external_coordinate(ch, _SLOT_DMM, runner=runner))
    second = _run(verify_external_coordinate(ch, _SLOT_DMM, runner=runner))
    assert first.verified is False and first.definitive is False
    assert second.verified is False and second.definitive is False
    assert calls == 2


def test_different_challenge_and_coord_serialize_and_cache_independently():
    active = 0
    peak = 0
    calls: list[tuple[str, str]] = []

    async def runner(argv, **_kw):
        nonlocal active, peak
        url = argv[argv.index("--url") + 1]
        coord = argv[argv.index("--url") + 2]
        calls.append((url, coord))
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.08)
        active -= 1
        return CompletedProcess(argv, 0, stdout=_stdout(coord), stderr="")

    ch_a = _gc(id="cache-a", geocheck_url=_CHECK_URL + "&a=1")
    ch_b = _gc(id="cache-b", geocheck_url=_CHECK_URL + "&b=2")
    other = format_dmm(parse_coord("N 51 30.200 E 0 00.200"))

    async def mixed():
        return await asyncio.gather(
            verify_external_coordinate(ch_a, _SLOT_DMM, runner=runner),
            verify_external_coordinate(ch_b, _SLOT_DMM, runner=runner),
        )

    a_res, b_res = _run(mixed())
    assert a_res.verified and b_res.verified
    assert peak >= 2
    assert len(calls) == 2

    overlap = 0
    in_flight = 0

    async def serial_runner(argv, **_kw):
        nonlocal overlap, in_flight
        in_flight += 1
        overlap = max(overlap, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        coord = argv[argv.index("--url") + 2]
        return CompletedProcess(argv, 0, stdout=_stdout(coord), stderr="")

    _reset_checker_state_for_tests()

    async def same_challenge_two_coords():
        return await asyncio.gather(
            verify_external_coordinate(ch_a, _SLOT_DMM, runner=serial_runner),
            verify_external_coordinate(ch_a, other, runner=serial_runner),
        )

    r1, r2 = _run(same_challenge_two_coords())
    assert r1.verified and r2.verified
    assert overlap == 1


# ── CliSolver integration ────────────────────────────────────────────────────


def _patch_verify(monkeypatch, verdict: ExternalCoordVerdict):
    async def fake(challenge, coord_text, **_kw):
        fake.calls.append((challenge.id, coord_text, challenge.geocheck_url))
        return verdict

    fake.calls = []
    monkeypatch.setattr(
        "muteki.solver.cli_solver.verify_external_coordinate", fake)
    return fake


def test_worker_ok_true_prose_cannot_verify(tmp_path, monkeypatch):
    receipt = _stdout()
    ch = _gc(digit_checksum=None, geocheck_url="")
    solver = _cli(ch, tmp_path, bus=_CaptureBus())
    _seed_output(solver, f"worksheet {_SLOT_CANDIDATE}\n{receipt}")
    fake = _patch_verify(monkeypatch, ExternalCoordVerdict(True, True, "不应调用"))
    _submit_and_drain(solver)
    assert fake.calls == []
    assert solver.graph.flags == []
    assert solver.shared_graph.snapshot().flags == []
    assert _last_decision(solver)["code"] == "coord_candidate"
    assert _last_decision(solver)["accepted"] is True
    dumped = json.dumps(solver.shared_graph.events(), ensure_ascii=False)
    assert '{"ok": true' not in dumped and '"ok":true' not in dumped.replace(" ", "")


def test_checksum_match_skips_external_runner(tmp_path, monkeypatch):
    fake = _patch_verify(monkeypatch, ExternalCoordVerdict(False, False, "不应调用"))
    solver = _cli(_gc(digit_checksum=_SLOT_CHECKSUM), tmp_path, bus=_CaptureBus())
    _seed_output(solver, f"worksheet {_SLOT_CANDIDATE}")
    _submit_and_drain(solver)
    assert fake.calls == []
    assert solver.graph.flags == [_SLOT_DMM]
    assert _last_decision(solver)["code"] == "coord_verified"


def test_operator_launder_skips_external_runner(tmp_path, monkeypatch):
    fake = _patch_verify(monkeypatch, ExternalCoordVerdict(True, True, "不应调用"))
    solver = _cli(_gc(digit_checksum=None), tmp_path, bus=_CaptureBus())
    solver._remember_operator_context(f"hint: try {_SLOT_CANDIDATE}")
    _seed_output(solver, f"echoed {_SLOT_CANDIDATE}")
    _submit_and_drain(solver)
    assert fake.calls == []
    assert solver.graph.flags == []
    assert _last_decision(solver)["code"] == "coord_rejected"


def test_origin_taint_skips_external_and_stays_candidate(tmp_path, monkeypatch):
    fake = _patch_verify(monkeypatch, ExternalCoordVerdict(True, True, "不应调用"))
    solver = _cli(_gc(digit_checksum=None), tmp_path, bus=_CaptureBus())
    solver._persist_raw_tool_output(
        f"solution: the coord is {_SLOT_CANDIDATE}",
        command="curl -sL https://writeups.example/gc.html",
    )
    _submit_and_drain(solver)
    assert fake.calls == []
    assert solver.graph.flags == []
    decision = _last_decision(solver)
    assert decision["accepted"] is True
    assert decision["code"] == "coord_candidate"
    assert "升级" in decision["detail"] or "校验" in decision["detail"]


def test_external_success_reaches_graph_flag(tmp_path, monkeypatch):
    fake = _patch_verify(
        monkeypatch, ExternalCoordVerdict(True, True, "外部校验通过"))
    solver = _cli(_gc(digit_checksum=None), tmp_path, bus=_CaptureBus())
    _seed_output(solver, f"worksheet {_SLOT_CANDIDATE}")
    _submit_and_drain(solver)
    assert fake.calls
    assert fake.calls[0][1] == _SLOT_DMM
    assert solver.graph.flags == [_SLOT_DMM]
    assert solver.shared_graph.snapshot().flags == [_SLOT_DMM]
    assert _SLOT_DMM in solver._validated_coord_submissions
    assert _last_decision(solver)["code"] == "coord_verified"
    dumped = json.dumps(solver.shared_graph.events(), ensure_ascii=False)
    assert "Looks good" not in dumped


def test_external_reject_and_unavailable_stay_candidate(tmp_path, monkeypatch):
    raw_secret = '{"ok":false,"message":"COOKIE=abc Authorization: Bearer x"}'
    fake = _patch_verify(
        monkeypatch, ExternalCoordVerdict(False, True, "外部校验未通过"))
    rejected = _cli(_gc(id="rej", digit_checksum=None), tmp_path / "rej",
                    bus=_CaptureBus())
    _seed_output(rejected, f"worksheet {_SLOT_CANDIDATE}\n{raw_secret}")
    _submit_and_drain(rejected)
    assert rejected.graph.flags == []
    decision = _last_decision(rejected)
    assert decision["accepted"] is True
    assert decision["code"] == "coord_candidate_checker_rejected"
    dumped = json.dumps(rejected.shared_graph.events(), ensure_ascii=False)
    assert "COOKIE=abc" not in dumped
    assert "Authorization" not in dumped
    assert raw_secret not in dumped
    evidence = rejected.shared_graph.snapshot().evidence
    assert any("未通过" in (ev.witness or "") or "未通过" in ev.fact for ev in evidence)

    fake2 = _patch_verify(
        monkeypatch, ExternalCoordVerdict(False, False, "外部校验暂时不可用"))
    down = _cli(_gc(id="down", digit_checksum=None), tmp_path / "down",
                bus=_CaptureBus())
    _seed_output(down, f"worksheet {_SLOT_CANDIDATE}")
    _submit_and_drain(down)
    assert down.graph.flags == []
    assert _last_decision(down)["code"] == "coord_candidate_checker_unavailable"
    assert fake2.calls


def test_infer_with_checker_url_sets_verifier_rate_limited():
    from apps.web.drivers import _infer_challenge

    with_url = _infer_challenge({
        "prompt": "解 GC8ABCD",
        "challenge": {
            "mode": "geocache",
            "geocheck_url": _CHECK_URL,
        },
    })
    assert with_url["challenge"]["mode"] == "geocache"
    assert with_url["challenge"]["verifier_rate_limited"] is True

    without = _infer_challenge({
        "prompt": "解 GC8ABCD",
        "challenge": {"mode": "geocache", "geocheck_url": ""},
    })
    assert without["challenge"].get("verifier_rate_limited") in (None, False)

    explicit = Challenge(
        id="direct", name="t", category="misc", mode="geocache",
        geocheck_url=_CHECK_URL,
    )
    assert explicit.verifier_rate_limited is False


def test_prompt_and_skill_say_host_submits_worker_does_not_run_checker():
    from muteki.solver import cli_solver as cs

    ch = Challenge(
        id="gc1", name="cache", category="misc", mode="geocache",
        gc_code="GC8ABCD", geocheck_url=_CHECK_URL,
        posted_lat=51.5, posted_lon=0.0,
        verifier_rate_limited=True,
    )
    spec = type("S", (), {"solver_id": "cli-1"})()
    solver = CliSolver(spec, ch, kb=False)
    prompts = [
        solver._build_prompt(),
        solver._build_explore_prompt(),
        cs._GC_EXEC_PROMPT,
        cs._GC_EXPLORE_PROMPT,
    ]
    skill = (
        Path(__file__).resolve().parents[1]
        / "skills" / "gc-blackboard" / "SKILL.md"
    ).read_text(encoding="utf-8")
    agents = (
        Path(__file__).resolve().parents[1]
        / "docker" / "worker" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    blobs = prompts + [skill, agents]
    for text in blobs:
        assert "READY_TO_SUBMIT=" in text or "READY_TO_SUBMIT" in text or text is agents
        assert "verifier:geocheck@" in text
        assert "submit-coord" in text
        assert "gc check" in text
        lowered = text.lower()
        assert (
            "host" in lowered
            or "主机" in text
            or "由 host" in text
            or "Muteki host" in text
            or "本机 host" in text
            or "由 Muteki" in text
        )
        assert (
            "不要自行运行" in text
            or "不要自己运行" in text
            or "do not run `gc check`" in lowered
            or "do not run gc check" in lowered
            or "不必运行 gc check" in text
            or "不要运行 `gc check`" in text
            or "不要运行 gc check" in text
        )
    exec_prompt = solver._build_prompt()
    assert "before `gc check`" not in exec_prompt
    assert "then run the verifier ONCE" not in exec_prompt
