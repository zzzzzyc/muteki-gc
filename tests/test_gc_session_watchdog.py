"""Geocaching session watchdog and HITL request lifecycle."""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from muteki.core.event_bus import EventBus
from muteki.core.events import Event, EventType, hitl_request_payload
from muteki.core.llm import ModelSpec
from muteki.models.solve_graph import Challenge
from muteki.sandbox.manager import SandboxManager
from muteki.solver.result import ArtifactStore
from muteki.swarm.swarm import Swarm


ROOT = Path(__file__).resolve().parents[1]
UI_EVENTS = ROOT / "apps" / "web" / "ui" / "lib" / "events.ts"

GC_WORKER = "gc-session-watchdog"
GC_NEED = "Geocaching 会话已过期，请运行 gc auth login"
GC_NEED_KIND = "external_blocker"
GC_KIND = "env_down"
GC_REASON = "Geocaching 会话已恢复"
DEFAULT_STATUS_URL = "http://127.0.0.1:8765/api/status"

ALLOWED_URLS = (
    DEFAULT_STATUS_URL,
    "http://127.0.0.1:1/api/status",
    "http://127.0.0.1:80/api/status",
    "http://127.0.0.1:65535/api/status",
    "http://[::1]:8765/api/status",
    "http://[::1]:1/api/status",
    "http://[::1]:65535/api/status",
)
REJECTED_URLS = (
    "http://localhost:8765/api/status",
    "http://127.0.0.1/api/status",
    "http://127.0.0.1:0/api/status",
    "http://127.0.0.1:65536/api/status",
    "http://127.0.0.1:8765/api/status?x=1",
    "http://127.0.0.1:8765/api/status#frag",
    "http://user:pass@127.0.0.1:8765/api/status",
    "http://127.0.0.1:8765/api/other",
    "http://127.0.0.1:8765/api/status/",
    "https://127.0.0.1:8765/api/status",
    "http://8.8.8.8:8765/api/status",
    "http://[::1]/api/status",
    "http://127.0.0.1:08765/api/status",
    "http://127.0.0.1:8765/api/status/extra",
)


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> Event:
        self.events.append(event)
        return event


def _challenge(mode: str = "geocache") -> Challenge:
    return Challenge(
        id=f"c-{mode}",
        name=f"{mode}-watchdog",
        category="misc",
        mode=mode,
        description="gc session watchdog",
    )


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
        **kw,
    )


def _watchdog_tasks() -> list[asyncio.Task]:
    return [
        t for t in asyncio.all_tasks()
        if t.get_name() == "gc-session-watchdog"
    ]


def _stable_request_id() -> str:
    return hitl_request_payload(
        GC_WORKER, GC_NEED, kind=GC_KIND, need_kind=GC_NEED_KIND,
    )["request_id"]


async def _drive_watch(statuses: list[Any], bus: Any | None = None, **kw: Any) -> Any:
    bus = RecordingBus() if bus is None else bus
    from muteki.solver.gc_session_watchdog import watch_gc_session

    i = {"n": 0}

    async def fetcher(_url: str) -> dict[str, Any]:
        n = i["n"]
        i["n"] += 1
        item = statuses[n] if n < len(statuses) else statuses[-1]
        if isinstance(item, BaseException):
            raise item
        return item

    async def sleep(_seconds: float) -> None:
        if i["n"] >= len(statuses):
            raise asyncio.CancelledError
        return None

    with pytest.raises(asyncio.CancelledError):
        await watch_gc_session(
            bus,
            run_id="run-gc",
            challenge_id="c-geocache",
            fetcher=fetcher,
            sleep=sleep,
            interval_s=0.01,
            **kw,
        )
    return bus


def _patch_hanging_watch(monkeypatch: pytest.MonkeyPatch, finished: dict[str, bool] | None = None):
    started = asyncio.Event()
    if finished is not None:
        finished["started_event"] = started

    async def _hang(*_a: Any, **_k: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if finished is not None:
                finished["cancelled"] = True
            raise
        finally:
            if finished is not None:
                finished["done"] = True

    monkeypatch.setattr(
        "muteki.solver.gc_session_watchdog.watch_gc_session",
        _hang,
        raising=False,
    )
    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.watch_gc_session",
        _hang,
        raising=False,
    )
    return _hang


# ── Event contract ──────────────────────────────────────────────────────────


