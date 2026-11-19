"""Read and append primitives for occurrence-bound editorial threads."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

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
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadJob,
    EditorialThreadJobState,
    EditorialThreadLockTimeout,
    EditorialThreadRecord,
)
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.model_conversations import ConversationPurpose, ConversationTransport
from cti_app.domain.model_runs import ModelProvider
from cti_app.infrastructure.database.repositories.editorial_threads import (
    translate_enqueue_database_error,
)

logger = logging.getLogger(__name__)


class EditorialThreadApplicationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EditorialThreadTimelineItem:
    id: UUID
    sequence: int
    kind: EditorialThreadEventKind
    actor_id: UUID | None
    proposal_id: UUID | None
    payload_sha256: str | None
    created_at: datetime
    body: str | None = None


@dataclass(frozen=True, slots=True)
class UnopenedThreadRead:
    kind: Literal["unopened"]
    binding: EditorialThreadBinding
    current: EditorialThreadBinding
    bridge_status: EditorialBridgeStatus
    event_sequence: int
    timeline: tuple[EditorialThreadTimelineItem, ...]
    etag: str
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class OpenThreadRead:
    kind: Literal["open"]
    thread_id: UUID
    origin: EditorialThreadBinding
    current: EditorialThreadBinding
    binding_state: Literal[EditorialBindingState.VALID]
    bridge_status: EditorialBridgeStatus
    event_sequence: int
    timeline: tuple[EditorialThreadTimelineItem, ...]
    etag: str
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class StaleThreadRead:
    kind: Literal["stale"]
    thread_id: UUID | None
    origin: EditorialThreadBinding | None
    current: EditorialThreadBinding
    binding_state: Literal[EditorialBindingState.STALE, EditorialBindingState.AMBIGUOUS]
    bridge_status: EditorialBridgeStatus
    event_sequence: int
    timeline: tuple[EditorialThreadTimelineItem, ...]
    etag: str
    not_modified: bool = False


type EditorialThreadRead = UnopenedThreadRead | OpenThreadRead | StaleThreadRead


@dataclass(frozen=True, slots=True)
class EditorialThreadSendResult:
    kind: Literal["accepted", "already_known"]
    thread_id: UUID
    entry_id: UUID
    generation: EditorialThreadJobState


class EditorialThreadService:
    """Project a thread from the workspace registry and its private event store."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialWorkspaceService,
        artifact_store: ProductionArtifactStore,
        model_conversations: ModelConversationService | None = None,
        enqueue_job: Callable[[UUID], None] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace = workspace_service
        self._artifact_store = artifact_store
        self._model_conversations = model_conversations
        self._enqueue_job = enqueue_job

    async def read(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        after_sequence: int = 0,
        etag: str | None = None,
    ) -> EditorialThreadRead:
        if after_sequence < 0:
            raise EditorialThreadApplicationError("after_sequence_invalid")

        try:
            resolution = await self._workspace.resolve_occurrence(subject_id, occurrence_id)
        except EditorialWorkspaceError as error:
            code = error.code if error.code != "editorial_workspace_error" else str(error)
            if code == "subject_not_found":
                raise EditorialThreadApplicationError("thread_not_found") from None
            raise
        if resolution.entry is None:
            raise EditorialThreadApplicationError("thread_not_found")
        if (
            resolution.artifact_id is None
            or resolution.artifact_version is None
            or resolution.canonical_sha256 is None
        ):
            raise EditorialThreadApplicationError("thread_binding_stale")
        current = EditorialThreadBinding(
            subject_id=resolution.record.subject_id,
            production_run_id=resolution.record.production_run_id,
            occurrence_id=resolution.entry.block_id,
            artifact_id=resolution.artifact_id,
            artifact_version=resolution.artifact_version,
            canonical_sha256=resolution.canonical_sha256,
        )
        current_state = EditorialBindingState(resolution.binding_state)

        # An unproven current occurrence may expose its current binding, but it
        # must never reveal whether a prior session or any events exist.
        if current_state is not EditorialBindingState.VALID:
            stale_result = StaleThreadRead(
                kind="stale",
                thread_id=None,
                origin=None,
                current=current,
                binding_state=current_state,
                bridge_status=EditorialBridgeStatus.READY,
                event_sequence=0,
                timeline=(),
                etag="",
            )
            stale_result = replace(
                stale_result,
                etag=_read_etag(stale_result, after_sequence, ()),
            )
            return replace(stale_result, not_modified=_etag_matches(etag, stale_result.etag))

        async with self._uow_factory() as uow:
            record = await uow.editorial_threads.get_current_by_occurrence(
                subject_id, current.production_run_id, occurrence_id
            )
            latest_job = (
                await uow.editorial_threads.latest_job_for_thread(record.thread.id)
                if record is not None
                else None
            )

        if record is None:
            unopened_result = UnopenedThreadRead(
                kind="unopened",
                binding=current,
                current=current,
                bridge_status=EditorialBridgeStatus.READY,
                event_sequence=0,
                timeline=(),
                etag="",
            )
            unopened_result = replace(
                unopened_result,
                etag=_read_etag(unopened_result, after_sequence, ()),
            )
            return replace(
                unopened_result,
                not_modified=_etag_matches(etag, unopened_result.etag),
            )

        thread = record.thread
        if (
            thread.origin.subject_id != subject_id
            or thread.origin.production_run_id != current.production_run_id
            or thread.origin.occurrence_id != occurrence_id
        ):
            unopened_result = UnopenedThreadRead(
                kind="unopened",
                binding=current,
                current=current,
                bridge_status=EditorialBridgeStatus.READY,
                event_sequence=0,
                timeline=(),
                etag="",
            )
            unopened_result = replace(
                unopened_result,
                etag=_read_etag(unopened_result, after_sequence, ()),
            )
            return replace(
                unopened_result,
                not_modified=_etag_matches(etag, unopened_result.etag),
            )
        events = tuple(event for event in record.events if event.sequence > after_sequence)
        projected_status = _project_bridge_status(record, latest_job)

        open_result = OpenThreadRead(
            kind="open",
            thread_id=thread.id,
            origin=thread.origin,
            current=current,
            binding_state=EditorialBindingState.VALID,
            bridge_status=projected_status,
            event_sequence=thread.next_sequence - 1,
            timeline=(),
            etag="",
        )

        open_result = replace(open_result, etag=_read_etag(open_result, after_sequence, events))
        if _etag_matches(etag, open_result.etag):
            return replace(open_result, not_modified=True)
        return replace(open_result, timeline=await self._read_bodies(events))

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
        if not idempotency_key.strip() or len(idempotency_key) > 255:
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
                    raise EditorialThreadApplicationError("bridge_unavailable")
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

    async def append_event(
        self,
        *,
        thread_id: UUID,
        kind: EditorialThreadEventKind,
        actor_id: UUID | None,
        idempotency_key: str,
        request_sha256: str,
        payload_blob_ref: UUID | None = None,
        payload_sha256: str | None = None,
        proposal_id: UUID | None = None,
        model_turn_id: UUID | None = None,
    ) -> EditorialThreadEvent:
        """Append one durable event; send and worker routes are added in M2-07."""
        async with self._uow_factory() as uow:
            event = await uow.editorial_threads.append_event(
                thread_id=thread_id,
                kind=kind,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                payload_blob_ref=payload_blob_ref,
                payload_sha256=payload_sha256,
                proposal_id=proposal_id,
                model_turn_id=model_turn_id,
            )
            await uow.commit()
            return event

    async def _read_bodies(
        self, events: tuple[EditorialThreadEvent, ...]
    ) -> tuple[EditorialThreadTimelineItem, ...]:
        projected: list[EditorialThreadTimelineItem] = []
        for event in events:
            body_text: str | None = None
            if event.payload_blob_ref is None or event.payload_sha256 is None:
                pass
            else:
                try:
                    body = await self._artifact_store.read_bytes(event.payload_blob_ref)
                except Exception:
                    raise EditorialThreadApplicationError("thread_body_unavailable") from None
                if hashlib.sha256(body).hexdigest() != event.payload_sha256:
                    raise EditorialThreadApplicationError("thread_body_hash_mismatch") from None
                try:
                    body_text = body.decode("utf-8")
                except UnicodeDecodeError:
                    raise EditorialThreadApplicationError("thread_body_unavailable") from None
            projected.append(
                EditorialThreadTimelineItem(
                    id=event.id,
                    sequence=event.sequence,
                    kind=event.kind,
                    actor_id=event.actor_id,
                    proposal_id=event.proposal_id,
                    payload_sha256=event.payload_sha256,
                    created_at=event.created_at,
                    body=body_text,
                )
            )
        return tuple(projected)


