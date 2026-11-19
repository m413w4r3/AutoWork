"""Durable execution for message-only editorial Bridge jobs."""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID, uuid4

from cti_app.application.editorial_bridge_gate import (
    EditorialBridgeGate,
    EditorialBridgeGateDisposition,
    EditorialBridgeSubmissionResolution,
)
from cti_app.application.editorial_conversation_turns import EditorialDiscussionObjective
from cti_app.application.editorial_discussion_context import EditorialDiscussionContextBuilder
from cti_app.application.editorial_thread_job_completion import (
    EditorialThreadFailureOutcome,
    EditorialThreadJobCompletion,
    EditorialThreadRefusalOutcome,
    EditorialThreadReplyOutcome,
    EditorialThreadSessionLostOutcome,
    EditorialThreadStateOutcome,
)
from cti_app.application.editorial_threads import EditorialThreadApplicationError
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.model_conversations import (
    ConversationBusyError,
    ConversationTurnFailedError,
    ModelConversationService,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial_bridge_generation_slot import EditorialBridgeGenerationLease
from cti_app.domain.editorial_thread import (
    EditorialBridgeStatus,
    EditorialThreadJobState,
)
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPurpose,
    ConversationTransport,
    ConversationTurnStatus,
)

logger = logging.getLogger(__name__)

EDITORIAL_THREAD_JOB_LEASE = timedelta(minutes=30)
EDITORIAL_THREAD_CAPACITY_RETRY_SECONDS = 5
EDITORIAL_THREAD_POLICY_REFUSAL = (
    "La politique de diffusion ne permet pas d’envoyer ce contenu au Bridge."  # noqa: RUF001
)
EDITORIAL_THREAD_CONTEXT_REFUSAL = (
    "Le contexte éditorial autorisé n’a pas pu être vérifié. Aucun message n’a été envoyé."  # noqa: RUF001
)


