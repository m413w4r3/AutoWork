"""Idempotent post-commit processing for accepted editorial proposals."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.editorial_revision_effects import (
    EditorialRevisionEffects,
)
from cti_app.application.editorial_workspace.registry import WorkspaceSnapshotRecord
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.domain.editorial_proposal_impact import (
    EditorialProposalImpact,
    EditorialProposalImpactState,
)
from cti_app.domain.production import ProductionArtifactStage

EDITORIAL_IMPACT_RUNNING_RECOVERY_AFTER = timedelta(minutes=5)
EDITORIAL_IMPACT_PROCESSING_TIMEOUT_SECONDS = 240
EDITORIAL_IMPACT_DISPATCH_RETRY_AFTER = timedelta(minutes=1)
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")


class EditorialImpactWorkspace(Protocol):
    async def snapshot_record_in_uow(
        self, uow: UnitOfWork, subject_id: UUID
    ) -> WorkspaceSnapshotRecord: ...


class EditorialSynthesisRebaser(Protocol):
    async def rebase_adopted_synthesis(
        self,
        uow: UnitOfWork,
        *,
        run: Any,
        run_guard: Any,
        base_snapshot: WorkspaceSnapshotRecord,
        accepted_snapshot: WorkspaceSnapshotRecord,
        actor_id: str,
    ) -> None: ...


class EditorialRevisionImpactError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EditorialProposalImpactService:
    """Process impact outbox rows while serializing against run writers."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        workspace_service: EditorialImpactWorkspace,
        revision_effects: EditorialRevisionEffects,
        synthesis_rebaser: EditorialSynthesisRebaser,
        actor_id: str = "editorial-impact-worker",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace_service = workspace_service
        self._revision_effects = revision_effects
        self._synthesis_rebaser = synthesis_rebaser
        self._actor_id = actor_id
        self._clock = clock or (lambda: datetime.now(UTC))

    async def process(self, impact_id: UUID) -> EditorialProposalImpact | None:
        impact: EditorialProposalImpact | None = None
        try:
            # Include claim time and commit in the bound for the fixed claimed_at lease.
            async with asyncio.timeout(EDITORIAL_IMPACT_PROCESSING_TIMEOUT_SECONDS):
                now = self._clock()
                async with self._uow_factory() as uow:
                    impact = await uow.editorial_proposal_impacts.claim(
                        impact_id,
                        now=now,
                        stale_running_before=now - EDITORIAL_IMPACT_RUNNING_RECOVERY_AFTER,
                    )
                    if impact is None:
                        return await uow.editorial_proposal_impacts.get_by_id(impact_id)
                    await uow.commit()
                return await self._apply_claimed(impact)
        except TimeoutError:
            if impact is not None:
                await self._record_failure(
                    impact,
                    EditorialRevisionImpactError("editorial_impact_processing_timeout"),
                )
            async with self._uow_factory() as uow:
                return await uow.editorial_proposal_impacts.get_by_id(impact_id)
        except Exception as error:
            if impact is None:
                raise
            await self._record_failure(impact, error)
            async with self._uow_factory() as uow:
                return await uow.editorial_proposal_impacts.get_by_id(impact_id)

    async def dispatchable_ids(self, *, limit: int = 100) -> Sequence[UUID]:
        now = self._clock()
        async with self._uow_factory() as uow:
            repository = uow.editorial_proposal_impacts
            candidates = await repository.list_dispatchable(
                now=now,
                stale_running_before=now - EDITORIAL_IMPACT_RUNNING_RECOVERY_AFTER,
                dispatch_before=now - EDITORIAL_IMPACT_DISPATCH_RETRY_AFTER,
                limit=limit,
            )
            reserved: list[UUID] = []
            for impact_id in candidates:
                if await repository.reserve_dispatch(
                    impact_id,
                    now=now,
                    stale_running_before=now - EDITORIAL_IMPACT_RUNNING_RECOVERY_AFTER,
                    dispatch_before=now - EDITORIAL_IMPACT_DISPATCH_RETRY_AFTER,
                ):
                    reserved.append(impact_id)
            if reserved:
                await uow.commit()
            return tuple(reserved)

    async def _apply_claimed(
        self, claimed: EditorialProposalImpact
    ) -> EditorialProposalImpact | None:
        async with self._uow_factory() as uow:
            run_guard = await self._revision_effects.acquire_run_guard(
                uow, claimed.production_run_id
            )
            run = await self._revision_effects.lock_run_row(uow, run_guard)
            impact = await uow.editorial_proposal_impacts.get_by_id(claimed.impact_id)
            if impact is None or impact.state is not EditorialProposalImpactState.RUNNING:
                return impact
            if run.subject_id != impact.subject_id:
                raise EditorialRevisionImpactError("editorial_impact_run_mismatch")

            current = await self._workspace_service.snapshot_record_in_uow(uow, impact.subject_id)
            if current.workspace_pin_hash != impact.accepted_pin_hash:
                updated = await self._set_state(
                    uow,
                    impact,
                    EditorialProposalImpactState.SUPERSEDED,
                )
                await uow.commit()
                return updated

            accepted_snapshot = await uow.editorial_workspace_snapshots.get_by_id(
                impact.accepted_snapshot_id
            )
            if (
                accepted_snapshot is None
                or accepted_snapshot.production_run_id != impact.production_run_id
                or accepted_snapshot.subject_id != impact.subject_id
                or accepted_snapshot.workspace_pin_hash != impact.accepted_pin_hash
            ):
                raise EditorialRevisionImpactError("editorial_impact_snapshot_missing")
            if accepted_snapshot.parent_snapshot_id is None:
                raise EditorialRevisionImpactError("editorial_impact_base_snapshot_missing")
            base_snapshot = await uow.editorial_workspace_snapshots.get_by_id(
                accepted_snapshot.parent_snapshot_id
            )
            if (
                base_snapshot is None
                or base_snapshot.production_run_id != impact.production_run_id
                or base_snapshot.subject_id != impact.subject_id
                or base_snapshot.workspace_pin_hash != impact.base_pin_hash
            ):
                raise EditorialRevisionImpactError("editorial_impact_base_snapshot_mismatch")

            accepted_artifact = await uow.production_artifacts.get(impact.accepted_artifact_id)
            if (
                accepted_artifact is None
                or accepted_artifact.production_run_id != impact.production_run_id
                or accepted_artifact.subject_id != impact.subject_id
            ):
                raise EditorialRevisionImpactError("editorial_impact_artifact_missing")

            if accepted_artifact.stage is ProductionArtifactStage.SYNTHESIS:
                await self._synthesis_rebaser.rebase_adopted_synthesis(
                    uow,
                    run=run,
                    run_guard=run_guard,
                    base_snapshot=base_snapshot,
                    accepted_snapshot=accepted_snapshot,
                    actor_id=self._actor_id,
                )
            elif accepted_artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT:
                raise EditorialRevisionImpactError("editorial_impact_artifact_stage_invalid")

            await uow.production_artifacts.mark_stages_stale(
                impact.production_run_id,
                {ProductionArtifactStage.PUBLICATION.value},
            )
            updated = await self._set_state(uow, impact, EditorialProposalImpactState.DONE)
            await uow.commit()
            return updated

    async def _record_failure(self, impact: EditorialProposalImpact, error: Exception) -> None:
        # Persist only this service's stable codes; lower-layer messages are not the contract.
        code = error.code if isinstance(error, EditorialRevisionImpactError) else None
        error_code = (
            code
            if isinstance(code, str) and _ERROR_CODE.fullmatch(code)
            else ("editorial_impact_failed")
        )
        now = self._clock()
        retry_seconds = min(30 * (2 ** min(max(impact.attempt_count - 1, 0), 7)), 3600)
        async with self._uow_factory() as uow:
            await uow.editorial_proposal_impacts.set_state(
                impact.impact_id,
                state=EditorialProposalImpactState.FAILED,
                expected_claimed_at=impact.claimed_at or now,
                now=now,
                available_at=now + timedelta(seconds=retry_seconds),
                last_error_code=error_code,
            )
            await uow.commit()

    async def _set_state(
        self,
        uow: UnitOfWork,
        impact: EditorialProposalImpact,
        state: EditorialProposalImpactState,
    ) -> EditorialProposalImpact:
        now = self._clock()
        updated = await uow.editorial_proposal_impacts.set_state(
            impact.impact_id,
            state=state,
            expected_claimed_at=impact.claimed_at or now,
            now=now,
            available_at=now,
            last_error_code=None,
        )
        if updated is None:
            raise EditorialRevisionImpactError("editorial_impact_missing")
        return updated
