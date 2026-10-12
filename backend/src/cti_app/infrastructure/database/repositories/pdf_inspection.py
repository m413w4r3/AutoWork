from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.pdf_inspection import PdfRenderReference
from cti_app.infrastructure.database.models.edition_publication import (
    EditionReleaseRow,
    PublicationManifestEntryRow,
    PublicationManifestRow,
)
from cti_app.infrastructure.database.models.edition_render import EditionRenderRow
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.models.production import ProductionArtifactRow
from cti_app.infrastructure.database.models.publication_render import PublicationRenderRow


class SqlAlchemyPdfInspectionSourceRepository:
    """Read every persisted render provenance matching a PDF output SHA-256."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_by_output_sha256(self, pdf_sha256: str) -> tuple[PdfRenderReference, ...]:
        references: list[PdfRenderReference] = []
        async with self._session_factory() as session:
            publication_rows = await session.execute(
                select(PublicationRenderRow, ProductionArtifactRow)
                .join(
                    ProductionArtifactRow,
                    ProductionArtifactRow.id == PublicationRenderRow.publication_artifact_id,
                )
                .where(
                    PublicationRenderRow.output_sha256 == pdf_sha256,
                    PublicationRenderRow.status == "succeeded",
                    PublicationRenderRow.format == "pdf",
                )
                .order_by(PublicationRenderRow.id)
            )
            for render, artifact in publication_rows:
                if render.output_blob_id is None or render.output_byte_size is None:
                    continue
                references.append(
                    PdfRenderReference(
                        id=render.id,
                        kind="publication_render",
                        status=render.status,
                        input_hash=render.input_hash,
                        renderer_version=render.renderer_version,
                        template_sha256=render.template_sha256,
                        compiler_version=render.compiler_version,
                        font_bundle_version=render.font_bundle_version,
                        output_blob_id=render.output_blob_id,
                        output_sha256=render.output_sha256,
                        output_byte_size=render.output_byte_size,
                        source={
                            "kind": "subject_publication",
                            "subject_id": str(artifact.subject_id),
                            "artifact_id": str(artifact.id),
                            "artifact_version": artifact.version,
                            "artifact_input_hash": artifact.input_hash,
                        },
                    )
                )

            edition_rows = await session.execute(
                select(EditionRenderRow, EditionReleaseRow, PublicationManifestRow, EditionRow)
                .join(
                    EditionReleaseRow,
                    EditionReleaseRow.id == EditionRenderRow.edition_release_id,
                )
                .join(
                    PublicationManifestRow,
                    PublicationManifestRow.id == EditionReleaseRow.manifest_id,
                )
                .join(EditionRow, EditionRow.id == EditionReleaseRow.edition_id)
                .where(
                    EditionRenderRow.output_sha256 == pdf_sha256,
                    EditionRenderRow.status == "succeeded",
                    EditionRenderRow.format == "pdf",
                )
                .order_by(EditionRenderRow.id)
            )
            for render, release, manifest, edition in edition_rows:
                if render.output_blob_id is None or render.output_byte_size is None:
                    continue
                entry_rows = await session.execute(
                    select(PublicationManifestEntryRow)
                    .where(PublicationManifestEntryRow.manifest_id == manifest.id)
                    .order_by(PublicationManifestEntryRow.position)
                )
                artifacts = [
                    {
                        "subject_id": str(entry.subject_id),
                        "artifact_id": str(entry.document_artifact_id),
                        "artifact_version": entry.document_artifact_version,
                        "artifact_input_hash": entry.document_input_hash,
                    }
                    for entry in entry_rows.scalars()
                ]
                references.append(
                    PdfRenderReference(
                        id=render.id,
                        kind="edition_render",
                        status=render.status,
                        input_hash=render.input_hash,
                        renderer_version=render.renderer_version,
                        template_sha256=render.template_sha256,
                        compiler_version=render.compiler_version,
                        font_bundle_version=render.font_bundle_version,
                        output_blob_id=render.output_blob_id,
                        output_sha256=render.output_sha256,
                        output_byte_size=render.output_byte_size,
                        source={
                            "kind": "edition_release",
                            "edition_id": str(edition.id),
                            "release_id": str(release.id),
                            "manifest_id": str(manifest.id),
                            "manifest_sha256": manifest.content_sha256,
                            "edition_document_sha256": release.edition_document_sha256,
                            "artifacts": artifacts,
                        },
                    )
                )
        return tuple(references)

    async def find_by_render_id(self, render_id: UUID) -> PdfRenderReference | None:
        """Read one persisted render and its frozen owner/manifest identity."""
        async with self._session_factory() as session:
            publication_result = await session.execute(
                select(PublicationRenderRow, ProductionArtifactRow)
                .join(
                    ProductionArtifactRow,
                    ProductionArtifactRow.id == PublicationRenderRow.publication_artifact_id,
                )
                .where(PublicationRenderRow.id == render_id)
            )
            publication = publication_result.first()
            if publication is not None:
                render, artifact = publication
                return _publication_reference(render, artifact)

            edition_result = await session.execute(
                select(EditionRenderRow, EditionReleaseRow, PublicationManifestRow, EditionRow)
                .join(
                    EditionReleaseRow,
                    EditionReleaseRow.id == EditionRenderRow.edition_release_id,
                )
                .join(
                    PublicationManifestRow,
                    PublicationManifestRow.id == EditionReleaseRow.manifest_id,
                )
                .join(EditionRow, EditionRow.id == EditionReleaseRow.edition_id)
                .where(EditionRenderRow.id == render_id)
            )
            edition = edition_result.first()
            if edition is None:
                return None
            render, release, manifest, edition_row = edition
            entry_result = await session.execute(
                select(PublicationManifestEntryRow)
                .where(PublicationManifestEntryRow.manifest_id == manifest.id)
                .order_by(PublicationManifestEntryRow.position)
            )
            entries = [
                {
                    "position": entry.position,
                    "subject_id": str(entry.subject_id),
                    "production_run_id": str(entry.production_run_id),
                    "pipeline_generation": entry.pipeline_generation,
                    "artifact_id": str(entry.document_artifact_id),
                    "artifact_version": entry.document_artifact_version,
                    "artifact_input_hash": entry.document_input_hash,
                    "document_sha256": entry.document_sha256,
                    "provenance_batch_id": (
                        str(entry.provenance_batch_id) if entry.provenance_batch_id else None
                    ),
                    "review_decision_id": (
                        str(entry.review_decision_id) if entry.review_decision_id else None
                    ),
                }
                for entry in entry_result.scalars()
            ]
            return PdfRenderReference(
                id=render.id,
                kind="edition_render",
                status=render.status,
                input_hash=render.input_hash,
                renderer_version=render.renderer_version,
                template_sha256=render.template_sha256,
                compiler_version=render.compiler_version,
                font_bundle_version=render.font_bundle_version,
                output_blob_id=render.output_blob_id,
                output_sha256=render.output_sha256,
                output_byte_size=render.output_byte_size,
                source={
                    "kind": "edition_release",
                    "edition_id": str(edition_row.id),
                    "release_id": str(release.id),
                    "manifest_id": str(manifest.id),
                    "manifest_schema_version": manifest.schema_version,
                    "manifest_sha256": manifest.content_sha256,
                    "composition_id": str(manifest.composition_id)
                    if manifest.composition_id
                    else None,
                    "composition_version": manifest.composition_version,
                    "batch_id": str(manifest.batch_id) if manifest.batch_id else None,
                    "edition_document_sha256": release.edition_document_sha256,
                    "entries": entries,
                    "artifacts": [
                        {
                            key: entry[key]
                            for key in (
                                "subject_id",
                                "artifact_id",
                                "artifact_version",
                                "artifact_input_hash",
                            )
                        }
                        for entry in entries
                    ],
                },
                renderer=render.renderer,
                template_version=render.template_version,
                compiler=render.compiler,
                source_blob_id=render.source_blob_id,
                render_data_blob_id=render.render_data_blob_id,
                error_code=render.error_code,
                error_message=render.error_message,
            )


def _publication_reference(
    render: PublicationRenderRow, artifact: ProductionArtifactRow
) -> PdfRenderReference:
    return PdfRenderReference(
        id=render.id,
        kind="publication_render",
        status=render.status,
        input_hash=render.input_hash,
        renderer_version=render.renderer_version,
        template_sha256=render.template_sha256,
        compiler_version=render.compiler_version,
        font_bundle_version=render.font_bundle_version,
        output_blob_id=render.output_blob_id,
        output_sha256=render.output_sha256,
        output_byte_size=render.output_byte_size,
        source={
            "kind": "subject_publication",
            "subject_id": str(artifact.subject_id),
            "artifact_id": str(artifact.id),
            "artifact_version": artifact.version,
            "artifact_input_hash": artifact.input_hash,
        },
        renderer=render.renderer,
        template_version=render.template_version,
        compiler=render.compiler,
        source_blob_id=render.source_blob_id,
        render_data_blob_id=render.render_data_blob_id,
        error_code=render.error_code,
        error_message=render.error_message,
    )
