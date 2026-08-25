"""Routes conversation: archive UI, release, lifecycle, cleanup start/complete/fail.

Encapsule les endpoints conversation sous un propriétaire explicite.
"""

import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from bridge.contracts import (
    CleanupFailureRequest,
    CleanupStartResponse,
    ConversationLifecycleResponse,
    ConversationReleaseRequest,
)
from bridge.lifecycle import TERMINAL_IDENTITY_ERROR_CODES
from bridge.registry import RunRegistry
from bridge.transport import Bridge
from bridge.ui import _ui_roundtrip


class ConversationRoutes:
    """Propriétaire des six endpoints conversation.

    Ne détient aucun état métier propre : `bridge` et `registry` sont des
    instances injectées par BridgeApplication, `router` est l'APIRouter à
    monter sur l'application FastAPI.
    """

    def __init__(
        self,
        *,
        bridge: Bridge,
        registry: RunRegistry,
        auth_dependency: Callable[..., Any],
    ) -> None:
        self.bridge = bridge
        self.registry = registry
        self.router = APIRouter(dependencies=[Depends(auth_dependency)])

        self.router.add_api_route(
            "/v1/bridge/conversations/{conversation_id}",
            self.archive_bridge_conversation,
            methods=["DELETE"],
        )
        self.router.add_api_route(
            "/v1/conversations/{conversation_id}/release",
            self.release_conversation,
            methods=["POST"],
            response_model=ConversationLifecycleResponse,
        )
        self.router.add_api_route(
            "/v1/conversations/{conversation_id}/lifecycle",
            self.get_conversation_lifecycle,
            methods=["GET"],
            response_model=ConversationLifecycleResponse,
        )
        self.router.add_api_route(
            "/v1/conversations/{conversation_id}/cleanup/start",
            self.start_conversation_cleanup,
            methods=["POST"],
            response_model=CleanupStartResponse,
        )
        self.router.add_api_route(
            "/v1/conversations/{conversation_id}/cleanup/complete",
            self.mark_conversation_deleted,
            methods=["POST"],
            response_model=ConversationLifecycleResponse,
        )
        self.router.add_api_route(
            "/v1/conversations/{conversation_id}/cleanup/fail",
            self.mark_conversation_cleanup_failed,
            methods=["POST"],
            response_model=ConversationLifecycleResponse,
        )

    async def archive_bridge_conversation(self, conversation_id: uuid.UUID):
        if not self.bridge.online:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "bridge_extension_disconnected",
                    "message": "Extension Chrome non connectée.",
                    "retryable": True,
                },
            )
        packet = await _ui_roundtrip(
            self.bridge,
            {"type": "conversation_archive", "conversation_id": str(conversation_id)},
        )
        return {"archived": packet.get("ok") is True, "conversation_id": str(conversation_id)}

    async def release_conversation(
        self,
        conversation_id: str,
        req: ConversationReleaseRequest,
    ) -> ConversationLifecycleResponse:
        """Release a conversation with an explicit outcome.

        Only the application client can decide when a conversation is no longer needed
        and what the outcome of that release is. The bridge applies the lifecycle policy
        only after this explicit signal.

        Outcome can be: success, failure, needs_review, or cancelled.
        Only 'success' may trigger automatic cleanup based on the conversation's policy.
        """
        try:
            result = self.registry.release_conversation(conversation_id, req.outcome)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        return ConversationLifecycleResponse(
            conversation_id=result["id"],
            policy=result["policy"],
            status=result["status"],
            release_outcome=result["release_outcome"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            released_at=result["released_at"],
            deleted_at=result["deleted_at"],
            cleanup_attempt_count=result["cleanup_attempt_count"],
            last_cleanup_attempt_at=result["last_cleanup_attempt_at"],
            last_cleanup_error_code=result["last_cleanup_error_code"],
            version=result["version"],
        )

    async def get_conversation_lifecycle(
        self,
        conversation_id: str,
    ) -> ConversationLifecycleResponse:
        """Retrieve the current lifecycle status of a conversation.

        This allows clients to query the current state, released_at timestamp,
        release outcome, cleanup status, and retry information.
        """
        result = self.registry.get_conversation_lifecycle(conversation_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        return ConversationLifecycleResponse(
            conversation_id=result["id"],
            policy=result["policy"],
            status=result["status"],
            release_outcome=result["release_outcome"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            released_at=result["released_at"],
            deleted_at=result["deleted_at"],
            cleanup_attempt_count=result["cleanup_attempt_count"],
            last_cleanup_attempt_at=result["last_cleanup_attempt_at"],
            last_cleanup_error_code=result["last_cleanup_error_code"],
            version=result["version"],
        )

    async def start_conversation_cleanup(
        self,
        conversation_id: str,
    ) -> CleanupStartResponse:
        """Initiate cleanup of a DELETE_PENDING conversation.

        This transitions the conversation from DELETE_PENDING to DELETING state
        and triggers the extension to open and delete the conversation via UI.

        Idempotent: calling again on DELETING or DELETED returns current state.

        Fail-closed: a CLEANUP_FAILED conversation whose last error is a terminal
        identity error (locator_mismatch/locator_invalid) is refused with 409 and
        left unchanged. No heuristic re-resolution and no override are permitted.
        """
        current = self.registry.get_conversation_lifecycle(conversation_id)
        if current is None:
            raise HTTPException(status_code=404, detail=f"Conversation not found: {conversation_id}")

        if current["status"] == "cleanup_failed" and current.get(
            "last_cleanup_error_code"
        ) in TERMINAL_IDENTITY_ERROR_CODES:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "cleanup_terminal_identity_error",
                    "message": (
                        "Cleanup cannot be retried: last failure was a terminal "
                        f"identity error ({current.get('last_cleanup_error_code')})."
                    ),
                    "retryable": False,
                },
            )

        try:
            result = self.registry.start_cleanup(conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        return CleanupStartResponse(
            conversation_id=result["id"],
            status=result["status"],
            cleanup_attempt_count=result["cleanup_attempt_count"],
        )

    async def mark_conversation_deleted(
        self,
        conversation_id: str,
    ) -> ConversationLifecycleResponse:
        """Mark a conversation as successfully deleted.

        Called by the extension after successfully deleting via UI.
        Idempotent: calling on already-DELETED returns current state.
        """
        try:
            result = self.registry.mark_conversation_deleted(conversation_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        return ConversationLifecycleResponse(
            conversation_id=result["id"],
            policy=result["policy"],
            status=result["status"],
            release_outcome=result["release_outcome"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            released_at=result["released_at"],
            deleted_at=result["deleted_at"],
            cleanup_attempt_count=result["cleanup_attempt_count"],
            last_cleanup_attempt_at=result["last_cleanup_attempt_at"],
            last_cleanup_error_code=result["last_cleanup_error_code"],
            version=result["version"],
        )

    async def mark_conversation_cleanup_failed(
        self,
        conversation_id: str,
        req: CleanupFailureRequest,
    ) -> ConversationLifecycleResponse:
        """Report cleanup failure and mark conversation CLEANUP_FAILED for retry.

        Transient cleanup failures may be retried while cleanup_attempt_count < 3;
        terminal identity errors are never retried.
        Idempotent: calling again increments attempt count.
        """
        try:
            result = self.registry.mark_cleanup_failed(
                conversation_id,
                error_code=req.error_code,
                error_message=req.error_message,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

        return ConversationLifecycleResponse(
            conversation_id=result["id"],
            policy=result["policy"],
            status=result["status"],
            release_outcome=result["release_outcome"],
            created_at=result["created_at"],
            updated_at=result["updated_at"],
            released_at=result["released_at"],
            deleted_at=result["deleted_at"],
            cleanup_attempt_count=result["cleanup_attempt_count"],
            last_cleanup_attempt_at=result["last_cleanup_attempt_at"],
            last_cleanup_error_code=result["last_cleanup_error_code"],
            version=result["version"],
        )
