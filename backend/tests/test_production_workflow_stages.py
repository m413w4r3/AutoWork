"""Q1 → Q2 orchestration against a fake conversation service.

What matters here is the handover: Q1's parsed report has to survive as a blob
and come back to constrain Q2, and a badly formatted answer must trigger exactly
one repair turn — never a second web search.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_evidence_pack import EvidenceChunk, ProductionEvidencePack
from cti_app.application.production_parsers import (
    parse_reference_report,
    reference_report_from_json,
)
from cti_app.application.production_prompts import REFERENCES_PROMPT_VERSION
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.source_evidence_processing import (
    ReferencedEvidenceLink,
    SourceEvidenceProcessingResult,
)
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.model_conversations import ConversationMode
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
)
from tests.test_production_parsers import PERFECT_Q1, PERFECT_Q2


class _JobContext:
    """Minimal job context: the collection pass needs a job id."""

    def __init__(self) -> None:
        self.job_id = uuid4()

    async def report_progress(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def check_cancelled(self) -> None:
        return None


BROKEN_Q1 = "Je n'ai pas trouvé de structure claire, voici mes notes en vrac."


@dataclass
class _Turn:
    id: UUID
    text: str


class _FakeConversations:
    """Replays canned answers and records how it was called."""

    def __init__(self, answers: list[str]) -> None:
        self._answers = answers
        self.calls: list[tuple[UUID, ConversationMode, str, str]] = []
        self._turns: dict[UUID, list[_Turn]] = {}
        self._turns_by_idempotency_key: dict[str, _Turn] = {}
        self.created: list[UUID] = []

    async def create(self, **kwargs: Any) -> Any:
        conversation_id = uuid4()
        self.created.append(conversation_id)
        self._turns[conversation_id] = []
        return type("Conversation", (), {"id": conversation_id})()

    async def add_turn(
        self,
        conversation_id: UUID,
        *,
        message: str,
        mode: ConversationMode,
        idempotency_key: str,
        **kwargs: Any,
    ) -> _Turn:
        self.calls.append((conversation_id, mode, idempotency_key, message))
        if existing := self._turns_by_idempotency_key.get(idempotency_key):
            return existing
        turn = _Turn(id=uuid4(), text=self._answers.pop(0))
        self._turns_by_idempotency_key[idempotency_key] = turn
        self._turns.setdefault(conversation_id, []).append(turn)
        return turn

    async def turns(self, conversation_id: UUID, **kwargs: Any) -> list[Any]:
        return [
            type("Content", (), {"turn": t, "output_text": t.text})()
            for t in self._turns.get(conversation_id, [])
        ]


class _Blobs:
    def __init__(self) -> None:
        self.data: dict[UUID, bytes] = {}

    async def ingest(self, source: Any, *, logical_bucket: str, mime_type: str) -> Any:
        blob_id = uuid4()
        self.data[blob_id] = source.read()
        return type("Record", (), {"id": blob_id})()

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return self.data[blob_id]


class _ArchiveProcessor:
    def __init__(self, texts: dict[UUID, str]) -> None:
        self.texts = texts

    async def read_derived_text(self, blob_id: UUID) -> str:
        return self.texts.get(blob_id, "198.51.100.10 malicious.example")

    async def process_subject(self, subject_id: UUID) -> SourceEvidenceProcessingResult:
        del subject_id
        return SourceEvidenceProcessingResult(2, 2, 0, 0, 2, ())

    async def select_referenced_evidence(self, subject_id: UUID) -> tuple[object, ...]:
        del subject_id
        return ()


class _Groups:
    def __init__(self, title: str) -> None:
        self._title = title

    async def get_by_subject(self, subject_id: UUID) -> Any:
        return type(
            "Group",
            (),
            {
                "title": self._title,
                "grouping_justification": "contexte",
                "edition_id": uuid4(),
                "actor_or_campaign": "TAG-182",
            },
        )()


class _Editions:
    async def get(self, edition_id: UUID) -> Any:
        return type(
            "Edition",
            (),
            {"period_start": date(2026, 7, 1), "period_end": date(2026, 7, 31)},
        )()


class _Artifacts:
    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matching = [a for a in self.items if a.stage.value == stage]
        return matching[-1] if matching else None

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return list(self.items)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        return None


class _Collections:
    """Every proposed source is already archived, so nothing is dropped."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    async def list_for_subject(self, subject_id: UUID) -> list[Any]:
        return self.items


