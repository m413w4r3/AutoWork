"""Close durable editorial message jobs according to a typed worker outcome."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from cti_app.application.editorial_conversation_turns import (
    has_persisted_not_submitted_proof,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadJobState,
    EditorialThreadReconciliationOutcome,
)
from cti_app.domain.model_conversations import ConversationTurnStatus
from cti_app.domain.model_runs import ModelSubmissionState


@dataclass(frozen=True, slots=True)
class EditorialThreadReplyOutcome:
    thread_id: UUID
    send_event: EditorialThreadEvent
    turn_id: UUID
    reply: str


@dataclass(frozen=True, slots=True)
class EditorialThreadRefusalOutcome:
    reason_code: str
    message: str


@dataclass(frozen=True, slots=True)
class EditorialThreadStateOutcome:
    thread_id: UUID
    job_state: EditorialThreadJobState
    bridge_status: EditorialBridgeStatus
    retry_after_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class EditorialThreadFailureOutcome:
    thread_id: UUID


@dataclass(frozen=True, slots=True)
class EditorialThreadSessionLostOutcome:
    thread_id: UUID
    reason_code: str


type EditorialThreadJobOutcome = (
    EditorialThreadReplyOutcome
    | EditorialThreadRefusalOutcome
    | EditorialThreadStateOutcome
    | EditorialThreadFailureOutcome
    | EditorialThreadSessionLostOutcome
)


class EditorialThreadJobCompletion:
    """Persist one worker result and finish or safely defer its leased job."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def close(
        self,
        job_id: UUID,
        lease_token: UUID,
        outcome: EditorialThreadJobOutcome,
    ) -> None:
        if isinstance(outcome, EditorialThreadReplyOutcome):
            await self._reply(job_id, lease_token, outcome)
        elif isinstance(outcome, EditorialThreadRefusalOutcome):
            await self._refusal(job_id, lease_token, outcome)
        elif isinstance(outcome, EditorialThreadFailureOutcome):
            state = await self._submission_state_for_job(job_id)
            if state is not ModelSubmissionState.NOT_SUBMITTED:
                await self._set_state(
                    job_id,
                    outcome.thread_id,
                    lease_token,
                    EditorialThreadJobState.RECONCILIATION_REQUIRED,
                    EditorialBridgeStatus.RECONCILIATION_REQUIRED,
                )
            else:
                await self._set_state(
                    job_id,
                    outcome.thread_id,
                    lease_token,
                    EditorialThreadJobState.FAILED,
                    EditorialBridgeStatus.READY,
                )
        elif isinstance(outcome, EditorialThreadSessionLostOutcome):
            await self._session_lost(job_id, lease_token, outcome)
        else:
            await self._set_state(
                job_id,
                outcome.thread_id,
                lease_token,
                outcome.job_state,
                outcome.bridge_status,
                retry_after_seconds=outcome.retry_after_seconds,
            )

    async def _reply(
        self,
        job_id: UUID,
        lease_token: UUID,
        outcome: EditorialThreadReplyOutcome,
    ) -> None:
        body = outcome.reply.encode("utf-8")
        blob_id = await self._artifact_store.put_bytes(
            body,
            bucket="editorial-thread-events",
            mime_type="text/plain; charset=utf-8",
        )
        digest = hashlib.sha256(body).hexdigest()
        request_hash = hashlib.sha256(
            f"editorial-thread-reply:{job_id}:{digest}".encode()
        ).hexdigest()
        async with self._uow_factory() as uow:
            await uow.editorial_threads.append_event(
                thread_id=outcome.thread_id,
                kind=EditorialThreadEventKind.BRIDGE_REPLY,
                actor_id=None,
                idempotency_key=f"editorial-thread-reply:{job_id}",
                request_sha256=request_hash,
                payload_blob_ref=blob_id,
                payload_sha256=digest,
                model_turn_id=outcome.turn_id,
            )
            await uow.editorial_threads.set_job_state(
                job_id, EditorialThreadJobState.COMPLETE, lease_token=lease_token
            )
            await uow.editorial_threads.set_bridge_status(
                outcome.thread_id, EditorialBridgeStatus.REPLY_AVAILABLE
            )
            await uow.commit()

    async def _refusal(
        self,
        job_id: UUID,
        lease_token: UUID,
        outcome: EditorialThreadRefusalOutcome,
    ) -> None:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job(job_id)
            if job is None:
                return
            record = await uow.editorial_threads.get(job.thread_id)
            if record is None:
                return
            thread_id = record.thread.id
        body = outcome.message.encode("utf-8")
        blob_id = await self._artifact_store.put_bytes(
            body,
            bucket="editorial-thread-events",
            mime_type="text/plain; charset=utf-8",
        )
        digest = hashlib.sha256(body).hexdigest()
        request_hash = hashlib.sha256(
            f"editorial-thread-refusal:{job_id}:{outcome.reason_code}".encode()
        ).hexdigest()
        async with self._uow_factory() as uow:
            await uow.editorial_threads.append_event(
                thread_id=thread_id,
                kind=EditorialThreadEventKind.REFUSED,
                actor_id=None,
                idempotency_key=f"editorial-thread-refusal:{job_id}",
                request_sha256=request_hash,
                payload_blob_ref=blob_id,
                payload_sha256=digest,
            )
            await uow.editorial_threads.set_job_state(
                job_id, EditorialThreadJobState.COMPLETE, lease_token=lease_token
            )
            await uow.editorial_threads.set_bridge_status(thread_id, EditorialBridgeStatus.REFUSED)
            await uow.commit()

    async def _session_lost(
        self,
        job_id: UUID,
        lease_token: UUID,
        outcome: EditorialThreadSessionLostOutcome,
    ) -> None:
        allowed_reasons = {"conversation_unavailable", "bridge_tab_closed"}
        if (
            outcome.reason_code not in allowed_reasons
            or await self._submission_state_for_job(job_id)
            is not ModelSubmissionState.NOT_SUBMITTED
            or await self._turn_error_code_for_job(job_id) != outcome.reason_code
        ):
            await self._set_state(
                job_id,
                outcome.thread_id,
                lease_token,
                EditorialThreadJobState.RECONCILIATION_REQUIRED,
                EditorialBridgeStatus.RECONCILIATION_REQUIRED,
            )
            return
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job(job_id)
            record = await uow.editorial_threads.get(outcome.thread_id)
            if job is None or record is None or job.thread_id != outcome.thread_id:
                return
            body = json.dumps(
                {
                    "outcome": EditorialThreadReconciliationOutcome.SESSION_LOST.value,
                    "message": (
                        "Cette session Bridge est perdue. Ouvrez un nouveau fil pour poursuivre."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            payload_sha256 = hashlib.sha256(body).hexdigest()
            payload_blob_ref = await self._artifact_store.put_bytes(
                body,
                bucket="editorial-thread-events",
                mime_type="text/plain; charset=utf-8",
            )
            await uow.editorial_threads.append_event(
                thread_id=outcome.thread_id,
                kind=EditorialThreadEventKind.RECONCILIATION,
                actor_id=None,
                idempotency_key=f"editorial-thread-session-lost:{job_id}",
                request_sha256=hashlib.sha256(
                    f"editorial-thread-session-lost:{job_id}:{job.send_event_id}".encode()
                ).hexdigest(),
                payload_blob_ref=payload_blob_ref,
                payload_sha256=payload_sha256,
            )
            await uow.editorial_threads.set_job_state(
                job_id, EditorialThreadJobState.COMPLETE, lease_token=lease_token
            )
            await uow.editorial_threads.set_bridge_status(
                outcome.thread_id, EditorialBridgeStatus.SESSION_LOST
            )
            await uow.commit()

    async def _turn_error_code_for_job(self, job_id: UUID) -> str | None:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job(job_id)
            if job is None:
                return None
            turn = await uow.model_conversation_turns.get_by_idempotency_key(
                f"editorial-thread-message:{job.send_event_id}"
            )
            record = await uow.editorial_threads.get(job.thread_id)
            if (
                turn is None
                or record is None
                or turn.conversation_id != record.thread.model_conversation_id
                or turn.objective != "editorial_message"
            ):
                return None
            return turn.error_code

    async def _set_state(
        self,
        job_id: UUID,
        thread_id: UUID,
        lease_token: UUID,
        job_state: EditorialThreadJobState,
        bridge_status: EditorialBridgeStatus,
        retry_after_seconds: float | None = None,
    ) -> None:
        async with self._uow_factory() as uow:
            lease_until = (
                datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
                if job_state is EditorialThreadJobState.WAITING_FOR_CAPACITY
                and retry_after_seconds is not None
                else None
            )
            await uow.editorial_threads.set_job_state(
                job_id,
                job_state,
                lease_token=lease_token,
                lease_until=lease_until,
            )
            await uow.editorial_threads.set_bridge_status(thread_id, bridge_status)
            await uow.commit()

    async def _submission_state_for_job(self, job_id: UUID) -> ModelSubmissionState | None:
        async with self._uow_factory() as uow:
            job = await uow.editorial_threads.get_job(job_id)
            record = await uow.editorial_threads.get(job.thread_id) if job else None
            if job is None or record is None or record.thread.model_conversation_id is None:
                return None
            turn = await uow.model_conversation_turns.get_by_idempotency_key(
                f"editorial-thread-message:{job.send_event_id}"
            )
            if (
                turn is None
                or turn.status
                not in {
                    ConversationTurnStatus.FAILED,
                    ConversationTurnStatus.NEEDS_REVIEW,
                }
                or turn.conversation_id != record.thread.model_conversation_id
                or turn.objective != "editorial_message"
            ):
                return None
            run = await uow.model_runs.get(turn.model_run_id)
            if run is None or not has_persisted_not_submitted_proof(turn, run):
                return None
            return ModelSubmissionState.NOT_SUBMITTED
