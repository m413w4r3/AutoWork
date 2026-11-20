from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_thread_common import encode_thread_request
from cti_app.application.editorial_threads import EditorialThreadService
from cti_app.application.editorial_workspace import resolve_occurrence
from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    project_blocks,
)
from cti_app.application.model_conversations import (
    ModelConversationError,
    ModelConversationService,
)
from cti_app.domain.editorial_thread import (
    EditorialBindingState,
    EditorialBridgeStatus,
    EditorialThread,
    EditorialThreadBinding,
    EditorialThreadConflict,
    EditorialThreadError,
    EditorialThreadEventKind,
    EditorialThreadRecord,
)
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialRevisionStatus,
    EditorialSourceStage,
    EditorialTransition,
    EditorialWorkspaceError,
    canonical_content_sha256,
)
from cti_app.domain.model_conversations import (
    CONTINUABLE_PURPOSES,
    ConversationMode,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import ModelProvider
from cti_app.infrastructure.database.repositories.editorial_threads import (
    SqlAlchemyEditorialThreadRepository,
)


def _binding(subject_id: UUID, occurrence_id: UUID) -> EditorialThreadBinding:
    return EditorialThreadBinding(
        subject_id=subject_id,
        production_run_id=uuid4(),
        occurrence_id=occurrence_id,
        artifact_id=uuid4(),
        artifact_version=1,
        canonical_sha256="a" * 64,
    )


def _thread(binding: EditorialThreadBinding) -> EditorialThread:
    return EditorialThread(
        id=uuid4(),
        origin=binding,
        model_conversation_id=uuid4(),
        bridge_status=EditorialBridgeStatus.READY,
        next_sequence=1,
        version=1,
    )


def _workspace_record(
    binding: EditorialThreadBinding,
    status: str = "ready",
    source_stage: EditorialSourceStage = EditorialSourceStage.SYNTHESIS,
) -> WorkspaceSnapshotRecord:
    is_enrichment = source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
    entry = (
        SimpleNamespace(
            block_id=binding.occurrence_id,
            source_stage=source_stage,
            revision_status="stale",
        )
        if status == "stale"
        else BlockRegistryEntry(
            block_id=binding.occurrence_id,
            kind=EditorialBlockKind.PARAGRAPH,
            parent_block_id=None,
            position=0,
            content_sha256="d" * 64,
            source_stage=source_stage,
            structural_pointer="/sections/0/paragraphs/0",
            transition=EditorialTransition.BOOTSTRAP,
            revision_status=EditorialRevisionStatus(status),
        )
    )
    return WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=binding.subject_id,
        production_run_id=binding.production_run_id,
        synthesis_artifact_id=uuid4() if is_enrichment else binding.artifact_id,
        synthesis_input_hash="e" * 64,
        synthesis_version=1 if is_enrichment else binding.artifact_version,
        synthesis_canonical_sha256="c" * 64 if is_enrichment else binding.canonical_sha256,
        enrichment_artifact_id=binding.artifact_id if is_enrichment else None,
        enrichment_version=binding.artifact_version if is_enrichment else None,
        enrichment_input_hash="f" * 64 if is_enrichment else None,
        enrichment_canonical_sha256=binding.canonical_sha256 if is_enrichment else None,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=1,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=None,
        workspace_pin_hash="1" * 64,
        registry_hash="2" * 64,
        block_registry=(entry,),
    )


