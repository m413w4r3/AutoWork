"""Non-canonical Bridge note proposal rows (M24-08B, ADR-0011 D2/D4).

A row is one targeted Bridge drafting answer awaiting human acceptance.  Its
text and provenance are only filled from a persisted ``advisor.consult``
answer; a pending request carries no text.  Acceptance appends a canonical
opinion version and records the human as the accepter, never as the author.
Every upstream reference is ``RESTRICT`` so nothing canonical detaches.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

NOTE_PROPOSAL_STATUS_VALUES_SQL = "'proposed', 'accepted', 'rejected', 'stale', 'cancelled'"
NOTE_PROPOSAL_TARGET_BLOCK_VALUES_SQL = "'lead', 'section'"
NOTE_PROPOSAL_BRIDGE_BLOCK_VALUES_SQL = (
    "'key_findings', 'additional_samples', 'infection_chain_and_capabilities', "
    "'network_and_infrastructure', 'claim_verification', 'proposed_detections', "
    "'uncertainties_and_next_leads'"
)
NOTE_PROPOSAL_CONSULTATION_VALUES_SQL = "'draft_text', 'review_text'"
NOTE_PROPOSAL_STATE_VALUES_SQL = "'observed', 'inferred', 'unresolved'"
NOTE_PROPOSAL_RELATION_VALUES_SQL = "'confirms', 'refutes', 'qualifies', 'unresolved'"


def _sha256_sql(column: str) -> str:
    return f"char_length({column}) = 64 AND {column} ~ '^[0-9a-f]{{64}}$'"


class InvestigationNoteProposalRow(Base):
    __tablename__ = "investigation_note_proposals"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_investigation_note_proposals"),
        UniqueConstraint(
            "investigation_id",
            "idempotency_key",
            name="uq_investigation_note_proposals_investigation_idempotency",
        ),
        ForeignKeyConstraint(
            ["investigation_id"],
            ["analyst_investigations.id"],
            name="fk_investigation_note_proposals_investigation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["task_id", "investigation_id"],
            ["investigation_tasks.id", "investigation_tasks.investigation_id"],
            name="fk_investigation_note_proposals_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["bridge_response_observation_id"],
            ["evidence_observations.id"],
            name="fk_investigation_note_proposals_response_observation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tool_invocation_id"],
            ["tool_invocations.id"],
            name="fk_investigation_note_proposals_tool_invocation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["analyst_decision_id"],
            ["analyst_decisions.id"],
            name="fk_investigation_note_proposals_analyst_decision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            f"status IN ({NOTE_PROPOSAL_STATUS_VALUES_SQL})",
            name="ck_investigation_note_proposals_status",
        ),
        CheckConstraint(
            f"target_block_type IN ({NOTE_PROPOSAL_TARGET_BLOCK_VALUES_SQL})",
            name="ck_investigation_note_proposals_target_block",
        ),
        CheckConstraint(
            f"bridge_block_type IN ({NOTE_PROPOSAL_BRIDGE_BLOCK_VALUES_SQL})",
            name="ck_investigation_note_proposals_bridge_block",
        ),
        CheckConstraint(
            f"consultation_type IN ({NOTE_PROPOSAL_CONSULTATION_VALUES_SQL})",
            name="ck_investigation_note_proposals_consultation",
        ),
        CheckConstraint(
            f"note_state IN ({NOTE_PROPOSAL_STATE_VALUES_SQL})",
            name="ck_investigation_note_proposals_note_state",
        ),
        CheckConstraint(
            f"note_relation IN ({NOTE_PROPOSAL_RELATION_VALUES_SQL})",
            name="ck_investigation_note_proposals_note_relation",
        ),
        CheckConstraint(
            "(target_block_type = 'lead' AND anchor ~ '^lead:[0-9]{4}$') OR "
            "(target_block_type = 'section' AND "
            "anchor ~ '^section:[0-9]+:paragraph:[0-9]{4}$')",
            name="ck_investigation_note_proposals_anchor",
        ),
        CheckConstraint(
            "jsonb_typeof(cited_finding_refs) = 'array' AND "
            "jsonb_typeof(cited_observation_refs) = 'array'",
            name="ck_investigation_note_proposals_citations_shape",
        ),
        CheckConstraint(
            "note_state = 'unresolved' OR jsonb_array_length(cited_finding_refs) + "
            "jsonb_array_length(cited_observation_refs) > 0",
            name="ck_investigation_note_proposals_sourced_note",
        ),
        CheckConstraint(
            "(consultation_type = 'review_text') = (base_note_id IS NOT NULL)",
            name="ck_investigation_note_proposals_base_note",
        ),
        CheckConstraint(
            "(proposed_text IS NULL AND proposed_text_sha256 IS NULL AND "
            "bridge_invocation_id IS NULL AND bridge_output_sha256 IS NULL AND "
            "bridge_response_observation_id IS NULL) OR "
            "(proposed_text IS NOT NULL AND proposed_text_sha256 IS NOT NULL AND "
            "bridge_invocation_id IS NOT NULL AND bridge_output_sha256 IS NOT NULL AND "
            "bridge_response_observation_id IS NOT NULL)",
            name="ck_investigation_note_proposals_provenance_complete",
        ),
        CheckConstraint(
            "proposed_text IS NULL OR btrim(proposed_text) <> ''",
            name="ck_investigation_note_proposals_text_non_empty",
        ),
        CheckConstraint(
            f"proposed_text_sha256 IS NULL OR ({_sha256_sql('proposed_text_sha256')})",
            name="ck_investigation_note_proposals_text_sha256",
        ),
        CheckConstraint(
            f"bridge_output_sha256 IS NULL OR ({_sha256_sql('bridge_output_sha256')})",
            name="ck_investigation_note_proposals_output_sha256",
        ),
        CheckConstraint(
            "bridge_invocation_id IS NULL OR btrim(bridge_invocation_id) <> ''",
            name="ck_investigation_note_proposals_bridge_invocation",
        ),
        CheckConstraint(
            "(base_opinion_version IS NULL) = (base_opinion_sha256 IS NULL)",
            name="ck_investigation_note_proposals_base_pair",
        ),
        CheckConstraint(
            "base_opinion_version IS NULL OR base_opinion_version >= 1",
            name="ck_investigation_note_proposals_base_version",
        ),
        CheckConstraint(
            f"base_opinion_sha256 IS NULL OR ({_sha256_sql('base_opinion_sha256')})",
            name="ck_investigation_note_proposals_base_sha256",
        ),
        CheckConstraint(
            "status_version >= 1",
            name="ck_investigation_note_proposals_status_version",
        ),
        CheckConstraint(
            "(status = 'accepted') = "
            "(accepted_opinion_version IS NOT NULL AND analyst_decision_id IS NOT NULL)",
            name="ck_investigation_note_proposals_accepted",
        ),
        CheckConstraint(
            "accepted_opinion_version IS NULL OR accepted_opinion_version >= 1",
            name="ck_investigation_note_proposals_accepted_version",
        ),
        CheckConstraint(
            "status <> 'accepted' OR bridge_response_observation_id IS NOT NULL",
            name="ck_investigation_note_proposals_accepted_provenance",
        ),
        Index(
            "ix_investigation_note_proposals_investigation_created",
            "investigation_id",
            "created_at",
            "id",
        ),
        Index(
            "ix_investigation_note_proposals_investigation_status",
            "investigation_id",
            "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    investigation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    opinion_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    anchor: Mapped[str] = mapped_column(Text, nullable=False)
    target_block_type: Mapped[str] = mapped_column(String(16), nullable=False)
    bridge_block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    consultation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    base_note_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    note_state: Mapped[str] = mapped_column(String(16), nullable=False)
    note_relation: Mapped[str] = mapped_column(String(16), nullable=False)
    cited_finding_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    cited_observation_refs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    proposed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bridge_invocation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    bridge_output_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bridge_response_observation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    tool_invocation_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    base_opinion_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    base_opinion_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_opinion_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    analyst_decision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