def _read_etag(
    result: EditorialThreadRead,
    after_sequence: int,
    events: tuple[EditorialThreadEvent, ...],
) -> str:
    origin = getattr(result, "origin", None)
    version = getattr(result, "event_sequence", 0)
    digest_input: dict[str, object] = {
        "kind": result.kind,
        "thread_id": str(getattr(result, "thread_id", "")),
        "origin": _binding_json(origin) if origin is not None else None,
        "current": _binding_json(result.current),
        "binding_state": str(getattr(result, "binding_state", "valid")),
        "bridge_status": result.bridge_status.value,
        "event_sequence": version,
        "after_sequence": after_sequence,
        "events": [
            {
                "id": str(event.id),
                "sequence": event.sequence,
                "kind": event.kind.value,
                "payload_sha256": event.payload_sha256,
                "proposal_id": str(event.proposal_id) if event.proposal_id else None,
            }
            for event in events
        ],
    }
    canonical = json.dumps(digest_input, sort_keys=True, separators=(",", ":"))
    return f'"{hashlib.sha256(canonical.encode("utf-8")).hexdigest()}"'


def _binding_json(binding: EditorialThreadBinding) -> dict[str, str | int]:
    return {
        "subject_id": str(binding.subject_id),
        "production_run_id": str(binding.production_run_id),
        "occurrence_id": str(binding.occurrence_id),
        "artifact_id": str(binding.artifact_id),
        "artifact_version": binding.artifact_version,
        "canonical_sha256": binding.canonical_sha256,
    }


