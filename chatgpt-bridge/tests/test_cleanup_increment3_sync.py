"""
Synchronous validation tests for Increment 3

These tests verify that the Cleanup UI Automation components
can be imported and have the correct structure.
"""

import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge.lifecycle import CleanupWorker, ConversationSweeper
from bridge.registry import RunRegistry


class TestIncrement3Integration:
    """Verify Increment 3 components exist and are properly structured."""

    def test_cleanup_worker_exists(self):
        """CleanupWorker class exists and has required methods."""
        assert hasattr(CleanupWorker, "process_cleanup_task")
        assert hasattr(CleanupWorker, "_send_cleanup_request")

    def test_conversation_sweeper_exists(self):
        """ConversationSweeper class exists and has required methods."""
        assert hasattr(ConversationSweeper, "sweep")
        assert hasattr(ConversationSweeper, "retry_failed")

    def test_run_registry_sweep_methods(self):
        """RunRegistry has new sweep methods."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = RunRegistry(db_path)
            assert hasattr(registry, "get_all_delete_pending")
            assert hasattr(registry, "get_all_cleanup_failed")

    def test_delete_pending_query(self):
        """get_all_delete_pending returns empty list initially."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = RunRegistry(db_path)
            pending = registry.get_all_delete_pending()
            assert isinstance(pending, list)
            assert len(pending) == 0

    def test_cleanup_failed_query(self):
        """get_all_cleanup_failed returns empty list initially."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = RunRegistry(db_path)
            failed = registry.get_all_cleanup_failed()
            assert isinstance(failed, list)
            assert len(failed) == 0

    def test_cleanup_workflow_states(self):
        """Verify complete DELETE_PENDING → DELETING → DELETED workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = RunRegistry(db_path)

            conv_id = str(uuid4())
            locator = "https://chatgpt.com/c/test"

            # Create conversation with DELETE_ON_SUCCESS
            registry.create_conversation(conv_id, locator, "delete_on_success")
            conv = registry.get_conversation_lifecycle(conv_id)
            assert conv["status"] == "active"

            # Release with SUCCESS → DELETE_PENDING
            registry.release_conversation(conv_id, "success")
            conv = registry.get_conversation_lifecycle(conv_id)
            assert conv["status"] == "delete_pending"
            assert len(registry.get_all_delete_pending()) == 1

            # Start cleanup → DELETING
            registry.start_cleanup(conv_id)
            conv = registry.get_conversation_lifecycle(conv_id)
            assert conv["status"] == "deleting"

            # Mark as deleted → DELETED
            registry.mark_conversation_deleted(conv_id)
            conv = registry.get_conversation_lifecycle(conv_id)
            assert conv["status"] == "deleted"
            assert conv["deleted_at"] is not None
            assert len(registry.get_all_delete_pending()) == 0

    def test_cleanup_failure_workflow(self):
        """Verify DELETE_PENDING → DELETING → CLEANUP_FAILED workflow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = RunRegistry(db_path)

            conv_id = str(uuid4())
            locator = "https://chatgpt.com/c/fail"

            registry.create_conversation(conv_id, locator, "delete_on_success")
            registry.release_conversation(conv_id, "success")

            # Attempt cleanup
            registry.start_cleanup(conv_id)
            conv = registry.get_conversation_lifecycle(conv_id)
            assert conv["status"] == "deleting"

            # Mark as failed
            registry.mark_cleanup_failed(conv_id, "menu_not_found", "Cannot find menu button")
            conv = registry.get_conversation_lifecycle(conv_id)
            assert conv["status"] == "cleanup_failed"
            assert conv["cleanup_attempt_count"] == 1
            assert conv["last_cleanup_error_code"] == "menu_not_found"
            assert len(registry.get_all_cleanup_failed()) == 1

    def test_cleanup_retry(self):
        """Verify retry increment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            registry = RunRegistry(db_path)

            conv_id = str(uuid4())
            registry.create_conversation(conv_id, "https://chatgpt.com/c/retry", "delete_on_success")
            registry.release_conversation(conv_id, "success")

            # Attempt 1
            registry.start_cleanup(conv_id)
            registry.mark_cleanup_failed(conv_id, "error", "msg")
            assert registry.get_conversation_lifecycle(conv_id)["cleanup_attempt_count"] == 1

            # Attempt 2
            registry.start_cleanup(conv_id)
            registry.mark_cleanup_failed(conv_id, "error", "msg")
            assert registry.get_conversation_lifecycle(conv_id)["cleanup_attempt_count"] == 2

            # Attempt 3
            registry.start_cleanup(conv_id)
            registry.mark_cleanup_failed(conv_id, "error", "msg")
            assert registry.get_conversation_lifecycle(conv_id)["cleanup_attempt_count"] == 3

    def test_content_js_selectors(self):
        """Verify content.js has cleanup selectors defined."""
        # This is a manual check - we'll import and verify dynamically
        import re

        content_js_path = Path(__file__).parent.parent / "extension" / "content.js"
        with open(content_js_path) as f:
            content = f.read()

        # Check for cleanup selectors
        assert "conversationOptionsButton" in content
        assert "deleteMenuItem" in content
        assert "confirmDeleteDialog" in content
        assert "confirmDeleteButton" in content

    def test_delete_conversation_function_exists(self):
        """Verify deleteConversation function is defined in content.js."""
        import re

        content_js_path = Path(__file__).parent.parent / "extension" / "content.js"
        with open(content_js_path) as f:
            content = f.read()

        # Check for function definition
        assert "async function deleteConversation(" in content
        assert "Vérifier le locator" in content or "locator_mismatch" in content

    def test_background_js_cleanup_handler(self):
        """Verify background.js has cleanup conversation handler."""
        background_js_path = Path(__file__).parent.parent / "extension" / "background.js"
        with open(background_js_path) as f:
            content = f.read()

        # Check for handler
        assert "handleCleanupConversation" in content
        assert "cleanup_conversation" in content

    def test_server_py_imports(self):
        """Verify server.py has all necessary imports."""
        from server import CleanupWorker, ConversationSweeper, bridge, run_registry

        # Verify they exist
        assert CleanupWorker is not None
        assert ConversationSweeper is not None
        assert bridge is not None
        assert run_registry is not None


