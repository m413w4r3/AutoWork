"""Per-stage status shown to the UI."""

from __future__ import annotations

from datetime import date
from typing import Any

from cti_app.domain.production import (
    ProductionArtifact,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_pipeline import artifact_stage_for, production_stages
from cti_app.domain.publication_kind import PublicationKind


def build_stage_statuses(
    run: ProductionRun,
    artifacts: dict[str, ProductionArtifact],
    *,
    archived_sources: int = 0,
    research_date: date | None = None,
    publication_kind: PublicationKind | None = None,
) -> dict[str, dict[str, Any]]:
    """Status of each stage, derived from the run's position and its artifacts.

    Stages before the current one are complete, the current one is running, and
    a terminal run reports its outcome on the stage it stopped at.
    """
    stages_for_pipeline = production_stages(publication_kind)
    current_stage = run.current_stage
    if current_stage not in stages_for_pipeline:
        # A route change can remove enrichment from a brief while a stale
        # control snapshot still points at that old branch. Display only the
        # active route's stages; the run itself remains fenced by its version.
        current_stage = ProductionStage.SYNTHESIS
    current_index = stages_for_pipeline.index(current_stage)
    statuses: dict[str, dict[str, Any]] = {}

    for index, stage in enumerate(stages_for_pipeline):
        artifact_stage = artifact_stage_for(stage)
        artifact = artifacts.get(artifact_stage.value) if artifact_stage else None

        if run.status is ProductionRunStatus.READY:
            status = "succeeded"
        elif index < current_index:
            status = "succeeded"
        elif index > current_index:
            status = "pending"
        elif run.status is ProductionRunStatus.NEEDS_REVIEW:
            status = "needs_review"
        elif run.status is ProductionRunStatus.FAILED:
            status = "failed"
        elif run.status is ProductionRunStatus.RUNNING:
            status = "running"
        elif run.status is ProductionRunStatus.CANCELLED:
            status = "cancelled"
        else:
            status = "pending"

        entry: dict[str, Any] = {
            "status": status,
            "version": artifact.version if artifact else None,
            "error_code": None,
            "error_message": None,
            "reused": bool(artifact and artifact.reused_from_artifact_id is not None),
            "reused_from_artifact_id": (
                str(artifact.reused_from_artifact_id)
                if artifact and artifact.reused_from_artifact_id is not None
                else None
            ),
            "reused_from_created_at": (
                artifact.metadata.get("reused_from_created_at") if artifact else None
            ),
            "research_date": research_date.isoformat() if research_date else None,
        }
        if index == current_index and run.status in {
            ProductionRunStatus.NEEDS_REVIEW,
            ProductionRunStatus.FAILED,
        }:
            entry["error_code"] = run.error_code
            entry["error_message"] = run.error_message
        if stage is ProductionStage.SOURCES:
            entry["archived_sources"] = archived_sources
        statuses[stage.value] = entry

    return statuses


def completed_stage_count(statuses: dict[str, dict[str, Any]]) -> int:
    return sum(1 for entry in statuses.values() if entry["status"] == "succeeded")
