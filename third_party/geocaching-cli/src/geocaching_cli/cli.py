"""Typer application. User-facing help is Chinese; code comments stay English."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import typer

from geocaching_cli import __version__
from geocaching_cli.checker import check_geocheck, detect_checker_site
from geocaching_cli.config import (
    app_dir,
    clear_credentials,
    clear_session,
    db_path,
    load_credentials,
    save_credentials,
)
from geocaching_cli.coord import (
    LatLon,
    digit_checksum,
    format_coord,
    haversine_m,
    midpoint,
    parse_coord,
    project,
)
from geocaching_cli.errors import CoordError, GeoCLIError, LiveError, LiveLoginError
from geocaching_cli.gpx import export_gpx
from geocaching_cli.plan import nearest_neighbor, require_start
from geocaching_cli.render import (
    emit_json,
    emit_table,
    err_console,
    print_cache_detail,
    print_cache_table,
)
from geocaching_cli.store import Store

app = typer.Typer(
    name="gc",
    help="Geocaching.com 个人 CLI：Playwright 登录后 search / show / logs。",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)

coord_app = typer.Typer(help="坐标解析、投影、中点与数字校验和。", no_args_is_help=True)
auth_app = typer.Typer(help="geocaching.com 登录（Playwright 处理 reCAPTCHA）。", no_args_is_help=True)
live_app = typer.Typer(
    help="官网命令别名（与顶级 search / show / logs / finds 相同）。",
    no_args_is_help=True,
)
local_app = typer.Typer(
    help="本地 GPX / SQLite，次要路径。主命令走官网。",
    no_args_is_help=True,
)
app.add_typer(coord_app, name="coord")
app.add_typer(auth_app, name="auth")
app.add_typer(live_app, name="live")
app.add_typer(local_app, name="local")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"geocaching-cli {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    db: Optional[Path] = typer.Option(
        None,
        "--db",
        help="SQLite 数据库路径。默认 $GEOCACHING_DB 或 $GEOCACHING_HOME/caches.db。",
        exists=False,
        dir_okay=False,
        writable=True,
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="打印版本并退出。",
    ),
) -> None:
    if db is not None:
        os.environ["GEOCACHING_DB"] = str(db.expanduser())


def _store() -> Store:
    return Store(db_path())


def _fail(message: str, code: int = 1) -> None:
    err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code)


def _parse_point(text: str, label: str = "坐标") -> LatLon:
    try:
        return parse_coord(text)
    except CoordError as exc:
        _fail(f"{label}无效: {exc}")
        raise  # pragma: no cover


def _filter_kwargs(
    *,
    query: Optional[str],
    near: Optional[str],
    radius: Optional[float],
    cache_type: Optional[str],
    owner: Optional[str],
    available: Optional[bool],
    archived: Optional[bool],
    dmin: Optional[float],
    dmax: Optional[float],
    tmin: Optional[float],
    tmax: Optional[float],
    limit: Optional[int],
) -> dict:
    origin = _parse_point(near, "--near") if near else None
    return {
        "text": query,
        "near": origin,
        "radius_km": radius,
        "cache_type": cache_type,
        "owner": owner,
        "available": available,
        "archived": archived,
        "difficulty_min": dmin,
        "difficulty_max": dmax,
        "terrain_min": tmin,
        "terrain_max": tmax,
        "limit": limit,
    }


@local_app.command("import")
def import_cmd(
    paths: list[Path] = typer.Argument(..., help="Groundspeak GPX 或 Pocket Query zip，可多个。", exists=True),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出导入报告。"),
) -> None:
    """将 GPX / Pocket Query zip 导入本地 SQLite（无需网络）。"""
    results = []
    try:
        with _store() as store:
            for path in paths:
                results.append(store.import_path(path).to_dict())
    except GeoCLIError as exc:
        _fail(str(exc))
    if as_json:
        emit_json({"imported": results, "db": str(db_path())})
        return
    for item in results:
        err_console.print(
            f"[green]导入[/green] {item['path']}: "
            f"新增 {item['caches_new']}，更新 {item['caches_updated']}，"
            f"航点 {item['waypoints']}，日志 {item['logs']}"
        )
    err_console.print(f"数据库 {db_path()}")


@local_app.command("list")
def list_cmd(
    query: Optional[str] = typer.Option(None, "--q", "-q", help="名称 / GC 码 / 描述 / 提示关键词。"),
    near: Optional[str] = typer.Option(None, "--near", help="中心坐标，任意 DD/DMM/DMS。"),
    radius: Optional[float] = typer.Option(None, "--radius", help="与 --near 联用的半径（千米）。"),
    cache_type: Optional[str] = typer.Option(None, "--type", help="类型，如 traditional / mystery / multi。"),
    owner: Optional[str] = typer.Option(None, "--owner", help="放置者（模糊匹配）。"),
    available: Optional[bool] = typer.Option(None, "--available/--unavailable", help="按是否可用筛选。"),
    archived: Optional[bool] = typer.Option(None, "--archived/--active", help="按是否归档筛选。"),
    dmin: Optional[float] = typer.Option(None, "--dmin", help="最低难度。"),
    dmax: Optional[float] = typer.Option(None, "--dmax", help="最高难度。"),
    tmin: Optional[float] = typer.Option(None, "--tmin", help="最低地形。"),
    tmax: Optional[float] = typer.Option(None, "--tmax", help="最高地形。"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最多返回条数。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """列出本地库中的缓存，可按类型、状态、距离筛选。"""
    with _store() as store:
        caches = store.search(**_filter_kwargs(
            query=query, near=near, radius=radius, cache_type=cache_type, owner=owner,
            available=available, archived=archived, dmin=dmin, dmax=dmax,
            tmin=tmin, tmax=tmax, limit=limit,
        ))
    if as_json:
        emit_json({"count": len(caches), "caches": [c.to_dict(include_children=False) for c in caches]})
        return
    print_cache_table(caches, title=f"本地缓存 ({len(caches)})", show_distance=near is not None)


@local_app.command("search")
def local_search_cmd(
    query: Optional[str] = typer.Option(None, "--q", "-q", help="名称 / GC 码 / 描述 / 提示关键词。"),
    near: Optional[str] = typer.Option(None, "--near", help="中心坐标，任意 DD/DMM/DMS。"),
    radius: Optional[float] = typer.Option(None, "--radius", help="与 --near 联用的半径（千米）。"),
    cache_type: Optional[str] = typer.Option(None, "--type", help="类型，如 traditional / mystery / multi。"),
    owner: Optional[str] = typer.Option(None, "--owner", help="放置者（模糊匹配）。"),
    available: Optional[bool] = typer.Option(None, "--available/--unavailable", help="按是否可用筛选。"),
    archived: Optional[bool] = typer.Option(None, "--archived/--active", help="按是否归档筛选。"),
    dmin: Optional[float] = typer.Option(None, "--dmin", help="最低难度。"),
    dmax: Optional[float] = typer.Option(None, "--dmax", help="最高难度。"),
    tmin: Optional[float] = typer.Option(None, "--tmin", help="最低地形。"),
    tmax: Optional[float] = typer.Option(None, "--tmax", help="最高地形。"),
    limit: Optional[int] = typer.Option(50, "--limit", "-n", help="最多返回条数。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """在本地库中搜索（与 list 相同筛选器，默认限制 50 条）。"""
    list_cmd(
        query=query, near=near, radius=radius, cache_type=cache_type, owner=owner,
        available=available, archived=archived, dmin=dmin, dmax=dmax,
        tmin=tmin, tmax=tmax, limit=limit, as_json=as_json,
    )


@local_app.command("show")
def local_show_cmd(
    gc_code: str = typer.Argument(..., help="GC 码，如 GC1A2B3。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出完整记录。"),
) -> None:
    """显示本地库中单个缓存的详情、附加航点与日志。"""
    with _store() as store:
        cache = store.get(gc_code)
    if cache is None:
            _fail(f"本地库没有 {gc_code.upper()}。官网查询请用 gc show {gc_code.upper()}。")
    if as_json:
        emit_json(cache.to_dict())
        return
    print_cache_detail(cache)


@local_app.command("export")
def export_cmd(
    dest: Path = typer.Argument(..., help="输出文件路径（.gpx 或 .json）。"),
    query: Optional[str] = typer.Option(None, "--q", "-q", help="名称 / GC 码 / 描述 / 提示关键词。"),
    near: Optional[str] = typer.Option(None, "--near", help="中心坐标。"),
    radius: Optional[float] = typer.Option(None, "--radius", help="半径（千米）。"),
    cache_type: Optional[str] = typer.Option(None, "--type", help="类型筛选。"),
    owner: Optional[str] = typer.Option(None, "--owner", help="放置者。"),
    available: Optional[bool] = typer.Option(None, "--available/--unavailable", help="按是否可用筛选。"),
    archived: Optional[bool] = typer.Option(None, "--archived/--active", help="按是否归档筛选。"),
    dmin: Optional[float] = typer.Option(None, "--dmin", help="最低难度。"),
    dmax: Optional[float] = typer.Option(None, "--dmax", help="最高难度。"),
    tmin: Optional[float] = typer.Option(None, "--tmin", help="最低地形。"),
    tmax: Optional[float] = typer.Option(None, "--tmax", help="最高地形。"),
    limit: Optional[int] = typer.Option(None, "--limit", "-n", help="最多导出条数。"),
    fmt: Optional[str] = typer.Option(None, "--format", help="gpx 或 json。默认按文件后缀推断。"),
    as_json: bool = typer.Option(False, "--json", help="向 stdout 打印导出摘要 JSON。"),
) -> None:
    """按当前筛选条件导出 GPX 或 JSON。"""
    inferred = (fmt or dest.suffix.lstrip(".") or "gpx").lower()
    if inferred not in {"gpx", "json"}:
        _fail("导出格式必须是 gpx 或 json")
    with _store() as store:
        caches = store.search(**_filter_kwargs(
            query=query, near=near, radius=radius, cache_type=cache_type, owner=owner,
            available=available, archived=archived, dmin=dmin, dmax=dmax,
            tmin=tmin, tmax=tmax, limit=limit,
        ))
        full = [store.get(c.gc_code) or c for c in caches]
    dest = dest.expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if inferred == "json":
        dest.write_text(
            json.dumps([c.to_dict() for c in full], ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
    else:
        dest.write_text(export_gpx(full), encoding="utf-8")
    summary = {"path": str(dest), "format": inferred, "count": len(full)}
    if as_json:
        emit_json(summary)
        return
    err_console.print(f"[green]已导出[/green] {len(full)} 条 → {dest}")


@local_app.command("plan")
def plan_cmd(
    start: Optional[str] = typer.Option(None, "--start", help="起点坐标。省略则用第一条有坐标的缓存。"),
    query: Optional[str] = typer.Option(None, "--q", "-q", help="先按关键词筛选再规划。"),
    near: Optional[str] = typer.Option(None, "--near", help="只规划该点附近的缓存。"),
    radius: Optional[float] = typer.Option(None, "--radius", help="与 --near 联用的半径（千米）。"),
    cache_type: Optional[str] = typer.Option(None, "--type", help="类型筛选。"),
    limit: int = typer.Option(15, "--limit", "-n", help="路线最多停靠点数。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出路线。"),
) -> None:
    """用最近邻启发式规划一条简单拜访顺序（非最优 TSP）。"""
    origin = _parse_point(start, "--start") if start else None
    with _store() as store:
        caches = store.search(
            **_filter_kwargs(
                query=query, near=near, radius=radius, cache_type=cache_type, owner=None,
                available=None, archived=False, dmin=None, dmax=None,
                tmin=None, tmax=None, limit=None,
            )
        )
        try:
            origin = require_start(origin, caches)
        except GeoCLIError as exc:
            _fail(str(exc))
        result = nearest_neighbor(caches, origin, limit=limit)
    if as_json:
        emit_json(result.to_dict())
        return
    emit_table(
        ["#", "GC", "名称", "本段", "累计", "方位"],
        [
            [
                str(index + 1),
                leg.gc_code,
                leg.name,
                f"{leg.distance_m / 1000.0:.2f} km",
                f"{leg.cumulative_m / 1000.0:.2f} km",
                f"{leg.bearing_deg:.0f}°",
            ]
            for index, leg in enumerate(result.legs)
        ],
        title=f"最近邻路线  共 {result.total_m / 1000.0:.2f} km",
    )


@local_app.command("stats")
def stats_cmd(
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出统计。"),
) -> None:
    """本地数据库统计。"""
    with _store() as store:
        payload = store.stats()
    if as_json:
        emit_json(payload)
        return
    emit_table(
        ["项", "值"],
        [
            ["数据库", payload["db"]],
            ["缓存", payload["caches"]],
            ["日志", payload["logs"]],
            ["航点", payload["waypoints"]],
            ["已归档", payload["archived"]],
        ],
    )


@app.command("config")
def config_cmd(
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出路径信息。"),
) -> None:
    """显示数据目录、数据库与凭证文件路径（不打印密钥）。"""
    creds = load_credentials()
    payload = {
        "app_dir": str(app_dir()),
        "db": str(db_path()),
        "credentials_configured": creds.configured,
        "username_configured": bool(creds.username),
        "cookie_configured": creds.has_cookie,
        "version": __version__,
    }
    if as_json:
        emit_json(payload)
        return
    emit_table(["项", "值"], [(k, v) for k, v in payload.items()])


@coord_app.command("parse")
def coord_parse(
    text: str = typer.Argument(..., help="任意 DD / DMM / DMS 字符串。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出三种格式。"),
) -> None:
    """解析坐标并同时给出 DD、DMM、DMS。"""
    point = _parse_point(text, "坐标")
    payload = point.to_dict()
    if as_json:
        emit_json(payload)
        return
    emit_table(["格式", "值"], [("DD", payload["dd"]), ("DMM", payload["dmm"]), ("DMS", payload["dms"])])


@coord_app.command("convert")
def coord_convert(
    text: str = typer.Argument(..., help="输入坐标。"),
    to: str = typer.Option("dmm", "--to", help="目标格式：dd / dmm / dms。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """把坐标转换成指定格式。"""
    point = _parse_point(text, "坐标")
    try:
        formatted = format_coord(point, to)
    except CoordError as exc:
        _fail(str(exc))
    payload = {"format": to, "value": formatted, **point.to_dict()}
    if as_json:
        emit_json(payload)
        return
    typer.echo(formatted)


@coord_app.command("project")
def coord_project(
    start: str = typer.Option(..., "--from", help="起点坐标。"),
    bearing: float = typer.Option(..., "--bearing", help="方位角（度，正北为 0，顺时针）。"),
    distance: float = typer.Option(..., "--distance", help="距离（米）。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """从起点按方位角与距离投影到新点。"""
    origin = _parse_point(start, "--from")
    try:
        dest = project(origin, bearing, distance)
    except CoordError as exc:
        _fail(str(exc))
    payload = {"from": origin.to_dict(), "bearing": bearing, "distance_m": distance, "to": dest.to_dict()}
    if as_json:
        emit_json(payload)
        return
    emit_table(
        ["项", "值"],
        [("起点", origin.to_dict()["dmm"]), ("方位", f"{bearing}°"), ("距离", f"{distance} m"), ("终点", dest.to_dict()["dmm"])],
    )


@coord_app.command("midpoint")
def coord_midpoint(
    a: str = typer.Argument(..., help="第一点。"),
    b: str = typer.Argument(..., help="第二点。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """计算两点球面中点。"""
    pa = _parse_point(a, "A")
    pb = _parse_point(b, "B")
    mid = midpoint(pa, pb)
    payload = {"a": pa.to_dict(), "b": pb.to_dict(), "midpoint": mid.to_dict()}
    if as_json:
        emit_json(payload)
        return
    typer.echo(mid.to_dict()["dmm"])


@coord_app.command("distance")
def coord_distance(
    a: str = typer.Argument(..., help="第一点。"),
    b: str = typer.Argument(..., help="第二点。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """计算两点大圆距离。"""
    pa = _parse_point(a, "A")
    pb = _parse_point(b, "B")
    meters = haversine_m(pa, pb)
    payload = {"a": pa.to_dict(), "b": pb.to_dict(), "distance_m": round(meters, 2), "distance_km": round(meters / 1000.0, 4)}
    if as_json:
        emit_json(payload)
        return
    typer.echo(f"{meters:.1f} m  ({meters / 1000.0:.3f} km)")


@coord_app.command("checksum")
def coord_checksum(
    text: str = typer.Argument(..., help="任意字符串（坐标或谜面）。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """数字校验和：各位数字之和、数字根、字母数（谜题辅助）。"""
    payload = digit_checksum(text)
    if as_json:
        emit_json(payload)
        return
    emit_table(
        ["项", "值"],
        [
            ("输入", payload["text"]),
            ("数字", "".join(str(d) for d in payload["digits"]) or "-"),
            ("位数", payload["digit_count"]),
            ("数字和", payload["digits_sum"]),
            ("数字根", payload["digital_root"]),
            ("字母数", payload["letter_count"]),
        ],
    )


@auth_app.command("login")
def auth_login(
    username: Optional[str] = typer.Option(None, "--username", "-u", help="Geocaching.com 用户名。默认读环境变量或本地凭证文件。"),
    password: Optional[str] = typer.Option(None, "--password", "-p", help="密码。更推荐用 GEOCACHING_PASSWORD，避免出现在 shell 历史。"),
    cookie: Optional[str] = typer.Option(None, "--cookie", help="已有 gspkauth cookie，跳过浏览器。"),
    cookie_file: Optional[Path] = typer.Option(None, "--cookie-file", help="只含 cookie 值的本地文件（不要放进仓库）。"),
    browser: bool = typer.Option(True, "--browser/--no-browser", help="默认用 Playwright 打开官网登录页并处理 reCAPTCHA。"),
    headed: bool = typer.Option(True, "--headed/--headless", help="弹出浏览器窗口。验证码出现时必须看得见才能点。"),
    save: bool = typer.Option(True, "--save/--no-save", help="把用户名/密码写到 gitignore 的本地凭证文件。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出结果。"),
) -> None:
    """用 Playwright 登录 geocaching.com，拿到会话后再跑 search / show。"""
    from geocaching_cli import live as live_mod

    creds = load_credentials()
    if username:
        creds.username = username
    if password:
        creds.password = password
    if cookie:
        creds.cookie = cookie
    if cookie_file is not None:
        creds.cookie = cookie_file.read_text(encoding="utf-8").strip()
    if not creds.has_cookie and not creds.has_password:
        if not creds.username:
            creds.username = typer.prompt("用户名")
        if not creds.password:
            creds.password = typer.prompt("密码", hide_input=True)
    try:
        geocaching = live_mod.login(creds, persist=True, browser=browser, headed=headed)
    except LiveLoginError as exc:
        extra = ""
        if exc.captcha:
            extra = " 再试 `gc auth login --headed`，在弹出窗口里完成验证码。"
        _fail(" ".join(part for part in (f"登录失败: {exc}", extra.strip()) if part))
    except LiveError as exc:
        _fail(str(exc))
    if save:
        save_credentials(creds)
    payload = {"ok": True, "username": live_mod.logged_username(geocaching)}
    if as_json:
        emit_json(payload)
        return
    err_console.print(f"[green]已登录[/green] {payload['username']}")


app.command("login")(auth_login)


@auth_app.command("status")
def auth_status(
    check: bool = typer.Option(False, "--check", help="尝试恢复会话或重新登录以确认在线状态。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """显示是否已配置凭证 / 会话。默认不访问网络。"""
    from geocaching_cli import live as live_mod

    payload = live_mod.status_payload()
    payload["online"] = None
    payload["error"] = None
    if check:
        try:
            geocaching = live_mod.connect()
            payload["online"] = True
            payload["username"] = live_mod.logged_username(geocaching) or payload.get("username")
        except LiveError as exc:
            payload["online"] = False
            payload["error"] = str(exc)
    if as_json:
        emit_json(payload)
        return
    rows = [
        ("用户名已配置", payload["username_configured"]),
        ("密码已配置", payload["password_configured"]),
        ("Cookie 已配置", payload["cookie_configured"]),
        ("会话缓存", payload["session_cached"]),
        ("用户名", payload.get("username") or "-"),
    ]
    if payload["online"] is not None:
        rows.append(("在线", payload["online"]))
    if payload["error"]:
        rows.append(("错误", payload["error"]))
    emit_table(["项", "值"], rows)


@auth_app.command("logout")
def auth_logout(
    forget: bool = typer.Option(False, "--forget", help="同时删除本地凭证文件。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """删除本地会话 cookie；加 --forget 也会删除保存的用户名/密码。"""
    clear_session()
    if forget:
        clear_credentials(forget_file=True)
    payload = {"ok": True, "forgot_credentials": forget}
    if as_json:
        emit_json(payload)
        return
    err_console.print("已退出本地会话" + ("，并删除凭证文件" if forget else ""))


def _live_client():
    from geocaching_cli import live as live_mod

    try:
        return live_mod.connect()
    except LiveLoginError as exc:
        extra = ""
        if exc.captcha:
            extra = " 请先跑 `gc auth login --headed`。"
        _fail(" ".join(part for part in (f"登录失败: {exc}", extra.strip()) if part))
    except LiveError as exc:
        _fail(str(exc))


@app.command("search")
@live_app.command("search")
def live_search(
    gc_code: Optional[str] = typer.Argument(None, help="可选 GC 码：先取该点再搜索附近。"),
    near: Optional[str] = typer.Option(None, "--near", help="中心坐标（与 GC 码二选一）。"),
    limit: int = typer.Option(10, "--limit", "-n", help="最多返回条数（默认 10，避免频繁请求）。"),
    save: bool = typer.Option(False, "--save", help="把结果写入本地数据库。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """在 geocaching.com 上按坐标或某个 GC 码附近搜索。"""
    from geocaching_cli import live as live_mod

    if limit <= 0:
        _fail("--limit 必须 > 0")
    if limit > 50:
        _fail("在线搜索 --limit 上限为 50，以免过度请求。")
    geocaching = _live_client()
    origin: LatLon | None = None
    if near:
        origin = _parse_point(near, "--near")
    elif gc_code:
        try:
            record = live_mod.show_cache(geocaching, gc_code, quick=True)
        except LiveError as exc:
            _fail(str(exc))
        if record.latitude is None or record.longitude is None:
            _fail(f"{record.gc_code} 没有可用坐标，无法搜索附近")
        origin = LatLon(record.latitude, record.longitude)
    else:
        _fail("请提供 --near 坐标，或传入一个 GC 码作为搜索中心")
    try:
        caches = live_mod.search_near(geocaching, origin.latitude, origin.longitude, limit=limit)
    except LiveError as exc:
        _fail(str(exc))
    if save:
        with _store() as store:
            for cache in caches:
                store.upsert_cache(cache, replace_children=False)
            store.conn.commit()
    if as_json:
        emit_json({"center": origin.to_dict(), "count": len(caches), "caches": [c.to_dict(include_children=False) for c in caches]})
        return
    print_cache_table(caches, title=f"在线搜索 ({len(caches)})")


@app.command("show")
@live_app.command("show")
def live_show(
    gc_code: str = typer.Argument(..., help="GC 码，如 GC1PAR2。"),
    save: bool = typer.Option(False, "--save", help="把详情写入本地数据库。"),
    quick: bool = typer.Option(False, "--quick", help="只拉地图摘要，不打开完整缓存页。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """在线读取单个缓存详情。"""
    from geocaching_cli import live as live_mod

    geocaching = _live_client()
    try:
        cache = live_mod.show_cache(geocaching, gc_code, quick=quick)
    except LiveError as exc:
        _fail(str(exc))
    if save:
        with _store() as store:
            store.upsert_cache(cache, replace_children=False)
            store.conn.commit()
    if as_json:
        emit_json(cache.to_dict())
        return
    print_cache_detail(cache)


@app.command("logs")
@live_app.command("logs")
def live_logs(
    gc_code: str = typer.Argument(..., help="GC 码。"),
    limit: int = typer.Option(10, "--limit", "-n", help="最多日志条数。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """在线读取某个缓存的日志。"""
    from geocaching_cli import live as live_mod

    if limit > 50:
        _fail("在线日志 --limit 上限为 50。")
    geocaching = _live_client()
    try:
        cache, logs = live_mod.load_logs(geocaching, gc_code, limit=limit)
    except LiveError as exc:
        _fail(str(exc))
    payload = {"gc_code": cache.gc_code, "name": cache.name, "logs": [log.to_dict() for log in logs]}
    if as_json:
        emit_json(payload)
        return
    err_console.print(f"[bold]{cache.gc_code}[/bold]  {cache.name}")
    emit_table(
        ["日期", "类型", "记录者", "内容"],
        [
            [log.logged_at or "-", log.log_type or "-", log.finder or "-", (log.text or "")[:80]]
            for log in logs
        ],
    )


@app.command("finds")
@live_app.command("my-finds")
def live_my_finds(
    limit: int = typer.Option(10, "--limit", "-n", help="最多条数。"),
    save: bool = typer.Option(False, "--save", help="写入本地数据库。"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出。"),
) -> None:
    """读取当前登录账号的 found-it 记录（pycaching my_finds）。"""
    from geocaching_cli import live as live_mod

    if limit > 50:
        _fail("my-finds --limit 上限为 50。")
    geocaching = _live_client()
    try:
        caches = live_mod.my_finds(geocaching, limit=limit)
    except LiveError as exc:
        _fail(str(exc))
    if save:
        with _store() as store:
            for cache in caches:
                store.upsert_cache(cache, replace_children=False)
            store.conn.commit()
    if as_json:
        emit_json({"count": len(caches), "caches": [c.to_dict(include_children=False) for c in caches]})
        return
    print_cache_table(caches, title=f"My finds ({len(caches)})")


@app.command("check")
def check_cmd(
    coord_text: str = typer.Argument(..., help="待校验坐标，任意 DD / DMM / DMS。"),
    url: str = typer.Option(..., "--url", help="GeoCheck / Certitude 的 HTTPS 校验页。"),
    headed: bool = typer.Option(False, "--headed/--headless", help="默认无头。弹出浏览器请加 --headed。"),
    as_json: bool = typer.Option(False, "--json", help="只向 stdout 打印一个 JSON 对象。"),
) -> None:
    """用 GeoCheck 或 Certitude 校验一组坐标（Playwright，默认无头）。"""
    try:
        detect_checker_site(url)
        parse_coord(coord_text)
    except (LiveError, CoordError) as exc:
        _fail(str(exc), 2)
    result = check_geocheck(url, coord_text, headed=headed)
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    elif result["ok"] and result["definitive"]:
        typer.echo("坐标校验通过")
    elif result["definitive"]:
        typer.echo("坐标被拒绝")
    else:
        typer.echo("未能获得校验结论")
    if result["ok"] and result["definitive"]:
        raise typer.Exit(0)
    if result["definitive"]:
        raise typer.Exit(1)
    raise typer.Exit(3)


@app.command("serve")
def serve_cmd(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="仅允许本机回环地址 127.0.0.1 或 ::1。",
    ),
    port: int = typer.Option(8765, "--port", help="监听端口。"),
) -> None:
    """启动仅供本机 Muteki/watchdog 使用的 HTTP API。"""
    from geocaching_cli.server import run_server

    try:
        run_server(host=host, port=port)
    except ValueError as exc:
        _fail(str(exc))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
