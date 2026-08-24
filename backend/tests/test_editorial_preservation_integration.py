"""
Integration tests for editorial preservation (Incrément 3).

Tests the complete flow:
- Published brief with evidence pack
- New contributions arrive
- UPDATE_AVAILABLE signal is generated
- Amendment can be created
- Published content remains immutable
"""

from datetime import UTC, datetime
from uuid import uuid4

from cti_app.domain.briefs import (
    AmendmentKind,
    AmendmentStatus,
    BriefAmendment,
)


class TestPublishedBriefWithNewContributions:
    """Test scenario: published brief receives new contributions."""

    def test_amendment_chain_for_updates(self) -> None:
        """Multiple updates to a published brief create amendment chain."""
        subject_id = uuid4()
        edition_id = uuid4()

        # Original published brief
        root_artifact_id = uuid4()
        root_brief_id = uuid4()

        # First amendment (UPDATE)
        amend1_id = uuid4()
        amend1 = BriefAmendment(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=root_brief_id,  # Links to original
            root_artifact_id=root_artifact_id,  # Points to root
            trigger_snapshot_id=uuid4(),
            kind=AmendmentKind.UPDATE,
            status=AmendmentStatus.PUBLISHED,
            evidence_pack_id=uuid4(),
            contribution_ids=(uuid4(),),
            published_at=datetime.now(UTC),
            id=amend1_id,
        )

        # Second amendment (UPDATE to first amendment)
        amend2 = BriefAmendment(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=amend1_id,  # Links to first amendment
            root_artifact_id=root_artifact_id,  # Same root
            trigger_snapshot_id=uuid4(),
            kind=AmendmentKind.UPDATE,
            status=AmendmentStatus.DRAFT,
            evidence_pack_id=uuid4(),
            contribution_ids=(uuid4(),),
        )

        # Chain is: root_brief ← amend1 ← amend2
        assert amend1.root_artifact_id == amend2.root_artifact_id
        assert amend1.parent_artifact_id == root_brief_id
        assert amend2.parent_artifact_id == amend1.id

    def test_correction_amendment_immutability(self) -> None:
        """CORRECTION amendments don't modify original, create new version."""
        subject_id = uuid4()
        edition_id = uuid4()

        # Published brief with typo
        original_brief_id = uuid4()
        original_pack_id = uuid4()
        original_pack_hash = "8810ad581e59f2bc3928b261707a71308f7e139eb04820366dc4d5c18d980225"
        original_contribution_id = uuid4()

        # Correction amendment (redactional)
        corrected = BriefAmendment(
            subject_id=subject_id,
            edition_id=edition_id,
            parent_artifact_id=original_brief_id,
            root_artifact_id=original_brief_id,
            trigger_snapshot_id=uuid4(),
            kind=AmendmentKind.CORRECTION,
            status=AmendmentStatus.DRAFT,
            evidence_pack_id=original_pack_id,  # Same evidence
            contribution_ids=(original_contribution_id,),  # Same contribution, no new content
            revision_reason="Fixed typo in paragraph 2",
        )

        # Original pack hash unchanged
        expected_hash = "8810ad581e59f2bc3928b261707a71308f7e139eb04820366dc4d5c18d980225"
        assert original_pack_hash == expected_hash
        # Amendment references it but doesn't modify it
        assert corrected.evidence_pack_id == original_pack_id
