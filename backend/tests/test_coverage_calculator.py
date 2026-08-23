"""
Tests for coverage calculator - contribution closure and new contributions detection.

Incrément 3: Préservation éditoriale
"""

from uuid import uuid4

import pytest

from cti_app.application.coverage_calculator import (
    contribution_closure,
    new_contributions,
    resolve_canonical_subject,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    SubjectMergeEvent,
)


class TestResolveCanonicalSubject:
    """Test subject_id resolution through merge chains."""

    def test_active_subject_resolves_to_itself(self) -> None:
        """An ACTIVE subject with no merges resolves to itself."""
        subject_id = uuid4()
        result = resolve_canonical_subject(subject_id, [])
        assert result == subject_id

    def test_simple_merge_chain(self) -> None:
        """Y → X resolves Y to X."""
        x_id = uuid4()
        y_id = uuid4()
        edition_id = uuid4()

        event = SubjectMergeEvent(
            edition_id=edition_id,
            from_subject_id=y_id,
            into_subject_id=x_id,
            merge_run_id=uuid4(),
            actor_id="test",
            reason="test merge",
        )

        # Y should resolve to X
        assert resolve_canonical_subject(y_id, [event]) == x_id
        # X should resolve to itself
        assert resolve_canonical_subject(x_id, [event]) == x_id

    def test_chain_z_to_y_to_x(self) -> None:
        """Z → Y → X: Z resolves to X through chain."""
        x_id = uuid4()
        y_id = uuid4()
        z_id = uuid4()
        edition_id = uuid4()
        run_id = uuid4()

        events = [
            SubjectMergeEvent(
                edition_id=edition_id,
                from_subject_id=y_id,
                into_subject_id=x_id,
                merge_run_id=run_id,
                actor_id="test",
                reason="merge Y into X",
            ),
            SubjectMergeEvent(
                edition_id=edition_id,
                from_subject_id=z_id,
                into_subject_id=y_id,
                merge_run_id=run_id,
                actor_id="test",
                reason="merge Z into Y",
            ),
        ]

        # Z should resolve to X (canonical)
        assert resolve_canonical_subject(z_id, events) == x_id

    def test_cycle_detection(self) -> None:
        """A → B → A cycle raises ValueError."""
        a_id = uuid4()
        b_id = uuid4()
        edition_id = uuid4()
        run_id = uuid4()

        # This is a bad data state, but we should detect it
        events = [
            SubjectMergeEvent(
                edition_id=edition_id,
                from_subject_id=a_id,
                into_subject_id=b_id,
                merge_run_id=run_id,
                actor_id="test",
                reason="A → B",
            ),
            SubjectMergeEvent(
                edition_id=edition_id,
                from_subject_id=b_id,
                into_subject_id=a_id,
                merge_run_id=run_id,
                actor_id="test",
                reason="B → A (cycle!)",
            ),
        ]

        with pytest.raises(ValueError, match="Cycle detected"):
            resolve_canonical_subject(a_id, events)