class TestContentJsDeleteConversation:
    """Test the structure of deleteConversation function in content.js."""

    def test_delete_conversation_has_error_codes(self):
        """Verify deleteConversation returns proper error codes."""
        content_js_path = Path(__file__).parent.parent / "extension" / "content.js"
        with open(content_js_path) as f:
            content = f.read()

        error_codes = [
            "locator_mismatch",
            "conversation_menu_not_found",
            "menu_open_timeout",
            "delete_action_not_found",
            "confirm_dialog_timeout",
            "confirm_button_not_found",
            "internal_error",
        ]

        for code in error_codes:
            assert f'"{code}"' in content or f"'{code}'" in content

    def test_delete_conversation_returns_result_object(self):
        """Verify deleteConversation returns proper result structure."""
        content_js_path = Path(__file__).parent.parent / "extension" / "content.js"
        with open(content_js_path) as f:
            content = f.read()

        required_fields = [
            "success",
            "conversation_id",
            "verified_deleted",
            "error_code",
            "error_message",
            "steps_completed",
        ]

        for field in required_fields:
            assert field in content

    def test_delete_conversation_message_handler(self):
        """Verify content.js handles delete_conversation messages."""
        content_js_path = Path(__file__).parent.parent / "extension" / "content.js"
        with open(content_js_path) as f:
            content = f.read()

        # Check for message type handler
        assert 'type === "delete_conversation"' in content or "delete_conversation" in content


class TestBackgroundJsIntegration:
    """Test background.js cleanup integration."""

    def test_background_js_routes_cleanup(self):
        """Verify background.js routes cleanup_conversation messages."""
        background_js_path = Path(__file__).parent.parent / "extension" / "background.js"
        with open(background_js_path) as f:
            content = f.read()

        # Check routing
        assert 'msg.type === "cleanup_conversation"' in content
        assert "handleCleanupConversation" in content

    def test_background_js_sends_to_content(self):
        """Verify handler sends message to content script."""
        background_js_path = Path(__file__).parent.parent / "extension" / "background.js"
        with open(background_js_path) as f:
            content = f.read()

        # Check message passing
        assert "sendToTab" in content
        assert "resolveConversationTab" in content
