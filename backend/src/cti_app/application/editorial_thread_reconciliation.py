"""Resolve and durably record an exact uncertain editorial thread turn."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from cti_app.application.editorial_thread_common import (
    _binding_from_resolution,
    _resolve_current,
    _same_thread_scope,
    is_valid_editorial_idempotency_key,
)
from cti_app.application.editorial_thread_types import (
    EditorialThreadApplicationError,
    EditorialThreadReconciliationResult,
)
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadJobState,
    EditorialThreadReconciliationOutcome,
)

_RECONCILIATION_MESSAGES = {
    EditorialThreadReconciliationOutcome.RECONCILED_REPLY: (
        "La réponse récupérée a été ajoutée à la discussion."
    ),
    EditorialThreadReconciliationOutcome.NOT_SUBMITTED: (
        "Le message n’a pas été reçu… Vous pouvez en envoyer un nouveau."  # noqa: RUF001
    ),
    EditorialThreadReconciliationOutcome.STILL_UNKNOWN: (
        "L’état de l’envoi reste incertain. La discussion demeure verrouillée."  # noqa: RUF001
    ),
    EditorialThreadReconciliationOutcome.SESSION_LOST: (
        "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre."
    ),
}


class EditorialThreadReconciliation:
    """Own exact-turn replay, Bridge readback and reconciliation event writes."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialWorkspaceService,
        artifact_store: ProductionArtifactStore,
        model_conversations: ModelConversationService | None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace = workspace_service
        self._artifact_store = artifact_store
        self._model_conversations = model_conversations

    async def reconcile(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        actor_id: UUID,
        idempotency_key: str,
    ) -> EditorialThreadReconciliationResult:
        """Read and record reconciliation for the exact blocked message turn."""
        if not is_valid_editorial_idempotency_key(idempotency_key):
            raise EditorialThreadApplicationError("idempotency_key_invalid")
        resolution = await _resolve_current(self._workspace, subject_id, occurrence_id)
        binding = _binding_from_resolution(resolution)
        async with self._uow_factory() as uow:
            record = await uow.editorial_threads.get_current_by_occurrence(
                subject_id, binding.production_run_id, occurrence_id
            )
            if record is None:
                raise EditorialThreadApplicationError("thread_not_found")
            if not _same_thread_scope(record.thread, binding):
                raise EditorialThreadApplicationError("thread_not_found")
            job = await uow.editorial_threads.latest_job_for_thread(record.thread.id)
            if job is None:
                raise EditorialThreadApplicationError("reconciliation_required")
            send_event = next(
                (event for event in record.events if event.id == job.send_event_id), None
            )
            if send_event is None or send_event.kind is not EditorialThreadEventKind.USER_MESSAGE:
                raise EditorialThreadApplicationError("reconciliation_required")
            thread = record.thread
            conversation_id = thread.model_conversation_id

        if conversation_id is None or self._model_conversations is None:
            raise EditorialThreadApplicationError("reconciliation_required")
        turn_idempotency_key = f"editorial-thread-message:{send_event.id}"
        try:
            turn_content = await self._model_conversations.turn_content_by_idempotency_key(
                conversation_id,
                turn_idempotency_key,
                context_subject_id=subject_id,
                editorial_thread_id=thread.id,
            )
        except Exception:
            raise EditorialThreadApplicationError("reconciliation_required") from None
        if turn_content is None:
            raise EditorialThreadApplicationError("reconciliation_required")
        turn_id = turn_content.turn.id
        digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": "reconcile",
                    "subject_id": str(subject_id),
                    "occurrence_id": str(occurrence_id),
                    "target": {
                        "job_id": str(job.id),
                        "send_event_id": str(send_event.id),
                        "turn_id": str(turn_id),
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self._uow_factory() as uow:
            current = await uow.editorial_threads.get_current_by_occurrence(
                subject_id, binding.production_run_id, occurrence_id
            )
            if current is None or current.thread.id != thread.id:
                raise EditorialThreadApplicationError("thread_not_found")
            current_job = await uow.editorial_threads.latest_job_for_thread(thread.id)
            current_send_event = next(
                (event for event in current.events if event.id == job.send_event_id),
                None,
            )
            if (
                current_job is None
                or current_job.id != job.id
                or current_send_event is None
                or current_send_event.id != send_event.id
            ):
                raise EditorialThreadApplicationError("reconciliation_required")
            existing = next(
                (event for event in current.events if event.idempotency_key == idempotency_key),
                None,
            )
            if existing is not None:
                if (
                    existing.kind is not EditorialThreadEventKind.RECONCILIATION
                    or existing.request_sha256 != digest
                ):
                    raise EditorialThreadApplicationError("idempotency_conflict")
                return await _reconciliation_result_from_event(
                    current.thread.id, existing, self._artifact_store
                )
            if current_job.state is not EditorialThreadJobState.RECONCILIATION_REQUIRED:
                raise EditorialThreadApplicationError("reconciliation_required")
        try:
            turn_result = await self._model_conversations._reconcile_editorial_thread_turn(
                conversation_id,
                thread_id=thread.id,
                turn_idempotency_key=turn_idempotency_key,
                context_subject_id=subject_id,
            )
        except Exception:
            turn_result = None
        if turn_result is not None and turn_result.turn_id not in (None, turn_id):
            turn_result = None
        outcome = (
            turn_result.outcome
            if turn_result is not None
            else EditorialThreadReconciliationOutcome.STILL_UNKNOWN
        )
        message = _RECONCILIATION_MESSAGES[outcome]
        reply_blob: UUID | None = None
        reply_digest: str | None = None
        if outcome is EditorialThreadReconciliationOutcome.RECONCILED_REPLY:
            if turn_result is None or not turn_result.reply:
                outcome = EditorialThreadReconciliationOutcome.STILL_UNKNOWN
                message = _RECONCILIATION_MESSAGES[outcome]
            else:
                reply = turn_result.reply.encode("utf-8")
                reply_digest = hashlib.sha256(reply).hexdigest()
                reply_blob = await self._artifact_store.put_bytes(
                    reply, bucket="editorial-thread-events", mime_type="text/plain; charset=utf-8"
                )

        reconciliation_payload = json.dumps(
            {"outcome": outcome.value, "message": message},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload_digest = hashlib.sha256(reconciliation_payload).hexdigest()
        payload_blob = await self._artifact_store.put_bytes(
            reconciliation_payload,
            bucket="editorial-thread-events",
            mime_type="text/plain; charset=utf-8",
        )
        recorded_result: EditorialThreadReconciliationResult | None = None
        try:
            async with self._uow_factory() as uow:
                latest = await uow.editorial_threads.get_current_by_occurrence(
                    subject_id, binding.production_run_id, occurrence_id
                )
                if latest is None or latest.thread.id != thread.id:
                    raise EditorialThreadApplicationError("thread_not_found")
                raced = next(
                    (event for event in latest.events if event.idempotency_key == idempotency_key),
                    None,
                )
                if raced is not None:
                    if (
                        raced.kind is not EditorialThreadEventKind.RECONCILIATION
                        or raced.request_sha256 != digest
                    ):
                        raise EditorialThreadApplicationError("idempotency_conflict")
                    return await _reconciliation_result_from_event(
                        thread.id, raced, self._artifact_store
                    )
                latest_job = await uow.editorial_threads.latest_job_for_thread(thread.id)
                latest_send_event = next(
                    (event for event in latest.events if event.id == job.send_event_id),
                    None,
                )
                if (
                    latest_job is None
                    or latest_job.id != job.id
                    or latest_send_event is None
                    or latest_send_event.id != send_event.id
                    or latest_job.state is not EditorialThreadJobState.RECONCILIATION_REQUIRED
                ):
                    raise EditorialThreadApplicationError("reconciliation_required")
                if reply_blob is not None and reply_digest is not None and turn_result is not None:
                    await uow.editorial_threads.append_event(
                        thread_id=thread.id,
                        kind=EditorialThreadEventKind.BRIDGE_REPLY,
                        actor_id=None,
                        idempotency_key=f"editorial-thread-reconciled-reply:{job.id}",
                        request_sha256=hashlib.sha256(
                            f"reconciled-reply:{job.id}:{reply_digest}".encode()
                        ).hexdigest(),
                        payload_blob_ref=reply_blob,
                        payload_sha256=reply_digest,
                        model_turn_id=turn_id,
                    )
                recorded_event = await uow.editorial_threads.append_event(
                    thread_id=thread.id,
                    kind=EditorialThreadEventKind.RECONCILIATION,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_sha256=digest,
                    payload_blob_ref=payload_blob,
                    payload_sha256=payload_digest,
                    model_turn_id=turn_id,
                )
                recorded_result = await _reconciliation_result_from_event(
                    thread.id, recorded_event, self._artifact_store
                )
                recorded_job_state = (
                    EditorialThreadJobState.RECONCILIATION_REQUIRED
                    if recorded_result.outcome is EditorialThreadReconciliationOutcome.STILL_UNKNOWN
                    else EditorialThreadJobState.COMPLETE
                )
                recorded_bridge_status = {
                    EditorialThreadReconciliationOutcome.RECONCILED_REPLY: (
                        EditorialBridgeStatus.REPLY_AVAILABLE
                    ),
                    EditorialThreadReconciliationOutcome.NOT_SUBMITTED: EditorialBridgeStatus.READY,
                    EditorialThreadReconciliationOutcome.STILL_UNKNOWN: (
                        EditorialBridgeStatus.RECONCILIATION_REQUIRED
                    ),
                    EditorialThreadReconciliationOutcome.SESSION_LOST: (
                        EditorialBridgeStatus.SESSION_LOST
                    ),
                }[recorded_result.outcome]
                await uow.editorial_threads.set_job_state(job.id, recorded_job_state)
                await uow.editorial_threads.set_bridge_status(thread.id, recorded_bridge_status)
                await uow.commit()
        except EditorialThreadApplicationError:
            raise
        except Exception:
            raise EditorialThreadApplicationError("editorial_thread_service_unavailable") from None
        if recorded_result is None:
            raise EditorialThreadApplicationError("editorial_thread_service_unavailable")
        return recorded_result


async def _reconciliation_result_from_event(
    thread_id: UUID,
    event: EditorialThreadEvent,
    artifact_store: ProductionArtifactStore,
) -> EditorialThreadReconciliationResult:
    if event.payload_blob_ref is None or event.payload_sha256 is None:
        raise EditorialThreadApplicationError("thread_body_unavailable")
    try:
        body = await artifact_store.read_bytes(event.payload_blob_ref)
        if hashlib.sha256(body).hexdigest() != event.payload_sha256:
            raise EditorialThreadApplicationError("thread_body_hash_mismatch")
        data: object = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("payload_not_object")
        raw_outcome = data.get("outcome")
        message = data.get("message")
        if not isinstance(raw_outcome, str) or not isinstance(message, str):
            raise ValueError("payload_shape_invalid")
        outcome = EditorialThreadReconciliationOutcome(raw_outcome)
    except EditorialThreadApplicationError:
        raise
    except Exception:
        raise EditorialThreadApplicationError("thread_body_unavailable") from None
    return EditorialThreadReconciliationResult(thread_id, outcome, message)


def _reconciliation_outcome(body: bytes) -> EditorialThreadReconciliationOutcome:
    try:
        data: object = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("payload_not_object")
        return EditorialThreadReconciliationOutcome(data["outcome"])
    except Exception:
        raise EditorialThreadApplicationError("thread_body_unavailable") from None


def _reconciliation_message(body: bytes) -> str:
    try:
        data: object = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("payload_message_invalid")
        message = data.get("message")
        if not isinstance(message, str):
            raise ValueError("payload_message_invalid")
        return message
    except Exception:
        raise EditorialThreadApplicationError("thread_body_unavailable") from None