class TestContributionClosure:
    """Test contribution closure - finding all contributions for a subject including merged ones."""

    def test_subject_with_no_merges(self) -> None:
        """A subject with no merges has its own contributions."""
        subject_id = uuid4()
        contribution_ids = {uuid4(), uuid4(), uuid4()}
        all_contributions = {subject_id: contribution_ids}

        closure = contribution_closure(subject_id, [], all_contributions)
        assert closure == contribution_ids

    def test_subject_absorbs_merged_contribution(self) -> None:
        """When Y → X, X's closure includes Y's contributions."""
        x_id = uuid4()
        y_id = uuid4()
        edition_id = uuid4()

        x_contrib = {uuid4(), uuid4()}
        y_contrib = {uuid4(), uuid4()}
        all_contributions = {x_id: x_contrib, y_id: y_contrib}

        event = SubjectMergeEvent(
            edition_id=edition_id,
            from_subject_id=y_id,
            into_subject_id=x_id,
            merge_run_id=uuid4(),
            actor_id="test",
            reason="merge Y into X",
        )

        # X's closure should include both X's and Y's contributions
        closure = contribution_closure(x_id, [event], all_contributions)
        assert closure == x_contrib | y_contrib

    def test_multiple_merges_into_canonical(self) -> None:
        """Multiple subjects merged into X all contribute to X's closure."""
        x_id = uuid4()
        y_id = uuid4()
        z_id = uuid4()
        edition_id = uuid4()
        run_id = uuid4()

        x_contrib = {uuid4()}
        y_contrib = {uuid4()}
        z_contrib = {uuid4()}
        all_contributions = {x_id: x_contrib, y_id: y_contrib, z_id: z_contrib}

        events = [
            SubjectMergeEvent(
                edition_id=edition_id,
                from_subject_id=y_id,
                into_subject_id=x_id,
                merge_run_id=run_id,
                actor_id="test",
                reason="Y → X",
            ),
            SubjectMergeEvent(
                edition_id=edition_id,
                from_subject_id=z_id,
                into_subject_id=x_id,
                merge_run_id=run_id,
                actor_id="test",
                reason="Z → X",
            ),
        ]

        closure = contribution_closure(x_id, events, all_contributions)
        assert closure == x_contrib | y_contrib | z_contrib


class TestNewContributions:
    """Test detection of new contributions not yet covered by artifact."""

    def test_no_new_contributions_when_all_covered(self) -> None:
        """If all contributions are covered, new_contributions is empty."""
        artifact_id = uuid4()
        subject_id = uuid4()
        edition_id = uuid4()

        contrib1 = uuid4()
        contrib2 = uuid4()

        # Both contributions are covered by the pack
        artifact_packs = [(uuid4(), {contrib1, contrib2})]

        # Simplified snapshot for testing
        snapshot = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            subjects=(),
            snapshot_hash="0" * 64,
        )

        result = new_contributions(
            artifact_id=artifact_id,
            artifact_subject_id=subject_id,
            artifact_packs=artifact_packs,
            current_snapshot=snapshot,
            merge_events=[],
            dismissed_contribution_ids=set(),
        )

        assert result == set()

    def test_new_contribution_detected(self) -> None:
        """New contributions not in any pack are detected."""
        artifact_id = uuid4()
        subject_id = uuid4()
        edition_id = uuid4()

        contrib1 = uuid4()
        contrib2 = uuid4()
        contrib_new = uuid4()

        # Pack only covers first two
        artifact_packs = [(uuid4(), {contrib1, contrib2})]

        snapshot = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            subjects=(),
            snapshot_hash="0" * 64,
        )

        result = new_contributions(
            artifact_id=artifact_id,
            artifact_subject_id=subject_id,
            artifact_packs=artifact_packs,
            current_snapshot=snapshot,
            merge_events=[],
            dismissed_contribution_ids=set(),
        )

        assert result == {contrib_new}

    def test_dismissed_contributions_not_returned(self) -> None:
        """Dismissed contributions are excluded from new contributions."""
        artifact_id = uuid4()
        subject_id = uuid4()
        edition_id = uuid4()

        contrib1 = uuid4()
        contrib2 = uuid4()

        artifact_packs = [(uuid4(), {contrib1})]

        snapshot = DiscoverySnapshot(
            edition_id=edition_id,
            version=1,
            parent_snapshot_id=None,
            intake_id=uuid4(),
            merge_run_id=uuid4(),
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            subjects=(),
            snapshot_hash="0" * 64,
        )

        # contrib2 is new but dismissed
        result = new_contributions(
            artifact_id=artifact_id,
            artifact_subject_id=subject_id,
            artifact_packs=artifact_packs,
            current_snapshot=snapshot,
            merge_events=[],
            dismissed_contribution_ids={contrib2},
        )

        assert result == set()