class _MemoryThreadRepository:
    def __init__(self, record: EditorialThreadRecord | None = None) -> None:
        self.record = record
        self.records = [record] if record is not None else []
        self.lookups: list[tuple[UUID, UUID, UUID]] = []
        self.created = 0
        self.commit_count = 0

    async def latest_job_for_thread(self, _thread_id: UUID):
        return None

    async def get_current_by_occurrence(
        self, subject_id: UUID, production_run_id: UUID, occurrence_id: UUID
    ) -> EditorialThreadRecord | None:
        self.lookups.append((subject_id, production_run_id, occurrence_id))
        candidates = [
            record
            for record in self.records
            if record.thread.origin.subject_id == subject_id
            and record.thread.origin.production_run_id == production_run_id
            and record.thread.origin.occurrence_id == occurrence_id
        ]
        return max(candidates, key=lambda record: record.thread.session_number, default=None)

    async def list_previous_sessions(
        self,
        subject_id: UUID,
        production_run_id: UUID,
        occurrence_id: UUID,
        *,
        before_session_number: int,
    ) -> tuple[int, ...]:
        return tuple(
            sorted(
                record.thread.session_number
                for record in self.records
                if record.thread.origin.subject_id == subject_id
                and record.thread.origin.production_run_id == production_run_id
                and record.thread.origin.occurrence_id == occurrence_id
                and record.thread.session_number < before_session_number
            )
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
    ):
        if self.record is None:
            raise AssertionError("thread must exist before append")
        record = self.record
        if record.thread.id != thread_id:
            raise AssertionError("append must use the selected thread")
        existing = next(
            (event for event in record.events if event.idempotency_key == idempotency_key),
            None,
        )
        updated, event = record.thread.append_event(
            kind=kind,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            payload_blob_ref=payload_blob_ref,
            payload_sha256=payload_sha256,
            proposal_id=proposal_id,
            model_turn_id=model_turn_id,
            existing_event=existing,
        )
        if existing is not None:
            return event
        self.record = replace(record, thread=updated, events=(*record.events, event))
        return event


class _MemoryUow:
    def __init__(self, repository: _MemoryThreadRepository) -> None:
        self.editorial_threads = repository
        self.model_conversation_turns = SimpleNamespace(
            get_for_conversation=lambda *_args, **_kwargs: pytest.fail(
                "thread reads must not expose private model turns"
            )
        )

    async def commit(self) -> None:
        self.editorial_threads.commit_count += 1


def _uow_factory(repository: _MemoryThreadRepository):
    @asynccontextmanager
    async def create_uow():
        yield _MemoryUow(repository)

    return create_uow


class _Workspace:
    def __init__(self, records: dict[UUID, WorkspaceSnapshotRecord]) -> None:
        self.records = records

    async def resolve_occurrence(self, subject_id: UUID, occurrence_id: UUID):
        record = self.records.get(subject_id)
        if record is None:
            raise EditorialWorkspaceError("subject_not_found")
        return resolve_occurrence(record, occurrence_id)


class _MemoryArtifactStore:
    def __init__(self, payloads: dict[UUID, bytes]) -> None:
        self.payloads = payloads
        self.reads: list[UUID] = []

    async def read_bytes(self, blob_id: UUID) -> bytes:
        self.reads.append(blob_id)
        return self.payloads[blob_id]


@pytest.mark.asyncio
async def test_get_unopened_thread_creates_no_row() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    current = _binding(subject_id, occurrence_id)
    repository = _MemoryThreadRepository()
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(current)}),
        artifact_store=_MemoryArtifactStore({}),
    )

    result = await service.read(subject_id, occurrence_id)

    assert result.kind == "unopened"
    assert result.binding == current
    assert repository.lookups == [(subject_id, current.production_run_id, occurrence_id)]
    assert repository.created == 0


def test_snapshot_and_thread_reads_share_the_current_occurrence_resolver() -> None:
    binding = _binding(uuid4(), uuid4())
    record = _workspace_record(binding)
    content = {"sections": [{"paragraphs": [{"text": "Synthetic paragraph"}]}]}
    entry = replace(
        record.block_registry[0],
        content_sha256=canonical_content_sha256(content["sections"][0]["paragraphs"][0]),
    )
    record = replace(record, block_registry=(entry,))

    resolution = resolve_occurrence(record, binding.occurrence_id)
    projected, _enrichments, _outline = project_blocks(
        entries=record.block_registry,
        synthesis=content,
        enrichment=None,
        record=record,
        allow_operations=False,
    )

    assert resolution.binding_state == "valid"
    assert resolution.artifact_id == binding.artifact_id
    assert projected[0]["block_id"] == str(binding.occurrence_id)
    assert projected[0]["text"] == "Synthetic paragraph"


def test_missing_and_unproven_registry_occurrences_resolve_stale() -> None:
    binding = _binding(uuid4(), uuid4())
    record = _workspace_record(binding)
    missing = resolve_occurrence(record, uuid4())
    unproven = resolve_occurrence(_workspace_record(binding, "stale"), binding.occurrence_id)
    ambiguous = resolve_occurrence(
        _workspace_record(binding, "needs_review"), binding.occurrence_id
    )

    assert missing.entry is None
    assert missing.binding_state == "stale"
    assert unproven.binding_state == "stale"
    assert ambiguous.binding_state == "ambiguous"


