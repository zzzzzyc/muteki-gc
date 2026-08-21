"""Offline GeoCheck / Certitude verifier tests. No live network."""

from __future__ import annotations

import hashlib
import json
import re
import socket
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from geocaching_cli.cli import app
from geocaching_cli.coord import format_dmm, parse_coord
from geocaching_cli.errors import CoordError, LiveError, LiveLoginError

from geocaching_cli.checker import (
    CAPTCHA_MAX_ATTEMPTS,
    CERTITUDE_SELECTORS,
    GEOCHECK_SELECTORS,
    check_geocheck,
    detect_checker_site,
    fill_geocheck_multi_field,
    fill_geocheck_one_field,
    parse_checker_result,
    solve_geocheck_captcha,
)

runner = CliRunner()

COORD = "N 39 54.252 E 116 24.444"
OTHER_COORD = "N 00 00.000 E 000 00.000"
MD5_12345 = hashlib.md5(b"12345").hexdigest()
MD5_00000 = hashlib.md5(b"00000").hexdigest()
MD5_99999 = hashlib.md5(b"99999").hexdigest()

# autoGC src/utils/selectors.ts — tests lock these exact values.
AUTOGC_GEOCHECK = {
    "oneFieldInput": 'input[name="coordOneField"]',
    "lat": "lat",
    "latdeg": "latdeg",
    "latmin": "latmin",
    "latdec": "latdec",
    "lon": "lon",
    "londeg": "londeg",
    "lonmin": "lonmin",
    "londec": "londec",
    "captchaInput": 'input[name="usercaptcha"]',
    "submitButton": 'input[type="submit"][value="Check"]',
    "successElement": 'input[name="ref"][value="/chkcorrect.php"]',
    "failureElement": "td.alert",
}
AUTOGC_CERTITUDE = {
    "solutionInput": "input#solution",
    "submitButton": '#submitButton, input[type="submit"]',
    "successElement": ".embossed.success",
    "failureElement": ".embossed.error-detail, .error-detail",
}

RESULT_KEYS = ("ok", "site", "message", "attempts", "coord_text", "definitive")


@pytest.fixture(autouse=True)
def _block_outbound_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("checker tests must not open network sockets")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


def _assert_result_shape(payload: dict[str, Any]) -> None:
    assert tuple(payload) == RESULT_KEYS or set(payload) >= set(RESULT_KEYS)
    for key in RESULT_KEYS:
        assert key in payload
    assert isinstance(payload["ok"], bool)
    assert payload["site"] in {"geocheck", "certitude"}
    assert isinstance(payload["message"], str)
    assert len(payload["message"]) <= 500
    assert isinstance(payload["attempts"], int)
    assert payload["attempts"] >= 1
    assert isinstance(payload["coord_text"], str)
    assert isinstance(payload["definitive"], bool)


def geocheck_success_html(coord: str = "N 39° 54.252 E 116° 24.444", message: str = "Congratulations") -> str:
    return f"""
    <html><body>
      <input name="ref" value="/chkcorrect.php">
      <table>
        <tr><td>Coordinate:</td><td><span class="cachedata">{coord}</span></td></tr>
      </table>
      <div class="common-text_e22jW cos-font-medium"><i>{message}</i></div>
    </body></html>
    """


def geocheck_failure_html(message: str = "Wrong coordinates") -> str:
    return f"<html><body><table><tr><td class='alert'>{message}</td></tr></table></body></html>"


def certitude_success_html(coord: str = COORD, message: str = "Well done") -> str:
    return f"""
    <html><body>
      <h3 class="embossed">{message}</h3>
      <div class="embossed success">Correct!</div>
      <div class="embossed success">{coord}</div>
      <div id="solution">{coord}</div>
    </body></html>
    """


def certitude_failure_html(message: str = "Incorrect solution") -> str:
    return f'<html><body><div class="embossed error-detail">{message}</div></body></html>'


# --- allowlist / SSRF -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "site"),
    [
        ("https://geocheck.org/geo_chk.php?gid=1", "geocheck"),
        ("https://www.geocheck.org/foo", "geocheck"),
        ("https://geotjek.dk/bar", "geocheck"),
        ("https://www.geotjek.dk/bar", "geocheck"),
        ("https://certitude.org/check", "certitude"),
        ("https://www.certitude.org/check", "certitude"),
        ("https://certitudes.org/check", "certitude"),
        ("https://www.certitudes.org/check", "certitude"),
        ("https://geocheck.org:443/ok", "geocheck"),
        ("HTTPS://WWW.GEOCHECK.ORG/OK", "geocheck"),
    ],
)
def test_allowlist_accepts_exact_https_hosts(url: str, site: str) -> None:
    assert detect_checker_site(url) == site


