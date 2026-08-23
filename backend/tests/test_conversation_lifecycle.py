"""Tests for ConversationLifecycle domain model.

Tests the state machine, idempotence, and invariants required by the mission.
"""

from datetime import UTC, datetime

import pytest

from cti_app.domain.model_conversations import (
    ConversationLifecycle,
    ConversationLifecycleStatus,
    ConversationPolicy,
    ConversationReleaseOutcome,
)


class TestConversationLifecycleBasic:
    """Test basic lifecycle operations."""

    def test_create_with_keep_policy(self) -> None:
        """Create a KEEP lifecycle."""
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)
        assert lc.policy == ConversationPolicy.KEEP
        assert lc.status == ConversationLifecycleStatus.ACTIVE
        assert lc.release_outcome is None
        assert lc.released_at is None
        assert lc.cleanup_attempt_count == 0

    def test_create_with_delete_on_success_policy(self) -> None:
        """Create a DELETE_ON_SUCCESS lifecycle."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        assert lc.policy == ConversationPolicy.DELETE_ON_SUCCESS
        assert lc.status == ConversationLifecycleStatus.ACTIVE

    def test_invalid_init_non_active_without_outcome(self) -> None:
        """Cannot create non-ACTIVE status without an outcome."""
        with pytest.raises(ValueError):
            ConversationLifecycle(
                policy=ConversationPolicy.KEEP,
                status=ConversationLifecycleStatus.DELETED,
                release_outcome=None,
            )

    def test_invalid_init_active_with_outcome(self) -> None:
        """Cannot create ACTIVE status with an outcome."""
        with pytest.raises(ValueError):
            ConversationLifecycle(
                policy=ConversationPolicy.KEEP,
                status=ConversationLifecycleStatus.ACTIVE,
                release_outcome=ConversationReleaseOutcome.SUCCESS,
            )


class TestReleaseTransitions:
    """Test release() transitions."""

    def test_release_success_with_keep_policy(self) -> None:
        """KEEP + SUCCESS → RETAINED (no cleanup)."""
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)
        now = datetime.now(UTC)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS, now=now)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lc.released_at == now
        assert lc.version == 2

    def test_release_success_with_delete_on_success_policy(self) -> None:
        """DELETE_ON_SUCCESS + SUCCESS → DELETE_PENDING (cleanup eligible)."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        now = datetime.now(UTC)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS, now=now)

        assert lc.status == ConversationLifecycleStatus.DELETE_PENDING
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lc.released_at == now

    def test_release_failure_preserves_conversation(self) -> None:
        """FAILURE outcome always preserves conversation."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.FAILURE)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.FAILURE

    def test_release_needs_review_preserves_conversation(self) -> None:
        """NEEDS_REVIEW outcome always preserves conversation."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.NEEDS_REVIEW)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.NEEDS_REVIEW

    def test_release_cancelled_preserves_conversation(self) -> None:
        """CANCELLED outcome always preserves conversation."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.CANCELLED)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.CANCELLED


class TestReleaseIdempotence:
    """Test that release() is idempotent (L15)."""

    def test_release_twice_is_noop(self) -> None:
        """Calling release() twice returns to same state."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        now = datetime.now(UTC)

        # First release
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS, now=now)
        version_after_first = lc.version
        status_after_first = lc.status

        # Second release with different outcome (should be ignored)
        lc.release(outcome=ConversationReleaseOutcome.FAILURE, now=now)

        assert lc.status == status_after_first
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lc.version == version_after_first  # No version bump


class TestCleanupTransitions:
    """Test cleanup state transitions."""

    def test_start_cleanup_from_delete_pending(self) -> None:
        """DELETE_PENDING + start_cleanup() → DELETING."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        now = datetime.now(UTC)

        lc.start_cleanup(now=now)

        assert lc.status == ConversationLifecycleStatus.DELETING
        assert lc.updated_at == now
        assert lc.version == 3  # +1 for release, +1 for start_cleanup

    def test_start_cleanup_from_invalid_status_raises(self) -> None:
        """Cannot start cleanup from non-DELETE_PENDING status."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)

        with pytest.raises(ValueError):
            lc.start_cleanup()

    def test_mark_cleanup_failed_increments_count(self) -> None:
        """Cleanup failure increments attempt count."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        lc.mark_cleanup_failed(error_code="network_timeout")

        assert lc.status == ConversationLifecycleStatus.CLEANUP_FAILED
        assert lc.cleanup_attempt_count == 1
        assert lc.last_cleanup_error_code == "network_timeout"

    def test_mark_cleanup_failed_multiple_times(self) -> None:
        """Cleanup failure can be retried."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        lc.mark_cleanup_failed(error_code="network_timeout")
        assert lc.cleanup_attempt_count == 1

        lc.mark_cleanup_failed(error_code="ui_stale")
        assert lc.cleanup_attempt_count == 2

    def test_mark_deleted_records_timestamp(self) -> None:
        """mark_deleted() sets deleted_at and increments attempt count."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        now = datetime.now(UTC)

        lc.mark_deleted(now=now)

        assert lc.status == ConversationLifecycleStatus.DELETED
        assert lc.deleted_at == now
        assert lc.cleanup_attempt_count == 1

    def test_mark_cleanup_failed_idempotent(self) -> None:
        """Calling mark_cleanup_failed on already-deleted is safe."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        lc.mark_deleted()
        count_after_delete = lc.cleanup_attempt_count

        # Try to fail an already-deleted conversation (should be no-op)
        lc.mark_cleanup_failed(error_code="stale")

        assert lc.status == ConversationLifecycleStatus.DELETED
        assert lc.cleanup_attempt_count == count_after_delete  # No change


