"""In-memory persistence for real `ModelConversationService` tests.

`ModelConversationService.add_turn` needs a unit of work exposing
`model_conversations`, `model_conversation_turns`, `model_runs` and `blobs`
(the last via `BlobCatalogService`). No existing test double combines all
four, so this module does — reusing `InMemoryModelRunRepository` from
`tests.model_support` for the ModelRun side, since that is exactly the
component P23.6 exercises against a real `ModelGateway`.
"""

from __future__ import annotations

from copy import deepcopy
from types import TracebackType
from typing import Self
from uuid import UUID

from cti_app.domain.blobs import BlobRecord
from cti_app.domain.model_conversations import ModelConversation, ModelConversationTurn
from cti_app.domain.model_runs import ModelRun
from tests.model_support import InMemoryModelRunRepository


class InMemoryBlobRepository:
    def __init__(self, blobs: dict[UUID, BlobRecord]) -> None:
        self._blobs = blobs

    async def add(self, blob: BlobRecord) -> None:
        self._blobs[blob.id] = blob

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return self._blobs.get(blob_id)

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        return next(
            (
                item
                for item in self._blobs.values()
                if item.descriptor.logical_bucket == logical_bucket
                and item.descriptor.sha256 == sha256
            ),
            None,
        )

    async def count_references(self, blob_id: UUID) -> int:
        del blob_id
        return 0

    async def delete(self, blob_id: UUID) -> None:
        self._blobs.pop(blob_id, None)


class InMemoryModelConversationRepository:
    def __init__(self, state: dict[UUID, ModelConversation]) -> None:
        self._state = state

    async def add(self, conversation: ModelConversation) -> None:
        self._state[conversation.id] = deepcopy(conversation)

    async def get(self, conversation_id: UUID) -> ModelConversation | None:
        conversation = self._state.get(conversation_id)
        return deepcopy(conversation) if conversation else None

    async def get_for_update(self, conversation_id: UUID) -> ModelConversation | None:
        return await self.get(conversation_id)

    async def save(self, conversation: ModelConversation) -> None:
        if conversation.id not in self._state:
            raise LookupError(conversation.id)
        self._state[conversation.id] = deepcopy(conversation)

    async def list(self, **_: object) -> list[ModelConversation]:
        return [deepcopy(item) for item in self._state.values()]


class InMemoryModelConversationTurnRepository:
    def __init__(self, state: dict[UUID, ModelConversationTurn]) -> None:
        self._state = state

    async def add(self, turn: ModelConversationTurn) -> None:
        self._state[turn.id] = deepcopy(turn)

    async def get(self, turn_id: UUID) -> ModelConversationTurn | None:
        turn = self._state.get(turn_id)
        return deepcopy(turn) if turn else None

    async def get_by_idempotency_key(self, key: str) -> ModelConversationTurn | None:
        return next(
            (deepcopy(item) for item in self._state.values() if item.idempotency_key == key),
            None,
        )

    async def get_by_model_run_id(self, model_run_id: UUID) -> ModelConversationTurn | None:
        return next(
            (deepcopy(item) for item in self._state.values() if item.model_run_id == model_run_id),
            None,
        )

    async def list_for_conversation(self, conversation_id: UUID) -> list[ModelConversationTurn]:
        return [
            deepcopy(item)
            for item in self._state.values()
            if item.conversation_id == conversation_id
        ]

    async def save(self, turn: ModelConversationTurn) -> None:
        if turn.id not in self._state:
            raise LookupError(turn.id)
        self._state[turn.id] = deepcopy(turn)


class _KnownIdRepository:
    """Stands in for `subjects`/`editions`: `ModelConversationService.create`
    only checks these exist when an id is given, and never reads their
    fields."""

    def __init__(self, known_ids: set[UUID]) -> None:
        self._known_ids = known_ids

    async def get(self, entity_id: UUID) -> object | None:
        return object() if entity_id in self._known_ids else None


class InMemoryConversationUnitOfWork:
    def __init__(self, factory: InMemoryConversationUnitOfWorkFactory) -> None:
        self.blobs = InMemoryBlobRepository(factory.blobs)
        self.model_runs = InMemoryModelRunRepository(factory.model_runs)
        self.model_conversations = InMemoryModelConversationRepository(factory.conversations)
        self.model_conversation_turns = InMemoryModelConversationTurnRepository(factory.turns)
        self.subjects = _KnownIdRepository(factory.known_subject_ids)
        self.editions = _KnownIdRepository(factory.known_edition_ids)

    async def __aenter__(self) -> Self:
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


class InMemoryConversationUnitOfWorkFactory:
    def __init__(
        self,
        *,
        known_subject_ids: set[UUID] | None = None,
        known_edition_ids: set[UUID] | None = None,
    ) -> None:
        self.blobs: dict[UUID, BlobRecord] = {}
        self.model_runs: dict[UUID, ModelRun] = {}
        self.conversations: dict[UUID, ModelConversation] = {}
        self.turns: dict[UUID, ModelConversationTurn] = {}
        self.known_subject_ids = known_subject_ids or set()
        self.known_edition_ids = known_edition_ids or set()

    def __call__(self) -> InMemoryConversationUnitOfWork:
        return InMemoryConversationUnitOfWork(self)