@pytest.mark.asyncio
async def test_thread_origin_is_immutable_and_ambiguous_lineage_stales() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    origin = _binding(subject_id, occurrence_id)
    thread = _thread(origin)
    blob_id = uuid4()
    body = b"must stay unread while continuity is ambiguous"
    thread, event = thread.append_event(
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=uuid4(),
        idempotency_key="ambiguous-message",
        request_sha256="a" * 64,
        payload_blob_ref=blob_id,
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=thread, events=(event,)))
    payloads = _MemoryArtifactStore({blob_id: body})
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(origin, "needs_review")}),
        artifact_store=payloads,
    )

    with pytest.raises(FrozenInstanceError):
        thread.origin.artifact_id = uuid4()  # type: ignore[misc]

    result = await service.read(subject_id, occurrence_id)

    assert result.kind == "stale"
    assert result.binding_state is EditorialBindingState.AMBIGUOUS
    assert result.origin is None
    assert result.thread_id is None
    assert result.event_sequence == 0
    assert result.timeline == ()
    assert repository.lookups == []
    assert payloads.reads == []


@pytest.mark.asyncio
async def test_unproven_current_occurrence_returns_stale_without_thread_lookup() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    current = _binding(subject_id, occurrence_id)
    thread = _thread(current)
    blob_id = uuid4()
    body = b"must stay unread while continuity is unproven"
    thread, event = thread.append_event(
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=uuid4(),
        idempotency_key="stale-message",
        request_sha256="c" * 64,
        payload_blob_ref=blob_id,
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=thread, events=(event,)))
    payloads = _MemoryArtifactStore({blob_id: body})
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(current, "stale")}),
        artifact_store=payloads,
    )

    result = await service.read(subject_id, occurrence_id)

    assert result.kind == "stale"
    assert result.current == current
    assert result.binding_state is EditorialBindingState.STALE
    assert result.timeline == ()
    assert result.thread_id is None
    assert repository.lookups == []
    assert payloads.reads == []


@pytest.mark.asyncio
async def test_enrichment_occurrence_uses_its_owning_artifact_binding() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    current = _binding(subject_id, occurrence_id)
    repository = _MemoryThreadRepository()
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace(
            {
                subject_id: _workspace_record(
                    current,
                    source_stage=EditorialSourceStage.EDITORIAL_ENRICHMENT,
                )
            }
        ),
        artifact_store=_MemoryArtifactStore({}),
    )

    result = await service.read(subject_id, occurrence_id)

    assert result.kind == "unopened"
    assert result.binding == current


@pytest.mark.asyncio
async def test_proven_current_pin_advance_opens_thread_with_immutable_origin() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    origin = _binding(subject_id, occurrence_id)
    current = replace(
        origin,
        artifact_id=uuid4(),
        artifact_version=origin.artifact_version + 1,
        canonical_sha256="b" * 64,
    )
    thread = _thread(origin)
    body = b"continuity follows the current registry"
    blob_id = uuid4()
    thread, event = thread.append_event(
        kind=EditorialThreadEventKind.BRIDGE_REPLY,
        actor_id=None,
        idempotency_key="advanced-version-reply",
        request_sha256="b" * 64,
        payload_blob_ref=blob_id,
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=thread, events=(event,)))
    payloads = _MemoryArtifactStore({blob_id: body})
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(current)}),
        artifact_store=payloads,
    )

    result = await service.read(subject_id, occurrence_id)

    assert result.kind == "open"
    assert result.origin == origin
    assert result.current == current
    assert result.origin.artifact_id != result.current.artifact_id
    assert result.timeline[0].body == body.decode()
    assert repository.lookups == [(subject_id, current.production_run_id, occurrence_id)]


