from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from typing import cast
from uuid import UUID

from cti_app.application.persistence import UnitOfWork
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import EditorialGroup, HumanDecision
from cti_app.domain.entities import Subject
from tests.discovery_support import InMemoryDiscoveryBatchRepository
from tests.edition_support import InMemoryEditionRepository


class InMemoryEditorialGroupRepository:
    def __init__(self, groups: dict[UUID, EditorialGroup], editions: dict[UUID, Edition]) -> None:
        self._groups = groups
        self._editions = editions

    async def add(self, group: EditorialGroup) -> None:
        self._groups[group.id] = deepcopy(group)

    async def get(self, group_id: UUID) -> EditorialGroup | None:
        value = self._groups.get(group_id)
        return deepcopy(value) if value else None

    async def get_for_update(self, group_id: UUID) -> EditorialGroup | None:
        return await self.get(group_id)

    async def list_for_edition(self, edition_id: UUID) -> list[EditorialGroup]:
        return [
            deepcopy(group) for group in self._groups.values() if group.edition_id == edition_id
        ]

    async def list_historical(self, edition_id: UUID) -> list[EditorialGroup]:
        edition = self._editions.get(edition_id)
        if edition is None:
            return []
        historical_ids = {
            item.id
            for item in self._editions.values()
            if item.country_code == edition.country_code
            and item.period_start < edition.period_start
        }
        return [
            deepcopy(group)
            for group in self._groups.values()
            if group.edition_id in historical_ids and group.status.value == "selected"
        ]

    async def save(self, group: EditorialGroup) -> None:
        if group.id not in self._groups:
            raise LookupError(group.id)
        self._groups[group.id] = deepcopy(group)


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


class EmptySourceDocumentRepository:
    async def list_for_subject(self, subject_id: UUID) -> list[object]:
        return []


class InMemoryEditorialUnitOfWork:
    def __init__(self, factory: InMemoryEditorialUnitOfWorkFactory) -> None:
        self.editions = InMemoryEditionRepository(factory.editions)
        self.discovery_batches = InMemoryDiscoveryBatchRepository(factory.batches)
        self.editorial_groups = InMemoryEditorialGroupRepository(factory.groups, factory.editions)
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
        self.groups: dict[UUID, EditorialGroup] = {}
        self.decisions: list[HumanDecision] = []
        self.subjects: dict[UUID, Subject] = {}

    def __call__(self) -> UnitOfWork:
        return cast(UnitOfWork, InMemoryEditorialUnitOfWork(self))
