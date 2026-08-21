"""Poll local gccli /api/status and surface a single session-expiry HITL card."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from muteki.core.event_bus import EventBus
from muteki.core.events import (
    Event,
    EventType,
    hitl_request_payload,
    hitl_resolved_payload,
)

DEFAULT_STATUS_URL = "http://127.0.0.1:8765/api/status"
_MAX_BODY = 64 * 1024
_WORKER = "gc-session-watchdog"
_NEED = "Geocaching 会话已过期，请运行 gc auth login"
_NEED_KIND = "external_blocker"
_KIND = "env_down"
_RECOVERED = "Geocaching 会话已恢复"


def validate_gc_status_url(url: str) -> str:
    """Accept only loopback http://127.0.0.1|<::1>:<port>/api/status."""
    if not isinstance(url, str) or not url:
        raise ValueError("status url must be a non-empty string")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError("status url is not a canonical loopback status endpoint") from exc
    if parsed.scheme != "http":
        raise ValueError("status url must be http loopback")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("status url must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("status url must not include query or fragment")
    if parsed.path != "/api/status":
        raise ValueError("status url path must be /api/status")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("status url port out of range") from exc
    if port is None or not (1 <= port <= 65535):
        raise ValueError("status url port out of range")
    host = parsed.hostname
    if host == "127.0.0.1":
        expected = f"http://127.0.0.1:{port}/api/status"
    elif host == "::1":
        expected = f"http://[::1]:{port}/api/status"
    else:
        raise ValueError("status url host must be loopback")
    if url != expected:
        raise ValueError("status url is not a canonical loopback status endpoint")
    return url


def _status_fields(data: Any) -> tuple[Any, Any] | None:
    if not isinstance(data, dict):
        return None
    online = data.get("online")
    error = data.get("error")
    if online is not None and not isinstance(online, bool):
        return None
    if error is not None and not isinstance(error, str):
        return None
    return online, error


def _fetch_status_sync(url: str, timeout: float) -> dict[str, Any]:
    headers: dict[str, str] = {}
    token = os.environ.get("GC_SERVE_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read(_MAX_BODY + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TimeoutError("gc status poll failed") from exc
    if len(raw) > _MAX_BODY:
        raise ValueError("status payload too large")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("status payload is not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("status payload must be a JSON object")
    return data


async def _default_fetch(url: str, timeout: float) -> dict[str, Any]:
    return await asyncio.to_thread(_fetch_status_sync, url, timeout)


async def watch_gc_session(
    bus: EventBus,
    *,
    run_id: str,
    challenge_id: str,
    status_url: str = DEFAULT_STATUS_URL,
    interval_s: float = 60.0,
    request_timeout_s: float = 5.0,
    fetcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    status_url = validate_gc_status_url(status_url)
    request = hitl_request_payload(
        _WORKER, _NEED, kind=_KIND, need_kind=_NEED_KIND,
    )
    request_id = str(request["request_id"])
    expired = False

    async def _fetch() -> dict[str, Any]:
        if fetcher is not None:
            return await fetcher(status_url)
        return await _default_fetch(status_url, request_timeout_s)

    while True:
        try:
            data = await _fetch()
            fields = _status_fields(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            await sleep(interval_s)
            continue
        if fields is None:
            await sleep(interval_s)
            continue
        online, error = fields
        if online is False and error == "session_expired":
            if not expired:
                expired = True
                await bus.emit(
                    Event(
                        event_type=EventType.HITL_REQUEST,
                        run_id=run_id,
                        challenge_id=challenge_id,
                        solver_id=_WORKER,
                        payload=request,
                    )
                )
        elif online is True and expired:
            expired = False
            await bus.emit(
                Event(
                    event_type=EventType.HITL_RESOLVED,
                    run_id=run_id,
                    challenge_id=challenge_id,
                    solver_id=_WORKER,
                    payload=hitl_resolved_payload(
                        request_id, worker=_WORKER, reason=_RECOVERED,
                    ),
                )
            )
        await sleep(interval_s)
