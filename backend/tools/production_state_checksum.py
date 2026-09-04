#!/usr/bin/env python3
"""Recompute `content_sha256` after hand-editing an exported production state.

Usage:
    python backend/tools/production_state_checksum.py etat.json           # verify
    python backend/tools/production_state_checksum.py etat.json --write   # fix

The checksum covers the canonical JSON form of the whole snapshot minus the
`content_sha256` field itself, exactly as `_validate_snapshot` recomputes it on
import. Editing the synthesis prose by hand and re-importing without this step
fails with `production_state_checksum_mismatch`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def canonical_checksum(payload: dict[str, Any]) -> str:
    """Return the application checksum for an exported snapshot payload."""
    source_root = Path(__file__).resolve().parents[1] / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from cti_app.application.production_state import compute_production_state_checksum

    return compute_production_state_checksum(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the file in place with the recomputed checksum",
    )
    args = parser.parse_args()

    payload = json.loads(args.path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        print("error: the production state must be a JSON object", file=sys.stderr)
        return 2

    expected = canonical_checksum(payload)
    current = payload.get("content_sha256")
    if current == expected:
        print(f"ok: content_sha256 is already {expected}")
        return 0

    print(f"stored:   {current}")
    print(f"computed: {expected}")
    if not args.write:
        print("run again with --write to fix the file", file=sys.stderr)
        return 1

    payload["content_sha256"] = expected
    args.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"written: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
