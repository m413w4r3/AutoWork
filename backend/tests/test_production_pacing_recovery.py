from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_jobs import ProductionStageChain
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.subject_production import EditionProductionService
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionBatchPhase,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)


class _Runs:
    def __init__(self, runs: list[SubjectProductionRun]) -> None:
        self.items = {run.id: run for run in runs}

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run


class _Batches:
    def __init__(self, batch: EditionProductionBatch) -> None:
        self.item = batch

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None:
        return self.item if batch_id == self.item.id else None

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None:
        return await self.get(batch_id)

    async def save(self, batch: EditionProductionBatch) -> None:
        self.item = batch


class _BatchItems:
    def __init__(self, items: list[EditionProductionBatchItem]) -> None:
        self.items = items

    async def list_for_batch(self, batch_id: UUID) -> list[EditionProductionBatchItem]:
        return [item for item in self.items if item.batch_id == batch_id]

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        return next((item for item in self.items if item.production_run_id == run_id), None)

    async def save(self, item: EditionProductionBatchItem) -> None:
        return None


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None

    async def update(self, edition: Edition, expected_version: int) -> bool:
        assert expected_version == edition.version - 1
        self.edition = edition
        return True


class _Audit:
    def __init__(self) -> None:
        self.events: list[EditionAuditEvent] = []

    async def append(self, event: EditionAuditEvent) -> None:
        self.events.append(event)


class _Uow:
    def __init__(
        self,
        batch: EditionProductionBatch,
        runs: list[SubjectProductionRun],
        items: list[EditionProductionBatchItem],
        edition: Edition | None = None,
    ) -> None:
        self.subject_production_runs = _Runs(runs)
        self.edition_production_batches = _Batches(batch)
        self.edition_production_batch_items = _BatchItems(items)
        if edition is not None:
            self.editions = _Editions(edition)
            self.edition_audit = _Audit()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _failed_run(edition_id: UUID, subject_id: UUID, code: str) -> SubjectProductionRun:
    run = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        current_stage=SubjectProductionStage.SOURCES,
    )
    run.start_running()
    run.mark_failed(code=code, message=code)
    return run


def _batch_uow(codes: list[str]) -> tuple[_Uow, list[SubjectProductionRun]]:
    edition_id = uuid4()
    runs = [_failed_run(edition_id, uuid4(), code) for code in codes]
    batch = EditionProductionBatch(
        edition_id=edition_id,
        status="running",
    )
    items = [
        EditionProductionBatchItem(
            batch_id=batch.id,
            subject_id=run.subject_id,
            production_run_id=run.id,
            position=position,
        )
        for position, run in enumerate(runs, start=1)
    ]
    edition = Edition(
        id=edition_id,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=len(runs),
        source_profile="default",
        status=EditionStatus.PRODUCTION,
    )
    return _Uow(batch, runs, items, edition), runs


def _q2_recovery_case(
    source_failures: Any,
) -> tuple[EditionProductionBatchItem, SubjectProductionRun]:
    uow, runs = _batch_uow(["q2_source_coverage_failed"])
    run = runs[0]
    run.error_details = {"source_failures": source_failures}
    return uow.edition_production_batch_items.items[0], run


def _q2_failure(
    *,
    retryable: Any = True,
    contributes_to_coverage: Any = True,
    failure_class: str | None = None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "error_code": "source_content_invalid",
        "retryable": retryable,
        "contributes_to_coverage": contributes_to_coverage,
    }
    if failure_class is not None:
        failure["failure_class"] = failure_class
    return failure


