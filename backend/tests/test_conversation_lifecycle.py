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
    def test_create_with_keep_policy(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)
        assert lc.policy == ConversationPolicy.KEEP
        assert lc.status == ConversationLifecycleStatus.ACTIVE
        assert lc.release_outcome is None
        assert lc.released_at is None
        assert lc.cleanup_attempt_count == 0

    def test_create_with_delete_on_success_policy(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        assert lc.policy == ConversationPolicy.DELETE_ON_SUCCESS
        assert lc.status == ConversationLifecycleStatus.ACTIVE

    def test_invalid_init_non_active_without_outcome(self) -> None:
        with pytest.raises(ValueError):
            ConversationLifecycle(
                policy=ConversationPolicy.KEEP,
                status=ConversationLifecycleStatus.DELETED,
                release_outcome=None,
            )

    def test_invalid_init_active_with_outcome(self) -> None:
        with pytest.raises(ValueError):
            ConversationLifecycle(
                policy=ConversationPolicy.KEEP,
                status=ConversationLifecycleStatus.ACTIVE,
                release_outcome=ConversationReleaseOutcome.SUCCESS,
            )


class TestReleaseTransitions:
    def test_release_success_with_keep_policy(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)
        now = datetime.now(UTC)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS, now=now)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lc.released_at == now
        assert lc.version == 2

    def test_release_success_with_delete_on_success_policy(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        now = datetime.now(UTC)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS, now=now)

        assert lc.status == ConversationLifecycleStatus.DELETE_PENDING
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lc.released_at == now

    def test_release_failure_preserves_conversation(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.FAILURE)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.FAILURE

    def test_release_needs_review_preserves_conversation(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.NEEDS_REVIEW)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.NEEDS_REVIEW

    def test_release_cancelled_preserves_conversation(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.CANCELLED)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.release_outcome == ConversationReleaseOutcome.CANCELLED


class TestReleaseIdempotence:
    """Idempotence required by L15."""

    def test_release_twice_is_noop(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        now = datetime.now(UTC)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS, now=now)
        version_after_first = lc.version
        status_after_first = lc.status

        lc.release(outcome=ConversationReleaseOutcome.FAILURE, now=now)

        assert lc.status == status_after_first
        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lc.version == version_after_first


class TestCleanupTransitions:
    def test_start_cleanup_from_delete_pending(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        now = datetime.now(UTC)

        lc.start_cleanup(now=now)

        assert lc.status == ConversationLifecycleStatus.DELETING
        assert lc.updated_at == now
        assert lc.version == 3  # +1 for release, +1 for start_cleanup

    def test_start_cleanup_from_invalid_status_raises(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)

        with pytest.raises(ValueError):
            lc.start_cleanup()

    def test_mark_cleanup_failed_increments_count(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        lc.mark_cleanup_failed(error_code="network_timeout")

        assert lc.status == ConversationLifecycleStatus.CLEANUP_FAILED
        assert lc.cleanup_attempt_count == 1
        assert lc.last_cleanup_error_code == "network_timeout"

    def test_mark_cleanup_failed_multiple_times(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        lc.mark_cleanup_failed(error_code="network_timeout")
        assert lc.cleanup_attempt_count == 1

        lc.mark_cleanup_failed(error_code="ui_stale")
        assert lc.cleanup_attempt_count == 2

    def test_mark_deleted_records_timestamp(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        now = datetime.now(UTC)

        lc.mark_deleted(now=now)

        assert lc.status == ConversationLifecycleStatus.DELETED
        assert lc.deleted_at == now
        assert lc.cleanup_attempt_count == 1

    def test_mark_cleanup_failed_idempotent(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        lc.mark_deleted()
        count_after_delete = lc.cleanup_attempt_count

        lc.mark_cleanup_failed(error_code="stale")

        assert lc.status == ConversationLifecycleStatus.DELETED
        assert lc.cleanup_attempt_count == count_after_delete


class TestCleanupWorkflow:
    def test_full_workflow_keep_policy(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        assert lc.status == ConversationLifecycleStatus.RETAINED
        assert lc.policy == ConversationPolicy.KEEP

    def test_full_workflow_delete_on_success_happy_path(self) -> None:
        """ACTIVE -> DELETE_PENDING -> DELETING -> DELETED."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)

        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lc.status == ConversationLifecycleStatus.DELETE_PENDING

        lc.start_cleanup()
        # Local var: mypy narrows `lc.status` from the assert above and
        # doesn't know start_cleanup() mutates it, which would flag this
        # compare as spurious without the rebind.
        expected_status = ConversationLifecycleStatus.DELETING
        assert lc.status == expected_status

        lc.mark_deleted()
        expected_status = ConversationLifecycleStatus.DELETED
        assert lc.status == expected_status

    def test_full_workflow_delete_on_success_with_retry(self) -> None:
        """ACTIVE -> DELETE_PENDING -> CLEANUP_FAILED -> DELETED (after retry)."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)

        lc.mark_cleanup_failed(error_code="network_timeout")
        assert lc.status == ConversationLifecycleStatus.CLEANUP_FAILED
        assert lc.cleanup_attempt_count == 1

        lc.mark_deleted()
        # Local var: mypy narrows `lc.status` from the assert above and
        # doesn't know mark_deleted() mutates it, which would flag this
        # compare as spurious without the rebind.
        expected_status = ConversationLifecycleStatus.DELETED
        assert lc.status == expected_status
        assert lc.cleanup_attempt_count == 2

    def test_failure_outcome_prevents_cleanup(self) -> None:
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)

        lc.release(outcome=ConversationReleaseOutcome.FAILURE)

        assert lc.status == ConversationLifecycleStatus.RETAINED


class TestInvariants:
    """Critical invariants from the mission (L1-L25)."""

    def test_L2_model_success_not_auto_cleanup(self) -> None:
        """L2: a successful model response alone never triggers cleanup."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        assert lc.status == ConversationLifecycleStatus.ACTIVE

    def test_L3_only_success_release_allows_cleanup(self) -> None:
        """L3: only release(SUCCESS) can enable cleanup."""
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
        """L10: policy is immutable across a continue — no setter exists to
        change it after creation, so a release() call can't affect it either."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        initial_policy = lc.policy
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lc.policy == initial_policy

    def test_L14_cleanup_not_blocking_success(self) -> None:
        """L14: cleanup is independent — failure to delete does not invalidate
        the SUCCESS release outcome."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        lc.mark_cleanup_failed(error_code="ui_error")

        assert lc.release_outcome == ConversationReleaseOutcome.SUCCESS

    def test_L15_release_idempotent(self) -> None:
        """L15: release is idempotent."""
        lc = ConversationLifecycle(policy=ConversationPolicy.DELETE_ON_SUCCESS)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        state_1 = (lc.status, lc.version)

        lc.release(outcome=ConversationReleaseOutcome.FAILURE)
        state_2 = (lc.status, lc.version)

        assert state_1 == state_2

    def test_L22_keep_never_triggers_cleanup(self) -> None:
        """L22: KEEP policy never triggers cleanup even with SUCCESS."""
        lc = ConversationLifecycle(policy=ConversationPolicy.KEEP)
        lc.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lc.status == ConversationLifecycleStatus.RETAINED
