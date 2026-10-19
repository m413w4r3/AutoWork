"""SQLAlchemy repositories for the durable investigation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.investigation_persistence import (
    InvestigationBudgetSnapshot,
    VersionConflictError,
)
from cti_app.application.investigation_state import InvestigationStateVersionConflict
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
)
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    BudgetReservation,
    BudgetReservationStatus,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    InvestigationFinding,
    MotifProvenance,
    NoveltyClass,
    QualificationActorKind,
    QualificationChange,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
    thaw_json,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    InvestigationWorkingState,
    StopReason,
)
from cti_app.infrastructure.database.models.core import BlobRow, SampleRow
from cti_app.infrastructure.database.models.investigation import (
    AnalystOpinionVersionRow,
    DetectionCandidateMotifObservationRow,
    DetectionCandidateMotifRow,
    DetectionCandidateTestRefRow,
    DetectionCandidateVersionRow,
    EvidenceObservationRow,
    InvestigationBudgetLimitRow,
    InvestigationBudgetRecordRow,
    InvestigationBudgetReservationAmountRow,
    InvestigationBudgetReservationRow,
    InvestigationEnvelopeRow,
    InvestigationFindingClaimRow,
    InvestigationFindingObservationRow,
    InvestigationFindingVersionRow,
    InvestigationWorkingStateRow,
    QualificationChangeRow,
    SampleAssociationVersionRow,
    ToolInvocationRow,
)
from cti_app.infrastructure.database.models.investigation import (
    SampleAssociationRow as SampleAssociationIdentityRow,
)


def _budget_to_payload(budget: InvestigationBudget) -> dict[str, Any]:
    return {
        "limits": {dimension.value: amount for dimension, amount in budget.limits.items()},
        "reservations": [
            {
                "operation_key": reservation.operation_key,
                "amounts": {
                    dimension.value: amount for dimension, amount in reservation.amounts.items()
                },
                "status": reservation.status.value,
                "committed_amounts": {
                    dimension.value: amount
                    for dimension, amount in reservation.committed_amounts.items()
                },
            }
            for reservation in budget.reservations
        ],
    }


def _budget_from_payload(payload: Mapping[str, Any]) -> InvestigationBudget:
    limits = {BudgetDimension(key): amount for key, amount in dict(payload["limits"]).items()}
    budget = InvestigationBudget(limits=limits)
    for value in payload["reservations"]:
        amounts = {BudgetDimension(key): amount for key, amount in dict(value["amounts"]).items()}
        operation_key = str(value["operation_key"])
        status = BudgetReservationStatus(value["status"])
        committed = {
            BudgetDimension(key): amount for key, amount in dict(value["committed_amounts"]).items()
        }
        budget.restore_reservation(
            BudgetReservation(
                operation_key=operation_key,
                amounts=amounts,
                status=status,
                committed_amounts=committed,
            )
        )
    return budget


def _budget_dimension_amounts(value: Mapping[str, int]) -> dict[BudgetDimension, int]:
    return {BudgetDimension(key): amount for key, amount in value.items()}


def _budget_amounts_to_json(value: Mapping[BudgetDimension, int]) -> dict[str, int]:
    return {dimension.value: amount for dimension, amount in value.items()}


class SqlAlchemyToolInvocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, invocation_id: UUID) -> ToolInvocation | None:
        row = await self._session.scalar(
            select(ToolInvocationRow)
            .where(ToolInvocationRow.id == invocation_id)
            .execution_options(populate_existing=True)
        )
        return _tool_invocation_from_row(row) if row is not None else None

    async def get_by_operation_key(self, operation_key: str) -> ToolInvocation | None:
        row = await self._session.scalar(
            select(ToolInvocationRow)
            .where(ToolInvocationRow.operation_key == operation_key)
            .execution_options(populate_existing=True)
        )
        return _tool_invocation_from_row(row) if row is not None else None

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[ToolInvocation, ...]:
        rows = await self._session.scalars(
            select(ToolInvocationRow)
            .where(ToolInvocationRow.investigation_id == investigation_id)
            .order_by(ToolInvocationRow.created_at, ToolInvocationRow.id)
            .execution_options(populate_existing=True)
        )
        return tuple(_tool_invocation_from_row(row) for row in rows)

    async def add_if_absent(self, invocation: ToolInvocation) -> ToolInvocation:
        existing_id = await self.get(invocation.id)
        if existing_id is not None:
            if existing_id != invocation:
                raise ValueError("Invocation id is already used")
            return existing_id

        statement = (
            insert(ToolInvocationRow)
            .values(**_tool_invocation_values(invocation))
            .on_conflict_do_nothing(index_elements=[ToolInvocationRow.operation_key])
            .returning(ToolInvocationRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        if inserted_id is not None:
            return invocation

        existing = await self.get_by_operation_key(invocation.operation_key)
        if existing is None:
            raise RuntimeError("Conflicting tool invocation was not visible after insert")
        if (
            existing.investigation_id != invocation.investigation_id
            or existing.tool_name != invocation.tool_name
            or existing.schema_version != invocation.schema_version
            or existing.normalized_parameters != invocation.normalized_parameters
        ):
            raise ValueError("An operation key cannot identify different invocation content")
        return existing

    async def update_if_version(
        self, invocation: ToolInvocation, *, expected_version: int
    ) -> ToolInvocation:
        current = await self.get(invocation.id)
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

        result = await self._session.execute(
            update(ToolInvocationRow)
            .where(
                ToolInvocationRow.id == invocation.id,
                ToolInvocationRow.version == expected_version,
            )
            .values(
                status=invocation.status.value,
                reserved_budget=_budget_amounts_to_json(invocation.reserved_budget),
                consumed_budget=_budget_amounts_to_json(invocation.consumed_budget),
                response_refs=[str(item) for item in invocation.response_refs],
                updated_at=invocation.updated_at,
                version=invocation.version,
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            refreshed = await self.get(invocation.id)
            if refreshed == invocation:
                return invocation
            raise VersionConflictError("Tool invocation version changed")
        await self._session.flush()
        return invocation


def _tool_invocation_values(invocation: ToolInvocation) -> dict[str, Any]:
    return {
        "id": invocation.id,
        "investigation_id": invocation.investigation_id,
        "operation_key": invocation.operation_key,
        "tool_name": invocation.tool_name,
        "schema_version": invocation.schema_version,
        "normalized_parameters": thaw_json(invocation.normalized_parameters),
        "purpose": invocation.purpose,
        "status": invocation.status.value,
        "reserved_budget": _budget_amounts_to_json(invocation.reserved_budget),
        "consumed_budget": _budget_amounts_to_json(invocation.consumed_budget),
        "response_refs": [str(item) for item in invocation.response_refs],
        "created_at": invocation.created_at,
        "updated_at": invocation.updated_at,
        "version": invocation.version,
    }


def _tool_invocation_from_row(row: ToolInvocationRow) -> ToolInvocation:
    return ToolInvocation(
        id=row.id,
        investigation_id=row.investigation_id,
        operation_key=row.operation_key,
        tool_name=row.tool_name,
        schema_version=row.schema_version,
        normalized_parameters=row.normalized_parameters,
        purpose=row.purpose,
        status=ToolInvocationStatus(row.status),
        reserved_budget=_budget_dimension_amounts(row.reserved_budget),
        consumed_budget=_budget_dimension_amounts(row.consumed_budget),
        response_refs=tuple(UUID(item) for item in row.response_refs),
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


class SqlAlchemyEvidenceObservationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        row = await self._session.scalar(
            select(EvidenceObservationRow)
            .where(EvidenceObservationRow.id == observation_id)
            .execution_options(populate_existing=True)
        )
        return _evidence_observation_from_row(row) if row is not None else None

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        rows = await self._session.scalars(
            select(EvidenceObservationRow)
            .where(EvidenceObservationRow.investigation_id == investigation_id)
            .order_by(EvidenceObservationRow.observed_at, EvidenceObservationRow.id)
        )
        return tuple(_evidence_observation_from_row(row) for row in rows)

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        existing = await self.get(observation.id)
        if existing is not None:
            if existing != observation:
                raise ValueError(
                    "An immutable observation id cannot be reused with different content"
                )
            return existing

        same_identity = await self._session.scalar(
            select(EvidenceObservationRow).where(
                EvidenceObservationRow.investigation_id == observation.investigation_id,
                EvidenceObservationRow.sha256 == observation.sha256,
                EvidenceObservationRow.kind == observation.kind.value,
                EvidenceObservationRow.object_type == observation.object_type,
                EvidenceObservationRow.object_id == observation.object_id,
            )
        )
        if same_identity is not None:
            return _evidence_observation_from_row(same_identity)

        blob_sha256 = await self._session.scalar(
            select(BlobRow.sha256).where(BlobRow.id == observation.blob_id)
        )
        if blob_sha256 is not None and blob_sha256 != observation.sha256:
            raise ValueError("Observation SHA-256 must match its canonical blob")

        statement = (
            insert(EvidenceObservationRow)
            .values(**_evidence_observation_values(observation))
            .on_conflict_do_nothing()
            .returning(EvidenceObservationRow.id)
        )
        inserted_id = await self._session.scalar(statement)
        if inserted_id is not None:
            return observation

        existing = await self.get(observation.id)
        if existing is not None:
            if existing != observation:
                raise ValueError(
                    "An immutable observation id cannot be reused with different content"
                )
            return existing
        same_identity = await self._session.scalar(
            select(EvidenceObservationRow).where(
                EvidenceObservationRow.investigation_id == observation.investigation_id,
                EvidenceObservationRow.sha256 == observation.sha256,
                EvidenceObservationRow.kind == observation.kind.value,
                EvidenceObservationRow.object_type == observation.object_type,
                EvidenceObservationRow.object_id == observation.object_id,
            )
        )
        if same_identity is None:
            raise RuntimeError("Conflicting evidence observation was not visible after insert")
        return _evidence_observation_from_row(same_identity)


def _evidence_observation_values(observation: EvidenceObservation) -> dict[str, Any]:
    return {
        "id": observation.id,
        "investigation_id": observation.investigation_id,
        "kind": observation.kind.value,
        "blob_id": observation.blob_id,
        "sha256": observation.sha256,
        "source": observation.source,
        "source_version": observation.source_version,
        "observed_at": observation.observed_at,
        "object_type": observation.object_type,
        "object_id": observation.object_id,
        "limitations": list(observation.limitations),
        "invocation_ref": observation.invocation_ref,
    }


def _evidence_observation_from_row(row: EvidenceObservationRow) -> EvidenceObservation:
    return EvidenceObservation(
        id=row.id,
        investigation_id=row.investigation_id,
        kind=EvidenceKind(row.kind),
        blob_id=row.blob_id,
        sha256=row.sha256,
        source=row.source,
        source_version=row.source_version,
        observed_at=row.observed_at,
        object_type=row.object_type,
        object_id=row.object_id,
        limitations=tuple(row.limitations),
        invocation_ref=row.invocation_ref,
    )


class SqlAlchemyFindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, finding_id: UUID) -> InvestigationFinding | None:
        row = await self._session.scalar(
            select(InvestigationFindingVersionRow)
            .where(InvestigationFindingVersionRow.id == finding_id)
            .execution_options(populate_existing=True)
        )
        return await self._finding_from_row(row) if row is not None else None

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationFinding, ...]:
        rows = await self._session.scalars(
            select(InvestigationFindingVersionRow)
            .where(InvestigationFindingVersionRow.investigation_id == investigation_id)
            .order_by(InvestigationFindingVersionRow.created_at, InvestigationFindingVersionRow.id)
        )
        return tuple([await self._finding_from_row(row) for row in rows])

    async def append_version(
        self, finding: InvestigationFinding, *, expected_previous_id: UUID | None
    ) -> InvestigationFinding:
        existing = await self.get(finding.id)
        if existing is not None:
            if existing == finding:
                return existing
            raise ValueError("Finding version id is already used")
        if finding.supersedes != expected_previous_id:
            raise ValueError("Finding supersedes must match the expected prior version")
        if expected_previous_id is None:
            if finding.version != 1:
                raise VersionConflictError("A first finding version must be v1")
        else:
            previous_row = await self._session.scalar(
                select(InvestigationFindingVersionRow)
                .where(InvestigationFindingVersionRow.id == expected_previous_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous_row is None or previous_row.version + 1 != finding.version:
                raise VersionConflictError("Finding prior version changed")
            if (
                previous_row.investigation_id != finding.investigation_id
                or previous_row.kind != finding.kind.value
            ):
                raise ValueError("Finding versions must retain investigation and kind")
            newer = await self._session.scalar(
                select(InvestigationFindingVersionRow.id).where(
                    InvestigationFindingVersionRow.supersedes_id == expected_previous_id
                )
            )
            if newer is not None:
                raise VersionConflictError("Finding already has a newer version")

        self._session.add(InvestigationFindingVersionRow(**_finding_values(finding)))
        await self._session.flush()
        self._session.add_all(
            [
                InvestigationFindingObservationRow(
                    finding_id=finding.id, position=index, observation_id=observation_id
                )
                for index, observation_id in enumerate(finding.observation_refs)
            ]
        )
        self._session.add_all(
            [
                InvestigationFindingClaimRow(
                    finding_id=finding.id, position=index, claim_ref=claim_ref
                )
                for index, claim_ref in enumerate(finding.claim_refs)
            ]
        )
        await self._session.flush()
        return finding

    async def _finding_from_row(self, row: InvestigationFindingVersionRow) -> InvestigationFinding:
        observation_links = await self._session.scalars(
            select(InvestigationFindingObservationRow.observation_id)
            .where(InvestigationFindingObservationRow.finding_id == row.id)
            .order_by(InvestigationFindingObservationRow.position)
        )
        claim_links = await self._session.scalars(
            select(InvestigationFindingClaimRow.claim_ref)
            .where(InvestigationFindingClaimRow.finding_id == row.id)
            .order_by(InvestigationFindingClaimRow.position)
        )
        observation_refs = tuple(UUID(value) for value in row.observation_ids)
        claim_refs = tuple(row.claim_refs)
        if tuple(observation_links) != observation_refs or tuple(claim_links) != claim_refs:
            raise ValueError("Finding reference metadata is inconsistent with its link records")
        return InvestigationFinding(
            id=row.id,
            investigation_id=row.investigation_id,
            kind=FindingKind(row.kind),
            assessment=FindingAssessment(row.assessment),
            statement=row.statement,
            observation_refs=observation_refs,
            claim_refs=claim_refs,
            novelty=NoveltyClass(row.novelty) if row.novelty is not None else None,
            scope=row.scope,
            limitations=tuple(row.limitations),
            version=row.version,
            supersedes=row.supersedes_id,
            created_at=row.created_at,
        )


def _finding_values(finding: InvestigationFinding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "investigation_id": finding.investigation_id,
        "kind": finding.kind.value,
        "assessment": finding.assessment.value,
        "statement": finding.statement,
        "observation_ids": [str(item) for item in finding.observation_refs],
        "claim_refs": list(finding.claim_refs),
        "novelty": finding.novelty.value if finding.novelty is not None else None,
        "scope": finding.scope,
        "limitations": list(finding.limitations),
        "version": finding.version,
        "supersedes_id": finding.supersedes,
        "created_at": finding.created_at,
    }


class SqlAlchemySampleAssociationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, association_id: UUID) -> SampleAssociation | None:
        identity = await self._session.scalar(
            select(SampleAssociationIdentityRow)
            .where(SampleAssociationIdentityRow.id == association_id)
            .execution_options(populate_existing=True)
        )
        return await self._association_from_identity(identity) if identity is not None else None

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        identities = await self._session.scalars(
            select(SampleAssociationIdentityRow)
            .where(SampleAssociationIdentityRow.investigation_id == investigation_id)
            .order_by(SampleAssociationIdentityRow.sample_sha256, SampleAssociationIdentityRow.id)
        )
        associations: list[SampleAssociation] = []
        for identity in identities:
            association = await self._association_from_identity(identity)
            if association is not None:
                associations.append(association)
        return tuple(associations)

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation:
        existing_identity = await self._session.scalar(
            select(SampleAssociationIdentityRow)
            .where(SampleAssociationIdentityRow.id == association.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_identity is None and (expected_version != 0 or association.version != 1):
            raise VersionConflictError("A first sample association version must be v1")
        identity_values = {
            "id": association.id,
            "investigation_id": association.investigation_id,
            "sample_id": await self._sample_id_for_hash(association.sample_sha256),
            "sample_sha256": association.sample_sha256,
            "origin": association.origin.value,
        }
        if existing_identity is None:
            await self._session.scalar(
                insert(SampleAssociationIdentityRow)
                .values(**identity_values)
                .on_conflict_do_nothing(index_elements=[SampleAssociationIdentityRow.id])
                .returning(SampleAssociationIdentityRow.id)
            )
        identity = await self._session.scalar(
            select(SampleAssociationIdentityRow)
            .where(SampleAssociationIdentityRow.id == association.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if identity is None:
            raise RuntimeError("Sample association identity was not visible after insert")
        if (
            identity.investigation_id != association.investigation_id
            or identity.sample_sha256 != association.sample_sha256
            or identity.origin != association.origin.value
        ):
            raise ValueError("Sample association versions must retain sample identity")

        current = await self._association_from_identity(identity)
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

        if current is not None and association.changes[:-1] != current.changes:
            raise ValueError("Qualification history is append-only")
        self._session.add(
            SampleAssociationVersionRow(
                association_id=association.id,
                version=association.version,
                qualification=association.qualification.value,
                related_sample_sha256=list(association.related_sample_sha256),
            )
        )
        await self._session.flush()
        if association.changes:
            change = association.changes[-1]
            self._session.add(
                QualificationChangeRow(
                    association_id=association.id,
                    version=association.version,
                    from_qualification=change.from_qualification.value,
                    to_qualification=change.to_qualification.value,
                    actor_kind=change.actor_kind.value,
                    actor_id=change.actor_id,
                    reason=change.reason,
                    changed_at=change.changed_at,
                )
            )
        await self._session.flush()
        return association

    async def _sample_id_for_hash(self, sha256: str) -> UUID | None:
        sample_ids = list(
            await self._session.scalars(
                select(SampleRow.id)
                .join(BlobRow, BlobRow.id == SampleRow.blob_id)
                .where(BlobRow.sha256 == sha256)
                .order_by(SampleRow.id)
                .limit(2)
            )
        )
        return sample_ids[0] if len(sample_ids) == 1 else None

    async def _association_from_identity(
        self, identity: SampleAssociationIdentityRow
    ) -> SampleAssociation | None:
        version_row = await self._session.scalar(
            select(SampleAssociationVersionRow)
            .where(SampleAssociationVersionRow.association_id == identity.id)
            .order_by(SampleAssociationVersionRow.version.desc())
            .limit(1)
        )
        if version_row is None:
            return None
        changes_rows = await self._session.scalars(
            select(QualificationChangeRow)
            .where(QualificationChangeRow.association_id == identity.id)
            .order_by(QualificationChangeRow.version)
        )
        changes = tuple(
            QualificationChange(
                from_qualification=SampleQualification(row.from_qualification),
                to_qualification=SampleQualification(row.to_qualification),
                actor_kind=QualificationActorKind(row.actor_kind),
                actor_id=row.actor_id,
                reason=row.reason,
                changed_at=row.changed_at,
            )
            for row in changes_rows
        )
        return SampleAssociation(
            id=identity.id,
            investigation_id=identity.investigation_id,
            sample_sha256=identity.sample_sha256,
            origin=SampleOrigin(identity.origin),
            qualification=SampleQualification(version_row.qualification),
            changes=changes,
            related_sample_sha256=tuple(version_row.related_sample_sha256),
            version=version_row.version,
        )


class SqlAlchemyDetectionCandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, version_id: UUID) -> DetectionCandidateVersion | None:
        row = await self._session.scalar(
            select(DetectionCandidateVersionRow)
            .where(DetectionCandidateVersionRow.id == version_id)
            .execution_options(populate_existing=True)
        )
        return await self._candidate_from_row(row) if row is not None else None

    async def list_for_candidate(self, candidate_id: UUID) -> tuple[DetectionCandidateVersion, ...]:
        rows = await self._session.scalars(
            select(DetectionCandidateVersionRow)
            .where(DetectionCandidateVersionRow.candidate_id == candidate_id)
            .order_by(DetectionCandidateVersionRow.version, DetectionCandidateVersionRow.id)
        )
        return tuple([await self._candidate_from_row(row) for row in rows])

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionCandidateVersion, ...]:
        rows = await self._session.scalars(
            select(DetectionCandidateVersionRow)
            .where(DetectionCandidateVersionRow.investigation_id == investigation_id)
            .order_by(
                DetectionCandidateVersionRow.candidate_id,
                DetectionCandidateVersionRow.version,
                DetectionCandidateVersionRow.id,
            )
        )
        return tuple([await self._candidate_from_row(row) for row in rows])

    async def append_version(
        self, version: DetectionCandidateVersion, *, expected_previous_id: UUID | None
    ) -> DetectionCandidateVersion:
        existing = await self.get(version.id)
        if existing is not None:
            if existing == version:
                return existing
            raise ValueError("Detection version id is already used")
        if version.parent_version_id != expected_previous_id:
            raise ValueError("Detection parent must match the expected prior version")
        if expected_previous_id is None:
            if version.version != 1:
                raise VersionConflictError("A first detection candidate must be v1")
        else:
            previous_row = await self._session.scalar(
                select(DetectionCandidateVersionRow)
                .where(DetectionCandidateVersionRow.id == expected_previous_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if previous_row is None or previous_row.version + 1 != version.version:
                raise VersionConflictError("Detection candidate prior version changed")
            if previous_row.candidate_id != version.candidate_id:
                raise ValueError("Detection versions must share a candidate identity")
            if previous_row.investigation_id != version.investigation_id:
                raise ValueError("Detection versions must retain investigation identity")
            newer = await self._session.scalar(
                select(DetectionCandidateVersionRow.id).where(
                    DetectionCandidateVersionRow.parent_version_id == expected_previous_id
                )
            )
            if newer is not None:
                raise VersionConflictError("Detection candidate already has a newer version")

        duplicate_version = await self._session.scalar(
            select(DetectionCandidateVersionRow.id).where(
                DetectionCandidateVersionRow.candidate_id == version.candidate_id,
                DetectionCandidateVersionRow.version == version.version,
            )
        )
        if duplicate_version is not None:
            raise VersionConflictError("Detection candidate version already exists")

        self._session.add(DetectionCandidateVersionRow(**_candidate_values(version)))
        await self._session.flush()
        motif_rows = [
            DetectionCandidateMotifRow(
                version_id=version.id,
                position=motif_index,
                motif=motif.motif,
                rationale=motif.rationale,
            )
            for motif_index, motif in enumerate(version.motif_provenance)
        ]
        self._session.add_all(motif_rows)
        await self._session.flush()
        for motif_index, motif in enumerate(version.motif_provenance):
            self._session.add_all(
                [
                    DetectionCandidateMotifObservationRow(
                        version_id=version.id,
                        motif_position=motif_index,
                        observation_position=observation_index,
                        observation_id=observation_id,
                    )
                    for observation_index, observation_id in enumerate(motif.observation_refs)
                ]
            )
        self._session.add_all(
            [
                DetectionCandidateTestRefRow(
                    version_id=version.id, position=index, test_ref=test_ref
                )
                for index, test_ref in enumerate(version.test_refs)
            ]
        )
        await self._session.flush()
        return version

    async def _candidate_from_row(
        self, row: DetectionCandidateVersionRow
    ) -> DetectionCandidateVersion:
        motif_rows = await self._session.scalars(
            select(DetectionCandidateMotifRow)
            .where(DetectionCandidateMotifRow.version_id == row.id)
            .order_by(DetectionCandidateMotifRow.position)
        )
        motifs: list[MotifProvenance] = []
        for motif_row in motif_rows:
            refs = await self._session.scalars(
                select(DetectionCandidateMotifObservationRow.observation_id)
                .where(
                    DetectionCandidateMotifObservationRow.version_id == row.id,
                    DetectionCandidateMotifObservationRow.motif_position == motif_row.position,
                )
                .order_by(DetectionCandidateMotifObservationRow.observation_position)
            )
            motifs.append(
                MotifProvenance(
                    motif=motif_row.motif,
                    rationale=motif_row.rationale,
                    observation_refs=tuple(refs),
                )
            )
        test_refs = await self._session.scalars(
            select(DetectionCandidateTestRefRow.test_ref)
            .where(DetectionCandidateTestRefRow.version_id == row.id)
            .order_by(DetectionCandidateTestRefRow.position)
        )
        return DetectionCandidateVersion(
            id=row.id,
            candidate_id=row.candidate_id,
            investigation_id=row.investigation_id,
            source=row.source,
            source_sha256=row.source_sha256,
            motif_provenance=tuple(motifs),
            status=DetectionCandidateStatus(row.status),
            version=row.version,
            parent_version_id=row.parent_version_id,
            test_refs=tuple(test_refs),
            created_at=row.created_at,
        )


def _candidate_values(version: DetectionCandidateVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "candidate_id": version.candidate_id,
        "investigation_id": version.investigation_id,
        "source": version.source,
        "source_sha256": version.source_sha256,
        "status": version.status.value,
        "version": version.version,
        "parent_version_id": version.parent_version_id,
        "created_at": version.created_at,
    }


class SqlAlchemyAnalystOpinionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, opinion_id: UUID, *, version: int | None = None) -> AnalystOpinionV1 | None:
        statement = select(AnalystOpinionVersionRow).where(
            AnalystOpinionVersionRow.opinion_id == opinion_id
        )
        if version is not None:
            statement = statement.where(AnalystOpinionVersionRow.version == version)
        else:
            statement = statement.order_by(AnalystOpinionVersionRow.version.desc()).limit(1)
        row = await self._session.scalar(statement.execution_options(populate_existing=True))
        return _opinion_from_row(row) if row is not None else None

    async def append_version(
        self, opinion: AnalystOpinionV1, *, expected_version: int
    ) -> AnalystOpinionV1:
        existing = await self.get(opinion.id, version=opinion.version)
        if existing is not None:
            if existing == opinion:
                return existing
            raise ValueError("Opinion version is append-only")
        current_row = await self._session.scalar(
            select(AnalystOpinionVersionRow)
            .where(AnalystOpinionVersionRow.opinion_id == opinion.id)
            .order_by(AnalystOpinionVersionRow.version.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = _opinion_from_row(current_row) if current_row is not None else None
        current_version = current.version if current is not None else 0
        if current_version != expected_version or opinion.version != expected_version + 1:
            raise VersionConflictError("Analyst opinion version changed")
        if current is not None and (
            current.investigation_id != opinion.investigation_id
            or current.synthesis_artifact_id != opinion.synthesis_artifact_id
            or current.synthesis_sha256 != opinion.synthesis_sha256
            or current.base_publication_artifact_id != opinion.base_publication_artifact_id
            or current.base_publication_sha256 != opinion.base_publication_sha256
        ):
            raise ValueError("Opinion versions must retain their article and synthesis identity")
        self._session.add(
            AnalystOpinionVersionRow(
                opinion_id=opinion.id,
                version=opinion.version,
                investigation_id=opinion.investigation_id,
                content=opinion.to_payload(),
                content_sha256=opinion.content_sha256,
                status=opinion.status.value,
                created_at=opinion.created_at,
            )
        )
        await self._session.flush()
        return opinion


def _opinion_from_row(row: AnalystOpinionVersionRow) -> AnalystOpinionV1:
    content = row.content
    if (
        UUID(str(content["id"])) != row.opinion_id
        or int(content["version"]) != row.version
        or UUID(str(content["investigation_id"])) != row.investigation_id
        or content["content_sha256"] != row.content_sha256
        or content["status"] != row.status
    ):
        raise ValueError("Opinion content metadata does not match its version row")
    blocks = tuple(
        OpinionBlock(
            block_type=OpinionBlockType(item["type"]),
            text=str(item["text"]),
            finding_refs=tuple(UUID(value) for value in item["finding_refs"]),
            evidence_refs=tuple(UUID(value) for value in item["evidence_refs"]),
            qualification=OpinionClaimQualification(item["qualification"]),
            certainty=float(item["certainty"]),
        )
        for item in content["blocks"]
    )
    return AnalystOpinionV1(
        id=row.opinion_id,
        version=row.version,
        investigation_id=row.investigation_id,
        synthesis_artifact_id=UUID(str(content["synthesis_artifact_id"])),
        synthesis_sha256=str(content["synthesis_sha256"]),
        base_publication_artifact_id=UUID(str(content["base_publication_artifact_id"])),
        base_publication_sha256=str(content["base_publication_sha256"]),
        blocks=blocks,
        unresolved_points=tuple(str(value) for value in content["unresolved_points"]),
        protocol_figures=tuple(
            OpinionProtocolFigureV1.from_payload(item)
            for item in content.get("protocol_figures", [])
        ),
        author_id=str(content["author_id"]),
        reviewer_id=(str(content["reviewer_id"]) if content["reviewer_id"] is not None else None),
        status=AnalystOpinionStatus(row.status),
        created_at=datetime.fromisoformat(str(content["created_at"])),
        content_sha256=row.content_sha256,
    )


def _working_state_from_row(row: InvestigationWorkingStateRow) -> InvestigationWorkingState:
    payload = row.state
    if not isinstance(payload, dict):
        raise ValueError("Persisted investigation state must be a JSON object")
    if payload.get("schema") != InvestigationWorkingState.SCHEMA:
        raise ValueError("Persisted investigation state has an unsupported schema")
    try:
        agenda = InvestigationAgenda(
            items=tuple(
                AgendaItem(
                    question=item["question"],
                    potential=item["potential"],
                    rank=item["rank"],
                )
                for item in payload["agenda"]
            )
        )
        hypotheses = tuple(
            Hypothesis(
                id=item["id"],
                statement=item["statement"],
                status=HypothesisStatus(item["status"]),
                evidence_refs=tuple(UUID(value) for value in item["evidence_refs"]),
                finding_refs=tuple(UUID(value) for value in item["finding_refs"]),
            )
            for item in payload["hypotheses"]
        )
        state = InvestigationWorkingState(
            investigation_id=UUID(payload["investigation_id"]),
            agenda=agenda,
            version=payload["version"],
            hypotheses=hypotheses,
            turn_index=payload["turn_index"],
            last_context_sha256=payload["last_context_sha256"],
            last_decision_sha256=payload["last_decision_sha256"],
            pending_context_snapshot_id=(
                UUID(payload["pending_context_snapshot_id"])
                if payload["pending_context_snapshot_id"] is not None
                else None
            ),
            pending_context_sha256=payload["pending_context_sha256"],
            stop_reason=StopReason(payload["stop_reason"])
            if payload["stop_reason"] is not None
            else None,
            no_progress_streak=payload["no_progress_streak"],
            deduplicated_findings=payload.get("deduplicated_findings", 0),
            last_seen_evidence_ids=tuple(
                UUID(value) for value in payload["last_seen_evidence_ids"]
            ),
            last_seen_finding_ids=tuple(UUID(value) for value in payload["last_seen_finding_ids"]),
            recent_actions=tuple(payload["recent_actions"]),
            base_publication_artifact_id=(
                UUID(payload["base_publication_artifact_id"])
                if payload["base_publication_artifact_id"] is not None
                else None
            ),
            base_publication_sha256=payload["base_publication_sha256"],
            execution_mode=InvestigationExecutionMode(
                payload.get("execution_mode", InvestigationExecutionMode.BRIDGE_LED.value)
            ),
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Persisted investigation state is invalid") from exc
    normalized_payload = dict(payload)
    normalized_payload.setdefault("execution_mode", InvestigationExecutionMode.BRIDGE_LED.value)
    if (
        state.to_payload() != normalized_payload
        or state.investigation_id != row.investigation_id
        or state.version != row.version
        or state.turn_index != row.turn_index
        or (state.stop_reason.value if state.stop_reason else None) != row.stop_reason
    ):
        raise ValueError("Persisted investigation state disagrees with its indexed columns")
    return state


def _working_state_values(
    state: InvestigationWorkingState, *, updated_at: datetime
) -> dict[str, Any]:
    return {
        "investigation_id": state.investigation_id,
        "state": state.to_payload(),
        "version": state.version,
        "turn_index": state.turn_index,
        "stop_reason": state.stop_reason.value if state.stop_reason else None,
        "updated_at": updated_at,
    }


class SqlAlchemyInvestigationStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, investigation_id: UUID) -> InvestigationWorkingState | None:
        row = await self._session.scalar(
            select(InvestigationWorkingStateRow)
            .where(InvestigationWorkingStateRow.investigation_id == investigation_id)
            .execution_options(populate_existing=True)
        )
        return _working_state_from_row(row) if row is not None else None

    async def save_if_version(
        self, state: InvestigationWorkingState, *, expected_version: int | None
    ) -> InvestigationWorkingState:
        current_row = await self._session.scalar(
            select(InvestigationWorkingStateRow)
            .where(InvestigationWorkingStateRow.investigation_id == state.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = _working_state_from_row(current_row) if current_row is not None else None
        if current == state:
            return current
        if current is None:
            if expected_version is not None or state.version != 1:
                raise InvestigationStateVersionConflict("First state version must be v1")
            inserted_id = await self._session.scalar(
                insert(InvestigationWorkingStateRow)
                .values(**_working_state_values(state, updated_at=datetime.now(UTC)))
                .on_conflict_do_nothing(
                    index_elements=[InvestigationWorkingStateRow.investigation_id]
                )
                .returning(InvestigationWorkingStateRow.investigation_id)
            )
            if inserted_id is not None:
                return state
            current = await self.get(state.investigation_id)
            if current == state:
                return current
            raise InvestigationStateVersionConflict("Investigation state version changed")
        if current.version != expected_version or state.version != current.version + 1:
            raise InvestigationStateVersionConflict("Investigation state version changed")

        result = await self._session.execute(
            update(InvestigationWorkingStateRow)
            .where(
                InvestigationWorkingStateRow.investigation_id == state.investigation_id,
                InvestigationWorkingStateRow.version == expected_version,
            )
            .values(**_working_state_values(state, updated_at=datetime.now(UTC)))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise InvestigationStateVersionConflict("Investigation state version changed")
        await self._session.flush()
        return state


class SqlAlchemyInvestigationEnvelopeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        row = await self._session.scalar(
            select(InvestigationEnvelopeRow)
            .where(InvestigationEnvelopeRow.investigation_id == investigation_id)
            .execution_options(populate_existing=True)
        )
        return _envelope_from_row(row) if row is not None else None

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope:
        current_row = await self._session.scalar(
            select(InvestigationEnvelopeRow)
            .where(InvestigationEnvelopeRow.investigation_id == envelope.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        current = _envelope_from_row(current_row) if current_row is not None else None
        if current == envelope:
            return current
        if current is None:
            if expected_version is not None or envelope.version != 1:
                raise VersionConflictError("A first envelope version must be v1")
            inserted_id = await self._session.scalar(
                insert(InvestigationEnvelopeRow)
                .values(**_envelope_values(envelope, updated_at=datetime.now(UTC)))
                .on_conflict_do_nothing(index_elements=[InvestigationEnvelopeRow.investigation_id])
                .returning(InvestigationEnvelopeRow.investigation_id)
            )
            if inserted_id is not None:
                return envelope
            current = await self.get(envelope.investigation_id)
            if current == envelope:
                return current
            raise VersionConflictError("Investigation envelope version changed")
        if current.version != expected_version or envelope.version != current.version + 1:
            raise VersionConflictError("Investigation envelope version changed")

        result = await self._session.execute(
            update(InvestigationEnvelopeRow)
            .where(
                InvestigationEnvelopeRow.investigation_id == envelope.investigation_id,
                InvestigationEnvelopeRow.version == expected_version,
            )
            .values(**_envelope_values(envelope, updated_at=datetime.now(UTC)))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise VersionConflictError("Investigation envelope version changed")
        await self._session.flush()
        return envelope


def _envelope_values(envelope: InvestigationEnvelope, *, updated_at: datetime) -> dict[str, Any]:
    return {
        "investigation_id": envelope.investigation_id,
        "allowed_providers": list(envelope.allowed_providers),
        "budget_snapshot": _budget_to_payload(envelope.budget),
        "acquire_hits_allowed": envelope.acquire_hits_allowed,
        "disclosure_tlp": envelope.disclosure.tlp.value if envelope.disclosure.tlp else None,
        "disclosure_do_not_submit": envelope.disclosure.do_not_submit,
        "disclosure_external_llm_allowed": envelope.disclosure.external_llm_allowed,
        "approval_decision_id": envelope.approval_decision_id,
        "approval_actor_id": envelope.approval_actor_id,
        "approved_at": envelope.approved_at,
        "updated_at": updated_at,
        "version": envelope.version,
    }


def _envelope_from_row(row: InvestigationEnvelopeRow) -> InvestigationEnvelope:
    return InvestigationEnvelope(
        investigation_id=row.investigation_id,
        allowed_providers=tuple(row.allowed_providers),
        budget=_budget_from_payload(row.budget_snapshot),
        acquire_hits_allowed=row.acquire_hits_allowed,
        disclosure=DisclosureBounds(
            tlp=TLPLevel(row.disclosure_tlp) if row.disclosure_tlp is not None else None,
            do_not_submit=row.disclosure_do_not_submit,
            external_llm_allowed=row.disclosure_external_llm_allowed,
        ),
        approval_decision_id=row.approval_decision_id,
        approval_actor_id=row.approval_actor_id,
        approved_at=row.approved_at,
        version=row.version,
    )


class SqlAlchemyInvestigationBudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, investigation_id: UUID) -> InvestigationBudgetSnapshot | None:
        row = await self._session.scalar(
            select(InvestigationBudgetRecordRow)
            .where(InvestigationBudgetRecordRow.investigation_id == investigation_id)
            .execution_options(populate_existing=True)
        )
        return await self._snapshot_from_row(row) if row is not None else None

    async def save_if_version(
        self,
        snapshot: InvestigationBudgetSnapshot,
        *,
        expected_version: int | None,
    ) -> InvestigationBudgetSnapshot:
        row = await self._session.scalar(
            select(InvestigationBudgetRecordRow)
            .where(InvestigationBudgetRecordRow.investigation_id == snapshot.investigation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            if expected_version is not None or snapshot.version != 1:
                raise VersionConflictError("A first investigation budget must be v1")
            inserted_id = await self._session.scalar(
                insert(InvestigationBudgetRecordRow)
                .values(
                    investigation_id=snapshot.investigation_id,
                    version=snapshot.version,
                    updated_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(
                    index_elements=[InvestigationBudgetRecordRow.investigation_id]
                )
                .returning(InvestigationBudgetRecordRow.investigation_id)
            )
            if inserted_id is not None:
                await self._write_budget_children(snapshot)
                await self._session.flush()
                saved = await self.get(snapshot.investigation_id)
                if saved is None:
                    raise RuntimeError("Saved investigation budget was not visible")
                return saved
            current = await self.get(snapshot.investigation_id)
            if current == snapshot:
                return current
            raise VersionConflictError("Investigation budget version changed")

        current = await self._snapshot_from_row(row)
        if current == snapshot:
            return current
        if current.version != expected_version or snapshot.version != current.version + 1:
            raise VersionConflictError("Investigation budget version changed")

        result = await self._session.execute(
            update(InvestigationBudgetRecordRow)
            .where(
                InvestigationBudgetRecordRow.investigation_id == snapshot.investigation_id,
                InvestigationBudgetRecordRow.version == expected_version,
            )
            .values(version=snapshot.version, updated_at=datetime.now(UTC))
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise VersionConflictError("Investigation budget version changed")
        await self._replace_budget_children(snapshot)
        await self._session.flush()
        saved = await self.get(snapshot.investigation_id)
        if saved is None:
            raise RuntimeError("Saved investigation budget was not visible")
        return saved

    async def _snapshot_from_row(
        self, row: InvestigationBudgetRecordRow
    ) -> InvestigationBudgetSnapshot:
        limits_rows = await self._session.scalars(
            select(InvestigationBudgetLimitRow)
            .where(InvestigationBudgetLimitRow.investigation_id == row.investigation_id)
            .order_by(InvestigationBudgetLimitRow.dimension)
        )
        limits = {BudgetDimension(item.dimension): item.amount for item in limits_rows}
        reservations = await self._session.scalars(
            select(InvestigationBudgetReservationRow)
            .where(InvestigationBudgetReservationRow.investigation_id == row.investigation_id)
            .order_by(InvestigationBudgetReservationRow.sequence)
        )
        budget = InvestigationBudget(limits=limits)
        for reservation in reservations:
            amount_rows = await self._session.scalars(
                select(InvestigationBudgetReservationAmountRow)
                .where(
                    InvestigationBudgetReservationAmountRow.investigation_id
                    == row.investigation_id,
                    InvestigationBudgetReservationAmountRow.operation_key
                    == reservation.operation_key,
                )
                .order_by(InvestigationBudgetReservationAmountRow.dimension)
            )
            amounts: dict[BudgetDimension, int] = {}
            committed: dict[BudgetDimension, int] = {}
            for amount_row in amount_rows:
                dimension = BudgetDimension(amount_row.dimension)
                if amount_row.reserved_amount is not None:
                    amounts[dimension] = amount_row.reserved_amount
                if amount_row.committed_amount is not None:
                    committed[dimension] = amount_row.committed_amount
            budget.reserve(reservation.operation_key, amounts)
            status = BudgetReservationStatus(reservation.status)
            if status is BudgetReservationStatus.COMMITTED:
                budget.commit(reservation.operation_key, committed)
            elif status is BudgetReservationStatus.RELEASED:
                budget.release(reservation.operation_key)
        return InvestigationBudgetSnapshot(
            investigation_id=row.investigation_id,
            version=row.version,
            budget=budget,
        )

    async def _write_budget_children(self, snapshot: InvestigationBudgetSnapshot) -> None:
        budget = snapshot.budget
        self._session.add_all(
            [
                InvestigationBudgetLimitRow(
                    investigation_id=snapshot.investigation_id,
                    dimension=dimension.value,
                    amount=amount,
                )
                for dimension, amount in budget.limits.items()
            ]
        )
        reservations = budget.reservations
        self._session.add_all(
            [
                InvestigationBudgetReservationRow(
                    investigation_id=snapshot.investigation_id,
                    operation_key=reservation.operation_key,
                    status=reservation.status.value,
                    sequence=sequence,
                )
                for sequence, reservation in enumerate(reservations, start=1)
            ]
        )
        await self._session.flush()
        for reservation in reservations:
            dimensions = set(reservation.amounts) | set(reservation.committed_amounts)
            self._session.add_all(
                [
                    InvestigationBudgetReservationAmountRow(
                        investigation_id=snapshot.investigation_id,
                        operation_key=reservation.operation_key,
                        dimension=dimension.value,
                        reserved_amount=reservation.amounts.get(dimension),
                        committed_amount=reservation.committed_amounts.get(dimension),
                    )
                    for dimension in dimensions
                ]
            )

    async def _replace_budget_children(self, snapshot: InvestigationBudgetSnapshot) -> None:
        investigation_id = snapshot.investigation_id
        await self._session.execute(
            delete(InvestigationBudgetReservationAmountRow).where(
                InvestigationBudgetReservationAmountRow.investigation_id == investigation_id
            )
        )
        await self._session.execute(
            delete(InvestigationBudgetReservationRow).where(
                InvestigationBudgetReservationRow.investigation_id == investigation_id
            )
        )
        await self._session.execute(
            delete(InvestigationBudgetLimitRow).where(
                InvestigationBudgetLimitRow.investigation_id == investigation_id
            )
        )
        await self._write_budget_children(snapshot)


__all__ = [
    "SqlAlchemyAnalystOpinionRepository",
    "SqlAlchemyDetectionCandidateRepository",
    "SqlAlchemyEvidenceObservationRepository",
    "SqlAlchemyFindingRepository",
    "SqlAlchemyInvestigationBudgetRepository",
    "SqlAlchemyInvestigationEnvelopeRepository",
    "SqlAlchemySampleAssociationRepository",
    "SqlAlchemyToolInvocationRepository",
]