@pytest.mark.asyncio
async def test_read_selects_highest_session_number_for_current_occurrence() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    binding = _binding(subject_id, occurrence_id)
    older = replace(_thread(binding), session_number=1)
    current = replace(_thread(binding), session_number=2)
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=older, events=()))
    repository.records.append(EditorialThreadRecord(thread=current, events=()))
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(binding)}),
        artifact_store=_MemoryArtifactStore({}),
    )

    result = await service.read(subject_id, occurrence_id)

    assert result.kind == "open"
    assert result.thread_id == current.id


def test_lost_session_can_start_a_new_immutable_session() -> None:
    origin = _binding(uuid4(), uuid4())
    lost = replace(_thread(origin), bridge_status=EditorialBridgeStatus.SESSION_LOST)
    current = replace(origin, artifact_id=uuid4(), artifact_version=2)

    next_session = lost.start_new_session(origin=current, model_conversation_id=uuid4())

    assert next_session.session_number == lost.session_number + 1
    assert next_session.origin == current
    assert next_session.bridge_status is EditorialBridgeStatus.READY
    assert next_session.next_sequence == 1
    assert next_session.version == 1
    with pytest.raises(EditorialThreadError, match="editorial_thread_session_not_lost"):
        _thread(origin).start_new_session(origin=current, model_conversation_id=uuid4())


@pytest.mark.asyncio
async def test_other_run_thread_is_not_loaded_or_used_as_an_etag_oracle() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    current = _binding(subject_id, occurrence_id)
    old_run = replace(current, production_run_id=uuid4())
    old_thread = _thread(old_run)
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=old_thread, events=()))
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(current)}),
        artifact_store=_MemoryArtifactStore({}),
    )

    first = await service.read(subject_id, occurrence_id)
    second = await service.read(subject_id, occurrence_id, etag=first.etag)

    assert first.kind == "unopened"
    assert second.kind == "unopened"
    assert second.not_modified is True
    assert repository.lookups == [
        (subject_id, current.production_run_id, occurrence_id),
        (subject_id, current.production_run_id, occurrence_id),
    ]


@pytest.mark.asyncio
async def test_missing_occurrence_uses_same_neutral_404_as_unknown_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    binding = _binding(subject_id, occurrence_id)
    repository = _MemoryThreadRepository()
    workspace = _Workspace({subject_id: _workspace_record(binding)})
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=workspace,
        artifact_store=_MemoryArtifactStore({}),
    )
    monkeypatch.setattr(editorial_workspace_api, "_thread_service", lambda _request: service)

    with pytest.raises(HTTPException) as missing_occurrence:
        await editorial_workspace_api.get_editorial_thread(
            subject_id, uuid4(), None, after_sequence=0, if_none_match='"probe"'
        )
    workspace.records.clear()
    with pytest.raises(HTTPException) as unknown_subject:
        await editorial_workspace_api.get_editorial_thread(
            subject_id, occurrence_id, None, after_sequence=0, if_none_match='"probe"'
        )

    assert missing_occurrence.value.status_code == unknown_subject.value.status_code == 404
    assert (
        missing_occurrence.value.detail
        == unknown_subject.value.detail
        == {
            "code": "thread_not_found",
            "message": "Discussion introuvable.",
        }
    )
    assert repository.lookups == []


def test_thread_events_append_only_and_sequence_unique() -> None:
    binding = _binding(uuid4(), uuid4())
    thread = _thread(binding)
    body = b"synthetic discussion body"
    body_sha256 = hashlib.sha256(body).hexdigest()
    blob_id = uuid4()

    first, event = thread.append_event(
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=uuid4(),
        idempotency_key="message-1",
        request_sha256="b" * 64,
        payload_blob_ref=blob_id,
        payload_sha256=body_sha256,
    )
    replay, existing = first.append_event(
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=event.actor_id,
        idempotency_key="message-1",
        request_sha256="b" * 64,
        payload_blob_ref=blob_id,
        payload_sha256=body_sha256,
        existing_event=event,
    )
    second, next_event = first.append_event(
        kind=EditorialThreadEventKind.GENERATION,
        actor_id=None,
        idempotency_key="generation-1",
        request_sha256="c" * 64,
    )

    assert event.sequence == 1
    assert next_event.sequence == 2
    assert replay == first
    assert existing == event
    assert second.next_sequence == 3
    assert not hasattr(event, "body")
    assert not hasattr(event, "text")
    with pytest.raises(EditorialThreadConflict, match="idempotency_conflict"):
        first.append_event(
            kind=EditorialThreadEventKind.USER_MESSAGE,
            actor_id=event.actor_id,
            idempotency_key="message-1",
            request_sha256="d" * 64,
            payload_blob_ref=blob_id,
            payload_sha256=body_sha256,
            existing_event=event,
        )


