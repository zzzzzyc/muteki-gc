"""GC worker toolbox: coord_calc.py subcommands and the gc-blackboard wrapper.

The calculator is a thin stdlib argparse front-end over the vendored
``muteki.vendor.geocaching_cli.coord`` core. The wrapper must exec the sibling
staged ``muteki-blackboard/blackboard.py`` and must not fork the protocol.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from muteki.models.solve_graph import Challenge
from muteki.swarm.shared_graph import SQLiteSharedGraph
from muteki.vendor.geocaching_cli import coord as gc_coord

_REPO = Path(__file__).resolve().parents[1]
_GC_SKILL = _REPO / "skills" / "gc-blackboard"
_CALC = _GC_SKILL / "coord_calc.py"
_WRAPPER = _GC_SKILL / "blackboard.py"
_ORIGIN = "N 40 41.352 W 074 02.670"
_OTHER = "N 40 42.000 W 074 03.000"


def _run_calc(*args: str, extra_env: dict[str, str] | None = None):
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [sys.executable, str(_CALC), *args],
        capture_output=True, text=True, env=env, timeout=15,
    )


def test_gc_skill_files_exist():
    skill_md = _GC_SKILL / "SKILL.md"
    assert skill_md.is_file(), "skills/gc-blackboard/SKILL.md missing"
    text = skill_md.read_text()
    assert "name: gc-blackboard" in text
    assert "read-deadends" in text
    assert "submit-coord" in text
    assert "never" in text.lower() or "不要" in text
    assert "coord_calc.py" in text
    assert "gc show" in text
    assert "verifier:geocheck@" in text
    assert "NEED_INPUT=" in text
    assert "NEED_KIND=external_blocker" in text
    assert _CALC.is_file(), "skills/gc-blackboard/coord_calc.py missing"
    assert _WRAPPER.is_file(), "skills/gc-blackboard/blackboard.py missing"


def test_coord_calc_project_matches_vendored_core():
    origin = gc_coord.parse_coord(_ORIGIN)
    expected = gc_coord.project(origin, 90.0, 100.0).to_dict()
    result = _run_calc("project", _ORIGIN, "90", "100")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert result.stdout.count("{") == 1
    for key in ("latitude", "longitude", "dd", "dmm", "dms"):
        assert key in payload
    assert payload["latitude"] == pytest.approx(expected["latitude"])
    assert payload["longitude"] == pytest.approx(expected["longitude"])
    assert payload["dd"] == expected["dd"]
    assert payload["dmm"] == expected["dmm"]
    assert payload["dms"] == expected["dms"]


def test_coord_calc_midpoint_matches_vendored_core():
    expected = gc_coord.midpoint(
        gc_coord.parse_coord(_ORIGIN), gc_coord.parse_coord(_OTHER),
    ).to_dict()
    result = _run_calc("midpoint", _ORIGIN, _OTHER)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["latitude"] == pytest.approx(expected["latitude"])
    assert payload["longitude"] == pytest.approx(expected["longitude"])
    assert payload["dd"] == expected["dd"]


def test_coord_calc_checksum_is_upstream_object():
    text = "N 51 28.123 W 000 00.456"
    expected = gc_coord.digit_checksum(text)
    result = _run_calc("checksum", text)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == expected
    assert payload["digits_sum"] == expected["digits_sum"]
    assert payload["digital_root"] == expected["digital_root"]


@pytest.mark.parametrize("fmt", ["dd", "dmm", "dms"])
def test_coord_calc_convert(fmt):
    point = gc_coord.parse_coord(_ORIGIN)
    result = _run_calc("convert", _ORIGIN, "--to", fmt)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["latitude"] == pytest.approx(point.latitude)
    assert payload["longitude"] == pytest.approx(point.longitude)
    assert payload["dd"] == point.to_dict()["dd"]
    assert payload["dmm"] == point.to_dict()["dmm"]
    assert payload["dms"] == point.to_dict()["dms"]
    assert payload[fmt] == gc_coord.format_coord(point, fmt)


def test_coord_calc_invalid_input_exits_2_with_one_chinese_stderr_line():
    result = _run_calc("project", "not-a-coord", "90", "100")
    assert result.returncode == 2
    assert result.stdout == ""
    err_lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(err_lines) == 1
    assert any("\u4e00" <= ch <= "\u9fff" for ch in err_lines[0])


def test_coord_calc_vendor_fallback_without_muteki_package(tmp_path):
    """Container layout: vendor lives under the skill; muteki is not importable."""
    assert _CALC.is_file()
    skill = tmp_path / "gc-blackboard"
    vendor = skill / "vendor" / "geocaching_cli"
    skill.mkdir()
    shutil.copytree(_REPO / "muteki" / "vendor" / "geocaching_cli", vendor)
    shutil.copy2(_CALC, skill / "coord_calc.py")

    blocker = tmp_path / "blocker"
    (blocker / "muteki").mkdir(parents=True)
    (blocker / "muteki" / "__init__.py").write_text("", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(skill / "coord_calc.py"), "checksum", "12ab34"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(blocker)},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == gc_coord.digit_checksum("12ab34")


def test_gc_wrapper_is_not_a_protocol_fork():
    text = _WRAPPER.read_text(encoding="utf-8")
    assert "sqlite3" not in text
    assert "fact_added" not in text
    assert "CREATE TABLE" not in text
    assert "os.exec" in text or "os.execl" in text or "subprocess" in text


def test_gc_wrapper_delegates_to_sibling_blackboard(tmp_path):
    ch = Challenge(id="c1", name="t", category="misc")
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "shared_graph.db", challenge=ch)
    db = graph.db_path
    graph.close()

    env = {
        **os.environ,
        "MUTEKI_BLACKBOARD_DB": str(db),
        "MUTEKI_WORKER_ID": "cli-gc",
    }
    write = subprocess.run(
        [sys.executable, str(_WRAPPER), "write-fact", "decoded Caesar shift 3", "--verified"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert write.returncode == 0, write.stderr
    read = subprocess.run(
        [sys.executable, str(_WRAPPER), "read-facts"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert read.returncode == 0, read.stderr
    assert "decoded Caesar shift 3" in read.stdout
    assert "VERIFIED" in read.stdout
