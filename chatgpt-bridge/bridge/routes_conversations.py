"""Routes conversation : ferme l'onglet local d'une conversation bridge.

Encapsule l'endpoint conversation sous un propriétaire explicite.
"""

import logging
import uuid
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException

from bridge.transport import Bridge
from bridge.ui import _ui_roundtrip

logger = logging.getLogger("chatgpt_bridge")


class ConversationRoutes:
    """Propriétaire de l'endpoint conversation.

    Ne détient aucun état métier propre : `bridge` est une instance injectée
    par BridgeApplication, `router` est l'APIRouter à monter sur
    l'application FastAPI.
    """

    def __init__(
        self,
        *,
        bridge: Bridge,
        auth_dependency: Callable[..., Any],
    ) -> None:
        self.bridge = bridge
        self.router = APIRouter(dependencies=[Depends(auth_dependency)])

        self.router.add_api_route(
            "/v1/bridge/conversations/{conversation_id}",
            self.archive_bridge_conversation,
            methods=["DELETE"],
        )

    async def archive_bridge_conversation(self, conversation_id: uuid.UUID):
        # NOTE: this endpoint only closes the local browser tab and drops the
        # extension's in-memory conversation_id -> tab mapping (see background.js
        # handleConversationArchive). That is sufficient because every conversation
        # the bridge opens fresh is put in ChatGPT's "Temporary chat" mode by
        # content.js's ensureTemporaryChat() before the first prompt is sent — it is
        # never written to ChatGPT's own history, so there is nothing left to delete
        # server-side once the tab is gone.
        logger.info("conversation_archive_requested conversation_id=%s", conversation_id)
        if not self.bridge.online:
            logger.warning(
                "conversation_archive_failed conversation_id=%s reason=extension_disconnected",
                conversation_id,
            )
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
        archived = packet.get("ok") is True
        logger.info(
            "conversation_archive_completed conversation_id=%s tab_closed=%s",
            conversation_id,
            archived,
        )
        return {"archived": archived, "conversation_id": str(conversation_id)}
