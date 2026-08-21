#!/usr/bin/env python3
"""Thin launcher for the sibling staged muteki-blackboard protocol.

This skill must not fork write-fact / claim / submit-coord implementation.
Commands are forwarded to the staged ``muteki-blackboard/blackboard.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _is_self(path: Path) -> bool:
    try:
        return path.resolve() == Path(__file__).resolve()
    except OSError:
        return False


def _is_launcher(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return False
    return "Thin launcher for the sibling staged" in head


def _protocol_script() -> Path:
    here = Path(os.path.dirname(os.path.abspath(__file__)))
    candidates = (
        here.parent / "muteki-blackboard" / "blackboard.py",
        Path(__file__).resolve().parent.parent / "muteki-blackboard" / "blackboard.py",
        Path("/opt/muteki/muteki-blackboard/blackboard.py"),
        Path("/usr/local/bin/blackboard.py"),
    )
    for cand in candidates:
        if not cand.is_file():
            continue
        if _is_self(cand) or _is_launcher(cand):
            continue
        return cand
    print("ERROR: 找不到 sibling muteki-blackboard/blackboard.py", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    target = _protocol_script()
    os.execv(sys.executable, [sys.executable, str(target), *sys.argv[1:]])
