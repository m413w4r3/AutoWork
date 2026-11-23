"""SQLAlchemy repository for non-canonical Bridge note proposals (M24-08B)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import (
    VersionConflictError,
    same_note_proposal_request,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    OpinionBlockType,
    note_content_blocks_from_payload,
    note_content_blocks_to_payload,
)
from cti_app.domain.opinion_note_proposal import (
    NoteProposalConsultationType,
    NoteProposalStatus,
    NoteProposalV1,
    NoteTargetBlockType,
)
from cti_app.infrastructure.database.models.opinion_note_proposals import (
    InvestigationNoteProposalRow,
)


def _from_row(row: InvestigationNoteProposalRow) -> NoteProposalV1:
    return NoteProposalV1(
        id=row.id,
        investigation_id=row.investigation_id,
        opinion_id=row.opinion_id,
        task_id=row.task_id,
        anchor=row.anchor,
        target_block_type=NoteTargetBlockType(row.target_block_type),
        bridge_block_type=OpinionBlockType(row.bridge_block_type),
        consultation_type=NoteProposalConsultationType(row.consultation_type),
        base_note_id=row.base_note_id,
        note_state=AnalystObservationState(row.note_state),
        note_relation=AnalystObservationRelation(row.note_relation),
        cited_finding_refs=tuple(UUID(item) for item in row.cited_finding_refs),
        cited_observation_refs=tuple(UUID(item) for item in row.cited_observation_refs),
        proposed_text=row.proposed_text,
        proposed_text_sha256=row.proposed_text_sha256,
        bridge_invocation_id=row.bridge_invocation_id,
        bridge_output_sha256=row.bridge_output_sha256,
        bridge_response_observation_id=row.bridge_response_observation_id,
        tool_invocation_id=row.tool_invocation_id,
        base_opinion_version=row.base_opinion_version,
        base_opinion_sha256=row.base_opinion_sha256,
        base_publication_artifact_id=row.base_publication_artifact_id,
        base_publication_version=row.base_publication_version,
        base_publication_sha256=row.base_publication_sha256,
        anchor_before_sha256=row.anchor_before_sha256,
        content_blocks=note_content_blocks_from_payload(row.content_blocks),
        status=NoteProposalStatus(row.status),
        status_version=row.status_version,
        accepted_opinion_version=row.accepted_opinion_version,
        analyst_decision_id=row.analyst_decision_id,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _values(proposal: NoteProposalV1) -> dict[str, object]:
    return {
        "id": proposal.id,
        "investigation_id": proposal.investigation_id,
        "opinion_id": proposal.opinion_id,
        "task_id": proposal.task_id,
        "anchor": proposal.anchor,
        "target_block_type": proposal.target_block_type.value,
        "bridge_block_type": proposal.bridge_block_type.value,
        "consultation_type": proposal.consultation_type.value,
        "base_note_id": proposal.base_note_id,
        "note_state": proposal.note_state.value,
        "note_relation": proposal.note_relation.value,
        "cited_finding_refs": [str(item) for item in proposal.cited_finding_refs],
        "cited_observation_refs": [str(item) for item in proposal.cited_observation_refs],
        "proposed_text": proposal.proposed_text,
        "proposed_text_sha256": proposal.proposed_text_sha256,
        "bridge_invocation_id": proposal.bridge_invocation_id,
        "bridge_output_sha256": proposal.bridge_output_sha256,
        "bridge_response_observation_id": proposal.bridge_response_observation_id,
        "tool_invocation_id": proposal.tool_invocation_id,
        "base_opinion_version": proposal.base_opinion_version,
        "base_opinion_sha256": proposal.base_opinion_sha256,
        "base_publication_artifact_id": proposal.base_publication_artifact_id,
        "base_publication_version": proposal.base_publication_version,
        "base_publication_sha256": proposal.base_publication_sha256,
        "anchor_before_sha256": proposal.anchor_before_sha256,
        "content_blocks": note_content_blocks_to_payload(proposal.content_blocks),
        "status": proposal.status.value,
        "status_version": proposal.status_version,
        "accepted_opinion_version": proposal.accepted_opinion_version,
        "analyst_decision_id": proposal.analyst_decision_id,
        "idempotency_key": proposal.idempotency_key,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
    }


class SqlAlchemyNoteProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, proposal_id: UUID) -> NoteProposalV1 | None:
        row = await self._session.get(InvestigationNoteProposalRow, proposal_id)
        return _from_row(row) if row is not None else None

    async def get_by_idempotency(
        self, investigation_id: UUID, idempotency_key: str
    ) -> NoteProposalV1 | None:
        row = await self._session.scalar(
            select(InvestigationNoteProposalRow).where(
                InvestigationNoteProposalRow.investigation_id == investigation_id,
                InvestigationNoteProposalRow.idempotency_key == idempotency_key,
            )
        )
        return _from_row(row) if row is not None else None

    async def list_for_investigation(
        self,
        investigation_id: UUID,
        *,
        status: NoteProposalStatus | None = None,
    ) -> tuple[NoteProposalV1, ...]:
        statement = select(InvestigationNoteProposalRow).where(
            InvestigationNoteProposalRow.investigation_id == investigation_id
        )
        if status is not None:
            statement = statement.where(InvestigationNoteProposalRow.status == status.value)
        statement = statement.order_by(
            InvestigationNoteProposalRow.created_at,
            InvestigationNoteProposalRow.id,
        )
        rows = await self._session.scalars(statement)
        return tuple(_from_row(row) for row in rows)

    async def append_if_absent(self, proposal: NoteProposalV1) -> tuple[NoteProposalV1, bool]:
        result = await self._session.execute(
            insert(InvestigationNoteProposalRow)
            .values(**_values(proposal))
            .on_conflict_do_nothing()
        )
        created = int(getattr(result, "rowcount", 0) or 0) == 1
        stored = await self.get(proposal.id)
        if created:
            if stored is None or stored != proposal:
                raise RuntimeError("note proposal insert did not produce its row")
            return stored, True
        replay = await self.get_by_idempotency(proposal.investigation_id, proposal.idempotency_key)
        if replay is not None:
            if not same_note_proposal_request(replay, proposal):
                raise VersionConflictError(
                    "Note proposal idempotency key records a different request"
                )
            return replay, False
        raise VersionConflictError("note proposal conflicts with a different row")

    async def update_if_version(
        self, proposal: NoteProposalV1, *, expected_status_version: int
    ) -> NoteProposalV1 | None:
        if proposal.status_version != expected_status_version + 1:
            raise ValueError("A note proposal update must advance one status version")
        row = await self._session.scalar(
            update(InvestigationNoteProposalRow)
            .where(
                InvestigationNoteProposalRow.id == proposal.id,
                InvestigationNoteProposalRow.status_version == expected_status_version,
            )
            .values(**_values(proposal))
            .returning(InvestigationNoteProposalRow)
        )
        return _from_row(row) if row is not None else None
