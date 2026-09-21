from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from typing import cast
from uuid import UUID

from cti_app.application.persistence import UnitOfWork
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.discovery_cumulative import DiscoverySnapshot
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import HumanDecision
from cti_app.domain.entities import Subject
from tests.discovery_support import InMemoryDiscoveryBatchRepository
from tests.edition_support import InMemoryEditionRepository


class InMemoryHumanDecisionRepository:
    def __init__(self, decisions: list[HumanDecision]) -> None:
        self._decisions = decisions

    async def append(self, decision: HumanDecision) -> None:
        self._decisions.append(deepcopy(decision))

    async def list_for_edition(self, edition_id: UUID) -> list[HumanDecision]:
        return [
            deepcopy(decision) for decision in self._decisions if decision.edition_id == edition_id
        ]


class InMemorySubjectRepository:
    def __init__(self, subjects: dict[UUID, Subject]) -> None:
        self._subjects = subjects

    async def add(self, subject: Subject) -> None:
        self._subjects[subject.id] = deepcopy(subject)

    async def get(self, subject_id: UUID) -> Subject | None:
        value = self._subjects.get(subject_id)
        return deepcopy(value) if value else None

    async def list_for_edition(self, edition_id: UUID) -> list[Subject]:
        return [
            deepcopy(subject)
            for subject in self._subjects.values()
            if subject.edition_id == edition_id
        ]

    async def update(self, subject: Subject, *, expected_version: int) -> bool:
        current = self._subjects.get(subject.id)
        if current is None or current.version != expected_version:
            return False
        self._subjects[subject.id] = deepcopy(subject)
        return True


class EmptySourceDocumentRepository:
    async def list_for_subject(self, subject_id: UUID) -> list[object]:
        return []


class InMemoryDiscoverySnapshotRepository:
    def __init__(self, snapshots: dict[UUID, DiscoverySnapshot]) -> None:
        self._snapshots = snapshots

    async def get_active(self, edition_id: UUID) -> DiscoverySnapshot | None:
        value = self._snapshots.get(edition_id)
        return deepcopy(value) if value else None


class InMemoryEditorialUnitOfWork:
    def __init__(self, factory: InMemoryEditorialUnitOfWorkFactory) -> None:
        self.editions = InMemoryEditionRepository(factory.editions)
        self.discovery_batches = InMemoryDiscoveryBatchRepository(factory.batches)
        self.discovery_snapshots = InMemoryDiscoverySnapshotRepository(factory.snapshots)
        self.human_decisions = InMemoryHumanDecisionRepository(factory.decisions)
        self.subjects = InMemorySubjectRepository(factory.subjects)
        self.source_documents = EmptySourceDocumentRepository()

    async def __aenter__(self) -> InMemoryEditorialUnitOfWork:
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


class InMemoryEditorialUnitOfWorkFactory:
    def __init__(self) -> None:
        self.editions: dict[UUID, Edition] = {}
        self.batches: dict[UUID, DiscoveryBatch] = {}
        self.decisions: list[HumanDecision] = []
        self.subjects: dict[UUID, Subject] = {}
        self.snapshots: dict[UUID, DiscoverySnapshot] = {}

    def __call__(self) -> UnitOfWork:
        return cast(UnitOfWork, InMemoryEditorialUnitOfWork(self))
