"""
Tests for Increment 3: Cleanup UI Automation + Worker + Sweeper

Tests the complete cleanup flow:
1. deleteConversation() in content.js
2. Message routing background.js
3. CleanupWorker processing
4. ConversationSweeper recovery
"""

import asyncio
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from server import CleanupWorker, ConversationSweeper, RunRegistry

pytestmark = pytest.mark.asyncio


@pytest.fixture
def temp_db():
    """Temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield db_path


@pytest.fixture
def registry(temp_db):
    """Fresh RunRegistry for each test."""
    return RunRegistry(temp_db)


class TestCleanupWorker:
    """Test CleanupWorker automation."""

    @pytest.mark.asyncio
    async def test_cleanup_success(self, registry):
        """Successful cleanup workflow."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test123", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        # Mock the bridge
        mock_bridge = AsyncMock()
        mock_bridge.request = AsyncMock(
            return_value={
                "success": True,
                "verified_deleted": True,
                "steps_completed": ["locator_verified", "menu_opened", "deletion_verified"],
            }
        )

        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        assert result is True
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_cleanup_failure_retryable(self, registry):
        """Cleanup failure marks as CLEANUP_FAILED for retry."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test456", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        # Mock the bridge returning a failure
        mock_bridge = AsyncMock()
        mock_bridge.request = AsyncMock(
            return_value={
                "success": False,
                "verified_deleted": False,
                "error_code": "menu_not_found",
                "error_message": "Cannot find conversation options button",
                "steps_completed": ["locator_verified"],
            }
        )

        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        assert result is False
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["cleanup_attempt_count"] == 1
        assert conv["last_cleanup_error_code"] == "menu_not_found"

    @pytest.mark.asyncio
    async def test_cleanup_timeout(self, registry):
        """Cleanup timeout is handled gracefully."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/timeout", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        mock_bridge = AsyncMock()
        mock_bridge.request = AsyncMock(side_effect=asyncio.TimeoutError())

        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        assert result is False
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "timeout"

    @pytest.mark.asyncio
    async def test_cleanup_locator_mismatch_fails_closed(self, registry):
        """Locator mismatch causes cleanup to fail (fail-closed)."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/mismatch", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        mock_bridge = AsyncMock()
        mock_bridge.request = AsyncMock(
            return_value={
                "success": False,
                "verified_deleted": False,
                "error_code": "locator_mismatch",
                "error_message": "URL mismatch",
                "steps_completed": [],
            }
        )

        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        assert result is False
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"

    @pytest.mark.asyncio
    async def test_cleanup_idempotent_on_already_deleted(self, registry):
        """Cleanup on already-deleted conversation is idempotent."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/deleted", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_conversation_deleted(conv_id)

        mock_bridge = AsyncMock()
        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        # Should return False because not in DELETE_PENDING anymore
        assert result is False
        mock_bridge.request.assert_not_called()


