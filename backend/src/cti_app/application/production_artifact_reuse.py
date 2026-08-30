"""Bounded application service for canonical production artifact reuse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.persistence import ProductionUnitOfWork, ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)

_COSTLY_STAGES = (
    ProductionArtifactStage.REFERENCES,
    ProductionArtifactStage.EXTRACTION,
    ProductionArtifactStage.SYNTHESIS,
)


@dataclass(frozen=True, slots=True)
class ProductionArtifactReuseResult:
    artifact: ProductionArtifact
    reused: bool


class ProductionArtifactReuseService:
    """Find an exact verified artifact and clone its identity into a run."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None,
        diagnostics: DiagnosticsLog | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._diagnostics = diagnostics or DiagnosticsLog(None)

    async def find_or_reuse(
        self,
        *,
        run: SubjectProductionRun,
        stage: ProductionArtifactStage,
        input_hash: str,
        allow_cross_run: bool = True,
    ) -> ProductionArtifactReuseResult | None:
        """Return a same-run cache hit or a new artifact cloned from another run."""
        if stage not in _COSTLY_STAGES:
            return None

        async with self._uow_factory() as uow:
            target_run = run
            runs = getattr(uow, "subject_production_runs", None)
            get_for_update = getattr(runs, "get_for_update", None)
            if get_for_update is not None:
                persisted_run = await get_for_update(run.id)
                if persisted_run is None:
                    return None
                target_run = persisted_run

            current = await uow.production_artifacts.get_current(target_run.id, stage.value)
            if current is not None and current.input_hash == input_hash:
                return ProductionArtifactReuseResult(current, reused=False)

            if (
                not allow_cross_run
                or not cross_run_reuse_allowed(target_run, stage)
                or self._artifact_store is None
            ):
                return None

            find_reusable = getattr(uow.production_artifacts, "find_reusable", None)
            if find_reusable is None:
                return None
            not_before = await self._invalidation_cutoff(uow, target_run, stage)
            candidate = await find_reusable(
                edition_id=target_run.edition_id,
                subject_id=target_run.subject_id,
                stage=stage.value,
                input_hash=input_hash,
                not_before=not_before,
            )
            if candidate is None:
                return None

            try:
                await self._verify_required_blob(candidate)
            except Exception as exc:
                self._diagnostics.record(
                    event="production.reuse_candidate_invalid",
                    run_id=target_run.id,
                    subject_id=target_run.subject_id,
                    stage=stage.value,
                    candidate_artifact_id=str(candidate.id),
                    source_run_id=str(candidate.production_run_id),
                    error_code="production.reuse_candidate_invalid",
                    error=str(exc),
                )
                return None

            prior = await uow.production_artifacts.list_for_run(target_run.id)
            version = (
                max(
                    (item.version for item in prior if item.stage == stage),
                    default=0,
                )
                + 1
            )
            metadata = dict(candidate.metadata)
            metadata.update(
                {
                    "reused": True,
                    "reused_from_artifact_id": str(candidate.id),
                    "reused_from_created_at": candidate.created_at.isoformat(),
                }
            )
            artifact = ProductionArtifact(
                production_run_id=target_run.id,
                subject_id=target_run.subject_id,
                stage=stage,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=candidate.raw_blob_id,
                canonical_blob_id=candidate.canonical_blob_id,
                rendered_blob_id=candidate.rendered_blob_id,
                model_run_id=candidate.model_run_id,
                conversation_turn_id=candidate.conversation_turn_id,
                reused_from_artifact_id=candidate.id,
                metadata=metadata,
            )
            await uow.production_artifacts.append(artifact)
            await uow.commit()
            return ProductionArtifactReuseResult(artifact, reused=True)

    async def _verify_required_blob(self, artifact: ProductionArtifact) -> None:
        if self._artifact_store is None:
            raise ValueError("artifact store unavailable")
        blob_id = (
            artifact.canonical_blob_id
            if artifact.stage
            in {
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
            }
            else artifact.rendered_blob_id
        )
        if blob_id is None:
            raise ValueError("required artifact blob is missing")
        await self._artifact_store.read_bytes(blob_id)

    async def _invalidation_cutoff(
        self,
        uow: ProductionUnitOfWork,
        run: SubjectProductionRun,
        stage: ProductionArtifactStage,
    ) -> datetime | None:
        repository = getattr(uow, "production_reuse_invalidations", None)
        if repository is None:
            return None
        invalidations = await repository.list_for_subject(run.edition_id, run.subject_id)
        stage_index = _COSTLY_STAGES.index(stage)
        applicable = [
            item.occurred_at
            for item in invalidations
            if item.from_stage in _COSTLY_STAGES
            and _COSTLY_STAGES.index(item.from_stage) <= stage_index
        ]
        return max(applicable) if applicable else None


def cross_run_reuse_allowed(run: SubjectProductionRun, stage: ProductionArtifactStage) -> bool:
    """Apply a persisted FORCE marker while keeping same-run idempotency intact."""
    forced_from = run.force_recompute_from_stage
    if forced_from is None:
        return True
    return _COSTLY_STAGES.index(stage) < _COSTLY_STAGES.index(
        ProductionArtifactStage(forced_from.value)
    )


__all__ = [
    "ProductionArtifactReuseResult",
    "ProductionArtifactReuseService",
    "cross_run_reuse_allowed",
]
