"""
Tests for replay activation - validating and promoting replays.

Incrément 4: Replay and lineage
"""

import pytest
from uuid import uuid4

from cti_app.application.replay_activator import (
    ReplayActivator,
    ReplayActivationError,
)
from cti_app.domain.discovery_cumulative import (
    DiscoverySnapshot,
    DiscoverySnapshotLineage,
    DiscoveryPlannerKind,
    ReplayIdentityMapping,
    ReplayIdentityResolution,
)


class TestReplayActivator:
    """Test replay activation and precondition validation."""

    @pytest.mark.asyncio
    async def test_activation_requires_mapping_for_published_subjects(self):
        """Cannot activate replay if published subject has no mapping."""
        activator = ReplayActivator()
        edition_id = uuid4()

        replay_snapshot = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.HEURISTIC,
            subjects=(),
            snapshot_hash="replay" + "0" * 59,
            lineage=DiscoverySnapshotLineage.REPLAY,
        )

        # Create mapping for one subject
        subject1 = uuid4()
        subject2 = uuid4()  # Missing mapping

        mappings = {
            subject1: ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=subject1,
                operational_subject_id=uuid4(),
                resolution=ReplayIdentityResolution.SAME,
                actor_id="test",
            )
        }

        published_subjects = [subject1, subject2]

        with pytest.raises(ReplayActivationError, match="Published subjects without mapping"):
            await activator.validate_activation_preconditions(
                replay_snapshot,
                mappings,
                published_subjects,
            )

    @pytest.mark.asyncio
    async def test_activation_succeeds_with_complete_mapping(self):
        """Activation succeeds when all published subjects have mappings."""
        activator = ReplayActivator()
        edition_id = uuid4()

        replay_snapshot = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.HEURISTIC,
            subjects=(),
            snapshot_hash="replay" + "0" * 59,
            lineage=DiscoverySnapshotLineage.REPLAY,
        )

        # Create mappings for all subjects
        subject1 = uuid4()
        subject2 = uuid4()
        replay_subject1 = uuid4()
        replay_subject2 = uuid4()

        mappings = {
            replay_subject1: ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=replay_subject1,
                operational_subject_id=subject1,
                resolution=ReplayIdentityResolution.SAME,
                actor_id="test",
            ),
            replay_subject2: ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=replay_subject2,
                operational_subject_id=subject2,
                resolution=ReplayIdentityResolution.SPLIT_OF,
                actor_id="test",
            ),
        }

        published_subjects = [subject1, subject2]

        # Should not raise
        await activator.validate_activation_preconditions(
            replay_snapshot,
            mappings,
            published_subjects,
        )

    @pytest.mark.asyncio
    async def test_activation_allows_new_subjects(self):
        """NEW subjects don't need operational mapping for activation."""
        activator = ReplayActivator()
        edition_id = uuid4()

        replay_snapshot = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.HEURISTIC,
            subjects=(),
            snapshot_hash="replay" + "0" * 59,
            lineage=DiscoverySnapshotLineage.REPLAY,
        )

        # One same, one new (no mapping needed)
        subject1 = uuid4()
        replay_subject1 = uuid4()
        replay_subject_new = uuid4()

        mappings = {
            replay_subject1: ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=replay_subject1,
                operational_subject_id=subject1,
                resolution=ReplayIdentityResolution.SAME,
                actor_id="test",
            ),
            replay_subject_new: ReplayIdentityMapping(
                replay_run_id=uuid4(),
                replay_subject_id=replay_subject_new,
                operational_subject_id=None,  # NEW
                resolution=ReplayIdentityResolution.NEW,
                actor_id="test",
            ),
        }

        published_subjects = [subject1]  # Only subject1 is published

        # Should succeed - new subjects don't need mapping if not published
        await activator.validate_activation_preconditions(
            replay_snapshot,
            mappings,
            published_subjects,
        )
