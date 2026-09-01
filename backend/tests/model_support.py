from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from uuid import UUID

from cti_app.application.model_gateway import ModelRunRepository, ModelRunUnitOfWork
from cti_app.domain.model_runs import ModelOutputRejection, ModelRun, ModelRunStatus


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

    async def find_successful_q2_checkpoint(self, checkpoint_key: str) -> ModelRun | None:
        matches = [
            run
            for run in self._state.values()
            if run.status is ModelRunStatus.SUCCEEDED
            and checkpoint_key in run.parameters.get("q2_checkpoint_keys", [])
        ]
        if not matches:
            return None
        return deepcopy(max(matches, key=lambda run: (run.updated_at, str(run.id))))

    async def save(self, run: ModelRun) -> None:
        if run.id not in self._state:
            raise LookupError(run.id)
        self._state[run.id] = deepcopy(run)


class InMemoryModelOutputRejectionRepository:
    def __init__(self) -> None:
        self.items: list[ModelOutputRejection] = []

    async def append(self, rejection: ModelOutputRejection) -> None:
        self.items.append(deepcopy(rejection))

    async def list_for_run(self, run_id: UUID) -> list[ModelOutputRejection]:
        return [deepcopy(item) for item in self.items if item.model_run_id == run_id]


class InMemoryModelRunUnitOfWork:
    model_runs: ModelRunRepository

    def __init__(self, state: dict[UUID, ModelRun]) -> None:
        self.model_runs = InMemoryModelRunRepository(state)
        self.model_output_rejections = InMemoryModelOutputRejectionRepository()

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
