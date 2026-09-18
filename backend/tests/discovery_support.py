from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.discovery.contracts import (
    discover_parameters_from_edition,
    discovery_request_snapshot,
)
from cti_app.application.persistence import (
    DiscoveryBatchRepository,
    DiscoveryRunRepository,
    EditionAuditRepository,
    EditionRepository,
    JobEventRepository,
    JobRepository,
)
from cti_app.domain.discovery import (
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryRun,
    DiscoveryRunInputMode,
)
from cti_app.domain.editions import Edition, EditionAuditEvent
from cti_app.domain.jobs import Job, JobEvent
from tests.edition_support import (
    InMemoryEditionAuditRepository,
    InMemoryEditionRepository,
)
from tests.job_support import InMemoryJobEventRepository, InMemoryJobRepository


class InMemoryDiscoveryBatchRepository:
    def __init__(
        self,
        state: dict[UUID, DiscoveryBatch],
        candidate_state: dict[UUID, DiscoveryCandidate] | None = None,
    ) -> None:
        # Without candidate_state, the repository serves legacy fixtures that seed
        # batches (and their candidate projection) directly into ``state``.
        self._state = state
        self._candidate_state = candidate_state

    async def add_if_absent(self, batch: DiscoveryBatch) -> bool:
        if batch.id in self._state:
            return False
        if self._candidate_state is None:
            self._state[batch.id] = deepcopy(batch)
            return True
        stored = deepcopy(batch)
        stored.candidates = []
        self._state[batch.id] = stored
        for position, candidate in enumerate(batch.candidates):
            canonical = DiscoveryCandidate.from_candidate_topic(
                candidate,
                discovery_run_id=batch.discovery_run_id,
                discovery_batch_id=batch.id,
                position=position,
                created_at=batch.created_at,
            )
            self._candidate_state[canonical.id] = canonical
        return True

    def _materialize(self, batch: DiscoveryBatch) -> DiscoveryBatch:
        candidate_state = self._candidate_state
        if candidate_state is None:
            return batch
        batch.candidates = [
            candidate.to_candidate_topic()
            for candidate in sorted(
                (
                    candidate
                    for candidate in candidate_state.values()
                    if candidate.discovery_batch_id == batch.id
                ),
                key=lambda candidate: candidate.position,
            )
        ]
        return batch

    async def get(self, batch_id: UUID) -> DiscoveryBatch | None:
        batch = self._state.get(batch_id)
        return self._materialize(deepcopy(batch)) if batch else None

    async def get_for_update(self, batch_id: UUID) -> DiscoveryBatch | None:
        batch = self._state.get(batch_id)
        return self._materialize(deepcopy(batch)) if batch else None

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryBatch]:
        return [
            self._materialize(deepcopy(item))
            for item in self._state.values()
            if item.edition_id == edition_id
        ]

    async def list_for_run(self, discovery_run_id: UUID) -> list[DiscoveryBatch]:
        return [
            self._materialize(deepcopy(item))
            for item in self._state.values()
            if item.discovery_run_id == discovery_run_id
        ]

    async def save(self, batch: DiscoveryBatch) -> None:
        if batch.id not in self._state:
            raise LookupError(batch.id)
        stored = deepcopy(batch)
        if self._candidate_state is not None:
            stored.candidates = []
        self._state[batch.id] = stored