@pytest.mark.parametrize(
    "url",
    [
        "http://geocheck.org/foo",
        "https://user:pass@geocheck.org/foo",
        "https://user@geocheck.org/foo",
        "https://geocheck.org:8443/foo",
        "https://geocheck.org:80/foo",
        "https://geocheck.org/foo#frag",
        "https://geocheck.org#frag",
        "https://localhost/foo",
        "https://localhost:443/foo",
        "https://127.0.0.1/foo",
        "https://[::1]/foo",
        "https://192.168.1.3/foo",
        "https://8.8.8.8/foo",
        "https://geocheck.org.evil.com/foo",
        "https://www.geocheck.org.evil.com/foo",
        "https://evilgeocheck.org/foo",
        "https://geocheck.org.evil.com",
        "https://not-geocheck.org/foo",
        "https://geocheck.org@evil.com/foo",
        "https://evil.com/?next=https://geocheck.org",
        "ftp://geocheck.org/foo",
        "https://geocheck.org./foo",
        "//geocheck.org/foo",
        "geocheck.org/foo",
        "https://geocheck.org%00.evil.com/",
        "javascript:alert(1)",
        "https://2130706433/",
        "https://0x7f000001/",
    ],
)
def test_allowlist_rejects_ssrf_tricks(url: str) -> None:
    with pytest.raises(LiveError) as excinfo:
        detect_checker_site(url)
    assert "password" not in str(excinfo.value).lower() or "user:pass" not in url
    assert "cookie" not in str(excinfo.value).lower()


def test_allowlist_rejects_userinfo_without_echoing_secret() -> None:
    with pytest.raises(LiveError) as excinfo:
        detect_checker_site("https://admin:s3cret-token@geocheck.org/x")
    text = str(excinfo.value)
    assert "s3cret-token" not in text
    assert "admin" not in text


# --- captcha --------------------------------------------------------------------------


def test_captcha_known_hash() -> None:
    html = f"<form onsubmit=\"return validateChkForm(this, '{MD5_12345}')\">"
    assert solve_geocheck_captcha(html) == "12345"


def test_captcha_uppercase_hash_and_flexible_quotes() -> None:
    html = f'validateOtherForm( this , "{MD5_12345.upper()}" )'
    assert solve_geocheck_captcha(html) == "12345"


def test_captcha_no_match_returns_none() -> None:
    html = "validateChkForm(this, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
    assert solve_geocheck_captcha(html) is None


def test_captcha_last_value_and_bounded_loop() -> None:
    html = f"validateGeoForm(this, '{MD5_99999}')"
    assert solve_geocheck_captcha(html) == "99999"
    assert CAPTCHA_MAX_ATTEMPTS == 100_000
    assert solve_geocheck_captcha("validateChkForm(this, 'ffffffffffffffffffffffffffffffff')") is None


def test_captcha_zero_padded_start() -> None:
    html = f"validateChkForm(this, '{MD5_00000}')"
    assert solve_geocheck_captcha(html) == "00000"


# --- selectors match autoGC -----------------------------------------------------------


def test_selectors_match_autogc() -> None:
    assert GEOCHECK_SELECTORS["oneFieldInput"] == AUTOGC_GEOCHECK["oneFieldInput"]
    assert GEOCHECK_SELECTORS["captchaInput"] == AUTOGC_GEOCHECK["captchaInput"]
    assert GEOCHECK_SELECTORS["submitButton"] == AUTOGC_GEOCHECK["submitButton"]
    assert GEOCHECK_SELECTORS["successElement"] == AUTOGC_GEOCHECK["successElement"]
    assert GEOCHECK_SELECTORS["failureElement"] == AUTOGC_GEOCHECK["failureElement"]
    for name in ("lat", "latdeg", "latmin", "latdec", "lon", "londeg", "lonmin", "londec"):
        assert name in GEOCHECK_SELECTORS["multiFieldInputs"]
        assert GEOCHECK_SELECTORS["multiFieldInputs"][name] == f'input[name="{name}"]'
    assert CERTITUDE_SELECTORS["solutionInput"] == AUTOGC_CERTITUDE["solutionInput"]
    assert CERTITUDE_SELECTORS["submitButton"] == AUTOGC_CERTITUDE["submitButton"]
    assert CERTITUDE_SELECTORS["successElement"] == AUTOGC_CERTITUDE["successElement"]
    assert CERTITUDE_SELECTORS["failureElement"] == AUTOGC_CERTITUDE["failureElement"]


# --- offline HTML parsing -------------------------------------------------------------


def test_geocheck_success_binds_normalized_coord() -> None:
    result = parse_checker_result("geocheck", geocheck_success_html(), coord_text=COORD)
    _assert_result_shape(result)
    assert result["ok"] is True
    assert result["definitive"] is True
    assert result["site"] == "geocheck"
    assert result["coord_text"] == format_dmm(parse_coord(COORD))
    assert "congrat" in result["message"].lower() or result["message"]


def test_geocheck_failure_is_definitive_reject() -> None:
    result = parse_checker_result("geocheck", geocheck_failure_html(), coord_text=COORD)
    assert result["ok"] is False
    assert result["definitive"] is True
    assert "wrong" in result["message"].lower()


def test_geocheck_unknown_is_not_definitive() -> None:
    result = parse_checker_result("geocheck", "<html><body><p>enter coords</p></body></html>", coord_text=COORD)
    assert result["ok"] is False
    assert result["definitive"] is False


def test_geocheck_coordinate_mismatch() -> None:
    html = geocheck_success_html(coord=OTHER_COORD)
    result = parse_checker_result("geocheck", html, coord_text=COORD)
    assert result["ok"] is False
    assert result["definitive"] is False
    assert "coordinate_mismatch" in result["message"]