def test_hitl_resolved_event_type_and_payload() -> None:
    from muteki.core.events import hitl_resolved_payload

    assert EventType.HITL_RESOLVED == "hitl.resolved"
    payload = hitl_resolved_payload(
        "H-abc123", worker=GC_WORKER, reason=GC_REASON,
    )
    assert payload["request_id"] == "H-abc123"
    assert payload["worker"] == GC_WORKER
    assert payload["reason"] == GC_REASON


# ── URL allow / reject ──────────────────────────────────────────────────────


@pytest.mark.parametrize("url", ALLOWED_URLS)
async def test_status_url_allowlist_accepts(url: str) -> None:
    from muteki.solver.gc_session_watchdog import watch_gc_session

    seen: list[str] = []

    async def fetcher(got: str) -> dict[str, Any]:
        seen.append(got)
        return {"online": True, "error": None}

    async def sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await watch_gc_session(
            RecordingBus(),
            run_id="r",
            challenge_id="c",
            status_url=url,
            fetcher=fetcher,
            sleep=sleep,
        )
    assert seen == [url]


@pytest.mark.parametrize("url", REJECTED_URLS)
async def test_status_url_allowlist_rejects(url: str) -> None:
    from muteki.solver.gc_session_watchdog import watch_gc_session

    async def fetcher(_got: str) -> dict[str, Any]:
        raise AssertionError(f"rejected URL must not be fetched: {url}")

    with pytest.raises(ValueError):
        await asyncio.wait_for(
            watch_gc_session(
                RecordingBus(),
                run_id="r",
                challenge_id="c",
                status_url=url,
                fetcher=fetcher,
                sleep=asyncio.sleep,
            ),
            timeout=0.5,
        )


# ── Default HTTP fetcher: token, size, shape ────────────────────────────────


class _StatusHandler(http.server.BaseHTTPRequestHandler):
    body = b'{"online": true, "error": null}'
    status = 200
    captured: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802
        type(self).captured = {
            "authorization": self.headers.get("Authorization"),
            "path": self.path,
            "host": self.headers.get("Host"),
        }
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args: Any) -> None:
        return None


