from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.editorial_revision_impact import (
    EditorialProposalImpactService,
    EditorialRevisionImpactError,
)
from cti_app.domain.editorial_proposal_impact import (
    EditorialProposalImpact,
    EditorialProposalImpactState,
    project_editorial_impact_status,
)
from cti_app.domain.editorial_workspace import EditorialRegistryConflictError
from cti_app.domain.production import ProductionArtifactStage


class _ImpactRepository:
    def __init__(self, impact: EditorialProposalImpact) -> None:
        self.impact = impact

    async def claim(self, impact_id, *, now, stale_running_before):
        if impact_id != self.impact.impact_id:
            return None
        if self.impact.state not in {
            EditorialProposalImpactState.PENDING,
            EditorialProposalImpactState.FAILED,
        }:
            return None
        self.impact = replace(
            self.impact,
            state=EditorialProposalImpactState.RUNNING,
            attempt_count=self.impact.attempt_count + 1,
            claimed_at=now,
            completed_at=None,
            last_error_code=None,
        )
        return self.impact

    async def get_by_id(self, impact_id):
        return self.impact if impact_id == self.impact.impact_id else None

    async def set_state(
        self,
        impact_id,
        *,
        state,
        expected_claimed_at,
        now,
        last_error_code,
        available_at,
    ):
        assert impact_id == self.impact.impact_id
        assert self.impact.claimed_at == expected_claimed_at
        self.impact = replace(
            self.impact,
            state=state,
            completed_at=(
                now
                if state
                in {
                    EditorialProposalImpactState.DONE,
                    EditorialProposalImpactState.FAILED,
                    EditorialProposalImpactState.SUPERSEDED,
                }
                else None
            ),
            last_error_code=last_error_code,
            available_at=available_at,
            updated_at=now,
        )
        return self.impact


class _Uow:
    def __init__(self, repo: _ImpactRepository, accepted_artifact) -> None:
        self.editorial_proposal_impacts = repo
        self.production_runs = SimpleNamespace(get_for_update=self._get_run)
        self.production_artifacts = SimpleNamespace(
            get=self._get_artifact,
            mark_stages_stale=self._mark_stale,
        )
        self.editorial_workspace_snapshots = SimpleNamespace(get_by_id=self._get_snapshot)
        self.accepted_artifact = accepted_artifact
        self.stale_stages: list[tuple[object, set[str]]] = []
        self.run_id = repo.impact.production_run_id
        self.snapshot = SimpleNamespace(
            snapshot_id=repo.impact.accepted_snapshot_id,
            subject_id=repo.impact.subject_id,
            production_run_id=repo.impact.production_run_id,
            workspace_pin_hash=repo.impact.accepted_pin_hash,
            parent_snapshot_id=uuid4(),
        )

    async def _get_run(self, run_id):
        assert run_id == self.run_id
        return SimpleNamespace(
            id=run_id,
            subject_id=self.editorial_proposal_impacts.impact.subject_id,
        )

    async def _get_artifact(self, artifact_id):
        return self.accepted_artifact if artifact_id == self.accepted_artifact.id else None

    async def _get_snapshot(self, snapshot_id):
        if snapshot_id == self.snapshot.snapshot_id:
            return self.snapshot
        return SimpleNamespace(
            snapshot_id=snapshot_id,
            subject_id=self.snapshot.subject_id,
            production_run_id=self.snapshot.production_run_id,
            workspace_pin_hash="a" * 64,
            parent_snapshot_id=None,
        )

    async def _mark_stale(self, run_id, stages):
        self.stale_stages.append((run_id, set(stages)))

    async def commit(self):
        return None


class _UowFactory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.uow

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class _RevisionEffects:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def acquire_run_guard(self, _uow, production_run_id):
        self.events.append("acquire_run_guard")
        return SimpleNamespace(production_run_id=production_run_id)

    async def lock_run_row(self, _uow, _guard):
        self.events.append("lock_run_row")
        return await _uow.production_runs.get_for_update(_guard.production_run_id)


class _Workspace:
    def __init__(self, uow: _Uow, events: list[str], *, current_pin_hash: str) -> None:
        self.uow = uow
        self.events = events
        self.current_pin_hash = current_pin_hash

    async def snapshot_record_in_uow(self, _uow, _subject_id):
        self.events.append("reread_current_pins")
        return SimpleNamespace(
            **{
                **vars(self.uow.snapshot),
                "workspace_pin_hash": self.current_pin_hash,
            }
        )