def test_certitude_success_failure_unknown_mismatch() -> None:
    ok = parse_checker_result("certitude", certitude_success_html(), coord_text=COORD)
    assert ok["ok"] is True and ok["definitive"] is True
    assert ok["site"] == "certitude"

    bad = parse_checker_result("certitude", certitude_failure_html(), coord_text=COORD)
    assert bad["ok"] is False and bad["definitive"] is True
    assert "incorrect" in bad["message"].lower()

    alt = parse_checker_result(
        "certitude",
        '<div class="error-detail">Nope</div>',
        coord_text=COORD,
    )
    assert alt["ok"] is False and alt["definitive"] is True

    unknown = parse_checker_result("certitude", "<html><p>loading</p></html>", coord_text=COORD)
    assert unknown["ok"] is False and unknown["definitive"] is False

    mismatch = parse_checker_result(
        "certitude",
        certitude_success_html(coord=OTHER_COORD),
        coord_text=COORD,
    )
    assert mismatch["ok"] is False
    assert mismatch["definitive"] is False
    assert "coordinate_mismatch" in mismatch["message"]


def test_message_whitespace_cap_and_no_secrets() -> None:
    long_msg = "  hello   " + ("X" * 600) + "  password=hunter2 cookie=gspkauth.abc  "
    html = geocheck_failure_html(long_msg)
    result = parse_checker_result("geocheck", html, coord_text=COORD)
    assert "  " not in result["message"]
    assert len(result["message"]) <= 500
    assert "hunter2" not in result["message"]
    assert "gspkauth" not in result["message"]
    assert "password=" not in result["message"].lower()
    assert "cookie=" not in result["message"].lower()


# --- fill helpers ---------------------------------------------------------------------


def _selector_field_name(selector: str) -> str:
    match = re.search(r'name="([^"]+)"', selector)
    return match.group(1) if match else ""


class FakeLocator:
    def __init__(self, page: "FakeFormPage", selector: str) -> None:
        self._page = page
        self.selector = selector
        self._used_first = False

    @property
    def first(self) -> FakeLocator:
        self._used_first = True
        self._page.first_used.append(self.selector)
        return self

    def count(self) -> int:
        return self._page.count_for(self.selector)

    def _require_first(self, action: str) -> None:
        if self._page.require_first and not self._used_first:
            raise AssertionError(f"must use .first before {action}: {self.selector}")

    def fill(self, value: str, timeout: int = 0) -> None:
        self._require_first("fill")
        if self.count() == 0:
            raise RuntimeError(f"missing {self.selector}")
        kind = self._page.kind_for(self.selector)
        if kind in {"radio", "select"}:
            raise AssertionError(f"{kind} must not receive fill: {self.selector}")
        self._page.filled[self.selector] = value

    def check(self, timeout: int = 0) -> None:
        self._require_first("check")
        if self._page.kind_for(self.selector) != "radio":
            raise AssertionError(f"check only for radio: {self.selector}")
        self._page.checked.append(self.selector)

    def select_option(self, value: str, timeout: int = 0) -> None:
        self._require_first("select_option")
        if self._page.kind_for(self.selector) != "select":
            raise AssertionError(f"select_option only for select: {self.selector}")
        self._page.selected[self.selector] = value

    def click(self, timeout: int = 0) -> None:
        self._require_first("click")
        self._page.clicked.append(self.selector)
        owner = getattr(self._page, "owner", None)
        if owner is not None:
            owner.handle_click(self.selector)

    def get_attribute(self, name: str) -> str | None:
        if name == "type" and self._page.kind_for(self.selector) == "radio":
            return "radio"
        return self._page.attrs.get(self.selector, {}).get(name)


class FakeFormPage:
    def __init__(self, present: set[str], *, require_first: bool = False) -> None:
        self.present = set(present)
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.checked: list[str] = []
        self.selected: dict[str, str] = {}
        self.first_used: list[str] = []
        self.require_first = require_first
        self.select_names: set[str] = set()
        self.attrs: dict[str, dict[str, str]] = {}
        self.owner: Any = None

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def kind_for(self, selector: str) -> str:
        if "select[" in selector or selector.startswith("select"):
            return "select"
        name = _selector_field_name(selector)
        if name in self.select_names:
            return "select"
        if '[type="radio"]' in selector or name in {"lat", "lon"}:
            return "radio"
        return "input"

    def count_for(self, selector: str) -> int:
        name = _selector_field_name(selector)
        radio_query = '[type="radio"]' in selector or (name in {"lat", "lon"} and "[value=" in selector)
        if radio_query:
            return 1 if name in {"lat", "lon"} else 0
        if "select[" in selector:
            if name in self.select_names or selector in self.present or f'select[name="{name}"]' in self.present:
                return 1
            return 0
        if selector in self.present:
            return 1
        if name and f'input[name="{name}"]' in self.present:
            return 1
        return 0


def test_fill_one_field_uses_normalized_dmm() -> None:
    page = FakeFormPage({GEOCHECK_SELECTORS["oneFieldInput"]}, require_first=True)
    fill_geocheck_one_field(page, "39.9042,116.4074")
    assert page.filled[GEOCHECK_SELECTORS["oneFieldInput"]] == COORD
    assert GEOCHECK_SELECTORS["oneFieldInput"] in page.first_used


