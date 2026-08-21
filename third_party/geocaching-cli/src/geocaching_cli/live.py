"""Optional live adapter around pycaching.

Offline commands must not import this module's network helpers at startup
beyond a cheap availability check. All geocaching.com traffic is explicit.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from geocaching_cli.browser_auth import playwright_login
from geocaching_cli.config import (
    Credentials,
    clear_session,
    load_credentials,
    load_session,
    save_session,
)
from geocaching_cli.errors import LiveError, LiveLoginError
from geocaching_cli.models import CacheRecord, LogRecord

DEFAULT_LIVE_LIMIT = 10
MIN_REQUEST_INTERVAL_S = 0.45


class RateLimiter:
    def __init__(self, interval_s: float = MIN_REQUEST_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        pause = self.interval_s - (now - self._last)
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()


def _import_pycaching():
    try:
        import pycaching
        from pycaching.errors import (
            LoadError,
            LoginFailedException,
            NotLoggedInException,
            PMOnlyException,
            TooManyRequestsError,
        )
        from pycaching.geocaching import Geocaching
        from pycaching.geo import Point
    except ImportError as exc:
        raise LiveError("未安装 pycaching。请执行: pip install 'geocaching-cli[ ]' 或 pip install pycaching") from exc
    return {
        "pycaching": pycaching,
        "Geocaching": Geocaching,
        "Point": Point,
        "LoginFailedException": LoginFailedException,
        "NotLoggedInException": NotLoggedInException,
        "PMOnlyException": PMOnlyException,
        "LoadError": LoadError,
        "TooManyRequestsError": TooManyRequestsError,
    }


def _cookie_map(session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = cookie.domain or ""
        if "geocaching.com" in domain:
            cookies[cookie.name] = cookie.value
    if not cookies:
        cookies = dict(session.cookies.get_dict())
    return cookies


def persist_session(geocaching, cookie_list: list[dict] | None = None) -> None:
    username = getattr(geocaching, "_logged_username", None)
    cookies = _cookie_map(geocaching._session)
    payload = {"username": username, "cookies": cookies}
    if cookie_list:
        payload["cookie_list"] = cookie_list
    save_session(payload)


def _apply_cookies(geocaching, cookies: dict[str, str]) -> None:
    for name, value in cookies.items():
        if not value:
            continue
        geocaching._session.cookies.set(name, value, domain=".geocaching.com", path="/")


def _apply_cookie_list(geocaching, cookie_list: list[dict]) -> None:
    for cookie in cookie_list:
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain") or ""
        if not name or not value or "geocaching.com" not in domain:
            continue
        geocaching._session.cookies.set(
            name,
            value,
            domain=domain,
            path=cookie.get("path") or "/",
        )


def login_with_browser_cookies(cookie_list: list[dict], *, persist: bool = True):
    mods = _import_pycaching()
    Geocaching = mods["Geocaching"]
    geocaching = Geocaching()
    _apply_cookie_list(geocaching, cookie_list)
    login_page = geocaching._request("account/signin", login_check=False)
    username = geocaching.get_logged_user(login_page)
    if not username:
        raise LiveLoginError("浏览器会话 cookie 无效，官网仍未视为已登录。")
    geocaching._logged_in = True
    geocaching._logged_username = username
    if persist:
        persist_session(geocaching, cookie_list=cookie_list)
    return geocaching


def login_via_playwright(creds: Credentials, *, headed: bool | None = None, persist: bool = True):
    cookies, cookie_list = playwright_login(creds.username or "", creds.password or "", headed=headed)
    try:
        return login_with_browser_cookies(cookie_list, persist=persist)
    except LiveLoginError:
        mods = _import_pycaching()
        Geocaching = mods["Geocaching"]
        LoginFailedException = mods["LoginFailedException"]
        geocaching = Geocaching()
        gspk = cookies.get("gspkauth")
        if not gspk:
            raise
        try:
            geocaching.login_with_cookie(gspk, username=creds.username)
        except LoginFailedException as exc:
            raise _login_error(exc) from exc
        if persist:
            persist_session(geocaching, cookie_list=cookie_list)
        return geocaching


def _login_error(exc: Exception) -> LiveLoginError:
    message = str(exc) or exc.__class__.__name__
    captcha = "captcha" in message.lower()
    return LiveLoginError(message, captcha=captcha)


def login(
    creds: Credentials | None = None,
    *,
    persist: bool = True,
    browser: bool = True,
    headed: bool | None = None,
) -> Any:
    """Log in via cookie, then Playwright, then pycaching form POST.

    Playwright is the default password path because a requests POST is
    rejected with reCAPTCHA. This function never prints secrets.
    """
    mods = _import_pycaching()
    Geocaching = mods["Geocaching"]
    LoginFailedException = mods["LoginFailedException"]
    creds = creds or load_credentials()
    geocaching = Geocaching()

    if creds.has_cookie:
        try:
            geocaching.login_with_cookie(
                creds.cookie,
                username=creds.username,
                cookie_name=creds.cookie_name or "gspkauth",
            )
            if persist:
                persist_session(geocaching)
            return geocaching
        except LoginFailedException as exc:
            if not creds.has_password:
                raise _login_error(exc) from exc
        except Exception as exc:
            if not creds.has_password:
                raise LiveLoginError(str(exc)) from exc

    if creds.has_password and browser:
        return login_via_playwright(creds, headed=headed, persist=persist)

    if creds.has_password:
        try:
            geocaching.login(creds.username, creds.password)
            if persist:
                persist_session(geocaching)
            return geocaching
        except LoginFailedException as exc:
            raise _login_error(exc) from exc
        except Exception as exc:
            raise LiveLoginError(str(exc)) from exc

    raise LiveLoginError(
        "未配置登录信息。请设置 GEOCACHING_USERNAME / GEOCACHING_PASSWORD，然后运行 gc auth login。"
    )


def connect(*, force_login: bool = False) -> Any:
    """Reuse a stored session cookie when possible, otherwise log in."""
    mods = _import_pycaching()
    Geocaching = mods["Geocaching"]
    LoginFailedException = mods["LoginFailedException"]

    if not force_login:
        stored = load_session()
        cookie_list = (stored or {}).get("cookie_list") or []
        if cookie_list:
            try:
                return login_with_browser_cookies(cookie_list, persist=False)
            except LiveLoginError:
                clear_session()
        cookies = (stored or {}).get("cookies") or {}
        gspk = cookies.get("gspkauth")
        if gspk:
            geocaching = Geocaching()
            try:
                geocaching.login_with_cookie(gspk, username=stored.get("username") if stored else None)
                return geocaching
            except (LoginFailedException, Exception):
                clear_session()

    return login(load_credentials(), persist=True)


def logged_username(geocaching) -> str | None:
    return getattr(geocaching, "_logged_username", None)


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
    except Exception:
        return default
    return default if value is None else value


def _location(cache) -> tuple[float | None, float | None]:
    try:
        loc = cache.location
    except Exception:
        return None, None
    if loc is None:
        return None, None
    try:
        return float(loc.latitude), float(loc.longitude)
    except Exception:
        return None, None


def cache_to_record(cache, *, source: str = "live") -> CacheRecord:
    lat, lon = _location(cache)
    wp = _safe_attr(cache, "wp") or _safe_attr(cache, "geocode") or ""
    cache_type = _safe_attr(cache, "type")
    if cache_type is not None:
        cache_type = getattr(cache_type, "name", None) or str(cache_type)
    size = _safe_attr(cache, "size")
    if size is not None:
        size = getattr(size, "name", None) or str(size)
    status = _safe_attr(cache, "status")
    archived = None
    available = None
    if status is not None:
        status_name = getattr(status, "name", str(status)).lower()
        archived = "archiv" in status_name
        available = "enable" in status_name or status_name in {"available", "active"}
    found = _safe_attr(cache, "found")
    if found is True and available is None:
        available = True
    return CacheRecord(
        gc_code=str(wp).upper(),
        name=str(_safe_attr(cache, "name") or wp),
        latitude=lat,
        longitude=lon,
        cache_type=cache_type,
        container=size,
        difficulty=_safe_attr(cache, "difficulty"),
        terrain=_safe_attr(cache, "terrain"),
        owner=_safe_attr(cache, "author") or _safe_attr(cache, "owner"),
        placed_at=str(_safe_attr(cache, "hidden") or "") or None,
        available=available,
        archived=archived,
        short_description=_safe_attr(cache, "summary") or None,
        long_description=_safe_attr(cache, "description") or None,
        encoded_hints=_safe_attr(cache, "hint") or None,
        url=_safe_attr(cache, "url") or (f"https://coord.info/{wp}" if wp else None),
        favorited=_safe_attr(cache, "favorites"),
        source=source,
    )


def logs_to_records(logs: Iterable[Any]) -> list[LogRecord]:
    records: list[LogRecord] = []
    for log in logs:
        log_type = _safe_attr(log, "type")
        if log_type is not None:
            log_type = getattr(log_type, "name", None) or str(log_type)
        records.append(
            LogRecord(
                log_id=str(_safe_attr(log, "uuid") or _safe_attr(log, "id") or "") or None,
                logged_at=str(_safe_attr(log, "visited") or "") or None,
                log_type=log_type,
                finder=_safe_attr(log, "author"),
                text=_safe_attr(log, "text"),
            )
        )
    return records


def search_near(geocaching, lat: float, lon: float, *, limit: int = DEFAULT_LIVE_LIMIT) -> list[CacheRecord]:
    mods = _import_pycaching()
    Point = mods["Point"]
    TooManyRequestsError = mods["TooManyRequestsError"]
    limiter = RateLimiter()
    limiter.wait()
    try:
        raw = geocaching.search(Point(lat, lon), limit=limit, wait_sleep=True)
    except TooManyRequestsError as exc:
        raise LiveError(f"触发站点速率限制: {exc}") from exc
    except Exception as exc:
        raise LiveError(f"在线搜索失败: {exc}") from exc

    results: list[CacheRecord] = []
    for cache in raw:
        if cache is None:
            continue
        results.append(cache_to_record(cache, source="live-search"))
        if len(results) >= limit:
            break
    return results


def show_cache(geocaching, gc_code: str, *, quick: bool = False) -> CacheRecord:
    mods = _import_pycaching()
    PMOnlyException = mods["PMOnlyException"]
    LoadError = mods["LoadError"]
    limiter = RateLimiter()
    limiter.wait()
    code = gc_code.strip().upper()
    try:
        cache = geocaching.get_cache(code)
        if quick:
            cache.load_quick()
        else:
            cache.load()
        return cache_to_record(cache, source="live-show")
    except PMOnlyException as exc:
        raise LiveError(f"{code} 为 Premium 会员限定，当前账号无法查看完整详情。") from exc
    except LoadError as exc:
        raise LiveError(f"无法加载 {code}: {exc}") from exc
    except Exception as exc:
        raise LiveError(f"在线读取 {code} 失败: {exc}") from exc


def load_logs(geocaching, gc_code: str, *, limit: int = DEFAULT_LIVE_LIMIT) -> tuple[CacheRecord, list[LogRecord]]:
    mods = _import_pycaching()
    LoadError = mods["LoadError"]
    limiter = RateLimiter()
    limiter.wait()
    code = gc_code.strip().upper()
    try:
        cache = geocaching.get_cache(code)
        cache.load_quick()
        record = cache_to_record(cache, source="live-logs")
        logs = logs_to_records(cache.load_logbook(limit=limit))
        record.logs = logs
        return record, logs
    except LoadError as exc:
        raise LiveError(f"无法加载 {code} 日志: {exc}") from exc
    except Exception as exc:
        raise LiveError(f"读取日志失败: {exc}") from exc


def my_finds(geocaching, *, limit: int = DEFAULT_LIVE_LIMIT) -> list[CacheRecord]:
    limiter = RateLimiter()
    limiter.wait()
    try:
        raw = geocaching.my_finds(limit=limit)
    except Exception as exc:
        raise LiveError(f"读取 my-finds 失败: {exc}") from exc
    results: list[CacheRecord] = []
    for cache in raw:
        if cache is None:
            continue
        results.append(cache_to_record(cache, source="live-finds"))
        if len(results) >= limit:
            break
    return results


def status_payload() -> dict[str, Any]:
    creds = load_credentials()
    session = load_session()
    return {
        "username_configured": bool(creds.username),
        "password_configured": bool(creds.password),
        "cookie_configured": creds.has_cookie,
        "session_cached": bool(session and session.get("cookies")),
        "session_username": (session or {}).get("username"),
        "username": creds.username,
    }
