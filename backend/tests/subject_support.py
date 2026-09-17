from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from types import TracebackType
from uuid import UUID

from cti_app.application.persistence import SubjectRepository
from cti_app.domain.editions import Edition, EditionAuditEvent
from cti_app.domain.entities import Subject
from tests.edition_support import InMemoryEditionAuditRepository, InMemoryEditionRepository


class InMemorySubjectRepository:
    def __init__(self, state: dict[UUID, Subject]) -> None:
        self._state = state

    async def add(self, subject: Subject) -> None:
        self._state[subject.id] = deepcopy(subject)

    async def get(self, subject_id: UUID) -> Subject | None:
        subject = self._state.get(subject_id)
        return deepcopy(subject) if subject else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[Subject]:
        return [
            deepcopy(subject)
            for subject in sorted(self._state.values(), key=lambda item: item.created_at)
            if subject.edition_id == edition_id
        ]

    async def update(self, subject: Subject, *, expected_version: int) -> bool:
        current = self._state.get(subject.id)
        if current is None or current.version != expected_version:
            return False
        self._state[subject.id] = deepcopy(subject)
        return True


class InMemorySubjectUnitOfWork:
    editions: InMemoryEditionRepository
    edition_audit: InMemoryEditionAuditRepository
    subjects: SubjectRepository

    def __init__(
        self,
        edition_state: dict[UUID, Edition],
        subject_state: dict[UUID, Subject],
        events: list[EditionAuditEvent],
    ) -> None:
        self.editions = InMemoryEditionRepository(edition_state)
        self.edition_audit = InMemoryEditionAuditRepository(events)
        self.subjects = InMemorySubjectRepository(subject_state)

    async def __aenter__(self) -> InMemorySubjectUnitOfWork:
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


class InMemorySubjectUnitOfWorkFactory:
    def __init__(
        self,
        edition_state: dict[UUID, Edition] | None = None,
        subject_state: dict[UUID, Subject] | None = None,
    ) -> None:
        self.edition_state = edition_state if edition_state is not None else {}
        self.subject_state = subject_state if subject_state is not None else {}
        self.events: list[EditionAuditEvent] = []
        self.state = self.edition_state

    def __call__(self) -> InMemorySubjectUnitOfWork:
        return InMemorySubjectUnitOfWork(self.edition_state, self.subject_state, self.events)