def _start_status_server(body: bytes, status: int = 200) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = type(
        "H",
        (_StatusHandler,),
        {"body": body, "status": status, "captured": {}},
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/api/status"


async def _poll_default_fetcher_once(url: str, **kw: Any) -> RecordingBus:
    from muteki.solver.gc_session_watchdog import watch_gc_session

    bus = RecordingBus()
    task = asyncio.create_task(
        watch_gc_session(
            bus,
            run_id="run-http",
            challenge_id="c-http",
            status_url=url,
            interval_s=0.05,
            request_timeout_s=1.0,
            **kw,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return bus


async def test_token_header_sent_but_never_logged_or_stored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    token = "gc-serve-secret-token-never-leak"
    monkeypatch.setenv("GC_SERVE_TOKEN", token)
    server, url = _start_status_server(b'{"online": true, "error": null}')
    caplog.set_level(logging.DEBUG)
    try:
        bus = await _poll_default_fetcher_once(url)
        captured = server.RequestHandlerClass.captured  # type: ignore[attr-defined]
        assert captured.get("authorization") == f"Bearer {token}"
        assert token not in caplog.text
        dumped = json.dumps(
            [ev.model_dump() for ev in bus.events], ensure_ascii=False,
        )
        assert token not in dumped
        import muteki.solver.gc_session_watchdog as mod
        assert token not in repr(vars(mod))
    finally:
        server.shutdown()


async def test_oversized_status_payload_is_transient() -> None:
    pad = "x" * (70 * 1024)
    body = json.dumps({"online": False, "error": "session_expired", "pad": pad}).encode()
    assert len(body) > 64 * 1024
    server, url = _start_status_server(body)
    try:
        bus = await _poll_default_fetcher_once(url)
        assert [ev.event_type for ev in bus.events] == []
    finally:
        server.shutdown()


async def test_non_object_json_payload_is_transient() -> None:
    server, url = _start_status_server(b'["online"]')
    try:
        bus = await _poll_default_fetcher_once(url)
        assert bus.events == []
    finally:
        server.shutdown()


# ── State machine ───────────────────────────────────────────────────────────


async def test_first_expiry_emits_one_request_repeats_dedup() -> None:
    expired = {"online": False, "error": "session_expired"}
    bus = await _drive_watch([expired, expired, expired])
    reqs = [ev for ev in bus.events if ev.event_type is EventType.HITL_REQUEST]
    assert len(reqs) == 1
    payload = reqs[0].payload
    assert payload["worker"] == GC_WORKER
    assert payload["need"] == GC_NEED
    assert payload["need_kind"] == GC_NEED_KIND
    assert payload["kind"] == GC_KIND
    assert payload["request_id"] == payload["id"] == _stable_request_id()


async def test_recovery_emits_one_resolved_with_same_id() -> None:
    expired = {"online": False, "error": "session_expired"}
    recovered = {"online": True, "error": None}
    bus = await _drive_watch([expired, expired, recovered, recovered])
    types = [ev.event_type for ev in bus.events]
    assert types == [EventType.HITL_REQUEST, EventType.HITL_RESOLVED]
    rid = _stable_request_id()
    assert bus.events[0].payload["request_id"] == rid
    assert bus.events[1].payload["request_id"] == rid
    assert bus.events[1].payload["reason"] == GC_REASON
    assert bus.events[1].payload["worker"] == GC_WORKER


async def test_later_reexpiry_emits_fresh_request_after_resolve() -> None:
    expired = {"online": False, "error": "session_expired"}
    recovered = {"online": True, "error": None}
    bus = await _drive_watch([expired, recovered, expired])
    types = [ev.event_type for ev in bus.events]
    assert types == [
        EventType.HITL_REQUEST,
        EventType.HITL_RESOLVED,
        EventType.HITL_REQUEST,
    ]
    ids = [ev.payload["request_id"] for ev in bus.events]
    assert ids[0] == ids[1] == ids[2] == _stable_request_id()


@pytest.mark.parametrize(
    "payload",
    [
        {"online": None, "error": None},
        {"online": None, "error": "session_expired"},
        {"online": False, "error": "connection refused"},
        {"online": False, "error": "timed out"},
        {"online": False, "error": None},
        {"online": True, "error": None},
        {"online": "false", "error": "session_expired"},
        {"online": False, "error": 1},
        {"online": False},
    ],
)
async def test_online_none_network_other_false_emit_nothing(payload: dict[str, Any]) -> None:
    bus = await _drive_watch([payload, payload])
    assert bus.events == []


async def test_fetcher_errors_are_transient() -> None:
    expired = {"online": False, "error": "session_expired"}
    bus = await _drive_watch([
        TimeoutError("slow"),
        RuntimeError("down"),
        {"online": False, "error": "session_expired"},
    ])
    assert [ev.event_type for ev in bus.events] == [EventType.HITL_REQUEST]
    assert bus.events[0].payload["need"] == GC_NEED


async def test_emit_errors_retry_without_premature_state_change() -> None:
    expired = {"online": False, "error": "session_expired"}
    recovered = {"online": True, "error": None}

    class FlakyBus:
        def __init__(self) -> None:
            self.events: list[Event] = []
            self.calls = 0

        async def emit(self, event: Event) -> Event:
            self.calls += 1
            if self.calls in (1, 3):
                raise RuntimeError("bus emit failed")
            self.events.append(event)
            return event

    bus = await _drive_watch(
        [expired, expired, recovered, recovered], bus=FlakyBus(),
    )
    types = [ev.event_type for ev in bus.events]
    assert types == [EventType.HITL_REQUEST, EventType.HITL_RESOLVED]
    assert bus.calls == 4


async def test_watch_clamps_nonfinite_interval_to_default() -> None:
    from muteki.solver.gc_session_watchdog import watch_gc_session

    slept: list[float] = []

    async def fetcher(_url: str) -> dict[str, Any]:
        return {"online": True, "error": None}

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await watch_gc_session(
            RecordingBus(),
            run_id="r",
            challenge_id="c",
            fetcher=fetcher,
            sleep=sleep,
            interval_s=float("nan"),
            request_timeout_s=float("inf"),
        )
    assert slept == [60.0]


async def test_watch_clamps_nonfinite_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muteki.solver import gc_session_watchdog as mod

    seen: dict[str, float] = {}

    async def fake_default(_url: str, timeout: float) -> dict[str, Any]:
        seen["timeout"] = timeout
        return {"online": True, "error": None}

    monkeypatch.setattr(mod, "_default_fetch", fake_default)

    async def sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mod.watch_gc_session(
            RecordingBus(),
            run_id="r",
            challenge_id="c",
            fetcher=None,
            sleep=sleep,
            interval_s=1.0,
            request_timeout_s=float("nan"),
        )
    assert seen["timeout"] == 5.0


def test_fetch_status_sync_disables_proxy_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error
    import urllib.request

    from muteki.solver.gc_session_watchdog import _fetch_status_sync

    built: dict[str, Any] = {}

    class DummyResp:
        def read(self, _n: int) -> bytes:
            return b'{"online": true, "error": null}'

        def __enter__(self) -> DummyResp:
            return self

        def __exit__(self, *_a: Any) -> bool:
            return False

    class DummyOpener:
        def open(self, req: Any, timeout: float | None = None) -> DummyResp:
            built["opened"] = getattr(req, "full_url", req)
            built["timeout"] = timeout
            return DummyResp()

    def capture_build(*handlers: Any) -> DummyOpener:
        built["handlers"] = handlers
        return DummyOpener()

    monkeypatch.setattr(urllib.request, "build_opener", capture_build)
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not use global urlopen")),
    )

    data = _fetch_status_sync("http://127.0.0.1:8765/api/status", 5.0)
    assert data == {"online": True, "error": None}
    handlers = built["handlers"]
    proxy = next(h for h in handlers if isinstance(h, urllib.request.ProxyHandler))
    assert dict(getattr(proxy, "proxies", {})) == {}
    redir = next(
        h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    )
    req = urllib.request.Request("http://127.0.0.1:8765/api/status")
    location = "http://evil.example/steal"
    with pytest.raises(urllib.error.HTTPError):
        redir.redirect_request(
            req, None, 302, "Found", {"Location": location}, location,
        )
    assert "evil.example" not in str(built.get("opened", ""))


async def test_cancellation_propagates_immediately() -> None:
    from muteki.solver.gc_session_watchdog import watch_gc_session

    started = asyncio.Event()

    async def fetcher(_url: str) -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {"online": True, "error": None}

    task = asyncio.create_task(
        watch_gc_session(
            RecordingBus(),
            run_id="r",
            challenge_id="c",
            fetcher=fetcher,
            sleep=asyncio.sleep,
            interval_s=30,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)


# ── Coordinator start / no-start ────────────────────────────────────────────


async def test_coordinator_starts_watchdog_only_for_geocache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_hanging_watch(monkeypatch)
    seen: dict[str, list[str]] = {}

    async def _observe(mode: str) -> list[str]:
        names: list[str] = []

        async def health() -> list[str]:
            names.extend(t.get_name() for t in asyncio.all_tasks())
            return []

        bus = EventBus()
        sw = _swarm(_challenge(mode), tmp_path / mode, bus=bus, wall_clock_budget=0.2)
        monkeypatch.setattr(sw, "_healthy_engines_async", health)
        await asyncio.wait_for(sw._run_coordinator(), timeout=3)
        return names

    seen["geocache"] = await _observe("geocache")
    seen["ctf"] = await _observe("ctf")
    seen["pentest"] = await _observe("pentest")
    assert "gc-session-watchdog" in seen["geocache"]
    assert "gc-session-watchdog" not in seen["ctf"]
    assert "gc-session-watchdog" not in seen["pentest"]


async def test_watchdog_starts_after_help_sink_and_before_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_hanging_watch(monkeypatch)
    bus = EventBus()
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)
    observed: dict[str, Any] = {}

    async def health() -> list[str]:
        observed["sinks"] = list(bus._sinks)
        observed["watchdog"] = [
            t.get_name() for t in asyncio.all_tasks()
            if t.get_name() == "gc-session-watchdog"
        ]
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert observed["sinks"], "help sink must be attached before health preflight"
    assert observed["watchdog"], "watchdog must start before health preflight"


async def test_coordinator_clamps_poll_interval_and_forwards_status_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_watch(*_a: Any, **kw: Any) -> None:
        captured.update(kw)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "muteki.solver.gc_session_watchdog.watch_gc_session",
        fake_watch,
        raising=False,
    )
    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.watch_gc_session",
        fake_watch,
        raising=False,
    )
    monkeypatch.setenv("MUTEKI_GC_SESSION_POLL_INTERVAL", "0.2")
    monkeypatch.setenv("MUTEKI_GC_STATUS_URL", "http://127.0.0.1:9999/api/status")
    bus = EventBus()
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)

    async def health() -> list[str]:
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert captured.get("interval_s", 0) >= 1
    assert captured.get("status_url") == "http://127.0.0.1:9999/api/status"


