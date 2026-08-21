"""Host-side GeoCheck / Certitude runner.

Only a ``gc check`` process started by this module can upgrade a local
coordinate candidate to verified. Worker-emitted ``ok=true`` JSON, markers,
or prose never count.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
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

_ENV_EXACT = frozenset({
    "HOME",
    "PATH",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "TMPDIR",
    "TMP",
    "TEMP",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "PLAYWRIGHT_BROWSERS_PATH",
    "XDG_DATA_HOME",
})
_ENV_PREFIXES = ("GEOCACHING_", "LC_", "SSL_")

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


def _challenge_identity(challenge: Challenge) -> tuple[str, str]:
    cid = str(getattr(challenge, "id", "") or "").strip()
    name = str(getattr(challenge, "name", "") or "").strip()
    url = str(getattr(challenge, "geocheck_url", "") or "").strip()
    ident = "|".join(part for part in (cid, name, url) if part)
    if not ident:
        ident = "geocache"
    return (ident, url)


def _canonical_coord(text: str) -> str | None:
    try:
        return format_dmm(parse_coord(text))
    except Exception:
        return None


def _checker_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ENV_EXACT or key.startswith(_ENV_PREFIXES):
            env[key] = value
    return env


def _is_unsafe_location(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    text = str(resolved).replace("\\", "/")
    roots: list[str] = []
    try:
        roots.append(str(Path.home().resolve()).replace("\\", "/"))
    except OSError:
        pass
    roots.append(str(Path(tempfile.gettempdir()).resolve()).replace("\\", "/"))
    roots.append("/tmp")
    roots.append("/var/tmp")
    try:
        roots.append(str(Path.cwd().resolve()).replace("\\", "/"))
    except OSError:
        pass
    for extra in (
        os.environ.get("MUTEKI_WORKSPACE"),
        os.environ.get("MUTEKI_SESSION_DIR"),
    ):
        if extra:
            try:
                roots.append(str(Path(extra).resolve()).replace("\\", "/"))
            except OSError:
                roots.append(str(extra).replace("\\", "/"))
    for root in roots:
        root = root.rstrip("/") or root
        if text == root or text.startswith(root + "/"):
            return True
    lowered = text.lower()
    if "/sessions/" in lowered or "/workspace/" in lowered:
        return True
    return False


def _group_or_world_writable(mode: int) -> bool:
    return bool(mode & (stat.S_IWGRP | stat.S_IWOTH))


def _expected_sha256() -> str:
    return (os.environ.get("MUTEKI_GC_CLI_SHA256") or "").strip().lower()


def _sha256_matches(path: str) -> bool:
    expected = _expected_sha256()
    if not expected:
        return True
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == expected


def _is_trusted_executable(resolved: Path, *, source: str) -> bool:
    try:
        st = os.stat(resolved)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if not os.access(resolved, os.X_OK):
        return False
    if _group_or_world_writable(st.st_mode):
        return False
    if source == "explicit":
        return True
    if source == "env":
        euid = os.geteuid()
        if st.st_uid == euid and euid != 0 and not _expected_sha256():
            return False
        return True
    if st.st_uid != 0:
        return False
    if _is_unsafe_location(resolved):
        return False
    return True


def _resolve_executable(explicit: str | None) -> str | None:
    if explicit:
        raw = explicit
        source = "explicit"
    elif os.environ.get("MUTEKI_GC_CLI"):
        raw = os.environ["MUTEKI_GC_CLI"]
        source = "env"
    else:
        found = shutil.which("gc")
        if not found:
            return None
        raw = found
        source = "path"
    path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not _is_trusted_executable(resolved, source=source):
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


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    pid = getattr(proc, "pid", None)
    if pid and hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _default_runner(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
) -> CompletedProcess[str]:
    if not _sha256_matches(argv[0]):
        raise PermissionError("gc cli hash mismatch")
    kwargs: dict = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": cwd,
        "env": env,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(argv[0], *argv[1:], **kwargs)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_process_tree(proc)
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

    key = _challenge_identity(challenge)
    cache_key = (key[0], key[1], requested)
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
