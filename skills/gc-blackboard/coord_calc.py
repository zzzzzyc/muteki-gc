#!/usr/bin/env python3
"""Deterministic coordinate calculator for geocache workers.

Thin argparse front-end over the vendored geocaching-cli coordinate core.
Prefers ``muteki.vendor.geocaching_cli``; container images may ship the same
package under ``<skill>/vendor/geocaching_cli``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load_coord():
    try:
        from muteki.vendor.geocaching_cli.coord import (
            CoordError,
            digit_checksum,
            format_coord,
            midpoint,
            parse_coord,
            project,
        )
        return parse_coord, project, midpoint, digit_checksum, format_coord, CoordError
    except ImportError:
        pass

    here = Path(__file__).resolve().parent
    for root in (here / "vendor", Path("/opt/muteki/gc-blackboard/vendor")):
        if (root / "geocaching_cli" / "coord.py").is_file():
            sys.path.insert(0, str(root))
            try:
                from geocaching_cli.coord import (  # type: ignore
                    CoordError,
                    digit_checksum,
                    format_coord,
                    midpoint,
                    parse_coord,
                    project,
                )
                return parse_coord, project, midpoint, digit_checksum, format_coord, CoordError
            except ImportError:
                break
    print(
        "无法加载坐标核心：缺少 muteki.vendor.geocaching_cli 与本地 vendor/geocaching_cli",
        file=sys.stderr,
    )
    raise SystemExit(2)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"参数错误: {message}", file=sys.stderr)
        raise SystemExit(2)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parse_coord, project, midpoint, digit_checksum, format_coord, CoordError = _load_coord()
    parser = _Parser(prog="coord_calc.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_project = sub.add_parser("project")
    p_project.add_argument("coord")
    p_project.add_argument("bearing_deg", type=float)
    p_project.add_argument("distance_m", type=float)

    p_mid = sub.add_parser("midpoint")
    p_mid.add_argument("coord_a")
    p_mid.add_argument("coord_b")

    p_sum = sub.add_parser("checksum")
    p_sum.add_argument("text")

    p_conv = sub.add_parser("convert")
    p_conv.add_argument("coord")
    p_conv.add_argument("--to", dest="fmt", required=True, choices=("dd", "dmm", "dms"))

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return 2 if code not in (0, None) else 0

    try:
        if args.cmd == "project":
            origin = parse_coord(args.coord)
            _print_json(project(origin, args.bearing_deg, args.distance_m).to_dict())
            return 0
        if args.cmd == "midpoint":
            _print_json(midpoint(parse_coord(args.coord_a), parse_coord(args.coord_b)).to_dict())
            return 0
        if args.cmd == "checksum":
            _print_json(digit_checksum(args.text))
            return 0
        if args.cmd == "convert":
            point = parse_coord(args.coord)
            payload = point.to_dict()
            payload[args.fmt] = format_coord(point, args.fmt)
            _print_json(payload)
            return 0
    except CoordError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — CLI boundary
        print(f"计算失败: {exc}", file=sys.stderr)
        return 2
    print("参数错误: 未知子命令", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