def test_recovery_policy_is_allow_list_only() -> None:
    assert ProductionRecoveryPolicyV1.is_auto_recoverable("bridge_server_error")
    for code in (
        "bridge_extension_disconnected",
        "bridge_unreachable",
        "bridge_rate_limited",
    ):
        assert ProductionRecoveryPolicyV1.is_auto_recoverable(code)
    assert ProductionRecoveryPolicyV1.is_auto_recoverable("synthesis_validation_failed")
    assert not ProductionRecoveryPolicyV1.is_auto_recoverable("q2_source_coverage_failed")
    assert not ProductionRecoveryPolicyV1.is_auto_recoverable("unknown_code")
    assert not ProductionRecoveryPolicyV1.is_auto_recoverable(
        "model_submission_reconciliation_required"
    )


def test_q2_terminal_source_failure_is_not_automatically_recoverable() -> None:
    item, run = _q2_recovery_case(
        {"S14": _q2_failure(retryable=False)},
    )

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


def test_q2_missing_error_details_is_not_automatically_recoverable() -> None:
    item, run = _q2_recovery_case(None)

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


def test_q2_malformed_source_failures_is_not_automatically_recoverable() -> None:
    item, run = _q2_recovery_case([_q2_failure()])

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


def test_q2_missing_retryability_is_not_automatically_recoverable() -> None:
    failure = _q2_failure()
    del failure["retryable"]
    item, run = _q2_recovery_case({"S1": failure})

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


def test_q2_mixed_retryability_is_not_automatically_recoverable() -> None:
    item, run = _q2_recovery_case(
        {
            "S1": _q2_failure(retryable=True),
            "S2": _q2_failure(retryable=False),
        }
    )

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


def test_q2_all_blocking_failures_retryable_is_automatically_recoverable() -> None:
    item, run = _q2_recovery_case(
        {
            "S1": _q2_failure(retryable=True),
            "S2": _q2_failure(retryable=True),
        }
    )

    assert ProductionRecoveryPolicyV1.eligible(item, run)


def test_q2_non_blocking_failure_does_not_hide_a_terminal_blocking_failure() -> None:
    item, run = _q2_recovery_case(
        {
            "S1": _q2_failure(retryable=True, contributes_to_coverage=False),
            "S2": _q2_failure(retryable=False),
        }
    )

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


@pytest.mark.parametrize(
    "failure_class",
    ("reconciliation_required", "control_invariant_failure"),
)
def test_q2_reconciliation_and_control_failures_are_manual_only(failure_class: str) -> None:
    item, run = _q2_recovery_case(
        {"S1": _q2_failure(failure_class=failure_class)},
    )

    assert not ProductionRecoveryPolicyV1.eligible(item, run)


@pytest.mark.asyncio
async def test_reconciliation_failure_is_never_automatically_recovered() -> None:
    uow, runs = _batch_uow(["model_submission_reconciliation_required"])
    service = EditionProductionService(lambda: uow)

    assert (
        await service.on_subject_terminal(uow.edition_production_batches.item.id, runs[0].id)
        is None
    )

    item = uow.edition_production_batch_items.items[0]
    assert item.auto_recovery_count == 0
    assert runs[0].pipeline_generation == 0
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.REVIEW


@pytest.mark.asyncio
async def test_cancelled_run_is_never_automatically_recovered() -> None:
    uow, runs = _batch_uow(["bridge_timeout"])
    runs[0].status = SubjectProductionStatus.CANCELLED
    service = EditionProductionService(lambda: uow)

    assert (
        await service.on_subject_terminal(uow.edition_production_batches.item.id, runs[0].id)
        is None
    )

    item = uow.edition_production_batch_items.items[0]
    assert item.auto_recovery_count == 0
    assert runs[0].pipeline_generation == 0
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.REVIEW


