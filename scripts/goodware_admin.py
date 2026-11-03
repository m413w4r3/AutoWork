#!/usr/bin/env python3
"""Load and inspect the canonical AutoWork goodware baseline."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from minio import Minio

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.goodware import GoodwareMeasurementService, GoodwareService
from cti_app.application.persistence import UnitOfWork
from cti_app.config import get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    load = commands.add_parser("load", help="import a verified v2 index into PostgreSQL and MinIO")
    load.add_argument("--artifact-dir", required=True, type=Path)
    load.add_argument("--source-dir", required=True, type=Path)
    load.add_argument("--investigation-id", type=UUID)
    commands.add_parser("status", help="show goodware baselines without changing state")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    service = GoodwareService(BlobCatalogService(blob_store, uow_factory), uow_factory)
    try:
        if args.command == "status":
            status = await service.status()
            if not status.baselines:
                print("goodware baseline: absent")
            else:
                print(
                    json.dumps(
                        {
                            "status": status.state.value,
                            "baselines": [
                                {
                                    "baseline_id": str(item.baseline_id),
                                    "fingerprint_sha256": item.baseline_fingerprint_sha256,
                                    "normalization_version": item.normalization_version,
                                    "pattern_version": item.pattern_version,
                                    "schema_version": item.schema_version,
                                    "key_version": item.key_version,
                                    "index_format_version": item.index_format_version,
                                    "source_count": item.source_count,
                                    "record_count": item.record_count,
                                    "occurrence_sum": item.occurrence_sum,
                                }
                                for item in status.baselines
                            ],
                        },
                        sort_keys=True,
                    )
                )
            return 0

        baseline = await service.import_index(args.artifact_dir, args.source_dir)
        if args.investigation_id is not None:
            await service.bind(args.investigation_id, baseline.id)
        measurement = GoodwareMeasurementService(
            blob_store,
            uow_factory,
            cache_root=settings.goodware_cache_root,
        )
        await measurement.prepare(baseline.id)
        print(
            json.dumps(
                {
                    "status": "loaded",
                    "baseline_id": str(baseline.id),
                    "fingerprint_sha256": baseline.baseline_fingerprint_sha256,
                    "record_count": baseline.record_count,
                    "occurrence_sum": baseline.occurrence_sum,
                    "investigation_id": (
                        str(args.investigation_id) if args.investigation_id is not None else None
                    ),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        print(f"error: goodware operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