class InMemoryDiscoveryCandidateRepository:
    def __init__(
        self,
        candidate_state: dict[UUID, DiscoveryCandidate],
        batches: dict[UUID, DiscoveryBatch],
    ) -> None:
        self._state = candidate_state
        self._batches = batches

    async def add_many(self, candidates: list[DiscoveryCandidate]) -> None:
        for candidate in candidates:
            self._state[candidate.id] = deepcopy(candidate)

    async def get(self, candidate_id: UUID) -> DiscoveryCandidate | None:
        candidate = self._state.get(candidate_id)
        return deepcopy(candidate) if candidate else None

    async def get_for_update(self, candidate_id: UUID) -> DiscoveryCandidate | None:
        candidate = self._state.get(candidate_id)
        return deepcopy(candidate) if candidate else None

    async def list_for_batch(self, discovery_batch_id: UUID) -> list[DiscoveryCandidate]:
        return sorted(
            [
                deepcopy(candidate)
                for candidate in self._state.values()
                if candidate.discovery_batch_id == discovery_batch_id
            ],
            key=lambda candidate: (candidate.position, candidate.id),
        )

    async def list_for_run(self, discovery_run_id: UUID) -> list[DiscoveryCandidate]:
        return sorted(
            [
                deepcopy(candidate)
                for candidate in self._state.values()
                if candidate.discovery_run_id == discovery_run_id
            ],
            key=self._revision_order,
        )

    def _revision_order(self, candidate: DiscoveryCandidate) -> tuple[datetime, str, int]:
        # Mirrors the SQL ordering: batch revision chronology, then batch position.
        batch = self._batches.get(candidate.discovery_batch_id)
        created_at = batch.created_at if batch is not None else candidate.created_at
        return created_at, str(candidate.discovery_batch_id), candidate.position

    async def list_for_edition(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> list[DiscoveryCandidate]:
        return sorted(
            [
                deepcopy(candidate)
                for candidate in self._state.values()
                if candidate.discovery_batch_id in self._batches
                and self._batches[candidate.discovery_batch_id].edition_id == edition_id
                and (
                    include_replaced
                    or self._batches[candidate.discovery_batch_id].replaced_by_batch_id is None
                )
            ],
            key=self._revision_order,
        )

    async def save_evidence(self, candidate: DiscoveryCandidate) -> None:
        if candidate.id not in self._state:
            raise LookupError(candidate.id)
        stored = deepcopy(self._state[candidate.id])
        stored.evidence = deepcopy(candidate.evidence)
        self._state[candidate.id] = stored


class InMemoryDiscoveryRunRepository:
    def __init__(self, state: dict[UUID, DiscoveryRun]) -> None:
        self._state = state

    async def add_if_absent(self, run: DiscoveryRun) -> bool:
        if any(
            item.edition_id == run.edition_id
            and item.input_mode is run.input_mode
            and item.idempotency_key == run.idempotency_key
            for item in self._state.values()
        ):
            return False
        self._state[run.id] = deepcopy(run)
        return True

    async def get(self, run_id: UUID) -> DiscoveryRun | None:
        run = self._state.get(run_id)
        return deepcopy(run) if run else None

    async def get_by_idempotency_key(
        self, edition_id: UUID, input_mode: DiscoveryRunInputMode, idempotency_key: str
    ) -> DiscoveryRun | None:
        for run in self._state.values():
            if (
                run.edition_id == edition_id
                and run.input_mode is input_mode
                and run.idempotency_key == idempotency_key
            ):
                return deepcopy(run)
        return None

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryRun]:
        return sorted(
            [deepcopy(run) for run in self._state.values() if run.edition_id == edition_id],
            key=lambda run: (run.created_at, run.id),
            reverse=True,
        )


class InMemoryDiscoveryUnitOfWork:
    discovery_batches: DiscoveryBatchRepository
    discovery_candidates: Any
    discovery_runs: DiscoveryRunRepository
    editions: EditionRepository
    edition_audit: EditionAuditRepository
    jobs: JobRepository
    job_events: JobEventRepository

    def __init__(
        self,
        state: dict[UUID, DiscoveryBatch],
        candidate_state: dict[UUID, DiscoveryCandidate],
        runs: dict[UUID, DiscoveryRun],
        editions: dict[UUID, Edition],
        edition_events: list[EditionAuditEvent],
        jobs: dict[UUID, Job],
        job_events: list[JobEvent],
    ) -> None:
        self.discovery_batches = InMemoryDiscoveryBatchRepository(state, candidate_state)
        self.discovery_candidates = InMemoryDiscoveryCandidateRepository(candidate_state, state)
        self.discovery_runs = InMemoryDiscoveryRunRepository(runs)
        self.editions = InMemoryEditionRepository(editions)
        self.edition_audit = InMemoryEditionAuditRepository(edition_events)
        self.jobs = InMemoryJobRepository(jobs)
        self.job_events = InMemoryJobEventRepository(job_events)

    async def __aenter__(self) -> InMemoryDiscoveryUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class InMemoryDiscoveryUnitOfWorkFactory:
    def __init__(self) -> None:
        self.state: dict[UUID, DiscoveryBatch] = {}
        self.candidate_state: dict[UUID, DiscoveryCandidate] = {}
        self.runs: dict[UUID, DiscoveryRun] = {}
        self.editions: dict[UUID, Edition] = {}
        self.edition_events: list[EditionAuditEvent] = []
        self.jobs: dict[UUID, Job] = {}
        self.job_events: list[JobEvent] = []

    def __call__(self) -> InMemoryDiscoveryUnitOfWork:
        return InMemoryDiscoveryUnitOfWork(
            self.state,
            self.candidate_state,
            self.runs,
            self.editions,
            self.edition_events,
            self.jobs,
            self.job_events,
        )


async def make_discovery_run_for_edition(
    uow_factory: Callable[[], Any],
    edition: Edition,
    *,
    source_profile: str = "default-v1",
    complementary_axis: str = "initial",
    actor_id: str = "integration-analyst",
) -> DiscoveryRun:
    run_id = uuid4()
    parameters = discover_parameters_from_edition(
        edition,
        discovery_run_id=run_id,
        source_profile=source_profile,
        complementary_axis=complementary_axis,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    run = DiscoveryRun(
        id=run_id,
        edition_id=edition.id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile=source_profile,
        complementary_axis=complementary_axis,
        request_snapshot=discovery_request_snapshot(parameters),
        idempotency_key=f"fixture-{run_id}",
        created_by=actor_id,
    )
    async with uow_factory() as uow:
        assert await uow.discovery_runs.add_if_absent(run)
        await uow.commit()
    return run
