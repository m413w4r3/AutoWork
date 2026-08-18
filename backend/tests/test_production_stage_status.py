"""Stage status exposed to the UI.

SOURCES produces no artifact and ASSEMBLY produces the `brief` artifact, so a
naive artifact-driven mapping reports SOURCES as pending forever.
"""

from __future__ import annotations

from uuid import uuid4

from cti_app.application.production_stage_status import (
    build_stage_statuses,
    completed_stage_count,
)
from cti_app.domain.production import (
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
)


def _run(stage: SubjectProductionStage) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        profile=ProductionProfile.BRIEF_AUTO,
    )
    run.start_running()
    run.current_stage = stage
    return run


def test_sources_is_complete_once_the_run_moved_past_it() -> None:
    run = _run(SubjectProductionStage.REFERENCES)

    stages = build_stage_statuses(run, {}, archived_sources=5)

    assert stages["sources"]["status"] == "succeeded"
    assert stages["sources"]["archived_sources"] == 5
    assert stages["references"]["status"] == "running"
    assert stages["extraction"]["status"] == "pending"


def test_ready_run_reports_every_stage_complete() -> None:
    run = _run(SubjectProductionStage.ASSEMBLY)
    run.mark_ready()

    stages = build_stage_statuses(run, {})

    assert completed_stage_count(stages) == 5


def test_needs_review_surfaces_the_reason_on_the_current_stage() -> None:
    run = _run(SubjectProductionStage.SYNTHESIS)
    run.mark_needs_review(code="unknown_source", message="[S9] inconnu")

    stages = build_stage_statuses(run, {})

    assert stages["synthesis"]["status"] == "needs_review"
    assert stages["synthesis"]["error_code"] == "unknown_source"
    assert stages["synthesis"]["error_message"] == "[S9] inconnu"
    # Earlier stages stay complete; later ones stay pending.
    assert stages["references"]["status"] == "succeeded"
    assert stages["assembly"]["status"] == "pending"


def test_failed_run_marks_the_stage_it_stopped_on() -> None:
    run = _run(SubjectProductionStage.REFERENCES)
    run.mark_failed(code="bridge_timeout", message="timeout")

    stages = build_stage_statuses(run, {})

    assert stages["references"]["status"] == "failed"
    assert stages["sources"]["status"] == "succeeded"
