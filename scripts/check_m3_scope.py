#!/usr/bin/env python3
"""Fail closed when a Codex lot changed files outside its declared scope.

Usage:
  python scripts/check_m3_scope.py --base HEAD~1 --allow path/a --allow path/b
  python scripts/check_m3_scope.py --base <pre-lot-sha> --allow-file /tmp/p09-allowed.txt

The allow-file format is one repository-relative path per line; blank lines and
lines beginning with # are ignored. Directory wildcards are intentionally not
supported: M3 prompts are expected to enumerate exact files.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import PurePosixPath


def _normalize(value: str) -> str:
    path = PurePosixPath(value.strip())
    if not value.strip() or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid repository-relative path: {value!r}")
    return path.as_posix()


def _allowed(args: argparse.Namespace) -> set[str]:
    values = list(args.allow)
    if args.allow_file:
        for raw in open(args.allow_file, encoding="utf-8"):
            line = raw.strip()
            if line and not line.startswith("#"):
                values.append(line)
    if not values:
        raise ValueError("at least one --allow or --allow-file entry is required")
    return {_normalize(value) for value in values}


def _changed(base: str) -> set[str]:
    command = ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", f"{base}...HEAD"]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    return {_normalize(line) for line in result.stdout.splitlines() if line.strip()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="commit/ref immediately before the lot")
    parser.add_argument("--allow", action="append", default=[], help="exact allowed path")
    parser.add_argument("--allow-file", help="text file containing exact allowed paths")
    args = parser.parse_args(argv)
    try:
        allowed = _allowed(args)
        changed = _changed(args.base)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"scope-check-error: {exc}", file=sys.stderr)
        return 2

    unexpected = sorted(changed - allowed)
    if unexpected:
        print("M3 scope violation; unexpected changed files:", file=sys.stderr)
        for path in unexpected:
            print(f"  {path}", file=sys.stderr)
        return 1

    print(f"M3 scope OK: {len(changed)} changed file(s), all explicitly allowed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
