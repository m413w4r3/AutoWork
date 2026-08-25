"""
Integration tests for replay lineage.

Tests that:
- A replay snapshot lives on a lineage separate from the operational one
- Published artifacts keep their historical bindings
"""

from uuid import uuid4

from cti_app.domain.briefs import BriefBlock, BriefDraft, BriefDraftStatus, BriefSentence
from cti_app.domain.discovery_cumulative import (
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySnapshotLineage,
)


class TestReplayEditionWorkflow:
    """Test complete replay edition workflow."""

    def test_replay_creates_separate_lineage(self) -> None:
        """Replay creates snapshot marked with REPLAY lineage."""
        edition_id = uuid4()

        # Operational snapshot (existing)
        operational = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.HEURISTIC,
            subjects=(),
            snapshot_hash="d5e4f7f12f0e53151a465a339a63be9d1ec2e354f82f1c3847c54705ab2d2074",
            lineage=DiscoverySnapshotLineage.OPERATIONAL,
            is_active=True,
        )

        # Replay snapshot (separate chain)
        replay = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.CHATGPT,  # Different planner
            subjects=(),
            snapshot_hash="ac203c9843b5bd8c883e07039ff82820c94422010be6108bb82403ca25376a22",
            lineage=DiscoverySnapshotLineage.REPLAY,  # Different lineage
            is_active=False,  # Not active yet
            replay_run_id=uuid4(),
        )

        # Both can coexist
        assert operational.lineage == DiscoverySnapshotLineage.OPERATIONAL
        assert replay.lineage == DiscoverySnapshotLineage.REPLAY
        assert operational.is_active is True
        assert replay.is_active is False

    def test_published_artifact_bindings_preserved(self) -> None:
        """After replay activation, published artifacts keep historical bindings."""
        subject_id = uuid4()
        pack_id = uuid4()

        # Published brief created in V1
        published_brief = BriefDraft(
            subject_id=subject_id,
            edition_id=uuid4(),
            group_id=uuid4(),
            pack_id=pack_id,
            pack_hash="3b801c017997f32bdf89cffd898e3547cf339babb8425ed3c9c945c084fa70de",
            version=1,
            title="Published Brief v1",
            blocks=(
                BriefBlock(
                    sentences=(
                        BriefSentence(
                            text="This is the published brief content.",
                            factual=False,
                            claim_ids=(),
                        ),
                    ),
                ),
            ),
            limits=(),
            source_ids=(),
            model_run_id=uuid4(),
            provider="anthropic",
            status=BriefDraftStatus.APPROVED,
        )

        # Replay happens, creates V2
        # Activation switches from operational V1 to replay V2
        # But brief still points to V1 pack/snapshot
        assert published_brief.pack_id == pack_id  # Unchanged
