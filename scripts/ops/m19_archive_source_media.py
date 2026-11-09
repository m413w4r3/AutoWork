#!/usr/bin/env python3
"""Archive image candidates from already archived production sources."""

from __future__ import annotations

import argparse
import asyncio
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import AccessBlockedError, UnsafeAddressError
from cti_app.application.persistence import (
    BlobRepository,
    SourceDocumentRepository,
    UnitOfWorkFactory,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.source_figure_inventory import (
    MAX_SOURCE_DOCUMENT_BYTES,
    ArchivedFigureSource,
    load_archived_source_figure_inventory,
)
from cti_app.application.source_media_extraction import (
    extract_source_media_observations,
)
from cti_app.config import Settings
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.source_media import SourceMediaRecord
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.collection_runtime import create_subject_collection_service
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine

DEFAULT_MAX_IMAGES_PER_SUBJECT = 200
DEFAULT_DELAY_SECONDS = 5.0


class NoVerifiedExtractionError(ValueError):
    """Raised when a subject has no verified extraction artifact to read."""


@dataclass(frozen=True, slots=True)
class OperatorRuntime:
    uow_factory: UnitOfWorkFactory
    artifact_store: ProductionArtifactStore
    collection_service: SubjectCollectionService


@dataclass(frozen=True, slots=True)
class SubjectPreview:
    html_sources: int
    pdf_sources: int
    image_observations: int
    records: tuple[SourceMediaRecord, ...]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subject_ids", nargs="+", metavar="SUBJECT_ID")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview archived sources and existing records without collecting (default)",
    )
    parser.add_argument(
        "--execute", action="store_true", help="fetch and archive image candidates"
    )
    parser.add_argument(
        "--i-confirm-user-authorized-image-fetch",
        action="store_true",
        help="confirm that image fetching was explicitly authorized",
    )
    parser.add_argument(
        "--max-images-per-subject",
        type=_positive_int,
        default=DEFAULT_MAX_IMAGES_PER_SUBJECT,
    )
    parser.add_argument(
        "--delay-seconds",
        type=_nonnegative_float,
        default=DEFAULT_DELAY_SECONDS,
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.subject_ids = [UUID(value) for value in args.subject_ids]
    except ValueError:
        parser.error("each SUBJECT_ID must be a valid UUID")
    if args.execute and not args.i_confirm_user_authorized_image_fetch:
        parser.error("--execute requires --i-confirm-user-authorized-image-fetch")
    if args.i_confirm_user_authorized_image_fetch and not args.execute:
        parser.error("--i-confirm-user-authorized-image-fetch requires --execute")
    if args.execute and args.dry_run:
        parser.error("--execute and --dry-run cannot be combined")
    return args


def build_runtime(
    *, settings: Settings, max_images_per_subject: int
) -> tuple[AsyncEngine, OperatorRuntime]:
    """Construct database, blob and collection dependencies from runtime settings."""
    engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
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
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(blob_store, uow_factory)
    )
    collection_service = create_subject_collection_service(
        settings=settings,
        uow_factory=uow_factory,
        blob_store=blob_store,
        model_gateway=None,
        source_media_max_images_per_subject=max_images_per_subject,
        stop_on_source_media_policy_block=True,
    )
    return engine, OperatorRuntime(uow_factory, artifact_store, collection_service)


async def _latest_verified_extraction(
    subject_id: UUID,
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
) -> ProductionExtractionV1:
    selected: ProductionArtifact | None = None
    async with uow_factory() as uow:
        runs = sorted(
            await uow.production_runs.list_for_subject(subject_id),
            key=lambda run: (run.run_number, run.created_at, run.id.hex),
            reverse=True,
        )
        for run in runs:
            artifacts = await uow.production_artifacts.list_for_run(run.id)
            verified = [
                artifact
                for artifact in artifacts
                if artifact.production_run_id == run.id
                and artifact.subject_id == subject_id
                and artifact.stage is ProductionArtifactStage.EXTRACTION
                and artifact.status is ProductionArtifactStatus.VERIFIED
                and artifact.canonical_blob_id is not None
            ]
            if verified:
                selected = max(
                    verified,
                    key=lambda artifact: (
                        artifact.version,
                        artifact.created_at,
                        artifact.id.hex,
                    ),
                )
                break
    if selected is None:
        raise NoVerifiedExtractionError(
            f"subject {subject_id} has no verified extraction artifact"
        )
    payload = await artifact_store.read_json(selected.canonical_blob_id)
    extraction = production_extraction_from_json(payload)
    if extraction.subject_id != subject_id:
        raise ValueError(
            "verified extraction subject identity does not match the requested subject"
        )
    return extraction