class _Documents:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def list_for_subject(self, subject_id: UUID) -> list[Any]:
        del subject_id
        return self.items


class _DerivedArtifacts:
    def __init__(self) -> None:
        self.items: dict[UUID, Any] = {}

    async def get(self, artifact_id: UUID) -> Any:
        return self.items.get(artifact_id)


class _Indicators:
    async def list_for_subject(self, subject_id: UUID) -> list[Any]:
        del subject_id
        return []


class _CollectionService:
    """Accepts supplemental sources and marks them archived immediately."""

    def __init__(self, uow: _Uow) -> None:
        self._uow = uow
        self.added: list[Any] = []
        self.collected = 0
        self.archived: list[UUID] = []

    async def add_supplemental_sources(self, subject_id: UUID, sources: Any) -> list[Any]:
        for source in sources:
            if any(item.canonical_url == source.url for item in self._uow.source_collections.items):
                self.added.append(source)
                continue
            document_id = uuid4()
            artifact_id = uuid4()
            blob_id = uuid4()
            self._uow.source_documents.items.append(
                type(
                    "Document",
                    (),
                    {"id": document_id, "final_url": source.url, "title": source.title},
                )()
            )
            self._uow.derived_artifacts.items[artifact_id] = type(
                "Artifact",
                (),
                {"id": artifact_id, "text_blob_id": blob_id, "parser_version": "test-parser-1"},
            )()
            self._uow.source_collections.items.append(
                type(
                    "Collection",
                    (),
                    {
                        "canonical_url": source.url,
                        "state": CollectionState.ARCHIVED,
                        "title": source.title,
                        "publisher": source.publisher,
                        "published_at": source.published_at,
                        "proposed_role": source.role,
                        "do_not_submit": False,
                        "external_llm_allowed": True,
                        "id": uuid4(),
                        "source_document_id": document_id,
                        "derived_artifact_id": artifact_id,
                        "origin_kind": SourceOriginKind.REFERENCE_RESEARCH,
                        "parent_source_collection_id": None,
                    },
                )()
            )
            self.added.append(source)
        return list(self.added)

    async def collect_subject(self, subject_id: UUID, job_id: UUID, context: Any) -> None:
        self.collected += 1

    async def add_referenced_evidence(self, subject_id: UUID, resources: Any) -> list[Any]:
        del subject_id
        children = []
        for resource in resources:
            document_id = uuid4()
            artifact_id = uuid4()
            self._uow.source_documents.items.append(
                type(
                    "Document",
                    (),
                    {
                        "id": document_id,
                        "final_url": resource.url,
                        "title": resource.anchor_text,
                    },
                )()
            )
            self._uow.derived_artifacts.items[artifact_id] = type(
                "Artifact",
                (),
                {"id": artifact_id, "text_blob_id": uuid4(), "parser_version": "test-parser-1"},
            )()
            child = type(
                "Collection",
                (),
                {
                    "id": uuid4(),
                    "canonical_url": resource.url,
                    "state": CollectionState.ARCHIVED,
                    "title": resource.anchor_text,
                    "publisher": None,
                    "published_at": None,
                    "proposed_role": None,
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                    "source_document_id": document_id,
                    "derived_artifact_id": artifact_id,
                    "origin_kind": SourceOriginKind.REFERENCED_EVIDENCE,
                    "parent_source_collection_id": resource.parent_source_collection_id,
                },
            )()
            self._uow.source_collections.items.append(child)
            children.append(child)
        self.added.extend(resources)
        return children

    async def archive_one(self, collection_id: UUID, job_id: UUID, *, context: Any) -> None:
        del job_id, context
        self.archived.append(collection_id)


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.run = run
        self.locked = 0

    async def get(self, run_id: UUID) -> SubjectProductionRun:
        return self.run

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun:
        self.locked += 1
        return self.run

    async def save(self, run: SubjectProductionRun) -> None:
        self.run = run