@pytest.mark.asyncio
async def test_thread_event_cannot_reference_another_conversations_turn() -> None:
    binding = _binding(uuid4(), uuid4())
    thread = _thread(binding)

    class _Result:
        def one_or_none(self):
            return uuid4(), binding.subject_id

    class _Session:
        def __init__(self) -> None:
            self.scalar_values = [
                SimpleNamespace(
                    id=thread.id,
                    subject_id=binding.subject_id,
                    production_run_id=binding.production_run_id,
                    occurrence_id=binding.occurrence_id,
                    session_number=thread.session_number,
                    origin_artifact_id=binding.artifact_id,
                    origin_artifact_version=binding.artifact_version,
                    origin_canonical_sha256=binding.canonical_sha256,
                    model_conversation_id=thread.model_conversation_id,
                    generation_state=thread.bridge_status.value,
                    next_sequence=thread.next_sequence,
                    version=thread.version,
                    created_at=thread.created_at,
                    updated_at=thread.updated_at,
                ),
                None,
            ]
            self.added: list[object] = []

        async def scalar(self, _statement):
            return self.scalar_values.pop(0)

        async def execute(self, _statement):
            return _Result()

        def add(self, row) -> None:
            self.added.append(row)

        async def flush(self) -> None:
            return None

    session = _Session()
    repository = SqlAlchemyEditorialThreadRepository(session)  # type: ignore[arg-type]

    with pytest.raises(EditorialThreadError, match="model_turn_thread_mismatch"):
        await repository.append_event(
            thread_id=thread.id,
            kind=EditorialThreadEventKind.BRIDGE_REPLY,
            actor_id=None,
            idempotency_key="foreign-turn",
            request_sha256="d" * 64,
            model_turn_id=uuid4(),
        )
    assert session.added == []


def test_editorial_discussion_purpose_is_continuable() -> None:
    conversation = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.EDITORIAL_DISCUSSION,
        title="Synthetic editorial discussion",
        status=ConversationStatus.READY,
        head_turn_id=uuid4(),
    )

    assert conversation.purpose in CONTINUABLE_PURPOSES
    conversation.start_turn(mode=ConversationMode.CONTINUE)
    assert conversation.status is ConversationStatus.BUSY


@pytest.mark.asyncio
async def test_generic_create_rejects_editorial_discussion_and_keeps_other_purposes() -> None:
    class _ConversationRepository:
        def __init__(self) -> None:
            self.added: list[ModelConversation] = []

        async def add(self, conversation: ModelConversation) -> None:
            self.added.append(conversation)

    conversations = _ConversationRepository()

    class _LookupRepository:
        async def get(self, _id):
            return None

    class _Uow:
        model_conversations = conversations
        subjects = _LookupRepository()
        editions = _LookupRepository()

        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def uow_factory():
        yield _Uow()

    service = ModelConversationService(uow_factory, None, None)  # type: ignore[arg-type]
    arguments = {
        "provider": ModelProvider.OPENAI,
        "transport": ConversationTransport.CHATGPT_BRIDGE,
        "title": "Synthetic discussion",
        "edition_id": None,
        "subject_id": None,
        "expected_profile": None,
        "requested_model": None,
    }

    with pytest.raises(ModelConversationError) as reserved:
        await service.create(purpose=ConversationPurpose.EDITORIAL_DISCUSSION, **arguments)
    assert reserved.value.code == "purpose_reserved_for_editorial_threads"

    existing = await service.create(purpose=ConversationPurpose.DRAFTING, **arguments)
    assert existing.purpose is ConversationPurpose.DRAFTING


