from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from uuid import UUID

from minio import Minio

from cti_app.config import get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect safe ModelRun diagnostics")
    parser.add_argument("run_id", type=UUID)
    parser.add_argument("--export", choices=("raw", "normalized"))
    return parser


async def _run(run_id: UUID, export: str | None) -> int:
    settings = get_settings()
    engine = create_postgres_engine(settings.postgres_dsn)
    factory = create_session_factory(engine)
    try:
        async with SqlAlchemyUnitOfWork(factory) as uow:
            run = await uow.model_runs.get(run_id)
        if run is None:
            print(f"ModelRun {run_id} introuvable", file=sys.stderr)
            return 2
        reference = run.raw_output_reference if export == "raw" else run.normalized_output_reference
        if export:
            if not reference or not reference.startswith("blob://"):
                print(f"Artefact {export} indisponible", file=sys.stderr)
                return 3
            async with SqlAlchemyUnitOfWork(factory) as uow:
                blob = await uow.blobs.get(UUID(reference.removeprefix("blob://")))
            if blob is None:
                print("Référence blob inconnue", file=sys.stderr)
                return 3
            store = MinioBlobStore(
                Minio(
                    settings.s3_endpoint,
                    access_key=settings.s3_access_key,
                    secret_key=settings.s3_secret_key,
                    secure=settings.s3_secure,
                ),
                physical_bucket=settings.s3_bucket,
            )
            sys.stdout.buffer.write(await store.read(blob.descriptor, max_bytes=10_000_000))
            return 0
        payload: dict[str, Any] = {
            "model_run_id": str(run.id),
            "provider": run.provider.value,
            "role": run.model_role.value,
            "status": run.status.value,
            "raw_output_reference": run.raw_output_reference,
            "raw_output_sha256": run.raw_output_sha256,
            "raw_output_chars": run.raw_output_chars,
            "normalized_output_reference": run.normalized_output_reference,
            "normalized_output_sha256": run.normalized_output_sha256,
            "parser_stage": run.parser_stage,
            "serializer_version": run.serializer_version,
            "normalization_version": run.normalization_version,
            "json_error_line": run.json_error_line,
            "json_error_column": run.json_error_column,
            "validation_errors": list(run.validation_errors),
            "transformations": list(run.transformations),
            "citation_count": run.citation_count,
            "extracted_url_count": run.extracted_url_count,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        print(
            "Export explicite : docker compose exec -T backend python -m "
            f"cti_app.model_run_diagnostics {run.id} --export raw > model-run-{run.id}.raw",
            file=sys.stderr,
        )
        return 0
    finally:
        await engine.dispose()


def main() -> None:
    arguments = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(arguments.run_id, arguments.export)))


if __name__ == "__main__":
    main()
