from __future__ import annotations

import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from geocaching_cli.cli import app

runner = CliRunner()


def test_root_and_subcommand_help() -> None:
    root = runner.invoke(app, ["--help"])
    assert root.exit_code == 0
    for name in ("login", "search", "show", "logs", "finds", "coord", "auth", "live", "local"):
        assert name in root.stdout

    for args in (
        ["login", "--help"],
        ["search", "--help"],
        ["show", "--help"],
        ["logs", "--help"],
        ["finds", "--help"],
        ["local", "--help"],
        ["local", "import", "--help"],
        ["local", "list", "--help"],
        ["local", "search", "--help"],
        ["local", "show", "--help"],
        ["coord", "--help"],
        ["coord", "parse", "--help"],
        ["coord", "project", "--help"],
        ["coord", "midpoint", "--help"],
        ["coord", "checksum", "--help"],
        ["auth", "--help"],
        ["auth", "login", "--help"],
        ["live", "--help"],
        ["live", "show", "--help"],
        ["live", "search", "--help"],
        ["live", "logs", "--help"],
        ["live", "my-finds", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, args
        assert result.stdout.strip()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "geocaching-cli" in result.stdout


def test_offline_workflow(isolated_home: Path, sample_gpx: Path, sample_wpts: Path, tmp_path: Path) -> None:
    imported = runner.invoke(app, ["local", "import", str(sample_gpx), str(sample_wpts), "--json"])
    assert imported.exit_code == 0, imported.output
    report = json.loads(imported.stdout)
    assert report["imported"][0]["caches_new"] == 4

    listed = runner.invoke(app, ["local", "list", "--json"])
    assert listed.exit_code == 0
    payload = json.loads(listed.stdout)
    assert payload["count"] == 4

    searched = runner.invoke(app, ["local", "search", "--type", "mystery", "--json"])
    assert json.loads(searched.stdout)["caches"][0]["gc_code"] == "GC2C3D4"

    shown = runner.invoke(app, ["local", "show", "gc1a2b3", "--json"])
    detail = json.loads(shown.stdout)
    assert detail["name"] == "Forbidden City Cache"
    assert detail["encoded_hints"] == "Look under the bench"
    assert len(detail["waypoints"]) == 1

    missing = runner.invoke(app, ["local", "show", "GCNOPE"])
    assert missing.exit_code == 1

    out_gpx = tmp_path / "out.gpx"
    exported = runner.invoke(app, ["local", "export", str(out_gpx), "--type", "traditional", "--active", "--json"])
    assert exported.exit_code == 0, exported.output
    assert out_gpx.is_file()
    assert "GC1A2B3" in out_gpx.read_text(encoding="utf-8")
    assert "GC4G5H6" not in out_gpx.read_text(encoding="utf-8")

    planned = runner.invoke(app, ["local", "plan", "--start", "39.90,116.40", "--limit", "3", "--json"])
    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.stdout)
    assert plan["stops"] == 3
    assert plan["total_m"] > 0

    stats = runner.invoke(app, ["local", "stats", "--json"])
    assert json.loads(stats.stdout)["caches"] == 4


def test_import_zip_and_coord_json(isolated_home: Path, sample_gpx: Path, sample_wpts: Path, tmp_path: Path) -> None:
    zpath = tmp_path / "pq.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(sample_gpx, arcname="pq.gpx")
        zf.write(sample_wpts, arcname="pq-wpts.gpx")
    result = runner.invoke(app, ["local", "import", str(zpath), "--json"])
    assert result.exit_code == 0, result.output
    imported = json.loads(result.stdout)["imported"][0]
    assert imported["caches_new"] == 4
    assert imported["waypoints"] >= 2
    shown = runner.invoke(app, ["local", "show", "GC3E4F5", "--json"])
    assert any(w["wpt_code"] == "GC3E4F5-1" for w in json.loads(shown.stdout)["waypoints"])

    checksum = runner.invoke(app, ["coord", "checksum", "GC123", "--json"])
    assert json.loads(checksum.stdout)["digits_sum"] == 6

    projected = runner.invoke(
        app,
        ["coord", "project", "--from", "0,0", "--bearing", "0", "--distance", "1000", "--json"],
    )
    assert json.loads(projected.stdout)["to"]["latitude"] > 0

    mid = runner.invoke(app, ["coord", "midpoint", "0,0", "0,2", "--json"])
    assert abs(json.loads(mid.stdout)["midpoint"]["longitude"] - 1.0) < 0.01


def test_coord_parse_numeric(isolated_home: Path) -> None:
    parsed = runner.invoke(app, ["coord", "parse", "N 40 41.352 W 074 02.670", "--json"])
    point = json.loads(parsed.stdout)
    assert abs(point["latitude"] - 40.6892) < 1e-4
    assert abs(point["longitude"] + 74.0445) < 1e-4
