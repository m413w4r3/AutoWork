"""PostgreSQL persistence for editorial proposal aggregates and timelines."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalModelRun,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.infrastructure.database.models.editorial_proposals import (
    EditorialProposalEventRow,
    EditorialProposalModelRunRow,
    EditorialProposalRow,
)


class SqlAlchemyEditorialProposalRepository:
    """Insert proposals idempotently and project their exact persisted state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, subject_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> EditorialProposal | None:
        statement = (
            select(EditorialProposalRow, EditorialProposalModelRunRow)
            .outerjoin(
                EditorialProposalModelRunRow,
                EditorialProposalModelRunRow.proposal_id == EditorialProposalRow.id,
            )
            .where(
                EditorialProposalRow.subject_id == subject_id,
                EditorialProposalRow.actor_id == actor_id,
                EditorialProposalRow.idempotency_key == idempotency_key,
            )
        )
        result = await self._session.execute(statement)
        row, link = result.one_or_none() or (None, None)
        return _proposal_from_rows(row, link) if row is not None else None

    async def get_for_occurrence(
        self, subject_id: UUID, occurrence_id: UUID, proposal_id: UUID
    ) -> EditorialProposal | None:
        statement = (
            select(EditorialProposalRow, EditorialProposalModelRunRow)
            .outerjoin(
                EditorialProposalModelRunRow,
                EditorialProposalModelRunRow.proposal_id == EditorialProposalRow.id,
            )
            .where(
                EditorialProposalRow.id == proposal_id,
                EditorialProposalRow.subject_id == subject_id,
                EditorialProposalRow.occurrence_id == occurrence_id,
            )
        )
        result = await self._session.execute(statement)
        row, link = result.one_or_none() or (None, None)
        return _proposal_from_rows(row, link) if row is not None else None

    async def add_if_absent(
        self,
        proposal: EditorialProposal,
        *,
        event: EditorialProposalEvent,
        model_run: EditorialProposalModelRun | None,
    ) -> tuple[EditorialProposal, bool]:
        statement = (
            insert(EditorialProposalRow)
            .values(**_proposal_values(proposal))
            .on_conflict_do_nothing(constraint="uq_editorial_proposals_subject_actor_idempotency")
            .returning(EditorialProposalRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        if inserted_id is None:
            existing = await self.get_by_idempotency_key(
                proposal.subject_id, proposal.actor_id, proposal.idempotency_key
            )
            if existing is None:
                raise RuntimeError("editorial_proposal_idempotency_read_missing")
            return existing, False

        self._session.add(EditorialProposalEventRow(**_event_values(event)))
        if model_run is not None:
            self._session.add(EditorialProposalModelRunRow(**_model_run_values(model_run)))
        await self._session.flush()
        stored = await self.get_by_idempotency_key(
            proposal.subject_id, proposal.actor_id, proposal.idempotency_key
        )
        if stored is None:
            raise RuntimeError("editorial_proposal_insert_missing")
        return stored, True

    async def count_patch_references(self, blob_id: UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(EditorialProposalRow)
            .where(EditorialProposalRow.patch_blob_ref == blob_id)
        )
        return int(count or 0)


def _proposal_values(proposal: EditorialProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "subject_id": proposal.subject_id,
        "production_run_id": proposal.production_run_id,
        "origin": proposal.origin.value,
        "snapshot_id": proposal.snapshot_id,
        "occurrence_id": proposal.occurrence_id,
        "base_artifact_id": proposal.base_artifact_id,
        "base_artifact_version": proposal.base_artifact_version,
        "base_input_hash": proposal.base_input_hash,
        "base_canonical_sha256": proposal.base_canonical_sha256,
        "base_routing_hash": proposal.base_routing_hash,
        "base_pin_hash": proposal.base_pin_hash,
        "target_id": proposal.target_id,
        "operation": proposal.operation,
        "before_hash": proposal.before_hash,
        "patch_blob_ref": proposal.patch_blob_ref,
        "patch_sha256": proposal.patch_sha256,
        "evidence_refs": list(proposal.evidence_refs),
        "diagnostics": proposal.diagnostics,
        "thread_id": proposal.thread_id,
        "variant_group_id": proposal.variant_group_id,
        "state": proposal.state.value,
        "state_version": proposal.state_version,
        "actor_id": proposal.actor_id,
        "idempotency_key": proposal.idempotency_key,
        "request_sha256": proposal.request_sha256,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
    }


def _event_values(event: EditorialProposalEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "proposal_id": event.proposal_id,
        "sequence": event.sequence,
        "kind": event.kind.value,
        "state": event.state.value,
        "state_version": event.state_version,
        "actor_id": event.actor_id,
        "idempotency_key": event.idempotency_key,
        "request_sha256": event.request_sha256,
        "patch_sha256": event.patch_sha256,
        "diagnostics": event.diagnostics,
        "created_at": event.created_at,
    }


def _model_run_values(link: EditorialProposalModelRun) -> dict[str, object]:
    return {
        "proposal_id": link.proposal_id,
        "thread_id": link.thread_id,
        "model_run_id": link.model_run_id,
        "model_turn_id": link.model_turn_id,
        "created_at": link.created_at,
    }


def _proposal_from_rows(
    row: EditorialProposalRow, link: EditorialProposalModelRunRow | None
) -> EditorialProposal:
    evidence = row.evidence_refs
    diagnostics = row.diagnostics
    if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
        raise RuntimeError("editorial_proposal_evidence_shape_invalid")
    if not isinstance(diagnostics, dict):
        raise RuntimeError("editorial_proposal_diagnostics_shape_invalid")
    return EditorialProposal(
        id=row.id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        origin=EditorialProposalOrigin(row.origin),
        snapshot_id=row.snapshot_id,
        occurrence_id=row.occurrence_id,
        base_artifact_id=row.base_artifact_id,
        base_artifact_version=row.base_artifact_version,
        base_input_hash=row.base_input_hash,
        base_canonical_sha256=row.base_canonical_sha256,
        base_routing_hash=row.base_routing_hash,
        base_pin_hash=row.base_pin_hash,
        target_id=row.target_id,
        operation=row.operation,
        before_hash=row.before_hash,
        patch_blob_ref=row.patch_blob_ref,
        patch_sha256=row.patch_sha256,
        evidence_refs=tuple(evidence),
        diagnostics=dict(diagnostics),
        thread_id=row.thread_id,
        variant_group_id=row.variant_group_id,
        model_run_id=link.model_run_id if link is not None else None,
        model_turn_id=link.model_turn_id if link is not None else None,
        state=EditorialProposalState(row.state),
        state_version=row.state_version,
        actor_id=row.actor_id,
        idempotency_key=row.idempotency_key,
        request_sha256=row.request_sha256,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