async def test_coordinator_clamps_nonfinite_poll_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_watch(*_a: Any, **kw: Any) -> None:
        captured.update(kw)
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.watch_gc_session",
        fake_watch,
        raising=False,
    )
    monkeypatch.setenv("MUTEKI_GC_SESSION_POLL_INTERVAL", "inf")
    bus = EventBus()
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)

    async def health() -> list[str]:
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", health)
    await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    interval = float(captured.get("interval_s"))
    assert interval == 60.0


# ── Coordinator cancel + await on every exit ────────────────────────────────


async def _geocache_coord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finished: dict[str, bool],
) -> Swarm:
    _patch_hanging_watch(monkeypatch, finished)
    bus = EventBus()
    return _swarm(_challenge("geocache"), tmp_path, bus=bus, wall_clock_budget=0.15)


async def test_health_preflight_early_return_cancels_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = {"cancelled": False, "done": False}
    sw = await _geocache_coord(tmp_path, monkeypatch, finished)

    async def none_healthy() -> list[str]:
        assert _watchdog_tasks(), "watchdog must exist before the empty-health return"
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", none_healthy)
    outcome = await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert "NoEligibleEngine" in outcome.reason
    assert finished["cancelled"] is True
    assert finished["done"] is True
    assert not _watchdog_tasks()


