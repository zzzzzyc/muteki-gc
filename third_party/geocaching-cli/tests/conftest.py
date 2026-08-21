from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_GPX = ROOT / "examples" / "sample.gpx"
SAMPLE_WPTS = ROOT / "examples" / "sample-wpts.gpx"
GPX11 = Path(__file__).resolve().parent / "fixtures" / "gpx11.gpx"


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gc-home"
    home.mkdir()
    monkeypatch.setenv("GEOCACHING_HOME", str(home))
    monkeypatch.delenv("GEOCACHING_DB", raising=False)
    monkeypatch.delenv("GEOCACHING_USERNAME", raising=False)
    monkeypatch.delenv("GEOCACHING_PASSWORD", raising=False)
    monkeypatch.delenv("GEOCACHING_COOKIE", raising=False)
    return home


@pytest.fixture
def sample_gpx() -> Path:
    return SAMPLE_GPX


@pytest.fixture
def sample_wpts() -> Path:
    return SAMPLE_WPTS


@pytest.fixture
def gpx11() -> Path:
    return GPX11
