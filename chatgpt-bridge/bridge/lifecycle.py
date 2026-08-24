"""Lifecycle de cleanup : CleanupWorker, ConversationSweeper (MOVE-ONLY depuis server.py)."""

import asyncio
import logging
import uuid

from bridge.registry import RunRegistry
from bridge.transport import Bridge

# Erreurs d'identité terminales (fail-closed) : un mismatch/absence de
# external_locator ne doit jamais être retenté automatiquement, par aucun
# chemin (sweeper, worker direct, endpoint HTTP).
# Voir chatgpt-bridge/AGENTS.md — "Destructive actions — fail closed".
TERMINAL_IDENTITY_ERROR_CODES = frozenset({"locator_mismatch", "locator_invalid"})


class CleanupWorker:
    """Traite les conversations DELETE_PENDING (et CLEANUP_FAILED retryables) via l'UI."""

    def __init__(self, registry: RunRegistry, bridge: "Bridge"):
        self.registry = registry
        self.bridge = bridge
        self.logger = logging.getLogger("cleanup_worker")

    async def process_cleanup_task(self, conversation_id: str) -> bool:
        """
        Exécute le cleanup d'une conversation:
        1. Récupère l'état de la conversation
        2. Valide le status (DELETE_PENDING ou CLEANUP_FAILED retryable)
        3. Marque comme DELETING
        4. Envoie une requête à l'extension
        5. Attend la réponse
        6. Marque comme DELETED ou CLEANUP_FAILED

        @param conversation_id: UUID de la conversation
        @return: True si succès, False sinon
        """
        try:
            # 1. Charger la conversation
            conv = self.registry.get_conversation_lifecycle(conversation_id)
            if not conv:
                self.logger.warning(f"Conversation {conversation_id} not found")
                return False

            if conv["status"] not in ("delete_pending", "cleanup_failed"):
                self.logger.warning(
                    f"Conversation {conversation_id} not in DELETE_PENDING/CLEANUP_FAILED, "
                    f"status={conv['status']}"
                )
                return False

            # Fail-closed : un CLEANUP_FAILED avec erreur d'identité terminale
            # (locator_mismatch/locator_invalid) n'est jamais retenté, même en
            # appelant le worker directement. Aucune requête Bridge, aucun
            # start_cleanup, aucun changement de status.
            if conv["status"] == "cleanup_failed" and conv.get(
                "last_cleanup_error_code"
            ) in TERMINAL_IDENTITY_ERROR_CODES:
                self.logger.warning(
                    f"Refusing retry for {conversation_id}: terminal identity error "
                    f"({conv.get('last_cleanup_error_code')})"
                )
                return False

            # 2. Vérifier le locator
            if not conv.get("external_locator"):
                self.logger.warning(f"Conversation {conversation_id} missing external_locator")
                self.registry.mark_cleanup_failed(
                    conversation_id, "locator_invalid", "Missing external_locator"
                )
                return False

            # 3. Marquer comme DELETING
            self.registry.start_cleanup(conversation_id)

            # 4. Envoyer au worker
            result = await self._send_cleanup_request(
                conversation_id=conversation_id,
                external_locator=conv["external_locator"],
                timeout=30,
            )

            # 5. Traiter le résultat
            if result["success"] and result.get("verified_deleted"):
                self.registry.mark_conversation_deleted(conversation_id)
                self.logger.info(
                    f"Conversation {conversation_id} deleted successfully. "
                    f"Steps: {result.get('steps_completed', [])}"
                )
                return True
            else:
                error_code = result.get("error_code", "unknown")
                error_msg = result.get("error_message", "No error message")
                self.registry.mark_cleanup_failed(
                    conversation_id,
                    error_code,
                    error_msg,
                )
                self.logger.warning(
                    f"Cleanup failed for {conversation_id}: {error_code} - {error_msg}"
                )
                return False
        except Exception as e:
            self.logger.error(f"Cleanup error for {conversation_id}: {e}", exc_info=True)
            try:
                self.registry.mark_cleanup_failed(
                    conversation_id,
                    "internal_error",
                    str(e),
                )
            except Exception as e2:
                self.logger.error(f"Failed to mark cleanup failed: {e2}")
            return False

    async def _send_cleanup_request(
        self, conversation_id: str, external_locator: str, timeout: int
    ) -> dict:
        """Envoie la requête de cleanup à l'extension via WebSocket."""
        message = {
            "type": "cleanup_conversation",
            "id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "external_locator": external_locator,
            "timeout": timeout,
        }

        # Utiliser le mécanisme d'aller-retour du bridge
        try:
            response = await self.bridge.request(message, timeout=timeout + 10)
            return response
        except asyncio.TimeoutError:
            self.logger.warning(f"Cleanup timeout for {conversation_id}")
            return {"success": False, "error_code": "timeout", "verified_deleted": False}
        except RuntimeError as e:
            if "non connectée" in str(e):
                self.logger.warning(f"Extension not connected during cleanup: {conversation_id}")
                return {"success": False, "error_code": "extension_disconnected", "verified_deleted": False}
            raise
        except Exception as e:
            self.logger.error(f"Error during cleanup request: {e}")
            return {
                "success": False,
                "error_code": "internal_error",
                "error_message": str(e),
                "verified_deleted": False,
            }


class ConversationSweeper:
    """Reprend les cleanups après un restart."""

    def __init__(self, registry: RunRegistry, worker: CleanupWorker):
        self.registry = registry
        self.worker = worker
        self.logger = logging.getLogger("conversation_sweeper")

    async def sweep(self):
        """Trouve et traite DELETE_PENDING après restart."""
        pending = self.registry.get_all_delete_pending()
        self.logger.info(f"Sweeping {len(pending)} DELETE_PENDING conversations")

        for conv_id in pending:
            try:
                await self.worker.process_cleanup_task(conv_id)
            except Exception as e:
                self.logger.error(f"Sweep error for {conv_id}: {e}", exc_info=True)
            await asyncio.sleep(0.5)  # Petit délai entre les tentatives

    async def retry_failed(self):
        """Retry les CLEANUP_FAILED."""
        failed = self.registry.get_all_cleanup_failed()
        self.logger.info(f"Retrying {len(failed)} CLEANUP_FAILED conversations")

        for conv_id in failed:
            try:
                conv = self.registry.get_conversation_lifecycle(conv_id)
                if not conv:
                    continue
                error_code = conv.get("last_cleanup_error_code")
                if error_code in TERMINAL_IDENTITY_ERROR_CODES:
                    self.logger.warning(
                        f"Not retrying {conv_id}: terminal identity error ({error_code})"
                    )
                    continue
                if conv.get("cleanup_attempt_count", 0) < 3:
                    await self.worker.process_cleanup_task(conv_id)
            except Exception as e:
                self.logger.error(f"Retry error for {conv_id}: {e}", exc_info=True)
            await asyncio.sleep(0.5)