async def test_normal_return_cancels_and_awaits_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = {"cancelled": False, "done": False}
    sw = await _geocache_coord(tmp_path, monkeypatch, finished)
    monkeypatch.setattr(sw, "_healthy_engines_async", _async_list(["claude"]))
    monkeypatch.setattr(sw, "_healthy_engines", lambda: ["claude"])

    async def dry_reason() -> int:
        return 0

    monkeypatch.setattr(sw, "_run_reason", dry_reason)

    class Idle:
        solver_id = "cli-claude"

        async def run(self) -> Any:
            from muteki.solver.types import SolveOutcome
            await asyncio.sleep(0)
            return SolveOutcome(False, None, 1, None, "miss")

    monkeypatch.setattr(sw, "_make_cli_worker", lambda *_a, **_k: Idle())
    await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert finished["cancelled"] is True
    assert finished["done"] is True
    assert not _watchdog_tasks()


async def test_exception_cancels_and_awaits_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = {"cancelled": False, "done": False}
    sw = await _geocache_coord(tmp_path, monkeypatch, finished)

    async def boom() -> list[str]:
        await asyncio.wait_for(finished["started_event"].wait(), timeout=1)
        assert _watchdog_tasks()
        raise RuntimeError("health probe exploded")

    monkeypatch.setattr(sw, "_healthy_engines_async", boom)
    with pytest.raises(RuntimeError, match="health probe exploded"):
        await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert finished["cancelled"] is True
    assert finished["done"] is True
    assert not _watchdog_tasks()


async def test_cancellation_cancels_and_awaits_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = {"cancelled": False, "done": False}
    sw = await _geocache_coord(tmp_path, monkeypatch, finished)
    gate = asyncio.Event()

    async def hang_health() -> list[str]:
        gate.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", hang_health)
    task = asyncio.create_task(sw._run_coordinator())
    await asyncio.wait_for(gate.wait(), timeout=2)
    assert _watchdog_tasks()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert finished["cancelled"] is True
    assert finished["done"] is True
    assert not _watchdog_tasks()


async def test_watchdog_exception_does_not_crash_coordinator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_watch(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("watchdog exploded")

    monkeypatch.setattr(
        "muteki.solver.gc_session_watchdog.watch_gc_session",
        boom_watch,
        raising=False,
    )
    monkeypatch.setattr(
        "muteki.swarm.coordinator_loop.watch_gc_session",
        boom_watch,
        raising=False,
    )
    bus = EventBus()
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)
    kinds: list[str] = []
    real_emit = sw._emit_coord_bb

    async def spy_emit(kind: str, **fields: Any) -> None:
        kinds.append(kind)
        await real_emit(kind, **fields)

    async def none_healthy() -> list[str]:
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(sw, "_emit_coord_bb", spy_emit)
    monkeypatch.setattr(sw, "_healthy_engines_async", none_healthy)
    outcome = await asyncio.wait_for(sw._run_coordinator(), timeout=3)
    assert "NoEligibleEngine" in outcome.reason
    assert "gc_session_watchdog_failed" in kinds


def _async_list(value: list[str]):
    async def _inner(*_a: Any, **_k: Any) -> list[str]:
        return value
    return _inner


