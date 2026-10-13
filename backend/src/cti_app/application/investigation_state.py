"""CAS persistence port and in-memory implementation for loop state."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Protocol
from uuid import UUID, uuid5

from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_state import InvestigationWorkingState

_CONTEXT_SNAPSHOT_NAMESPACE = UUID("83e4d571-d840-4a78-9d55-6689d6ebfe51")


class InvestigationStateVersionConflict(RuntimeError):
    """The persisted working state changed since the caller loaded it."""


class InvestigationStateRepository(Protocol):
    async def get(self, investigation_id: UUID) -> InvestigationWorkingState | None: ...

    async def save_if_version(
        self, state: InvestigationWorkingState, *, expected_version: int | None
    ) -> InvestigationWorkingState: ...


class InvestigationContextSnapshotRepository(Protocol):
    async def store(self, canonical_json: str) -> UUID: ...

    async def load(self, snapshot_id: UUID) -> str | None: ...


class InMemoryInvestigationContextSnapshotRepository:
    """Immutable canonical context snapshots keyed by content identity."""

    def __init__(self) -> None:
        self._items: dict[UUID, str] = {}
        self._lock = asyncio.Lock()

    async def store(self, canonical_json: str) -> UUID:
        try:
            payload = json.loads(canonical_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Context snapshot must be valid canonical JSON") from exc
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload).decode() != canonical_json
        ):
            raise ValueError("Context snapshot must use canonical JSON encoding")
        digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        snapshot_id = uuid5(_CONTEXT_SNAPSHOT_NAMESPACE, digest)
        async with self._lock:
            existing = self._items.get(snapshot_id)
            if existing is not None and existing != canonical_json:
                raise ValueError("Immutable context snapshot id cannot be reused")
            self._items[snapshot_id] = canonical_json
        return snapshot_id

    async def load(self, snapshot_id: UUID) -> str | None:
        return self._items.get(snapshot_id)


class InMemoryInvestigationStateRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationWorkingState] = {}
        self._lock = asyncio.Lock()

    async def get(self, investigation_id: UUID) -> InvestigationWorkingState | None:
        return self._items.get(investigation_id)

    async def save_if_version(
        self, state: InvestigationWorkingState, *, expected_version: int | None
    ) -> InvestigationWorkingState:
        async with self._lock:
            current = self._items.get(state.investigation_id)
            if current == state:
                return current
            if current is None:
                if expected_version is not None or state.version != 1:
                    raise InvestigationStateVersionConflict("First state version must be v1")
            elif current.version != expected_version or state.version != current.version + 1:
                raise InvestigationStateVersionConflict("Investigation state version changed")
            self._items[state.investigation_id] = state
            return state


__all__ = [
    "InMemoryInvestigationContextSnapshotRepository",
    "InMemoryInvestigationStateRepository",
    "InvestigationContextSnapshotRepository",
    "InvestigationStateRepository",
    "InvestigationStateVersionConflict",
]