@pytest.mark.asyncio
async def test_recovery_runs_candidates_in_editorial_order_and_finishes_review() -> None:
    uow, runs = _batch_uow(["bridge_timeout", "no_model_response"])
    service = EditionProductionService(
        lambda: uow,
        ProductionPacingPolicy(
            subject_jitter_min_seconds=0,
            subject_jitter_max_seconds=0,
            model_jitter_min_seconds=0,
            model_jitter_max_seconds=0,
        ),
    )

    first = await service.on_subject_terminal(uow.edition_production_batches.item.id, runs[1].id)
    assert first is runs[0]
    assert first.pipeline_generation == 1
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.RECOVERY
    assert uow.edition_production_batch_items.items[0].auto_recovery_count == 1

    first.mark_failed(code="unknown_code", message="manual")
    second = await service.on_subject_terminal(uow.edition_production_batches.item.id, first.id)
    assert second is runs[1]
    assert second.pipeline_generation == 1
    assert uow.edition_production_batch_items.items[1].auto_recovery_count == 1

    second.mark_failed(code="unknown_code", message="manual")
    assert (
        await service.on_subject_terminal(uow.edition_production_batches.item.id, second.id)
    ) is None
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.REVIEW
    assert uow.edition_production_batches.item.status == "completed_with_issues"
    assert uow.editions.edition.status is EditionStatus.REVIEW
    assert len(uow.edition_audit.events) == 1


@pytest.mark.asyncio
async def test_batch_terminal_handoff_moves_production_edition_to_review() -> None:
    uow, runs = _batch_uow(["unknown_code"])
    service = EditionProductionService(lambda: uow)

    result = await service.on_subject_terminal(uow.edition_production_batches.item.id, runs[0].id)

    assert result is None
    assert uow.editions.edition.status is EditionStatus.REVIEW


@pytest.mark.asyncio
async def test_initial_failure_does_not_block_remaining_subjects_before_recovery() -> None:
    uow, runs = _batch_uow(["bridge_timeout", "unknown_code", "unknown_code"])
    runs[1].status = SubjectProductionStatus.QUEUED
    runs[2].status = SubjectProductionStatus.QUEUED
    service = EditionProductionService(lambda: uow)
    batch_id = uow.edition_production_batches.item.id

    second = await service.on_subject_terminal(batch_id, runs[0].id)
    assert second is runs[1]
    assert runs[1].status is SubjectProductionStatus.RUNNING
    runs[1].mark_failed(code="unknown_code", message="manual")

    third = await service.on_subject_terminal(batch_id, runs[1].id)
    assert third is runs[2]
    assert runs[2].status is SubjectProductionStatus.RUNNING
    runs[2].mark_failed(code="unknown_code", message="manual")

    recovery = await service.on_subject_terminal(batch_id, runs[2].id)
    assert recovery is runs[0]
    assert runs[0].pipeline_generation == 1
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.RECOVERY


@pytest.mark.asyncio
async def test_recovery_subject_schedule_is_persisted_before_dispatch() -> None:
    uow, runs = _batch_uow(["bridge_timeout"])
    policy = ProductionPacingPolicy(
        subject_jitter_min_seconds=7,
        subject_jitter_max_seconds=7,
        model_jitter_min_seconds=0,
        model_jitter_max_seconds=0,
    )
    service = EditionProductionService(lambda: uow, policy)

    recovered = await service.on_subject_terminal(
        uow.edition_production_batches.item.id, runs[0].id
    )

    assert recovered is runs[0]
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.RECOVERY
    dispatch_at = uow.edition_production_batches.item.next_dispatch_at
    assert dispatch_at is not None
    assert 0 < policy.delay_until(dispatch_at, now=datetime.now(UTC)) <= 7000


@pytest.mark.asyncio
async def test_unknown_error_is_manual_only_and_count_never_exceeds_one() -> None:
    uow, runs = _batch_uow(["unknown_code"])
    service = EditionProductionService(lambda: uow)

    assert (
        await service.on_subject_terminal(uow.edition_production_batches.item.id, runs[0].id)
        is None
    )
    item = uow.edition_production_batch_items.items[0]
    assert item.auto_recovery_count == 0
    assert runs[0].pipeline_generation == 0
    assert uow.edition_production_batches.item.phase is ProductionBatchPhase.REVIEW


