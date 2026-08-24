from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.briefs import (
    BriefBlock,
    BriefDraft,
    BriefDraftStatus,
    BriefEvidencePack,
    BriefSentence,
    EvidencePackScope,
)
from cti_app.infrastructure.database.models.briefs import (
    BriefDraftRow,
    BriefEvidencePackRow,
)


class SqlAlchemyBriefEvidencePackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, pack: BriefEvidencePack) -> None:
        self._session.add(BriefEvidencePackRow(**_brief_pack_values(pack)))
        await self._session.flush()

    async def get(self, pack_id: UUID) -> BriefEvidencePack | None:
        row = await self._session.get(BriefEvidencePackRow, pack_id)
        return _brief_pack_from_row(row) if row else None

    async def get_current(self, subject_id: UUID) -> BriefEvidencePack | None:
        row = await self._session.scalar(
            select(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.subject_id == subject_id)
            .order_by(BriefEvidencePackRow.version.desc())
            .limit(1)
        )
        return _brief_pack_from_row(row) if row else None

    async def get_by_hash(self, subject_id: UUID, content_hash: str) -> BriefEvidencePack | None:
        row = await self._session.scalar(
            select(BriefEvidencePackRow).where(
                BriefEvidencePackRow.subject_id == subject_id,
                BriefEvidencePackRow.content_hash == content_hash,
            )
        )
        return _brief_pack_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefEvidencePack]:
        rows = await self._session.scalars(
            select(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.subject_id == subject_id)
            .order_by(BriefEvidencePackRow.version)
        )
        return [_brief_pack_from_row(row) for row in rows]


class SqlAlchemyBriefDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, draft: BriefDraft) -> None:
        self._session.add(BriefDraftRow(**_brief_draft_values(draft)))
        await self._session.flush()

    async def get(self, draft_id: UUID) -> BriefDraft | None:
        row = await self._session.get(BriefDraftRow, draft_id)
        return _brief_draft_from_row(row) if row else None

    async def get_current(self, subject_id: UUID) -> BriefDraft | None:
        row = await self._session.scalar(
            select(BriefDraftRow)
            .where(BriefDraftRow.subject_id == subject_id)
            .order_by(BriefDraftRow.version.desc())
            .limit(1)
        )
        return _brief_draft_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefDraft]:
        rows = await self._session.scalars(
            select(BriefDraftRow)
            .where(BriefDraftRow.subject_id == subject_id)
            .order_by(BriefDraftRow.version)
        )
        return [_brief_draft_from_row(row) for row in rows]


def _brief_pack_values(pack: BriefEvidencePack) -> dict[str, object]:
    return {
        "id": pack.id,
        "subject_id": pack.subject_id,
        "edition_id": pack.edition_id,
        "group_id": pack.group_id,
        "version": pack.version,
        "content_hash": pack.content_hash,
        "object_hashes": list(pack.object_hashes),
        "sources": list(pack.sources),
        "claims": list(pack.claims),
        "indicators": list(pack.indicators),
        "normalized_entities": list(pack.normalized_entities),
        "uncertainties": list(pack.uncertainties),
        "human_decisions": list(pack.human_decisions),
        "blob_id": pack.blob_id,
        "created_by": pack.created_by,
        "created_at": pack.created_at,
        "built_from_snapshot_id": pack.built_from_snapshot_id,
        "built_from_snapshot_version": pack.built_from_snapshot_version,
        "covered_contribution_ids": [str(value) for value in pack.covered_contribution_ids],
        "scope": pack.scope.value,
        "base_pack_id": pack.base_pack_id,
    }


def _brief_pack_from_row(row: BriefEvidencePackRow) -> BriefEvidencePack:
    return BriefEvidencePack(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        version=row.version,
        content_hash=row.content_hash,
        object_hashes=tuple(row.object_hashes),
        sources=tuple(row.sources),
        claims=tuple(row.claims),
        indicators=tuple(row.indicators),
        normalized_entities=tuple(row.normalized_entities),
        uncertainties=tuple(row.uncertainties),
        human_decisions=tuple(row.human_decisions),
        blob_id=row.blob_id,
        created_by=row.created_by,
        created_at=row.created_at,
        built_from_snapshot_id=row.built_from_snapshot_id,
        built_from_snapshot_version=row.built_from_snapshot_version,
        covered_contribution_ids=tuple(UUID(value) for value in row.covered_contribution_ids or ()),
        scope=EvidencePackScope(row.scope or "full"),
        base_pack_id=row.base_pack_id,
    )


def _brief_draft_values(draft: BriefDraft) -> dict[str, object]:
    return {
        "id": draft.id,
        "subject_id": draft.subject_id,
        "edition_id": draft.edition_id,
        "group_id": draft.group_id,
        "pack_id": draft.pack_id,
        "pack_hash": draft.pack_hash,
        "version": draft.version,
        "title": draft.title,
        "blocks": [
            {
                "id": str(block.id),
                "sentences": [
                    {
                        "id": str(sentence.id),
                        "text": sentence.text,
                        "factual": sentence.factual,
                        "claim_ids": [str(item) for item in sentence.claim_ids],
                        "indicator_ids": [str(item) for item in sentence.indicator_ids],
                    }
                    for sentence in block.sentences
                ],
            }
            for block in draft.blocks
        ],
        "limits": list(draft.limits),
        "source_ids": [str(item) for item in draft.source_ids],
        "model_run_id": draft.model_run_id,
        "provider": draft.provider,
        "status": draft.status.value,
        "parent_draft_id": draft.parent_draft_id,
        "regenerated_block_id": draft.regenerated_block_id,
        "created_at": draft.created_at,
    }


def _brief_draft_from_row(row: BriefDraftRow) -> BriefDraft:
    return BriefDraft(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        pack_id=row.pack_id,
        pack_hash=row.pack_hash,
        version=row.version,
        title=row.title,
        blocks=tuple(
            BriefBlock(
                id=UUID(str(block["id"])),
                sentences=tuple(
                    BriefSentence(
                        id=UUID(str(sentence["id"])),
                        text=str(sentence["text"]),
                        factual=bool(sentence["factual"]),
                        claim_ids=tuple(UUID(str(item)) for item in sentence["claim_ids"]),
                        indicator_ids=tuple(UUID(str(item)) for item in sentence["indicator_ids"]),
                    )
                    for sentence in block["sentences"]
                ),
            )
            for block in row.blocks
        ),
        limits=tuple(row.limits),
        source_ids=tuple(UUID(item) for item in row.source_ids),
        model_run_id=row.model_run_id,
        provider=row.provider,
        status=BriefDraftStatus(row.status),
        parent_draft_id=row.parent_draft_id,
        regenerated_block_id=row.regenerated_block_id,
        created_at=row.created_at,
    )