@dataclass
class _Uow:
    run: SubjectProductionRun
    production_artifacts: _Artifacts = field(default_factory=_Artifacts)
    source_collections: _Collections = field(default_factory=_Collections)
    source_documents: _Documents = field(default_factory=_Documents)
    derived_artifacts: _DerivedArtifacts = field(default_factory=_DerivedArtifacts)
    indicators: _Indicators = field(default_factory=_Indicators)
    editions: _Editions = field(default_factory=_Editions)
    editorial_groups: _Groups = field(default_factory=lambda: _Groups("TAG-182"))
    subject_production_runs: _Runs = field(init=False)

    def __post_init__(self) -> None:
        self.subject_production_runs = _Runs(self.run)

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _build(
    answers: list[str], diagnostics: DiagnosticsLog | None = None, processor: Any | None = None
) -> tuple[ProductionWorkflowOrchestrator, _Uow, _FakeConversations]:
    run = SubjectProductionRun(
        subject_id=uuid4(), edition_id=uuid4(), profile=ProductionProfile.BRIEF_AUTO
    )
    run.start_running()
    uow = _Uow(run=run)
    for url, _text in (
        ("https://research.example/rapport", "198.51.100.10 malicious.example"),
        ("https://other.example/analyse", "203.0.113.7 evil.example"),
    ):
        document_id, artifact_id, blob_id = uuid4(), uuid4(), uuid4()
        uow.source_documents.items.append(
            type("Document", (), {"id": document_id, "final_url": url, "title": url})()
        )
        uow.derived_artifacts.items[artifact_id] = type(
            "Artifact",
            (),
            {"id": artifact_id, "text_blob_id": blob_id, "parser_version": "test-parser-1"},
        )()
        uow.source_collections.items.append(
            type(
                "Collection",
                (),
                {
                    "id": uuid4(),
                    "canonical_url": url,
                    "state": CollectionState.ARCHIVED,
                    "title": url,
                    "publisher": "test",
                    "published_at": None,
                    "proposed_role": None,
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                    "source_document_id": document_id,
                    "derived_artifact_id": artifact_id,
                        "origin_kind": SourceOriginKind.REFERENCE_RESEARCH,
                    "parent_source_collection_id": None,
                },
            )()
        )
    archive_texts = {
        artifact.text_blob_id: text
        for artifact, text in zip(
            uow.derived_artifacts.items.values(),
            ("198.51.100.10 malicious.example", "203.0.113.7 evil.example"),
            strict=True,
        )
    }
    conversations = _FakeConversations(answers)
    store = ProductionArtifactStore(_Blobs())  # type: ignore[arg-type]
    archive_processor = _ArchiveProcessor(archive_texts)
    if processor is not None and not hasattr(processor, "read_derived_text"):
        processor.read_derived_text = archive_processor.read_derived_text
    orchestrator = ProductionWorkflowOrchestrator(
        lambda: uow,  # type: ignore[arg-type]
        model_service=conversations,  # type: ignore[arg-type]
        collection_service=_CollectionService(uow),  # type: ignore[arg-type]
        artifact_store=store,
        diagnostics=diagnostics,
        source_evidence_processor=processor or archive_processor,
    )
    return orchestrator, uow, conversations


async def test_retry_reuses_the_same_logical_model_turn() -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1])
    conversation_id = (await conversations.create()).id
    run = uow.subject_production_runs.run

    for _ in range(2):
        result, _, _ = await orchestrator._ask_with_format_repair(
            run=run,
            conversation_id=conversation_id,
            stage="references",
            prompt="same prompt",
            prompt_version="1",
            repair_version="1",
            mode=ConversationMode.FRESH,
            parse=lambda value: parse_reference_report(value, date.today()),
            external_llm_allowed=True,
        )
        assert result is not None

    assert len(conversations.calls) == 2
    assert {call[2] for call in conversations.calls} == {f"references-{run.id}-v1"}
    assert len(conversations._turns[conversation_id]) == 1