class _Rebaser:
    def __init__(self, events: list[str], error: Exception | None = None) -> None:
        self.events = events
        self.error = error

    async def rebase_adopted_synthesis(self, _uow, **_kwargs):
        self.events.append("rebase")
        if self.error is not None:
            raise self.error


def _impact(*, accepted_pin_hash: str = "b" * 64) -> EditorialProposalImpact:
    return EditorialProposalImpact(
        impact_id=uuid4(),
        subject_id=uuid4(),
        production_run_id=uuid4(),
        proposal_id=uuid4(),
        accepted_artifact_id=uuid4(),
        accepted_snapshot_id=uuid4(),
        base_pin_hash="a" * 64,
        accepted_pin_hash=accepted_pin_hash,
        sequence=1,
    )


@pytest.mark.parametrize(
    ("render_status", "expected_preview_status", "expected_pdf_status"),
    [
        ("READY", "READY", "CURRENT"),
        ("IN_PROGRESS", "GENERATING", "STALE"),
        ("GENERATING", "GENERATING", "STALE"),
        ("FAILED", "FAILED", "STALE"),
        ("STALE", "STALE", "STALE"),
        (None, "STALE", "STALE"),
    ],
)
def test_impact_projection_preserves_real_pdf_render_statuses(
    render_status: str | None, expected_preview_status: str, expected_pdf_status: str
) -> None:
    assert project_editorial_impact_status(
        None,
        render_status=render_status,
        publication_stale=False,
    ) == ("none", expected_preview_status, expected_pdf_status)


@pytest.mark.parametrize(
    "state",
    [
        EditorialProposalImpactState.PENDING,
        EditorialProposalImpactState.RUNNING,
        EditorialProposalImpactState.FAILED,
    ],
)
def test_active_or_failed_impact_without_outdated_publication_preserves_ready_pdf(
    state: EditorialProposalImpactState,
) -> None:
    impact = replace(
        _impact(),
        state=state,
        completed_at=(datetime.now(UTC) if state is EditorialProposalImpactState.FAILED else None),
    )

    assert project_editorial_impact_status(
        impact,
        render_status="READY",
        publication_stale=False,
    ) == (state.value, "READY", "CURRENT")


@pytest.mark.parametrize(
    "state",
    [
        EditorialProposalImpactState.PENDING,
        EditorialProposalImpactState.RUNNING,
        EditorialProposalImpactState.FAILED,
    ],
)
def test_active_or_failed_impact_marks_ready_pdf_stale_when_publication_is_outdated(
    state: EditorialProposalImpactState,
) -> None:
    impact = replace(
        _impact(),
        state=state,
        completed_at=(datetime.now(UTC) if state is EditorialProposalImpactState.FAILED else None),
    )

    assert project_editorial_impact_status(
        impact,
        render_status="READY",
        publication_stale=True,
    ) == (state.value, "STALE", "STALE")


def test_pending_impact_preserves_real_render_states_when_publication_is_outdated() -> None:
    pending = _impact()
    assert project_editorial_impact_status(
        pending,
        render_status="IN_PROGRESS",
        publication_stale=True,
    ) == ("pending", "STALE", "STALE")
    assert project_editorial_impact_status(
        pending,
        render_status="FAILED",
        publication_stale=True,
    ) == ("pending", "STALE", "STALE")


def test_done_impact_only_marks_pdf_stale_when_publication_is_stale() -> None:
    pending = _impact()
    done = replace(
        pending,
        state=EditorialProposalImpactState.DONE,
        completed_at=datetime.now(UTC),
    )
    assert project_editorial_impact_status(
        done,
        render_status="READY",
        publication_stale=False,
    ) == ("up_to_date", "READY", "CURRENT")
    assert project_editorial_impact_status(
        done,
        render_status="READY",
        publication_stale=True,
    ) == ("up_to_date", "STALE", "STALE")


def test_processing_timeout_is_strictly_shorter_than_claim_lease() -> None:
    from cti_app.application import editorial_revision_impact as impact_module

    timeout = getattr(impact_module, "EDITORIAL_IMPACT_PROCESSING_TIMEOUT_SECONDS", 0)
    lease = getattr(impact_module, "EDITORIAL_IMPACT_RUNNING_RECOVERY_AFTER", 0)
    lease_seconds = lease.total_seconds() if hasattr(lease, "total_seconds") else 0
    assert timeout < lease_seconds


