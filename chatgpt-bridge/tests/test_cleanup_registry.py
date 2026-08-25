"""
Tests for the cleanup registry state machine.

Tests the state transitions and cleanup automation without actual UI execution.
"""

import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

# Import test helper
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge.registry import RunRegistry


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield db_path


@pytest.fixture
def registry(temp_db):
    return RunRegistry(temp_db)


class TestCleanupStateTransitions:
    def test_start_cleanup_from_delete_pending(self, registry):
        """DELETE_PENDING → DELETING transition."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        state = registry.get_conversation_lifecycle(conv_id)
        assert state["status"] == "delete_pending"

        result = registry.start_cleanup(conv_id)
        assert result["status"] == "deleting"
        assert result["version"] == 3  # created (1), released (2), deleting (3)

    def test_start_cleanup_idempotent_from_deleting(self, registry):
        """start_cleanup on DELETING is idempotent."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)

        result = registry.start_cleanup(conv_id)
        assert result["status"] == "deleting"

    def test_mark_deleted_transitions_to_deleted(self, registry):
        """DELETING → DELETED transition."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)

        result = registry.mark_conversation_deleted(conv_id)
        assert result["status"] == "deleted"
        assert result["deleted_at"] is not None
        assert result["cleanup_attempt_count"] == 1

    def test_mark_deleted_idempotent(self, registry):
        """mark_conversation_deleted on DELETED is idempotent."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_conversation_deleted(conv_id)

        result = registry.mark_conversation_deleted(conv_id)
        assert result["status"] == "deleted"
        assert result["cleanup_attempt_count"] == 1  # No increment on idempotent call

    def test_mark_cleanup_failed_from_deleting(self, registry):
        """DELETING → CLEANUP_FAILED transition."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)

        result = registry.mark_cleanup_failed(
            conv_id,
            error_code="delete_button_not_found",
            error_message="Could not find delete button",
        )
        assert result["status"] == "cleanup_failed"
        assert result["cleanup_attempt_count"] == 1
        assert result["last_cleanup_error_code"] == "delete_button_not_found"

    def test_mark_cleanup_failed_retryable(self, registry):
        """Retry after CLEANUP_FAILED."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)

        registry.mark_cleanup_failed(conv_id, error_code="timeout")

        result = registry.start_cleanup(conv_id)
        assert result["status"] == "deleting"
        assert result["cleanup_attempt_count"] == 1

        result = registry.mark_conversation_deleted(conv_id)
        assert result["status"] == "deleted"
        assert result["cleanup_attempt_count"] == 2  # Incremented on success

    def test_keep_policy_never_enters_cleanup(self, registry):
        """KEEP policy → RETAINED (no cleanup states)."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "keep")
        registry.release_conversation(conv_id, "success")

        state = registry.get_conversation_lifecycle(conv_id)
        assert state["status"] == "retained"

        with pytest.raises(ValueError, match="Cannot start cleanup"):
            registry.start_cleanup(conv_id)

    def test_failure_outcome_does_not_cleanup(self, registry):
        """FAILURE outcome → RETAINED (no cleanup)."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "failure")

        state = registry.get_conversation_lifecycle(conv_id)
        assert state["status"] == "retained"

    def test_cleanup_attempt_count_tracking(self, registry):
        """Cleanup attempt count increments correctly."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        assert registry.get_conversation_lifecycle(conv_id)["cleanup_attempt_count"] == 0

        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, error_code="timeout")
        state = registry.get_conversation_lifecycle(conv_id)
        assert state["cleanup_attempt_count"] == 1

        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, error_code="timeout")
        state = registry.get_conversation_lifecycle(conv_id)
        assert state["cleanup_attempt_count"] == 2

        registry.start_cleanup(conv_id)
        registry.mark_conversation_deleted(conv_id)
        state = registry.get_conversation_lifecycle(conv_id)
        assert state["cleanup_attempt_count"] == 3
        assert state["status"] == "deleted"


class TestCleanupErrorScenarios:
    def test_start_cleanup_on_retained_fails(self, registry):
        """start_cleanup on RETAINED conversation fails."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "keep")
        registry.release_conversation(conv_id, "success")

        with pytest.raises(ValueError, match="Cannot start cleanup"):
            registry.start_cleanup(conv_id)

    def test_start_cleanup_on_nonexistent_fails(self, registry):
        """start_cleanup on non-existent conversation fails."""
        with pytest.raises(ValueError, match="Conversation not found"):
            registry.start_cleanup("nonexistent-id")

    def test_mark_deleted_on_active_fails(self, registry):
        """mark_conversation_deleted on ACTIVE conversation fails."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")

        with pytest.raises(ValueError):
            registry.mark_conversation_deleted(conv_id)

    def test_error_code_truncated(self, registry):
        """Long error codes are truncated to 64 chars."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)

        long_code = "a" * 100
        registry.mark_cleanup_failed(conv_id, error_code=long_code)

        state = registry.get_conversation_lifecycle(conv_id)
        assert len(state["last_cleanup_error_code"]) == 64
        assert state["last_cleanup_error_code"] == "a" * 64


class TestCleanupInvariants:
    def test_invariant_only_success_triggers_cleanup(self, registry):
        """Only SUCCESS outcome can lead to cleanup states."""
        for outcome in ["failure", "needs_review", "cancelled"]:
            conv_id = str(uuid4())
            registry.create_conversation(
                conv_id, "https://chatgpt.com/c/test", "delete_on_success"
            )
            registry.release_conversation(conv_id, outcome)

            state = registry.get_conversation_lifecycle(conv_id)
            assert state["status"] == "retained", f"Outcome {outcome} should preserve"

    def test_invariant_keep_never_transitions_to_delete_pending(self, registry):
        """KEEP policy never enters DELETE_PENDING."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "keep")
        registry.release_conversation(conv_id, "success")

        state = registry.get_conversation_lifecycle(conv_id)
        assert state["status"] == "retained"

    def test_invariant_cleanup_failure_doesnt_block_workflow(self, registry):
        """Cleanup failure does not affect conversation state beyond cleanup status."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, error_code="timeout")

        state = registry.get_conversation_lifecycle(conv_id)
        assert state["release_outcome"] == "success"
        assert state["status"] == "cleanup_failed"

    def test_invariant_release_idempotent(self, registry):
        """Calling release twice is idempotent."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test", "delete_on_success")

        result1 = registry.release_conversation(conv_id, "success")
        result2 = registry.release_conversation(conv_id, "success")

        assert result1["version"] == result2["version"]
        assert result1["status"] == result2["status"]
        assert result1["released_at"] == result2["released_at"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