async def _archived_source_inputs(
    subject_id: UUID,
    extraction: ProductionExtractionV1,
    *,
    source_document_repository: SourceDocumentRepository,
    blob_repository: BlobRepository,
    artifact_store: ProductionArtifactStore,
) -> tuple[ArchivedFigureSource, ...]:
    documents = await source_document_repository.list_for_subject(subject_id)
    documents_by_id = {document.id: document for document in documents}
    sources: list[ArchivedFigureSource] = []
    for source in extraction.sources:
        document = documents_by_id.get(source.source_document_id)
        if document is None or document.subject_id != subject_id:
            continue
        blob_id = document.decoded_blob_id or document.blob_id
        blob_record = await blob_repository.get(blob_id)
        if blob_record is None:
            continue
        mime_type = (
            (document.detected_mime_type or blob_record.descriptor.mime_type)
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if mime_type not in {"text/html", "application/xhtml+xml", "application/pdf"}:
            continue
        content = b""
        if blob_record.descriptor.size <= MAX_SOURCE_DOCUMENT_BYTES:
            content = await artifact_store.read_bytes(
                blob_id, max_bytes=MAX_SOURCE_DOCUMENT_BYTES
            )
        sources.append(
            ArchivedFigureSource(
                source_document_id=document.id,
                source_url=document.final_url or source.canonical_url,
                mime_type=mime_type,
                blob_id=blob_id,
                sha256=blob_record.descriptor.sha256,
                byte_size=blob_record.descriptor.size,
                content=content,
            )
        )
    return tuple(sources)


async def _preview_subject(
    subject_id: UUID,
    extraction: ProductionExtractionV1,
    *,
    runtime: OperatorRuntime,
) -> SubjectPreview:
    policy = runtime.collection_service.source_media_archiver.policy
    async with runtime.uow_factory() as uow:
        sources = await _archived_source_inputs(
            subject_id,
            extraction,
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=runtime.artifact_store,
        )
        observations = extract_source_media_observations(
            sources,
            boilerplate_pattern=policy.boilerplate_pattern,
            landmark_pattern=policy.landmark_pattern,
            tracking_pattern=policy.tracking_pattern,
            related_content_heading_pattern=policy.related_content_heading_pattern,
        )
        records = tuple(
            await uow.source_media_candidates.list_for_subject_policy(
                subject_id,
                runtime.collection_service.source_media_archiver.policy_sha256,
            )
        )
    return SubjectPreview(
        html_sources=sum(
            source.mime_type in {"text/html", "application/xhtml+xml"}
            for source in sources
        ),
        pdf_sources=sum(source.mime_type == "application/pdf" for source in sources),
        image_observations=len(observations),
        records=records,
    )


def _counts(values: Counter[str]) -> str:
    if not values:
        return "none"
    return ",".join(f"{key}:{values[key]}" for key in sorted(values))


def _record_counts(
    records: Sequence[SourceMediaRecord],
) -> tuple[Counter[str], Counter[str]]:
    return (
        Counter(record.status.value for record in records),
        Counter(record.reason_code.value for record in records),
    )


def _has_policy_block(records: Sequence[SourceMediaRecord]) -> bool:
    return any(
        record.collection_diagnostics.get("outcome") == "blocked" for record in records
    )


def _archived_bytes(records: Sequence[SourceMediaRecord]) -> int:
    by_digest = {
        record.sha256: record.byte_size
        for record in records
        if record.blob_id is not None
        and record.sha256 is not None
        and record.byte_size is not None
    }
    return sum(by_digest.values())


async def process_subject(
    subject_id: UUID,
    *,
    runtime: OperatorRuntime,
    execute: bool,
) -> bool:
    extraction = await _latest_verified_extraction(
        subject_id,
        uow_factory=runtime.uow_factory,
        artifact_store=runtime.artifact_store,
    )
    archiver = runtime.collection_service.source_media_archiver
    if not execute:
        preview = await _preview_subject(subject_id, extraction, runtime=runtime)
        statuses, reasons = _record_counts(preview.records)
        print(
            f"subject={subject_id} mode=dry-run html_sources={preview.html_sources} "
            f"pdf_sources={preview.pdf_sources} image_observations={preview.image_observations} "
            f"existing_records={len(preview.records)} statuses=[{_counts(statuses)}] "
            f"reasons=[{_counts(reasons)}]"
        )
        return True

    async with runtime.uow_factory() as uow:
        await load_archived_source_figure_inventory(
            subject_id=subject_id,
            extraction_sources=extraction.sources,
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=runtime.artifact_store,
            media_archiver=archiver,
        )
        records = tuple(
            await uow.source_media_candidates.list_for_subject_policy(
                subject_id, archiver.policy_sha256
            )
        )
    statuses, reasons = _record_counts(records)
    blocked = _has_policy_block(records)
    print(
        f"subject={subject_id} mode=execute records={len(records)} "
        f"statuses=[{_counts(statuses)}] reasons=[{_counts(reasons)}] "
        f"archived_bytes={_archived_bytes(records)} policy_blocked={str(blocked).lower()}"
    )
    return not blocked


async def process_subjects(
    subject_ids: Sequence[UUID],
    *,
    runtime: OperatorRuntime,
    execute: bool,
    delay_seconds: float,
) -> int:
    errors = 0
    for index, subject_id in enumerate(subject_ids):
        try:
            should_continue = await process_subject(
                subject_id, runtime=runtime, execute=execute
            )
        except NoVerifiedExtractionError as exc:
            print(f"ERROR: {exc}")
            errors += 1
            should_continue = True
        except (UnsafeAddressError, AccessBlockedError):
            print(
                f"ERROR: subject={subject_id} stopped by collection access/SSRF policy"
            )
            return 1
        except Exception as exc:  # noqa: BLE001 - suppress source data in operator diagnostics
            print(
                f"ERROR: subject={subject_id} failed ({type(exc).__name__}); "
                "details omitted to avoid exposing source data"
            )
            errors += 1
            should_continue = True
        if not should_continue:
            return 1
        if index + 1 < len(subject_ids) and delay_seconds:
            await asyncio.sleep(delay_seconds)
    return 1 if errors else 0


async def _run(args: argparse.Namespace) -> int:
    # Runtime values come only from the container environment; Settings normally
    # supports a local .env file, which this operator must not read.
    settings = Settings(_env_file=None)
    engine, runtime = build_runtime(
        settings=settings,
        max_images_per_subject=args.max_images_per_subject,
    )
    try:
        return await process_subjects(
            args.subject_ids,
            runtime=runtime,
            execute=args.execute,
            delay_seconds=args.delay_seconds,
        )
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Stopped by operator", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
