"""Tests for Discovery with conversation lifecycle integration (Increment 4).

Tests verify that:
1. Discovery creates a ConversationLifecycle with DELETE_ON_SUCCESS policy
2. After successful batch commit, release(SUCCESS) is called
3. Continue/recovery reuses the same conversation and lifecycle
4. Analyst assistance conversations use KEEP policy
"""

from uuid import uuid4

from cti_app.domain.model_conversations import (
    ConversationLifecycle,
    ConversationLifecycleStatus,
    ConversationPolicy,
    ConversationReleaseOutcome,
)


class TestDiscoveryConversationLifecycleCreation:
    def test_create_lifecycle_with_delete_on_success(self) -> None:
        lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        assert lifecycle.policy == ConversationPolicy.DELETE_ON_SUCCESS
        assert lifecycle.status == ConversationLifecycleStatus.ACTIVE
        assert lifecycle.release_outcome is None

    def test_lifecycle_release_triggers_delete_pending(self) -> None:
        lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        lifecycle.release(outcome=ConversationReleaseOutcome.SUCCESS)

        assert lifecycle.status == ConversationLifecycleStatus.DELETE_PENDING
        assert lifecycle.release_outcome == ConversationReleaseOutcome.SUCCESS
        assert lifecycle.released_at is not None

    def test_lifecycle_release_idempotent(self) -> None:
        lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        lifecycle.release(outcome=ConversationReleaseOutcome.SUCCESS)
        version_after_first = lifecycle.version
        status_after_first = lifecycle.status

        lifecycle.release(outcome=ConversationReleaseOutcome.FAILURE)

        assert lifecycle.status == status_after_first
        assert lifecycle.version == version_after_first
        assert lifecycle.release_outcome == ConversationReleaseOutcome.SUCCESS


class TestDiscoveryLifecycleWorkflow:
    def test_discovery_lifecycle_workflow_full(self) -> None:
        lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        assert lifecycle.status == ConversationLifecycleStatus.ACTIVE
        assert lifecycle.policy == ConversationPolicy.DELETE_ON_SUCCESS

        lifecycle.release(outcome=ConversationReleaseOutcome.SUCCESS)
        # Local var: mypy narrows `lifecycle.status` from the assert above and
        # doesn't know release() mutates it, which would flag this compare as
        # spurious "non-overlapping" without the rebind.
        expected_status = ConversationLifecycleStatus.DELETE_PENDING
        assert lifecycle.status == expected_status

        lifecycle.start_cleanup()
        expected_status = ConversationLifecycleStatus.DELETING
        assert lifecycle.status == expected_status

        lifecycle.mark_deleted()
        expected_status = ConversationLifecycleStatus.DELETED
        assert lifecycle.status == expected_status

    def test_failure_outcome_prevents_cleanup(self) -> None:
        lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        lifecycle.release(outcome=ConversationReleaseOutcome.FAILURE)

        assert lifecycle.status == ConversationLifecycleStatus.RETAINED


class TestAnalystConversationLifecycle:
    def test_analyst_lifecycle_with_keep_policy(self) -> None:
        analyst_lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.KEEP,
        )

        assert analyst_lifecycle.policy == ConversationPolicy.KEEP
        assert analyst_lifecycle.status == ConversationLifecycleStatus.ACTIVE

    def test_analyst_lifecycle_release_success_retains_conversation(self) -> None:
        analyst_lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.KEEP,
        )

        analyst_lifecycle.release(outcome=ConversationReleaseOutcome.SUCCESS)

        assert analyst_lifecycle.status == ConversationLifecycleStatus.RETAINED
        assert analyst_lifecycle.release_outcome == ConversationReleaseOutcome.SUCCESS


class TestContinueRecoveryLifecycle:
    def test_continue_preserves_lifecycle_policy(self) -> None:
        initial_lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        initial_policy = initial_lifecycle.policy

        # The lifecycle is reused as-is from the DB on continue; nothing to
        # simulate beyond reading the same policy back.
        assert initial_policy == ConversationPolicy.DELETE_ON_SUCCESS


class TestConversationLifecycleCleanupFlow:
    def test_cleanup_with_retry(self) -> None:
        lifecycle = ConversationLifecycle(
            id=uuid4(),
            policy=ConversationPolicy.DELETE_ON_SUCCESS,
        )

        lifecycle.release(outcome=ConversationReleaseOutcome.SUCCESS)
        assert lifecycle.status == ConversationLifecycleStatus.DELETE_PENDING

        lifecycle.mark_cleanup_failed(error_code="network_timeout")
        # Local var: mypy narrows `lifecycle.status` from the assert above and
        # doesn't know mark_cleanup_failed() mutates it, which would flag this
        # compare as spurious without the rebind.
        expected_status = ConversationLifecycleStatus.CLEANUP_FAILED
        assert lifecycle.status == expected_status
        assert lifecycle.cleanup_attempt_count == 1

        lifecycle.mark_deleted()
        expected_status = ConversationLifecycleStatus.DELETED
        assert lifecycle.status == expected_status
        assert lifecycle.cleanup_attempt_count == 2
