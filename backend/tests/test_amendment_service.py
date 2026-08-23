"""
Tests for amendment service - creating amendments and delta packs.

Incrément 3: Préservation éditoriale
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.amendment_service import AmendmentService, DeltaPackBuilder
from cti_app.domain.briefs import (
    AmendmentKind,
    AmendmentStatus,
    BriefAmendment,
    BriefEvidencePack,
    EvidencePackScope,
)


@pytest.fixture
def base_pack() -> BriefEvidencePack:
    """A sample FULL evidence pack to amend."""
    return BriefEvidencePack(
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        version=1,
        content_hash="a" * 64,
        object_hashes=("b" * 64, "c" * 64),
        sources=(),
        claims=(),
        indicators=(),
        normalized_entities=(),
        uncertainties=(),
        human_decisions=(),
        blob_id=uuid4(),
        built_from_snapshot_id=uuid4(),
        built_from_snapshot_version=1,
        covered_contribution_ids=(uuid4(), uuid4()),
        scope=EvidencePackScope.FULL,
    )


class TestDeltaPackBuilder:
    """Test DELTA pack construction."""

    def test_delta_pack_has_correct_scope(self, base_pack: BriefEvidencePack) -> None:
        """A DELTA pack must have scope=DELTA and reference base pack."""
        builder = DeltaPackBuilder(
            parent_pack=base_pack,
            trigger_snapshot_id=uuid4(),
            trigger_snapshot_version=2,
            new_contribution_ids={uuid4()},
            covered_source_ids=[],
            covered_claim_ids=[],
            covered_indicator_ids=[],
            new_entities=[],
            new_uncertainties=[],
        )

        delta_pack = builder.build()

        assert delta_pack.scope == EvidencePackScope.DELTA
        assert delta_pack.base_pack_id == base_pack.id
        assert delta_pack.built_from_snapshot_version == 2

    def test_delta_pack_cannot_have_full_scope_with_base(
        self, base_pack: BriefEvidencePack
    ) -> None:
        """A FULL pack must not reference a base pack."""
        full_pack_params: dict[str, Any] = {
            "subject_id": base_pack.subject_id,
            "edition_id": base_pack.edition_id,
            "group_id": base_pack.group_id,
            "version": 2,
            "content_hash": "d" * 64,
            "object_hashes": (),
            "sources": (),
            "claims": (),
            "indicators": (),
            "normalized_entities": (),
            "uncertainties": (),
            "human_decisions": (),
            "blob_id": uuid4(),
            "built_from_snapshot_id": uuid4(),
            "built_from_snapshot_version": 2,
            "covered_contribution_ids": (),
            "scope": EvidencePackScope.FULL,
            "base_pack_id": base_pack.id,  # Invalid for FULL
        }

        with pytest.raises(ValueError, match="FULL pack must not reference"):
            BriefEvidencePack(**full_pack_params)

    def test_delta_pack_requires_base_pack(self, base_pack: BriefEvidencePack) -> None:
        """A DELTA pack must reference a base pack."""
        delta_pack_params: dict[str, Any] = {
            "subject_id": base_pack.subject_id,
            "edition_id": base_pack.edition_id,
            "group_id": base_pack.group_id,
            "version": 2,
            "content_hash": "d" * 64,
            "object_hashes": (),
            "sources": (),
            "claims": (),
            "indicators": (),
            "normalized_entities": (),
            "uncertainties": (),
            "human_decisions": (),
            "blob_id": uuid4(),
            "built_from_snapshot_id": uuid4(),
            "built_from_snapshot_version": 2,
            "covered_contribution_ids": (),
            "scope": EvidencePackScope.DELTA,
            "base_pack_id": None,  # Invalid for DELTA
        }

        with pytest.raises(ValueError, match="DELTA pack must reference"):
            BriefEvidencePack(**delta_pack_params)


class TestAmendmentService:
    """Test amendment creation and management."""

    @pytest.mark.asyncio
    async def test_create_update_amendment_draft(self, base_pack: BriefEvidencePack) -> None:
        """Create an UPDATE amendment in DRAFT status."""
        service = AmendmentService()
        subject_id = uuid4()
        edition_id = uuid4()
        root_artifact_id = uuid4()
        trigger_snapshot_id = uuid4()
        new_contrib_ids = {uuid4(), uuid4()}

        amendment = await service.create_amendment_draft(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=root_artifact_id,
            root_artifact_id=root_artifact_id,
            kind=AmendmentKind.UPDATE,
            trigger_snapshot_id=trigger_snapshot_id,
            trigger_snapshot_version=2,
            new_contribution_ids=new_contrib_ids,
            parent_pack=base_pack,
            revision_reason=None,
        )

        assert amendment.kind == AmendmentKind.UPDATE
        assert amendment.status == AmendmentStatus.DRAFT
        assert amendment.root_artifact_id == root_artifact_id
        assert amendment.contribution_ids == tuple(new_contrib_ids)

    @pytest.mark.asyncio
    async def test_correction_amendment_requires_reason(self, base_pack: BriefEvidencePack) -> None:
        """CORRECTION amendments must have revision_reason."""
        service = AmendmentService()

        with pytest.raises(ValueError, match="CORRECTION amendments require"):
            await service.create_amendment_draft(
                subject_id=uuid4(),
                edition_id=uuid4(),
                parent_artifact_id=uuid4(),
                root_artifact_id=uuid4(),
                kind=AmendmentKind.CORRECTION,
                trigger_snapshot_id=uuid4(),
                trigger_snapshot_version=2,
                new_contribution_ids={uuid4()},
                parent_pack=base_pack,
                revision_reason=None,  # Missing!
            )

    @pytest.mark.asyncio
    async def test_amendment_chain(self, base_pack: BriefEvidencePack) -> None:
        """Amendments can chain: amendment of amendment."""
        service = AmendmentService()
        edition_id = uuid4()
        subject_id = uuid4()
        root_artifact_id = uuid4()

        # First amendment
        amend1 = await service.create_amendment_draft(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=root_artifact_id,
            root_artifact_id=root_artifact_id,
            kind=AmendmentKind.UPDATE,
            trigger_snapshot_id=uuid4(),
            trigger_snapshot_version=2,
            new_contribution_ids={uuid4()},
            parent_pack=base_pack,
        )

        # Amendment of the amendment
        amend2 = await service.create_amendment_draft(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=amend1.id,  # Amend the first amendment
            root_artifact_id=root_artifact_id,  # Root stays the same
            kind=AmendmentKind.UPDATE,
            trigger_snapshot_id=uuid4(),
            trigger_snapshot_version=3,
            new_contribution_ids={uuid4()},
            parent_pack=base_pack,  # Would use amend1's pack in real code
        )

        assert amend2.parent_artifact_id == amend1.id
        assert amend2.root_artifact_id == root_artifact_id

    def test_redactional_revision(self, base_pack: BriefEvidencePack) -> None:
        """Create a redactional revision of a published amendment."""
        service = AmendmentService()

        # Create an amendment and mark it published
        published = BriefAmendment(
            subject_id=uuid4(),
            edition_id=uuid4(),
            parent_artifact_id=uuid4(),
            root_artifact_id=uuid4(),
            trigger_snapshot_id=uuid4(),
            kind=AmendmentKind.UPDATE,
            status=AmendmentStatus.PUBLISHED,
            evidence_pack_id=uuid4(),
            contribution_ids=(uuid4(),),
            published_at=datetime.now(UTC),
        )

        revised = service.create_redactional_revision(
            published,
            revision_reason="Fixed typo in third paragraph",
        )

        assert revised.status == AmendmentStatus.DRAFT
        assert revised.parent_artifact_id == published.id
        assert revised.revision_reason == "Fixed typo in third paragraph"
        assert revised.evidence_pack_id == published.evidence_pack_id

    def test_redactional_revision_only_for_published(self, base_pack: BriefEvidencePack) -> None:
        """Redactional revisions only work on published amendments."""
        service = AmendmentService()

        draft = BriefAmendment(
            subject_id=uuid4(),
            edition_id=uuid4(),
            parent_artifact_id=uuid4(),
            root_artifact_id=uuid4(),
            trigger_snapshot_id=uuid4(),
            kind=AmendmentKind.UPDATE,
            status=AmendmentStatus.DRAFT,  # Not published
            evidence_pack_id=uuid4(),
            contribution_ids=(uuid4(),),
        )

        with pytest.raises(ValueError, match="Only published amendments"):
            service.create_redactional_revision(draft, "Some change")