def test_fill_multi_field_without_dec() -> None:
    present = {GEOCHECK_SELECTORS["multiFieldInputs"][name] for name in ("lat", "latdeg", "latmin", "lon", "londeg", "lonmin")}
    page = FakeFormPage(present, require_first=True)
    fill_geocheck_multi_field(page, COORD)
    fields = GEOCHECK_SELECTORS["multiFieldInputs"]
    assert any('name="lat"' in sel and 'value="N"' in sel for sel in page.checked)
    assert any('name="lon"' in sel and 'value="E"' in sel for sel in page.checked)
    assert fields["lat"] not in page.filled
    assert fields["lon"] not in page.filled
    assert page.filled[fields["latdeg"]] == "39"
    assert page.filled[fields["latmin"]] == "54.252"
    assert str(int(page.filled[fields["londeg"]])) == "116"
    assert page.filled[fields["lonmin"]] == "24.444"


def test_fill_multi_field_with_dec_split() -> None:
    names = ("lat", "latdeg", "latmin", "latdec", "lon", "londeg", "lonmin", "londec")
    present = {GEOCHECK_SELECTORS["multiFieldInputs"][name] for name in names}
    page = FakeFormPage(present, require_first=True)
    fill_geocheck_multi_field(page, COORD)
    fields = GEOCHECK_SELECTORS["multiFieldInputs"]
    assert page.filled[fields["latmin"]] == "54"
    assert page.filled[fields["latdec"]] == "252"
    assert page.filled[fields["lonmin"]] == "24"
    assert page.filled[fields["londec"]] == "444"
    assert any('value="N"' in sel for sel in page.checked)


def test_fill_radio_and_select_reject_fill() -> None:
    page = FakeFormPage(set(), require_first=True)
    page.select_names.add("latdeg")
    page.present.add('select[name="latdeg"]')
    page.present.add('input[name="latmin"]')
    page.present.add('input[name="lonmin"]')
    page.present.add('input[name="londeg"]')
    fill_geocheck_multi_field(page, COORD)
    assert any('value="N"' in sel for sel in page.checked)
    assert page.selected.get('select[name="latdeg"]') == "39"
    assert 'select[name="latdeg"]' not in page.filled
    assert all("latdeg" not in key or "select" not in key for key in page.filled)


# --- mocked Playwright ----------------------------------------------------------------


class FakeTimeout(Exception):
    pass


class LiveLocator(FakeLocator):
    pass


class FakeRoute:
    def __init__(self, url: str, resource_type: str = "document") -> None:
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.aborted = False
        self.continued = False

    def continue_(self, **_kwargs: Any) -> None:
        if self.aborted:
            raise AssertionError("continue after abort")
        self.continued = True

    def abort(self, **_kwargs: Any) -> None:
        self.aborted = True


class LivePage:
    def __init__(
        self,
        *,
        html: str,
        start_url: str,
        final_url: str | None = None,
        timeout: bool = False,
        after_submit_url: str | None = None,
        after_submit_html: str | None = None,
        goto_error: Exception | None = None,
    ) -> None:
        self._html = html
        self.start_url = start_url
        self.final_url = final_url or start_url
        self.url = start_url
        self.timeout = timeout
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.checked: list[str] = []
        self.selected: dict[str, str] = {}
        self.goto_calls: list[dict[str, Any]] = []
        self.context: LiveContext | None = None
        self.after_submit_url = after_submit_url
        self.after_submit_html = after_submit_html
        self.goto_error = goto_error
        self.aborted_nav = False
        self.present = {
            GEOCHECK_SELECTORS["oneFieldInput"],
            GEOCHECK_SELECTORS["captchaInput"],
            GEOCHECK_SELECTORS["submitButton"],
            CERTITUDE_SELECTORS["solutionInput"],
            CERTITUDE_SELECTORS["submitButton"],
            "#submitButton",
            'input[type="submit"]',
            GEOCHECK_SELECTORS["successElement"],
            GEOCHECK_SELECTORS["failureElement"],
            CERTITUDE_SELECTORS["successElement"],
            ".embossed.error-detail",
            ".error-detail",
        }

    def locator(self, selector: str) -> FakeLocator:
        page = FakeFormPage(self.present)
        page.filled = self.filled
        page.clicked = self.clicked
        page.checked = self.checked
        page.selected = self.selected
        page.owner = self
        return FakeLocator(page, selector)

    def _navigate(self, target: str, *, resource_type: str = "document") -> bool:
        if self.context is not None and self.context.handlers:
            route = self.context.dispatch(target, resource_type=resource_type)
            if route.aborted:
                self.aborted_nav = self.aborted_nav or resource_type == "document"
                return False
        return True

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        if self.goto_error is not None:
            raise self.goto_error
        if not self._navigate(self.final_url):
            return
        self.url = self.final_url

    def handle_click(self, selector: str) -> None:
        submit_like = "submit" in selector.lower() or "Check" in selector
        if not submit_like or not self.after_submit_url:
            return
        if not self._navigate(self.after_submit_url):
            return
        self.url = self.after_submit_url
        if self.after_submit_html is not None:
            self._html = self.after_submit_html

    def content(self) -> str:
        return self._html

    def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        if self.timeout:
            raise FakeTimeout("selector wait timed out")


