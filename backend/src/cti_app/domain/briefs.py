from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4


class BriefDraftStatus(StrEnum):
    DRAFT = "draft"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    PROMOTED = "promoted"


class EvidencePackScope(StrEnum):
    """Scope of evidence in a pack: full subject history or delta updates."""
    FULL = "full"
    DELTA = "delta"


class AmendmentKind(StrEnum):
    """Type of amendment to published content."""
    UPDATE = "update"
    CORRECTION = "correction"
    CLARIFICATION = "clarification"


class AmendmentStatus(StrEnum):
    """Lifecycle status of an amendment."""
    DRAFT = "draft"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    PUBLISHED = "published"


@dataclass(frozen=True, slots=True)
class BriefEvidencePack:
    subject_id: UUID
    edition_id: UUID
    group_id: UUID
    version: int
    content_hash: str
    object_hashes: tuple[str, ...]
    sources: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]
    indicators: tuple[dict[str, Any], ...]
    normalized_entities: tuple[dict[str, str], ...]
    uncertainties: tuple[dict[str, Any], ...]
    human_decisions: tuple[dict[str, Any], ...]
    blob_id: UUID
    # Incrément 3: préservation éditoriale
    built_from_snapshot_id: UUID
    built_from_snapshot_version: int
    covered_contribution_ids: tuple[UUID, ...]
    scope: EvidencePackScope = EvidencePackScope.FULL
    base_pack_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_by: str = "system"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.version < 1 or self.built_from_snapshot_version < 1:
            raise ValueError("Evidence pack and snapshot versions must be positive")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise ValueError("Evidence pack hash must be a lowercase SHA-256")
        if not self.created_by.strip():
            raise ValueError("Evidence pack creator is required")
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in self.object_hashes):
            raise ValueError("Every evidence object must have a SHA-256")
        if self.scope == EvidencePackScope.DELTA and self.base_pack_id is None:
            raise ValueError("A DELTA pack must reference its base pack")
        if self.scope == EvidencePackScope.FULL and self.base_pack_id is not None:
            raise ValueError("A FULL pack must not reference a base pack")


@dataclass(frozen=True, slots=True)
class BriefSentence:
    text: str
    factual: bool
    claim_ids: tuple[UUID, ...]
    indicator_ids: tuple[UUID, ...] = ()
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("A brief sentence cannot be empty")
        if self.factual and not self.claim_ids:
            raise ValueError("Every factual sentence must reference a claim")


@dataclass(frozen=True, slots=True)
class BriefBlock:
    sentences: tuple[BriefSentence, ...]
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.sentences:
            raise ValueError("A brief block cannot be empty")


@dataclass(frozen=True, slots=True)
class BriefDraft:
    subject_id: UUID
    edition_id: UUID
    group_id: UUID
    pack_id: UUID
    pack_hash: str
    version: int
    title: str
    blocks: tuple[BriefBlock, ...]
    limits: tuple[str, ...]
    source_ids: tuple[UUID, ...]
    model_run_id: UUID
    provider: str
    status: BriefDraftStatus = BriefDraftStatus.DRAFT
    parent_draft_id: UUID | None = None
    regenerated_block_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.version < 1 or not self.title.strip():
            raise ValueError("Brief title and positive version are required")
        if not 1 <= len(self.blocks) <= 3:
            raise ValueError("A brief must contain one to three paragraphs")
        if not re.fullmatch(r"[0-9a-f]{64}", self.pack_hash):
            raise ValueError("Draft evidence pack hash must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class BriefAmendment:
    """Amendment to a published brief: update, correction, or clarification (Incrément 3)."""
    subject_id: UUID
    edition_id: UUID
    parent_artifact_id: UUID  # The artifact being amended (can be another amendment)
    root_artifact_id: UUID  # The original artifact in the chain
    trigger_snapshot_id: UUID  # Snapshot that triggered the amendment
    kind: AmendmentKind
    status: AmendmentStatus
    evidence_pack_id: UUID  # Pack DELTA with only new contributions
    contribution_ids: tuple[UUID, ...]  # Contributions addressed by this amendment
    draft_id: UUID | None = None
    revision_reason: str | None = None  # For redactional revisions
    published_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.parent_artifact_id == self.root_artifact_id:
            # This is the first amendment to the original
            pass
        if self.status == AmendmentStatus.PUBLISHED and self.published_at is None:
            raise ValueError("A published amendment must have a published_at timestamp")
        if self.kind == AmendmentKind.CORRECTION and not self.revision_reason:
            raise ValueError("A correction amendment must have a revision reason")
        if not self.contribution_ids:
            raise ValueError("An amendment must address at least one contribution")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def object_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()
