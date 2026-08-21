from __future__ import annotations

import zipfile
from pathlib import Path

from geocaching_cli.coord import parse_coord
from geocaching_cli.store import Store


def test_import_search_show(isolated_home: Path, sample_gpx: Path, sample_wpts: Path) -> None:
    db = isolated_home / "caches.db"
    with Store(db) as store:
        report = store.import_path(sample_gpx)
        assert report.caches_new == 4
        store.import_path(sample_wpts)

        all_caches = store.search()
        assert len(all_caches) == 4

        mystery = store.search(cache_type="mystery")
        assert [c.gc_code for c in mystery] == ["GC2C3D4"]

        active = store.search(archived=False)
        assert {c.gc_code for c in active} == {"GC1A2B3", "GC2C3D4", "GC3E4F5"}

        hint_hits = store.search(text="bench")
        assert [c.gc_code for c in hint_hits] == ["GC1A2B3"]

        nearby = store.search(near=parse_coord("39.9042,116.4074"), radius_km=3)
        assert nearby[0].gc_code == "GC1A2B3"
        assert nearby[0].distance_m is not None
        assert nearby[0].distance_m < 50

        detail = store.get("gc1a2b3")
        assert detail is not None
        assert len(detail.logs) == 2
        assert any(w.wpt_code == "GC1A2B3-P" for w in detail.waypoints)

        stats = store.stats()
        assert stats["caches"] == 4
        assert stats["waypoints"] == 2
        assert stats["logs"] == 3


def test_import_pocket_query_zip(isolated_home: Path, sample_gpx: Path, sample_wpts: Path, tmp_path: Path) -> None:
    zpath = tmp_path / "pq.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(sample_gpx, arcname="sample.gpx")
        zf.write(sample_wpts, arcname="sample-wpts.gpx")
    with Store(isolated_home / "caches.db") as store:
        report = store.import_path(zpath)
        assert report.caches_new == 4
        assert report.waypoints >= 2
        assert store.get("GC3E4F5") is not None
        extras = store.waypoints_for("GC3E4F5")
        assert any(w.wpt_code == "GC3E4F5-1" for w in extras)


def test_wpts_before_main_gpx_are_kept(isolated_home: Path, sample_gpx: Path, sample_wpts: Path) -> None:
    with Store(isolated_home / "caches.db") as store:
        store.import_path(sample_wpts)
        store.import_path(sample_gpx)
        extras = store.waypoints_for("GC1A2B3")
        assert any(w.wpt_code == "GC1A2B3-P" for w in extras)


def test_upsert_updates(isolated_home: Path, sample_gpx: Path) -> None:
    with Store(isolated_home / "caches.db") as store:
        store.import_path(sample_gpx)
        again = store.import_path(sample_gpx)
        assert again.caches_new == 0
        assert again.caches_updated == 4
