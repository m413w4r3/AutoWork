from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from datetime import date
from types import TracebackType
from uuid import UUID

from cti_app.application.persistence import EditionAuditRepository, EditionRepository
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus


class InMemoryEditionRepository:
    def __init__(self, state: dict[UUID, Edition]) -> None:
        self._state = state

    async def add_if_absent(self, edition: Edition) -> bool:
        if await self.get_by_logical_key(
            edition.country_code, edition.period_start, edition.period_end
        ):
            return False
        self._state[edition.id] = deepcopy(edition)
        return True

    async def get(self, edition_id: UUID) -> Edition | None:
        edition = self._state.get(edition_id)
        return deepcopy(edition) if edition else None

    async def get_by_logical_key(
        self, country_code: str, period_start: date, period_end: date
    ) -> Edition | None:
        for edition in self._state.values():
            if (
                edition.country_code == country_code
                and edition.period_start == period_start
                and edition.period_end == period_end
            ):
                return deepcopy(edition)
        return None

    async def update(self, edition: Edition, expected_version: int) -> bool:
        current = self._state.get(edition.id)
        if current is None or current.version != expected_version:
            return False
        self._state[edition.id] = deepcopy(edition)
        return True

    async def delete(self, edition_id: UUID, expected_version: int) -> bool:
        current = self._state.get(edition_id)
        if current is None or current.version != expected_version:
            return False
        del self._state[edition_id]
        for edition in self._state.values():
            if edition.previous_edition_id == edition_id:
                edition.previous_edition_id = None
        return True

    async def list(
        self,
        *,
        offset: int,
        limit: int,
        country_code: str | None,
        period_start: date | None,
        period_end: date | None,
        status: EditionStatus | None,
    ) -> tuple[Sequence[Edition], int]:
        matches = [
            deepcopy(edition)
            for edition in self._state.values()
            if (country_code is None or edition.country_code == country_code)
            and (period_start is None or edition.period_start >= period_start)
            and (period_end is None or edition.period_end <= period_end)
            and (status is None or edition.status is status)
        ]
        matches.sort(key=lambda item: (item.period_start, item.country_code), reverse=True)
        return matches[offset : offset + limit], len(matches)


class InMemoryEditionAuditRepository:
    def __init__(self, events: list[EditionAuditEvent]) -> None:
        self._events = events

    async def append(self, event: EditionAuditEvent) -> None:
        self._events.append(deepcopy(event))

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionAuditEvent]:
        return [deepcopy(event) for event in self._events if event.edition_id == edition_id]

    async def delete_for_edition(self, edition_id: UUID) -> None:
        self._events[:] = [event for event in self._events if event.edition_id != edition_id]


class InMemoryEditionUnitOfWork:
    editions: EditionRepository
    edition_audit: EditionAuditRepository

    def __init__(self, state: dict[UUID, Edition], events: list[EditionAuditEvent]) -> None:
        self.editions = InMemoryEditionRepository(state)
        self.edition_audit = InMemoryEditionAuditRepository(events)

    async def __aenter__(self) -> InMemoryEditionUnitOfWork:
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


class InMemoryEditionUnitOfWorkFactory:
    def __init__(self) -> None:
        self.state: dict[UUID, Edition] = {}
        self.events: list[EditionAuditEvent] = []

    def __call__(self) -> InMemoryEditionUnitOfWork:
        return InMemoryEditionUnitOfWork(self.state, self.events)