class LiveContext:
    def __init__(self, page: LivePage, closed: dict[str, bool], kwargs: dict[str, Any]) -> None:
        self.page = page
        self.closed = closed
        self.kwargs = kwargs
        self.handlers: list[Any] = []
        self.page_created = False
        self.route_installed_before_page = False
        self.dispatched: list[FakeRoute] = []

    def route(self, pattern: str, handler: Any) -> None:
        if not self.page_created:
            self.route_installed_before_page = True
        self.handlers.append(handler)

    def dispatch(self, url: str, resource_type: str = "document") -> FakeRoute:
        route = FakeRoute(url, resource_type=resource_type)
        for handler in self.handlers:
            handler(route)
        self.dispatched.append(route)
        return route

    def new_page(self) -> LivePage:
        self.page_created = True
        self.page.context = self
        return self.page

    def close(self) -> None:
        self.closed["context"] = True


class LiveBrowser:
    def __init__(self, page: LivePage, closed: dict[str, bool]) -> None:
        self.page = page
        self.closed = closed
        self.contexts: list[LiveContext] = []
        self.launch_kwargs: dict[str, Any] = {}

    def new_context(self, **kwargs: Any) -> LiveContext:
        ctx = LiveContext(self.page, self.closed, kwargs)
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed["browser"] = True


class LivePlaywright:
    def __init__(self, browser: LiveBrowser) -> None:
        self.browser = browser

    def __enter__(self) -> LivePlaywright:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None


def _install_playwright(
    monkeypatch: pytest.MonkeyPatch,
    page: LivePage,
    *,
    headed_holder: dict[str, Any] | None = None,
    launch_error: Exception | None = None,
) -> tuple[dict[str, bool], LiveBrowser]:
    closed = {"context": False, "browser": False}
    browser = LiveBrowser(page, closed)

    def fake_import():
        return (lambda: LivePlaywright(browser), FakeTimeout)

    def fake_launch(_sync, _pw, *, headed: bool):
        if launch_error is not None:
            raise launch_error
        if headed_holder is not None:
            headed_holder["headed"] = headed
        return browser

    monkeypatch.setattr("geocaching_cli.checker._import_playwright", fake_import)
    monkeypatch.setattr("geocaching_cli.checker._launch_browser_retry", fake_launch)
    monkeypatch.setattr(
        "geocaching_cli.browser_auth.playwright_login",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not call playwright_login")),
    )
    return closed, browser


def test_mocked_playwright_success(monkeypatch: pytest.MonkeyPatch) -> None:
    html = geocheck_success_html() + f"validateChkForm(this, '{MD5_12345}')"
    page = LivePage(html=html, start_url="https://geocheck.org/chk")
    headed: dict[str, Any] = {}
    closed, _browser = _install_playwright(monkeypatch, page, headed_holder=headed)
    result = check_geocheck("https://geocheck.org/chk", COORD, headed=False, timeout_s=5)
    _assert_result_shape(result)
    assert result["ok"] is True
    assert result["definitive"] is True
    assert result["coord_text"] == COORD
    assert headed["headed"] is False
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"
    assert page.filled[GEOCHECK_SELECTORS["captchaInput"]] == "12345"
    assert GEOCHECK_SELECTORS["submitButton"] in page.clicked
    assert closed == {"context": True, "browser": True}


def test_mocked_playwright_context_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    page = LivePage(html=geocheck_success_html(), start_url="https://www.geocheck.org/c")
    closed = {"context": False, "browser": False}
    browser = LiveBrowser(page, closed)

    monkeypatch.setattr(
        "geocaching_cli.checker._import_playwright",
        lambda: ((lambda: LivePlaywright(browser)), FakeTimeout),
    )
    monkeypatch.setattr(
        "geocaching_cli.checker._launch_browser_retry",
        lambda *_a, **_k: browser,
    )
    result = check_geocheck("https://www.geocheck.org/c", COORD)
    assert result["ok"] is True
    assert browser.contexts[0].kwargs.get("locale") == "en-US"
    assert "Mozilla" in (browser.contexts[0].kwargs.get("user_agent") or "")
    assert closed["context"] and closed["browser"]


def test_mocked_playwright_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    page = LivePage(html=geocheck_failure_html(), start_url="https://geocheck.org/c")
    closed, _browser = _install_playwright(monkeypatch, page)
    result = check_geocheck("https://geocheck.org/c", COORD)
    assert result["ok"] is False
    assert result["definitive"] is True
    assert closed == {"context": True, "browser": True}


def test_mocked_playwright_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    page = LivePage(
        html="<html><body>still loading</body></html>",
        start_url="https://certitude.org/c",
        timeout=True,
    )
    closed, _browser = _install_playwright(monkeypatch, page)
    result = check_geocheck("https://certitude.org/c", COORD, timeout_s=1)
    assert result["ok"] is False
    assert result["definitive"] is False
    assert result["site"] == "certitude"
    assert closed == {"context": True, "browser": True}


def test_mocked_playwright_final_host_redirect_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    html = geocheck_success_html()
    page = LivePage(
        html=html,
        start_url="https://geocheck.org/c",
        final_url="https://evil.example/phish",
    )
    closed, _browser = _install_playwright(monkeypatch, page)
    result = check_geocheck("https://geocheck.org/c", COORD)
    assert result["ok"] is False
    assert result["definitive"] is False
    assert result["ok"] is False
    assert GEOCHECK_SELECTORS["submitButton"] not in page.clicked
    assert closed == {"context": True, "browser": True}


