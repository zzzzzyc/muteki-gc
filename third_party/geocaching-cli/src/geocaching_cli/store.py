"""SQLite-backed local cache store."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from geocaching_cli.coord import LatLon, haversine_m
from geocaching_cli.errors import StoreError
from geocaching_cli.gpx import iter_gpx_from_path, parse_gpx_bytes
from geocaching_cli.models import (
    AttributeRecord,
    CacheRecord,
    ImportResult,
    LogRecord,
    WaypointRecord,
)
from geocaching_cli.types_util import canonical_cache_type

SCHEMA = """
CREATE TABLE IF NOT EXISTS caches (
    gc_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    latitude REAL,
    longitude REAL,
    cache_type TEXT,
    container TEXT,
    difficulty REAL,
    terrain REAL,
    owner TEXT,
    placed_at TEXT,
    country TEXT,
    state TEXT,
    available INTEGER,
    archived INTEGER,
    short_description TEXT,
    long_description TEXT,
    encoded_hints TEXT,
    url TEXT,
    favorited INTEGER,
    source TEXT,
    imported_at TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gc_code TEXT NOT NULL,
    log_id TEXT,
    logged_at TEXT,
    log_type TEXT,
    finder TEXT,
    text TEXT,
    FOREIGN KEY (gc_code) REFERENCES caches(gc_code) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gc_code TEXT,
    wpt_code TEXT,
    name TEXT,
    latitude REAL,
    longitude REAL,
    wpt_type TEXT,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS attributes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gc_code TEXT NOT NULL,
    attr_id INTEGER,
    name TEXT,
    included INTEGER,
    FOREIGN KEY (gc_code) REFERENCES caches(gc_code) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_caches_type ON caches(cache_type);
CREATE INDEX IF NOT EXISTS idx_caches_owner ON caches(owner);
CREATE INDEX IF NOT EXISTS idx_waypoints_gc ON waypoints(gc_code);
CREATE INDEX IF NOT EXISTS idx_logs_gc ON logs(gc_code);
"""


def _bool_to_sql(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _sql_to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def upsert_cache(self, cache: CacheRecord, *, replace_children: bool = True) -> bool:
        """Insert or update a cache. Returns True if the row was new."""
        existing = self.conn.execute(
            "SELECT gc_code FROM caches WHERE gc_code = ?", (cache.gc_code,)
        ).fetchone()
        self.conn.execute(
            """
            INSERT INTO caches (
                gc_code, name, latitude, longitude, cache_type, container,
                difficulty, terrain, owner, placed_at, country, state,
                available, archived, short_description, long_description,
                encoded_hints, url, favorited, source, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(gc_code) DO UPDATE SET
                name=excluded.name,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                cache_type=excluded.cache_type,
                container=excluded.container,
                difficulty=excluded.difficulty,
                terrain=excluded.terrain,
                owner=excluded.owner,
                placed_at=excluded.placed_at,
                country=excluded.country,
                state=excluded.state,
                available=excluded.available,
                archived=excluded.archived,
                short_description=excluded.short_description,
                long_description=excluded.long_description,
                encoded_hints=excluded.encoded_hints,
                url=excluded.url,
                favorited=COALESCE(excluded.favorited, caches.favorited),
                source=excluded.source,
                imported_at=excluded.imported_at
            """,
            (
                cache.gc_code,
                cache.name,
                cache.latitude,
                cache.longitude,
                cache.cache_type,
                cache.container,
                cache.difficulty,
                cache.terrain,
                cache.owner,
                cache.placed_at,
                cache.country,
                cache.state,
                _bool_to_sql(cache.available),
                _bool_to_sql(cache.archived),
                cache.short_description,
                cache.long_description,
                cache.encoded_hints,
                cache.url,
                cache.favorited,
                cache.source,
                cache.imported_at,
            ),
        )
        if replace_children:
            self.conn.execute("DELETE FROM logs WHERE gc_code = ?", (cache.gc_code,))
            self.conn.execute("DELETE FROM attributes WHERE gc_code = ?", (cache.gc_code,))
            for log in cache.logs:
                self._insert_log(cache.gc_code, log)
            for attr in cache.attributes:
                self._insert_attribute(cache.gc_code, attr)
            # Extra parking/stage waypoints usually live in a separate *-wpts.gpx.
            # Only replace them when this cache record actually carries some.
            if cache.waypoints:
                self.conn.execute("DELETE FROM waypoints WHERE gc_code = ?", (cache.gc_code,))
                for wpt in cache.waypoints:
                    self._insert_waypoint(wpt, default_gc=cache.gc_code)
        return existing is None

    def _insert_log(self, gc_code: str, log: LogRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO logs (gc_code, log_id, logged_at, log_type, finder, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (gc_code, log.log_id, log.logged_at, log.log_type, log.finder, log.text),
        )

    def _insert_attribute(self, gc_code: str, attr: AttributeRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO attributes (gc_code, attr_id, name, included)
            VALUES (?, ?, ?, ?)
            """,
            (gc_code, attr.attr_id, attr.name, _bool_to_sql(attr.included)),
        )

    def _insert_waypoint(self, wpt: WaypointRecord, default_gc: str | None = None) -> None:
        gc_code = wpt.gc_code or default_gc
        self.conn.execute(
            """
            INSERT INTO waypoints (gc_code, wpt_code, name, latitude, longitude, wpt_type, comment)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                gc_code,
                wpt.wpt_code,
                wpt.name,
                wpt.latitude,
                wpt.longitude,
                wpt.wpt_type,
                wpt.comment,
            ),
        )

    def add_waypoints(self, waypoints: Iterable[WaypointRecord]) -> int:
        count = 0
        for wpt in waypoints:
            if wpt.gc_code:
                self.conn.execute(
                    "DELETE FROM waypoints WHERE gc_code = ? AND wpt_code = ?",
                    (wpt.gc_code, wpt.wpt_code),
                )
            self._insert_waypoint(wpt)
            count += 1
        return count

    def import_path(self, path: Path) -> ImportResult:
        result = ImportResult(path=str(path))
        try:
            chunks = list(iter_gpx_from_path(Path(path)))
        except Exception as exc:
            raise StoreError(str(exc)) from exc
        for label, data, is_wpts in chunks:
            caches, extras = parse_gpx_bytes(data, source=label)
            if is_wpts:
                result.waypoints += self.add_waypoints(extras)
                result.skipped += len(caches)
                continue
            for cache in caches:
                is_new = self.upsert_cache(cache)
                if is_new:
                    result.caches_new += 1
                else:
                    result.caches_updated += 1
                result.logs += len(cache.logs)
                result.attributes += len(cache.attributes)
                result.waypoints += len(cache.waypoints)
            result.waypoints += self.add_waypoints(extras)
        self.conn.commit()
        return result

    def _row_to_cache(self, row: sqlite3.Row, *, children: bool = False) -> CacheRecord:
        cache = CacheRecord(
            gc_code=row["gc_code"],
            name=row["name"],
            latitude=row["latitude"],
            longitude=row["longitude"],
            cache_type=row["cache_type"],
            container=row["container"],
            difficulty=row["difficulty"],
            terrain=row["terrain"],
            owner=row["owner"],
            placed_at=row["placed_at"],
            country=row["country"],
            state=row["state"],
            available=_sql_to_bool(row["available"]),
            archived=_sql_to_bool(row["archived"]),
            short_description=row["short_description"],
            long_description=row["long_description"],
            encoded_hints=row["encoded_hints"],
            url=row["url"],
            favorited=row["favorited"],
            source=row["source"],
            imported_at=row["imported_at"],
        )
        if children:
            cache.logs = self.logs_for(cache.gc_code)
            cache.waypoints = self.waypoints_for(cache.gc_code)
            cache.attributes = self.attributes_for(cache.gc_code)
        return cache

    def get(self, gc_code: str) -> CacheRecord | None:
        code = gc_code.strip().upper()
        row = self.conn.execute("SELECT * FROM caches WHERE gc_code = ?", (code,)).fetchone()
        if row is None:
            return None
        return self._row_to_cache(row, children=True)

    def logs_for(self, gc_code: str) -> list[LogRecord]:
        rows = self.conn.execute(
            "SELECT log_id, logged_at, log_type, finder, text FROM logs WHERE gc_code = ? ORDER BY logged_at DESC",
            (gc_code,),
        ).fetchall()
        return [
            LogRecord(
                log_id=row["log_id"],
                logged_at=row["logged_at"],
                log_type=row["log_type"],
                finder=row["finder"],
                text=row["text"],
            )
            for row in rows
        ]

    def waypoints_for(self, gc_code: str) -> list[WaypointRecord]:
        rows = self.conn.execute(
            "SELECT gc_code, wpt_code, name, latitude, longitude, wpt_type, comment "
            "FROM waypoints WHERE gc_code = ?",
            (gc_code,),
        ).fetchall()
        return [
            WaypointRecord(
                wpt_code=row["wpt_code"],
                name=row["name"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                wpt_type=row["wpt_type"],
                comment=row["comment"],
                gc_code=row["gc_code"],
            )
            for row in rows
        ]

    def attributes_for(self, gc_code: str) -> list[AttributeRecord]:
        rows = self.conn.execute(
            "SELECT attr_id, name, included FROM attributes WHERE gc_code = ?",
            (gc_code,),
        ).fetchall()
        return [
            AttributeRecord(
                attr_id=row["attr_id"],
                name=row["name"],
                included=_sql_to_bool(row["included"]),
            )
            for row in rows
        ]

    def search(
        self,
        *,
        text: str | None = None,
        near: LatLon | None = None,
        radius_km: float | None = None,
        cache_type: str | None = None,
        owner: str | None = None,
        available: bool | None = None,
        archived: bool | None = None,
        difficulty_min: float | None = None,
        difficulty_max: float | None = None,
        terrain_min: float | None = None,
        terrain_max: float | None = None,
        limit: int | None = None,
    ) -> list[CacheRecord]:
        sql = "SELECT * FROM caches WHERE 1=1"
        params: list[Any] = []
        if text:
            needle = f"%{text}%"
            sql += (
                " AND (gc_code LIKE ? OR name LIKE ? OR IFNULL(owner,'') LIKE ?"
                " OR IFNULL(long_description,'') LIKE ? OR IFNULL(short_description,'') LIKE ?"
                " OR IFNULL(encoded_hints,'') LIKE ?)"
            )
            params.extend([needle, needle, needle, needle, needle, needle])
        if owner:
            sql += " AND IFNULL(owner,'') LIKE ?"
            params.append(f"%{owner}%")
        if available is not None:
            sql += " AND available = ?"
            params.append(_bool_to_sql(available))
        if archived is not None:
            sql += " AND archived = ?"
            params.append(_bool_to_sql(archived))
        if difficulty_min is not None:
            sql += " AND difficulty >= ?"
            params.append(difficulty_min)
        if difficulty_max is not None:
            sql += " AND difficulty <= ?"
            params.append(difficulty_max)
        if terrain_min is not None:
            sql += " AND terrain >= ?"
            params.append(terrain_min)
        if terrain_max is not None:
            sql += " AND terrain <= ?"
            params.append(terrain_max)

        rows = self.conn.execute(sql, params).fetchall()
        records = [self._row_to_cache(row, children=False) for row in rows]

        if cache_type:
            want = canonical_cache_type(cache_type)
            records = [item for item in records if canonical_cache_type(item.cache_type or "") == want]

        if near is not None:
            filtered: list[CacheRecord] = []
            radius_m = None if radius_km is None else radius_km * 1000.0
            for item in records:
                if item.latitude is None or item.longitude is None:
                    continue
                item.distance_m = haversine_m(near, LatLon(item.latitude, item.longitude))
                if radius_m is not None and item.distance_m > radius_m:
                    continue
                filtered.append(item)
            records = sorted(filtered, key=lambda c: c.distance_m or 0.0)
        else:
            records = sorted(records, key=lambda c: c.gc_code)

        if limit is not None and limit >= 0:
            records = records[:limit]
        return records

    def stats(self) -> dict[str, Any]:
        caches = self.conn.execute("SELECT COUNT(*) FROM caches").fetchone()[0]
        logs = self.conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        waypoints = self.conn.execute("SELECT COUNT(*) FROM waypoints").fetchone()[0]
        archived = self.conn.execute(
            "SELECT COUNT(*) FROM caches WHERE archived = 1"
        ).fetchone()[0]
        types = [
            {"cache_type": row["cache_type"] or "unknown", "count": row["n"]}
            for row in self.conn.execute(
                "SELECT cache_type, COUNT(*) AS n FROM caches GROUP BY cache_type ORDER BY n DESC"
            )
        ]
        return {
            "db": str(self.path),
            "caches": caches,
            "logs": logs,
            "waypoints": waypoints,
            "archived": archived,
            "types": types,
        }