@pytest.mark.asyncio
async def test_replaying_done_impact_is_a_noop() -> None:
    service, impact, repository, _uow, events = _service(current_pin_hash="b" * 64)
    repository.impact = replace(
        impact,
        state=EditorialProposalImpactState.DONE,
        completed_at=datetime.now(UTC),
    )

    replayed = await service.process(impact.impact_id)

    assert replayed is repository.impact
    assert events == []


def _service(*, current_pin_hash: str, error: Exception | None = None):
    impact = _impact()
    accepted_artifact = SimpleNamespace(
        id=impact.accepted_artifact_id,
        production_run_id=impact.production_run_id,
        subject_id=impact.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
    )
    repository = _ImpactRepository(impact)
    uow = _Uow(repository, accepted_artifact)
    events: list[str] = []
    service = EditorialProposalImpactService(
        uow_factory=_UowFactory(uow),
        workspace_service=_Workspace(uow, events, current_pin_hash=current_pin_hash),
        revision_effects=_RevisionEffects(events),
        synthesis_rebaser=_Rebaser(events, error),
        clock=lambda: datetime.now(UTC),
    )
    return service, impact, repository, uow, events


@pytest.mark.asyncio
async def test_out_of_order_impact_a_b_marks_old_pins_superseded_noop() -> None:
    service, impact, repository, uow, events = _service(current_pin_hash="c" * 64)

    await service.process(impact.impact_id)

    assert repository.impact.state is EditorialProposalImpactState.SUPERSEDED
    assert events == ["acquire_run_guard", "lock_run_row", "reread_current_pins"]
    assert not uow.stale_stages


@pytest.mark.asyncio
async def test_worker_locks_run_and_rereads_current_pins() -> None:
    service, impact, repository, uow, events = _service(current_pin_hash="b" * 64)

    await service.process(impact.impact_id)

    assert events[:3] == ["acquire_run_guard", "lock_run_row", "reread_current_pins"]
    assert "rebase" in events
    assert repository.impact.state is EditorialProposalImpactState.DONE
    assert uow.stale_stages


@pytest.mark.asyncio
async def test_impact_failure_never_rolls_back_accepted_artifact() -> None:
    service, impact, repository, uow, _events = _service(
        current_pin_hash="b" * 64,
        error=RuntimeError("synthetic failure"),
    )
    accepted_artifact_id = uow.accepted_artifact.id

    await service.process(impact.impact_id)

    assert uow.accepted_artifact.id == accepted_artifact_id
    assert repository.impact.state is EditorialProposalImpactState.FAILED
    assert repository.impact.last_error_code == "editorial_impact_failed"


@pytest.mark.asyncio
async def test_impact_failure_does_not_expose_nested_registry_error_code() -> None:
    service, _impact_row, repository, _uow, _events = _service(
        current_pin_hash="b" * 64,
        error=EditorialRegistryConflictError("editorial_registry_conflict"),
    )

    await service.process(repository.impact.impact_id)

    assert repository.impact.state is EditorialProposalImpactState.FAILED
    assert repository.impact.last_error_code == "editorial_impact_failed"


@pytest.mark.asyncio
async def test_impact_failure_keeps_stable_consumer_error_code() -> None:
    service, _impact_row, repository, _uow, _events = _service(
        current_pin_hash="b" * 64,
        error=EditorialRevisionImpactError("editorial_impact_inputs_missing"),
    )

    await service.process(repository.impact.impact_id)

    assert repository.impact.state is EditorialProposalImpactState.FAILED
    assert repository.impact.last_error_code == "editorial_impact_inputs_missing"


@pytest.mark.asyncio
async def test_exceeding_processing_timeout_abandons_work_as_replayable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application import editorial_revision_impact as impact_module

    service, _impact_row, repository, _uow, _events = _service(current_pin_hash="b" * 64)

    async def wait_forever(_claimed: EditorialProposalImpact) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        impact_module,
        "EDITORIAL_IMPACT_PROCESSING_TIMEOUT_SECONDS",
        0.001,
        raising=False,
    )
    monkeypatch.setattr(service, "_apply_claimed", wait_forever)

    result = await asyncio.wait_for(
        service.process(repository.impact.impact_id),
        timeout=0.01,
    )

    assert result is repository.impact
    assert result.state is EditorialProposalImpactState.FAILED
    assert result.last_error_code == "editorial_impact_processing_timeout"
