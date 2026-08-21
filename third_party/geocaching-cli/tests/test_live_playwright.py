from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from geocaching_cli.cli import app

runner = CliRunner()

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return os.environ.get("GEOCACHING_LIVE_TEST", "").strip() in {"1", "true", "yes"}


@pytest.mark.skipif(not _live_enabled(), reason="Set GEOCACHING_LIVE_TEST=1 to hit geocaching.com")
def test_playwright_login_and_show(isolated_home) -> None:
    if not os.environ.get("GEOCACHING_USERNAME") or not os.environ.get("GEOCACHING_PASSWORD"):
        pytest.skip("GEOCACHING_USERNAME / GEOCACHING_PASSWORD required")

    login = runner.invoke(app, ["auth", "login", "--no-save", "--json"])
    assert login.exit_code == 0, login.output
    assert "username" in login.stdout.lower() or login.stdout.strip()

    shown = runner.invoke(app, ["show", "GC1PAR2", "--json"])
    assert shown.exit_code == 0, shown.output
    assert "GC1PAR2" in shown.stdout
