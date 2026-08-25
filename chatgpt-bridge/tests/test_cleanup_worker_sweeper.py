"""
Tests for the CleanupWorker and ConversationSweeper.

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

from bridge.lifecycle import CleanupWorker, ConversationSweeper
from bridge.registry import RunRegistry

pytestmark = pytest.mark.asyncio


class _ScriptedBridge:
    """Fake bridge whose .request() answers are scripted call-by-call.

    Used where we must NOT mock CleanupWorker itself: the worker's real control
    flow runs, only the network edge is faked.
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
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield db_path


@pytest.fixture
def registry(temp_db):
    return RunRegistry(temp_db)


class TestCleanupWorker:
    @pytest.mark.asyncio
    async def test_cleanup_success(self, registry):
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test123", "delete_on_success")
        registry.release_conversation(conv_id, "success")

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
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/test456", "delete_on_success")
        registry.release_conversation(conv_id, "success")

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
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/deleted", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_conversation_deleted(conv_id)

        mock_bridge = AsyncMock()
        worker = CleanupWorker(registry, mock_bridge)
        result = await worker.process_cleanup_task(conv_id)

        # Not in DELETE_PENDING/CLEANUP_FAILED anymore, so refused before any bridge call.
        assert result is False
        mock_bridge.request.assert_not_called()


class TestConversationSweeper:
    @pytest.mark.asyncio
    async def test_sweep_delete_pending(self, registry):
        conv_ids = [str(uuid4()) for _ in range(3)]
        for conv_id in conv_ids:
            registry.create_conversation(conv_id, f"https://chatgpt.com/c/{conv_id[:8]}", "delete_on_success")
            registry.release_conversation(conv_id, "success")

        assert len(registry.get_all_delete_pending()) == 3

        mock_worker = AsyncMock()
        mock_worker.process_cleanup_task = AsyncMock(return_value=True)

        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.sweep()

        assert mock_worker.process_cleanup_task.call_count == 3

    @pytest.mark.asyncio
    async def test_sweep_continues_on_error(self, registry):
        conv_ids = [str(uuid4()) for _ in range(3)]
        for i, conv_id in enumerate(conv_ids):
            registry.create_conversation(conv_id, f"https://chatgpt.com/c/{i}", "delete_on_success")
            registry.release_conversation(conv_id, "success")

        mock_worker = AsyncMock()
        # Middle call raises; sweep must not abort on it.
        mock_worker.process_cleanup_task = AsyncMock(
            side_effect=[True, Exception("test error"), True]
        )

        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.sweep()

        assert mock_worker.process_cleanup_task.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_failed_cleanup(self, registry):
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
        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/max", "delete_on_success")
        registry.release_conversation(conv_id, "success")

        for _ in range(3):
            registry.start_cleanup(conv_id)
            registry.mark_cleanup_failed(conv_id, "error", "Test")

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["cleanup_attempt_count"] == 3

        mock_worker = AsyncMock()
        sweeper = ConversationSweeper(registry, mock_worker)
        await sweeper.retry_failed()

        # cleanup_attempt_count == 3 already hit the retry ceiling (< 3 in retry_failed).
        mock_worker.process_cleanup_task.assert_not_called()


class TestCleanupIntegration:
    @pytest.mark.asyncio
    async def test_complete_cleanup_flow(self, registry):
        conv_id = str(uuid4())
        locator = "https://chatgpt.com/c/complete"
        registry.create_conversation(conv_id, locator, "delete_on_success")
        registry.release_conversation(conv_id, "success")

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "delete_pending"

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

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "deleted"
        assert conv["deleted_at"] is not None
        assert conv["cleanup_attempt_count"] == 1

    @pytest.mark.asyncio
    async def test_restart_recovery_workflow(self, registry):
        conv_ids = [str(uuid4()) for _ in range(2)]
        for conv_id in conv_ids:
            registry.create_conversation(conv_id, f"https://chatgpt.com/c/{conv_id[:8]}", "delete_on_success")
            registry.release_conversation(conv_id, "success")

        # New RunRegistry instance over a copy of the DB, simulating a process restart.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "restart.db"
            import shutil

            registry.checkpoint_and_close()  # flush WAL before copying the file
            shutil.copy(registry.path, db_path)

            recovered_registry = RunRegistry(db_path)
            pending = recovered_registry.get_all_delete_pending()
            assert len(pending) == 2

            mock_worker = AsyncMock()
            mock_worker.process_cleanup_task = AsyncMock(return_value=True)

            sweeper = ConversationSweeper(recovered_registry, mock_worker)
            await sweeper.sweep()

            assert mock_worker.process_cleanup_task.call_count == 2


class TestTransientRetry:
    """Transient CLEANUP_FAILED must be really retryable; terminal identity
    failures (locator_mismatch/locator_invalid) never must be — through no code
    path (worker direct, sweeper, HTTP endpoint)."""

    @pytest.mark.asyncio
    async def test_real_retry_of_transient_cleanup_failure_reaches_deleted(self, registry):
        """Verify transient failures are retried without CleanupWorker mocking."""
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
    async def test_endpoint_refuses_locator_mismatch_with_409(self, registry):
        """Critical test 3: POST /cleanup/start rejects a terminal identity
        CLEANUP_FAILED with 409 and leaves state unchanged."""
        from fastapi import HTTPException

        from bridge.routes_conversations import ConversationRoutes

        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/endpoint-mismatch", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, "locator_mismatch", "URL mismatch")

        async def noop_auth() -> None:
            return None

        routes = ConversationRoutes(
            bridge=MagicMock(),
            registry=registry,
            auth_dependency=noop_auth,
        )

        with pytest.raises(HTTPException) as exc_info:
            await routes.start_conversation_cleanup(conv_id)

        assert exc_info.value.status_code == 409

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_mismatch"

    @pytest.mark.asyncio
    async def test_endpoint_refuses_locator_invalid_with_409(self, registry):
        """Critical test 3b: same principle for locator_invalid."""
        from fastapi import HTTPException

        from bridge.routes_conversations import ConversationRoutes

        conv_id = str(uuid4())
        registry.create_conversation(conv_id, "https://chatgpt.com/c/endpoint-invalid", "delete_on_success")
        registry.release_conversation(conv_id, "success")
        registry.start_cleanup(conv_id)
        registry.mark_cleanup_failed(conv_id, "locator_invalid", "Missing external_locator")

        async def noop_auth() -> None:
            return None

        routes = ConversationRoutes(
            bridge=MagicMock(),
            registry=registry,
            auth_dependency=noop_auth,
        )

        with pytest.raises(HTTPException) as exc_info:
            await routes.start_conversation_cleanup(conv_id)

        assert exc_info.value.status_code == 409

        conv = registry.get_conversation_lifecycle(conv_id)
        assert conv["status"] == "cleanup_failed"
        assert conv["last_cleanup_error_code"] == "locator_invalid"
