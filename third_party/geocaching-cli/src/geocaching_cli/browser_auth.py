"""Playwright login for geocaching.com.

pycaching's requests POST hits a reCAPTCHA wall. A real browser executes the
widget, can click the checkbox, and yields a usable gspkauth session cookie.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from typing import Any

from geocaching_cli.config import app_dir
from geocaching_cli.errors import LiveLoginError

logger = logging.getLogger(__name__)

SIGNIN_URL = "https://www.geocaching.com/account/signin"
COOKIE_BANNER_SELECTORS = (
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#onetrust-accept-btn-handler",
    "button#onetrust-accept-btn-handler",
    "button:has-text('Allow all')",
    "button:has-text('Accept Cookies')",
    "button:has-text('I Accept')",
    "button:has-text('Accept All')",
)
USERNAME_SELECTORS = (
    "#UsernameOrEmail",
    "input[name='UsernameOrEmail']",
    "input[type='email']",
)
PASSWORD_SELECTORS = (
    "#Password",
    "input[name='Password']",
    "input[type='password']",
)
SUBMIT_SELECTORS = (
    "#SignIn",
    "input#SignIn",
    "input[type='submit']",
)


def browser_profile_dir():
    return app_dir() / "browser-profile"


def headed_from_env(*, explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    headed = os.environ.get("GEOCACHING_HEADED", "").strip().lower()
    if headed in {"1", "true", "yes", "on"}:
        return True
    headless = os.environ.get("GEOCACHING_HEADLESS", "").strip().lower()
    if headless in {"1", "true", "yes", "on"}:
        return False
    return True


def _import_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LiveLoginError(
            "未安装 Playwright。请执行: pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright, PlaywrightTimeout


def _first_locator(page, selectors: tuple[str, ...]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                return locator
        except Exception:
            continue
    return None


def _click_if_visible(page, selectors: tuple[str, ...], timeout_ms: int = 2500) -> bool:
    locator = _first_locator(page, selectors)
    if locator is None:
        return False
    try:
        locator.click(timeout=timeout_ms)
        return True
    except Exception:
        return False


def _dismiss_cookie_banner(page) -> None:
    _click_if_visible(page, COOKIE_BANNER_SELECTORS, timeout_ms=3000)


def _fill_first(page, selectors: tuple[str, ...], value: str) -> None:
    locator = _first_locator(page, selectors)
    if locator is None:
        raise LiveLoginError("登录页找不到用户名或密码输入框，页面结构可能已改。")
    locator.click(timeout=8000)
    locator.fill("")
    try:
        locator.press_sequentially(value, delay=25, timeout=15000)
    except Exception:
        locator.fill(value, timeout=8000)
    locator.dispatch_event("input")
    locator.dispatch_event("change")


def _field_value_len(page, selector: str) -> int:
    try:
        return len(page.locator(selector).input_value() or "")
    except Exception:
        return 0


def _wait_for_signin_enabled(page, timeout_ms: int = 15000) -> None:
    page.wait_for_function(
        """() => {
            const el = document.querySelector('#SignIn');
            return !!el && !el.disabled;
        }""",
        timeout=timeout_ms,
    )


def _debug_screenshot(page, name: str) -> None:
    try:
        path = app_dir() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True)
        logger.info("Wrote login screenshot %s", path)
    except Exception:
        logger.debug("Could not write login screenshot", exc_info=True)


def _apply_manual_captcha_clicks(page) -> bool:
    """If GEOCACHING_HOME/captcha-clicks.json exists, click those 0-based tiles."""
    path = app_dir() / "captcha-clicks.json"
    if not path.is_file():
        return False
    try:
        indices = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(indices, list):
        return False
    try:
        frame = page.frame_locator("iframe[src*='bframe']").first
        tiles = frame.locator(".rc-imageselect-tile")
        for index in indices:
            tiles.nth(int(index)).click(timeout=3000)
            page.wait_for_timeout(250)
        verify = frame.locator("#recaptcha-verify-button")
        if verify.count():
            verify.first.click(timeout=3000)
        path.unlink()
        return True
    except Exception:
        logger.debug("Manual captcha clicks failed", exc_info=True)
        return False


def _click_recaptcha_checkbox(page) -> bool:
    """Click the visible reCAPTCHA v2 checkbox if the challenge iframe is present."""
    frame_selectors = (
        "iframe[title='reCAPTCHA']",
        "iframe[src*='recaptcha']",
        "iframe[title*='recaptcha' i]",
    )
    for selector in frame_selectors:
        try:
            frame = page.frame_locator(selector).first
            box = frame.locator("#recaptcha-anchor, .recaptcha-checkbox-border, .recaptcha-checkbox")
            if box.count() == 0:
                continue
            box.first.click(timeout=4000)
            try:
                frame.locator(".recaptcha-checkbox-checked, [aria-checked='true']").first.wait_for(
                    timeout=15000
                )
            except Exception:
                pass
            return True
        except Exception:
            continue
    return False


def _cookie_map(raw: list[dict[str, Any]]) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in raw:
        domain = cookie.get("domain") or ""
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value and "geocaching.com" in domain:
            cookies[name] = value
    return cookies


def _has_session(cookies: dict[str, str]) -> bool:
    return bool(cookies.get("gspkauth"))


def _looks_logged_in(page, cookies: dict[str, str]) -> bool:
    if _has_session(cookies) and "signin" not in (page.url or "").lower():
        return True
    if _has_session(cookies):
        try:
            body = page.content()
        except Exception:
            body = ""
        if '"username"' in body and "UsernameOrEmail" not in body:
            return True
    return False


def _ensure_chromium(sync_playwright) -> None:
    try:
        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
            if path and os.path.exists(path):
                return
    except Exception:
        pass
    logger.info("Installing Playwright Chromium browser")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LiveLoginError(f"无法安装 Playwright Chromium: {detail or result.returncode}")


def _launch_browser(playwright, *, headed: bool):
    launch_kwargs = {
        "headless": not headed,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    try:
        return playwright.chromium.launch(channel="chrome", **launch_kwargs)
    except Exception:
        logger.debug("System Chrome not available, falling back to bundled Chromium")
    return playwright.chromium.launch(**launch_kwargs)


def _launch_browser_retry(sync_playwright, playwright, *, headed: bool):
    try:
        return _launch_browser(playwright, headed=headed)
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" not in message:
            raise LiveLoginError(f"无法启动浏览器: {exc}") from exc
        _ensure_chromium(sync_playwright)
        try:
            return playwright.chromium.launch(
                headless=not headed,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as retry_exc:
            raise LiveLoginError(f"无法启动浏览器: {retry_exc}") from retry_exc


def playwright_login(
    username: str,
    password: str,
    *,
    headed: bool | None = None,
    timeout_s: float = 180,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Open geocaching.com in Chromium, sign in, return (cookie_map, raw cookies).

    Secrets are never logged. The browser window stays up while a CAPTCHA
    checkbox or image challenge is on screen so it can be completed.
    """
    if not username or not password:
        raise LiveLoginError("Playwright 登录需要用户名和密码。")

    sync_playwright, PlaywrightTimeout = _import_playwright()
    use_headed = headed_from_env(explicit=headed)
    deadline = time.monotonic() + timeout_s

    with sync_playwright() as playwright:
        browser = _launch_browser_retry(sync_playwright, playwright, headed=use_headed)
        context = None
        try:
            context = browser.new_context(
                locale="en-US",
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/192.168.1.3 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#UsernameOrEmail", timeout=20000)
            page.wait_for_timeout(1500)
            _dismiss_cookie_banner(page)

            raw = context.cookies()
            cookies = _cookie_map(raw)
            if _looks_logged_in(page, cookies):
                return cookies, raw

            _fill_first(page, USERNAME_SELECTORS, username)
            _fill_first(page, PASSWORD_SELECTORS, password)
            user_len = _field_value_len(page, "#UsernameOrEmail")
            pass_len = _field_value_len(page, "#Password")
            if user_len == 0 or pass_len == 0:
                _debug_screenshot(page, "login-empty-fields.png")
                raise LiveLoginError(
                    f"登录框没有填上（用户名 {user_len} 字，密码 {pass_len} 字），未提交。"
                )
            try:
                _wait_for_signin_enabled(page)
            except Exception as exc:
                _debug_screenshot(page, "login-disabled.png")
                raise LiveLoginError("登录按钮仍是 disabled，表单校验没过。") from exc

            _click_recaptcha_checkbox(page)
            if not _click_if_visible(page, SUBMIT_SELECTORS, timeout_ms=8000):
                _debug_screenshot(page, "login-no-submit.png")
                raise LiveLoginError("找不到登录提交按钮。")

            while time.monotonic() < deadline:
                raw = context.cookies()
                cookies = _cookie_map(raw)
                if _looks_logged_in(page, cookies):
                    return cookies, raw
                if page.locator("div.g-recaptcha, iframe[src*='recaptcha']").count() > 0:
                    _click_recaptcha_checkbox(page)
                    _debug_screenshot(page, "captcha.png")
                    _apply_manual_captcha_clicks(page)
                page.wait_for_timeout(500)

            raw = context.cookies()
            cookies = _cookie_map(raw)
            if _has_session(cookies):
                return cookies, raw
            _debug_screenshot(page, "login-timeout.png")
            raise LiveLoginError(
                "Playwright 登录超时：页面仍在要求 CAPTCHA 或登录未完成。"
                "再跑一次 `gc auth login --headed`，在弹出的窗口里点完验证码。"
            )
        except LiveLoginError:
            raise
        except PlaywrightTimeout as exc:
            raise LiveLoginError(f"Playwright 等待超时: {exc}") from exc
        except Exception as exc:
            raise LiveLoginError(f"Playwright 登录失败: {exc}") from exc
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            try:
                browser.close()
            except Exception:
                pass
