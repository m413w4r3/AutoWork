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

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_diagnostics import ProductionDiagnosticsLog
from cti_app.application.production_parsers import reference_report_from_json
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.collection import CollectionState
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
        turn = _Turn(id=uuid4(), text=self._answers.pop(0))
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


class _CollectionService:
    """Accepts supplemental sources and marks them archived immediately."""

    def __init__(self, collections: _Collections) -> None:
        self._collections = collections
        self.added: list[Any] = []
        self.collected = 0

    async def add_supplemental_sources(self, subject_id: UUID, sources: Any) -> list[Any]:
        for source in sources:
            self._collections.items.append(
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
                    },
                )()
            )
            self.added.append(source)
        return list(self.added)

    async def collect_subject(self, subject_id: UUID, job_id: UUID, context: Any) -> None:
        self.collected += 1


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.run = run

    async def get(self, run_id: UUID) -> SubjectProductionRun:
        return self.run

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun:
        return self.run

    async def save(self, run: SubjectProductionRun) -> None:
        self.run = run


@dataclass
class _Uow:
    run: SubjectProductionRun
    production_artifacts: _Artifacts = field(default_factory=_Artifacts)
    source_collections: _Collections = field(default_factory=_Collections)
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
    answers: list[str], diagnostics: ProductionDiagnosticsLog | None = None
) -> tuple[ProductionWorkflowOrchestrator, _Uow, _FakeConversations]:
    run = SubjectProductionRun(
        subject_id=uuid4(), edition_id=uuid4(), profile=ProductionProfile.BRIEF_AUTO
    )
    run.start_running()
    uow = _Uow(run=run)
    conversations = _FakeConversations(answers)
    store = ProductionArtifactStore(_Blobs())  # type: ignore[arg-type]
    orchestrator = ProductionWorkflowOrchestrator(
        lambda: uow,  # type: ignore[arg-type]
        model_service=conversations,  # type: ignore[arg-type]
        collection_service=_CollectionService(uow.source_collections),  # type: ignore[arg-type]
        artifact_store=store,
        diagnostics=diagnostics,
    )
    return orchestrator, uow, conversations


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


async def test_extraction_reuses_the_same_conversation_and_the_q1_corpus() -> None:
    orchestrator, uow, conversations = _build([PERFECT_Q1, PERFECT_Q2])
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

    # One conversation, Q1 fresh then Q2 continue.
    assert len(conversations.created) == 1
    assert [mode for _, mode, _, _ in conversations.calls] == [
        ConversationMode.FRESH,
        ConversationMode.CONTINUE,
    ]
    assert len({cid for cid, _, _, _ in conversations.calls}) == 1


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
        f"references-{uow.run.id}-v1",
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
    log = ProductionDiagnosticsLog.from_env(tmp_path)
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
    log = ProductionDiagnosticsLog.from_env(tmp_path)
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
