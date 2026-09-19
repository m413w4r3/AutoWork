from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.discovery.contracts import (
    discover_parameters_from_edition,
    discovery_request_snapshot,
)
from cti_app.application.persistence import (
    DiscoveryBatchRepository,
    DiscoveryCandidateRepository,
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
    def __init__(self, state: dict[UUID, DiscoveryBatch]) -> None:
        self._state = state

    async def add_if_absent(self, batch: DiscoveryBatch) -> bool:
        if batch.id in self._state:
            return False
        self._state[batch.id] = deepcopy(batch)
        return True

    async def get(self, batch_id: UUID) -> DiscoveryBatch | None:
        batch = self._state.get(batch_id)
        return deepcopy(batch) if batch else None

    async def get_for_update(self, batch_id: UUID) -> DiscoveryBatch | None:
        batch = self._state.get(batch_id)
        return deepcopy(batch) if batch else None

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryBatch]:
        return [deepcopy(item) for item in self._state.values() if item.edition_id == edition_id]

    async def list_for_run(self, discovery_run_id: UUID) -> list[DiscoveryBatch]:
        return [
            deepcopy(item)
            for item in self._state.values()
            if item.discovery_run_id == discovery_run_id
        ]

    async def save(self, batch: DiscoveryBatch) -> None:
        if batch.id not in self._state:
            raise LookupError(batch.id)
        self._state[batch.id] = deepcopy(batch)


class InMemoryDiscoveryCandidateRepository:
    def __init__(
        self,
        state: dict[UUID, DiscoveryCandidate],
        batches: dict[UUID, DiscoveryBatch],
    ) -> None:
        self._state = state
        self._batches = batches

    async def add_sequence(self, candidates: Sequence[DiscoveryCandidate]) -> None:
        if any(candidate.id in self._state for candidate in candidates):
            raise ValueError("Discovery candidate already exists")
        positions = {(candidate.discovery_batch_id, candidate.position) for candidate in candidates}
        if len(positions) != len(candidates):
            raise ValueError("Discovery candidate position already exists")
        if any(
            candidate.supersedes_candidate_id is not None
            and any(
                existing.supersedes_candidate_id == candidate.supersedes_candidate_id
                for existing in self._state.values()
            )
            for candidate in candidates
        ):
            raise ValueError("Discovery candidate replacement already exists")
        self._state.update({candidate.id: deepcopy(candidate) for candidate in candidates})

    async def get(self, candidate_id: UUID) -> DiscoveryCandidate | None:
        candidate = self._state.get(candidate_id)
        return deepcopy(candidate) if candidate else None

    async def list_for_batch(self, batch_id: UUID) -> list[DiscoveryCandidate]:
        return sorted(
            [
                deepcopy(candidate)
                for candidate in self._state.values()
                if candidate.discovery_batch_id == batch_id
            ],
            key=lambda candidate: (candidate.position, candidate.id),
        )

    async def list_for_edition(
        self, edition_id: UUID, *, include_replaced: bool = False
    ) -> list[DiscoveryCandidate]:
        successor_ids = {
            candidate.supersedes_candidate_id
            for candidate in self._state.values()
            if candidate.supersedes_candidate_id is not None
        }
        candidates = [
            candidate
            for candidate in self._state.values()
            if self._batches[candidate.discovery_batch_id].edition_id == edition_id
            and (
                include_replaced
                or self._batches[candidate.discovery_batch_id].is_active_revision
            )
            and (include_replaced or candidate.id not in successor_ids)
        ]
        return [
            deepcopy(candidate)
            for candidate in sorted(
                candidates,
                key=lambda candidate: (
                    candidate.created_at,
                    candidate.position,
                    candidate.id,
                ),
            )
        ]

    async def save(self, candidate: DiscoveryCandidate) -> None:
        if candidate.id not in self._state:
            raise LookupError(candidate.id)
        self._state[candidate.id] = deepcopy(candidate)


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
    discovery_candidates: DiscoveryCandidateRepository
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
        self.discovery_batches = InMemoryDiscoveryBatchRepository(state)
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


def canonical_candidates_for(batch: DiscoveryBatch) -> list[DiscoveryCandidate]:
    """Canonical rows a successful discovery path persists alongside its batch."""
    return [
        DiscoveryCandidate(
            id=candidate.id,
            discovery_run_id=batch.discovery_run_id,
            discovery_batch_id=batch.id,
            position=position,
            candidate=deepcopy(candidate),
            created_at=batch.created_at,
        )
        for position, candidate in enumerate(batch.candidates, 1)
    ]


async def persist_batch_with_candidates(uow: Any, batch: DiscoveryBatch) -> None:
    assert await uow.discovery_batches.add_if_absent(batch)
    await uow.discovery_candidates.add_sequence(canonical_candidates_for(batch))
