"""Persistent, non-canonical editorial proposal identities and transitions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EditorialProposalError(ValueError):
    """A proposal request or transition violates its closed contract."""

    code = "proposal_patch_invalid"


class EditorialProposalConflict(EditorialProposalError):
    code = "proposal_idempotency_conflict"


class EditorialProposalState(StrEnum):
    PREPARING = "preparing"
    PROPOSED = "proposed"
    REFUSED = "refused"
    NEEDS_REVIEW = "needs_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


class EditorialProposalOrigin(StrEnum):
    BRIDGE_DRAFT = "bridge_draft"
    MANUAL_PATCH = "manual_patch"


@dataclass(frozen=True, slots=True)
class ManualTextPatch:
    """A closed manual text operation accepted by the editorial workshop."""

    operation: TargetedEditorialRevisionOperation
    text: str

    def __post_init__(self) -> None:
        if self.operation not in {
            TargetedEditorialRevisionOperation.REWRITE_TITLE,
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
        }:
            raise EditorialProposalError("proposal_operation_unsupported")
        if not isinstance(self.text, str):
            raise EditorialProposalError("proposal_patch_invalid")


@dataclass(frozen=True, slots=True)
class ManualPatch:
    """The persisted proposal origin for one validated typed manual patch."""

    typed_patch: ManualTextPatch


class EditorialProposalEventKind(StrEnum):
    PREPARED = "prepared"
    REFUSED = "refused"
    NEEDS_REVIEW = "needs_review"
    STATE_CHANGED = "state_changed"


@dataclass(frozen=True, slots=True)
class EditorialProposal:
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    origin: EditorialProposalOrigin
    snapshot_id: UUID
    occurrence_id: UUID
    base_artifact_id: UUID
    base_artifact_version: int
    base_input_hash: str
    base_canonical_sha256: str
    base_routing_hash: str | None
    base_pin_hash: str
    target_id: str
    operation: str
    before_hash: str
    patch_blob_ref: UUID | None
    patch_sha256: str | None
    evidence_refs: tuple[dict[str, object], ...]
    diagnostics: dict[str, object]
    thread_id: UUID | None
    variant_group_id: UUID
    model_run_id: UUID | None
    model_turn_id: UUID | None
    state: EditorialProposalState
    state_version: int
    actor_id: UUID
    idempotency_key: str
    request_sha256: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decision_id: UUID | None = None
    accepted_artifact_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.base_artifact_version < 1 or self.state_version < 1:
            raise EditorialProposalError("proposal_version_invalid")
        for value in (
            self.base_input_hash,
            self.base_canonical_sha256,
            self.base_pin_hash,
            self.before_hash,
            self.request_sha256,
        ):
            if not _SHA256.fullmatch(value):
                raise EditorialProposalError("proposal_hash_invalid")
        if self.patch_sha256 is not None and not _SHA256.fullmatch(self.patch_sha256):
            raise EditorialProposalError("proposal_hash_invalid")
        if (self.patch_blob_ref is None) != (self.patch_sha256 is None):
            raise EditorialProposalError("proposal_patch_reference_invalid")
        if (self.model_run_id is None) != (self.model_turn_id is None):
            raise EditorialProposalError("proposal_model_run_reference_invalid")
        if self.operation not in {
            operation.value for operation in TargetedEditorialRevisionOperation
        }:
            raise EditorialProposalError("proposal_operation_invalid")
        if not self.target_id:
            raise EditorialProposalError("proposal_target_invalid")
        if (
            self.state
            in {
                EditorialProposalState.PROPOSED,
                EditorialProposalState.ACCEPTED,
                EditorialProposalState.REJECTED,
                EditorialProposalState.STALE,
            }
            and self.patch_blob_ref is None
        ):
            raise EditorialProposalError("proposal_patch_reference_invalid")
        if (
            self.state
            in {
                EditorialProposalState.REFUSED,
                EditorialProposalState.NEEDS_REVIEW,
            }
            and self.patch_blob_ref is not None
        ):
            raise EditorialProposalError("proposal_patch_reference_invalid")
        if self.origin is EditorialProposalOrigin.BRIDGE_DRAFT and (
            self.thread_id is None or self.model_run_id is None or self.model_turn_id is None
        ):
            raise EditorialProposalError("proposal_bridge_origin_invalid")

    def transition(
        self, state: EditorialProposalState, *, now: datetime | None = None
    ) -> EditorialProposal:
        allowed = {
            EditorialProposalState.PREPARING: {
                EditorialProposalState.PROPOSED,
                EditorialProposalState.REFUSED,
                EditorialProposalState.NEEDS_REVIEW,
            },
            EditorialProposalState.PROPOSED: {
                EditorialProposalState.ACCEPTED,
                EditorialProposalState.REJECTED,
                EditorialProposalState.STALE,
            },
        }
        if state not in allowed.get(self.state, set()):
            raise EditorialProposalError("proposal_state_conflict")
        return replace(
            self,
            state=state,
            state_version=self.state_version + 1,
            updated_at=now or datetime.now(UTC),
        )


@dataclass(frozen=True, slots=True)
class EditorialProposalEvent:
    id: UUID
    proposal_id: UUID
    sequence: int
    kind: EditorialProposalEventKind
    state: EditorialProposalState
    state_version: int
    actor_id: UUID
    idempotency_key: str
    request_sha256: str
    patch_sha256: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1 or self.state_version < 1 or not self.idempotency_key:
            raise EditorialProposalError("proposal_event_identity_invalid")
        if not _SHA256.fullmatch(self.request_sha256):
            raise EditorialProposalError("proposal_hash_invalid")
        if self.patch_sha256 is not None and not _SHA256.fullmatch(self.patch_sha256):
            raise EditorialProposalError("proposal_hash_invalid")
        expected_state = {
            EditorialProposalEventKind.PREPARED: EditorialProposalState.PROPOSED,
            EditorialProposalEventKind.REFUSED: EditorialProposalState.REFUSED,
            EditorialProposalEventKind.NEEDS_REVIEW: EditorialProposalState.NEEDS_REVIEW,
        }.get(self.kind)
        if expected_state is not None and self.state is not expected_state:
            raise EditorialProposalError("proposal_event_state_invalid")
        if self.kind is EditorialProposalEventKind.STATE_CHANGED and self.state not in {
            EditorialProposalState.ACCEPTED,
            EditorialProposalState.REJECTED,
            EditorialProposalState.STALE,
        }:
            raise EditorialProposalError("proposal_event_state_invalid")


@dataclass(frozen=True, slots=True)
class EditorialProposalModelRun:
    proposal_id: UUID
    thread_id: UUID
    model_run_id: UUID
    model_turn_id: UUID
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def variant_group_identity(
    *, base_pin_hash: str, occurrence_id: UUID, operation: str, thread_id: UUID | None
) -> UUID:
    """Return a stable variant identity for this pin, target, operation and origin scope."""
    payload = json.dumps(
        [base_pin_hash, str(occurrence_id), operation, str(thread_id)],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return uuid5(NAMESPACE_URL, f"autowork:editorial-proposal-variant:{digest}")
