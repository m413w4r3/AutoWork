"""
Cleanup UI Automation — Incrément 2

Gère l'automatisation de la suppression de conversations via l'interface ChatGPT.
Utilise l'extension Chrome pour:
  1. Ouvrir la conversation via external_locator
  2. Vérifier l'identité (locator vs DOM)
  3. Exécuter le menu Delete
  4. Tracker les tentatives et les erreurs
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Callable, Optional
from uuid import UUID

logger = logging.getLogger("chatgpt_bridge.cleanup")

CLEANUP_UI_TIMEOUT = 30  # secondes max pour une action UI
CLEANUP_RETRY_BACKOFF = [1, 5, 30]  # délais retry (secondes)
CLEANUP_MAX_ATTEMPTS = 3


class CleanupErrorCode(StrEnum):
    """Codes d'erreur spécifiques au cleanup."""
    LOCATOR_INVALID = "locator_invalid"
    LOCATOR_MISMATCH = "locator_mismatch"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    DELETE_BUTTON_NOT_FOUND = "delete_button_not_found"
    DELETE_ACTION_FAILED = "delete_action_failed"
    TIMEOUT = "timeout"
    EXTENSION_DISCONNECTED = "extension_disconnected"
    INTERNAL_ERROR = "internal_error"


@dataclass(slots=True)
class CleanupTask:
    """Tâche de cleanup pour une conversation DELETE_PENDING."""
    conversation_id: UUID
    external_locator: str  # URL ChatGPT.com
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    attempts: int = 0
    last_error: Optional[CleanupErrorCode] = None
    last_error_message: Optional[str] = None

    def next_retry_delay(self) -> Optional[float]:
        """Délai avant la prochaine tentative, ou None si max atteint."""
        if self.attempts >= CLEANUP_MAX_ATTEMPTS:
            return None
        return CLEANUP_RETRY_BACKOFF[min(self.attempts, len(CLEANUP_RETRY_BACKOFF) - 1)]

    def record_attempt(
        self,
        success: bool,
        error_code: Optional[CleanupErrorCode] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Enregistre le résultat d'une tentative."""
        self.attempts += 1
        if not success:
            self.last_error = error_code
            self.last_error_message = error_message


class CleanupRequest(BaseModel):
    """Requête d'automatisation UI: ouvrir une conversation et la supprimer."""
    task_id: str  # UUID
    external_locator: str
    action: str = "delete_conversation"  # extensible: verify_identity, navigate, etc.
    timeout: float = CLEANUP_UI_TIMEOUT


class CleanupResponse(BaseModel):
    """Réponse d'une action de cleanup."""
    task_id: str
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    verified_title: Optional[str] = None
    dom_snapshot: Optional[dict[str, Any]] = None  # Debug: état DOM avant action


@dataclass(slots=True)
class CleanupAutomation:
    """Orchestrateur du cleanup via l'extension."""

    ws_send_message: Callable[[str, dict[str, Any]], asyncio.Future]
    """Fonction pour envoyer un message via WebSocket à l'extension."""

    async def execute_cleanup(self, task: CleanupTask) -> tuple[bool, Optional[CleanupErrorCode]]:
        """Exécute une tâche de cleanup (ouvrir + vérifier + supprimer)."""
        try:
            # Étape 1: Ouvrir la conversation
            logger.info(f"[Cleanup {task.conversation_id}] Opening conversation...")
            verify_result = await self._step_verify_identity(task)
            if not verify_result["success"]:
                error_code = verify_result.get("error_code", CleanupErrorCode.INTERNAL_ERROR)
                logger.warning(
                    f"[Cleanup {task.conversation_id}] Identity verification failed: {error_code}"
                )
                task.record_attempt(
                    success=False,
                    error_code=error_code,
                    error_message=verify_result.get("error_message"),
                )
                return False, error_code

            # Étape 2: Exécuter le delete
            logger.info(f"[Cleanup {task.conversation_id}] Executing delete action...")
            delete_result = await self._step_delete_conversation(task)
            if not delete_result["success"]:
                error_code = delete_result.get("error_code", CleanupErrorCode.INTERNAL_ERROR)
                logger.warning(
                    f"[Cleanup {task.conversation_id}] Delete action failed: {error_code}"
                )
                task.record_attempt(
                    success=False,
                    error_code=error_code,
                    error_message=delete_result.get("error_message"),
                )
                return False, error_code

            # Succès!
            logger.info(f"[Cleanup {task.conversation_id}] ✓ Successfully deleted")
            task.record_attempt(success=True)
            return True, None

        except asyncio.TimeoutError:
            logger.error(f"[Cleanup {task.conversation_id}] Timeout")
            task.record_attempt(
                success=False,
                error_code=CleanupErrorCode.TIMEOUT,
                error_message="Cleanup action timed out",
            )
            return False, CleanupErrorCode.TIMEOUT
        except Exception as e:
            logger.exception(f"[Cleanup {task.conversation_id}] Unexpected error: {e}")
            task.record_attempt(
                success=False,
                error_code=CleanupErrorCode.INTERNAL_ERROR,
                error_message=str(e),
            )
            return False, CleanupErrorCode.INTERNAL_ERROR

    async def _step_verify_identity(self, task: CleanupTask) -> dict[str, Any]:
        """Étape 1: Ouvrir conversation et vérifier l'identité via DOM."""
        try:
            response = await self._send_ui_command(
                task_id=str(task.conversation_id),
                action="verify_identity",
                locator=task.external_locator,
                timeout=CLEANUP_UI_TIMEOUT,
            )

            if response.get("verified"):
                logger.debug(f"[Cleanup {task.conversation_id}] Identity verified: {response.get('title')}")
                return {"success": True}
            else:
                return {
                    "success": False,
                    "error_code": CleanupErrorCode.LOCATOR_MISMATCH,
                    "error_message": f"Identity mismatch: {response.get('reason')}",
                }
        except Exception as e:
            return {
                "success": False,
                "error_code": CleanupErrorCode.INTERNAL_ERROR,
                "error_message": str(e),
            }

    async def _step_delete_conversation(self, task: CleanupTask) -> dict[str, Any]:
        """Étape 2: Exécuter l'action delete via le menu."""
        try:
            response = await self._send_ui_command(
                task_id=str(task.conversation_id),
                action="delete_conversation",
                locator=task.external_locator,
                timeout=CLEANUP_UI_TIMEOUT,
            )

            if response.get("deleted"):
                logger.debug(f"[Cleanup {task.conversation_id}] Conversation deleted")
                return {"success": True}
            else:
                return {
                    "success": False,
                    "error_code": CleanupErrorCode.DELETE_ACTION_FAILED,
                    "error_message": response.get("reason", "Delete action failed"),
                }
        except Exception as e:
            return {
                "success": False,
                "error_code": CleanupErrorCode.INTERNAL_ERROR,
                "error_message": str(e),
            }

    async def _send_ui_command(
        self,
        task_id: str,
        action: str,
        locator: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Envoie une commande UI à l'extension et attend la réponse."""
        command = {
            "type": "cleanup_command",
            "task_id": task_id,
            "action": action,
            "locator": locator,
            "timeout": timeout,
        }

        try:
            # Envoyer via WebSocket et attendre réponse
            future = self.ws_send_message("cleanup", command)
            result = await asyncio.wait_for(future, timeout=timeout + 2)
            return result
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(f"UI command timeout: {action}")


from pydantic import BaseModel

