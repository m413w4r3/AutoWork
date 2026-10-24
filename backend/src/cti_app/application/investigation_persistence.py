"""Async repository ports and in-memory fakes for investigation contracts.

The A2 persistence lot should adapt these ports to its Unit of Work. This
module intentionally does not modify or depend on application/persistence.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from cti_app.application.context_pagination import (
    KeysetPageV1,
    decode_keyset_cursor,
    make_keyset_page,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1
from cti_app.domain.investigation import (
    BudgetReservationStatus,
    DetectionCandidateVersion,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    SampleAssociation,
    ToolInvocation,
)
from cti_app.domain.investigation_experiment import InvestigationExperimentV1


class VersionConflictError(RuntimeError):
    """Raised when a compare-and-swap append does not match the current version."""


class ToolInvocationRepository(Protocol):
    async def get(self, invocation_id: UUID) -> ToolInvocation | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[ToolInvocation, ...]: ...

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "tool_invocations",
    ) -> KeysetPageV1[ToolInvocation]: ...

    async def get_by_operation_key(self, operation_key: str) -> ToolInvocation | None: ...

    async def add_if_absent(self, invocation: ToolInvocation) -> ToolInvocation: ...

    async def update_if_version(
        self, invocation: ToolInvocation, *, expected_version: int
    ) -> ToolInvocation: ...


class EvidenceObservationRepository(Protocol):
    async def get(self, observation_id: UUID) -> EvidenceObservation | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]: ...

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "evidence_observations",
    ) -> KeysetPageV1[EvidenceObservation]: ...

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation: ...


class FindingRepository(Protocol):
    async def get(self, finding_id: UUID) -> InvestigationFinding | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationFinding, ...]: ...

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "findings",
        latest_only: bool = False,
    ) -> KeysetPageV1[InvestigationFinding]: ...

    async def append_version(
        self, finding: InvestigationFinding, *, expected_previous_id: UUID | None
    ) -> InvestigationFinding: ...


class InvestigationExperimentRepository(Protocol):
    async def version(self, investigation_id: UUID) -> int: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationExperimentV1, ...]: ...

    async def append_if_version(
        self, experiment: InvestigationExperimentV1, *, expected_version: int
    ) -> InvestigationExperimentV1: ...


class SampleAssociationRepository(Protocol):
    async def get(self, association_id: UUID) -> SampleAssociation | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[SampleAssociation, ...]: ...

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation: ...


class DetectionCandidateRepository(Protocol):
    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None: ...

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]: ...

    async def list_for_candidate(
        self, candidate_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]: ...

    async def append_version(
        self, version: DetectionCandidateVersion, *, expected_previous_id: UUID | None
    ) -> DetectionCandidateVersion: ...


class AnalystOpinionRepository(Protocol):
    async def get(
        self, opinion_id: UUID, *, version: int | None = None
    ) -> AnalystOpinionV1 | None: ...

    async def append_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1: ...


class InvestigationEnvelopeRepository(Protocol):
    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None: ...

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope: ...


@dataclass(frozen=True, slots=True)
class InvestigationBudgetSnapshot:
    investigation_id: UUID
    version: int
    budget: InvestigationBudget

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if not isinstance(self.budget, InvestigationBudget):
            raise TypeError("budget must be an InvestigationBudget")
        object.__setattr__(self, "budget", _copy_budget(self.budget))


class InvestigationBudgetRepository(Protocol):
    async def get(self, investigation_id: UUID) -> InvestigationBudgetSnapshot | None: ...

    async def save_if_version(
        self,
        snapshot: InvestigationBudgetSnapshot,
        *,
        expected_version: int | None,
    ) -> InvestigationBudgetSnapshot: ...


def _copy_budget(budget: InvestigationBudget) -> InvestigationBudget:
    copied = InvestigationBudget(limits=dict(budget.limits))
    for reservation in budget.reservations:
        copied.reserve(reservation.operation_key, reservation.amounts)
        if reservation.status is BudgetReservationStatus.COMMITTED:
            copied.commit(reservation.operation_key, reservation.committed_amounts)
        elif reservation.status is BudgetReservationStatus.RELEASED:
            copied.release(reservation.operation_key)
    return copied


def _memory_keyset_page(
    items: tuple[object, ...],
    *,
    section: str,
    investigation_id: UUID,
    cursor: str | None,
    limit: int,
    descending: bool,
    position: object,
    latest_only: bool = False,
) -> KeysetPageV1:
    """Page a materialized in-memory snapshot; this does not bound source memory."""
    if not 1 <= limit <= 1_000:
        raise ValueError("Context page limit must be between 1 and 1000")
    position_fn = position  # keep the generic callback local for type-checkers
    ordered = sorted(items, key=position_fn, reverse=descending)  # type: ignore[arg-type]
    if cursor is not None:
        decoded = decode_keyset_cursor(
            cursor,
            section=section,
            investigation_id=investigation_id,
            descending=descending,
        )
        try:
            sort_key = datetime.fromisoformat(decoded["sort_key"])
            item_id = UUID(decoded["item_id"])
        except (TypeError, ValueError):
            raise ValueError("Context keyset cursor is invalid") from None
        pivot = (sort_key, item_id)
        ordered = [
            item
            for item in ordered
            if (position_fn(item) < pivot if descending else position_fn(item) > pivot)  # type: ignore[operator]
        ]
    return make_keyset_page(
        section=section,
        investigation_id=investigation_id,
        items=tuple(ordered[: limit + 1]),
        total=len(items),
        limit=limit,
        cursor=cursor,
        position=position_fn,
        descending=descending,
        source_bounded=False,
        unbounded_reason="In-memory repository materializes the full collection",
    )


class InMemoryToolInvocationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, ToolInvocation] = {}
        self._by_operation: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    async def get(self, invocation_id: UUID) -> ToolInvocation | None:
        return self._items.get(invocation_id)

    async def get_by_operation_key(self, operation_key: str) -> ToolInvocation | None:
        invocation_id = self._by_operation.get(operation_key)
        return self._items.get(invocation_id) if invocation_id is not None else None

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[ToolInvocation, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                ),
                key=lambda item: (item.created_at, item.id.hex),
            )
        )

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "tool_invocations",
    ) -> KeysetPageV1[ToolInvocation]:
        items = await self.list_for_investigation(investigation_id)
        return _memory_keyset_page(
            items,
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            limit=limit,
            descending=descending,
            position=lambda item: (item.created_at, item.id),
        )

    async def add_if_absent(self, invocation: ToolInvocation) -> ToolInvocation:
        async with self._lock:
            existing_id = self._by_operation.get(invocation.operation_key)
            if existing_id is not None:
                existing = self._items[existing_id]
                if (
                    existing.investigation_id != invocation.investigation_id
                    or existing.tool_name != invocation.tool_name
                    or existing.schema_version != invocation.schema_version
                    or existing.normalized_parameters != invocation.normalized_parameters
                ):
                    raise ValueError(
                        "An operation key cannot identify different invocation content"
                    )
                return existing
            if invocation.id in self._items:
                raise ValueError("Invocation id is already used")
            self._items[invocation.id] = invocation
            self._by_operation[invocation.operation_key] = invocation.id
            return invocation

    async def update_if_version(
        self, invocation: ToolInvocation, *, expected_version: int
    ) -> ToolInvocation:
        async with self._lock:
            current = self._items.get(invocation.id)
            if current == invocation:
                return invocation
            if current is None or current.version != expected_version:
                raise VersionConflictError("Tool invocation version changed")
            if (
                invocation.operation_key != current.operation_key
                or invocation.investigation_id != current.investigation_id
                or invocation.tool_name != current.tool_name
                or invocation.schema_version != current.schema_version
                or invocation.normalized_parameters != current.normalized_parameters
                or invocation.purpose != current.purpose
                or invocation.version != expected_version + 1
            ):
                raise ValueError(
                    "Tool invocation updates must preserve identity and advance one version"
                )
            self._items[invocation.id] = invocation
            return invocation


class InMemoryEvidenceObservationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, EvidenceObservation] = {}
        self._lock = asyncio.Lock()

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return self._items.get(observation_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item for item in self._items.values() if item.investigation_id == investigation_id
        )

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "evidence_observations",
    ) -> KeysetPageV1[EvidenceObservation]:
        items = await self.list_for_investigation(investigation_id)
        return _memory_keyset_page(
            items,
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            limit=limit,
            descending=descending,
            position=lambda item: (item.observed_at, item.id),
        )

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        async with self._lock:
            existing = self._items.get(observation.id)
            if existing is not None:
                if existing != observation:
                    raise ValueError(
                        "An immutable observation id cannot be reused with different content"
                    )
                return existing
            self._items[observation.id] = observation
            return observation


class InMemoryFindingRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationFinding] = {}
        self._lock = asyncio.Lock()

    async def get(self, finding_id: UUID) -> InvestigationFinding | None:
        return self._items.get(finding_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationFinding, ...]:
        return tuple(
            item for item in self._items.values() if item.investigation_id == investigation_id
        )

    async def page_for_investigation(
        self,
        investigation_id: UUID,
        *,
        cursor: str | None,
        limit: int,
        descending: bool = False,
        section: str = "findings",
        latest_only: bool = False,
    ) -> KeysetPageV1[InvestigationFinding]:
        items = await self.list_for_investigation(investigation_id)
        if latest_only:
            superseded = {item.supersedes for item in items if item.supersedes is not None}
            items = tuple(item for item in items if item.id not in superseded)
        return _memory_keyset_page(
            items,
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            limit=limit,
            descending=descending,
            position=lambda item: (item.created_at, item.id),
        )

    async def append_version(
        self, finding: InvestigationFinding, *, expected_previous_id: UUID | None
    ) -> InvestigationFinding:
        async with self._lock:
            if finding.id in self._items:
                existing = self._items[finding.id]
                if existing == finding:
                    return existing
                raise ValueError("Finding version id is already used")
            if finding.supersedes != expected_previous_id:
                raise ValueError("Finding supersedes must match the expected prior version")
            if expected_previous_id is None:
                if finding.version != 1:
                    raise VersionConflictError("A first finding version must be v1")
            else:
                previous = self._items.get(expected_previous_id)
                if previous is None or previous.version + 1 != finding.version:
                    raise VersionConflictError("Finding prior version changed")
                if (
                    previous.investigation_id != finding.investigation_id
                    or previous.kind is not finding.kind
                ):
                    raise ValueError("Finding versions must retain investigation and kind")
                if any(item.supersedes == expected_previous_id for item in self._items.values()):
                    raise VersionConflictError("Finding already has a newer version")
            self._items[finding.id] = finding
            return finding


class InMemorySampleAssociationRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, SampleAssociation] = {}
        self._lock = asyncio.Lock()

    async def get(self, association_id: UUID) -> SampleAssociation | None:
        return self._items.get(association_id)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            item for item in self._items.values() if item.investigation_id == investigation_id
        )

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation:
        async with self._lock:
            current = self._items.get(association.id)
            if current == association:
                return association
            if current is None:
                if expected_version != 0 or association.version != 1:
                    raise VersionConflictError("A first sample association version must be v1")
            elif current.version != expected_version or association.version != expected_version + 1:
                raise VersionConflictError("Sample association version changed")
            elif (
                current.investigation_id != association.investigation_id
                or current.sample_sha256 != association.sample_sha256
                or current.origin is not association.origin
            ):
                raise ValueError("Sample association versions must retain sample identity")
            self._items[association.id] = association
            return association


class InMemoryDetectionCandidateRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, DetectionCandidateVersion] = {}
        self._lock = asyncio.Lock()

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        return self._items.get(version_id)

    async def list_for_candidate(self, candidate_id: UUID) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(
            sorted(
                (item for item in self._items.values() if item.candidate_id == candidate_id),
                key=lambda item: (item.version, item.id.hex),
            )
        )

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._items.values()
                    if item.investigation_id == investigation_id
                ),
                key=lambda item: (item.candidate_id.hex, item.version, item.id.hex),
            )
        )

    async def append_version(
        self, version: DetectionCandidateVersion, *, expected_previous_id: UUID | None
    ) -> DetectionCandidateVersion:
        async with self._lock:
            if version.id in self._items:
                existing = self._items[version.id]
                if existing == version:
                    return existing
                raise ValueError("Detection version id is already used")
            if version.parent_version_id != expected_previous_id:
                raise ValueError("Detection parent must match the expected prior version")
            if expected_previous_id is None:
                if version.version != 1:
                    raise VersionConflictError("A first detection candidate must be v1")
            else:
                previous = self._items.get(expected_previous_id)
                if previous is None or previous.version + 1 != version.version:
                    raise VersionConflictError("Detection candidate prior version changed")
                if previous.candidate_id != version.candidate_id:
                    raise ValueError("Detection versions must share a candidate identity")
                if previous.investigation_id != version.investigation_id:
                    raise ValueError("Detection versions must retain investigation identity")
                if any(
                    item.parent_version_id == expected_previous_id for item in self._items.values()
                ):
                    raise VersionConflictError("Detection candidate already has a newer version")
            self._items[version.id] = version
            return version


class InMemoryAnalystOpinionRepository:
    def __init__(self) -> None:
        self._items: dict[tuple[UUID, int], AnalystOpinionV1] = {}
        self._lock = asyncio.Lock()

    async def get(self, opinion_id: UUID, *, version: int | None = None) -> AnalystOpinionV1 | None:
        if version is not None:
            return self._items.get((opinion_id, version))
        versions = [
            item for (item_id, _version), item in self._items.items() if item_id == opinion_id
        ]
        return max(versions, key=lambda item: item.version) if versions else None

    async def append_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1:
        async with self._lock:
            key = (opinion.id, opinion.version)
            existing = self._items.get(key)
            if existing is not None:
                if existing == opinion:
                    return existing
                raise ValueError("Opinion version is append-only")
            current = await self.get(opinion.id)
            current_version = current.version if current else 0
            if current_version != expected_version or opinion.version != expected_version + 1:
                raise VersionConflictError("Analyst opinion version changed")
            if current is not None and (
                current.investigation_id != opinion.investigation_id
                or current.synthesis_artifact_id != opinion.synthesis_artifact_id
                or current.synthesis_sha256 != opinion.synthesis_sha256
                or current.base_publication_artifact_id != opinion.base_publication_artifact_id
                or current.base_publication_sha256 != opinion.base_publication_sha256
            ):
                raise ValueError(
                    "Opinion versions must retain their article and synthesis identity"
                )
            self._items[key] = opinion
            return opinion


class InMemoryInvestigationEnvelopeRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationEnvelope] = {}
        self._lock = asyncio.Lock()

    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        return self._items.get(investigation_id)

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope:
        async with self._lock:
            current = self._items.get(envelope.investigation_id)
            if current == envelope:
                return current
            if current is None:
                if expected_version is not None or envelope.version != 1:
                    raise VersionConflictError("A first envelope version must be v1")
            elif current.version != expected_version or envelope.version != current.version + 1:
                raise VersionConflictError("Investigation envelope version changed")
            self._items[envelope.investigation_id] = envelope
            return envelope


class InMemoryInvestigationBudgetRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, InvestigationBudgetSnapshot] = {}
        self._lock = asyncio.Lock()

    async def get(self, investigation_id: UUID) -> InvestigationBudgetSnapshot | None:
        current = self._items.get(investigation_id)
        if current is None:
            return None
        return InvestigationBudgetSnapshot(
            investigation_id=current.investigation_id,
            version=current.version,
            budget=current.budget,
        )

    async def save_if_version(
        self,
        snapshot: InvestigationBudgetSnapshot,
        *,
        expected_version: int | None,
    ) -> InvestigationBudgetSnapshot:
        async with self._lock:
            current = self._items.get(snapshot.investigation_id)
            if current == snapshot:
                return InvestigationBudgetSnapshot(
                    investigation_id=current.investigation_id,
                    version=current.version,
                    budget=current.budget,
                )
            if current is None:
                if expected_version is not None or snapshot.version != 1:
                    raise VersionConflictError("A first investigation budget must be v1")
            elif current.version != expected_version or snapshot.version != current.version + 1:
                raise VersionConflictError("Investigation budget version changed")
            stored = InvestigationBudgetSnapshot(
                investigation_id=snapshot.investigation_id,
                version=snapshot.version,
                budget=snapshot.budget,
            )
            self._items[snapshot.investigation_id] = stored
            return InvestigationBudgetSnapshot(
                investigation_id=stored.investigation_id,
                version=stored.version,
                budget=stored.budget,
            )


__all__ = [
    "AnalystOpinionRepository",
    "DetectionCandidateRepository",
    "EvidenceObservationRepository",
    "FindingRepository",
    "InMemoryAnalystOpinionRepository",
    "InMemoryDetectionCandidateRepository",
    "InMemoryEvidenceObservationRepository",
    "InMemoryFindingRepository",
    "InMemoryInvestigationBudgetRepository",
    "InMemoryInvestigationEnvelopeRepository",
    "InMemorySampleAssociationRepository",
    "InMemoryToolInvocationRepository",
    "InvestigationBudgetRepository",
    "InvestigationBudgetSnapshot",
    "InvestigationEnvelopeRepository",
    "SampleAssociationRepository",
    "ToolInvocationRepository",
    "VersionConflictError",
]
