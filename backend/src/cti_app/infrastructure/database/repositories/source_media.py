from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.source_media import (
    SourceMediaReasonCode,
    SourceMediaRecord,
    SourceMediaStatus,
)
from cti_app.infrastructure.database.models.source_media import SourceMediaCandidateRow


class SqlAlchemySourceMediaRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, candidate_id: UUID) -> SourceMediaRecord | None:
        row = await self._session.get(SourceMediaCandidateRow, candidate_id)
        return _source_media_from_row(row) if row is not None else None

    async def list_for_subject_policy(
        self, subject_id: UUID, policy_sha256: str
    ) -> Sequence[SourceMediaRecord]:
        rows = await self._session.scalars(
            select(SourceMediaCandidateRow)
            .where(
                SourceMediaCandidateRow.subject_id == subject_id,
                SourceMediaCandidateRow.policy_sha256 == policy_sha256,
            )
            .order_by(SourceMediaCandidateRow.id)
        )
        return [_source_media_from_row(row) for row in rows]

    async def add_if_absent(self, record: SourceMediaRecord) -> SourceMediaRecord:
        await self._session.execute(
            insert(SourceMediaCandidateRow)
            .values(
                id=record.id,
                subject_id=record.subject_id,
                source_document_id=record.source_document_id,
                blob_id=record.blob_id,
                sha256=record.sha256,
                mime_type=record.mime_type,
                width=record.width,
                height=record.height,
                byte_size=record.byte_size,
                page=record.page,
                page_bbox=record.page_bbox,
                anchor=record.anchor,
                dom_locator=record.dom_locator,
                original_url=record.original_url,
                requested_url=record.requested_url,
                final_url=record.final_url,
                alt_text=record.alt_text,
                caption_text=record.caption_text,
                nearby_heading_text=record.nearby_heading_text,
                collection_diagnostics=record.collection_diagnostics,
                policy_version=record.policy_version,
                policy_sha256=record.policy_sha256,
                status=record.status.value,
                reason_code=record.reason_code.value,
                perceptual_hash=record.perceptual_hash,
                collected_at=record.collected_at,
            )
            .on_conflict_do_nothing(index_elements=[SourceMediaCandidateRow.id])
        )
        stored = await self.get(record.id)
        if stored is None:
            raise RuntimeError("Source media candidate insert did not produce a row")
        return stored


def _source_media_from_row(row: SourceMediaCandidateRow) -> SourceMediaRecord:
    return SourceMediaRecord(
        id=row.id,
        subject_id=row.subject_id,
        source_document_id=row.source_document_id,
        blob_id=row.blob_id,
        sha256=row.sha256,
        mime_type=row.mime_type,
        width=row.width,
        height=row.height,
        byte_size=row.byte_size,
        page=row.page,
        page_bbox=dict(row.page_bbox) if row.page_bbox is not None else None,
        anchor=row.anchor,
        dom_locator=row.dom_locator,
        original_url=row.original_url,
        requested_url=row.requested_url,
        final_url=row.final_url,
        alt_text=row.alt_text,
        caption_text=row.caption_text,
        nearby_heading_text=row.nearby_heading_text,
        collection_diagnostics=dict(row.collection_diagnostics),
        policy_version=row.policy_version,
        policy_sha256=row.policy_sha256,
        status=SourceMediaStatus(row.status),
        reason_code=SourceMediaReasonCode(row.reason_code),
        perceptual_hash=row.perceptual_hash,
        collected_at=row.collected_at,
    )
