from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from typer.testing import CliRunner

from geocaching_cli.cli import app
from geocaching_cli.config import save_session
from geocaching_cli.errors import LiveError, LiveLoginError
from geocaching_cli.models import CacheRecord
from geocaching_cli.server import create_server, make_handler, run_server

runner = CliRunner()


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@contextmanager
def serving(handler_cls: type, host: str = "127.0.0.1") -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer((host, 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_json(
    server: ThreadingHTTPServer,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        conn.request("GET", path, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        return response.status, payload, dict(response.headers)
    finally:
        conn.close()


def configure_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEOCACHING_USERNAME", "tester")
    monkeypatch.setenv("GEOCACHING_PASSWORD", "unit-test-password")


def test_status_offline_unconfigured_does_not_network(isolated_home, monkeypatch) -> None:
    def boom(**_kwargs):
        raise AssertionError("connect must not be called without config or session")

    monkeypatch.setattr("geocaching_cli.live.connect", boom)
    clock = FakeClock(12.5)
    with serving(make_handler(clock=clock)) as server:
        status, payload, headers = request_json(server, "/api/status")

    assert status == 200
    assert payload == {
        "configured": False,
        "has_session": False,
        "username": None,
        "online": None,
        "error": None,
        "checked_at": 12.5,
    }
    assert "application/json" in headers["Content-Type"]
    assert "utf-8" in headers["Content-Type"].lower()
    dumped = json.dumps(payload)
    assert "unit-test-password" not in dumped
    assert "Authorization" not in dumped


def test_status_online_success(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)
    calls = {"n": 0}

    def fake_connect(**_kwargs):
        calls["n"] += 1
        return SimpleNamespace(_logged_username="tester")

    monkeypatch.setattr("geocaching_cli.live.connect", fake_connect)
    clock = FakeClock(3.0)
    with serving(make_handler(clock=clock)) as server:
        status, payload, _headers = request_json(server, "/api/status")

    assert status == 200
    assert payload["configured"] is True
    assert payload["has_session"] is False
    assert payload["username"] == "tester"
    assert payload["online"] is True
    assert payload["error"] is None
    assert payload["checked_at"] == 3.0
    assert calls["n"] == 1


def test_status_session_expired_stable_code(isolated_home, monkeypatch) -> None:
    save_session({"username": "cached", "cookies": {"gspkauth": "dummy-session"}})

    def expired(**_kwargs):
        raise LiveLoginError("cookie no longer accepted")

    monkeypatch.setattr("geocaching_cli.live.connect", expired)
    with serving(make_handler()) as server:
        status, payload, _headers = request_json(server, "/api/status")

    assert status == 200
    assert payload["has_session"] is True
    assert payload["online"] is False
    assert payload["error"] == "session_expired"
    dumped = json.dumps(payload)
    assert "dummy-session" not in dumped
    assert "gspkauth" not in dumped


def test_status_other_live_error_is_safe_string(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)

    def fail(**_kwargs):
        raise LiveError("触发站点速率限制")

    monkeypatch.setattr("geocaching_cli.live.connect", fail)
    with serving(make_handler()) as server:
        status, payload, _headers = request_json(server, "/api/status")

    assert status == 200
    assert payload["online"] is False
    assert payload["error"] == "触发站点速率限制"
    assert "unit-test-password" not in json.dumps(payload)


def test_status_ttl_reuses_connect_until_expiry(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)
    calls = {"n": 0}

    def fake_connect(**_kwargs):
        calls["n"] += 1
        return SimpleNamespace(_logged_username="tester")

    monkeypatch.setattr("geocaching_cli.live.connect", fake_connect)
    clock = FakeClock(100.0)
    handler = make_handler(status_ttl_s=60.0, clock=clock)
    with serving(handler) as server:
        first = request_json(server, "/api/status")[1]
        clock.advance(59.0)
        second = request_json(server, "/api/status")[1]
        clock.advance(1.0)
        third = request_json(server, "/api/status")[1]

    assert calls["n"] == 2
    assert first["checked_at"] == 100.0
    assert second["checked_at"] == 100.0
    assert third["checked_at"] == 160.0
    assert first["online"] is True
    assert third["online"] is True


def test_status_cache_is_per_handler_not_module(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)
    calls = {"n": 0}

    def fake_connect(**_kwargs):
        calls["n"] += 1
        return SimpleNamespace(_logged_username="tester")

    monkeypatch.setattr("geocaching_cli.live.connect", fake_connect)
    clock = FakeClock(0.0)
    first = make_handler(status_ttl_s=60.0, clock=clock)
    second = make_handler(status_ttl_s=60.0, clock=clock)
    with serving(first) as server_a, serving(second) as server_b:
        request_json(server_a, "/api/status")
        request_json(server_b, "/api/status")

    assert calls["n"] == 2


def test_show_success_normalizes_gc_code(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)
    record = CacheRecord(
        gc_code="GC1PAR2",
        name="Geocaching HQ",
        latitude=47.644,
        longitude=-122.119,
        cache_type="traditional",
        source="live-show",
    )
    seen: dict[str, Any] = {}

    def fake_connect(**_kwargs):
        seen["connected"] = True
        return object()

    def fake_show(_geocaching, gc_code: str, **_kwargs):
        seen["gc_code"] = gc_code
        return record

    monkeypatch.setattr("geocaching_cli.live.connect", fake_connect)
    monkeypatch.setattr("geocaching_cli.live.show_cache", fake_show)
    with serving(make_handler()) as server:
        status, payload, _headers = request_json(server, "/api/show/gc1par2")

    assert status == 200
    assert payload == record.to_dict()
    assert seen["connected"] is True
    assert seen["gc_code"] == "GC1PAR2"


def test_show_rejects_invalid_gc_code(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)

    def boom(**_kwargs):
        raise AssertionError("invalid GC code must not hit the network")

    monkeypatch.setattr("geocaching_cli.live.connect", boom)
    monkeypatch.setattr("geocaching_cli.live.show_cache", boom)
    with serving(make_handler()) as server:
        bad_gc = request_json(server, "/api/show/NOT-A-CACHE")
        too_short = request_json(server, "/api/show/GC")
        hyphen = request_json(server, "/api/show/GC-1")

    for status, payload, _headers in (bad_gc, too_short, hyphen):
        assert status == 200
        assert payload["error"] == "invalid_gc_code"


def test_token_missing_wrong_and_correct(isolated_home, monkeypatch) -> None:
    configure_password(monkeypatch)
    monkeypatch.setenv("GC_SERVE_TOKEN", "correct-token")
    monkeypatch.setattr(
        "geocaching_cli.live.connect",
        lambda **_kwargs: SimpleNamespace(_logged_username="tester"),
    )
    with serving(make_handler()) as server:
        missing = request_json(server, "/api/status")
        wrong = request_json(
            server,
            "/api/status",
            headers={"Authorization": "Bearer wrong"},
        )
        correct = request_json(
            server,
            "/api/status",
            headers={"Authorization": "Bearer correct-token"},
        )

    assert missing[0] == 200
    assert missing[1] == {"error": "unauthorized"}
    assert wrong[0] == 200
    assert wrong[1] == {"error": "unauthorized"}
    assert correct[0] == 200
    assert correct[1]["online"] is True
    assert "correct-token" not in json.dumps(correct[1])
    assert "wrong" not in json.dumps(wrong[1])


def test_unknown_route_is_404_json(isolated_home) -> None:
    with serving(make_handler()) as server:
        status, payload, _headers = request_json(server, "/api/nope")

    assert status == 404
    assert "error" in payload


def test_non_loopback_host_rejected() -> None:
    with pytest.raises(ValueError):
        create_server(host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        create_server(host="192.168.1.10", port=0)
    with pytest.raises(ValueError):
        create_server(host="localhost", port=0)
    with pytest.raises(ValueError):
        run_server(host="8.8.8.8", port=8765)


def test_create_server_defaults_and_loopback(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(
        "geocaching_cli.live.connect",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("no network")),
    )
    server = create_server(port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        status, payload, _headers = request_json(server, "/api/status")
        assert status == 200
        assert payload["online"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_access_log_does_not_leak_request(isolated_home, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GC_SERVE_TOKEN", "log-secret-token")
    with serving(make_handler()) as server:
        request_json(
            server,
            "/api/status",
            headers={"Authorization": "Bearer log-secret-token"},
        )
    captured = capsys.readouterr()
    assert "/api/status" not in captured.out
    assert "/api/status" not in captured.err
    assert "log-secret-token" not in captured.out
    assert "log-secret-token" not in captured.err


def test_typer_help_lists_serve() -> None:
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    assert "serve" in root.stdout

    help_result = runner.invoke(app, ["serve", "--help"])
    assert help_result.exit_code == 0
    assert "--host" in help_result.stdout
    assert "--port" in help_result.stdout
    assert "本机" in help_result.stdout or "本地" in help_result.stdout

    rejected = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert rejected.exit_code != 0
