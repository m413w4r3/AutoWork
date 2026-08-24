from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.model_conversations import (
    ConversationLifecycle,
    ConversationLifecycleStatus,
    ConversationPolicy,
    ConversationPurpose,
    ConversationReleaseOutcome,
    ConversationStatus,
    ConversationTransport,
    ConversationTurnStatus,
    ModelConversation,
    ModelConversationTurn,
)
from cti_app.domain.model_runs import ModelProvider
from cti_app.infrastructure.database.models.schema import (
    ConversationLifecycleRow,
    ModelConversationRow,
    ModelConversationTurnRow,
)


class SqlAlchemyModelConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, conversation: ModelConversation) -> None:
        self._session.add(ModelConversationRow(**_model_conversation_values(conversation)))
        await self._session.flush()

    async def get(self, conversation_id: UUID) -> ModelConversation | None:
        row = await self._session.get(ModelConversationRow, conversation_id)
        return _model_conversation_from_row(row) if row else None

    async def get_for_update(self, conversation_id: UUID) -> ModelConversation | None:
        row = await self._session.scalar(
            select(ModelConversationRow)
            .where(ModelConversationRow.id == conversation_id)
            .with_for_update()
        )
        return _model_conversation_from_row(row) if row else None

    async def save(self, conversation: ModelConversation) -> None:
        values = _model_conversation_values(conversation)
        values.pop("id")
        result = await self._session.execute(
            update(ModelConversationRow)
            .where(
                ModelConversationRow.id == conversation.id,
                ModelConversationRow.version == conversation.version - 1,
            )
            .values(**values)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise LookupError(f"Conversation {conversation.id} absente ou version obsolète")

    async def list(
        self,
        *,
        edition_id: UUID | None,
        subject_id: UUID | None,
        purpose: ConversationPurpose | None,
        status: ConversationStatus | None,
        provider: ModelProvider | None,
    ) -> Sequence[ModelConversation]:
        filters = []
        if edition_id is not None:
            filters.append(ModelConversationRow.edition_id == edition_id)
        if subject_id is not None:
            filters.append(ModelConversationRow.subject_id == subject_id)
        if purpose is not None:
            filters.append(ModelConversationRow.purpose == purpose.value)
        if status is not None:
            filters.append(ModelConversationRow.status == status.value)
        if provider is not None:
            filters.append(ModelConversationRow.provider == provider.value)
        rows = await self._session.scalars(
            select(ModelConversationRow)
            .where(*filters)
            .order_by(ModelConversationRow.updated_at.desc(), ModelConversationRow.id)
        )
        return [_model_conversation_from_row(row) for row in rows]


class SqlAlchemyModelConversationTurnRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, turn: ModelConversationTurn) -> None:
        self._session.add(ModelConversationTurnRow(**_model_conversation_turn_values(turn)))
        await self._session.flush()

    async def get(self, turn_id: UUID) -> ModelConversationTurn | None:
        row = await self._session.get(ModelConversationTurnRow, turn_id)
        return _model_conversation_turn_from_row(row) if row else None

    async def get_by_idempotency_key(self, key: str) -> ModelConversationTurn | None:
        row = await self._session.scalar(
            select(ModelConversationTurnRow).where(ModelConversationTurnRow.idempotency_key == key)
        )
        return _model_conversation_turn_from_row(row) if row else None

    async def list_for_conversation(self, conversation_id: UUID) -> Sequence[ModelConversationTurn]:
        rows = await self._session.scalars(
            select(ModelConversationTurnRow)
            .where(ModelConversationTurnRow.conversation_id == conversation_id)
            .order_by(ModelConversationTurnRow.sequence)
        )
        return [_model_conversation_turn_from_row(row) for row in rows]

    async def save(self, turn: ModelConversationTurn) -> None:
        row = await self._session.get(ModelConversationTurnRow, turn.id)
        if row is None:
            raise LookupError(turn.id)
        for name, value in _model_conversation_turn_values(turn).items():
            setattr(row, name, value)
        await self._session.flush()


class SqlAlchemyConversationLifecycleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, lifecycle: ConversationLifecycle) -> None:
        self._session.add(
            ConversationLifecycleRow(**_conversation_lifecycle_values(lifecycle))
        )
        await self._session.flush()

    async def get(self, lifecycle_id: UUID) -> ConversationLifecycle | None:
        row = await self._session.get(ConversationLifecycleRow, lifecycle_id)
        return _conversation_lifecycle_from_row(row) if row else None

    async def get_by_conversation_id(self, conversation_id: UUID) -> ConversationLifecycle | None:
        row = await self._session.scalar(
            select(ConversationLifecycleRow).where(
                ConversationLifecycleRow.conversation_id == conversation_id
            )
        )
        return _conversation_lifecycle_from_row(row) if row else None

    async def save(self, lifecycle: ConversationLifecycle) -> None:
        values = _conversation_lifecycle_values(lifecycle)
        values.pop("id")
        result = await self._session.execute(
            update(ConversationLifecycleRow)
            .where(
                ConversationLifecycleRow.id == lifecycle.id,
                ConversationLifecycleRow.version == lifecycle.version - 1,
            )
            .values(**values)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise LookupError(f"Conversation lifecycle {lifecycle.id} not found or stale version")

    async def list_delete_pending(self) -> Sequence[ConversationLifecycle]:
        """List all lifecycles waiting for cleanup."""
        rows = await self._session.scalars(
            select(ConversationLifecycleRow)
            .where(
                ConversationLifecycleRow.status
                == ConversationLifecycleStatus.DELETE_PENDING.value
            )
            .order_by(ConversationLifecycleRow.created_at)
        )
        return [_conversation_lifecycle_from_row(row) for row in rows]

    async def list_cleanup_failed(self) -> Sequence[ConversationLifecycle]:
        """List all lifecycles with failed cleanup attempts."""
        rows = await self._session.scalars(
            select(ConversationLifecycleRow)
            .where(
                ConversationLifecycleRow.status
                == ConversationLifecycleStatus.CLEANUP_FAILED.value
            )
            .order_by(ConversationLifecycleRow.last_cleanup_attempt_at)
        )
        return [_conversation_lifecycle_from_row(row) for row in rows]


def _model_conversation_values(conversation: ModelConversation) -> dict[str, object]:
    return {
        "id": conversation.id,
        "provider": conversation.provider.value,
        "transport": conversation.transport.value,
        "purpose": conversation.purpose.value,
        "edition_id": conversation.edition_id,
        "subject_id": conversation.subject_id,
        "title": conversation.title,
        "status": conversation.status.value,
        "external_id": conversation.external_id,
        "external_locator": conversation.external_locator,
        "expected_profile": conversation.expected_profile,
        "requested_model": conversation.requested_model,
        "head_turn_id": conversation.head_turn_id,
        "turn_count": conversation.turn_count,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
        "last_used_at": conversation.last_used_at,
        "version": conversation.version,
    }


def _model_conversation_from_row(row: ModelConversationRow) -> ModelConversation:
    return ModelConversation(
        id=row.id,
        provider=ModelProvider(row.provider),
        transport=ConversationTransport(row.transport),
        purpose=ConversationPurpose(row.purpose),
        edition_id=row.edition_id,
        subject_id=row.subject_id,
        title=row.title,
        status=ConversationStatus(row.status),
        external_id=row.external_id,
        external_locator=row.external_locator,
        expected_profile=row.expected_profile,
        requested_model=row.requested_model,
        head_turn_id=row.head_turn_id,
        turn_count=row.turn_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_used_at=row.last_used_at,
        version=row.version,
    )


def _model_conversation_turn_values(turn: ModelConversationTurn) -> dict[str, object]:
    return {
        "id": turn.id,
        "conversation_id": turn.conversation_id,
        "sequence": turn.sequence,
        "parent_turn_id": turn.parent_turn_id,
        "model_run_id": turn.model_run_id,
        "input_blob_reference": turn.input_blob_reference,
        "input_sha256": turn.input_sha256,
        "output_blob_reference": turn.output_blob_reference,
        "output_sha256": turn.output_sha256,
        "status": turn.status.value,
        "external_turn_id": turn.external_turn_id,
        "idempotency_key": turn.idempotency_key,
        "correlation_id": turn.correlation_id,
        "error_code": turn.error_code,
        "error_message": turn.error_message,
        "error_details": turn.error_details,
        "created_at": turn.created_at,
        "started_at": turn.started_at,
        "finished_at": turn.finished_at,
    }


def _model_conversation_turn_from_row(
    row: ModelConversationTurnRow,
) -> ModelConversationTurn:
    return ModelConversationTurn(
        id=row.id,
        conversation_id=row.conversation_id,
        sequence=row.sequence,
        parent_turn_id=row.parent_turn_id,
        model_run_id=row.model_run_id,
        input_blob_reference=row.input_blob_reference,
        input_sha256=row.input_sha256,
        output_blob_reference=row.output_blob_reference,
        output_sha256=row.output_sha256,
        status=ConversationTurnStatus(row.status),
        external_turn_id=row.external_turn_id,
        idempotency_key=row.idempotency_key,
        correlation_id=row.correlation_id,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _conversation_lifecycle_values(lifecycle: ConversationLifecycle) -> dict[str, object]:
    return {
        "id": lifecycle.id,
        "conversation_id": lifecycle.id,  # Use lifecycle ID as conversation_id for now
        "policy": lifecycle.policy.value,
        "status": lifecycle.status.value,
        "release_outcome": lifecycle.release_outcome.value if lifecycle.release_outcome else None,
        "released_at": lifecycle.released_at,
        "deleted_at": lifecycle.deleted_at,
        "cleanup_attempt_count": lifecycle.cleanup_attempt_count,
        "last_cleanup_attempt_at": lifecycle.last_cleanup_attempt_at,
        "last_cleanup_error_code": lifecycle.last_cleanup_error_code,
        "created_at": lifecycle.created_at,
        "updated_at": lifecycle.updated_at,
        "version": lifecycle.version,
    }


def _conversation_lifecycle_from_row(row: ConversationLifecycleRow) -> ConversationLifecycle:
    return ConversationLifecycle(
        id=row.id,
        policy=ConversationPolicy(row.policy),
        status=ConversationLifecycleStatus(row.status),
        release_outcome=(
            ConversationReleaseOutcome(row.release_outcome) if row.release_outcome else None
        ),
        released_at=row.released_at,
        deleted_at=row.deleted_at,
        cleanup_attempt_count=row.cleanup_attempt_count,
        last_cleanup_attempt_at=row.last_cleanup_attempt_at,
        last_cleanup_error_code=row.last_cleanup_error_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )
