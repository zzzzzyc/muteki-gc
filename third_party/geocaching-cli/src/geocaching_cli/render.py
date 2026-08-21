"""Rich tables and JSON emission."""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from rich import box
from rich.console import Console
from rich.table import Table

from geocaching_cli.coord import LatLon, format_dmm
from geocaching_cli.models import CacheRecord

console = Console()
err_console = Console(stderr=True)


def emit_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def emit_table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
) -> None:
    table = Table(title=title, box=box.SIMPLE, show_lines=False, expand=False)
    for column in columns:
        table.add_column(column, overflow="ellipsis")
    for row in rows:
        table.add_row(*["" if cell is None else str(cell) for cell in row])
    console.print(table)


def format_dt(cache: CacheRecord) -> str:
    if cache.difficulty is None and cache.terrain is None:
        return "-"
    diff = "-" if cache.difficulty is None else f"{cache.difficulty:g}"
    terr = "-" if cache.terrain is None else f"{cache.terrain:g}"
    return f"{diff}/{terr}"


def format_coords(cache: CacheRecord) -> str:
    if cache.latitude is None or cache.longitude is None:
        return "-"
    return format_dmm(LatLon(cache.latitude, cache.longitude))


def format_km(distance_m: float | None) -> str:
    if distance_m is None:
        return "-"
    return f"{distance_m / 1000.0:.2f} km"


def print_cache_table(caches: list[CacheRecord], *, title: str | None = None, show_distance: bool = False) -> None:
    columns = ["GC", "名称", "类型", "D/T", "尺寸", "状态", "坐标"]
    if show_distance:
        columns.append("距离")
    rows = []
    for cache in caches:
        row = [
            cache.gc_code,
            cache.name,
            cache.cache_type or "-",
            format_dt(cache),
            cache.container or "-",
            cache.status,
            format_coords(cache),
        ]
        if show_distance:
            row.append(format_km(cache.distance_m))
        rows.append(row)
    emit_table(columns, rows, title=title)


def print_cache_detail(cache: CacheRecord) -> None:
    console.print(f"[bold]{cache.gc_code}[/bold]  {cache.name}")
    console.print(
        f"类型 {cache.cache_type or '-'}  ·  D/T {format_dt(cache)}  ·  "
        f"尺寸 {cache.container or '-'}  ·  {cache.status}"
    )
    if cache.latitude is not None and cache.longitude is not None:
        point = LatLon(cache.latitude, cache.longitude)
        console.print(f"坐标 DD   {point.to_dict()['dd']}")
        console.print(f"坐标 DMM  {point.to_dict()['dmm']}")
        console.print(f"坐标 DMS  {point.to_dict()['dms']}")
    owner_line = cache.owner or "-"
    placed = cache.placed_at or "-"
    region = ", ".join(part for part in [cache.state, cache.country] if part) or "-"
    console.print(f"放置 {owner_line}  ·  {placed}  ·  {region}")
    if cache.url:
        console.print(f"链接 {cache.url}")
    if cache.short_description:
        console.print(f"\n[bold]简介[/bold]\n{cache.short_description}")
    if cache.long_description:
        console.print(f"\n[bold]描述[/bold]\n{cache.long_description}")
    if cache.encoded_hints:
        console.print(f"\n[bold]提示[/bold]\n{cache.encoded_hints}")
    if cache.attributes:
        names = []
        for attr in cache.attributes:
            mark = "+" if attr.included else "-"
            names.append(f"{mark}{attr.name or attr.attr_id}")
        console.print(f"\n[bold]属性[/bold]  {', '.join(names)}")
    if cache.waypoints:
        console.print("\n[bold]附加航点[/bold]")
        print_waypoint_table(cache.waypoints)
    if cache.logs:
        console.print("\n[bold]日志[/bold]")
        emit_table(
            ["日期", "类型", "记录者", "内容"],
            [
                [log.logged_at or "-", log.log_type or "-", log.finder or "-", (log.text or "")[:80]]
                for log in cache.logs
            ],
        )


def print_waypoint_table(waypoints: Sequence[Any]) -> None:
    rows = []
    for wpt in waypoints:
        coords = "-"
        if getattr(wpt, "latitude", None) is not None and getattr(wpt, "longitude", None) is not None:
            coords = format_dmm(LatLon(wpt.latitude, wpt.longitude))
        rows.append(
            [
                getattr(wpt, "wpt_code", None) or "-",
                getattr(wpt, "name", None) or "-",
                getattr(wpt, "wpt_type", None) or "-",
                coords,
                getattr(wpt, "comment", None) or "",
            ]
        )
    emit_table(["代码", "名称", "类型", "坐标", "备注"], rows)
