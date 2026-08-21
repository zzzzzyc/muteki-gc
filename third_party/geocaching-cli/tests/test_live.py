from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from geocaching_cli.cli import app
from geocaching_cli.errors import LiveLoginError
from geocaching_cli.live import cache_to_record, logs_to_records
from geocaching_cli.models import CacheRecord

runner = CliRunner()


class FakePoint:
    def __init__(self, latitude: float, longitude: float) -> None:
        self.latitude = latitude
        self.longitude = longitude


class FakeCache:
    def __init__(self) -> None:
        self.wp = "GC1PAR2"
        self.name = "Geocaching HQ"
        self.location = FakePoint(47.644, -122.119)
        self.type = SimpleNamespace(name="traditional")
        self.size = SimpleNamespace(name="regular")
        self.difficulty = 1.0
        self.terrain = 1.5
        self.author = "Groundspeak"
        self.hidden = "2000-09-03"
        self.description = "HQ cache"
        self.hint = "none"
        self.favorites = 10
        self.url = "https://coord.info/GC1PAR2"
        self.status = SimpleNamespace(name="enabled")


def test_cache_to_record_and_logs() -> None:
    record = cache_to_record(FakeCache())
    assert record.gc_code == "GC1PAR2"
    assert record.latitude == 47.644
    assert record.cache_type == "traditional"

    logs = logs_to_records(
        [
            SimpleNamespace(
                uuid="1",
                visited="2024-01-01",
                type=SimpleNamespace(name="found_it"),
                author="Alice",
                text="TFTC",
            )
        ]
    )
    assert logs[0].finder == "Alice"
    assert logs[0].log_type == "found_it"


def test_live_show_mocked(isolated_home, monkeypatch) -> None:
    fake = FakeCache()

    monkeypatch.setattr("geocaching_cli.live.connect", lambda **kwargs: object())
    monkeypatch.setattr("geocaching_cli.live.show_cache", lambda *_args, **_kwargs: cache_to_record(fake))

    result = runner.invoke(app, ["live", "show", "GC1PAR2", "--json"])
    assert result.exit_code == 0, result.output
    assert "GC1PAR2" in result.stdout
    assert "Geocaching HQ" in result.stdout


def test_live_search_mocked(isolated_home, monkeypatch) -> None:
    record = CacheRecord(
        gc_code="GCNEAR",
        name="Nearby",
        latitude=47.64,
        longitude=-122.12,
        cache_type="Traditional Cache",
    )
    monkeypatch.setattr("geocaching_cli.live.connect", lambda **kwargs: object())
    monkeypatch.setattr("geocaching_cli.live.search_near", lambda *_args, **_kwargs: [record])
    result = runner.invoke(app, ["live", "search", "--near", "47.64,-122.12", "--json"])
    assert result.exit_code == 0, result.output
    assert "GCNEAR" in result.stdout


def test_auth_login_error_is_clean(isolated_home, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise LiveLoginError("CAPTCHA is required to login to the site.", captcha=True)

    monkeypatch.setattr("geocaching_cli.live.login", boom)
    result = runner.invoke(app, ["auth", "login", "-u", "user", "-p", "pass", "--no-browser"])
    assert result.exit_code == 1
    assert "CAPTCHA" in result.output
    assert "headed" in result.output