class EditorialThreadWorker:
    """Claim a job, rebuild its context, invoke Bridge, and close the job."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialWorkspaceService,
        artifact_store: ProductionArtifactStore,
        model_conversations: ModelConversationService,
    ) -> None:
        self._uow_factory = uow_factory
        self._context_builder = EditorialDiscussionContextBuilder(
            uow_factory=uow_factory,
            workspace_service=workspace_service,
            artifact_store=artifact_store,
        )
        self._completion = EditorialThreadJobCompletion(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
        )
        self._model_conversations = model_conversations
        self._bridge_gate = EditorialBridgeGate(uow_factory)

    async def process(self, job_id: UUID) -> int | None:
        lease_token = uuid4()
        async with self._uow_factory() as uow:
            claimed = await uow.editorial_threads.claim_job(
                job_id,
                lease_token=lease_token,
                lease_duration=EDITORIAL_THREAD_JOB_LEASE,
            )
            if claimed is None:
                return None
            await uow.editorial_threads.set_bridge_status(
                claimed.thread_id, EditorialBridgeStatus.GENERATING
            )
            await uow.commit()

        try:
            prepared = await self._context_builder.prepare_job(job_id)
        except EditorialThreadApplicationError as error:
            await self._refuse_context_error(job_id, lease_token, error)
            return None

        record = prepared.record
        send_event = prepared.send_event
        conversation_id = record.thread.model_conversation_id
        if conversation_id is None:
            await self._close_failed(job_id, record.thread.id, lease_token)
            return None
        original_send_event_id = send_event.id
        bridge_lease: EditorialBridgeGenerationLease | None = None
        turn_idempotency_key: str | None = None

        try:
            conversation = await self._model_conversations.get_editorial_thread_conversation(
                conversation_id,
                thread_id=record.thread.id,
                context_subject_id=record.thread.origin.subject_id,
            )
            if (
                conversation.purpose is not ConversationPurpose.EDITORIAL_DISCUSSION
                or conversation.transport is not ConversationTransport.CHATGPT_BRIDGE
                or conversation.subject_id != record.thread.origin.subject_id
            ):
                await self._close_failed(job_id, record.thread.id, lease_token)
                return None
            mode = ConversationMode.FRESH
            if conversation.head_turn_id is not None:
                async with self._uow_factory() as uow:
                    parent = await uow.model_conversation_turns.get(conversation.head_turn_id)
                if (
                    parent is None
                    or parent.status is not ConversationTurnStatus.SUCCEEDED
                    or not parent.external_turn_id
                ):
                    await self._close_reconciliation(job_id, record.thread.id, lease_token)
                    return None
                mode = ConversationMode.CONTINUE

            # Rebuild policy and context immediately before the gateway handoff.
            current = await self._context_builder.prepare_job(job_id)
            if (
                current.record.thread.id != record.thread.id
                or current.record.thread.model_conversation_id != conversation_id
                or current.send_event.id != original_send_event_id
            ):
                await self._close_reconciliation(job_id, record.thread.id, lease_token)
                return None
            turn_idempotency_key = f"editorial-thread-message:{current.send_event.id}"
            assert turn_idempotency_key is not None
            gate_attempt = await self._bridge_gate.acquire(
                thread_id=record.thread.id,
                job_id=job_id,
                worker_lease_token=lease_token,
                turn_idempotency_key=turn_idempotency_key,
            )
            if (
                gate_attempt.disposition
                is EditorialBridgeGateDisposition.CURRENT_REQUIRES_RECONCILIATION
            ):
                await self._close_reconciliation(job_id, record.thread.id, lease_token)
                return None
            if gate_attempt.lease is None:
                retry_after = EDITORIAL_THREAD_CAPACITY_RETRY_SECONDS
                await self._completion.close(
                    job_id,
                    lease_token,
                    EditorialThreadStateOutcome(
                        thread_id=record.thread.id,
                        job_state=EditorialThreadJobState.WAITING_FOR_CAPACITY,
                        bridge_status=EditorialBridgeStatus.WAITING_FOR_CAPACITY,
                        retry_after_seconds=retry_after,
                    ),
                )
                return retry_after * 1000
            bridge_lease = gate_attempt.lease
            if not await self._bridge_gate.is_current_worker(bridge_lease):
                return None
            turn = await self._model_conversations.add_turn(
                conversation_id,
                message=current.instruction,
                mode=mode,
                external_llm_allowed=current.external_llm_allowed,
                web_search=False,
                idempotency_key=turn_idempotency_key,
                correlation_id=str(job_id),
                context_subject_id=record.thread.origin.subject_id,
                _editorial_thread_id=record.thread.id,
                editorial_objective=EditorialDiscussionObjective.MESSAGE,
                editorial_context=current.context,
            )
        except EditorialThreadApplicationError as error:
            if bridge_lease is not None and turn_idempotency_key is not None:
                resolution = await self._bridge_gate.finish_failed_attempt(
                    bridge_lease,
                    turn_idempotency_key=turn_idempotency_key,
                )
                if resolution is EditorialBridgeSubmissionResolution.UNKNOWN:
                    await self._close_reconciliation(job_id, record.thread.id, lease_token)
                    return None
            await self._refuse_context_error(job_id, lease_token, error)
            return None
        except ConversationBusyError:
            if bridge_lease is None or turn_idempotency_key is None:
                await self._close_failed(job_id, record.thread.id, lease_token)
                return None
            await self._bridge_gate.finish_failed_attempt(
                bridge_lease,
                turn_idempotency_key=turn_idempotency_key,
            )
            retry_after = EDITORIAL_THREAD_CAPACITY_RETRY_SECONDS
            await self._completion.close(
                job_id,
                lease_token,
                EditorialThreadStateOutcome(
                    thread_id=record.thread.id,
                    job_state=EditorialThreadJobState.WAITING_FOR_CAPACITY,
                    bridge_status=EditorialBridgeStatus.WAITING_FOR_CAPACITY,
                    retry_after_seconds=retry_after,
                ),
            )
            return retry_after * 1000
        except ConversationTurnFailedError as error:
            if bridge_lease is None or turn_idempotency_key is None:
                await self._close_failed(job_id, record.thread.id, lease_token)
                return None
            resolution = await self._bridge_gate.finish_failed_attempt(
                bridge_lease,
                turn_idempotency_key=turn_idempotency_key,
            )
            if resolution is EditorialBridgeSubmissionResolution.UNKNOWN:
                await self._close_reconciliation(job_id, record.thread.id, lease_token)
                return None
            if error.submission_state == "pre_submission" and error.code in {
                "conversation_unavailable",
                "bridge_tab_closed",
            }:
                await self._completion.close(
                    job_id,
                    lease_token,
                    EditorialThreadSessionLostOutcome(
                        thread_id=record.thread.id,
                        reason_code=error.code,
                    ),
                )
                return None
            if error.status is ConversationTurnStatus.NEEDS_REVIEW or (
                error.submission_state != "pre_submission"
            ):
                await self._close_reconciliation(job_id, record.thread.id, lease_token)
                return None
            if error.code == "bridge_rate_limited":
                delay = error.retry_after_seconds or EDITORIAL_THREAD_CAPACITY_RETRY_SECONDS
                delay = max(1.0, min(float(delay), 86_400.0))
                await self._completion.close(
                    job_id,
                    lease_token,
                    EditorialThreadStateOutcome(
                        thread_id=record.thread.id,
                        job_state=EditorialThreadJobState.WAITING_FOR_CAPACITY,
                        bridge_status=EditorialBridgeStatus.WAITING_FOR_CAPACITY,
                        retry_after_seconds=delay,
                    ),
                )
                return int(delay * 1000)
            if error.code == "external_llm_blocked":
                await self._completion.close(
                    job_id,
                    lease_token,
                    EditorialThreadRefusalOutcome(
                        reason_code=error.code,
                        message=EDITORIAL_THREAD_POLICY_REFUSAL,
                    ),
                )
                return None
            await self._close_failed(job_id, record.thread.id, lease_token)
            return None
        except Exception as error:
            if bridge_lease is not None and turn_idempotency_key is not None:
                resolution = await self._bridge_gate.finish_failed_attempt(
                    bridge_lease,
                    turn_idempotency_key=turn_idempotency_key,
                )
                if resolution is EditorialBridgeSubmissionResolution.UNKNOWN:
                    await self._close_reconciliation(job_id, record.thread.id, lease_token)
                    return None
            logger.warning(
                "editorial_thread_job_failed job_id=%s error_type=%s",
                job_id,
                type(error).__name__,
            )
            await self._completion.close(
                job_id,
                lease_token,
                EditorialThreadFailureOutcome(thread_id=record.thread.id),
            )
            return None

        if bridge_lease is None:
            await self._close_reconciliation(job_id, record.thread.id, lease_token)
            return None
        if not await self._bridge_gate.finish_turn(bridge_lease, turn_id=turn.id):
            await self._close_reconciliation(job_id, record.thread.id, lease_token)
            return None

        if (
            turn.objective != "editorial_message"
            or turn.status is not ConversationTurnStatus.SUCCEEDED
        ):
            await self._close_reconciliation(job_id, record.thread.id, lease_token)
            return None
        contents = await self._model_conversations.turns_for_editorial_thread(
            conversation_id,
            thread_id=record.thread.id,
            context_subject_id=record.thread.origin.subject_id,
        )
        content = next((item for item in contents if item.turn.id == turn.id), None)
        if content is None or not content.output_text:
            await self._close_reconciliation(job_id, record.thread.id, lease_token)
            return None
        await self._completion.close(
            job_id,
            lease_token,
            EditorialThreadReplyOutcome(
                thread_id=record.thread.id,
                send_event=send_event,
                turn_id=turn.id,
                reply=content.output_text,
            ),
        )
        return None

    async def _refuse_context_error(
        self, job_id: UUID, lease_token: UUID, error: EditorialThreadApplicationError
    ) -> None:
        if error.code == "external_llm_blocked":
            outcome = EditorialThreadRefusalOutcome(
                reason_code=error.code,
                message=EDITORIAL_THREAD_POLICY_REFUSAL,
            )
        else:
            outcome = EditorialThreadRefusalOutcome(
                reason_code="editorial_context_unavailable",
                message=EDITORIAL_THREAD_CONTEXT_REFUSAL,
            )
        await self._completion.close(job_id, lease_token, outcome)

    async def _close_failed(self, job_id: UUID, thread_id: UUID, lease_token: UUID) -> None:
        await self._completion.close(
            job_id,
            lease_token,
            EditorialThreadStateOutcome(
                thread_id=thread_id,
                job_state=EditorialThreadJobState.FAILED,
                bridge_status=EditorialBridgeStatus.READY,
            ),
        )

    async def _close_reconciliation(self, job_id: UUID, thread_id: UUID, lease_token: UUID) -> None:
        await self._completion.close(
            job_id,
            lease_token,
            EditorialThreadStateOutcome(
                thread_id=thread_id,
                job_state=EditorialThreadJobState.RECONCILIATION_REQUIRED,
                bridge_status=EditorialBridgeStatus.RECONCILIATION_REQUIRED,
            ),
        )