@pytest.mark.asyncio
async def test_generic_add_turn_rejects_editorial_discussion_replay() -> None:
    subject_id = uuid4()
    conversation = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.EDITORIAL_DISCUSSION,
        title="Synthetic editorial discussion",
        subject_id=subject_id,
        status=ConversationStatus.READY,
    )
    message = "Synthetic replay"
    existing_turn = ModelConversationTurn(
        conversation_id=conversation.id,
        sequence=1,
        model_run_id=uuid4(),
        input_blob_reference="blob://synthetic",
        input_sha256=hashlib.sha256(message.encode()).hexdigest(),
        idempotency_key="synthetic-editorial-turn",
        status=ConversationTurnStatus.SUCCEEDED,
    )

    class _ConversationRepository:
        async def get(self, _conversation_id):
            return conversation

    class _TurnRepository:
        async def get_by_idempotency_key(self, _key):
            return existing_turn

    class _Uow:
        model_conversations = _ConversationRepository()
        model_conversation_turns = _TurnRepository()

    @asynccontextmanager
    async def uow_factory():
        yield _Uow()

    service = ModelConversationService(uow_factory, None, None)  # type: ignore[arg-type]

    with pytest.raises(ModelConversationError) as reserved:
        await service.add_turn(
            conversation.id,
            message=message,
            mode=ConversationMode.FRESH,
            external_llm_allowed=True,
            idempotency_key="synthetic-editorial-turn",
            correlation_id="synthetic-correlation",
            context_subject_id=subject_id,
        )
    assert reserved.value.code == "conversation_not_found"


@pytest.mark.asyncio
async def test_event_append_commits_thread_and_event_together() -> None:
    binding = _binding(uuid4(), uuid4())
    thread = _thread(binding)
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=thread, events=()))
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({}),
        artifact_store=_MemoryArtifactStore({}),
    )

    event = await service.append_event(
        thread_id=thread.id,
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=uuid4(),
        idempotency_key="synthetic-append",
        request_sha256="f" * 64,
    )

    assert event.sequence == 1
    assert repository.record is not None
    assert repository.record.thread.next_sequence == 2
    assert repository.record.events == (event,)
    assert repository.commit_count == 1


@pytest.mark.asyncio
async def test_thread_read_supports_after_sequence_and_etag() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    binding = _binding(subject_id, occurrence_id)
    thread = _thread(binding)
    thread, first = thread.append_event(
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=uuid4(),
        idempotency_key="one",
        request_sha256="1" * 64,
    )
    thread, second = thread.append_event(
        kind=EditorialThreadEventKind.BRIDGE_REPLY,
        actor_id=None,
        idempotency_key="two",
        request_sha256="2" * 64,
    )
    repository = _MemoryThreadRepository(
        EditorialThreadRecord(thread=thread, events=(first, second))
    )
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(binding)}),
        artifact_store=_MemoryArtifactStore({}),
    )

    initial = await service.read(subject_id, occurrence_id)
    incremental = await service.read(subject_id, occurrence_id, after_sequence=1)
    unchanged = await service.read(subject_id, occurrence_id, etag=initial.etag)

    assert [item.sequence for item in initial.timeline] == [1, 2]
    assert [item.sequence for item in incremental.timeline] == [2]
    assert incremental.etag != initial.etag
    assert unchanged.not_modified is True


