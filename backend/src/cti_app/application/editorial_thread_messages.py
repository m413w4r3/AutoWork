"""Persist editorial message events and their durable Bridge jobs."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from cti_app.application.editorial_thread_common import is_valid_editorial_idempotency_key
from cti_app.application.editorial_thread_types import (
    EditorialThreadApplicationError,
    EditorialThreadSendResult,
)
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial_thread import (
    EditorialBindingState,
    EditorialBridgeStatus,
    EditorialThread,
    EditorialThreadActiveJobConflict,
    EditorialThreadBinding,
    EditorialThreadJobState,
    EditorialThreadLockTimeout,
)
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.model_conversations import ConversationPurpose, ConversationTransport
from cti_app.domain.model_runs import ModelProvider
from cti_app.infrastructure.database.repositories.editorial_threads import (
    translate_enqueue_database_error,
)

logger = logging.getLogger(__name__)


class EditorialThreadMessages:
    """Own message enqueue and idempotent send replay."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialWorkspaceService,
        artifact_store: ProductionArtifactStore,
        model_conversations: ModelConversationService | None,
        enqueue_job: Callable[[UUID], None] | None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace = workspace_service
        self._artifact_store = artifact_store
        self._model_conversations = model_conversations
        self._enqueue_job = enqueue_job

    async def send_message(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        text: str,
        actor_id: UUID,
        idempotency_key: str,
    ) -> EditorialThreadSendResult:
        """Append a message and durable job after resolving the current occurrence."""
        if not text.strip():
            raise EditorialThreadApplicationError("editorial_message_invalid")
        if not is_valid_editorial_idempotency_key(idempotency_key):
            raise EditorialThreadApplicationError("idempotency_key_invalid")
        payload_digest = hashlib.sha256(
            json.dumps(
                {"kind": "message", "text": text},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        try:
            resolution = await self._workspace.resolve_occurrence(subject_id, occurrence_id)
        except EditorialWorkspaceError as error:
            code = error.code if error.code != "editorial_workspace_error" else str(error)
            if code == "subject_not_found":
                raise EditorialThreadApplicationError("thread_not_found") from None
            raise EditorialThreadApplicationError("thread_not_found") from None
        if (
            resolution.entry is None
            or resolution.binding_state != EditorialBindingState.VALID.value
            or resolution.artifact_id is None
            or resolution.artifact_version is None
            or resolution.canonical_sha256 is None
        ):
            raise EditorialThreadApplicationError("thread_not_found")

        binding = EditorialThreadBinding(
            subject_id=resolution.record.subject_id,
            production_run_id=resolution.record.production_run_id,
            occurrence_id=resolution.entry.block_id,
            artifact_id=resolution.artifact_id,
            artifact_version=resolution.artifact_version,
            canonical_sha256=resolution.canonical_sha256,
        )
        async with self._uow_factory() as uow:
            record = await uow.editorial_threads.get_current_by_occurrence(
                subject_id, binding.production_run_id, occurrence_id
            )
            if record is not None:
                existing = next(
                    (event for event in record.events if event.idempotency_key == idempotency_key),
                    None,
                )
                if existing is not None:
                    if existing.request_sha256 != payload_digest:
                        raise EditorialThreadApplicationError("idempotency_conflict")
                    job = await uow.editorial_threads.get_job_for_event(existing.id)
                    if job is None:
                        raise EditorialThreadApplicationError("editorial_thread_job_missing")
                    return EditorialThreadSendResult(
                        kind="already_known",
                        thread_id=record.thread.id,
                        entry_id=existing.id,
                        generation=job.state,
                    )
                latest_job = await uow.editorial_threads.latest_job_for_thread(record.thread.id)
                if latest_job is not None and latest_job.state in {
                    EditorialThreadJobState.RECONCILIATION_REQUIRED,
                }:
                    raise EditorialThreadApplicationError("reconciliation_required")
                if latest_job is not None and latest_job.state in {
                    EditorialThreadJobState.QUEUED,
                    EditorialThreadJobState.WAITING_FOR_CAPACITY,
                    EditorialThreadJobState.RUNNING,
                }:
                    raise EditorialThreadApplicationError("editorial_thread_busy")
                if record.thread.bridge_status is EditorialBridgeStatus.SESSION_LOST:
                    raise EditorialThreadApplicationError("session_lost")
                if record.thread.model_conversation_id is None:
                    raise EditorialThreadApplicationError("editorial_thread_conversation_missing")
                thread = record.thread
                conversation_id = thread.model_conversation_id
            else:
                thread_id = uuid5(
                    NAMESPACE_URL,
                    "editorial-thread:v1:"
                    f"{subject_id}:{binding.production_run_id}:{occurrence_id}:1",
                )
                conversation_id = uuid5(NAMESPACE_URL, f"editorial-thread-conversation:{thread_id}")
                thread = EditorialThread(
                    id=thread_id,
                    origin=binding,
                    model_conversation_id=conversation_id,
                    bridge_status=EditorialBridgeStatus.READY,
                    next_sequence=1,
                    version=1,
                    session_number=1,
                )

        if self._model_conversations is None:
            raise EditorialThreadApplicationError("editorial_thread_service_unavailable")
        if thread.model_conversation_id is None:
            raise EditorialThreadApplicationError("editorial_thread_conversation_missing")
        conversation_id = thread.model_conversation_id
        try:
            conversation = await self._model_conversations.get_or_create(
                conversation_id,
                provider=ModelProvider.OPENAI,
                transport=ConversationTransport.CHATGPT_BRIDGE,
                purpose=ConversationPurpose.EDITORIAL_DISCUSSION,
                title="Discussion éditoriale",
                edition_id=None,
                subject_id=subject_id,
                expected_profile=None,
                requested_model=None,
                _editorial_thread_id=thread.id,
            )
        except Exception:
            raise EditorialThreadApplicationError("editorial_thread_service_unavailable") from None
        if (
            conversation.id != conversation_id
            or conversation.subject_id != subject_id
            or conversation.purpose is not ConversationPurpose.EDITORIAL_DISCUSSION
            or conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
        ):
            raise EditorialThreadApplicationError("thread_not_found")

        if record is None:
            async with self._uow_factory() as uow:
                try:
                    thread = await uow.editorial_threads.create_if_absent(thread)
                    await uow.commit()
                except Exception:
                    raise EditorialThreadApplicationError("thread_not_found") from None

        body = text.encode("utf-8")
        try:
            blob_id = await self._artifact_store.put_bytes(
                body,
                bucket="editorial-thread-events",
                mime_type="text/plain; charset=utf-8",
            )
        except Exception:
            raise EditorialThreadApplicationError("thread_body_unavailable") from None
        job_id = uuid4()
        async with self._uow_factory() as uow:
            try:
                event, job, already_known = await uow.editorial_threads.enqueue_message(
                    job_id=job_id,
                    thread_id=thread.id,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_sha256=payload_digest,
                    payload_blob_ref=blob_id,
                    payload_sha256=hashlib.sha256(body).hexdigest(),
                )
                await uow.commit()
            except Exception as error:
                database_error = translate_enqueue_database_error(error)
                if isinstance(
                    error,
                    (EditorialThreadActiveJobConflict, EditorialThreadLockTimeout),
                ) or isinstance(
                    database_error,
                    (EditorialThreadActiveJobConflict, EditorialThreadLockTimeout),
                ):
                    try:
                        await uow.rollback()
                    except Exception:
                        raise EditorialThreadApplicationError(
                            "editorial_thread_service_unavailable"
                        ) from None
                    raise EditorialThreadApplicationError("editorial_thread_busy") from None
                code = getattr(error, "code", "")
                if code in {"", "editorial_thread_error"}:
                    code = str(error)
                if code in {
                    "editorial_thread_busy",
                    "reconciliation_required",
                    "idempotency_conflict",
                    "thread_not_found",
                }:
                    raise EditorialThreadApplicationError(code) from None
                raise EditorialThreadApplicationError(
                    "editorial_thread_service_unavailable"
                ) from None

        if not already_known and self._enqueue_job is not None:
            try:
                self._enqueue_job(job.id)
            except Exception as error:
                logger.warning(
                    "editorial_thread_job_dispatch_failed job_id=%s error_type=%s",
                    job.id,
                    type(error).__name__,
                )
        return EditorialThreadSendResult(
            kind="already_known" if already_known else "accepted",
            thread_id=thread.id,
            entry_id=event.id,
            generation=job.state,
        )