@pytest.mark.parametrize(
    "stage",
    ("references", "extraction", "synthesis"),
)
async def test_prompt_version_creates_a_new_logical_turn(stage: str) -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1, PERFECT_Q1])
    conversation_id = (await conversations.create()).id

    for prompt_version in ("1", "2"):
        result, _, _ = await orchestrator._ask_with_format_repair(
            run=uow.run,
            conversation_id=conversation_id,
            stage=stage,
            prompt="same prompt",
            prompt_version=prompt_version,
            repair_version="1",
            mode=ConversationMode.FRESH,
            parse=lambda value: parse_reference_report(value, date.today()),
            external_llm_allowed=True,
        )
        assert result is not None

    assert {call[2] for call in conversations.calls} == {
        f"{stage}-{uow.run.id}-v1",
        f"{stage}-{uow.run.id}-v2",
    }
    assert len(conversations._turns[conversation_id]) == 2


async def test_repair_version_creates_a_new_logical_repair_turn() -> None:
    orchestrator, uow, conversations = _build([BROKEN_Q1, PERFECT_Q1, PERFECT_Q1])
    conversation_id = (await conversations.create()).id

    for repair_version in ("1", "2"):
        result, _, _ = await orchestrator._ask_with_format_repair(
            run=uow.run,
            conversation_id=conversation_id,
            stage="references",
            prompt="same prompt",
            prompt_version="1",
            repair_version=repair_version,
            mode=ConversationMode.FRESH,
            parse=lambda value: parse_reference_report(value, date.today()),
            external_llm_allowed=True,
        )
        assert result is not None and result.usable

    assert {
        call[2] for call in conversations.calls if "format-repair" in call[2]
    } == {
        f"references-format-repair-{uow.run.id}-v1",
        f"references-format-repair-{uow.run.id}-v2",
    }


async def test_q2_repairs_have_distinct_chunk_idempotency_keys() -> None:
    orchestrator, uow, conversations = _build([BROKEN_Q1, PERFECT_Q1, BROKEN_Q1, PERFECT_Q1])
    conversation_id = (await conversations.create()).id

    for chunk_id in ("chunk-a", "chunk-b"):
        result, _, _ = await orchestrator._ask_with_format_repair(
            run=uow.run,
            conversation_id=conversation_id,
            stage="extraction",
            prompt="chunk prompt",
            prompt_version="4",
            repair_version="1",
            mode=ConversationMode.FRESH,
            repair_mode=ConversationMode.FRESH,
            request_identity=chunk_id,
            parse=lambda value: parse_reference_report(value, date.today()),
            external_llm_allowed=True,
        )
        assert result is not None and result.usable

    repair_keys = [key for _, _, key, _ in conversations.calls if "format-repair" in key]
    assert len(repair_keys) == 2
    assert len(set(repair_keys)) == 2
    assert all(
        f"-v4-{chunk}-rv1" in key
        for chunk, key in zip(("chunk-a", "chunk-b"), repair_keys, strict=True)
    )
    repair_messages = [
        message for _, _, key, message in conversations.calls if "format-repair" in key
    ]
    assert all("Answer to reformat:\n" + BROKEN_Q1 in message for message in repair_messages)


async def test_references_stage_stores_a_readable_report() -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    assert result["status"] == "success", result
    assert result["sources_count"] == 2
    assert result["events_count"] == 1

    artifact = uow.production_artifacts.items[-1]
    assert artifact.stage is ProductionArtifactStage.REFERENCES
    # The canonical payload must be readable back, not just counted.
    assert artifact.canonical_blob_id is not None
    assert artifact.raw_blob_id is not None
    store = cast(ProductionArtifactStore, orchestrator._artifact_store)
    payload = await store.read_json(artifact.canonical_blob_id)
    assert len(reference_report_from_json(payload).sources) == 2

    # Q1 opens the conversation and asks fresh.
    assert conversations.calls[0][1] is ConversationMode.FRESH