# ── Help sink ───────────────────────────────────────────────────────────────


async def test_help_sink_removes_exact_pending_request_and_wakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muteki.core.events import hitl_resolved_payload

    _patch_hanging_watch(monkeypatch)
    bus = EventBus()
    recorded: list[Event] = []

    async def recorder(ev: Event) -> None:
        recorded.append(ev)

    bus.add_sink(recorder)
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)
    ready = asyncio.Event()

    async def hang_health() -> list[str]:
        ready.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", hang_health)
    task = asyncio.create_task(sw._run_coordinator())
    await asyncio.wait_for(ready.wait(), timeout=2)
    try:
        keep = hitl_request_payload("other-worker", "need a VPS")
        gc_payload = hitl_request_payload(
            GC_WORKER, GC_NEED, kind=GC_KIND, need_kind=GC_NEED_KIND,
        )
        await bus.emit(Event(
            event_type=EventType.HITL_REQUEST, run_id=sw.run_id, payload=keep,
        ))
        await bus.emit(Event(
            event_type=EventType.HITL_REQUEST, run_id=sw.run_id, payload=gc_payload,
        ))
        assert {h["request_id"] for h in sw._pending_help} == {
            keep["request_id"], gc_payload["request_id"],
        }
        gc_row = next(
            h for h in sw._pending_help
            if h["request_id"] == gc_payload["request_id"]
        )
        assert gc_row["need_kind"] == "external_blocker"
        sw._operator_paused = True
        assert sw._operator_event is not None
        sw._operator_event.clear()

        await asyncio.wait_for(
            bus.emit(Event(
                event_type=EventType.HITL_RESOLVED,
                run_id=sw.run_id,
                payload=hitl_resolved_payload(
                    gc_payload["request_id"], worker=GC_WORKER, reason=GC_REASON,
                ),
            )),
            timeout=1,
        )
        assert [h["request_id"] for h in sw._pending_help] == [keep["request_id"]]
        assert sw._operator_paused is True
        assert not sw._operator_event.is_set(), (
            "partial resolve must not wake/unfreeze while other blockers remain"
        )

        sw._operator_event.clear()
        await asyncio.wait_for(
            bus.emit(Event(
                event_type=EventType.HITL_RESOLVED,
                run_id=sw.run_id,
                payload=hitl_resolved_payload(
                    keep["request_id"], worker="other-worker", reason="cleared",
                ),
            )),
            timeout=1,
        )
        assert sw._pending_help == []
        assert sw._operator_paused is False
        assert sw._operator_event.is_set()
        assert EventType.HITL_RESPONSE not in {ev.event_type for ev in recorded}
        assert EventType.CONTROL_COMMAND not in {ev.event_type for ev in recorded}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_help_sink_keeps_pause_when_control_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from muteki.core.events import hitl_resolved_payload

    _patch_hanging_watch(monkeypatch)
    bus = EventBus()
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)
    ready = asyncio.Event()

    async def hang_health() -> list[str]:
        ready.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", hang_health)
    task = asyncio.create_task(sw._run_coordinator())
    await asyncio.wait_for(ready.wait(), timeout=2)
    try:
        payload = hitl_request_payload(
            GC_WORKER, GC_NEED, kind=GC_KIND, need_kind=GC_NEED_KIND,
        )
        await bus.emit(Event(
            event_type=EventType.HITL_REQUEST, run_id=sw.run_id, payload=payload,
        ))
        sw._operator_paused = True
        sw._control_frozen = True
        assert sw._operator_event is not None
        sw._operator_event.clear()
        await asyncio.wait_for(
            bus.emit(Event(
                event_type=EventType.HITL_RESOLVED,
                run_id=sw.run_id,
                payload={"id": payload["id"], "worker": GC_WORKER, "reason": GC_REASON},
            )),
            timeout=1,
        )
        assert sw._pending_help == []
        assert sw._operator_paused is True
        assert not sw._operator_event.is_set(), (
            "control-frozen full resolve must not open a resume window"
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_guoqi_is_not_a_global_blocker_keyword() -> None:
    import inspect

    from muteki.swarm.swarm import Swarm

    src = inspect.getsource(Swarm._mechanical_need_kind)
    assert "过期" not in src
    generic = "请处理过期问题后再继续"
    assert Swarm._mechanical_need_kind(generic) == "worker_uncertainty"
    assert Swarm._rechecked_need_kind(generic, "external_blocker") == "worker_uncertainty"
    assert Swarm._mechanical_need_kind("instance expired") == "external_blocker"


async def test_help_sink_trusts_watchdog_blocker_but_not_generic_guoqi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_hanging_watch(monkeypatch)
    bus = EventBus()
    sw = _swarm(_challenge("geocache"), tmp_path, bus=bus)
    ready = asyncio.Event()

    async def hang_health() -> list[str]:
        ready.set()
        await asyncio.Event().wait()
        return []

    monkeypatch.setattr(sw, "_healthy_engines_async", hang_health)
    task = asyncio.create_task(sw._run_coordinator())
    await asyncio.wait_for(ready.wait(), timeout=2)
    try:
        gc_payload = hitl_request_payload(
            GC_WORKER, GC_NEED, kind=GC_KIND, need_kind=GC_NEED_KIND,
        )
        generic = hitl_request_payload(
            "cli-claude", "请处理过期问题后再继续",
            kind="need_input", need_kind="external_blocker",
        )
        await bus.emit(Event(
            event_type=EventType.HITL_REQUEST, run_id=sw.run_id, payload=gc_payload,
        ))
        await bus.emit(Event(
            event_type=EventType.HITL_REQUEST, run_id=sw.run_id, payload=generic,
        ))
        pending_ids = [h["request_id"] for h in sw._pending_help]
        assert gc_payload["request_id"] in pending_ids
        gc_row = next(h for h in sw._pending_help if h["request_id"] == gc_payload["request_id"])
        assert gc_row["need_kind"] == "external_blocker"
        assert generic["request_id"] not in pending_ids
        assert any(
            str(row.get("need", "")).startswith("请处理过期")
            for row in sw._pending_uncertainty_reviews
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ── Frontend reducer ────────────────────────────────────────────────────────


def test_frontend_source_contract_hitl_resolved() -> None:
    src = UI_EVENTS.read_text()
    assert 'HITL_RESOLVED = "hitl.resolved"' in src
    assert "case EventType.HITL_RESOLVED" in src


def test_frontend_reducer_removes_exactly_matching_card() -> None:
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    ui_root = UI_EVENTS.parent.parent
    if not (ui_root / "node_modules" / "typescript").exists():
        pytest.skip("apps/web/ui/node_modules/typescript missing")
    script = textwrap.dedent(
        f"""
        const fs = require("fs");
        const ts = require("typescript");
        const vm = require("vm");
        const source = fs.readFileSync({json.dumps(str(UI_EVENTS))}, "utf8");
        const out = ts.transpileModule(source, {{
          compilerOptions: {{ module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 }}
        }}).outputText;
        const sandbox = {{ module: {{ exports: {{}} }}, exports: {{}} }};
        sandbox.exports = sandbox.module.exports;
        vm.runInNewContext(out, sandbox, {{ filename: "events.js" }});
        const lib = sandbox.module.exports;
        function assert(cond, msg) {{ if (!cond) throw new Error(msg); }}

        assert(lib.EventType.HITL_RESOLVED === "hitl.resolved", "enum value");

        let s = lib.emptyDeck("run-gc-hitl");
        s = lib.reduce(s, {{ event_type: lib.EventType.HITL_REQUEST, run_id: "run-gc-hitl", ts: 1,
          payload: {{ request_id: "H-gc", worker: "gc-session-watchdog",
            need: "Geocaching 会话已过期，请运行 gc auth login", kind: "env_down",
            need_kind: "external_blocker" }} }});
        s = lib.reduce(s, {{ event_type: lib.EventType.HITL_REQUEST, run_id: "run-gc-hitl", ts: 2,
          payload: {{ request_id: "H-other", worker: "cli-claude", need: "need a VPS" }} }});
        assert(s.hitlRequests.map((r) => r.id).join(",") === "H-gc,H-other", "both cards");

        s = lib.reduce(s, {{ event_type: lib.EventType.HITL_RESOLVED, run_id: "run-gc-hitl", ts: 3,
          payload: {{ request_id: "H-gc", worker: "gc-session-watchdog",
            reason: "Geocaching 会话已恢复" }} }});
        assert(s.hitlRequests.length === 1, "only the matching card is removed");
        assert(s.hitlRequests[0].id === "H-other", "unrelated card remains");
        """
    )
    subprocess.run(["node", "-e", script], cwd=ui_root, check=True, capture_output=True, text=True)
