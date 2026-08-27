#!/usr/bin/env python3
"""Reject committed-looking binary payloads from M2 test fixture directories."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_PATH = Path("backend/tests/fixtures")
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml"}


def find_binary_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    failures: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            failures.append(path)
            continue
        try:
            payload = path.read_bytes()
            if b"\x00" in payload:
                failures.append(path)
                continue
            payload.decode("utf-8")
        except (OSError, UnicodeDecodeError):
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