async def test_extraction_uses_independent_stateless_conversations_per_q2_chunk() -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1, PERFECT_Q2, PERFECT_Q2])
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    uow.run.current_stage = SubjectProductionStage.EXTRACTION
    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.EXTRACTION, correlation_id="c1"
    )

    assert result["status"] == "success", result
    assert result["supported_items"] == 3
    assert result["candidate_pack_hash"]
    artifact = uow.production_artifacts.items[-1]
    diagnostics = artifact.metadata["deterministic_verification"]
    assert diagnostics["candidate_pack_hash"] == result["candidate_pack_hash"]
    assert diagnostics["initial_candidate_pack_hash"] == result["initial_candidate_pack_hash"]

    # Q1 and every Q2 chunk use independent fresh conversations.
    assert len(conversations.created) == 3
    assert [mode for _, mode, _, _ in conversations.calls] == [
        ConversationMode.FRESH,
        ConversationMode.FRESH,
        ConversationMode.FRESH,
    ]
    assert len({cid for cid, _, _, _ in conversations.calls}) == 3


async def test_three_q2_chunks_with_one_loss_produce_no_canonical_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, uow, conversations = _build(
        [PERFECT_Q1, PERFECT_Q2, BROKEN_Q1, BROKEN_Q1, PERFECT_Q2]
    )
    chunks = tuple(
        EvidenceChunk(
            source_document_id=uuid4(),
            parent_source_ids=(),
            source_ids=(f"S{index}",),
            title=f"chunk {index}",
            origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
            chunk_id=f"q2-chunk-{index}",
            text=f"archived evidence {index}",
            sha256=f"hash-{index}",
        )
        for index in range(1, 4)
    )

    async def fake_pack(*args: Any, **kwargs: Any) -> ProductionEvidencePack:
        return ProductionEvidencePack("ready", "three-chunk-pack", chunks, {})

    monkeypatch.setattr(orchestrator, "_build_production_evidence_pack", fake_pack)
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.REFERENCES)
    uow.run.current_stage = SubjectProductionStage.EXTRACTION

    result = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)

    assert result["status"] == "needs_review"
    assert result["error_code"] == "q2_chunk_coverage_failed"
    assert result["completed_chunk_ids"] == ["q2-chunk-1", "q2-chunk-3"]
    assert result["failed_chunk_ids"] == ["q2-chunk-2"]
    assert [artifact.stage for artifact in uow.production_artifacts.items] == [
        ProductionArtifactStage.REFERENCES
    ]
    assert len(conversations.created) == 4


async def test_deterministic_evidence_processing_runs_before_q2() -> None:
    class _Processor:
        def __init__(self) -> None:
            self.calls: list[UUID] = []
            self.conversations: _FakeConversations | None = None

        async def process_subject(self, subject_id: UUID) -> SourceEvidenceProcessingResult:
            assert self.conversations is not None
            assert not any(
                key.startswith("extraction-") for _, _, key, _ in self.conversations.calls
            )
            self.calls.append(subject_id)
            return SourceEvidenceProcessingResult(1, 1, 0, 0, 2, ())

        async def select_referenced_evidence(self, subject_id: UUID) -> tuple[object, ...]:
            assert subject_id
            return ()

    processor = _Processor()
    orchestrator, uow, conversations = _build(
        [PERFECT_Q1, PERFECT_Q2, PERFECT_Q2], processor=processor
    )
    processor.conversations = conversations
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    uow.run.current_stage = SubjectProductionStage.EXTRACTION
    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.EXTRACTION, correlation_id="c1"
    )

    assert processor.calls == [uow.run.subject_id]
    assert result["source_evidence_processing"] == {
        "sources_seen": 1,
        "sources_processed": 1,
        "sources_cached": 0,
        "sources_failed": 0,
        "indicator_occurrences": 2,
        "outcomes": [],
    }


