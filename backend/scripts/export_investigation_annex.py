#!/usr/bin/env python3
"""Export deterministic investigation annexes from canonical PostgreSQL and MinIO state."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from minio import Minio

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_annex_export import write_investigation_annex
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.investigation_annex_reader import (
    load_investigation_annex_snapshot,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("investigation_id", type=UUID)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--include-restricted-archive",
        action="store_true",
        help=(
            "copy policy-eligible samples, captures, figures, and notes into "
            "private restricted paths"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing annex directory atomically",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    try:
        session_factory = create_session_factory(engine)
        blob_store = MinioBlobStore(
            Minio(
                settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                secure=settings.s3_secure,
            ),
            physical_bucket=settings.s3_bucket,
        )
        artifact_store = ProductionArtifactStore(
            BlobCatalogService(blob_store, lambda: SqlAlchemyUnitOfWork(session_factory))
        )
        snapshot = await load_investigation_annex_snapshot(
            session_factory,
            args.investigation_id,
            artifact_store,
            include_restricted_archive=args.include_restricted_archive,
        )
        result = write_investigation_annex(
            snapshot,
            args.output_dir,
            include_restricted_archive=args.include_restricted_archive,
            overwrite=args.overwrite,
        )
        print(
            json.dumps(
                {
                    "investigation_id": str(args.investigation_id),
                    "manifest_sha256": result.manifest_sha256,
                    "object_count": result.object_count,
                    "file_count": result.file_count,
                    "output_dir": str(result.output_dir),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(arguments))
    except LookupError:
        print("Investigation not found", file=sys.stderr)
        return 2
    except FileExistsError:
        print("Annex destination exists or could not be replaced", file=sys.stderr)
        return 2
    except Exception:
        print("Annex export failed; sensitive source details were not printed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
