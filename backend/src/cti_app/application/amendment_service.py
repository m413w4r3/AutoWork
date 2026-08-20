"""
Incrément 3: Amendment creation and delta pack management.

This module handles:
- Creating amendments (UPDATE, CORRECTION, CLARIFICATION)
- Building DELTA packs with only new contributions
- Linking amendment chains
- Maintaining amendment audit trail
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from cti_app.domain.briefs import (
    BriefAmendment,
    AmendmentStatus,
    AmendmentKind,
    EvidencePackScope,
    BriefEvidencePack,
)


@dataclass
class DeltaPackBuilder:
    """
    Builder for DELTA packs that cover only new contributions.

    A DELTA pack:
    - Covers only contributions added since parent pack
    - References the parent pack via base_pack_id
    - Shares the same structure as a FULL pack but with limited content
    - Ensures amendments don't restate the entire brief
    """

    parent_pack: BriefEvidencePack
    trigger_snapshot_id: UUID
    trigger_snapshot_version: int
    new_contribution_ids: set[UUID]
    covered_source_ids: list[UUID]
    covered_claim_ids: list[UUID]
    covered_indicator_ids: list[UUID]
    new_entities: list[dict[str, str]]
    new_uncertainties: list[dict[str, object]]

    def build(self) -> BriefEvidencePack:
        """
        Build a DELTA evidence pack.

        Returns:
            BriefEvidencePack with scope=DELTA and base_pack_id set
        """
        # For DELTA packs, we only include new contributions
        # This is a simplified view - in practice, you'd filter all content by new_contribution_ids
        import hashlib
        import json

        # Calculate content hash from new materials
        delta_content = {
            "sources": self.covered_source_ids,
            "claims": self.covered_claim_ids,
            "indicators": self.covered_indicator_ids,
            "entities": self.new_entities,
            "uncertainties": self.new_uncertainties,
        }
        content_json = json.dumps(delta_content, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(content_json.encode()).hexdigest()

        return BriefEvidencePack(
            subject_id=self.parent_pack.subject_id,
            edition_id=self.parent_pack.edition_id,
            group_id=self.parent_pack.group_id,
            version=self.parent_pack.version + 1,
            content_hash=content_hash,
            object_hashes=(),  # Simplified - would be computed from actual objects
            sources=(),
            claims=(),
            indicators=(),
            normalized_entities=tuple(self.new_entities),
            uncertainties=tuple(self.new_uncertainties),
            human_decisions=(),
            blob_id=UUID("00000000-0000-0000-0000-000000000000"),  # Placeholder
            built_from_snapshot_id=self.trigger_snapshot_id,
            built_from_snapshot_version=self.trigger_snapshot_version,
            covered_contribution_ids=tuple(self.new_contribution_ids),
            scope=EvidencePackScope.DELTA,
            base_pack_id=self.parent_pack.id,
            created_by="system",
        )


class AmendmentService:
    """
    Service for creating and managing amendments to published content.

    Key principles (from D15, D17, Incrément 3):
    - Published content is immutable; amendments reference it explicitly
    - Amendments are intra-edition (V1)
    - An amendment's pack is DELTA, not full restatement
    - Amendments are created in DRAFT, follow standard approval workflow
    """

    async def create_amendment_draft(
        self,
        subject_id: UUID,
        edition_id: UUID,
        parent_artifact_id: UUID,
        root_artifact_id: UUID,
        kind: AmendmentKind,
        trigger_snapshot_id: UUID,
        trigger_snapshot_version: int,
        new_contribution_ids: set[UUID],
        parent_pack: BriefEvidencePack,
        revision_reason: str | None = None,
    ) -> BriefAmendment:
        """
        Create a new amendment in DRAFT status.

        An amendment:
        - References the artifact it amends (parent_artifact_id)
        - Maintains a link to the root original artifact
        - Uses a DELTA pack with only new contributions
        - Never modifies the original content

        Args:
            subject_id: The discovery subject
            edition_id: The edition
            parent_artifact_id: The artifact being amended (can be another amendment)
            root_artifact_id: The original artifact in the chain
            kind: Type of amendment (UPDATE, CORRECTION, CLARIFICATION)
            trigger_snapshot_id: Snapshot that triggered the amendment
            trigger_snapshot_version: Version of that snapshot
            new_contribution_ids: Contributions this amendment addresses
            parent_pack: The pack from the parent artifact
            revision_reason: Required for CORRECTION kind

        Returns:
            BriefAmendment in DRAFT status
        """
        if kind == AmendmentKind.CORRECTION and not revision_reason:
            raise ValueError("CORRECTION amendments require a revision reason")

        # Build DELTA pack
        builder = DeltaPackBuilder(
            parent_pack=parent_pack,
            trigger_snapshot_id=trigger_snapshot_id,
            trigger_snapshot_version=trigger_snapshot_version,
            new_contribution_ids=new_contribution_ids,
            covered_source_ids=[],  # Would be populated from actual data
            covered_claim_ids=[],
            covered_indicator_ids=[],
            new_entities=[],
            new_uncertainties=[],
        )
        delta_pack = builder.build()

        # Create amendment in DRAFT status
        amendment = BriefAmendment(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=parent_artifact_id,
            root_artifact_id=root_artifact_id,
            trigger_snapshot_id=trigger_snapshot_id,
            kind=kind,
            status=AmendmentStatus.DRAFT,
            evidence_pack_id=delta_pack.id,
            contribution_ids=tuple(new_contribution_ids),
            revision_reason=revision_reason,
        )

        return amendment

    def create_redactional_revision(
        self,
        amendment: BriefAmendment,
        revision_reason: str,
    ) -> BriefAmendment:
        """
        Create a new version for redactional (non-semantic) changes.

        Per D15/17.1: published content is never edited in place.
        A redactional correction creates a new version with revision_reason,
        the previous version remaining accessible.

        Args:
            amendment: The amendment to create a revision for
            revision_reason: Description of redactional changes

        Returns:
            New BriefAmendment with same content but new ID and revision_reason
        """
        if amendment.status != AmendmentStatus.PUBLISHED:
            raise ValueError("Only published amendments can be redactionally revised")

        revised = BriefAmendment(
            subject_id=amendment.subject_id,
            edition_id=amendment.edition_id,
            parent_artifact_id=amendment.id,  # Link to previous version
            root_artifact_id=amendment.root_artifact_id,
            trigger_snapshot_id=amendment.trigger_snapshot_id,
            kind=amendment.kind,
            status=AmendmentStatus.DRAFT,
            evidence_pack_id=amendment.evidence_pack_id,  # Same pack
            contribution_ids=amendment.contribution_ids,
            revision_reason=revision_reason,
        )

        return revised