def test_check_validates_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = {"n": 0}

    def boom(*_a, **_k):
        launched["n"] += 1
        raise AssertionError("should not launch")

    monkeypatch.setattr("geocaching_cli.checker._import_playwright", boom)
    monkeypatch.setattr("geocaching_cli.checker._launch_browser_retry", boom)

    with pytest.raises(LiveError):
        check_geocheck("http://geocheck.org/c", COORD)
    with pytest.raises(CoordError):
        check_geocheck("https://geocheck.org/c", "not-a-coord")
    assert launched["n"] == 0


def test_same_family_redirect_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    page = LivePage(
        html=geocheck_success_html(),
        start_url="https://geocheck.org/c",
        final_url="https://www.geotjek.dk/c",
    )
    _install_playwright(monkeypatch, page)
    result = check_geocheck("https://geocheck.org/c", COORD)
    assert result["ok"] is True
    assert result["definitive"] is True


# --- review: route allowlist, operational errors, verdicts ----------------------------


def certitude_success_input_html(coord: str = COORD, message: str = "Well done") -> str:
    return f"""
    <html><body>
      <h3 class="embossed">{message}</h3>
      <div class="embossed success">Congratulations, that is the correct solution.</div>
      <form>
        <input id="solution" name="solution" type="text" value="{coord}">
      </form>
    </body></html>
    """


def test_request_is_allowed_family_and_blocks_ssrf() -> None:
    from geocaching_cli.checker import request_is_allowed

    assert request_is_allowed("https://geocheck.org/a", "geocheck")
    assert request_is_allowed("https://www.geotjek.dk/a", "geocheck")
    assert request_is_allowed("https://geocheck.org:443/a", "geocheck")
    assert request_is_allowed("about:blank", "geocheck")
    assert request_is_allowed("data:text/plain,hi", "geocheck")
    assert request_is_allowed("blob:https://geocheck.org/1", "geocheck")
    assert request_is_allowed("https://certitude.org/x", "certitude")
    for url in (
        "https://evil.example/x",
        "https://cdn.example/jquery.js",
        "http://geocheck.org/a",
        "https://geocheck.org:8443/a",
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://169.254.1.1/x",
        "https://10.0.0.8/x",
        "https://certitude.org/x",
    ):
        assert request_is_allowed(url, "geocheck") is False


def test_family_route_aborts_off_family_before_continue() -> None:
    from geocaching_cli.checker import install_family_route

    events: list[str] = []

    class Rec(FakeRoute):
        def continue_(self, **_kwargs: Any) -> None:
            if self.aborted:
                raise AssertionError("continue after abort")
            self.continued = True
            events.append("continue")

        def abort(self, **_kwargs: Any) -> None:
            self.aborted = True
            events.append("abort")

    handlers: list[Any] = []

    class Ctx:
        def route(self, pattern: str, handler: Any) -> None:
            handlers.append((pattern, handler))

    ctx = Ctx()
    install_family_route(ctx, "geocheck")
    assert handlers
    handler = handlers[0][1]

    evil = Rec("https://127.0.0.1/steal")
    handler(evil)
    assert evil.aborted is True
    assert evil.continued is False
    assert events[-1] == "abort"

    cdn = Rec("https://cdn.example/lib.js")
    handler(cdn)
    assert cdn.aborted is True
    assert cdn.continued is False

    ok = Rec("https://www.geotjek.dk/img.png")
    handler(ok)
    assert ok.continued is True
    assert ok.aborted is False


def test_route_installed_before_new_page_and_off_family_goto_aborted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = LivePage(html=geocheck_success_html(), start_url="https://geocheck.org/c", final_url="https://evil.example/phish")
    closed, browser = _install_playwright(monkeypatch, page)
    result = check_geocheck("https://geocheck.org/c", COORD)
    ctx = browser.contexts[0]
    assert ctx.route_installed_before_page is True
    evil_routes = [item for item in ctx.dispatched if "evil.example" in item.request.url]
    assert evil_routes
    assert evil_routes[0].aborted is True
    assert evil_routes[0].continued is False
    assert result["ok"] is False
    assert result["definitive"] is False
    assert result["message"] == "redirect_rejected"
    assert GEOCHECK_SELECTORS["submitButton"] not in page.clicked
    assert closed == {"context": True, "browser": True}


def test_submit_time_host_change_cannot_produce_success(monkeypatch: pytest.MonkeyPatch) -> None:
    form = "<html><body><p>enter coords</p></body></html>"
    page = LivePage(
        html=form,
        start_url="https://geocheck.org/c",
        after_submit_url="https://evil.example/ok",
        after_submit_html=geocheck_success_html(),
    )
    closed, browser = _install_playwright(monkeypatch, page)
    result = check_geocheck("https://geocheck.org/c", COORD)
    assert result["ok"] is False
    assert result["definitive"] is False
    assert result["message"] == "redirect_rejected"
    evil_routes = [item for item in browser.contexts[0].dispatched if "evil.example" in item.request.url]
    assert evil_routes
    assert evil_routes[0].aborted is True
    assert evil_routes[0].continued is False
    assert closed == {"context": True, "browser": True}


