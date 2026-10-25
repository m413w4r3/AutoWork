"""SQL persistence for versioned OSINT_OPEN policy and lineage records."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.investigation import BudgetDimension, DisclosureBounds, TLPLevel
from cti_app.domain.osint_open import (
    OsintCapabilities,
    OsintInheritance,
    OsintInheritanceStatus,
    OsintPolicyEvent,
    OsintPolicyEventType,
    OsintPolicyMode,
    OsintPolicyStatus,
    OsintPolicyVersion,
    OsintScopeKind,
)
from cti_app.infrastructure.database.models.investigation import ToolInvocationRow
from cti_app.infrastructure.database.models.osint_open_policy import (
    OsintOpenInheritanceRow,
    OsintOpenPolicyEventRow,
    OsintOpenPolicyRow,
)


class SqlAlchemyOsintOpenPolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_for_scope(
        self, scope_kind: OsintScopeKind, scope_ref: str, *, for_update: bool = False
    ) -> OsintPolicyVersion | None:
        statement = (
            select(OsintOpenPolicyRow)
            .where(
                OsintOpenPolicyRow.scope_kind == scope_kind.value,
                OsintOpenPolicyRow.scope_ref == scope_ref,
            )
            .order_by(OsintOpenPolicyRow.version.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return await self._policy_from_row(row) if row is not None else None

    async def lock_scope(self, scope_kind: OsintScopeKind, scope_ref: str) -> None:
        key = f"osint-open:{scope_kind.value}:{scope_ref}"
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:scope_key, 0))"),
            {"scope_key": key},
        )

    async def lock_scope_index(self) -> None:
        """Serialize policy-set changes against start-time matching, including empty scopes."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('osint-open-scope-index', 0))")
        )

    async def latest_for_subject(self, subject_id: UUID) -> Sequence[OsintPolicyVersion]:
        rows = await self._session.scalars(
            select(OsintOpenPolicyRow)
            .where(
                OsintOpenPolicyRow.scope_kind.in_(
                    (OsintScopeKind.INSTANCE.value, OsintScopeKind.SUBJECT_SET.value)
                )
            )
            .order_by(OsintOpenPolicyRow.policy_id, OsintOpenPolicyRow.version.desc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        latest: dict[UUID, OsintOpenPolicyRow] = {}
        for row in rows:
            latest.setdefault(row.policy_id, row)
        matched = [
            await self._policy_from_row(row)
            for row in latest.values()
            if row.scope_kind == OsintScopeKind.INSTANCE.value
            or subject_id in OsintPolicyVersion.scope_subject_ids(row.scope_ref)
        ]
        return tuple(matched)

    async def lock_investigation_start(self, investigation_id: UUID) -> None:
        """Serialize start-time pin creation without exposing a session to application code."""
        value = investigation_id.int
        high = (value >> 96) & 0xFFFFFFFF
        low = value & 0xFFFFFFFF
        high = high if high < 0x80000000 else high - 0x100000000
        low = low if low < 0x80000000 else low - 0x100000000
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key_high, :key_low)"),
            {"key_high": high, "key_low": low},
        )

    async def invocation_count(self, investigation_id: UUID) -> int:
        count = await self._session.scalar(
            select(func.count())
            .select_from(ToolInvocationRow)
            .where(ToolInvocationRow.investigation_id == investigation_id)
        )
        return int(count or 0)

    async def get_version(
        self, policy_id: UUID, version: int, *, for_update: bool = False
    ) -> OsintPolicyVersion | None:
        statement = (
            select(OsintOpenPolicyRow)
            .where(
                OsintOpenPolicyRow.policy_id == policy_id,
                OsintOpenPolicyRow.version == version,
            )
            .execution_options(populate_existing=True)
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement)
        return await self._policy_from_row(row) if row is not None else None

    async def by_correlation(
        self, correlation_id: str
    ) -> tuple[OsintPolicyVersion, OsintPolicyEvent] | None:
        event_row = await self._session.scalar(
            select(OsintOpenPolicyEventRow).where(
                OsintOpenPolicyEventRow.correlation_id == correlation_id
            )
        )
        if event_row is None:
            return None
        policy_row = await self._session.scalar(
            select(OsintOpenPolicyRow).where(
                OsintOpenPolicyRow.policy_id == event_row.policy_id,
                OsintOpenPolicyRow.version == event_row.policy_version,
            )
        )
        if policy_row is None:
            raise RuntimeError("OSINT_OPEN event has no policy version")
        return await self._policy_from_row(policy_row), _event_from_row(event_row)

    async def event_for_version(self, policy_id: UUID, version: int) -> OsintPolicyEvent | None:
        row = await self._session.scalar(
            select(OsintOpenPolicyEventRow).where(
                OsintOpenPolicyEventRow.policy_id == policy_id,
                OsintOpenPolicyEventRow.policy_version == version,
            )
        )
        return _event_from_row(row) if row is not None else None

    async def list_events(self, policy_id: UUID) -> Sequence[OsintPolicyEvent]:
        rows = await self._session.scalars(
            select(OsintOpenPolicyEventRow)
            .where(OsintOpenPolicyEventRow.policy_id == policy_id)
            .order_by(OsintOpenPolicyEventRow.policy_version)
        )
        return tuple(_event_from_row(row) for row in rows)

    async def event_by_id(self, event_id: UUID) -> OsintPolicyEvent | None:
        row = await self._session.get(OsintOpenPolicyEventRow, event_id)
        return _event_from_row(row) if row is not None else None

    async def add_version(self, policy: OsintPolicyVersion, event: OsintPolicyEvent) -> None:
        caps = policy.capabilities
        self._session.add(
            OsintOpenPolicyRow(
                policy_id=policy.policy_id,
                version=policy.version,
                scope_kind=policy.scope_kind.value,
                scope_ref=policy.scope_ref,
                mode=policy.mode.value,
                status=policy.status.value,
                allowed_providers=list(caps.allowed_providers),
                budget_limits={key.value: value for key, value in caps.budget_limits.items()},
                acquire_hits_allowed=caps.acquire_hits_allowed,
                disclosure_tlp=(caps.disclosure.tlp or TLPLevel.CLEAR).value,
                disclosure_do_not_submit=caps.disclosure.do_not_submit,
                disclosure_external_llm_allowed=caps.disclosure.external_llm_allowed,
                max_effect_claims_per_investigation=caps.max_effect_claims_per_investigation,
                actor_id=policy.actor_id,
                reason=policy.reason,
                correlation_id=policy.correlation_id,
                activation_correlation_id=policy.activation_correlation_id,
                previous_version=policy.previous_version,
                created_at=policy.created_at,
            )
        )
        await self._session.flush()
        self._session.add(
            OsintOpenPolicyEventRow(
                id=event.id,
                policy_id=event.policy_id,
                policy_version=event.policy_version,
                event_type=event.event_type.value,
                actor_id=event.actor_id,
                reason=event.reason,
                correlation_id=event.correlation_id,
                payload_sha256=event.payload_sha256,
                previous_version=event.previous_version,
                created_at=event.created_at,
            )
        )
        await self._session.flush()

    async def get_inheritance(
        self, investigation_id: UUID, *, for_update: bool = False
    ) -> OsintInheritance | None:
        statement = select(OsintOpenInheritanceRow).where(
            OsintOpenInheritanceRow.investigation_id == investigation_id
        )
        if for_update:
            statement = statement.with_for_update()
        row = await self._session.scalar(statement.execution_options(populate_existing=True))
        return _inheritance_from_row(row) if row is not None else None

    async def add_inheritance(self, inheritance: OsintInheritance) -> OsintInheritance:
        row = OsintOpenInheritanceRow(
            investigation_id=inheritance.investigation_id,
            subject_id=inheritance.subject_id,
            policy_id=inheritance.policy_id,
            policy_version=inheritance.policy_version,
            policy_event_id=inheritance.policy_event_id,
            correlation_id=inheritance.correlation_id,
            activation_correlation_id=inheritance.activation_correlation_id,
            status=inheritance.status.value,
            created_at=inheritance.created_at,
            updated_at=inheritance.updated_at,
        )
        self._session.add(row)
        await self._session.flush()
        return inheritance

    async def active_inheritances_for_policy(self, policy_id: UUID) -> Sequence[OsintInheritance]:
        rows = await self._session.scalars(
            select(OsintOpenInheritanceRow)
            .where(
                OsintOpenInheritanceRow.policy_id == policy_id,
                OsintOpenInheritanceRow.status == OsintInheritanceStatus.ACTIVE.value,
            )
            .order_by(OsintOpenInheritanceRow.investigation_id)
            .with_for_update()
        )
        return tuple(_inheritance_from_row(row) for row in rows)

    async def set_inheritance_status(
        self,
        inheritance: OsintInheritance,
        status: OsintInheritanceStatus,
    ) -> OsintInheritance:
        now = datetime.now(UTC)
        result = await self._session.execute(
            update(OsintOpenInheritanceRow)
            .where(
                OsintOpenInheritanceRow.investigation_id == inheritance.investigation_id,
                OsintOpenInheritanceRow.policy_id == inheritance.policy_id,
                OsintOpenInheritanceRow.policy_version == inheritance.policy_version,
                OsintOpenInheritanceRow.status == OsintInheritanceStatus.ACTIVE.value,
            )
            .values(status=status.value, updated_at=now)
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            current = await self.get_inheritance(inheritance.investigation_id)
            if current is not None and current.status is status:
                return current
            raise RuntimeError("OSINT_OPEN inheritance status changed concurrently")
        await self._session.flush()
        return OsintInheritance(
            investigation_id=inheritance.investigation_id,
            subject_id=inheritance.subject_id,
            policy_id=inheritance.policy_id,
            policy_version=inheritance.policy_version,
            policy_event_id=inheritance.policy_event_id,
            correlation_id=inheritance.correlation_id,
            activation_correlation_id=inheritance.activation_correlation_id,
            status=status,
            created_at=inheritance.created_at,
            updated_at=now,
        )

    async def set_inheritance_status_by_investigation(
        self,
        investigation_id: UUID,
        status: OsintInheritanceStatus,
    ) -> OsintInheritance | None:
        if status is not OsintInheritanceStatus.SUPERSEDED:
            raise ValueError("Manual G2 actions may only supersede a policy pin")
        inheritance = await self.get_inheritance(investigation_id, for_update=True)
        if inheritance is None or inheritance.status is status:
            return inheritance
        result = await self._session.execute(
            update(OsintOpenInheritanceRow)
            .where(
                OsintOpenInheritanceRow.investigation_id == investigation_id,
                OsintOpenInheritanceRow.status.in_(
                    (
                        OsintInheritanceStatus.ACTIVE.value,
                        OsintInheritanceStatus.REVOKED.value,
                    )
                ),
            )
            .values(status=status.value, updated_at=datetime.now(UTC))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise RuntimeError("OSINT_OPEN inheritance status changed concurrently")
        await self._session.flush()
        return OsintInheritance(
            investigation_id=inheritance.investigation_id,
            subject_id=inheritance.subject_id,
            policy_id=inheritance.policy_id,
            policy_version=inheritance.policy_version,
            policy_event_id=inheritance.policy_event_id,
            correlation_id=inheritance.correlation_id,
            activation_correlation_id=inheritance.activation_correlation_id,
            status=status,
            created_at=inheritance.created_at,
            updated_at=datetime.now(UTC),
        )

    async def _policy_from_row(self, row: OsintOpenPolicyRow) -> OsintPolicyVersion:
        event_row = await self._session.scalar(
            select(OsintOpenPolicyEventRow).where(
                OsintOpenPolicyEventRow.policy_id == row.policy_id,
                OsintOpenPolicyEventRow.policy_version == row.version,
            )
        )
        if event_row is None:
            raise RuntimeError("OSINT_OPEN policy version has no audit event")
        return OsintPolicyVersion(
            policy_id=row.policy_id,
            version=row.version,
            scope_kind=OsintScopeKind(row.scope_kind),
            scope_ref=row.scope_ref,
            mode=OsintPolicyMode(row.mode),
            status=OsintPolicyStatus(row.status),
            capabilities=OsintCapabilities(
                allowed_providers=tuple(row.allowed_providers),
                budget_limits={
                    BudgetDimension(key): int(value) for key, value in row.budget_limits.items()
                },
                acquire_hits_allowed=row.acquire_hits_allowed,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel(row.disclosure_tlp),
                    do_not_submit=row.disclosure_do_not_submit,
                    external_llm_allowed=row.disclosure_external_llm_allowed,
                ),
                max_effect_claims_per_investigation=row.max_effect_claims_per_investigation,
            ),
            actor_id=row.actor_id,
            reason=row.reason,
            correlation_id=row.correlation_id,
            activation_correlation_id=row.activation_correlation_id,
            event_id=event_row.id,
            created_at=row.created_at,
            previous_version=row.previous_version,
        )


def _event_from_row(row: OsintOpenPolicyEventRow) -> OsintPolicyEvent:
    return OsintPolicyEvent(
        id=row.id,
        policy_id=row.policy_id,
        policy_version=row.policy_version,
        event_type=OsintPolicyEventType(row.event_type),
        actor_id=row.actor_id,
        reason=row.reason,
        correlation_id=row.correlation_id,
        payload_sha256=row.payload_sha256,
        previous_version=row.previous_version,
        created_at=row.created_at,
    )


def _inheritance_from_row(row: OsintOpenInheritanceRow) -> OsintInheritance:
    return OsintInheritance(
        investigation_id=row.investigation_id,
        subject_id=row.subject_id,
        policy_id=row.policy_id,
        policy_version=row.policy_version,
        policy_event_id=row.policy_event_id,
        correlation_id=row.correlation_id,
        activation_correlation_id=row.activation_correlation_id,
        status=OsintInheritanceStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["SqlAlchemyOsintOpenPolicyRepository"]