class TestConversationSweeper:
    """Test ConversationSweeper recovery."""

    @pytest.mark.asyncio
    async def test_sweep_delete_pending(self, registry):
        """Sweeper processes all DELETE_PENDING conversations."""
        conv_ids = [str(uuid4()) for _ in range(3)]
        for conv_id in conv_ids:
            registry.create_conversation(conv_id, f"https://chatgpt.com/c/{conv_id[:8]}", "delete_on_success")
            registry.release_conversation(conv_id, "success")

        assert len(registry.get_all_delete_pending()) == 3

        # Mock the worker
        mock_worker = AsyncMock()
        mock_worker.process_cleanup_task = AsyncMock(return_value=True)

        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.sweep()

        assert mock_worker.process_cleanup_task.call_count == 3

    @pytest.mark.asyncio
    async def test_sweep_continues_on_error(self, registry):
        """Sweeper continues processing even if one fails."""
        conv_ids = [str(uuid4()) for _ in range(3)]
        for i, conv_id in enumerate(conv_ids):
            registry.create_conversation(conv_id, f"https://chatgpt.com/c/{i}", "delete_on_success")
            registry.release_conversation(conv_id, "success")

        mock_worker = AsyncMock()
        # Second call raises an error, others succeed
        mock_worker.process_cleanup_task = AsyncMock(
            side_effect=[True, Exception("test error"), True]
        )

        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.sweep()

        # Should have attempted all 3 despite one error
        assert mock_worker.process_cleanup_task.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_failed_cleanup(self, registry):
        """Sweeper retries CLEANUP_FAILED conversations."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/retry", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, "menu_not_found", "Test error")

        assert len(registry.get_all_cleanup_failed()) == 1

        mock_worker = AsyncMock()
        mock_worker.process_cleanup_task = AsyncMock(return_value=True)

        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.retry_failed()

        mock_worker.process_cleanup_task.assert_called_once_with(conv_id)

    @pytest.mark.asyncio
    async def test_retry_failed_never_retries_locator_mismatch(self, registry):
        """Fail-closed: locator_mismatch is a terminal identity error, never auto-retried."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/mismatch-retry", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        mock_bridge = AsyncMock()
        mock_bridge.request = AsyncMock(
            return_value={
                "success": False,
                "verified_deleted": False,
                "error_code": "locator_mismatch",
                "error_message": "URL mismatch",
                "steps_completed": [],
            }
        )
        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        assert result is False
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_mismatch"

        mock_worker = AsyncMock()
        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.retry_failed()

        mock_worker.process_cleanup_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_failed_never_retries_locator_invalid(self, registry):
        """Fail-closed: locator_invalid is a terminal identity error, never auto-retried."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/invalid-retry", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        mock_bridge = AsyncMock()
        worker = CleanupWorker(registry, mock_bridge)

        # Force la conversation en état DELETE_PENDING sans external_locator exploitable
        # en simulant l'échec worker "locator_invalid" directement via mark_cleanup_failed,
        # comme le fait process_cleanup_task quand external_locator est absent.
        registry.mark_cleanup_failed(conv_id, "locator_invalid", "Missing external_locator")

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_invalid"

        mock_worker = AsyncMock()
        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.retry_failed()

        mock_worker.process_cleanup_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_failed_respects_max_attempts(self, registry):
        """Sweeper doesn't retry if max attempts reached."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/max", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        # Simulate 3 failed attempts
        for _ in range(3):
            registry.start_cleanup(conv_id)
            registry.mark_cleanup_failed(conv_id, "error", "Test")

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["cleanup_attempt_count"] == 3

        mock_worker = AsyncMock()
        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.retry_failed()

        # Should not retry because max attempts reached
        mock_worker.process_cleanup_task.assert_not_called()


class TestCleanupIntegration:
    """Integration tests for the complete cleanup flow."""

    @pytest.mark.asyncio
    async def test_complete_cleanup_flow(self, registry):
        """Test the complete flow: DELETE_PENDING → DELETING → DELETED."""
        conv_id = str(uuid4())
        locator = "https://chatgpt.com/c/complete"
        registry.create_conversation(conv_id, locator, "delete_on_success")
        registry.release_conversation(conv_id, "success")

        # Verify DELETE_PENDING
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "delete_pending"

        # Mock successful cleanup
        mock_bridge = AsyncMock()
        mock_bridge.request = AsyncMock(
            return_value={
                "success": True,
                "verified_deleted": True,
                "steps_completed": [
                    "locator_verified",
                    "found_menu_button",
                    "menu_opened",
                    "found_delete_item",
                    "found_confirm_button",
                    "clicked_confirm",
                    "deletion_verified",
                ],
            }
        )

        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        assert result is True

        # Verify DELETED
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "deleted"
        assert conv["deleted_at"] is not None
        assert conv["cleanup_attempt_count"] == 1

    @pytest.mark.asyncio
    async def test_restart_recovery_workflow(self, registry):
        """Test that sweeper recovers DELETE_PENDING after restart."""
        # Simulate state before "crash"
        conv_ids = [str(uuid4()) for _ in range(2)]
        for conv_id in conv_ids:
            registry.create_conversation(conv_id, f"https://chatgpt.com/c/{conv_id[:8]}", "delete_on_success")
            registry.release_conversation(conv_id, "success")

        # Create a new registry instance (simulating restart)
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "restart.db"
            # Copy the database
            import shutil

            shutil.copy(registry.db_path, db_path)

            recovered_registry = RunRegistry(db_path)
            pending = recovered_registry.get_all_delete_pending()
            assert len(pending) == 2  # Both conversations should still be DELETE_PENDING

            # Now sweeper processes them
            mock_worker = AsyncMock()
            mock_worker.process_cleanup_task = AsyncMock(return_value=True)

            sweeper = ConversationSweeper(recovered_registry, mock_worker)
            await sweeper.sweep()

            assert mock_worker.process_cleanup_task.call_count == 2