def test_launch_and_goto_errors_are_fixed_operational(monkeypatch: pytest.MonkeyPatch) -> None:
    page = LivePage(html=geocheck_success_html(), start_url="https://geocheck.org/c")
    closed, _browser = _install_playwright(
        monkeypatch,
        page,
        launch_error=LiveLoginError("Executable /secret/path password=hunter2"),
    )
    result = check_geocheck("https://geocheck.org/c", COORD)
    assert result["ok"] is False
    assert result["definitive"] is False
    assert result["message"] == "operational_error"
    assert "hunter2" not in result["message"]
    assert "/secret/path" not in result["message"]
    assert closed["browser"] is False

    page2 = LivePage(
        html=geocheck_success_html(),
        start_url="https://geocheck.org/c",
        goto_error=RuntimeError("net::ERR_FAILED cookie=abc"),
    )
    closed2, _b2 = _install_playwright(monkeypatch, page2)
    result2 = check_geocheck("https://geocheck.org/c", COORD)
    assert result2["ok"] is False
    assert result2["definitive"] is False
    assert result2["message"] == "operational_error"
    assert "cookie=abc" not in result2["message"]
    assert closed2 == {"context": True, "browser": True}


def test_cli_operational_failure_json_and_chinese(monkeypatch: pytest.MonkeyPatch, isolated_home) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise LiveLoginError("Executable /secret/path password=hunter2")

    monkeypatch.setattr("geocaching_cli.checker._import_playwright", lambda: ((lambda: LivePlaywright(LiveBrowser(LivePage(html="", start_url="https://geocheck.org/c"), {"context": False, "browser": False}))), FakeTimeout))
    monkeypatch.setattr("geocaching_cli.checker._launch_browser_retry", boom)

    as_json = runner.invoke(app, ["check", "--url", "https://geocheck.org/x", COORD, "--json"])
    assert as_json.exit_code == 3
    assert "Traceback" not in as_json.output
    decoder = json.JSONDecoder()
    obj, idx = decoder.raw_decode(as_json.stdout.strip())
    assert as_json.stdout.strip()[idx:].strip() == ""
    assert obj["ok"] is False
    assert obj["definitive"] is False
    assert obj["message"] == "operational_error"
    assert "hunter2" not in as_json.output
    assert "/secret/path" not in as_json.output

    text = runner.invoke(app, ["check", "--url", "https://geocheck.org/x", COORD])
    assert text.exit_code == 3
    assert "未能获得校验结论" in text.stdout
    assert "Traceback" not in text.output


def test_certitude_submit_uses_first_matching_locator() -> None:
    from geocaching_cli.checker import _submit_checker

    page = FakeFormPage(
        {CERTITUDE_SELECTORS["submitButton"], "#submitButton", 'input[type="submit"]'},
        require_first=True,
    )
    _submit_checker(page, "certitude")
    assert CERTITUDE_SELECTORS["submitButton"] in page.clicked
    assert CERTITUDE_SELECTORS["submitButton"] in page.first_used


def test_alert_captcha_session_is_not_definitive_reject() -> None:
    captcha = parse_checker_result(
        "geocheck",
        geocheck_failure_html("Invalid captcha / usercaptcha, try again"),
        coord_text=COORD,
    )
    assert captcha["ok"] is False
    assert captcha["definitive"] is False

    session = parse_checker_result(
        "geocheck",
        geocheck_failure_html("Your session expired, please login"),
        coord_text=COORD,
    )
    assert session["ok"] is False
    assert session["definitive"] is False

    verify = parse_checker_result(
        "geocheck",
        geocheck_failure_html("Enter the verification code"),
        coord_text=COORD,
    )
    assert verify["ok"] is False
    assert verify["definitive"] is False

    wrong = parse_checker_result("geocheck", geocheck_failure_html("Wrong coordinates"), coord_text=COORD)
    assert wrong["ok"] is False
    assert wrong["definitive"] is True


def test_unsolved_captcha_does_not_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    html = geocheck_success_html() + "validateChkForm(this, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')"
    page = LivePage(html=html, start_url="https://geocheck.org/c")
    closed, _browser = _install_playwright(monkeypatch, page)
    result = check_geocheck("https://geocheck.org/c", COORD)
    assert result["ok"] is False
    assert result["definitive"] is False
    assert result["message"] == "captcha_unsolved"
    assert GEOCHECK_SELECTORS["submitButton"] not in page.clicked
    assert closed == {"context": True, "browser": True}


def test_certitude_solution_value_attribute_and_fallback() -> None:
    realistic = parse_checker_result("certitude", certitude_success_input_html(), coord_text=COORD)
    assert realistic["ok"] is True
    assert realistic["definitive"] is True
    assert realistic["coord_text"] == COORD

    mismatch = parse_checker_result(
        "certitude",
        certitude_success_input_html(coord=OTHER_COORD),
        coord_text=COORD,
    )
    assert mismatch["ok"] is False
    assert mismatch["definitive"] is False
    assert "coordinate_mismatch" in mismatch["message"]

    fallback = parse_checker_result(
        "certitude",
        """
        <html><body>
          <div class="embossed success">Correct! N 39 54.252 E 116 24.444</div>
        </body></html>
        """,
        coord_text=COORD,
    )
    assert fallback["ok"] is True
    assert fallback["definitive"] is True

    geocheck = parse_checker_result("geocheck", geocheck_success_html(), coord_text=COORD)
    assert geocheck["ok"] is True and geocheck["definitive"] is True