async def test_linked_evidence_is_archived_before_deterministic_extraction() -> None:
    class _Processor:
        def __init__(self) -> None:
            self.archived: list[UUID] | None = None

        async def select_referenced_evidence(
            self, subject_id: UUID
        ) -> tuple[ReferencedEvidenceLink, ...]:
            return (ReferencedEvidenceLink(uuid4(), "https://evidence.example/iocs.json", "IOCs"),)

        async def process_subject(self, subject_id: UUID) -> SourceEvidenceProcessingResult:
            assert self.archived is not None and len(self.archived) == 1
            return SourceEvidenceProcessingResult(1, 1, 0, 0, 0, ())

    processor = _Processor()
    orchestrator, uow, _ = _build([PERFECT_Q1, PERFECT_Q2, PERFECT_Q2], processor=processor)
    collection_service = cast(_CollectionService, orchestrator._collection_service)
    processor.archived = collection_service.archived
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.REFERENCES)

    uow.run.current_stage = SubjectProductionStage.EXTRACTION
    result = await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.EXTRACTION,
        context=_JobContext(),
    )

    assert result["status"] == "success"
    assert result["referenced_evidence"] == {"selected": 1, "added": 1}


async def test_linked_evidence_requires_persisted_job_context() -> None:
    class _Processor:
        async def select_referenced_evidence(
            self, subject_id: UUID
        ) -> tuple[ReferencedEvidenceLink, ...]:
            return (ReferencedEvidenceLink(uuid4(), "https://evidence.example/iocs.json", "IOCs"),)

    processor = _Processor()
    orchestrator, uow, _ = _build([PERFECT_Q1, PERFECT_Q2], processor=processor)
    collection_service = cast(_CollectionService, orchestrator._collection_service)
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.REFERENCES)
    uow.run.current_stage = SubjectProductionStage.EXTRACTION

    with pytest.raises(RuntimeError, match="persisted job context"):
        await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)

    assert collection_service.archived == []


