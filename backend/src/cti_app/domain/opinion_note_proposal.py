"""Non-canonical Bridge note proposals (ADR-0011 D2/D4, M24-08B).

A proposal is a targeted Bridge drafting answer kept beside the canonical
analyst opinion until a human accepts it by explicit version and hash.  The
stored text and its provenance columns are only ever filled from a persisted
``advisor.consult`` answer; a pending request carries no text and can never be
accepted.  The module is pure: it validates the shape of a proposal and the
anchor space (``lead``/``section``), it never writes anything.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid5

from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystOpinionV1,
    OpinionBlockType,
    investigation_opinion_id,
)
from cti_app.domain.editorial import AnalystDecision

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEAD_ANCHOR = re.compile(r"^lead:[0-9]{4}$")
_SECTION_ANCHOR = re.compile(r"^section:[0-9]+:paragraph:[0-9]{4}$")

_NOTE_PROPOSAL_NOTE_NAMESPACE = UUID("1b26d9b0-8e1a-5b16-9f9d-2f9a1e4c7d63")

PROSE_BLOCK_TYPES = tuple(
    item
    for item in OpinionBlockType
    if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
)


class NoteProposalStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"
    CANCELLED = "cancelled"


class NoteTargetBlockType(StrEnum):
    """The anchor space a note lives in: article lead or article section."""

    LEAD = "lead"
    SECTION = "section"


class NoteProposalConsultationType(StrEnum):
    DRAFT_TEXT = "draft_text"
    REVIEW_TEXT = "review_text"


_TERMINAL_STATUSES = frozenset(
    {
        NoteProposalStatus.ACCEPTED,
        NoteProposalStatus.REJECTED,
        NoteProposalStatus.STALE,
        NoteProposalStatus.CANCELLED,
    }
)


def anchor_target_block_type(anchor: str) -> NoteTargetBlockType:
    """Classify one note anchor; reject anything outside the two article spaces."""
    if not isinstance(anchor, str):
        raise ValueError("A note proposal anchor must be text")
    if _LEAD_ANCHOR.fullmatch(anchor) is not None:
        return NoteTargetBlockType.LEAD
    if _SECTION_ANCHOR.fullmatch(anchor) is not None:
        return NoteTargetBlockType.SECTION
    raise ValueError("A note proposal anchor must be 'lead:NNNN' or 'section:I:paragraph:NNNN'")


def note_proposal_note_id(proposal_id: UUID) -> UUID:
    """Stable identity of the note an accepted proposal creates."""
    if not isinstance(proposal_id, UUID):
        raise TypeError("A note proposal identity must be a UUID")
    return uuid5(_NOTE_PROPOSAL_NOTE_NAMESPACE, str(proposal_id))


def note_draft_text_from_sentences(sentence_texts: Sequence[str]) -> str:
    """Join Bridge sentence texts without flattening stored paragraph breaks.

    Sentence texts are stored verbatim.  Ordinary sentences keep the historical
    single-space join (so existing note texts and their SHA-256 do not change),
    but a boundary that already carries a newline is joined as-is: a stored
    paragraph break (``\\n\\n``) or a list line therefore survives byte for byte.
    This exact function materialises the proposal text and recomputes the
    acceptance provenance hash.
    """
    if not sentence_texts:
        return ""
    result = sentence_texts[0]
    for text in sentence_texts[1:]:
        if not result.endswith(("\n", "\r")) and not text.startswith(("\n", "\r")):
            result += " "
        result += text
    return result


def _validate_sha256(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class NoteProposalV1:
    """One targeted Bridge note proposal, canonical only after human acceptance."""

    investigation_id: UUID
    opinion_id: UUID
    anchor: str
    target_block_type: NoteTargetBlockType
    bridge_block_type: OpinionBlockType
    consultation_type: NoteProposalConsultationType
    note_state: AnalystObservationState
    note_relation: AnalystObservationRelation
    idempotency_key: str
    id: UUID
    status: NoteProposalStatus = NoteProposalStatus.PROPOSED
    status_version: int = 1
    task_id: UUID | None = None
    base_note_id: UUID | None = None
    cited_finding_refs: tuple[UUID, ...] = ()
    cited_observation_refs: tuple[UUID, ...] = ()
    proposed_text: str | None = None
    proposed_text_sha256: str | None = None
    bridge_invocation_id: str | None = None
    bridge_output_sha256: str | None = None
    bridge_response_observation_id: UUID | None = None
    tool_invocation_id: UUID | None = None
    base_opinion_version: int | None = None
    base_opinion_sha256: str | None = None
    base_publication_artifact_id: UUID | None = None
    base_publication_version: int | None = None
    base_publication_sha256: str | None = None
    anchor_before_sha256: str | None = None
    accepted_opinion_version: int | None = None
    analyst_decision_id: UUID | None = None
    created_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or not isinstance(self.investigation_id, UUID):
            raise ValueError("Note proposal identities must be UUIDs")
        if self.opinion_id != investigation_opinion_id(self.investigation_id):
            raise ValueError("Note proposal opinion identity does not match its investigation")
        if self.task_id is not None and not isinstance(self.task_id, UUID):
            raise TypeError("Note proposal task identity must be a UUID")
        if self.base_note_id is not None and not isinstance(self.base_note_id, UUID):
            raise TypeError("Note proposal base note identity must be a UUID")
        expected_target = anchor_target_block_type(self.anchor)
        if self.target_block_type is not expected_target:
            raise ValueError("Note proposal target block type does not match its anchor")
        if not isinstance(self.bridge_block_type, OpinionBlockType) or (
            self.bridge_block_type not in PROSE_BLOCK_TYPES
        ):
            raise ValueError("Note proposal block type must be one publishable opinion block")
        if not isinstance(self.consultation_type, NoteProposalConsultationType):
            raise TypeError("Note proposal consultation type is invalid")
        if self.consultation_type is NoteProposalConsultationType.REVIEW_TEXT:
            if self.base_note_id is None:
                raise ValueError("A review proposal requires the reviewed note identity")
        elif self.base_note_id is not None:
            raise ValueError("A draft proposal does not carry a reviewed note identity")
        if not isinstance(self.note_state, AnalystObservationState):
            raise TypeError("Note proposal state must use AnalystObservationState")
        if not isinstance(self.note_relation, AnalystObservationRelation):
            raise TypeError("Note proposal relation must use AnalystObservationRelation")
        if not isinstance(self.status, NoteProposalStatus):
            raise TypeError("Note proposal status must use NoteProposalStatus")
        if (
            isinstance(self.status_version, bool)
            or not isinstance(self.status_version, int)
            or self.status_version < 1
        ):
            raise ValueError("Note proposal status version must be >= 1")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ValueError("Note proposal idempotency key must be non-empty")
        object.__setattr__(self, "cited_finding_refs", tuple(self.cited_finding_refs))
        object.__setattr__(self, "cited_observation_refs", tuple(self.cited_observation_refs))
        if any(not isinstance(item, UUID) for item in self.cited_finding_refs) or any(
            not isinstance(item, UUID) for item in self.cited_observation_refs
        ):
            raise ValueError("Note proposal citations must be UUIDs")
        if len(set(self.cited_finding_refs)) != len(self.cited_finding_refs) or len(
            set(self.cited_observation_refs)
        ) != len(self.cited_observation_refs):
            raise ValueError("Note proposal citations must be unique")
        if self.note_state is not AnalystObservationState.UNRESOLVED and not (
            self.cited_finding_refs or self.cited_observation_refs
        ):
            raise ValueError("A sourced note proposal requires at least one citation")

        text_fields = (
            self.proposed_text,
            self.proposed_text_sha256,
            self.bridge_invocation_id,
            self.bridge_output_sha256,
            self.bridge_response_observation_id,
        )
        materialised = all(item is not None for item in text_fields)
        if any(item is not None for item in text_fields) and not materialised:
            raise ValueError(
                "A note proposal carries its stored Bridge text, hashes and response together"
            )
        if materialised:
            if not isinstance(self.proposed_text, str) or not self.proposed_text.strip():
                raise ValueError("A materialised note proposal requires non-empty Bridge text")
            expected_text_sha = hashlib.sha256(self.proposed_text.encode("utf-8")).hexdigest()
            if self.proposed_text_sha256 != expected_text_sha:
                raise ValueError("Note proposal text hash does not match its stored text")
            text_sha256 = self.proposed_text_sha256
            output_sha256 = self.bridge_output_sha256
            if text_sha256 is None or output_sha256 is None:
                raise ValueError("A materialised note proposal requires both hashes")
            _validate_sha256(text_sha256, "proposed_text_sha256")
            _validate_sha256(output_sha256, "bridge_output_sha256")
            if (
                not isinstance(self.bridge_invocation_id, str)
                or not self.bridge_invocation_id.strip()
                or len(self.bridge_invocation_id) > 128
            ):
                raise ValueError("Note proposal Bridge invocation identity is invalid")
            if not isinstance(self.bridge_response_observation_id, UUID):
                raise ValueError("Note proposal Bridge response observation must be a UUID")

        if self.tool_invocation_id is not None and not isinstance(self.tool_invocation_id, UUID):
            raise TypeError("Note proposal tool invocation identity must be a UUID")
        base_pair = (self.base_opinion_version, self.base_opinion_sha256)
        if (base_pair[0] is None) != (base_pair[1] is None):
            raise ValueError("Note proposal base opinion version and hash are a pair")
        if self.base_opinion_version is not None:
            if (
                isinstance(self.base_opinion_version, bool)
                or not isinstance(self.base_opinion_version, int)
                or self.base_opinion_version < 1
            ):
                raise ValueError("Note proposal base opinion version must be >= 1")
            base_sha256 = self.base_opinion_sha256
            if base_sha256 is None:
                raise ValueError("Note proposal base opinion hash is missing")
            _validate_sha256(base_sha256, "base_opinion_sha256")

        base_publication = (
            self.base_publication_artifact_id,
            self.base_publication_version,
            self.base_publication_sha256,
        )
        if any(item is not None for item in base_publication) and any(
            item is None for item in base_publication
        ):
            raise ValueError(
                "Note proposal base publication identity, version and hash are complete together"
            )
        if self.base_publication_artifact_id is not None:
            if not isinstance(self.base_publication_artifact_id, UUID):
                raise ValueError("Note proposal base publication identity must be a UUID")
            if (
                isinstance(self.base_publication_version, bool)
                or not isinstance(self.base_publication_version, int)
                or self.base_publication_version < 1
            ):
                raise ValueError("Note proposal base publication version must be >= 1")
            base_publication_sha256 = self.base_publication_sha256
            if base_publication_sha256 is None:
                raise ValueError("Note proposal base publication hash is missing")
            _validate_sha256(base_publication_sha256, "base_publication_sha256")
        if (self.base_publication_artifact_id is None) != (self.anchor_before_sha256 is None):
            raise ValueError(
                "Note proposal anchor text identity requires its pinned publication base"
            )
        if self.anchor_before_sha256 is not None:
            _validate_sha256(self.anchor_before_sha256, "anchor_before_sha256")

        accepted_fields = (self.accepted_opinion_version, self.analyst_decision_id)
        is_accepted = self.status is NoteProposalStatus.ACCEPTED
        if is_accepted and not materialised:
            raise ValueError("An accepted note proposal requires its stored Bridge text")
        if is_accepted != all(item is not None for item in accepted_fields):
            raise ValueError(
                "An accepted note proposal carries its accepted version and decision together"
            )
        if self.accepted_opinion_version is not None and (
            not isinstance(self.accepted_opinion_version, int)
            or isinstance(self.accepted_opinion_version, bool)
            or self.accepted_opinion_version < 1
        ):
            raise ValueError("Accepted opinion version must be >= 1")
        if self.analyst_decision_id is not None and not isinstance(self.analyst_decision_id, UUID):
            raise ValueError("An accepted note proposal decision must be a UUID")
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise TypeError(f"Note proposal {name} must be a datetime")
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"Note proposal {name} must be timezone-aware")

    @property
    def materialised(self) -> bool:
        return self.proposed_text is not None and self.bridge_response_observation_id is not None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def with_status(
        self,
        status: NoteProposalStatus,
        *,
        accepted_opinion_version: int | None = None,
        analyst_decision_id: UUID | None = None,
        now: datetime | None = None,
    ) -> NoteProposalV1:
        """Return the next CAS version of this proposal (status_version + 1)."""
        if status is not NoteProposalStatus.ACCEPTED and (
            accepted_opinion_version is not None or analyst_decision_id is not None
        ):
            raise ValueError("Only an acceptance records an opinion version and decision")
        return NoteProposalV1(
            id=self.id,
            investigation_id=self.investigation_id,
            opinion_id=self.opinion_id,
            anchor=self.anchor,
            target_block_type=self.target_block_type,
            bridge_block_type=self.bridge_block_type,
            consultation_type=self.consultation_type,
            note_state=self.note_state,
            note_relation=self.note_relation,
            idempotency_key=self.idempotency_key,
            status=status,
            status_version=self.status_version + 1,
            task_id=self.task_id,
            base_note_id=self.base_note_id,
            cited_finding_refs=self.cited_finding_refs,
            cited_observation_refs=self.cited_observation_refs,
            proposed_text=self.proposed_text,
            proposed_text_sha256=self.proposed_text_sha256,
            bridge_invocation_id=self.bridge_invocation_id,
            bridge_output_sha256=self.bridge_output_sha256,
            bridge_response_observation_id=self.bridge_response_observation_id,
            tool_invocation_id=self.tool_invocation_id,
            base_opinion_version=self.base_opinion_version,
            base_opinion_sha256=self.base_opinion_sha256,
            base_publication_artifact_id=self.base_publication_artifact_id,
            base_publication_version=self.base_publication_version,
            base_publication_sha256=self.base_publication_sha256,
            anchor_before_sha256=self.anchor_before_sha256,
            accepted_opinion_version=accepted_opinion_version,
            analyst_decision_id=analyst_decision_id,
            created_at=self.created_at,
            updated_at=now or datetime.now(UTC),
        )

    def with_materialisation(
        self,
        *,
        proposed_text: str,
        proposed_text_sha256: str,
        bridge_invocation_id: str,
        bridge_output_sha256: str,
        bridge_response_observation_id: UUID,
        now: datetime | None = None,
    ) -> NoteProposalV1:
        """Return the same proposal with its persisted Bridge answer filled in."""
        if self.materialised:
            if (
                self.proposed_text != proposed_text
                or self.proposed_text_sha256 != proposed_text_sha256
                or self.bridge_invocation_id != bridge_invocation_id
                or self.bridge_output_sha256 != bridge_output_sha256
                or self.bridge_response_observation_id != bridge_response_observation_id
            ):
                raise ValueError("A proposal cannot be materialised with different Bridge text")
            return self
        return replace(
            self,
            proposed_text=proposed_text,
            proposed_text_sha256=proposed_text_sha256,
            bridge_invocation_id=bridge_invocation_id,
            bridge_output_sha256=bridge_output_sha256,
            bridge_response_observation_id=bridge_response_observation_id,
            status_version=self.status_version + 1,
            updated_at=now or datetime.now(UTC),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "investigation_id": str(self.investigation_id),
            "opinion_id": str(self.opinion_id),
            "task_id": str(self.task_id) if self.task_id is not None else None,
            "anchor": self.anchor,
            "target_block_type": self.target_block_type.value,
            "bridge_block_type": self.bridge_block_type.value,
            "consultation_type": self.consultation_type.value,
            "base_note_id": str(self.base_note_id) if self.base_note_id is not None else None,
            "note_state": self.note_state.value,
            "note_relation": self.note_relation.value,
            "cited_finding_refs": [str(item) for item in self.cited_finding_refs],
            "cited_observation_refs": [str(item) for item in self.cited_observation_refs],
            "proposed_text": self.proposed_text,
            "proposed_text_sha256": self.proposed_text_sha256,
            "bridge_invocation_id": self.bridge_invocation_id,
            "bridge_output_sha256": self.bridge_output_sha256,
            "bridge_response_observation_id": (
                str(self.bridge_response_observation_id)
                if self.bridge_response_observation_id is not None
                else None
            ),
            "tool_invocation_id": (
                str(self.tool_invocation_id) if self.tool_invocation_id is not None else None
            ),
            "base_opinion_version": self.base_opinion_version,
            "base_opinion_sha256": self.base_opinion_sha256,
            "base_publication_artifact_id": (
                str(self.base_publication_artifact_id)
                if self.base_publication_artifact_id is not None
                else None
            ),
            "base_publication_version": self.base_publication_version,
            "base_publication_sha256": self.base_publication_sha256,
            "anchor_before_sha256": self.anchor_before_sha256,
            "status": self.status.value,
            "status_version": self.status_version,
            "accepted_opinion_version": self.accepted_opinion_version,
            "analyst_decision_id": (
                str(self.analyst_decision_id) if self.analyst_decision_id is not None else None
            ),
            "idempotency_key": self.idempotency_key,
            "materialised": self.materialised,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class NoteProposalAcceptanceV1:
    """One human acceptance: the canonical opinion version and its decision."""

    proposal: NoteProposalV1
    opinion: AnalystOpinionV1
    decision: AnalystDecision
    idempotent_replay: bool


__all__ = [
    "PROSE_BLOCK_TYPES",
    "NoteProposalAcceptanceV1",
    "NoteProposalConsultationType",
    "NoteProposalStatus",
    "NoteProposalV1",
    "NoteTargetBlockType",
    "anchor_target_block_type",
    "note_draft_text_from_sentences",
    "note_proposal_note_id",
]
