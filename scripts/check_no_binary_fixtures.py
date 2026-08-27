#!/usr/bin/env python3
"""Reject committed-looking binary payloads from M2 test fixture directories."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_PATH = Path("backend/tests/fixtures")
EXECUTABLE_SUFFIXES = {".exe", ".dll", ".sys", ".scr", ".com", ".elf", ".bin", ".so"}


def find_binary_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    failures: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix.lower() == ".pyc":
            continue
        if path.suffix.lower() in EXECUTABLE_SUFFIXES:
            failures.append(path)
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)
    failures = find_binary_files(args.root)
    if failures:
        for path in failures:
            print(f"binary fixture forbidden: {path}", file=sys.stderr)
        return 1
    print(f"{args.root}: no binary fixture files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
