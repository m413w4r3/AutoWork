"""Resume plan for a cancelled production run.

Cancelling a production stops the pipeline; it destroys nothing.  Archived
sources, references, extraction, synthesis and publication artifacts, the Q2
per-source checkpoints and the run's own progress snapshot all survive, because
cancellation only writes a status on the run.  Resuming is therefore a question
about the evidence that already exists: which stages are demonstrably complete,
which is the first that is not, and how many model calls the rest of the
pipeline still owes.

The plan is deliberately derived from artifacts rather than from
``current_stage``: a run cancelled mid-stage keeps pointing at the stage it was
executing, which says nothing about whether that stage produced its artifact.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    production_stages,
)

# A Q2 source entry only counts as done under these two statuses; every other
# status (pending, running, failed, skipped) may still cost a model call.
# ``production_workflow`` writes them and imports this set back.
EXTRACTION_PROGRESS_COMPLETED_STATUSES = frozenset({"cached", "succeeded"})

# The artifact that evidences each stage, when there is one.  SOURCES is
# evidenced by the archived source collections instead.
STAGE_ARTIFACT: dict[SubjectProductionStage, ProductionArtifactStage | None] = {
    SubjectProductionStage.SOURCES: None,
    SubjectProductionStage.REFERENCES: ProductionArtifactStage.REFERENCES,
    SubjectProductionStage.EXTRACTION: ProductionArtifactStage.EXTRACTION,
    SubjectProductionStage.SYNTHESIS: ProductionArtifactStage.SYNTHESIS,
    SubjectProductionStage.ASSEMBLY: ProductionArtifactStage.PUBLICATION,
}

# Model calls a stage still owes when it has to run.  Extraction is variable:
# it owes one call per source that has no usable answer yet.
_STAGE_MODEL_CALLS: dict[SubjectProductionStage, int] = {
    SubjectProductionStage.SOURCES: 0,
    SubjectProductionStage.REFERENCES: 1,
    SubjectProductionStage.SYNTHESIS: 1,
    SubjectProductionStage.ASSEMBLY: 0,
}


@dataclass(frozen=True, slots=True)
class ProductionResumePlan:
    """What a resume will reuse, what it will run, and what it will cost."""

    previous_status: SubjectProductionStatus
    resume_from_stage: SubjectProductionStage
    completed_stages: tuple[SubjectProductionStage, ...]
    reused_artifacts: tuple[str, ...]
    model_calls_expected: int

    def as_log_fields(self) -> dict[str, Any]:
        """The ``production.resume.plan`` payload."""
        return {
            "previous_status": self.previous_status.value,
            "resume_from_stage": self.resume_from_stage.value,
            "reused_artifacts": list(self.reused_artifacts),
            "model_calls_expected": self.model_calls_expected,
        }


def pending_extraction_sources(progress: Mapping[str, Any] | None) -> int | None:
    """Sources of the last extraction pass that still owe a model answer.

    ``None`` when the run never recorded a per-source snapshot: the caller then
    has no evidence of partial extraction and must assume the whole stage runs.
    """
    if not isinstance(progress, Mapping):
        return None
    sources = progress.get("sources")
    if not isinstance(sources, list) or not sources:
        return None
    return sum(
        1
        for entry in sources
        if not isinstance(entry, Mapping)
        or entry.get("status") not in EXTRACTION_PROGRESS_COMPLETED_STATUSES
    )


def _artifact_completes_stage(artifact: ProductionArtifact | None) -> bool:
    """A stage is done when its current artifact still stands.

    A stale artifact is the trace of an invalidated generation, never evidence
    that its stage is done — the same rule the retry prerequisites apply.  The
    status is read defensively so a repository handing back a lighter row than
    the full entity still counts as evidence.
    """
    if artifact is None:
        return False
    return getattr(artifact, "status", ProductionArtifactStatus.VERIFIED) is not (
        ProductionArtifactStatus.STALE
    )


def resolve_retry_stage(
    live_artifact_stages: Collection[str],
    *,
    current_stage: SubjectProductionStage,
) -> SubjectProductionStage:
    """The stage a retry must actually start from.

    Same doctrine as :func:`plan_production_resume`, applied to the Review
    retry: ``current_stage`` is where the run last executed, which says nothing
    about what is still missing.  An upstream repair stales SYNTHESIS and
    PUBLICATION while the run keeps pointing at ASSEMBLY; offering ASSEMBLY
    there sends the analyst straight into ``retry_prerequisite_missing``,
    because ASSEMBLY requires the current SYNTHESIS artifact the repair just
    invalidated.

    So the retry aims at the first pipeline stage whose artifact is not
    current.  SOURCES is skipped: it is evidenced by archived collections
    rather than by an artifact, and the retry's own prerequisite check owns
    that question.  When every stage has its artifact the run is complete, and
    replaying its last stage is the honest answer.

    ``live_artifact_stages`` holds :class:`ProductionArtifactStage` values for
    the artifacts that are not STALE.
    """
    live = set(live_artifact_stages)
    for stage in production_stages():
        artifact_stage = STAGE_ARTIFACT[stage]
        if artifact_stage is None:
            continue
        if artifact_stage.value not in live:
            return stage
    return current_stage


def plan_production_resume(
    run: SubjectProductionRun,
    *,
    artifacts: Mapping[str, ProductionArtifact | None],
    archived_source_count: int,
) -> ProductionResumePlan:
    """Derive the resume plan from the artifacts that really exist.

    ``artifacts`` is keyed by :class:`ProductionArtifactStage` value.  The
    returned ``model_calls_expected`` is an upper bound: a stage that has to run
    may still find a reusable checkpoint for a source, which costs nothing.
    """
    stages = production_stages()
    completed: list[SubjectProductionStage] = []
    reused: list[str] = []
    resume_from: SubjectProductionStage | None = None

    for stage in stages:
        artifact_stage = STAGE_ARTIFACT[stage]
        if artifact_stage is None:
            complete = archived_source_count > 0
        else:
            complete = _artifact_completes_stage(artifacts.get(artifact_stage.value))
        if not complete:
            resume_from = stage
            break
        completed.append(stage)
        if artifact_stage is not None:
            reused.append(artifact_stage.value)

    if resume_from is None:
        # Everything is already produced: the run was cancelled after its last
        # stage persisted its artifact.  Replaying assembly is deterministic and
        # free, and it is what brings the run back to READY.
        resume_from = SubjectProductionStage.ASSEMBLY
        completed = [stage for stage in stages if stage is not SubjectProductionStage.ASSEMBLY]
        reused = [
            artifact_stage.value
            for stage in completed
            if (artifact_stage := STAGE_ARTIFACT[stage]) is not None
        ]

    resume_index = stages.index(resume_from)
    model_calls = 0
    for stage in stages[resume_index:]:
        if stage is SubjectProductionStage.EXTRACTION:
            pending = pending_extraction_sources(run.extraction_progress)
            model_calls += pending if pending is not None else archived_source_count
        else:
            model_calls += _STAGE_MODEL_CALLS[stage]

    return ProductionResumePlan(
        previous_status=run.status,
        resume_from_stage=resume_from,
        completed_stages=tuple(completed),
        reused_artifacts=tuple(reused),
        model_calls_expected=model_calls,
    )


__all__ = [
    "EXTRACTION_PROGRESS_COMPLETED_STATUSES",
    "STAGE_ARTIFACT",
    "ProductionResumePlan",
    "pending_extraction_sources",
    "plan_production_resume",
]