def test_safe_message_redacts_bearer_keeps_short_cookie(isolated_home) -> None:
    from geocaching_cli.checker import _safe_message
    from geocaching_cli.config import save_session

    save_session({"cookies": {"lang": "en", "gspkauth": "long-auth-token-value"}})
    text = (
        "Wrong coordinates in en region "
        "Authorization: Bearer aabbccdd11223344 "
        "gspkauth=long-auth-token-value"
    )
    out = _safe_message(text)
    assert "en" in out
    assert "aabbccdd11223344" not in out
    assert "long-auth-token-value" not in out
    assert "Bearer" not in out
    assert "Authorization" not in out
    assert len(out) <= 500


# --- CLI ------------------------------------------------------------------------------


def test_cli_invalid_url_and_coord_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "geocaching_cli.checker.check_geocheck",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no live call")),
    )
    bad_url = runner.invoke(app, ["check", "--url", "http://geocheck.org/x", COORD])
    assert bad_url.exit_code == 2
    bad_coord = runner.invoke(app, ["check", "--url", "https://geocheck.org/x", "zzz"])
    assert bad_coord.exit_code == 2


def _cli_result(**overrides: Any) -> dict[str, Any]:
    payload = {
        "ok": False,
        "site": "geocheck",
        "message": "short safe text",
        "attempts": 1,
        "coord_text": COORD,
        "definitive": True,
    }
    payload.update(overrides)
    return payload


def test_cli_exit_codes_and_chinese(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("geocaching_cli.cli.check_geocheck", lambda *_a, **_k: _cli_result(ok=True, definitive=True))
    success = runner.invoke(app, ["check", "--url", "https://geocheck.org/x", COORD])
    assert success.exit_code == 0
    assert any(token in success.stdout for token in ("通过", "成功", "正确"))

    monkeypatch.setattr("geocaching_cli.cli.check_geocheck", lambda *_a, **_k: _cli_result(ok=False, definitive=True))
    rejected = runner.invoke(app, ["check", "--url", "https://geocheck.org/x", COORD])
    assert rejected.exit_code == 1
    assert any(token in rejected.stdout for token in ("拒绝", "错误", "失败"))

    monkeypatch.setattr("geocaching_cli.cli.check_geocheck", lambda *_a, **_k: _cli_result(ok=False, definitive=False))
    unknown = runner.invoke(app, ["check", "--url", "https://geocheck.org/x", COORD])
    assert unknown.exit_code == 3
    assert any(token in unknown.stdout for token in ("未能", "未知", "无法"))


def test_cli_json_purity_and_no_secrets(monkeypatch: pytest.MonkeyPatch, isolated_home) -> None:
    monkeypatch.setenv("GEOCACHING_PASSWORD", "super-secret-pass")
    monkeypatch.setenv("GEOCACHING_COOKIE", "gspkauth=leaked-cookie")
    payload = _cli_result(ok=True, definitive=True, message="ok")
    seen: dict[str, Any] = {}

    def fake_check(url: str, coord_text: str, *, headed: bool = False, timeout_s: float = 90.0) -> dict[str, Any]:
        seen["headed"] = headed
        seen["url"] = url
        return payload

    monkeypatch.setattr("geocaching_cli.cli.check_geocheck", fake_check)
    result = runner.invoke(
        app,
        ["check", "--url", "https://geocheck.org/x", COORD, "--json", "--headless"],
    )
    assert result.exit_code == 0
    decoder = json.JSONDecoder()
    obj, idx = decoder.raw_decode(result.stdout.strip())
    assert result.stdout.strip()[idx:].strip() == ""
    assert obj == payload
    assert "super-secret-pass" not in result.output
    assert "leaked-cookie" not in result.output
    assert seen["headed"] is False

    headed = runner.invoke(
        app,
        ["check", "--url", "https://geocheck.org/x", COORD, "--json", "--headed"],
    )
    assert headed.exit_code == 0
    assert json.loads(headed.stdout)["ok"] is True


def test_cli_json_reject_and_unknown_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "geocaching_cli.cli.check_geocheck",
        lambda *_a, **_k: _cli_result(ok=False, definitive=True),
    )
    rejected = runner.invoke(app, ["check", "--url", "https://certitude.org/x", COORD, "--json"])
    assert rejected.exit_code == 1
    assert json.loads(rejected.stdout)["definitive"] is True

    monkeypatch.setattr(
        "geocaching_cli.cli.check_geocheck",
        lambda *_a, **_k: _cli_result(ok=False, definitive=False, site="certitude"),
    )
    unknown = runner.invoke(app, ["check", "--url", "https://certitude.org/x", COORD, "--json"])
    assert unknown.exit_code == 3
    assert json.loads(unknown.stdout)["definitive"] is False


def test_cli_help_lists_check() -> None:
    help_result = runner.invoke(app, ["check", "--help"])
    assert help_result.exit_code == 0
    assert "--url" in help_result.stdout
    assert "--json" in help_result.stdout
    assert "--headed" in help_result.stdout or "--headless" in help_result.stdout