def _etag_matches(request_etag: str | None, current_etag: str) -> bool:
    if request_etag is None:
        return False
    return any(value.strip() in {"*", current_etag} for value in request_etag.split(","))


def _project_bridge_status(
    record: EditorialThreadRecord, job: EditorialThreadJob | None
) -> EditorialBridgeStatus:
    if job is None:
        return record.thread.bridge_status
    if job.state is EditorialThreadJobState.RECONCILIATION_REQUIRED:
        return EditorialBridgeStatus.RECONCILIATION_REQUIRED
    if job.state is EditorialThreadJobState.WAITING_FOR_CAPACITY:
        return EditorialBridgeStatus.WAITING_FOR_CAPACITY
    if job.state in {EditorialThreadJobState.QUEUED, EditorialThreadJobState.RUNNING}:
        return EditorialBridgeStatus.GENERATING
    if job.state is EditorialThreadJobState.COMPLETE:
        last_kind = record.events[-1].kind if record.events else None
        if last_kind is EditorialThreadEventKind.BRIDGE_REPLY:
            return EditorialBridgeStatus.REPLY_AVAILABLE
        if last_kind is EditorialThreadEventKind.REFUSED:
            return EditorialBridgeStatus.REFUSED
        return EditorialBridgeStatus.READY
    if job.state is EditorialThreadJobState.FAILED:
        if record.thread.bridge_status is EditorialBridgeStatus.SESSION_LOST:
            return EditorialBridgeStatus.SESSION_LOST
        return EditorialBridgeStatus.READY
    return record.thread.bridge_status
