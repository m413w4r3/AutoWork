#!/usr/bin/env python3
"""Export deterministic investigation annexes from canonical PostgreSQL and MinIO state."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from minio import Minio
from sqlalchemy import func, select

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_annex_export import (
    ANNEX_DISCLOSURE_PROFILES,
    AnnexIncompleteExportError,
    write_investigation_annex,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import get_settings
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.database.investigation_annex_reader import (
    load_investigation_annex_snapshot,
)
from cti_app.infrastructure.database.models.investigation import AnalystOpinionVersionRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("investigation_id", type=UUID)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--include-restricted-archive",
        action="store_true",
        help="include the profile-selected private material and create technical-archive.tar.gz",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(ANNEX_DISCLOSURE_PROFILES),
        default=None,
        help="override ADVISOR_DISCLOSURE_PROFILE for this export",
    )
    parser.add_argument(
        "--with-pdf-preview",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="render the latest current draft opinion PDF when available (default: enabled)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing annex directory atomically",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    profile = args.profile or settings.advisor_disclosure_profile
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
            profile=profile,
        )
        opinion_preview_pdf: bytes | None = None
        opinion_preview_reason: str | None = None
        opinion_preview_metadata: dict[str, Any] = {}
        if args.with_pdf_preview:
            try:
                preview = await _render_latest_draft_preview(
                    session_factory,
                    args.investigation_id,
                    artifact_store,
                )
            except Exception as exc:
                preview = None
                preview_error_code = getattr(exc, "code", None)
                cause = (
                    str(preview_error_code)
                    if isinstance(preview_error_code, str)
                    else type(exc).__name__
                )
                opinion_preview_reason = f"indisponible: {cause}"
            if preview is None and opinion_preview_reason is None:
                opinion_preview_reason = "indisponible: aucun avis brouillon disponible"
            elif preview is not None:
                opinion_preview_pdf = preview.content
                opinion_preview_metadata = {
                    "opinion_id": str(preview.opinion_id),
                    "opinion_version": preview.opinion_version,
                    "opinion_sha256": preview.opinion_sha256,
                    "base_publication_sha256": preview.base_publication_sha256,
                    "lineage_sha256": hashlib.sha256(
                        json.dumps(
                            preview.lineage_manifest,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                }
        else:
            opinion_preview_reason = "indisponible: génération désactivée par l'option CLI"
        result = write_investigation_annex(
            snapshot,
            args.output_dir,
            include_restricted_archive=args.include_restricted_archive,
            overwrite=args.overwrite,
            profile=profile,
            opinion_preview_pdf=opinion_preview_pdf,
            opinion_preview_unavailable_reason=opinion_preview_reason,
            opinion_preview_metadata=opinion_preview_metadata,
        )
        print(
            json.dumps(
                {
                    "investigation_id": str(args.investigation_id),
                    "manifest_sha256": result.manifest_sha256,
                    "object_count": result.object_count,
                    "file_count": result.file_count,
                    "profile": profile,
                    "output_dir": str(result.output_dir),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        await engine.dispose()


async def _render_latest_draft_preview(
    session_factory: Any,
    investigation_id: UUID,
    artifact_store: ProductionArtifactStore,
) -> Any | None:
    """Render the latest current draft opinion through the existing preview service."""
    from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
    from cti_app.application.media_assets import MediaAssetStore
    from cti_app.application.publication_rendering import PublicationRenderService
    from cti_app.application.typst_paths import typst_bundle_paths
    from cti_app.application.typst_rendering import TypstRenderer
    from cti_app.domain.analyst_opinion import AnalystOpinionStatus
    from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler

    latest_versions = (
        select(
            AnalystOpinionVersionRow.opinion_id.label("opinion_id"),
            func.max(AnalystOpinionVersionRow.version).label("latest_version"),
        )
        .where(AnalystOpinionVersionRow.investigation_id == investigation_id)
        .group_by(AnalystOpinionVersionRow.opinion_id)
        .subquery()
    )
    async with session_factory() as session:
        rows = await session.execute(
            select(
                AnalystOpinionVersionRow.opinion_id,
                AnalystOpinionVersionRow.version,
                AnalystOpinionVersionRow.created_at,
            )
            .join(
                latest_versions,
                (AnalystOpinionVersionRow.opinion_id == latest_versions.c.opinion_id)
                & (AnalystOpinionVersionRow.version == latest_versions.c.latest_version),
            )
            .where(
                AnalystOpinionVersionRow.investigation_id == investigation_id,
                AnalystOpinionVersionRow.status == AnalystOpinionStatus.DRAFT.value,
            )
            .order_by(
                AnalystOpinionVersionRow.created_at.desc(),
                AnalystOpinionVersionRow.version.desc(),
            )
            .limit(1)
        )
        latest = rows.first()
    if latest is None:
        return None
    opinion_id, version, _created_at = latest

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    async with uow_factory() as uow:
        opinion = await uow.analyst_opinions.get(opinion_id, version=version)
    if opinion is None or opinion.status is not AnalystOpinionStatus.DRAFT:
        return None

    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    media_asset_store = MediaAssetStore(artifact_store, uow_factory)
    renderer = PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media_asset_store,
        renderer=TypstRenderer(),
        compiler=TypstSubprocessCompiler(binary=os.environ.get("TYPST_BINARY", "typst")),
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
    )
    preview_service = AnalystOpinionPreviewService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        renderer=renderer,
    )
    return await preview_service.preview_pdf(
        opinion.id,
        version=opinion.version,
        expected_opinion_sha256=opinion.content_sha256,
        expected_base_publication_sha256=opinion.base_publication_sha256,
    )


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
    except AnnexIncompleteExportError as exc:
        print(
            f"Annex export incomplete: expected canonical {exc.asset_type} bytes were not written",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print(
            f"Annex export failed ({type(exc).__name__}); message withheld (may contain evidence)."
            " Re-run with ANNEX_EXPORT_DEBUG=1 to print the traceback locally.",
            file=sys.stderr,
        )
        if os.environ.get("ANNEX_EXPORT_DEBUG") == "1":
            import traceback

            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
