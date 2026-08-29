"""Repositories for immutable publication manifests and releases."""

from collections.abc import Sequence
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.edition_publication import (
    EditionRelease,
    PublicationManifestEntryV1,
    PublicationManifestExclusionV1,
    PublicationManifestV1,
)
from cti_app.infrastructure.database.models.edition_publication import (
    EditionReleaseRow,
    PublicationManifestEntryRow,
    PublicationManifestExclusionRow,
    PublicationManifestRow,
)


class SqlAlchemyPublicationManifestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, manifest: PublicationManifestV1, manifest_blob_id: UUID) -> None:
        self._session.add(
            PublicationManifestRow(
                id=manifest.id,
                edition_id=manifest.edition_id,
                edition_version=manifest.edition_version,
                batch_id=manifest.batch_id,
                manifest_blob_id=manifest_blob_id,
                content_sha256=manifest.content_sha256,
                created_by=manifest.created_by,
                created_at=manifest.created_at,
            )
        )
        await self._session.flush()

    async def get(self, manifest_id: UUID) -> PublicationManifestV1 | None:
        return await self._load_manifest(
            select(PublicationManifestRow).where(PublicationManifestRow.id == manifest_id)
        )

    async def get_blob_id(self, manifest_id: UUID) -> UUID | None:
        return cast(
            UUID | None,
            await self._session.scalar(
                select(PublicationManifestRow.manifest_blob_id).where(
                    PublicationManifestRow.id == manifest_id
                )
            ),
        )

    async def get_for_edition_version(
        self, edition_id: UUID, edition_version: int
    ) -> PublicationManifestV1 | None:
        return await self._load_manifest(
            select(PublicationManifestRow).where(
                PublicationManifestRow.edition_id == edition_id,
                PublicationManifestRow.edition_version == edition_version,
            )
        )

    async def get_latest_for_edition(self, edition_id: UUID) -> PublicationManifestV1 | None:
        return await self._load_manifest(
            select(PublicationManifestRow)
            .where(PublicationManifestRow.edition_id == edition_id)
            .order_by(
                PublicationManifestRow.edition_version.desc(), PublicationManifestRow.id.desc()
            )
            .limit(1)
        )

    async def _load_manifest(
        self, query: Select[tuple[PublicationManifestRow]]
    ) -> PublicationManifestV1 | None:
        row = await self._session.scalar(query)
        if row is None:
            return None
        entries = await self._session.scalars(
            select(PublicationManifestEntryRow)
            .where(PublicationManifestEntryRow.manifest_id == row.id)
            .order_by(PublicationManifestEntryRow.position)
        )
        exclusions = await self._session.scalars(
            select(PublicationManifestExclusionRow)
            .where(PublicationManifestExclusionRow.manifest_id == row.id)
            .order_by(PublicationManifestExclusionRow.subject_id)
        )
        return PublicationManifestV1(
            id=row.id,
            edition_id=row.edition_id,
            edition_version=row.edition_version,
            batch_id=row.batch_id,
            created_by=row.created_by,
            created_at=row.created_at,
            entries=tuple(_entry_from_row(entry) for entry in entries),
            exclusions=tuple(_exclusion_from_row(exclusion) for exclusion in exclusions),
            content_sha256=row.content_sha256,
        )


class SqlAlchemyPublicationManifestEntryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(
        self, manifest_id: UUID, entries: Sequence[PublicationManifestEntryV1]
    ) -> None:
        self._session.add_all(
            [
                PublicationManifestEntryRow(
                    id=uuid4(),
                    manifest_id=manifest_id,
                    position=entry.position,
                    subject_id=entry.subject_id,
                    production_run_id=entry.production_run_id,
                    pipeline_generation=entry.pipeline_generation,
                    document_artifact_id=entry.document_artifact_id,
                    document_artifact_version=entry.document_artifact_version,
                    document_input_hash=entry.document_input_hash,
                )
                for entry in entries
            ]
        )
        await self._session.flush()

    async def list_for_manifest(self, manifest_id: UUID) -> Sequence[PublicationManifestEntryV1]:
        rows = await self._session.scalars(
            select(PublicationManifestEntryRow)
            .where(PublicationManifestEntryRow.manifest_id == manifest_id)
            .order_by(PublicationManifestEntryRow.position)
        )
        return [_entry_from_row(row) for row in rows]


class SqlAlchemyPublicationManifestExclusionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(
        self, manifest_id: UUID, exclusions: Sequence[PublicationManifestExclusionV1]
    ) -> None:
        self._session.add_all(
            [
                PublicationManifestExclusionRow(
                    id=uuid4(),
                    manifest_id=manifest_id,
                    subject_id=exclusion.subject_id,
                    review_decision_id=exclusion.review_decision_id,
                )
                for exclusion in exclusions
            ]
        )
        await self._session.flush()

    async def list_for_manifest(
        self, manifest_id: UUID
    ) -> Sequence[PublicationManifestExclusionV1]:
        rows = await self._session.scalars(
            select(PublicationManifestExclusionRow)
            .where(PublicationManifestExclusionRow.manifest_id == manifest_id)
            .order_by(PublicationManifestExclusionRow.subject_id)
        )
        return [_exclusion_from_row(row) for row in rows]


class SqlAlchemyEditionReleaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, release: EditionRelease) -> bool:
        statement = (
            insert(EditionReleaseRow)
            .values(
                id=release.id,
                edition_id=release.edition_id,
                manifest_id=release.manifest_id,
                edition_document_blob_id=release.edition_document_blob_id,
                markdown_blob_id=release.markdown_blob_id,
                docx_blob_id=release.docx_blob_id,
                edition_document_sha256=release.edition_document_sha256,
                markdown_sha256=release.markdown_sha256,
                docx_sha256=release.docx_sha256,
                created_at=release.created_at,
            )
            .on_conflict_do_nothing(index_elements=[EditionReleaseRow.manifest_id])
        )
        result = await self._session.execute(statement)
        await self._session.flush()
        return bool(getattr(result, "rowcount", 0))

    async def get_by_manifest(self, manifest_id: UUID) -> EditionRelease | None:
        row = await self._session.scalar(
            select(EditionReleaseRow).where(EditionReleaseRow.manifest_id == manifest_id)
        )
        return _release_from_row(row) if row else None

    async def get_for_edition(self, edition_id: UUID) -> EditionRelease | None:
        row = await self._session.scalar(
            select(EditionReleaseRow)
            .where(EditionReleaseRow.edition_id == edition_id)
            .order_by(EditionReleaseRow.created_at.desc(), EditionReleaseRow.id.desc())
            .limit(1)
        )
        return _release_from_row(row) if row else None


def _entry_from_row(row: PublicationManifestEntryRow) -> PublicationManifestEntryV1:
    return PublicationManifestEntryV1(
        position=row.position,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        pipeline_generation=row.pipeline_generation,
        document_artifact_id=row.document_artifact_id,
        document_artifact_version=row.document_artifact_version,
        document_input_hash=row.document_input_hash,
    )


def _exclusion_from_row(row: PublicationManifestExclusionRow) -> PublicationManifestExclusionV1:
    return PublicationManifestExclusionV1(
        subject_id=row.subject_id, review_decision_id=row.review_decision_id
    )


def _release_from_row(row: EditionReleaseRow) -> EditionRelease:
    return EditionRelease(
        id=row.id,
        edition_id=row.edition_id,
        manifest_id=row.manifest_id,
        edition_document_blob_id=row.edition_document_blob_id,
        markdown_blob_id=row.markdown_blob_id,
        docx_blob_id=row.docx_blob_id,
        edition_document_sha256=row.edition_document_sha256,
        markdown_sha256=row.markdown_sha256,
        docx_sha256=row.docx_sha256,
        created_at=row.created_at,
    )
