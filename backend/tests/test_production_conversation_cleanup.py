"""Tests for cleanup of completed Q1/Q4 production conversations."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.production import (
    SubjectProductionRun,
    SubjectProductionStage,
)
from cti_app.integrations.models import BridgeTransportError


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.run = run

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.run if run_id == self.run.id else None


class _Snapshots:
    async def get_by_run(self, run_id: UUID) -> object:
        return object()


class _Uow:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.subject_production_runs = _Runs(run)
        self.production_input_snapshots = _Snapshots()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Diagnostics:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **fields: Any) -> None:
        self.events.append(fields)

    def record_stage_outcome(self, **fields: Any) -> None:
        self.events.append(fields)


class _ModelService:
    def __init__(self, failure: Exception | None = None) -> None:
        self.archived: list[UUID] = []
        self.failure = failure

    async def archive(self, conversation_id: UUID, *, context_subject_id: UUID) -> None:
        if self.failure is not None:
            raise self.failure
        self.archived.append(conversation_id)


def _run(stage: SubjectProductionStage) -> SubjectProductionRun:
    run = SubjectProductionRun(subject_id=uuid4(), edition_id=uuid4())
    run.start_running()
    run.current_stage = stage
    return run


def _orchestrator(
    run: SubjectProductionRun,
    model_service: _ModelService,
    diagnostics: _Diagnostics,
) -> ProductionWorkflowOrchestrator:
    return ProductionWorkflowOrchestrator(
        lambda: _Uow(run),  # type: ignore[arg-type]
        model_service=model_service,  # type: ignore[arg-type]
        diagnostics=diagnostics,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "conversation_field"),
    [
        (SubjectProductionStage.REFERENCES, "references_conversation_id"),
        (SubjectProductionStage.SYNTHESIS, "synthesis_conversation_id"),
    ],
)
async def test_completed_references_and_synthesis_archive_conversation(
    stage: SubjectProductionStage,
    conversation_field: str,
) -> None:
    run = _run(stage)
    conversation_id = uuid4()
    setattr(run, conversation_field, conversation_id)
    model_service = _ModelService()
    orchestrator = _orchestrator(run, model_service, _Diagnostics())

    async def completed(*args: object, **kwargs: object) -> dict[str, str]:
        return {"stage": stage.value, "status": "success"}

    setattr(orchestrator, f"_execute_{stage.value}_stage", completed)

    result = await orchestrator.execute_stage(run.id, stage)

    assert result["status"] == "success"
    assert model_service.archived == [conversation_id]


@pytest.mark.asyncio
async def test_needs_review_keeps_synthesis_conversation_open() -> None:
    run = _run(SubjectProductionStage.SYNTHESIS)
    run.synthesis_conversation_id = uuid4()
    model_service = _ModelService()
    orchestrator = _orchestrator(run, model_service, _Diagnostics())

    async def needs_review(*args: object, **kwargs: object) -> dict[str, str]:
        return {"stage": "synthesis", "status": "needs_review"}

    orchestrator._execute_synthesis_stage = needs_review  # type: ignore[method-assign]

    await orchestrator.execute_stage(run.id, SubjectProductionStage.SYNTHESIS)

    assert model_service.archived == []


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_change_success_and_is_diagnosed() -> None:
    run = _run(SubjectProductionStage.SYNTHESIS)
    run.synthesis_conversation_id = uuid4()
    diagnostics = _Diagnostics()
    orchestrator = _orchestrator(
        run,
        _ModelService(
            BridgeTransportError(
                "bridge_extension_disconnected",
                "Extension Chrome non connectée.",
                retryable=True,
                phase="conversation_archive",
                conversation_id=str(run.synthesis_conversation_id),
                diagnostics={"tab_id": 11, "window_id": 12},
            )
        ),
        diagnostics,
    )

    async def completed(*args: object, **kwargs: object) -> dict[str, str]:
        return {"stage": "synthesis", "status": "success"}

    orchestrator._execute_synthesis_stage = completed  # type: ignore[method-assign]

    result = await orchestrator.execute_stage(run.id, SubjectProductionStage.SYNTHESIS)

    assert result["status"] == "success"
    failures = [
        event
        for event in diagnostics.events
        if event.get("event") == "production.conversation_close_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["conversation_id"] == str(run.synthesis_conversation_id)
    assert failures[0]["error_code"] == "bridge_extension_disconnected"
    assert failures[0]["cause_code"] == "bridge_extension_disconnected"
    assert failures[0]["retryable"] is True
    assert failures[0]["phase"] == "conversation_archive"
    assert failures[0]["details"] == {"tab_id": 11, "window_id": 12}
    assert "Extension Chrome non connectée" in failures[0]["error_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [
    SubjectProductionStage.SOURCES,
    SubjectProductionStage.EXTRACTION,
    SubjectProductionStage.ASSEMBLY,
])
async def test_stage_without_conversation_does_not_archive(stage: SubjectProductionStage) -> None:
    run = _run(stage)
    model_service = _ModelService()
    orchestrator = _orchestrator(run, model_service, _Diagnostics())

    async def completed(*args: object, **kwargs: object) -> dict[str, str]:
        return {"stage": stage.value, "status": "success"}

    setattr(orchestrator, f"_execute_{stage.value}_stage", completed)

    await orchestrator.execute_stage(run.id, stage)

    assert model_service.archived == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cached", "reused"])
@pytest.mark.parametrize(
    ("stage", "conversation_field"),
    [
        (SubjectProductionStage.REFERENCES, "references_conversation_id"),
        (SubjectProductionStage.SYNTHESIS, "synthesis_conversation_id"),
    ],
)
async def test_cached_and_reused_results_retry_conversation_cleanup(
    stage: SubjectProductionStage,
    conversation_field: str,
    status: str,
) -> None:
    run = _run(stage)
    conversation_id = uuid4()
    setattr(run, conversation_field, conversation_id)
    model_service = _ModelService()
    orchestrator = _orchestrator(run, model_service, _Diagnostics())

    async def cached_or_reused(*args: object, **kwargs: object) -> dict[str, str]:
        return {"stage": stage.value, "status": status}

    setattr(orchestrator, f"_execute_{stage.value}_stage", cached_or_reused)

    await orchestrator.execute_stage(run.id, stage)

    assert model_service.archived == [conversation_id]
