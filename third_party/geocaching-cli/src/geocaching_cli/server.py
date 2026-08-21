"""Local-only stdlib HTTP API for Muteki / watchdog.

Network imports stay inside request handlers. Secrets never appear in responses
or the default access log.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from geocaching_cli.config import load_credentials, load_session
from geocaching_cli.errors import LiveError, LiveLoginError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1"})
GC_CODE_RE = re.compile(r"^GC[A-Z0-9]+$", re.IGNORECASE)


def _require_loopback(host: str) -> None:
    if host not in ALLOWED_HOSTS:
        raise ValueError("仅允许绑定 127.0.0.1 或 ::1")


def _safe_error(message: str) -> str:
    """Strip known secrets from an error string before returning it."""
    secrets: list[str] = []
    creds = load_credentials()
    if creds.password:
        secrets.append(creds.password)
    if creds.cookie:
        secrets.append(creds.cookie)
    token = os.environ.get("GC_SERVE_TOKEN")
    if token:
        secrets.append(token)
    session = load_session() or {}
    cookies = session.get("cookies") or {}
    if isinstance(cookies, dict):
        secrets.extend(str(value) for value in cookies.values() if value)
    safe = message
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "***")
    return safe


def _has_session(session: dict[str, Any] | None) -> bool:
    if not session:
        return False
    return bool(session.get("cookies") or session.get("cookie_list"))


def _username(session: dict[str, Any] | None) -> str | None:
    creds = load_credentials()
    if creds.username:
        return creds.username
    if session and session.get("username"):
        return session.get("username")
    return None


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :]
    return token or None


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    expected = os.environ.get("GC_SERVE_TOKEN")
    if not expected:
        return True
    provided = _bearer_token(handler.headers.get("Authorization"))
    if provided is None:
        return False
    try:
        return hmac.compare_digest(provided, expected)
    except (TypeError, ValueError):
        return False


def _normalize_gc(raw: str) -> str | None:
    code = unquote(raw).strip()
    if not GC_CODE_RE.fullmatch(code):
        return None
    return code.upper()


def _status_payload(clock: Callable[[], float]) -> dict[str, Any]:
    session = load_session()
    creds = load_credentials()
    configured = creds.configured
    has_session = _has_session(session)
    username = _username(session)
    if not configured and not has_session:
        return {
            "configured": False,
            "has_session": False,
            "username": username,
            "online": None,
            "error": None,
            "checked_at": clock(),
        }

    from geocaching_cli import live as live_mod

    checked_at = clock()
    try:
        geocaching = live_mod.connect()
        username = live_mod.logged_username(geocaching) or username
        return {
            "configured": configured,
            "has_session": has_session,
            "username": username,
            "online": True,
            "error": None,
            "checked_at": checked_at,
        }
    except LiveLoginError:
        return {
            "configured": configured,
            "has_session": has_session,
            "username": username,
            "online": False,
            "error": "session_expired",
            "checked_at": checked_at,
        }
    except LiveError as exc:
        return {
            "configured": configured,
            "has_session": has_session,
            "username": username,
            "online": False,
            "error": _safe_error(str(exc)),
            "checked_at": checked_at,
        }


def _show_payload(gc_code: str) -> dict[str, Any]:
    from geocaching_cli import live as live_mod

    try:
        geocaching = live_mod.connect()
        record = live_mod.show_cache(geocaching, gc_code)
        return record.to_dict()
    except LiveLoginError:
        return {"error": "session_expired"}
    except LiveError as exc:
        return {"error": _safe_error(str(exc))}


def make_handler(
    *,
    status_ttl_s: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> type[BaseHTTPRequestHandler]:
    cache: dict[str, Any] = {"payload": None, "checked_at": None}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if not _authorized(self):
                self._send_json(200, {"error": "unauthorized"})
                return
            path = urlparse(self.path).path
            if path.rstrip("/") == "/api/status":
                now = clock()
                with lock:
                    cached = cache["payload"]
                    checked_at = cache["checked_at"]
                    if (
                        cached is not None
                        and checked_at is not None
                        and (now - checked_at) < status_ttl_s
                    ):
                        self._send_json(200, cached)
                        return
                payload = _status_payload(clock)
                if payload["online"] is not None:
                    with lock:
                        cache["payload"] = payload
                        cache["checked_at"] = payload["checked_at"]
                self._send_json(200, payload)
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "show":
                gc_code = _normalize_gc(parts[2])
                if gc_code is None:
                    self._send_json(200, {"error": "invalid_gc_code"})
                    return
                self._send_json(200, _show_payload(gc_code))
                return
            self._send_json(404, {"error": "not_found"})

    return Handler


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    status_ttl_s: float = 60.0,
) -> ThreadingHTTPServer:
    _require_loopback(host)
    handler = make_handler(status_ttl_s=status_ttl_s)
    if host == "::1":

        class IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        return IPv6Server((host, port), handler)
    return ThreadingHTTPServer((host, port), handler)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = create_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