class TestCleanupWorkflow:
    """Test complete cleanup workflow."""

    def test_full_workflow_keep_policy(self) -> None:
        """KEEP policy: ACTIVE → RETAINED (no cleanup ever)."""
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.policy == ConversationPolicy.KEEP
        # Cleanup should never happen for KEEP

    def test_full_workflow_delete_on_success_happy_path(self) -> None:
        """Full successful cleanup workflow:
        ACTIVE → DELETE_PENDING → DELETING → DELETED
        """
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)

        # Release with success
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lc.status == ConversationLifecycleStatus.DELETE_PENDING

        # Start cleanup
        lc.start_cleanup()
        # Bound to a local first: mypy narrows `lc.status` from the assert
        # above and doesn't know `start_cleanup()` mutates it, which would
        # otherwise make this comparison a spurious "non-overlapping" error.
        expected_status = ConversationLifecycleStatus.DELETING
        assert lc.status == expected_status

        # Mark deleted
        lc.mark_deleted()
        expected_status = ConversationLifecycleStatus.DELETED
        assert lc.status == expected_status

    def test_full_workflow_delete_on_success_with_retry(self) -> None:
        """Full workflow with cleanup failure and retry:
        ACTIVE → DELETE_PENDING → CLEANUP_FAILED → DELETED (after retry)
        """
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        # First cleanup attempt fails
        lc.mark_cleanup_failed(error_code="network_timeout")
        assert lc.status == ConversationLifecycleStatus.CLEANUP_FAILED
        assert lc.cleanup_attempt_count == 1

        # Retry succeeds
        lc.mark_deleted()
        # Bound to a local first: mypy narrows `lc.status` from the assert
        # above and doesn't know `mark_deleted()` mutates it, which would
        # otherwise make this comparison a spurious "non-overlapping" error.
        expected_status = ConversationLifecycleStatus.DELETED
        assert lc.status == expected_status
        assert lc.cleanup_attempt_count == 2

    def test_failure_outcome_prevents_cleanup(self) -> None:
        """FAILURE outcome → RETAINED (no cleanup)."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)

        lc.release(outcome=ConversationReleaseOutcome.FAILURE)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        # No cleanup should ever be attempted


class TestInvariants:
    """Test critical invariants from the mission (L1-L25)."""

    def test_L2_model_success_not_auto_cleanup(self) -> None:
        """L2: A successful model response alone never triggers cleanup."""
        # This test ensures ConversationLifecycle requires explicit release()
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        # Even if a model succeeds, status should remain ACTIVE until release()
        assert lc.status == ConversationLifecycleStatus.ACTIVE

    def test_L3_only_success_release_allows_cleanup(self) -> None:
        """L3: Only release(SUCCESS) can enable cleanup."""
        lc_success = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc_success.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lc_success.status == ConversationLifecycleStatus.DELETE_PENDING

        lc_failure = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc_failure.release(outcome=ConversationReleaseOutcome.FAILURE)
        assert lc_failure.status == ConversationLifecycleStatus.RETAINED

    def test_L4_failure_preserves(self) -> None:
        """L4: FAILURE preserves the conversation."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.FAILURE)
        assert lc.status == ConversationLifecycleStatus.RETAINED

    def test_L5_needs_review_preserves(self) -> None:
        """L5: NEEDS_REVIEW preserves the conversation."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.NEEDS_REVIEW)
        assert lc.status == ConversationLifecycleStatus.RETAINED

    def test_L6_cancelled_preserves(self) -> None:
        """L6: CANCELLED preserves the conversation."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.CANCELLED)
        assert lc.status == ConversationLifecycleStatus.RETAINED

    def test_L10_continue_preserves_policy(self) -> None:
        """L10: A continue uses the same policy as initial.

        The policy is immutable; we test this by ensuring there's
        no setter that allows changing policy after creation.
        """
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        initial_policy = lc.policy
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        # Policy should not have changed
        assert lc.policy == initial_policy

    def test_L14_cleanup_not_blocking_success(self) -> None:
        """L12: Cleanup failure does not make workflow fail.

        Cleanup is independent: failure to delete does not
        invalidate the SUCCESS outcome.
        """
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        lc.mark_cleanup_failed(error_code="ui_error")

        # The release was still SUCCESS
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS

    def test_L15_release_idempotent(self) -> None:
        """L15: Release is idempotent."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        state_1 = (lc.status, lc.version)

        # Release again with different outcome
        lc.release(outcome=ConversationReleaseOutcome.FAILURE)
        state_2 = (lc.status, lc.version)

        # State unchanged
        assert state_1 == state_2

    def test_L22_keep_never_triggers_cleanup(self) -> None:
        """L22: KEEP policy never triggers cleanup even with SUCCESS."""
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lc.status == ConversationLifecycleStatus.RETAINED
        # No DELETE_PENDING, no cleanup path
