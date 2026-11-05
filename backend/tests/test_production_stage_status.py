"""Stage status exposed to the UI.

SOURCES produces no artifact and ASSEMBLY produces the `publication` artifact, so a
naive artifact-driven mapping reports SOURCES as pending forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.api.production import get_subject_production
from cti_app.application.production_stage_status import (
    build_stage_statuses,
    completed_stage_count,
)
from cti_app.domain.production import (
    ProductionRun,
    ProductionStage,
)
from cti_app.domain.production_routing import RoutingStatus
from cti_app.domain.publication_kind import PublicationKind


def _run(stage: ProductionStage) -> ProductionRun:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
    )
    run.start_running()
    run.current_stage = stage
    return run


def test_sources_is_complete_once_the_run_moved_past_it() -> None:
    run = _run(ProductionStage.REFERENCES)

    stages = build_stage_statuses(run, {}, archived_sources=5)

    assert stages["sources"]["status"] == "succeeded"
    assert stages["sources"]["archived_sources"] == 5
    assert stages["references"]["status"] == "running"
    assert stages["extraction"]["status"] == "pending"


def test_ready_run_reports_every_stage_complete() -> None:
    run = _run(ProductionStage.ASSEMBLY)
    run.mark_ready()

    stages = build_stage_statuses(run, {})

    assert tuple(stages) == (
        "sources",
        "references",
        "extraction",
        "relevance_projection",
        "synthesis",
        "editorial_enrichment",
        "assembly",
    )
    assert completed_stage_count(stages) == 7


def test_needs_review_surfaces_the_reason_on_the_current_stage() -> None:
    run = _run(ProductionStage.SYNTHESIS)
    run.mark_needs_review(code="unknown_source", message="[S9] inconnu")

    stages = build_stage_statuses(run, {})

    assert stages["synthesis"]["status"] == "needs_review"
    assert stages["synthesis"]["error_code"] == "unknown_source"
    assert stages["synthesis"]["error_message"] == "[S9] inconnu"
    # Earlier stages stay complete; later ones stay pending.
    assert stages["references"]["status"] == "succeeded"
    assert stages["assembly"]["status"] == "pending"


def test_failed_run_marks_the_stage_it_stopped_on() -> None:
    run = _run(ProductionStage.REFERENCES)
    run.mark_failed(code="bridge_timeout", message="timeout")

    stages = build_stage_statuses(run, {})

    assert stages["references"]["status"] == "failed"
    assert stages["sources"]["status"] == "succeeded"


def test_brief_status_omits_editorial_enrichment() -> None:
    run = _run(ProductionStage.ASSEMBLY)

    stages = build_stage_statuses(run, {}, publication_kind=PublicationKind.BRIEF)

    assert "editorial_enrichment" not in stages
    assert stages["synthesis"]["status"] == "succeeded"
    assert stages["assembly"]["status"] == "running"


@pytest.mark.asyncio
async def test_subject_production_status_uses_the_routed_publication_kind() -> None:
    run = _run(ProductionStage.ASSEMBLY)
    run.routing_decision_id = uuid4()
    subject = SimpleNamespace(title="Brief")
    uow = _StatusUow(run, subject)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                uow_factory=lambda: uow,
                job_service=None,
                job_dispatcher=None,
            ),
        )
    )

    result = await get_subject_production(run.subject_id, request)

    assert "editorial_enrichment" not in result.stages
    assert result.progress_current == 5
    assert result.progress_total == 6


class _StatusUow:
    def __init__(self, run: ProductionRun, subject: object) -> None:
        self.production_runs = SimpleNamespace(get_current_for_subject=self._get_run)
        self.subjects = SimpleNamespace(get=self._get_subject)
        self.edition_production_batch_items = SimpleNamespace(get_by_run=self._no_item)
        self.production_input_snapshots = SimpleNamespace(get_by_run=self._get_snapshot)
        self.production_artifacts = SimpleNamespace(
            list_for_run=self._no_artifacts,
            get_current=self._no_artifact,
        )
        self.source_collections = SimpleNamespace(list_for_subject=self._no_collections)
        self.production_routing_decisions = SimpleNamespace(get=self._get_route)
        self._run = run
        self._subject = subject

    async def __aenter__(self) -> _StatusUow:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def _get_run(self, subject_id: object) -> ProductionRun:
        return self._run

    async def _get_subject(self, subject_id: object) -> object:
        return self._subject

    async def _no_item(self, run_id: object) -> None:
        return None

    async def _get_snapshot(self, run_id: object) -> object:
        return SimpleNamespace(research_date=None)

    async def _no_artifacts(self, run_id: object) -> list[object]:
        return []

    async def _no_artifact(self, run_id: object, stage: str) -> None:
        return None

    async def _no_collections(self, subject_id: object) -> list[object]:
        return []

    async def _get_route(self, decision_id: object) -> object:
        return SimpleNamespace(
            status=RoutingStatus.DECIDED,
            publication_kind=PublicationKind.BRIEF,
            reason_code=None,
            policy_version="test-v1",
            confirmed_ioc_count=0,
            contextual_ioc_count=0,
            ambiguous_ioc_count=0,
            critical_source_diagnostics=(),
        )
