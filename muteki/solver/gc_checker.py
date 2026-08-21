"""Host-side GeoCheck / Certitude runner.

Only a ``gc check`` process started by this module can upgrade a local
coordinate candidate to verified. Worker-emitted ``ok=true`` JSON, markers,
or prose never count.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from subprocess import CompletedProcess
from typing import NamedTuple
from weakref import WeakKeyDictionary

from muteki.models.solve_graph import Challenge
from muteki.vendor.geocaching_cli.coord import format_dmm, parse_coord

MSG_NOT_CONFIGURED = "当前题目未配置外部校验"
MSG_BAD_EXECUTABLE = "外部校验程序不可用"
MSG_TIMEOUT = "外部校验超时"
MSG_MALFORMED = "外部校验结果无法解析"
MSG_MISMATCH = "外部校验返回的坐标与请求不一致"
MSG_UNAVAILABLE = "外部校验暂时不可用"
MSG_VERIFIED = "外部校验通过"
MSG_REJECTED = "外部校验未通过"

_SECRET_PREFIXES = (
    "OPENAI_",
    "ANTHROPIC_",
    "DEEPSEEK_",
    "MUTEKI_DEEPSEEK_",
    "XAI_",
    "GROK_",
)
_SECRET_KEYS = frozenset({"CURSOR_API_KEY"})

_REQUIRED_TYPES: dict[str, type] = {
    "ok": bool,
    "definitive": bool,
    "coord_text": str,
    "site": str,
    "message": str,
    "attempts": int,
}

_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Lock]
] = WeakKeyDictionary()
_CACHE: dict[tuple[str, str, str], ExternalCoordVerdict] = {}


class ExternalCoordVerdict(NamedTuple):
    verified: bool
    definitive: bool
    message: str


def _reset_checker_state_for_tests() -> None:
    _LOCKS.clear()
    _CACHE.clear()


def _loop_bucket(store: WeakKeyDictionary) -> dict:
    loop = asyncio.get_running_loop()
    bucket = store.get(loop)
    if bucket is None:
        bucket = {}
        store[loop] = bucket
    return bucket


def _lock_for(key: tuple[str, str]) -> asyncio.Lock:
    locks = _loop_bucket(_LOCKS)
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _canonical_coord(text: str) -> str | None:
    try:
        return format_dmm(parse_coord(text))
    except Exception:
        return None


def _checker_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _SECRET_KEYS:
            continue
        if any(key.startswith(prefix) for prefix in _SECRET_PREFIXES):
            continue
        env[key] = value
    return env


def _resolve_executable(explicit: str | None) -> str | None:
    if explicit:
        raw = explicit
    elif os.environ.get("MUTEKI_GC_CLI"):
        raw = os.environ["MUTEKI_GC_CLI"]
    else:
        found = shutil.which("gc")
        if not found:
            return None
        raw = found
    path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    try:
        st = resolved.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if not os.access(resolved, os.X_OK):
        return None
    return str(resolved)


def _parse_checker_object(stdout: str) -> dict | None:
    text = (stdout or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if type(obj) is not dict:
        return None
    for key, expected in _REQUIRED_TYPES.items():
        if key not in obj:
            return None
        value = obj[key]
        if expected is bool and type(value) is not bool:
            return None
        if expected is int and type(value) is not int:
            return None
        if expected is str and type(value) is not str:
            return None
    return obj


async def _default_runner(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> CompletedProcess[str]:
    proc = await asyncio.create_subprocess_exec(
        argv[0],
        *argv[1:],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.communicate()
        except Exception:
            pass
        raise
    return CompletedProcess(
        argv,
        proc.returncode if proc.returncode is not None else -1,
        stdout=(stdout_b or b"").decode("utf-8", errors="replace"),
        stderr=(stderr_b or b"").decode("utf-8", errors="replace"),
    )


def _verdict_from_process(
    result: CompletedProcess[str], requested: str,
) -> ExternalCoordVerdict:
    obj = _parse_checker_object(result.stdout or "")
    if obj is None:
        return ExternalCoordVerdict(False, False, MSG_MALFORMED)
    returned = _canonical_coord(str(obj["coord_text"]))
    if returned is None or returned != requested:
        return ExternalCoordVerdict(False, False, MSG_MISMATCH)
    ok = obj["ok"]
    definitive = obj["definitive"]
    rc = int(result.returncode)
    if rc == 0 and ok is True and definitive is True:
        return ExternalCoordVerdict(True, True, MSG_VERIFIED)
    if rc == 1 and ok is False and definitive is True:
        return ExternalCoordVerdict(False, True, MSG_REJECTED)
    return ExternalCoordVerdict(False, False, MSG_UNAVAILABLE)


async def _invoke_checker(
    challenge: Challenge,
    requested: str,
    *,
    executable: str | None,
    timeout_s: float,
    runner: Callable[..., Awaitable[CompletedProcess[str]]] | None,
) -> ExternalCoordVerdict:
    resolved = _resolve_executable(executable)
    if resolved is None:
        if runner is None:
            return ExternalCoordVerdict(False, False, MSG_BAD_EXECUTABLE)
        resolved = executable or "gc"
    url = str(getattr(challenge, "geocheck_url", "") or "").strip()
    argv = [
        resolved,
        "check",
        "--url",
        url,
        requested,
        "--headless",
        "--json",
    ]
    env = _checker_env()
    run = runner if runner is not None else _default_runner
    try:
        with tempfile.TemporaryDirectory(prefix="muteki-gc-check-") as tmp:
            result = await run(
                argv, cwd=tmp, env=env, timeout=timeout_s)
    except asyncio.TimeoutError:
        return ExternalCoordVerdict(False, False, MSG_TIMEOUT)
    except Exception:
        return ExternalCoordVerdict(False, False, MSG_UNAVAILABLE)
    return _verdict_from_process(result, requested)


async def verify_external_coordinate(
    challenge: Challenge,
    coord_text: str,
    *,
    executable: str | None = None,
    timeout_s: float = 120.0,
    runner: Callable[..., Awaitable[CompletedProcess[str]]] | None = None,
) -> ExternalCoordVerdict:
    if getattr(challenge, "mode", "") != "geocache":
        return ExternalCoordVerdict(False, False, MSG_NOT_CONFIGURED)
    url = str(getattr(challenge, "geocheck_url", "") or "").strip()
    if not url:
        return ExternalCoordVerdict(False, False, MSG_NOT_CONFIGURED)
    requested = _canonical_coord(coord_text)
    if requested is None:
        return ExternalCoordVerdict(False, False, MSG_MALFORMED)

    key = (str(getattr(challenge, "id", "") or ""), url)
    cache_key = (key[0], url, requested)
    lock = _lock_for(key)
    async with lock:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached
        verdict = await _invoke_checker(
            challenge,
            requested,
            executable=executable,
            timeout_s=timeout_s,
            runner=runner,
        )
        if verdict.definitive:
            _CACHE[cache_key] = verdict
        return verdict
