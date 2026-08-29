"""Per-stage status shown to the UI."""

from __future__ import annotations

from typing import Any

from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    production_stages,
)

# Artifact that evidences each stage, when there is one.
_STAGE_ARTIFACT: dict[SubjectProductionStage, ProductionArtifactStage | None] = {
    SubjectProductionStage.SOURCES: None,
    SubjectProductionStage.REFERENCES: ProductionArtifactStage.REFERENCES,
    SubjectProductionStage.EXTRACTION: ProductionArtifactStage.EXTRACTION,
    SubjectProductionStage.SYNTHESIS: ProductionArtifactStage.SYNTHESIS,
    SubjectProductionStage.ASSEMBLY: ProductionArtifactStage.PUBLICATION,
}


def build_stage_statuses(
    run: SubjectProductionRun,
    artifacts: dict[str, ProductionArtifact],
    *,
    archived_sources: int = 0,
) -> dict[str, dict[str, Any]]:
    """Status of each stage, derived from the run's position and its artifacts.

    Stages before the current one are complete, the current one is running, and
    a terminal run reports its outcome on the stage it stopped at.
    """
    stages_for_pipeline = production_stages()
    current_stage = run.current_stage
    current_index = stages_for_pipeline.index(current_stage)
    statuses: dict[str, dict[str, Any]] = {}

    for index, stage in enumerate(stages_for_pipeline):
        artifact_stage = _STAGE_ARTIFACT[stage]
        artifact = artifacts.get(artifact_stage.value) if artifact_stage else None

        if run.status is SubjectProductionStatus.READY:
            status = "succeeded"
        elif index < current_index:
            status = "succeeded"
        elif index > current_index:
            status = "pending"
        elif run.status is SubjectProductionStatus.NEEDS_REVIEW:
            status = "needs_review"
        elif run.status is SubjectProductionStatus.FAILED:
            status = "failed"
        elif run.status is SubjectProductionStatus.RUNNING:
            status = "running"
        elif run.status is SubjectProductionStatus.CANCELLED:
            status = "cancelled"
        else:
            status = "pending"

        entry: dict[str, Any] = {
            "status": status,
            "version": artifact.version if artifact else None,
            "error_code": None,
            "error_message": None,
        }
        if index == current_index and run.status in {
            SubjectProductionStatus.NEEDS_REVIEW,
            SubjectProductionStatus.FAILED,
        }:
            entry["error_code"] = run.error_code
            entry["error_message"] = run.error_message
        if stage is SubjectProductionStage.SOURCES:
            entry["archived_sources"] = archived_sources
        statuses[stage.value] = entry

    return statuses


def completed_stage_count(statuses: dict[str, dict[str, Any]]) -> int:
    return sum(1 for entry in statuses.values() if entry["status"] == "succeeded")
