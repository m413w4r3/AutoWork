"""Read and append primitives for occurrence-bound editorial threads."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

from cti_app.application.editorial_thread_messages import EditorialThreadMessages
from cti_app.application.editorial_thread_reconciliation import (
    EditorialThreadReconciliation,
    _reconciliation_message,
    _reconciliation_outcome,
)
from cti_app.application.editorial_thread_sessions import EditorialThreadSessions
from cti_app.application.editorial_thread_types import (
    EditorialThreadApplicationError,
    EditorialThreadReconciliationResult,
    EditorialThreadSendResult,
    EditorialThreadSessionStartResult,
)
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial_thread import (
    EditorialBindingState,
    EditorialBridgeStatus,
    EditorialThread,
    EditorialThreadBinding,
    EditorialThreadEvent,
    EditorialThreadEventKind,
    EditorialThreadJob,
    EditorialThreadJobState,
    EditorialThreadReconciliationOutcome,
    EditorialThreadRecord,
)
from cti_app.domain.editorial_workspace import EditorialWorkspaceError

logger = logging.getLogger(__name__)

__all__ = [
    "EditorialThreadApplicationError",
    "EditorialThreadReconciliationResult",
    "EditorialThreadSendResult",
    "EditorialThreadService",
    "EditorialThreadSessionStartResult",
]


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
    reconciliation_outcome: EditorialThreadReconciliationOutcome | None = None


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
    previous_sessions: tuple[int, ...] = ()


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
            previous_sessions=await self._previous_sessions(thread),
        )

        open_result = replace(open_result, etag=_read_etag(open_result, after_sequence, events))
        if _etag_matches(etag, open_result.etag):
            return replace(open_result, not_modified=True)
        return replace(open_result, timeline=await self._read_bodies(events))

    async def _previous_sessions(self, thread: EditorialThread) -> tuple[int, ...]:
        async with self._uow_factory() as uow:
            return await uow.editorial_threads.list_previous_sessions(
                thread.origin.subject_id,
                thread.origin.production_run_id,
                thread.origin.occurrence_id,
                before_session_number=thread.session_number,
            )

    async def start_new_session(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        idempotency_key: str,
    ) -> EditorialThreadSessionStartResult:
        return await EditorialThreadSessions(
            uow_factory=self._uow_factory,
            workspace_service=self._workspace,
            model_conversations=self._model_conversations,
        ).start_new_session(subject_id, occurrence_id, idempotency_key=idempotency_key)

    async def reconcile(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        actor_id: UUID,
        idempotency_key: str,
    ) -> EditorialThreadReconciliationResult:
        return await EditorialThreadReconciliation(
            uow_factory=self._uow_factory,
            workspace_service=self._workspace,
            artifact_store=self._artifact_store,
            model_conversations=self._model_conversations,
        ).reconcile(
            subject_id,
            occurrence_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
        )

    async def send_message(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        *,
        text: str,
        actor_id: UUID,
        idempotency_key: str,
    ) -> EditorialThreadSendResult:
        return await EditorialThreadMessages(
            uow_factory=self._uow_factory,
            workspace_service=self._workspace,
            artifact_store=self._artifact_store,
            model_conversations=self._model_conversations,
            enqueue_job=self._enqueue_job,
        ).send_message(
            subject_id,
            occurrence_id,
            text=text,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
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
                if event.kind is EditorialThreadEventKind.RECONCILIATION:
                    body_text = _reconciliation_message(body)
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
                    reconciliation_outcome=(
                        _reconciliation_outcome(body)
                        if event.kind is EditorialThreadEventKind.RECONCILIATION
                        and event.payload_blob_ref is not None
                        else None
                    ),
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
        if record.thread.bridge_status in {
            EditorialBridgeStatus.REPLY_AVAILABLE,
            EditorialBridgeStatus.SESSION_LOST,
        }:
            return record.thread.bridge_status
        return EditorialBridgeStatus.READY
    if job.state is EditorialThreadJobState.FAILED:
        if record.thread.bridge_status is EditorialBridgeStatus.SESSION_LOST:
            return EditorialBridgeStatus.SESSION_LOST
        return EditorialBridgeStatus.READY
    return record.thread.bridge_status