async def test_badly_formatted_answer_triggers_one_repair_turn() -> None:
    orchestrator, uow, conversations = _build([BROKEN_Q1, PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    assert result["status"] == "success", result
    assert "references_format_repair" in result["repair_actions"]

    keys = [key for _, _, key, _ in conversations.calls]
    assert keys == [
        f"references-{uow.run.id}-v{REFERENCES_PROMPT_VERSION}",
        f"references-format-repair-{uow.run.id}-v1",
    ]
    # The repair must not be another search.
    assert conversations.calls[1][1] is ConversationMode.CONTINUE
    assert "Do NOT search the web again" in conversations.calls[1][3]


async def test_still_unreadable_after_repair_is_needs_review_not_failed() -> None:
    orchestrator, uow, conversations = _build([BROKEN_Q1, BROKEN_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "references_format_unusable"
    assert len(conversations.calls) == 2


@pytest.mark.parametrize("stage", [SubjectProductionStage.REFERENCES])
async def test_bridge_timeout_is_reported_as_transient(
    stage: SubjectProductionStage,
) -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1])
    uow.run.current_stage = stage

    class _Boom(Exception):
        code = "bridge_timeout"

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise _Boom("bridge timed out")

    conversations.add_turn = explode  # type: ignore[method-assign]

    result = await orchestrator.execute_stage(uow.run.id, stage, correlation_id="c1")

    assert result["status"] == "transient_error"
    assert result["error_code"] == "bridge_timeout"


async def test_new_q1_sources_are_attached_and_collected() -> None:
    """Q1's publications must be integrated before the report is recorded."""
    orchestrator, uow, _ = _build([PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    service = cast(_CollectionService, orchestrator._collection_service)

    result = await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.REFERENCES,
        context=_JobContext(),  # type: ignore[arg-type]
        correlation_id="c1",
    )

    assert result["status"] == "success", result
    assert result["new_sources"] == 2
    assert result["archived_sources"] == 2
    # A collection pass ran for the newly attached URLs.
    assert service.collected == 1
    assert {s.url for s in service.added} == {
        "https://research.example/rapport",
        "https://other.example/analyse",
    }


async def test_event_without_any_archived_source_sends_the_run_to_review() -> None:
    """An unreachable corpus must not silently produce an empty brief."""
    orchestrator, uow, _ = _build([PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    # Nothing ever gets archived.
    uow.source_collections.items.clear()
    uow.source_documents.items.clear()
    uow.derived_artifacts.items.clear()
    async def add_nothing(subject_id: Any, sources: Any) -> list[Any]:
        return []

    service = cast(_CollectionService, orchestrator._collection_service)
    service.add_supplemental_sources = add_nothing  # type: ignore[method-assign]

    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "no_event_with_archived_source"


async def test_source_forbidding_external_model_blocks_the_stage() -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    uow.source_collections.items.append(
        type(
            "Collection",
            (),
            {
                "canonical_url": "https://restricted.example/x",
                "state": CollectionState.ARCHIVED,
                "title": None,
                "publisher": None,
                "published_at": None,
                "proposed_role": None,
                "do_not_submit": True,
                "external_llm_allowed": False,
            },
        )()
    )

    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "external_llm_blocked"
    # Nothing was ever sent to the model.
    assert conversations.calls == []


async def test_diagnostics_trail_captures_the_whole_stage(tmp_path: Path) -> None:
    """After a run, the trail must answer what was asked and what came back."""
    log = DiagnosticsLog.from_env(tmp_path)
    orchestrator, uow, _ = _build([PERFECT_Q1], diagnostics=log)
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.REFERENCES,
        context=_JobContext(),  # type: ignore[arg-type]
        correlation_id="corr-42",
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event["event"] for event in events]
    assert kinds == ["model.answer", "parse.result", "stage.outcome"]
    assert all(event["correlation_id"] == "corr-42" for event in events)

    answer_file = tmp_path / events[0]["payload_file"]
    body = answer_file.read_text(encoding="utf-8")
    assert "--- PROMPT ---" in body
    assert "## SOURCE S1" in body

    assert events[1]["usable"] is True
    assert events[2]["status"] == "success"
    assert events[2]["sources_count"] == 2


async def test_diagnostics_trail_records_a_format_repair(tmp_path: Path) -> None:
    """A repair must be visible as its own model exchange."""
    log = DiagnosticsLog.from_env(tmp_path)
    orchestrator, uow, _ = _build([BROKEN_Q1, PERFECT_Q1], diagnostics=log)
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.REFERENCES,
        context=_JobContext(),  # type: ignore[arg-type]
        correlation_id="corr-43",
    )

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    stages = [event.get("stage") for event in events]
    assert "references-repair" in stages
    # The failed first parse is on record, with the reason.
    first_parse = next(e for e in events if e["event"] == "parse.result")
    assert first_parse["usable"] is False
    assert first_parse["errors"]


async def test_a_stage_never_holds_a_row_lock_across_the_model_call() -> None:
    """A stage spans a full model round-trip.

    Selecting the run `FOR UPDATE` for that whole time deadlocked the stage
    against the unit of work it opens itself: the job hung in `running`, no
    conversation turn was ever created, and the bridge was never called.
    """
    orchestrator, uow, _ = _build([PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.REFERENCES,
        context=_JobContext(),  # type: ignore[arg-type]
        correlation_id="c1",
    )

    # Only the short conversation-id write may take the lock, never the stage.
    assert uow.subject_production_runs.locked <= 1


async def test_lost_conversation_parks_the_subject_instead_of_failing_it() -> None:
    """A conversation whose locator went stale is an operational exception.

    Retrying the same turn cannot help, but the subject is intact and the batch
    must keep moving, so it is a review case rather than a failure.
    """
    orchestrator, uow, conversations = _build([PERFECT_Q1])
    uow.run.current_stage = SubjectProductionStage.REFERENCES

    class _Gone(Exception):
        code = "conversation_unavailable"

    async def explode(*args: Any, **kwargs: Any) -> Any:
        raise _Gone("La conversation ChatGPT est inaccessible.")

    conversations.add_turn = explode  # type: ignore[method-assign]

    result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="c1"
    )

    assert result["status"] == "needs_review"
    assert result["error_code"] == "conversation_unavailable"