@pytest.mark.asyncio
async def test_auto_recovery_count_blocks_a_second_automatic_recovery() -> None:
    uow, runs = _batch_uow(["bridge_timeout"])
    service = EditionProductionService(lambda: uow)
    batch_id = uow.edition_production_batches.item.id

    recovered = await service.on_subject_terminal(batch_id, runs[0].id)
    assert recovered is runs[0]
    assert uow.edition_production_batch_items.items[0].auto_recovery_count == 1

    recovered.mark_failed(code="bridge_timeout", message="bridge stopped again")
    assert await service.on_subject_terminal(batch_id, recovered.id) is None
    assert uow.edition_production_batch_items.items[0].auto_recovery_count == 1


@pytest.mark.asyncio
async def test_subject_schedule_is_persisted_and_cleared_when_worker_starts() -> None:
    uow, runs = _batch_uow(["bridge_timeout", "unknown_code"])
    # The first run is terminal and the second is queued: this is the normal
    # hand-off path between two subjects of the initial pass.
    runs[1].status = SubjectProductionStatus.QUEUED
    policy = ProductionPacingPolicy(
        subject_jitter_min_seconds=7,
        subject_jitter_max_seconds=7,
        model_jitter_min_seconds=0,
        model_jitter_max_seconds=0,
    )
    service = EditionProductionService(lambda: uow, policy)

    next_run = await service.on_subject_terminal(uow.edition_production_batches.item.id, runs[0].id)
    assert next_run is runs[1]
    assert uow.edition_production_batches.item.next_dispatch_at is not None
    assert (
        policy.delay_until(
            uow.edition_production_batches.item.next_dispatch_at,
            now=datetime.now(UTC),
        )
        <= 7000
    )
    await service.clear_next_dispatch(next_run.id)
    assert uow.edition_production_batches.item.next_dispatch_at is None


@pytest.mark.asyncio
async def test_stage_dispatch_uses_model_jitter_and_subject_override() -> None:
    class Jobs:
        async def submit(self, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(id=uuid4())

    class Dispatcher:
        def __init__(self) -> None:
            self.delays: list[int] = []

        async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
            del job_id
            self.delays.append(delay_ms)

    dispatcher = Dispatcher()
    chain = ProductionStageChain(
        ProductionPacingPolicy(
            subject_jitter_min_seconds=0,
            subject_jitter_max_seconds=0,
            model_jitter_min_seconds=11,
            model_jitter_max_seconds=11,
        )
    )
    chain.bind(Jobs(), dispatcher)  # type: ignore[arg-type]
    run = SubjectProductionRun(subject_id=uuid4(), edition_id=uuid4())

    await chain.submit(
        run=run,
        stage=SubjectProductionStage.REFERENCES,
        correlation_id="test",
    )
    await chain.submit(
        run=run,
        stage=SubjectProductionStage.SOURCES,
        correlation_id="test",
        delay_ms=7000,
    )
    assert dispatcher.delays == [11000, 7000]


@pytest.mark.parametrize(
    ("stage", "keep_references", "keep_synthesis"),
    (
        (SubjectProductionStage.SOURCES, False, False),
        (SubjectProductionStage.REFERENCES, False, False),
        (SubjectProductionStage.EXTRACTION, True, False),
        (SubjectProductionStage.SYNTHESIS, True, False),
        (SubjectProductionStage.ASSEMBLY, True, True),
    ),
)
def test_business_retry_uses_fresh_conversations_per_stage(
    stage: SubjectProductionStage, keep_references: bool, keep_synthesis: bool
) -> None:
    references_id = uuid4()
    synthesis_id = uuid4()
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        references_conversation_id=references_id,
        synthesis_conversation_id=synthesis_id,
    )
    run.start_running()
    run.mark_failed(code="bridge_timeout", message="failure")

    run.retry_from_stage(stage)

    assert run.references_conversation_id == (references_id if keep_references else None)
    assert run.synthesis_conversation_id == (synthesis_id if keep_synthesis else None)
    assert run.pipeline_generation == 1
