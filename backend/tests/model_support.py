from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from uuid import UUID

from cti_app.application.model_gateway import ModelRunRepository, ModelRunUnitOfWork
from cti_app.domain.model_runs import ModelRun


class InMemoryModelRunRepository:
    def __init__(self, state: dict[UUID, ModelRun]) -> None:
        self._state = state

    async def add(self, run: ModelRun) -> None:
        if run.id in self._state:
            raise ValueError("Duplicate model run")
        self._state[run.id] = deepcopy(run)

    async def get(self, run_id: UUID) -> ModelRun | None:
        run = self._state.get(run_id)
        return deepcopy(run) if run else None

    async def get_for_update(self, run_id: UUID) -> ModelRun | None:
        return await self.get(run_id)

    async def save(self, run: ModelRun) -> None:
        if run.id not in self._state:
            raise LookupError(run.id)
        self._state[run.id] = deepcopy(run)


class InMemoryModelRunUnitOfWork:
    model_runs: ModelRunRepository

    def __init__(self, state: dict[UUID, ModelRun]) -> None:
        self.model_runs = InMemoryModelRunRepository(state)

    async def __aenter__(self) -> InMemoryModelRunUnitOfWork:
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


class InMemoryModelRunUnitOfWorkFactory:
    def __init__(self) -> None:
        self.state: dict[UUID, ModelRun] = {}

    def __call__(self) -> ModelRunUnitOfWork:
        return InMemoryModelRunUnitOfWork(self.state)
