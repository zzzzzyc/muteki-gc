"""GeoCheck / Certitude coordinate verifier.

Automated tests must stay offline. Live Playwright is optional and never
follows a final URL off the site-family allowlist.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Literal
from urllib.parse import urlparse

from geocaching_cli.browser_auth import (
    _import_playwright,
    _launch_browser_retry,
    headed_from_env,
)
from geocaching_cli.coord import format_dmm, parse_coord
from geocaching_cli.errors import CoordError, LiveError

CheckerSite = Literal["geocheck", "certitude"]

GEOCHECK_HOSTS = frozenset(
    {
        "geocheck.org",
        "www.geocheck.org",
        "geotjek.dk",
        "www.geotjek.dk",
    }
)
CERTITUDE_HOSTS = frozenset(
    {
        "certitude.org",
        "www.certitude.org",
        "certitudes.org",
        "www.certitudes.org",
    }
)

GEOCHECK_SELECTORS = {
    "oneFieldInput": 'input[name="coordOneField"]',
    "multiFieldInputs": {
        "lat": 'input[name="lat"]',
        "latdeg": 'input[name="latdeg"]',
        "latmin": 'input[name="latmin"]',
        "latdec": 'input[name="latdec"]',
        "lon": 'input[name="lon"]',
        "londeg": 'input[name="londeg"]',
        "lonmin": 'input[name="lonmin"]',
        "londec": 'input[name="londec"]',
    },
    "captchaImage": 'img[src="/dimages/captcha.php"]',
    "captchaInput": 'input[name="usercaptcha"]',
    "submitButton": 'input[type="submit"][value="Check"]',
    "successElement": 'input[name="ref"][value="/chkcorrect.php"]',
    "failureElement": "td.alert",
}

CERTITUDE_SELECTORS = {
    "solutionInput": "input#solution",
    "submitButton": '#submitButton, input[type="submit"]',
    "successElement": ".embossed.success",
    "failureElement": ".embossed.error-detail, .error-detail",
}

CAPTCHA_MAX_ATTEMPTS = 100_000
CAPTCHA_RE = re.compile(
    r"validate\w+Form\s*\(\s*this\s*,\s*['\"]([0-9a-fA-F]{32})['\"]\s*\)",
)
SECRET_RE = re.compile(
    r"(?:password|passwd|cookie|gspkauth)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
AUTH_RE = re.compile(
    r"(?i)authorization\s*[:=]\s*bearer\s+\S+|authorization\s*[:=]\s*\S+|bearer\s+\S+",
)
_OPERATIONAL_ALERT_RE = re.compile(
    r"captcha|usercaptcha|verification code|session|\blogin\b",
    re.IGNORECASE,
)
_WRONG_COORD_RE = re.compile(
    r"wrong|incorrect|invalid coord|not (?:the )?correct|bad coord",
    re.IGNORECASE,
)
_MIN_SECRET_LEN = 8
CONSERVATIVE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/192.168.1.3 Safari/537.36"
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag = tag.lower()
        self.attrs = {key.lower(): (value or "") for key, value in attrs}
        self.children: list[_Node] = []
        self.parent: _Node | None = None
        self._texts: list[str] = []

    def classes(self) -> set[str]:
        return {item for item in self.attrs.get("class", "").split() if item}

    @property
    def text(self) -> str:
        parts = list(self._texts)
        for child in self.children:
            parts.append(child.text)
        return "".join(parts)


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", [])
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, attrs)
        parent = self._stack[-1]
        node.parent = parent
        parent.children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name in _VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == name:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self._stack[-1]._texts.append(data)


def _parse_html(html: str) -> _Node:
    parser = _TreeParser()
    parser.feed(html or "")
    parser.close()
    return parser.root


def _walk(node: _Node) -> Iterable[_Node]:
    yield node
    for child in node.children:
        yield from _walk(child)


def _find(root: _Node, pred: Callable[[_Node], bool]) -> list[_Node]:
    return [node for node in _walk(root) if node.tag != "document" and pred(node)]


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host.startswith(("0x", "0X")):
        return True
    if host.isdigit():
        return True
    return False


def detect_checker_site(url: str) -> CheckerSite:
    raw = (url or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        raise LiveError("校验地址无效")
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise LiveError("校验地址必须是 HTTPS")
    if "@" in (parsed.netloc or "") or parsed.username is not None or parsed.password is not None:
        raise LiveError("校验地址不能包含用户信息")
    if parsed.fragment:
        raise LiveError("校验地址不能包含片段")
    if parsed.port is not None and parsed.port != 443:
        raise LiveError("校验地址端口无效")
    host = (parsed.hostname or "").lower()
    if not host or "%" in host or "\x00" in host or host.endswith("."):
        raise LiveError("校验地址无效")
    if host == "localhost" or _looks_like_ip(host):
        raise LiveError("校验地址无效")
    if host in GEOCHECK_HOSTS:
        return "geocheck"
    if host in CERTITUDE_HOSTS:
        return "certitude"
    raise LiveError("校验地址不在允许列表中")


def _family_matches(url: str, site: CheckerSite) -> bool:
    try:
        parsed = urlparse(url or "")
        cleaned = parsed._replace(fragment="").geturl()
        return detect_checker_site(cleaned) == site
    except LiveError:
        return False


def request_is_allowed(url: str, site: CheckerSite) -> bool:
    raw = (url or "").strip()
    if raw.startswith(("about:", "data:", "blob:")):
        return True
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        return False
    if "@" in (parsed.netloc or "") or parsed.username is not None or parsed.password is not None:
        return False
    if parsed.port is not None and parsed.port != 443:
        return False
    host = (parsed.hostname or "").lower()
    if not host or "%" in host or "\x00" in host or host.endswith("."):
        return False
    if host == "localhost" or _looks_like_ip(host):
        return False
    allowed = GEOCHECK_HOSTS if site == "geocheck" else CERTITUDE_HOSTS
    return host in allowed


def install_family_route(context: Any, site: CheckerSite, aborted_nav: list[str] | None = None) -> None:
    aborted = aborted_nav if aborted_nav is not None else []

    def _handler(route: Any) -> None:
        request = getattr(route, "request", None)
        url = getattr(request, "url", "") or ""
        if request_is_allowed(url, site):
            route.continue_()
            return
        resource_type = getattr(request, "resource_type", None)
        if resource_type in {None, "document", "navigation"}:
            aborted.append(url)
        route.abort()

    context.route("**/*", _handler)


def solve_geocheck_captcha(html: str) -> str | None:
    match = CAPTCHA_RE.search(html or "")
    if match is None:
        return None
    target = match.group(1).lower()
    for index in range(CAPTCHA_MAX_ATTEMPTS):
        value = f"{index:05d}"
        if hashlib.md5(value.encode("ascii")).hexdigest() == target:
            return value
    return None


def _known_secrets() -> list[str]:
    secrets: list[str] = []
    for key in ("GEOCACHING_PASSWORD", "GEOCACHING_COOKIE", "GC_SERVE_TOKEN"):
        value = os.environ.get(key)
        if value and len(value) >= _MIN_SECRET_LEN:
            secrets.append(value)
    try:
        from geocaching_cli.config import load_credentials, load_session

        creds = load_credentials()
        if creds.password and len(creds.password) >= _MIN_SECRET_LEN:
            secrets.append(creds.password)
        if creds.cookie and len(creds.cookie) >= _MIN_SECRET_LEN:
            secrets.append(creds.cookie)
        session = load_session() or {}
        cookies = session.get("cookies") or {}
        if isinstance(cookies, dict):
            secrets.extend(
                str(item) for item in cookies.values() if item and len(str(item)) >= _MIN_SECRET_LEN
            )
    except Exception:
        pass
    return [item for item in secrets if item and len(item) >= _MIN_SECRET_LEN]


def _safe_message(text: str) -> str:
    cleaned = (text or "").replace("\u00a0", " ")
    cleaned = AUTH_RE.sub("", cleaned)
    cleaned = SECRET_RE.sub("", cleaned)
    for secret in _known_secrets():
        if secret:
            cleaned = cleaned.replace(secret, "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 500:
        cleaned = cleaned[:500]
    return cleaned


def _result(
    *,
    ok: bool,
    site: str,
    message: str,
    attempts: int,
    coord_text: str,
    definitive: bool,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "site": site,
        "message": _safe_message(message),
        "attempts": int(attempts),
        "coord_text": coord_text,
        "definitive": bool(definitive),
    }


def _normalize_coord_text(coord_text: str) -> str:
    return format_dmm(parse_coord(coord_text))


def _coords_equal(exposed: str | None, expected: str) -> bool | None:
    if not exposed or not exposed.strip():
        return None
    try:
        return format_dmm(parse_coord(exposed)) == format_dmm(parse_coord(expected))
    except CoordError:
        return None


def _next_sibling(node: _Node) -> _Node | None:
    parent = node.parent
    if parent is None:
        return None
    siblings = parent.children
    try:
        index = siblings.index(node)
    except ValueError:
        return None
    if index + 1 >= len(siblings):
        return None
    return siblings[index + 1]


def _extract_geocheck_coord(root: _Node) -> str | None:
    for cell in _find(root, lambda node: node.tag == "td"):
        if cell.text.replace("\u00a0", " ").strip() != "Coordinate:":
            continue
        sibling = _next_sibling(cell)
        if sibling is None:
            return None
        cachedata = _find(sibling, lambda node: "cachedata" in node.classes())
        raw = cachedata[0].text if cachedata else sibling.text
        return raw.replace("\u00a0", " ").strip() or None
    return None


def _extract_certitude_coord(root: _Node) -> str | None:
    solutions = _find(root, lambda node: node.attrs.get("id") == "solution")
    if solutions:
        node = solutions[0]
        if node.tag == "input":
            value = node.attrs.get("value", "").replace("\u00a0", " ").strip()
            if value:
                return value
        else:
            text = node.text.replace("\u00a0", " ").strip()
            if text:
                return text
    successes = _find(
        root,
        lambda node: "embossed" in node.classes() and "success" in node.classes(),
    )
    if len(successes) > 1:
        return successes[-1].text.replace("\u00a0", " ").strip() or None
    if successes:
        return successes[0].text.replace("\u00a0", " ").strip() or None
    return None


def _alert_is_operational(text: str) -> bool:
    return bool(_OPERATIONAL_ALERT_RE.search(text or ""))


def _alert_is_coord_reject(text: str) -> bool:
    if _alert_is_operational(text):
        return False
    return bool(_WRONG_COORD_RE.search(text or ""))


def _extract_geocheck_message(root: _Node, *, success: bool, failure: bool) -> str:
    if failure:
        alerts = _find(root, lambda node: node.tag == "td" and "alert" in node.classes())
        if alerts:
            return alerts[0].text
    boxes = _find(
        root,
        lambda node: node.tag == "div"
        and "common-text_e22jW" in node.classes()
        and "cos-font-medium" in node.classes(),
    )
    if boxes:
        italics = _find(boxes[0], lambda node: node.tag == "i")
        return (italics[0].text if italics else boxes[0].text)
    if success:
        return "correct"
    if failure:
        return "incorrect"
    return "no verdict"


def _extract_certitude_message(root: _Node, *, success: bool, failure: bool) -> str:
    if failure:
        details = _find(root, lambda node: "error-detail" in node.classes())
        if details:
            return details[0].text
    headings = _find(root, lambda node: node.tag == "h3" and "embossed" in node.classes())
    if headings:
        return headings[0].text
    if success:
        return "correct"
    if failure:
        return "incorrect"
    return "no verdict"


def parse_checker_result(site: str, html: str, *, coord_text: str) -> dict[str, Any]:
    try:
        normalized = _normalize_coord_text(coord_text)
    except CoordError:
        normalized = coord_text
    root = _parse_html(html)
    kind = "certitude" if site == "certitude" else "geocheck"
    if kind == "geocheck":
        success = bool(
            _find(
                root,
                lambda node: node.tag == "input"
                and node.attrs.get("name") == "ref"
                and node.attrs.get("value") == "/chkcorrect.php",
            )
        )
        failure = bool(_find(root, lambda node: node.tag == "td" and "alert" in node.classes()))
        exposed = _extract_geocheck_coord(root)
        message = _extract_geocheck_message(root, success=success, failure=failure)
    else:
        success = bool(
            _find(
                root,
                lambda node: "embossed" in node.classes() and "success" in node.classes(),
            )
        )
        failure = bool(_find(root, lambda node: "error-detail" in node.classes()))
        exposed = _extract_certitude_coord(root)
        message = _extract_certitude_message(root, success=success, failure=failure)

    if success:
        matched = _coords_equal(exposed, normalized)
        if matched is False:
            return _result(
                ok=False,
                site=kind,
                message="coordinate_mismatch",
                attempts=1,
                coord_text=normalized,
                definitive=False,
            )
        return _result(
            ok=True,
            site=kind,
            message=message or "correct",
            attempts=1,
            coord_text=normalized,
            definitive=True,
        )
    if failure:
        definitive = True
        if kind == "geocheck":
            if _alert_is_operational(message) or not _alert_is_coord_reject(message):
                definitive = False
        return _result(
            ok=False,
            site=kind,
            message=message or "incorrect",
            attempts=1,
            coord_text=normalized,
            definitive=definitive,
        )
    return _result(
        ok=False,
        site=kind,
        message=message or "no verdict",
        attempts=1,
        coord_text=normalized,
        definitive=False,
    )


def _set_named_field(page: Any, name: str, value: str) -> bool:
    radio = page.locator(f'input[name="{name}"][type="radio"][value="{value}"]')
    if radio.count() > 0:
        radio.first.check()
        return True
    valued = page.locator(f'input[name="{name}"][value="{value}"]')
    if valued.count() > 0:
        typ = None
        try:
            typ = valued.first.get_attribute("type")
        except Exception:
            typ = None
        if typ == "radio" or name in {"lat", "lon"}:
            valued.first.check()
            return True
    select = page.locator(f'select[name="{name}"]')
    if select.count() > 0:
        select.first.select_option(str(value))
        return True
    primary = GEOCHECK_SELECTORS["multiFieldInputs"].get(name) or f'input[name="{name}"]'
    locator = page.locator(primary)
    if locator.count() > 0:
        locator.first.fill(str(value))
        return True
    return False


def fill_geocheck_one_field(page: Any, coord_text: str) -> None:
    page.locator(GEOCHECK_SELECTORS["oneFieldInput"]).first.fill(_normalize_coord_text(coord_text))


def fill_geocheck_multi_field(page: Any, coord_text: str) -> None:
    lat_hem, lat_deg, lat_min, lon_hem, lon_deg, lon_min = _normalize_coord_text(coord_text).split()
    _set_named_field(page, "lat", lat_hem)
    _set_named_field(page, "latdeg", lat_deg)
    dec_selector = GEOCHECK_SELECTORS["multiFieldInputs"]["latdec"]
    if page.locator(dec_selector).count() > 0:
        lat_whole, lat_dec = lat_min.split(".")
        _set_named_field(page, "latmin", lat_whole)
        _set_named_field(page, "latdec", lat_dec)
    else:
        _set_named_field(page, "latmin", lat_min)
    _set_named_field(page, "lon", lon_hem)
    _set_named_field(page, "londeg", lon_deg)
    lon_dec_selector = GEOCHECK_SELECTORS["multiFieldInputs"]["londec"]
    if page.locator(lon_dec_selector).count() > 0:
        lon_whole, lon_dec = lon_min.split(".")
        _set_named_field(page, "lonmin", lon_whole)
        _set_named_field(page, "londec", lon_dec)
    else:
        _set_named_field(page, "lonmin", lon_min)


def fill_certitude_solution(page: Any, coord_text: str) -> None:
    locator = page.locator(CERTITUDE_SELECTORS["solutionInput"])
    if locator.count() > 0:
        locator.first.fill(_normalize_coord_text(coord_text))


def _submit_checker(page: Any, site: CheckerSite) -> None:
    selector = (
        CERTITUDE_SELECTORS["submitButton"]
        if site == "certitude"
        else GEOCHECK_SELECTORS["submitButton"]
    )
    locator = page.locator(selector)
    if locator.count() > 0:
        locator.first.click()


def _fill_checker(page: Any, site: CheckerSite, coord_text: str) -> str | None:
    if site == "certitude":
        fill_certitude_solution(page, coord_text)
        return None
    one = page.locator(GEOCHECK_SELECTORS["oneFieldInput"])
    if one.count() > 0:
        fill_geocheck_one_field(page, coord_text)
    else:
        fill_geocheck_multi_field(page, coord_text)
    html = page.content()
    if CAPTCHA_RE.search(html or ""):
        captcha = solve_geocheck_captcha(html)
        if not captcha:
            return "captcha_unsolved"
        field = page.locator(GEOCHECK_SELECTORS["captchaInput"])
        if field.count() > 0:
            field.first.fill(captcha)
    return None


def _wait_selectors(site: CheckerSite) -> str:
    if site == "certitude":
        return (
            f"{CERTITUDE_SELECTORS['successElement']}, "
            f"{CERTITUDE_SELECTORS['failureElement']}"
        )
    return (
        f"{GEOCHECK_SELECTORS['successElement']}, "
        f"{GEOCHECK_SELECTORS['failureElement']}"
    )


def check_geocheck(
    url: str,
    coord_text: str,
    *,
    headed: bool = False,
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    site = detect_checker_site(url)
    normalized = _normalize_coord_text(coord_text)
    try:
        sync_playwright, playwright_timeout = _import_playwright()
    except LiveError:
        return _result(
            ok=False,
            site=site,
            message="operational_error",
            attempts=1,
            coord_text=normalized,
            definitive=False,
        )

    use_headed = headed_from_env(explicit=headed)
    timeout_ms = max(1, int(float(timeout_s) * 1000))
    browser = None
    context = None
    aborted_nav: list[str] = []

    def _operational() -> dict[str, Any]:
        return _result(
            ok=False,
            site=site,
            message="operational_error",
            attempts=1,
            coord_text=normalized,
            definitive=False,
        )

    def _rejected() -> dict[str, Any]:
        return _result(
            ok=False,
            site=site,
            message="redirect_rejected",
            attempts=1,
            coord_text=normalized,
            definitive=False,
        )

    try:
        with sync_playwright() as playwright:
            try:
                browser = _launch_browser_retry(sync_playwright, playwright, headed=use_headed)
            except LiveError:
                return _operational()
            try:
                context = browser.new_context(
                    locale="en-US",
                    user_agent=CONSERVATIVE_UA,
                )
                install_family_route(context, site, aborted_nav)
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except playwright_timeout:
                    return _result(
                        ok=False,
                        site=site,
                        message="timeout",
                        attempts=1,
                        coord_text=normalized,
                        definitive=False,
                    )
                except Exception:
                    return _operational()
                if aborted_nav or not _family_matches(getattr(page, "url", ""), site):
                    return _rejected()
                fill_error = _fill_checker(page, site, normalized)
                if fill_error == "captcha_unsolved":
                    return _result(
                        ok=False,
                        site=site,
                        message="captcha_unsolved",
                        attempts=1,
                        coord_text=normalized,
                        definitive=False,
                    )
                _submit_checker(page, site)
                try:
                    page.wait_for_selector(_wait_selectors(site), timeout=timeout_ms)
                except playwright_timeout:
                    pass
                if aborted_nav or not _family_matches(getattr(page, "url", ""), site):
                    return _rejected()
                return parse_checker_result(site, page.content(), coord_text=normalized)
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except playwright_timeout:
        return _result(
            ok=False,
            site=site,
            message="timeout",
            attempts=1,
            coord_text=normalized,
            definitive=False,
        )
    except Exception:
        return _operational()
