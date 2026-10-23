#!/usr/bin/env python3
"""Export a redacted, read-only audit bundle for one investigation."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

from cti_app.application.investigation_audit_export import write_audit_bundle
from cti_app.config import get_settings
from cti_app.infrastructure.database.investigation_audit_reader import (
    read_investigation_audit_snapshot,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a redacted audit bundle from canonical investigation records"
    )
    parser.add_argument("investigation_id", type=UUID)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="rebuild remaining files from an existing atomic RUN_EVENTS.jsonl.gz checkpoint",
    )
    return parser


async def _run(investigation_id: UUID, output_dir: Path, *, resume: bool) -> int:
    checkpoint = output_dir / "RUN_EVENTS.jsonl.gz"
    snapshot = None
    if not (resume and checkpoint.is_file()):
        settings = get_settings()
        engine = create_postgres_engine(settings.postgres_dsn)
        try:
            snapshot = await read_investigation_audit_snapshot(
                create_session_factory(engine), investigation_id
            )
        finally:
            await engine.dispose()
    bundle = write_audit_bundle(
        snapshot,
        output_dir,
        str(investigation_id),
        resume=resume,
    )
    print(
        f"Wrote audit bundle for {investigation_id}: "
        f"{bundle.event_count} events; resumed={bundle.resumed_from_events}"
    )
    return 0


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return asyncio.run(
            _run(arguments.investigation_id, arguments.output_dir, resume=arguments.resume)
        )
    except LookupError:
        print("Investigation not found", file=sys.stderr)
        return 2
    except Exception:
        # Avoid echoing a DSN, provider response, CTI content, or blob reference.
        print("Audit export failed; sensitive source details were not printed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
