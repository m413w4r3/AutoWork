#!/usr/bin/env python3
"""Read the local diagnostics trail written by the production pipeline.

`var/diagnostics/events.jsonl` is one JSON object per line, which is the right
shape for a machine and the wrong shape for someone trying to work out why a
merge did not apply. This prints it as a timeline, resolves the stored payload
files, and lets you narrow to one event family, correlation id, or edition.

    scripts/diagnostics.py                     # the last 30 events
    scripts/diagnostics.py -n 100 merge.       # every merge event, prefix match
    scripts/diagnostics.py --correlation <id>  # one HTTP request end to end
    scripts/diagnostics.py --failures -v       # failures, with their tracebacks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "var" / "diagnostics"

# Fields the header line already shows, or that are noise in a timeline.
HEADER_FIELDS = {"at", "event", "stage", "correlation_id", "pid"}


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        sys.exit(f"Aucun journal à {path}. La production n'a encore rien écrit.")
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            # A partially flushed last line is normal while a run is in flight.
            print(f"[ligne {number} illisible, ignorée]", file=sys.stderr)
    return entries


def _matches(entry: dict[str, Any], args: argparse.Namespace) -> bool:
    event = str(entry.get("event", ""))
    if args.event and not any(event.startswith(prefix) for prefix in args.event):
        return False
    if args.failures and "fail" not in event and "error" not in entry:
        return False
    if args.correlation and entry.get("correlation_id") != args.correlation:
        return False
    if args.edition and entry.get("edition_id") != args.edition:
        return False
    return True


def _render(entry: dict[str, Any], root: Path, verbose: bool) -> None:
    at = str(entry.get("at", "?"))[:19].replace("T", " ")
    stage = entry.get("stage") or "-"
    print(f"{at}  {entry.get('event', '?'):<32} [{stage}]")
    correlation = entry.get("correlation_id")
    if correlation:
        print(f"    correlation : {correlation}")
    for key, value in sorted(entry.items()):
        if key in HEADER_FIELDS or key == "payload_file":
            continue
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        text = str(rendered)
        if not verbose and len(text) > 200:
            text = text[:200] + "…"
        print(f"    {key:<12}: {text}")
    payload_file = entry.get("payload_file")
    if payload_file:
        path = root / str(payload_file)
        print(f"    payload     : {path}")
        if verbose and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                print(f"      | {line}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("event", nargs="*", help="Ne garder que ces préfixes d'événement")
    parser.add_argument("-n", "--limit", type=int, default=30, help="Nombre d'événements (défaut : 30)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Champs entiers et payloads inclus")
    parser.add_argument("--failures", action="store_true", help="Seulement les échecs")
    parser.add_argument("--correlation", help="Une seule requête HTTP, par identifiant de corrélation")
    parser.add_argument("--edition", help="Une seule édition, par identifiant")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help=f"Défaut : {DEFAULT_ROOT}")
    args = parser.parse_args()

    entries = [entry for entry in _load(args.root / "events.jsonl") if _matches(entry, args)]
    if not entries:
        print("Aucun événement ne correspond.")
        return
    for entry in entries[-args.limit :]:
        _render(entry, args.root, args.verbose)
    print(f"{len(entries)} événement(s) correspondant(s), {min(len(entries), args.limit)} affiché(s).")


if __name__ == "__main__":
    main()
