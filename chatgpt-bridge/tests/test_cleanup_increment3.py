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

from bridge.registry import RunRegistry
from server import CleanupWorker, ConversationSweeper

pytestmark = pytest.mark.asyncio


class _ScriptedBridge:
    """Fake bridge whose .request() answers are scripted call-by-call.

    Used where we must NOT mock CleanupWorker itself (R54c critical test 1):
    the worker's real control flow runs, only the network edge is faked.
    """

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def request(self, message: dict, timeout: int | None = None) -> dict:
        self.calls.append(message)
        if not self._responses:
            raise AssertionError("_ScriptedBridge: no more scripted responses")
        return self._responses.pop(0)


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
            # Checkpoint WAL before copying to ensure all data is flushed
            import shutil

            registry.checkpoint_and_close()
            shutil.copy(registry.path, db_path)

            recovered_registry = RunRegistry(db_path)
            pending = recovered_registry.get_all_delete_pending()
            assert len(pending) == 2  # Both conversations should still be DELETE_PENDING

            # Now sweeper processes them
            mock_worker = AsyncMock()
            mock_worker.process_cleanup_task = AsyncMock(return_value=True)

            sweeper = ConversationSweeper(recovered_registry, mock_worker)
            await sweeper.sweep()

            assert mock_worker.process_cleanup_task.call_count == 2


class TestR54cTransientRetry:
    """R54c: transient CLEANUP_FAILED must be really retryable, terminal
    identity failures (locator_mismatch/locator_invalid) never must be —
    through no code path (worker direct, sweeper, HTTP endpoint)."""

    @pytest.mark.asyncio
    async def test_real_retry_of_transient_cleanup_failure_reaches_deleted(self, registry):
        """Critical test 1: real retry, no CleanupWorker mocking.

        This must fail on the pre-fix code (process_cleanup_task rejects
        CLEANUP_FAILED outright) and pass after the fix.
        """
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/transient", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "delete_pending"

        # First attempt fails with a real transient error.
        bridge = _ScriptedBridge(
            [
                {
                    "success": False,
                    "verified_deleted": False,
                    "error_code": "conversation_menu_not_found",
                    "error_message": "Menu button not found",
                    "steps_completed": ["locator_verified"],
                },
            ]
        )
        worker = CleanupWorker(registry, bridge)
        first = await worker.process_cleanup_task(conv_id)
        assert first is False

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "conversation_menu_not_found"

        # Retry succeeds: verified deletion.
        bridge._responses.append(
            {
                "success": True,
                "verified_deleted": True,
                "steps_completed": ["locator_verified", "menu_opened", "deletion_verified"],
            }
        )

        sweeper = ConversationSweeper(registry, worker)  # real CleanupWorker, not a mock
        calls_before_retry = len(bridge.calls)
        await sweeper.retry_failed()

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "deleted"
        assert len(bridge.calls) - calls_before_retry == 1

    @pytest.mark.asyncio
    async def test_worker_direct_refuses_locator_mismatch(self, registry):
        """Critical test 2a: worker refuses a terminal identity failure directly."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/mismatch-direct", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, "locator_mismatch", "URL mismatch")

        bridge = _ScriptedBridge([])
        worker = CleanupWorker(registry, bridge)

        result = await worker.process_cleanup_task(conv_id)

        assert result is False
        assert bridge.calls == []
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_mismatch"

    @pytest.mark.asyncio
    async def test_worker_direct_refuses_locator_invalid(self, registry):
        """Critical test 2b: same principle for locator_invalid."""
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/invalid-direct", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, "locator_invalid", "Missing external_locator")

        bridge = _ScriptedBridge([])
        worker = CleanupWorker(registry, bridge)

        result = await worker.process_cleanup_task(conv_id)

        assert result is False
        assert bridge.calls == []
        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_invalid"

    @pytest.mark.asyncio
    async def test_endpoint_refuses_locator_mismatch_with_409(self, registry, monkeypatch):
        """Critical test 3: POST /cleanup/start rejects a terminal identity
        CLEANUP_FAILED with 409 and leaves state unchanged."""
        import server
        from fastapi import HTTPException

        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/endpoint-mismatch", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, "locator_mismatch", "URL mismatch")

        monkeypatch.setattr(server, "run_registry", registry)

        with pytest.raises(HTTPException) as exc_info:
            await server.start_conversation_cleanup(conv_id)

        assert exc_info.value.status_code == 409

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_mismatch"
