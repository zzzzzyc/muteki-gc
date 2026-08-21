"""Groundspeak GPX and Pocket Query zip import/export.

Parser is namespace-prefix agnostic: it matches local names and treats any
``*groundspeak.com/cache*`` URI as the Groundspeak extension, covering GPX 1.0
(direct child) and GPX 1.1 (under ``extensions``).
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from geocaching_cli.errors import ImportError_
from geocaching_cli.models import AttributeRecord, CacheRecord, LogRecord, WaypointRecord

GC_CODE_RE = re.compile(r"^GC[0-9A-Z]+$", re.IGNORECASE)
GC_PREFIX_RE = re.compile(r"^(GC[0-9A-Z]+)-", re.IGNORECASE)
WPTS_NAME_RE = re.compile(r"-wpts\.gpx$", re.IGNORECASE)


def localname(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def namespace_uri(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def is_groundspeak_cache(el: ET.Element) -> bool:
    if localname(el.tag) != "cache":
        return False
    uri = namespace_uri(el.tag).lower()
    return "groundspeak.com/cache" in uri or uri == ""


def children(el: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(el) if localname(child.tag) == name]


def child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    found = children(el, name)
    return found[0] if found else None


def text_of(el: ET.Element | None, default: str | None = None) -> str | None:
    if el is None or el.text is None:
        return default
    value = el.text.strip()
    return value if value else default


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in {"true", "1", "yes"}


def _as_float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def find_groundspeak_cache(wpt: ET.Element) -> ET.Element | None:
    for el in wpt.iter():
        if el is wpt:
            continue
        if is_groundspeak_cache(el):
            return el
    return None


def parent_gc_code(code: str | None) -> str | None:
    if not code:
        return None
    match = GC_PREFIX_RE.match(code.strip())
    if match:
        return match.group(1).upper()
    return None


def looks_like_gc(code: str | None) -> bool:
    return bool(code and GC_CODE_RE.match(code.strip()))


def _parse_logs(cache_el: ET.Element) -> list[LogRecord]:
    logs_el = child(cache_el, "logs")
    if logs_el is None:
        return []
    records: list[LogRecord] = []
    for log_el in children(logs_el, "log"):
        records.append(
            LogRecord(
                log_id=log_el.attrib.get("id"),
                logged_at=text_of(child(log_el, "date")),
                log_type=text_of(child(log_el, "type")),
                finder=text_of(child(log_el, "finder")),
                text=text_of(child(log_el, "text")),
            )
        )
    return records


def _parse_attributes(cache_el: ET.Element) -> list[AttributeRecord]:
    attrs_el = child(cache_el, "attributes")
    if attrs_el is None:
        return []
    records: list[AttributeRecord] = []
    for attr_el in children(attrs_el, "attribute"):
        records.append(
            AttributeRecord(
                attr_id=_as_int(attr_el.attrib.get("id")),
                name=text_of(attr_el),
                included=_as_bool(attr_el.attrib.get("inc")),
            )
        )
    return records


def _type_from_wpt(wpt: ET.Element, cache_el: ET.Element | None) -> str | None:
    if cache_el is not None:
        named = text_of(child(cache_el, "type"))
        if named:
            return named
    raw = text_of(child(wpt, "type"))
    if not raw:
        return None
    if "|" in raw:
        return raw.split("|", 1)[1].strip() or raw
    return raw


def parse_waypoint_element(wpt: ET.Element) -> tuple[CacheRecord | None, WaypointRecord | None]:
    lat = _as_float(wpt.attrib.get("lat"))
    lon = _as_float(wpt.attrib.get("lon"))
    code = text_of(child(wpt, "name"))
    desc = text_of(child(wpt, "desc"))
    comment = text_of(child(wpt, "cmt"))
    url = text_of(child(wpt, "url"))
    urlname = text_of(child(wpt, "urlname"))
    wpt_type = text_of(child(wpt, "type"))
    cache_el = find_groundspeak_cache(wpt)

    if cache_el is not None:
        gc_code = (code or "").upper()
        if not looks_like_gc(gc_code):
            gc_code = gc_code or "UNKNOWN"
        name = text_of(child(cache_el, "name")) or urlname or desc or gc_code
        cache = CacheRecord(
            gc_code=gc_code,
            name=name,
            latitude=lat,
            longitude=lon,
            cache_type=_type_from_wpt(wpt, cache_el),
            container=text_of(child(cache_el, "container")),
            difficulty=_as_float(text_of(child(cache_el, "difficulty"))),
            terrain=_as_float(text_of(child(cache_el, "terrain"))),
            owner=text_of(child(cache_el, "owner")) or text_of(child(cache_el, "placed_by")),
            placed_at=text_of(child(wpt, "time")),
            country=text_of(child(cache_el, "country")),
            state=text_of(child(cache_el, "state")),
            available=_as_bool(cache_el.attrib.get("available")),
            archived=_as_bool(cache_el.attrib.get("archived")),
            short_description=text_of(child(cache_el, "short_description")),
            long_description=text_of(child(cache_el, "long_description")),
            encoded_hints=text_of(child(cache_el, "encoded_hints")),
            url=url,
            logs=_parse_logs(cache_el),
            attributes=_parse_attributes(cache_el),
        )
        return cache, None

    parent = parent_gc_code(code)
    type_name = wpt_type or ""
    is_extra = parent is not None or type_name.lower().startswith("waypoint")
    is_plain_cache = looks_like_gc(code) and "geocache" in type_name.lower()

    if is_plain_cache and not is_extra:
        gc_code = (code or "").upper()
        cache = CacheRecord(
            gc_code=gc_code,
            name=urlname or desc or gc_code,
            latitude=lat,
            longitude=lon,
            cache_type=_type_from_wpt(wpt, None),
            placed_at=text_of(child(wpt, "time")),
            url=url,
        )
        return cache, None

    if is_extra or parent:
        waypoint = WaypointRecord(
            wpt_code=code.upper() if code else None,
            name=urlname or desc or code,
            latitude=lat,
            longitude=lon,
            wpt_type=_type_from_wpt(wpt, None) or type_name,
            comment=comment or desc,
            gc_code=parent,
        )
        return None, waypoint

    if looks_like_gc(code):
        gc_code = (code or "").upper()
        cache = CacheRecord(
            gc_code=gc_code,
            name=urlname or desc or gc_code,
            latitude=lat,
            longitude=lon,
            cache_type=_type_from_wpt(wpt, None),
            placed_at=text_of(child(wpt, "time")),
            url=url,
        )
        return cache, None

    return None, None


def parse_gpx_bytes(data: bytes, *, source: str = "gpx") -> tuple[list[CacheRecord], list[WaypointRecord]]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImportError_(f"GPX XML 解析失败: {exc}") from exc

    caches: list[CacheRecord] = []
    waypoints: list[WaypointRecord] = []
    imported_at = datetime.now(timezone.utc).isoformat()

    for wpt in root.iter():
        if localname(wpt.tag) != "wpt":
            continue
        cache, extra = parse_waypoint_element(wpt)
        if cache is not None:
            cache.source = source
            cache.imported_at = imported_at
            caches.append(cache)
        elif extra is not None:
            waypoints.append(extra)
    return caches, waypoints


def parse_gpx_file(path: Path, *, source: str | None = None) -> tuple[list[CacheRecord], list[WaypointRecord]]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImportError_(f"无法读取文件: {path}: {exc}") from exc
    return parse_gpx_bytes(data, source=source or str(path))


def is_waypoint_filename(name: str) -> bool:
    return bool(WPTS_NAME_RE.search(name))


def iter_gpx_from_path(path: Path) -> Iterable[tuple[str, bytes, bool]]:
    """Yield ``(label, bytes, is_wpts_file)`` from a GPX file or PQ zip."""
    if not path.exists():
        raise ImportError_(f"路径不存在: {path}")
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as zf:
                names = list(zf.namelist())
                gpx_names = [n for n in names if n.lower().endswith(".gpx") and not n.endswith("/")]
                # Main cache GPX first, then *-wpts.gpx, so extra waypoints are not wiped.
                gpx_names.sort(key=lambda n: (is_waypoint_filename(n), n.lower()))
                if not gpx_names:
                    raise ImportError_(f"zip 中没有 .gpx 文件: {path}")
                for name in gpx_names:
                    # Read in-memory; never extract to disk (zip-slip safe).
                    with zf.open(name) as handle:
                        yield f"{path}::{name}", handle.read(), is_waypoint_filename(name)
        except zipfile.BadZipFile as exc:
            raise ImportError_(f"损坏的 zip: {path}") from exc
        return
    if path.suffix.lower() == ".gpx" or path.is_file():
        yield str(path), path.read_bytes(), is_waypoint_filename(path.name)
        return
    raise ImportError_(f"不支持的导入路径: {path}")


def _xml_escape(value: str | None) -> str:
    if not value:
        return ""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _fmt_bool(value: bool | None) -> str:
    if value is None:
        return "True"
    return "True" if value else "False"


def export_gpx(caches: list[CacheRecord], *, name: str = "geocaching-cli export") -> str:
    """Serialize caches to a Groundspeak-flavored GPX 1.0 document."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<gpx xmlns:xsd="http://www.w3.org/2001/XMLSchema"',
        '     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '     version="1.0"',
        '     creator="geocaching-cli"',
        '     xmlns="http://www.topografix.com/GPX/1/0"',
        '     xmlns:groundspeak="http://www.groundspeak.com/cache/1/0/1">',
        f"  <name>{_xml_escape(name)}</name>",
        f"  <desc>Exported by geocaching-cli</desc>",
        f"  <time>{now}</time>",
    ]
    for cache in caches:
        lat = f"{cache.latitude:.6f}" if cache.latitude is not None else "0"
        lon = f"{cache.longitude:.6f}" if cache.longitude is not None else "0"
        gtype = cache.cache_type or "Traditional Cache"
        lines.append(f'  <wpt lat="{lat}" lon="{lon}">')
        if cache.placed_at:
            lines.append(f"    <time>{_xml_escape(cache.placed_at)}</time>")
        lines.append(f"    <name>{_xml_escape(cache.gc_code)}</name>")
        lines.append(f"    <desc>{_xml_escape(cache.name)}</desc>")
        if cache.url:
            lines.append(f"    <url>{_xml_escape(cache.url)}</url>")
        lines.append(f"    <urlname>{_xml_escape(cache.name)}</urlname>")
        lines.append("    <sym>Geocache</sym>")
        lines.append(f"    <type>Geocache|{_xml_escape(gtype)}</type>")
        lines.append(
            f'    <groundspeak:cache available="{_fmt_bool(cache.available)}" '
            f'archived="{_fmt_bool(cache.archived)}">'
        )
        lines.append(f"      <groundspeak:name>{_xml_escape(cache.name)}</groundspeak:name>")
        if cache.owner:
            lines.append(f"      <groundspeak:placed_by>{_xml_escape(cache.owner)}</groundspeak:placed_by>")
            lines.append(f"      <groundspeak:owner>{_xml_escape(cache.owner)}</groundspeak:owner>")
        lines.append(f"      <groundspeak:type>{_xml_escape(gtype)}</groundspeak:type>")
        if cache.container:
            lines.append(f"      <groundspeak:container>{_xml_escape(cache.container)}</groundspeak:container>")
        if cache.attributes:
            lines.append("      <groundspeak:attributes>")
            for attr in cache.attributes:
                inc = "1" if attr.included else "0"
                attr_id = "" if attr.attr_id is None else str(attr.attr_id)
                lines.append(
                    f'        <groundspeak:attribute id="{attr_id}" inc="{inc}">'
                    f"{_xml_escape(attr.name)}</groundspeak:attribute>"
                )
            lines.append("      </groundspeak:attributes>")
        if cache.difficulty is not None:
            lines.append(f"      <groundspeak:difficulty>{cache.difficulty}</groundspeak:difficulty>")
        if cache.terrain is not None:
            lines.append(f"      <groundspeak:terrain>{cache.terrain}</groundspeak:terrain>")
        if cache.country:
            lines.append(f"      <groundspeak:country>{_xml_escape(cache.country)}</groundspeak:country>")
        if cache.state:
            lines.append(f"      <groundspeak:state>{_xml_escape(cache.state)}</groundspeak:state>")
        if cache.short_description:
            lines.append(
                "      <groundspeak:short_description html=\"False\">"
                f"{_xml_escape(cache.short_description)}</groundspeak:short_description>"
            )
        if cache.long_description:
            lines.append(
                "      <groundspeak:long_description html=\"True\">"
                f"{_xml_escape(cache.long_description)}</groundspeak:long_description>"
            )
        if cache.encoded_hints:
            lines.append(
                f"      <groundspeak:encoded_hints>{_xml_escape(cache.encoded_hints)}"
                "</groundspeak:encoded_hints>"
            )
        if cache.logs:
            lines.append("      <groundspeak:logs>")
            for log in cache.logs:
                log_id = log.log_id or ""
                lines.append(f'        <groundspeak:log id="{_xml_escape(log_id)}">')
                if log.logged_at:
                    lines.append(f"          <groundspeak:date>{_xml_escape(log.logged_at)}</groundspeak:date>")
                if log.log_type:
                    lines.append(f"          <groundspeak:type>{_xml_escape(log.log_type)}</groundspeak:type>")
                if log.finder:
                    lines.append(f"          <groundspeak:finder>{_xml_escape(log.finder)}</groundspeak:finder>")
                if log.text:
                    lines.append(
                        f"          <groundspeak:text>{_xml_escape(log.text)}</groundspeak:text>"
                    )
                lines.append("        </groundspeak:log>")
            lines.append("      </groundspeak:logs>")
        lines.append("    </groundspeak:cache>")
        lines.append("  </wpt>")
        for wpt in cache.waypoints:
            if wpt.latitude is None or wpt.longitude is None:
                continue
            wlat = f"{wpt.latitude:.6f}"
            wlon = f"{wpt.longitude:.6f}"
            wtype = wpt.wpt_type or "Reference Point"
            lines.append(f'  <wpt lat="{wlat}" lon="{wlon}">')
            lines.append(f"    <name>{_xml_escape(wpt.wpt_code or '')}</name>")
            if wpt.comment:
                lines.append(f"    <cmt>{_xml_escape(wpt.comment)}</cmt>")
            lines.append(f"    <desc>{_xml_escape(wpt.name or '')}</desc>")
            lines.append(f"    <type>Waypoint|{_xml_escape(wtype)}</type>")
            lines.append("  </wpt>")
    lines.append("</gpx>")
    return "\n".join(lines) + "\n"