@pytest.mark.asyncio
async def test_thread_route_returns_etag_and_honors_if_none_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    binding = _binding(subject_id, occurrence_id)
    repository = _MemoryThreadRepository()
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(binding)}),
        artifact_store=_MemoryArtifactStore({}),
    )
    monkeypatch.setattr(editorial_workspace_api, "_thread_service", lambda _request: service)
    first = await editorial_workspace_api.get_editorial_thread(
        subject_id,
        occurrence_id,
        None,  # type: ignore[arg-type]
        after_sequence=0,
        if_none_match=None,
    )
    first_body = json.loads(first.body)  # type: ignore[union-attr]
    second = await editorial_workspace_api.get_editorial_thread(
        subject_id,
        occurrence_id,
        None,  # type: ignore[arg-type]
        after_sequence=0,
        if_none_match=first.headers["ETag"],  # type: ignore[union-attr]
    )

    assert first.status_code == 200  # type: ignore[union-attr]
    assert first_body["kind"] == "unopened"
    assert first.headers["ETag"] == first_body["etag"]  # type: ignore[union-attr]
    assert second.status_code == 304  # type: ignore[union-attr]
    assert second.headers["ETag"] == first.headers["ETag"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_thread_api_decodes_message_and_proposal_requests_with_legacy_text_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    binding = _binding(subject_id, occurrence_id)
    thread = _thread(binding)
    artifact_store = _MemoryArtifactStore({})
    expected_bodies = (
        ("message", "Texte utilisateur du message."),
        ("proposal", "Instruction utilisateur pour proposer."),
        ("message", "Ancien message M2 en texte brut."),
    )
    events = []
    for index, (objective, text) in enumerate(expected_bodies, start=1):
        body = encode_thread_request(text, objective) if index < 3 else text.encode("utf-8")
        blob_id = uuid4()
        artifact_store.payloads[blob_id] = body
        thread, event = thread.append_event(
            kind=EditorialThreadEventKind.USER_MESSAGE,
            actor_id=uuid4(),
            idempotency_key=f"thread-read-{index}",
            request_sha256=str(index) * 64,
            payload_blob_ref=blob_id,
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )
        events.append(event)
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread, tuple(events)))
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace({subject_id: _workspace_record(binding)}),
        artifact_store=artifact_store,
    )
    monkeypatch.setattr(editorial_workspace_api, "_thread_service", lambda _request: service)

    response = await editorial_workspace_api.get_editorial_thread(
        subject_id,
        occurrence_id,
        None,  # type: ignore[arg-type]
        after_sequence=0,
        if_none_match=None,
    )

    assert response.status_code == 200  # type: ignore[union-attr]
    timeline = json.loads(response.body)["timeline"]  # type: ignore[union-attr]
    assert [item["body"] for item in timeline] == [text for _, text in expected_bodies]


def test_proposal_context_offers_only_operations_with_a_preparation_path() -> None:
    from cti_app.application.editorial_discussion_context import (
        _supported_proposal_operations,
    )

    assert _supported_proposal_operations(
        stage="synthesis",
        block_kind="title",
        offered_operations={"rewrite_title", "rewrite_section"},
        paragraph_validation_ready=False,
    ) == ("rewrite_title",)
    assert _supported_proposal_operations(
        stage="synthesis",
        block_kind="paragraph",
        offered_operations={"rewrite_paragraph", "rewrite_section"},
        paragraph_validation_ready=True,
    ) == ("rewrite_paragraph",)
    assert (
        _supported_proposal_operations(
            stage="synthesis",
            block_kind="paragraph",
            offered_operations={"rewrite_paragraph"},
            paragraph_validation_ready=False,
        )
        == ()
    )
    assert (
        _supported_proposal_operations(
            stage="editorial_enrichment",
            block_kind="figure",
            offered_operations={"rewrite_title", "rewrite_paragraph"},
            paragraph_validation_ready=True,
        )
        == ()
    )


@pytest.mark.asyncio
async def test_thread_bodies_and_model_turns_use_subject_acl() -> None:
    owner_id, other_subject_id, occurrence_id = uuid4(), uuid4(), uuid4()
    binding = _binding(owner_id, occurrence_id)
    thread = _thread(binding)
    blob_id = uuid4()
    body = b"synthetic private body"
    thread, event = thread.append_event(
        kind=EditorialThreadEventKind.USER_MESSAGE,
        actor_id=uuid4(),
        idempotency_key="private-message",
        request_sha256="e" * 64,
        payload_blob_ref=blob_id,
        payload_sha256=hashlib.sha256(body).hexdigest(),
    )
    repository = _MemoryThreadRepository(EditorialThreadRecord(thread=thread, events=(event,)))
    payloads = _MemoryArtifactStore({blob_id: body})
    service = EditorialThreadService(
        uow_factory=_uow_factory(repository),
        workspace_service=_Workspace(
            {
                owner_id: _workspace_record(binding),
                other_subject_id: _workspace_record(_binding(other_subject_id, occurrence_id)),
            }
        ),
        artifact_store=payloads,
    )

    denied = await service.read(other_subject_id, occurrence_id)
    allowed = await service.read(owner_id, occurrence_id)

    assert denied.kind == "unopened"
    assert payloads.reads == [blob_id]
    assert allowed.timeline[0].body == body.decode()
    assert not hasattr(allowed, "model_conversation_id")
    assert not hasattr(allowed.timeline[0], "model_turn_id")
