from __future__ import annotations

from types import SimpleNamespace

from geocaching_cli.browser_auth import (
    _cookie_map,
    _has_session,
    _looks_logged_in,
    headed_from_env,
    playwright_login,
)
from geocaching_cli.errors import LiveLoginError
from geocaching_cli.live import login


class FakeLocator:
    def __init__(self, present: bool = True) -> None:
        self.present = present
        self.filled = None
        self.clicked = False

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1 if self.present else 0

    def fill(self, value: str, timeout: int = 0) -> None:
        self.filled = value

    def press_sequentially(self, value: str, delay: int = 0, timeout: int = 0) -> None:
        self.filled = value

    def click(self, timeout: int = 0) -> None:
        self.clicked = True

    def dispatch_event(self, name: str) -> None:
        return None

    def wait_for(self, timeout: int = 0) -> None:
        return None


class FakePage:
    def __init__(self) -> None:
        self.url = "https://www.geocaching.com/play/map"
        self._content = '<script>"username": "tester"</script>'

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(present=True)

    def frame_locator(self, selector: str) -> FakeLocator:
        return FakeLocator(present=False)

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self.url = url

    def content(self) -> str:
        return self._content

    def wait_for_timeout(self, ms: int) -> None:
        return None

    def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        return None

    def wait_for_function(self, expression: str, timeout: int = 0) -> None:
        return None

    def screenshot(self, path: str = "", full_page: bool = False) -> None:
        return None


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()

    def new_page(self) -> FakePage:
        return self.page

    def cookies(self):
        return [
            {"name": "gspkauth", "value": "session-token", "domain": ".geocaching.com", "path": "/"},
            {"name": "other", "value": "x", "domain": "example.com", "path": "/"},
        ]

    def close(self) -> None:
        return None


class FakeBrowser:
    def new_context(self, **_kwargs) -> FakeContext:
        return FakeContext()

    def close(self) -> None:
        return None


class FakeChromium:
    def launch(self, **_kwargs) -> FakeBrowser:
        return FakeBrowser()


class FakePlaywright:
    chromium = FakeChromium()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_cookie_map_keeps_geocaching_only() -> None:
    cookies = _cookie_map(
        [
            {"name": "gspkauth", "value": "abc", "domain": ".geocaching.com"},
            {"name": "noise", "value": "1", "domain": "evil.test"},
        ]
    )
    assert cookies == {"gspkauth": "abc"}
    assert _has_session(cookies)


def test_looks_logged_in_when_off_signin() -> None:
    page = SimpleNamespace(url="https://www.geocaching.com/play/map")
    assert _looks_logged_in(page, {"gspkauth": "tok"})


def test_headed_env(monkeypatch) -> None:
    monkeypatch.delenv("GEOCACHING_HEADED", raising=False)
    monkeypatch.delenv("GEOCACHING_HEADLESS", raising=False)
    assert headed_from_env() is True
    monkeypatch.setenv("GEOCACHING_HEADLESS", "1")
    assert headed_from_env() is False
    assert headed_from_env(explicit=True) is True


def test_playwright_login_extracts_cookie(monkeypatch) -> None:
    monkeypatch.setattr(
        "geocaching_cli.browser_auth._import_playwright",
        lambda: (lambda: FakePlaywright(), TimeoutError),
    )
    monkeypatch.setattr(
        "geocaching_cli.browser_auth._launch_browser_retry",
        lambda *_args, **_kwargs: FakeBrowser(),
    )
    cookies, raw = playwright_login("user", "pass", headed=False, timeout_s=5)
    assert cookies["gspkauth"] == "session-token"
    assert raw[0]["name"] == "gspkauth"


def test_login_uses_playwright_by_default(isolated_home, monkeypatch) -> None:
    monkeypatch.setenv("GEOCACHING_USERNAME", "user")
    monkeypatch.setenv("GEOCACHING_PASSWORD", "pass")

    sentinel = object()

    def fake_playwright(creds, *, headed=None, persist=True):
        assert creds.username == "user"
        assert persist is True
        return sentinel

    monkeypatch.setattr("geocaching_cli.live.login_via_playwright", fake_playwright)
    assert login() is sentinel


def test_playwright_login_requires_credentials() -> None:
    try:
        playwright_login("", "")
    except LiveLoginError as exc:
        assert "用户名" in str(exc)
    else:
        raise AssertionError("expected LiveLoginError")
