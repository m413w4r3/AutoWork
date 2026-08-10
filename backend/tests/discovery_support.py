from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from uuid import UUID

from cti_app.application.persistence import DiscoveryBatchRepository, DiscoveryUnitOfWork
from cti_app.domain.discovery import DiscoveryBatch


class InMemoryDiscoveryBatchRepository:
    def __init__(self, state: dict[UUID, DiscoveryBatch]) -> None:
        self._state = state

    async def add_if_absent(self, batch: DiscoveryBatch) -> bool:
        if any(
            item.edition_id == batch.edition_id and item.request_hash == batch.request_hash
            for item in self._state.values()
        ):
            return False
        self._state[batch.id] = deepcopy(batch)
        return True

    async def get(self, batch_id: UUID) -> DiscoveryBatch | None:
        batch = self._state.get(batch_id)
        return deepcopy(batch) if batch else None

    async def get_by_request_hash(
        self, edition_id: UUID, request_hash: str
    ) -> DiscoveryBatch | None:
        batch = next(
            (
                item
                for item in self._state.values()
                if item.edition_id == edition_id and item.request_hash == request_hash
            ),
            None,
        )
        return deepcopy(batch) if batch else None

    async def list_for_edition(self, edition_id: UUID) -> list[DiscoveryBatch]:
        return [deepcopy(item) for item in self._state.values() if item.edition_id == edition_id]

    async def save(self, batch: DiscoveryBatch) -> None:
        if batch.id not in self._state:
            raise LookupError(batch.id)
        self._state[batch.id] = deepcopy(batch)


class InMemoryDiscoveryUnitOfWork:
    discovery_batches: DiscoveryBatchRepository

    def __init__(self, state: dict[UUID, DiscoveryBatch]) -> None:
        self.discovery_batches = InMemoryDiscoveryBatchRepository(state)

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

    def __call__(self) -> DiscoveryUnitOfWork:
        return InMemoryDiscoveryUnitOfWork(self.state)
