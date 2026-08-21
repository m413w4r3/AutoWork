from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.briefs import (
    BriefBlock,
    BriefDraft,
    BriefDraftStatus,
    BriefEvidencePack,
    BriefSentence,
    EvidencePackScope,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    AttemptOutcome,
    Claim,
    ClaimKind,
    CollectionAttempt,
    CollectionPolicySnapshot,
    CollectionState,
    DerivedArtifact,
    Indicator,
    IndicatorKind,
    RejectedModelProposal,
    SourceCollection,
    SourceOriginKind,
    SourceSpan,
)
from cti_app.domain.discovery import (
    DiscoverySourceMode,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryIdentityStatus,
    DiscoveryInputMode,
    DiscoveryIntake,
    DiscoveryMemberReference,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySnapshotLineage,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
    SubjectContribution,
    SubjectMergeEvent,
)
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
)
from cti_app.infrastructure.database.models import (
    BriefDraftRow,
    BriefEvidencePackRow,
    ClaimRow,
    CollectionAttemptRow,
    CollectionPolicySnapshotRow,
    DerivedArtifactRow,
    DiscoveryIntakeRow,
    DiscoveryMergeRunRow,
    DiscoverySnapshotRow,
    DiscoverySubjectIdentityRow,
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    EditionRow,
    EditorialGroupRow,
    HumanDecisionRow,
    IndicatorRow,
    ProductionArtifactRow,
    RejectedModelProposalRow,
    SourceCollectionRow,
    SubjectContributionRow,
    SubjectMergeEventRow,
    SubjectProductionRunRow,
)
from cti_app.infrastructure.database.repositories.discovery import (
    _candidate_from_payload,
    _candidate_payload,
)


class SqlAlchemyDiscoveryIntakeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, intake: DiscoveryIntake) -> bool:
        statement = (
            insert(DiscoveryIntakeRow)
            .values(
                id=intake.id,
                edition_id=intake.edition_id,
                sequence=intake.sequence,
                input_mode=intake.input_mode.value,
                raw_report_hash=intake.raw_report_hash,
                parsed_report_hash=intake.parsed_report_hash,
                intake_hash=intake.intake_hash,
                research_model_run_id=intake.research_model_run_id,
                source_mode=intake.source_mode.value,
                complementary_axis=intake.complementary_axis,
                batch_id=intake.batch_id,
                created_by=intake.created_by,
                created_at=intake.created_at,
            )
            .on_conflict_do_nothing(
                index_elements=[DiscoveryIntakeRow.edition_id, DiscoveryIntakeRow.intake_hash]
            )
            .returning(DiscoveryIntakeRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, intake_id: UUID) -> DiscoveryIntake | None:
        row = await self._session.get(DiscoveryIntakeRow, intake_id)
        return _discovery_intake_from_row(row) if row else None

    async def get_by_batch(self, batch_id: UUID) -> DiscoveryIntake | None:
        row = await self._session.scalar(
            select(DiscoveryIntakeRow).where(DiscoveryIntakeRow.batch_id == batch_id)
        )
        return _discovery_intake_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryIntake]:
        rows = await self._session.scalars(
            select(DiscoveryIntakeRow)
            .where(DiscoveryIntakeRow.edition_id == edition_id)
            .order_by(DiscoveryIntakeRow.sequence)
        )
        return [_discovery_intake_from_row(row) for row in rows]

    async def next_sequence(self, edition_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(DiscoveryIntakeRow.sequence)).where(
                DiscoveryIntakeRow.edition_id == edition_id
            )
        )
        return int(current or 0) + 1


class SqlAlchemyDiscoveryMergeRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, run: DiscoveryMergeRun) -> bool:
        statement = (
            insert(DiscoveryMergeRunRow)
            .values(**_discovery_merge_run_values(run))
            .on_conflict_do_nothing(index_elements=[DiscoveryMergeRunRow.merge_input_hash])
            .returning(DiscoveryMergeRunRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def mark_resolved(self, run_id: UUID) -> None:
        await self._session.execute(
            update(DiscoveryMergeRunRow)
            .where(
                DiscoveryMergeRunRow.id == run_id,
                DiscoveryMergeRunRow.validation_status
                == MergeValidationStatus.NEEDS_REVIEW.value,
            )
            .values(validation_status=MergeValidationStatus.RESOLVED.value)
        )

    async def get(self, run_id: UUID) -> DiscoveryMergeRun | None:
        row = await self._session.get(DiscoveryMergeRunRow, run_id)
        return _discovery_merge_run_from_row(row) if row else None

    async def get_by_input_hash(self, merge_input_hash: str) -> DiscoveryMergeRun | None:
        row = await self._session.scalar(
            select(DiscoveryMergeRunRow).where(
                DiscoveryMergeRunRow.merge_input_hash == merge_input_hash
            )
        )
        return _discovery_merge_run_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoveryMergeRun]:
        rows = await self._session.scalars(
            select(DiscoveryMergeRunRow)
            .where(DiscoveryMergeRunRow.edition_id == edition_id)
            .order_by(DiscoveryMergeRunRow.created_at.desc(), DiscoveryMergeRunRow.id)
        )
        return [_discovery_merge_run_from_row(row) for row in rows]


class SqlAlchemyDiscoverySubjectIdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_many_if_absent(self, identities: Sequence[DiscoverySubjectIdentity]) -> None:
        for identity in identities:
            await self._session.execute(
                insert(DiscoverySubjectIdentityRow)
                .values(**_discovery_identity_values(identity))
                .on_conflict_do_nothing(
                    index_elements=[
                        DiscoverySubjectIdentityRow.edition_id,
                        DiscoverySubjectIdentityRow.origin_key,
                    ]
                )
            )

    async def get(self, subject_id: UUID) -> DiscoverySubjectIdentity | None:
        row = await self._session.get(DiscoverySubjectIdentityRow, subject_id)
        return _discovery_identity_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[DiscoverySubjectIdentity]:
        rows = await self._session.scalars(
            select(DiscoverySubjectIdentityRow)
            .where(DiscoverySubjectIdentityRow.edition_id == edition_id)
            .order_by(DiscoverySubjectIdentityRow.created_at, DiscoverySubjectIdentityRow.id)
        )
        return [_discovery_identity_from_row(row) for row in rows]

    async def resolve_canonical_subject(self, subject_id: UUID) -> UUID:
        visited: set[UUID] = set()
        current = subject_id
        while True:
            if current in visited:
                raise RuntimeError("Cycle in discovery subject identity projection")
            visited.add(current)
            row = await self._session.get(DiscoverySubjectIdentityRow, current)
            if row is None:
                raise LookupError(f"Unknown discovery subject {current}")
            if row.status == DiscoveryIdentityStatus.ACTIVE.value:
                return row.id
            if row.merged_into_id is None:
                raise RuntimeError("Merged identity has no canonical target")
            current = row.merged_into_id

    async def contribution_closure(self, subject_id: UUID) -> Sequence[SubjectContribution]:
        canonical_id = await self.resolve_canonical_subject(subject_id)
        canonical = await self._session.get(DiscoverySubjectIdentityRow, canonical_id)
        if canonical is None:
            raise LookupError(f"Unknown discovery subject {subject_id}")
        identities = await self._session.scalars(
            select(DiscoverySubjectIdentityRow.id).where(
                DiscoverySubjectIdentityRow.edition_id == canonical.edition_id
            )
        )
        member_ids = [
            identity_id
            for identity_id in identities
            if await self.resolve_canonical_subject(identity_id) == canonical_id
        ]
        rows = await self._session.scalars(
            select(SubjectContributionRow)
            .where(SubjectContributionRow.subject_id.in_(member_ids))
            .order_by(SubjectContributionRow.created_at, SubjectContributionRow.id)
        )
        return [_subject_contribution_from_row(row) for row in rows]


class SqlAlchemySubjectMergeEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, events: Sequence[SubjectMergeEvent]) -> None:
        identities = SqlAlchemyDiscoverySubjectIdentityRepository(self._session)
        for event in events:
            target = await identities.resolve_canonical_subject(event.into_subject_id)
            source_root = await identities.resolve_canonical_subject(event.from_subject_id)
            if target != event.into_subject_id:
                raise ValueError("A subject merge target must already be canonical")
            if source_root == target:
                raise ValueError("A subject merge would create a cycle or no-op")
            source = await self._session.get(DiscoverySubjectIdentityRow, event.from_subject_id)
            target_row = await self._session.get(DiscoverySubjectIdentityRow, target)
            if source is None or target_row is None or source.edition_id != target_row.edition_id:
                raise ValueError("A subject merge must stay within one edition")
            self._session.add(
                SubjectMergeEventRow(
                    id=event.id,
                    edition_id=event.edition_id,
                    from_subject_id=event.from_subject_id,
                    into_subject_id=target,
                    merge_run_id=event.merge_run_id,
                    actor_id=event.actor_id,
                    reason=event.reason,
                    created_at=event.created_at,
                )
            )
            source.status = DiscoveryIdentityStatus.MERGED.value
            source.merged_into_id = target

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectMergeEvent]:
        rows = await self._session.scalars(
            select(SubjectMergeEventRow)
            .where(SubjectMergeEventRow.edition_id == edition_id)
            .order_by(SubjectMergeEventRow.created_at, SubjectMergeEventRow.id)
        )
        return [
            SubjectMergeEvent(
                id=row.id,
                edition_id=row.edition_id,
                from_subject_id=row.from_subject_id,
                into_subject_id=row.into_subject_id,
                merge_run_id=row.merge_run_id,
                actor_id=row.actor_id,
                reason=row.reason,
                created_at=row.created_at,
            )
            for row in rows
        ]


class SqlAlchemyDiscoverySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, snapshot: DiscoverySnapshot) -> None:
        self._session.add(DiscoverySnapshotRow(**_discovery_snapshot_values(snapshot)))
        await self._session.flush()

    async def get(self, snapshot_id: UUID) -> DiscoverySnapshot | None:
        row = await self._session.get(DiscoverySnapshotRow, snapshot_id)
        return _discovery_snapshot_from_row(row) if row else None

    async def get_for_intake(self, intake_id: UUID) -> DiscoverySnapshot | None:
        row = await self._session.scalar(
            select(DiscoverySnapshotRow).where(
                DiscoverySnapshotRow.intake_id == intake_id,
                DiscoverySnapshotRow.lineage == DiscoverySnapshotLineage.OPERATIONAL.value,
            )
        )
        return _discovery_snapshot_from_row(row) if row else None

    async def get_active(self, edition_id: UUID) -> DiscoverySnapshot | None:
        row = await self._session.scalar(
            select(DiscoverySnapshotRow).where(
                DiscoverySnapshotRow.edition_id == edition_id,
                DiscoverySnapshotRow.lineage == DiscoverySnapshotLineage.OPERATIONAL.value,
                DiscoverySnapshotRow.is_active.is_(True),
            )
        )
        return _discovery_snapshot_from_row(row) if row else None

    async def get_active_for_update(self, edition_id: UUID) -> DiscoverySnapshot | None:
        # Serializes reconciliation even when the edition has no snapshot yet,
        # where a row-level lock alone cannot protect concurrent bootstraps.
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:edition_id))"),
            {"edition_id": str(edition_id)},
        )
        row = await self._session.scalar(
            select(DiscoverySnapshotRow)
            .where(
                DiscoverySnapshotRow.edition_id == edition_id,
                DiscoverySnapshotRow.lineage == DiscoverySnapshotLineage.OPERATIONAL.value,
                DiscoverySnapshotRow.is_active.is_(True),
            )
            .with_for_update()
        )
        return _discovery_snapshot_from_row(row) if row else None

    async def deactivate(self, snapshot_id: UUID) -> None:
        await self._session.execute(
            update(DiscoverySnapshotRow)
            .where(DiscoverySnapshotRow.id == snapshot_id)
            .values(is_active=False)
        )


class SqlAlchemySubjectContributionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, contributions: Sequence[SubjectContribution]) -> None:
        for contribution in contributions:
            await self._session.execute(
                insert(SubjectContributionRow)
                .values(**_subject_contribution_values(contribution))
                .on_conflict_do_nothing(
                    index_elements=[
                        SubjectContributionRow.intake_id,
                        SubjectContributionRow.candidate_key,
                    ]
                )
            )

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SubjectContribution]:
        rows = await self._session.scalars(
            select(SubjectContributionRow)
            .where(SubjectContributionRow.subject_id == subject_id)
            .order_by(SubjectContributionRow.created_at, SubjectContributionRow.id)
        )
        return [_subject_contribution_from_row(row) for row in rows]

    async def list_recent_subject_ids(
        self, edition_id: UUID, *, minimum_snapshot_version: int
    ) -> Sequence[UUID]:
        rows = await self._session.scalars(
            select(SubjectContributionRow.subject_id)
            .join(
                DiscoverySubjectIdentityRow,
                DiscoverySubjectIdentityRow.id == SubjectContributionRow.subject_id,
            )
            .where(
                DiscoverySubjectIdentityRow.edition_id == edition_id,
                SubjectContributionRow.first_seen_version >= minimum_snapshot_version,
            )
            .distinct()
        )
        return list(rows)


class SqlAlchemyEditorialGroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, group: EditorialGroup) -> None:
        self._session.add(EditorialGroupRow(**_editorial_group_values(group)))
        await self._session.flush()

    async def get(self, group_id: UUID) -> EditorialGroup | None:
        row = await self._session.get(EditorialGroupRow, group_id)
        return _editorial_group_from_row(row) if row else None

    async def get_for_update(self, group_id: UUID) -> EditorialGroup | None:
        row = await self._session.scalar(
            select(EditorialGroupRow).where(EditorialGroupRow.id == group_id).with_for_update()
        )
        return _editorial_group_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        rows = await self._session.scalars(
            select(EditorialGroupRow)
            .where(EditorialGroupRow.edition_id == edition_id)
            .order_by(EditorialGroupRow.created_at, EditorialGroupRow.id)
        )
        return [_editorial_group_from_row(row) for row in rows]

    async def list_historical(self, edition_id: UUID) -> Sequence[EditorialGroup]:
        edition = await self._session.get(EditionRow, edition_id)
        if edition is None:
            return []
        rows = await self._session.scalars(
            select(EditorialGroupRow)
            .join(EditionRow, EditionRow.id == EditorialGroupRow.edition_id)
            .where(
                EditionRow.country_code == edition.country_code,
                EditionRow.period_start < edition.period_start,
                EditorialGroupRow.status == EditorialGroupStatus.SELECTED.value,
            )
            .order_by(EditionRow.period_start.desc(), EditorialGroupRow.created_at.desc())
        )
        return [_editorial_group_from_row(row) for row in rows]

    async def get_by_subject(self, subject_id: UUID) -> EditorialGroup | None:
        row = await self._session.scalar(
            select(EditorialGroupRow).where(EditorialGroupRow.subject_id == subject_id)
        )
        return _editorial_group_from_row(row) if row else None

    async def save(self, group: EditorialGroup) -> None:
        row = await self._session.get(EditorialGroupRow, group.id)
        if row is None:
            raise LookupError(f"Editorial group {group.id} does not exist")
        for field_name, value in _editorial_group_values(group).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyHumanDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, decision: HumanDecision) -> None:
        self._session.add(
            HumanDecisionRow(
                id=decision.id,
                edition_id=decision.edition_id,
                decision_type=decision.decision_type.value,
                group_ids=[str(item) for item in decision.group_ids],
                actor_id=decision.actor_id,
                correlation_id=decision.correlation_id,
                payload=decision.payload,
                occurred_at=decision.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[HumanDecision]:
        rows = await self._session.scalars(
            select(HumanDecisionRow)
            .where(HumanDecisionRow.edition_id == edition_id)
            .order_by(HumanDecisionRow.occurred_at, HumanDecisionRow.id)
        )
        return [
            HumanDecision(
                id=row.id,
                edition_id=row.edition_id,
                decision_type=HumanDecisionType(row.decision_type),
                group_ids=tuple(UUID(item) for item in row.group_ids),
                actor_id=row.actor_id,
                correlation_id=row.correlation_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]


class SqlAlchemySourceCollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, collection: SourceCollection) -> bool:
        statement = (
            insert(SourceCollectionRow)
            .values(**_source_collection_values(collection))
            .on_conflict_do_nothing(
                index_elements=[
                    SourceCollectionRow.subject_id,
                    SourceCollectionRow.canonical_url,
                ]
            )
            .returning(SourceCollectionRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        row = await self._session.get(SourceCollectionRow, collection_id)
        return _source_collection_from_row(row) if row else None

    async def get_for_update(self, collection_id: UUID) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow)
            .where(SourceCollectionRow.id == collection_id)
            .with_for_update()
        )
        return _source_collection_from_row(row) if row else None

    async def get_by_canonical_url(
        self, subject_id: UUID, canonical_url: str
    ) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow).where(
                SourceCollectionRow.subject_id == subject_id,
                SourceCollectionRow.canonical_url == canonical_url,
            )
        )
        return _source_collection_from_row(row) if row else None

    async def get_by_candidate(
        self, subject_id: UUID, source_candidate_id: UUID
    ) -> SourceCollection | None:
        row = await self._session.scalar(
            select(SourceCollectionRow).where(
                SourceCollectionRow.subject_id == subject_id,
                SourceCollectionRow.source_candidate_id == source_candidate_id,
            )
        )
        return _source_collection_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[SourceCollection]:
        rows = await self._session.scalars(
            select(SourceCollectionRow)
            .where(SourceCollectionRow.subject_id == subject_id)
            .order_by(SourceCollectionRow.created_at, SourceCollectionRow.id)
        )
        return [_source_collection_from_row(row) for row in rows]

    async def save(self, collection: SourceCollection) -> None:
        row = await self._session.get(SourceCollectionRow, collection.id)
        if row is None:
            raise LookupError(f"Source collection {collection.id} does not exist")
        for field_name, value in _source_collection_values(collection).items():
            setattr(row, field_name, value)
        await self._session.flush()


class SqlAlchemyCollectionAttemptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, attempt: CollectionAttempt) -> None:
        self._session.add(CollectionAttemptRow(**_collection_attempt_values(attempt)))
        await self._session.flush()

    async def list_for_collection(self, collection_id: UUID) -> Sequence[CollectionAttempt]:
        rows = await self._session.scalars(
            select(CollectionAttemptRow)
            .where(CollectionAttemptRow.collection_id == collection_id)
            .order_by(CollectionAttemptRow.attempted_at, CollectionAttemptRow.id)
        )
        return [_collection_attempt_from_row(row) for row in rows]


class SqlAlchemyCollectionPolicySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_if_absent(self, snapshot: CollectionPolicySnapshot) -> bool:
        statement = (
            insert(CollectionPolicySnapshotRow)
            .values(**_policy_snapshot_values(snapshot))
            .on_conflict_do_nothing(index_elements=[CollectionPolicySnapshotRow.id])
            .returning(CollectionPolicySnapshotRow.id)
        )
        return await self._session.scalar(statement) is not None

    async def get(self, snapshot_id: str) -> CollectionPolicySnapshot | None:
        row = await self._session.get(CollectionPolicySnapshotRow, snapshot_id)
        return _policy_snapshot_from_row(row) if row else None


class SqlAlchemyDerivedArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, artifact: DerivedArtifact) -> None:
        self._session.add(DerivedArtifactRow(**_derived_artifact_values(artifact)))
        await self._session.flush()

    async def get(self, artifact_id: UUID) -> DerivedArtifact | None:
        row = await self._session.get(DerivedArtifactRow, artifact_id)
        return _derived_artifact_from_row(row) if row else None


class SqlAlchemyClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, claims: Sequence[Claim]) -> None:
        self._session.add_all([ClaimRow(**_claim_values(claim)) for claim in claims])
        await self._session.flush()

    async def get(self, claim_id: UUID) -> Claim | None:
        row = await self._session.get(ClaimRow, claim_id)
        return _claim_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Claim]:
        rows = await self._session.scalars(
            select(ClaimRow)
            .where(ClaimRow.subject_id == subject_id)
            .order_by(ClaimRow.created_at, ClaimRow.id)
        )
        return [_claim_from_row(row) for row in rows]


class SqlAlchemyIndicatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, indicators: Sequence[Indicator]) -> None:
        self._session.add_all(
            [IndicatorRow(**_indicator_values(indicator)) for indicator in indicators]
        )
        await self._session.flush()

    async def get(self, indicator_id: UUID) -> Indicator | None:
        row = await self._session.get(IndicatorRow, indicator_id)
        return _indicator_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[Indicator]:
        rows = await self._session.scalars(
            select(IndicatorRow)
            .where(IndicatorRow.subject_id == subject_id)
            .order_by(IndicatorRow.created_at, IndicatorRow.id)
        )
        return [_indicator_from_row(row) for row in rows]


class SqlAlchemyRejectedModelProposalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, proposals: Sequence[RejectedModelProposal]) -> None:
        self._session.add_all(
            [RejectedModelProposalRow(**_rejected_proposal_values(item)) for item in proposals]
        )
        await self._session.flush()


class SqlAlchemyBriefEvidencePackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, pack: BriefEvidencePack) -> None:
        self._session.add(BriefEvidencePackRow(**_brief_pack_values(pack)))
        await self._session.flush()

    async def get(self, pack_id: UUID) -> BriefEvidencePack | None:
        row = await self._session.get(BriefEvidencePackRow, pack_id)
        return _brief_pack_from_row(row) if row else None

    async def get_current(self, subject_id: UUID) -> BriefEvidencePack | None:
        row = await self._session.scalar(
            select(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.subject_id == subject_id)
            .order_by(BriefEvidencePackRow.version.desc())
            .limit(1)
        )
        return _brief_pack_from_row(row) if row else None

    async def get_by_hash(self, subject_id: UUID, content_hash: str) -> BriefEvidencePack | None:
        row = await self._session.scalar(
            select(BriefEvidencePackRow).where(
                BriefEvidencePackRow.subject_id == subject_id,
                BriefEvidencePackRow.content_hash == content_hash,
            )
        )
        return _brief_pack_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefEvidencePack]:
        rows = await self._session.scalars(
            select(BriefEvidencePackRow)
            .where(BriefEvidencePackRow.subject_id == subject_id)
            .order_by(BriefEvidencePackRow.version)
        )
        return [_brief_pack_from_row(row) for row in rows]


class SqlAlchemyBriefDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, draft: BriefDraft) -> None:
        self._session.add(BriefDraftRow(**_brief_draft_values(draft)))
        await self._session.flush()

    async def get(self, draft_id: UUID) -> BriefDraft | None:
        row = await self._session.get(BriefDraftRow, draft_id)
        return _brief_draft_from_row(row) if row else None

    async def get_current(self, subject_id: UUID) -> BriefDraft | None:
        row = await self._session.scalar(
            select(BriefDraftRow)
            .where(BriefDraftRow.subject_id == subject_id)
            .order_by(BriefDraftRow.version.desc())
            .limit(1)
        )
        return _brief_draft_from_row(row) if row else None

    async def list_for_subject(self, subject_id: UUID) -> Sequence[BriefDraft]:
        rows = await self._session.scalars(
            select(BriefDraftRow)
            .where(BriefDraftRow.subject_id == subject_id)
            .order_by(BriefDraftRow.version)
        )
        return [_brief_draft_from_row(row) for row in rows]


def _discovery_intake_from_row(row: DiscoveryIntakeRow) -> DiscoveryIntake:
    return DiscoveryIntake(
        id=row.id,
        edition_id=row.edition_id,
        sequence=row.sequence,
        input_mode=DiscoveryInputMode(row.input_mode),
        raw_report_hash=row.raw_report_hash,
        parsed_report_hash=row.parsed_report_hash,
        intake_hash=row.intake_hash,
        research_model_run_id=row.research_model_run_id,
        source_mode=DiscoverySourceMode(row.source_mode),
        complementary_axis=row.complementary_axis,
        batch_id=row.batch_id,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _discovery_merge_run_values(run: DiscoveryMergeRun) -> dict[str, object]:
    return {
        "id": run.id,
        "edition_id": run.edition_id,
        "parent_snapshot_id": run.parent_snapshot_id,
        "intake_id": run.intake_id,
        "planner_kind": run.planner_kind.value,
        "merge_model_run_id": run.merge_model_run_id,
        "prompt_version": run.prompt_version,
        "policy_version": run.policy_version,
        "blocking_version": run.blocking_version,
        "merge_input_hash": run.merge_input_hash,
        "handle_map": run.handle_map,
        "included_subject_ids": [str(value) for value in run.included_subject_ids],
        "excluded_subject_count": run.excluded_subject_count,
        "raw_output_reference": run.raw_output_reference,
        "normalized_output_reference": run.normalized_output_reference,
        "validation_status": run.validation_status.value,
        "warnings": list(run.warnings),
        "review_reasons": list(run.review_reasons),
        "plan_payload": run.plan_payload,
        "supersedes_merge_run_id": run.supersedes_merge_run_id,
        "rebase_count": run.rebase_count,
        "created_at": run.created_at,
    }


def _discovery_merge_run_from_row(row: DiscoveryMergeRunRow) -> DiscoveryMergeRun:
    return DiscoveryMergeRun(
        id=row.id,
        edition_id=row.edition_id,
        parent_snapshot_id=row.parent_snapshot_id,
        intake_id=row.intake_id,
        planner_kind=DiscoveryPlannerKind(row.planner_kind),
        merge_model_run_id=row.merge_model_run_id,
        prompt_version=row.prompt_version,
        policy_version=row.policy_version,
        blocking_version=row.blocking_version,
        merge_input_hash=row.merge_input_hash,
        handle_map=row.handle_map,
        included_subject_ids=tuple(UUID(value) for value in row.included_subject_ids),
        excluded_subject_count=row.excluded_subject_count,
        raw_output_reference=row.raw_output_reference,
        normalized_output_reference=row.normalized_output_reference,
        validation_status=MergeValidationStatus(row.validation_status),
        warnings=tuple(row.warnings),
        review_reasons=tuple(row.review_reasons),
        plan_payload=row.plan_payload,
        supersedes_merge_run_id=row.supersedes_merge_run_id,
        rebase_count=row.rebase_count,
        created_at=row.created_at,
    )


def _discovery_identity_values(identity: DiscoverySubjectIdentity) -> dict[str, object]:
    return {
        "id": identity.id,
        "edition_id": identity.edition_id,
        "origin_key": identity.origin_key,
        "cross_edition_lineage_id": identity.cross_edition_lineage_id,
        "created_by_merge_run_id": identity.created_by_merge_run_id,
        "status": identity.status.value,
        "merged_into_id": identity.merged_into_id,
        "created_at": identity.created_at,
    }


def _discovery_identity_from_row(
    row: DiscoverySubjectIdentityRow,
) -> DiscoverySubjectIdentity:
    return DiscoverySubjectIdentity(
        id=row.id,
        edition_id=row.edition_id,
        origin_key=row.origin_key,
        cross_edition_lineage_id=row.cross_edition_lineage_id,
        created_by_merge_run_id=row.created_by_merge_run_id,
        status=DiscoveryIdentityStatus(row.status),
        merged_into_id=row.merged_into_id,
        created_at=row.created_at,
    )


def _discovery_snapshot_values(snapshot: DiscoverySnapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "edition_id": snapshot.edition_id,
        "version": snapshot.version,
        "parent_snapshot_id": snapshot.parent_snapshot_id,
        "intake_id": snapshot.intake_id,
        "merge_run_id": snapshot.merge_run_id,
        "planner_kind": snapshot.planner_kind.value,
        "lineage": snapshot.lineage.value,
        "replay_run_id": snapshot.replay_run_id,
        "subjects": [
            {
                "subject_id": str(subject.subject_id),
                "candidate": _candidate_payload(subject.candidate),
                "member_references": [
                    {"batch_id": str(ref.batch_id), "candidate_id": str(ref.candidate_id)}
                    for ref in subject.member_references
                ],
                "created_at": subject.created_at.isoformat(),
            }
            for subject in snapshot.subjects
        ],
        "snapshot_hash": snapshot.snapshot_hash,
        "is_active": snapshot.is_active,
        "created_at": snapshot.created_at,
    }


def _discovery_snapshot_from_row(row: DiscoverySnapshotRow) -> DiscoverySnapshot:
    return DiscoverySnapshot(
        id=row.id,
        edition_id=row.edition_id,
        version=row.version,
        parent_snapshot_id=row.parent_snapshot_id,
        intake_id=row.intake_id,
        merge_run_id=row.merge_run_id,
        planner_kind=DiscoveryPlannerKind(row.planner_kind),
        lineage=DiscoverySnapshotLineage(row.lineage),
        replay_run_id=row.replay_run_id,
        subjects=tuple(
            DiscoverySubject(
                subject_id=UUID(value["subject_id"]),
                candidate=_candidate_from_payload(value["candidate"]),
                member_references=tuple(
                    DiscoveryMemberReference(
                        batch_id=UUID(reference["batch_id"]),
                        candidate_id=UUID(reference["candidate_id"]),
                    )
                    for reference in value["member_references"]
                ),
                created_at=datetime.fromisoformat(value["created_at"]),
            )
            for value in row.subjects
        ),
        snapshot_hash=row.snapshot_hash,
        is_active=row.is_active,
        created_at=row.created_at,
    )


def _subject_contribution_values(contribution: SubjectContribution) -> dict[str, object]:
    return {
        "id": contribution.id,
        "subject_id": contribution.subject_id,
        "intake_id": contribution.intake_id,
        "candidate_key": contribution.candidate_key,
        "candidate_id": contribution.candidate_id,
        "first_seen_snapshot_id": contribution.first_seen_snapshot_id,
        "first_seen_version": contribution.first_seen_version,
        "contributed_title": contribution.contributed_title,
        "contributed_summary": contribution.contributed_summary,
        "contributed_source_ids": [str(value) for value in contribution.contributed_source_ids],
        "contributed_provisional_ioc_ids": [
            str(value) for value in contribution.contributed_provisional_ioc_ids
        ],
        "merge_run_id": contribution.merge_run_id,
        "merge_group_index": contribution.merge_group_index,
        "created_at": contribution.created_at,
    }


def _subject_contribution_from_row(row: SubjectContributionRow) -> SubjectContribution:
    return SubjectContribution(
        id=row.id,
        subject_id=row.subject_id,
        intake_id=row.intake_id,
        candidate_key=row.candidate_key,
        candidate_id=row.candidate_id,
        first_seen_snapshot_id=row.first_seen_snapshot_id,
        first_seen_version=row.first_seen_version,
        contributed_title=row.contributed_title,
        contributed_summary=row.contributed_summary,
        contributed_source_ids=tuple(UUID(value) for value in row.contributed_source_ids),
        contributed_provisional_ioc_ids=tuple(
            UUID(value) for value in row.contributed_provisional_ioc_ids
        ),
        merge_run_id=row.merge_run_id,
        merge_group_index=row.merge_group_index,
        created_at=row.created_at,
    )


def _editorial_group_values(group: EditorialGroup) -> dict[str, object]:
    return {
        "id": group.id,
        "edition_id": group.edition_id,
        "title": group.title,
        "outcome": group.outcome.value,
        "status": group.status.value,
        "source_relationship_status": group.source_relationship_status.value,
        "needs_source_verification": group.needs_source_verification,
        "needs_source_expansion": group.needs_source_expansion,
        "grouping_confidence": group.grouping_confidence.value,
        "grouping_justification": group.grouping_justification,
        "potential_historical_group_id": group.potential_historical_group_id,
        "editorial_type": group.editorial_type.value if group.editorial_type else None,
        "subject_id": group.subject_id,
        "discovery_subject_id": group.discovery_subject_id,
        "payload": {
            "candidate_references": [
                {"batch_id": str(item.batch_id), "candidate_id": str(item.candidate_id)}
                for item in group.candidate_references
            ],
            "score": {
                "impact": group.score.impact,
                "novelty": group.score.novelty,
                "technical_depth": group.score.technical_depth,
                "hunting_potential": group.score.hunting_potential,
                "actionability": group.score.actionability,
                "source_quality": group.score.source_quality,
                "justifications": group.score.justifications,
            },
        },
        "version": group.version,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _editorial_group_from_row(row: EditorialGroupRow) -> EditorialGroup:
    payload = row.payload
    score = cast(dict[str, Any], payload["score"])
    references = cast(list[dict[str, str]], payload["candidate_references"])
    return EditorialGroup(
        id=row.id,
        edition_id=row.edition_id,
        title=row.title,
        candidate_references=tuple(
            CandidateReference(UUID(item["batch_id"]), UUID(item["candidate_id"]))
            for item in references
        ),
        outcome=GroupingOutcome(row.outcome),
        status=EditorialGroupStatus(row.status),
        score=EditorialScore(
            impact=int(score["impact"]),
            novelty=int(score["novelty"]),
            technical_depth=int(score["technical_depth"]),
            hunting_potential=int(score["hunting_potential"]),
            actionability=int(score["actionability"]),
            source_quality=int(score["source_quality"]),
            justifications=cast(dict[str, str], score["justifications"]),
        ),
        source_relationship_status=SourceRelationshipStatus(row.source_relationship_status),
        needs_source_verification=row.needs_source_verification,
        needs_source_expansion=row.needs_source_expansion,
        grouping_confidence=GroupingConfidence(row.grouping_confidence),
        grouping_justification=row.grouping_justification,
        potential_historical_group_id=row.potential_historical_group_id,
        discovery_subject_id=row.discovery_subject_id,
        editorial_type=EditorialType(row.editorial_type) if row.editorial_type else None,
        subject_id=row.subject_id,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _source_collection_values(collection: SourceCollection) -> dict[str, object]:
    return {
        "id": collection.id,
        "subject_id": collection.subject_id,
        "edition_id": collection.edition_id,
        "group_id": collection.group_id,
        "batch_id": collection.batch_id,
        "source_candidate_id": collection.source_candidate_id,
        "origin_kind": collection.origin_kind.value,
        "requested_url": collection.requested_url,
        "canonical_url": collection.canonical_url,
        "title": collection.title,
        "publisher": collection.publisher,
        "published_at": collection.published_at,
        "source_tlp": collection.source_tlp.value,
        "sensitivity": collection.sensitivity,
        "external_llm_allowed": collection.external_llm_allowed,
        "do_not_submit": collection.do_not_submit,
        "proposed_role": collection.proposed_role.value,
        "relationship_status": collection.relationship_status.value,
        "relationship_evidence": collection.relationship_evidence,
        "state": collection.state.value,
        "source_document_id": collection.source_document_id,
        "decoded_blob_id": collection.decoded_blob_id,
        "latest_attempt_id": collection.latest_attempt_id,
        "derived_artifact_id": collection.derived_artifact_id,
        "fetch_job_id": collection.fetch_job_id,
        "fetch_policy_snapshot_id": collection.fetch_policy_snapshot_id,
        "fetch_started_at": collection.fetch_started_at,
        "fetch_lease_expires_at": collection.fetch_lease_expires_at,
        "error_reason": collection.error_reason,
        "attempt_count": collection.attempt_count,
        "created_at": collection.created_at,
        "updated_at": collection.updated_at,
    }


def _source_collection_from_row(row: SourceCollectionRow) -> SourceCollection:
    return SourceCollection(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        batch_id=row.batch_id,
        source_candidate_id=row.source_candidate_id,
        origin_kind=SourceOriginKind(row.origin_kind),
        requested_url=row.requested_url,
        canonical_url=row.canonical_url,
        title=row.title,
        publisher=row.publisher,
        published_at=row.published_at,
        source_tlp=TLP(row.source_tlp),
        sensitivity=row.sensitivity,
        external_llm_allowed=row.external_llm_allowed,
        do_not_submit=row.do_not_submit,
        proposed_role=SourceRole(row.proposed_role),
        relationship_status=SourceRelationshipStatus(row.relationship_status),
        relationship_evidence=row.relationship_evidence,
        state=CollectionState(row.state),
        source_document_id=row.source_document_id,
        decoded_blob_id=row.decoded_blob_id,
        latest_attempt_id=row.latest_attempt_id,
        derived_artifact_id=row.derived_artifact_id,
        fetch_job_id=row.fetch_job_id,
        fetch_policy_snapshot_id=row.fetch_policy_snapshot_id,
        fetch_started_at=row.fetch_started_at,
        fetch_lease_expires_at=row.fetch_lease_expires_at,
        error_reason=row.error_reason,
        attempt_count=row.attempt_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _collection_attempt_values(attempt: CollectionAttempt) -> dict[str, object]:
    return {
        "id": attempt.id,
        "collection_id": attempt.collection_id,
        "job_id": attempt.job_id,
        "configuration_id": attempt.policy_snapshot_id,
        "policy_snapshot_id": attempt.policy_snapshot_id,
        "requested_url": attempt.requested_url,
        "final_url": attempt.final_url,
        "redirect_chain": list(attempt.redirect_chain),
        "attempted_at": attempt.attempted_at,
        "completed_at": attempt.completed_at,
        "http_status": attempt.http_status,
        "declared_content_type": attempt.declared_content_type,
        "detected_content_type": attempt.detected_content_type,
        "size": attempt.encoded_size,
        "sha256": attempt.encoded_sha256,
        "encoded_size": attempt.encoded_size,
        "encoded_sha256": attempt.encoded_sha256,
        "decoded_size": attempt.decoded_size,
        "decoded_sha256": attempt.decoded_sha256,
        "content_encoding": attempt.content_encoding,
        "allowed_headers": attempt.allowed_headers,
        "outcome": attempt.outcome.value,
        "failure_reason": attempt.failure_reason,
    }


def _collection_attempt_from_row(row: CollectionAttemptRow) -> CollectionAttempt:
    return CollectionAttempt(
        id=row.id,
        collection_id=row.collection_id,
        job_id=row.job_id,
        policy_snapshot_id=row.policy_snapshot_id,
        requested_url=row.requested_url,
        final_url=row.final_url,
        redirect_chain=tuple(row.redirect_chain),
        attempted_at=row.attempted_at,
        completed_at=row.completed_at,
        http_status=row.http_status,
        declared_content_type=row.declared_content_type,
        detected_content_type=row.detected_content_type,
        encoded_size=row.encoded_size,
        encoded_sha256=row.encoded_sha256,
        decoded_size=row.decoded_size,
        decoded_sha256=row.decoded_sha256,
        content_encoding=row.content_encoding,
        allowed_headers=row.allowed_headers,
        outcome=AttemptOutcome(row.outcome),
        failure_reason=row.failure_reason,
    )


def _derived_artifact_values(artifact: DerivedArtifact) -> dict[str, object]:
    return {
        "id": artifact.id,
        "source_document_id": artifact.source_document_id,
        "text_blob_id": artifact.text_blob_id,
        "parser_name": artifact.parser_name,
        "parser_version": artifact.parser_version,
        "text_length": artifact.text_length,
        "publication_metadata": artifact.publication_metadata,
        "created_at": artifact.created_at,
    }


def _derived_artifact_from_row(row: DerivedArtifactRow) -> DerivedArtifact:
    return DerivedArtifact(
        id=row.id,
        source_document_id=row.source_document_id,
        text_blob_id=row.text_blob_id,
        parser_name=row.parser_name,
        parser_version=row.parser_version,
        text_length=row.text_length,
        publication_metadata=row.publication_metadata,
        created_at=row.created_at,
    )


def _claim_values(claim: Claim) -> dict[str, object]:
    return {
        "id": claim.id,
        "subject_id": claim.subject_id,
        "edition_id": claim.edition_id,
        "group_id": claim.group_id,
        "source_document_id": claim.source_document_id,
        "derived_artifact_id": claim.derived_artifact_id,
        "kind": claim.kind.value,
        "value": claim.value,
        "span_start": claim.span.start,
        "span_end": claim.span.end,
        "extraction_method": claim.extraction_method,
        "extraction_payload": claim.extraction_payload,
        "chunk_id": claim.chunk_id,
        "local_span_start": claim.local_span.start if claim.local_span else None,
        "local_span_end": claim.local_span.end if claim.local_span else None,
        "model_run_id": claim.model_run_id,
        "created_at": claim.created_at,
    }


def _claim_from_row(row: ClaimRow) -> Claim:
    return Claim(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        source_document_id=row.source_document_id,
        derived_artifact_id=row.derived_artifact_id,
        kind=ClaimKind(row.kind),
        value=row.value,
        span=SourceSpan(row.span_start, row.span_end),
        extraction_method=row.extraction_method,
        extraction_payload=row.extraction_payload,
        chunk_id=row.chunk_id,
        local_span=(
            SourceSpan(row.local_span_start, row.local_span_end)
            if row.local_span_start is not None and row.local_span_end is not None
            else None
        ),
        model_run_id=row.model_run_id,
        created_at=row.created_at,
    )


def _indicator_values(indicator: Indicator) -> dict[str, object]:
    return {
        "id": indicator.id,
        "subject_id": indicator.subject_id,
        "edition_id": indicator.edition_id,
        "group_id": indicator.group_id,
        "source_document_id": indicator.source_document_id,
        "derived_artifact_id": indicator.derived_artifact_id,
        "kind": indicator.kind.value,
        "original_value": indicator.original_value,
        "normalized_value": indicator.normalized_value,
        "span_start": indicator.span.start,
        "span_end": indicator.span.end,
        "created_at": indicator.created_at,
    }


def _indicator_from_row(row: IndicatorRow) -> Indicator:
    return Indicator(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        source_document_id=row.source_document_id,
        derived_artifact_id=row.derived_artifact_id,
        kind=IndicatorKind(row.kind),
        original_value=row.original_value,
        normalized_value=row.normalized_value,
        span=SourceSpan(row.span_start, row.span_end),
        created_at=row.created_at,
    )


def _brief_pack_values(pack: BriefEvidencePack) -> dict[str, object]:
    return {
        "id": pack.id,
        "subject_id": pack.subject_id,
        "edition_id": pack.edition_id,
        "group_id": pack.group_id,
        "version": pack.version,
        "content_hash": pack.content_hash,
        "object_hashes": list(pack.object_hashes),
        "sources": list(pack.sources),
        "claims": list(pack.claims),
        "indicators": list(pack.indicators),
        "normalized_entities": list(pack.normalized_entities),
        "uncertainties": list(pack.uncertainties),
        "human_decisions": list(pack.human_decisions),
        "blob_id": pack.blob_id,
        "created_by": pack.created_by,
        "created_at": pack.created_at,
        "built_from_snapshot_id": pack.built_from_snapshot_id,
        "built_from_snapshot_version": pack.built_from_snapshot_version,
        "covered_contribution_ids": [str(value) for value in pack.covered_contribution_ids],
        "scope": pack.scope.value,
        "base_pack_id": pack.base_pack_id,
    }


def _brief_pack_from_row(row: BriefEvidencePackRow) -> BriefEvidencePack:
    return BriefEvidencePack(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        version=row.version,
        content_hash=row.content_hash,
        object_hashes=tuple(row.object_hashes),
        sources=tuple(row.sources),
        claims=tuple(row.claims),
        indicators=tuple(row.indicators),
        normalized_entities=tuple(row.normalized_entities),
        uncertainties=tuple(row.uncertainties),
        human_decisions=tuple(row.human_decisions),
        blob_id=row.blob_id,
        created_by=row.created_by,
        created_at=row.created_at,
        built_from_snapshot_id=row.built_from_snapshot_id,
        built_from_snapshot_version=row.built_from_snapshot_version,
        covered_contribution_ids=tuple(UUID(value) for value in row.covered_contribution_ids or ()),
        scope=EvidencePackScope(row.scope or "full"),
        base_pack_id=row.base_pack_id,
    )


def _brief_draft_values(draft: BriefDraft) -> dict[str, object]:
    return {
        "id": draft.id,
        "subject_id": draft.subject_id,
        "edition_id": draft.edition_id,
        "group_id": draft.group_id,
        "pack_id": draft.pack_id,
        "pack_hash": draft.pack_hash,
        "version": draft.version,
        "title": draft.title,
        "blocks": [
            {
                "id": str(block.id),
                "sentences": [
                    {
                        "id": str(sentence.id),
                        "text": sentence.text,
                        "factual": sentence.factual,
                        "claim_ids": [str(item) for item in sentence.claim_ids],
                        "indicator_ids": [str(item) for item in sentence.indicator_ids],
                    }
                    for sentence in block.sentences
                ],
            }
            for block in draft.blocks
        ],
        "limits": list(draft.limits),
        "source_ids": [str(item) for item in draft.source_ids],
        "model_run_id": draft.model_run_id,
        "provider": draft.provider,
        "status": draft.status.value,
        "parent_draft_id": draft.parent_draft_id,
        "regenerated_block_id": draft.regenerated_block_id,
        "created_at": draft.created_at,
    }


def _brief_draft_from_row(row: BriefDraftRow) -> BriefDraft:
    return BriefDraft(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        group_id=row.group_id,
        pack_id=row.pack_id,
        pack_hash=row.pack_hash,
        version=row.version,
        title=row.title,
        blocks=tuple(
            BriefBlock(
                id=UUID(str(block["id"])),
                sentences=tuple(
                    BriefSentence(
                        id=UUID(str(sentence["id"])),
                        text=str(sentence["text"]),
                        factual=bool(sentence["factual"]),
                        claim_ids=tuple(UUID(str(item)) for item in sentence["claim_ids"]),
                        indicator_ids=tuple(UUID(str(item)) for item in sentence["indicator_ids"]),
                    )
                    for sentence in block["sentences"]
                ),
            )
            for block in row.blocks
        ),
        limits=tuple(row.limits),
        source_ids=tuple(UUID(item) for item in row.source_ids),
        model_run_id=row.model_run_id,
        provider=row.provider,
        status=BriefDraftStatus(row.status),
        parent_draft_id=row.parent_draft_id,
        regenerated_block_id=row.regenerated_block_id,
        created_at=row.created_at,
    )


def _policy_snapshot_values(snapshot: CollectionPolicySnapshot) -> dict[str, object]:
    return {
        "id": snapshot.id,
        "max_redirects": snapshot.max_redirects,
        "timeout_seconds": snapshot.timeout_seconds,
        "max_download_bytes": snapshot.max_download_bytes,
        "max_expanded_bytes": snapshot.max_expanded_bytes,
        "max_decompression_ratio": snapshot.max_decompression_ratio,
        "user_agent": snapshot.user_agent,
        "allowed_domains": list(snapshot.allowed_domains),
        "blocked_domains": list(snapshot.blocked_domains),
        "collector_version": snapshot.collector_version,
        "extraction_limits": snapshot.extraction_limits,
        "created_at": snapshot.created_at,
    }


def _policy_snapshot_from_row(row: CollectionPolicySnapshotRow) -> CollectionPolicySnapshot:
    return CollectionPolicySnapshot(
        id=row.id,
        max_redirects=row.max_redirects,
        timeout_seconds=row.timeout_seconds,
        max_download_bytes=row.max_download_bytes,
        max_expanded_bytes=row.max_expanded_bytes,
        max_decompression_ratio=row.max_decompression_ratio,
        user_agent=row.user_agent,
        allowed_domains=tuple(row.allowed_domains),
        blocked_domains=tuple(row.blocked_domains),
        collector_version=row.collector_version,
        extraction_limits=row.extraction_limits,
        created_at=row.created_at,
    )


def _rejected_proposal_values(proposal: RejectedModelProposal) -> dict[str, object]:
    return {
        "id": proposal.id,
        "source_document_id": proposal.source_document_id,
        "derived_artifact_id": proposal.derived_artifact_id,
        "chunk_id": proposal.chunk_id,
        "category": proposal.category,
        "requested_kind": proposal.requested_kind,
        "reason": proposal.reason,
        "proposal_hash": proposal.proposal_hash,
        "model_run_id": proposal.model_run_id,
        "created_at": proposal.created_at,
    }


class SqlAlchemySubjectProductionRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, run: SubjectProductionRun) -> None:
        row = SubjectProductionRunRow(
            id=run.id,
            subject_id=run.subject_id,
            edition_id=run.edition_id,
            profile=run.profile.value,
            status=run.status.value,
            current_stage=run.current_stage.value,
            conversation_id=run.conversation_id,
            run_number=run.run_number,
            research_date=run.research_date,
            error_code=run.error_code,
            error_message=run.error_message,
            error_details=run.error_details,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
            updated_at=run.updated_at,
            version=run.version,
        )
        self._session.add(row)

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        row = await self._session.get(SubjectProductionRunRow, run_id)
        return _subject_production_run_from_row(row) if row else None

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.id == run_id)
            .with_for_update()
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def save(self, run: SubjectProductionRun) -> None:
        stmt = (
            update(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.id == run.id)
            .values(
                status=run.status.value,
                current_stage=run.current_stage.value,
                conversation_id=run.conversation_id,
                research_date=run.research_date,
                error_code=run.error_code,
                error_message=run.error_message,
                error_details=run.error_details,
                started_at=run.started_at,
                finished_at=run.finished_at,
                updated_at=run.updated_at,
                version=run.version,
            )
        )
        await self._session.execute(stmt)

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.subject_id == subject_id)
            .order_by(SubjectProductionRunRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _subject_production_run_from_row(row) if row else None

    async def list_for_edition(self, edition_id: UUID) -> Sequence[SubjectProductionRun]:
        query = (
            select(SubjectProductionRunRow)
            .where(SubjectProductionRunRow.edition_id == edition_id)
            .order_by(SubjectProductionRunRow.created_at)
        )
        result = await self._session.execute(query)
        return [_subject_production_run_from_row(row) for row in result.scalars()]


class SqlAlchemyProductionArtifactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, artifact: ProductionArtifact) -> None:
        row = ProductionArtifactRow(
            id=artifact.id,
            production_run_id=artifact.production_run_id,
            subject_id=artifact.subject_id,
            stage=artifact.stage.value,
            version=artifact.version,
            input_hash=artifact.input_hash,
            status=artifact.status.value,
            raw_blob_id=artifact.raw_blob_id,
            canonical_blob_id=artifact.canonical_blob_id,
            rendered_blob_id=artifact.rendered_blob_id,
            model_run_id=artifact.model_run_id,
            conversation_turn_id=artifact.conversation_turn_id,
            artifact_metadata=artifact.metadata,
            created_at=artifact.created_at,
        )
        self._session.add(row)

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        row = await self._session.get(ProductionArtifactRow, artifact_id)
        return _production_artifact_from_row(row) if row else None

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        query = (
            select(ProductionArtifactRow)
            .where(
                (ProductionArtifactRow.production_run_id == run_id)
                & (ProductionArtifactRow.stage == stage)
                & (ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value)
            )
            .order_by(ProductionArtifactRow.version.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _production_artifact_from_row(row) if row else None

    async def list_for_run(self, run_id: UUID) -> Sequence[ProductionArtifact]:
        query = (
            select(ProductionArtifactRow)
            .where(ProductionArtifactRow.production_run_id == run_id)
            .order_by(ProductionArtifactRow.stage, ProductionArtifactRow.version)
        )
        result = await self._session.execute(query)
        return [_production_artifact_from_row(row) for row in result.scalars()]

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        # Get stage ordering
        stages = [
            ProductionArtifactStage.REFERENCES.value,
            ProductionArtifactStage.EXTRACTION.value,
            ProductionArtifactStage.SYNTHESIS.value,
            ProductionArtifactStage.BRIEF.value,
        ]
        if stage not in stages:
            return

        stage_idx = stages.index(stage)
        downstream_stages = stages[stage_idx + 1 :]

        if downstream_stages:
            stmt = (
                update(ProductionArtifactRow)
                .where(
                    (ProductionArtifactRow.production_run_id == run_id)
                    & (ProductionArtifactRow.stage.in_(downstream_stages))
                )
                .values(status=ProductionArtifactStatus.STALE.value)
            )
            await self._session.execute(stmt)


class SqlAlchemyEditionProductionBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, batch: EditionProductionBatch) -> None:
        row = EditionProductionBatchRow(
            id=batch.id,
            edition_id=batch.edition_id,
            profile=batch.profile.value,
            status=batch.status,
            created_at=batch.created_at,
            started_at=batch.started_at,
            finished_at=batch.finished_at,
            version=batch.version,
        )
        self._session.add(row)

    async def get(self, batch_id: UUID) -> EditionProductionBatch | None:
        row = await self._session.get(EditionProductionBatchRow, batch_id)
        return _edition_production_batch_from_row(row) if row else None

    async def get_for_update(self, batch_id: UUID) -> EditionProductionBatch | None:
        query = (
            select(EditionProductionBatchRow)
            .where(EditionProductionBatchRow.id == batch_id)
            .with_for_update()
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None

    async def get_latest_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        query = (
            select(EditionProductionBatchRow)
            .where(EditionProductionBatchRow.edition_id == edition_id)
            .order_by(EditionProductionBatchRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None

    async def save(self, batch: EditionProductionBatch) -> None:
        stmt = (
            update(EditionProductionBatchRow)
            .where(EditionProductionBatchRow.id == batch.id)
            .values(
                status=batch.status,
                started_at=batch.started_at,
                finished_at=batch.finished_at,
                version=batch.version,
            )
        )
        await self._session.execute(stmt)

    async def get_active_for_edition(self, edition_id: UUID) -> EditionProductionBatch | None:
        query = (
            select(EditionProductionBatchRow)
            .where(
                (EditionProductionBatchRow.edition_id == edition_id)
                & (EditionProductionBatchRow.status.in_(["queued", "running"]))
            )
            .order_by(EditionProductionBatchRow.created_at.desc())
            .limit(1)
        )
        result = await self._session.execute(query)
        row = result.scalar_one_or_none()
        return _edition_production_batch_from_row(row) if row else None


class SqlAlchemyEditionProductionBatchItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_many(self, items: Sequence[EditionProductionBatchItem]) -> None:
        for item in items:
            row = EditionProductionBatchItemRow(
                id=item.id,
                batch_id=item.batch_id,
                subject_id=item.subject_id,
                production_run_id=item.production_run_id,
                position=item.position,
                created_at=item.created_at,
            )
            self._session.add(row)

    async def list_for_batch(self, batch_id: UUID) -> Sequence[EditionProductionBatchItem]:
        query = (
            select(EditionProductionBatchItemRow)
            .where(EditionProductionBatchItemRow.batch_id == batch_id)
            .order_by(EditionProductionBatchItemRow.position)
        )
        result = await self._session.execute(query)
        return [_edition_production_batch_item_from_row(row) for row in result.scalars()]

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        query = select(EditionProductionBatchItemRow).where(
            EditionProductionBatchItemRow.production_run_id == run_id
        )
        result = await self._session.execute(query)
        row = result.scalars().first()
        return _edition_production_batch_item_from_row(row) if row else None


def _subject_production_run_from_row(row: SubjectProductionRunRow) -> SubjectProductionRun:
    from cti_app.domain.production import (
        ProductionProfile,
        SubjectProductionRun,
        SubjectProductionStage,
        SubjectProductionStatus,
    )

    return SubjectProductionRun(
        id=row.id,
        subject_id=row.subject_id,
        edition_id=row.edition_id,
        profile=ProductionProfile(row.profile),
        status=SubjectProductionStatus(row.status),
        current_stage=SubjectProductionStage(row.current_stage),
        conversation_id=row.conversation_id,
        run_number=row.run_number,
        research_date=row.research_date,
        error_code=row.error_code,
        error_message=row.error_message,
        error_details=row.error_details,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
    )


def _production_artifact_from_row(row: ProductionArtifactRow) -> ProductionArtifact:
    from cti_app.domain.production import (
        ProductionArtifact,
        ProductionArtifactStage,
        ProductionArtifactStatus,
    )

    return ProductionArtifact(
        id=row.id,
        production_run_id=row.production_run_id,
        subject_id=row.subject_id,
        stage=ProductionArtifactStage(row.stage),
        version=row.version,
        input_hash=row.input_hash,
        status=ProductionArtifactStatus(row.status),
        raw_blob_id=row.raw_blob_id,
        canonical_blob_id=row.canonical_blob_id,
        rendered_blob_id=row.rendered_blob_id,
        model_run_id=row.model_run_id,
        conversation_turn_id=row.conversation_turn_id,
        metadata=row.artifact_metadata,
        created_at=row.created_at,
    )


def _edition_production_batch_from_row(row: EditionProductionBatchRow) -> EditionProductionBatch:
    from cti_app.domain.production import (
        EditionProductionBatch,
        ProductionProfile,
    )

    return EditionProductionBatch(
        id=row.id,
        edition_id=row.edition_id,
        profile=ProductionProfile(row.profile),
        status=row.status,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        version=row.version,
    )


def _edition_production_batch_item_from_row(
    row: EditionProductionBatchItemRow,
) -> EditionProductionBatchItem:
    from cti_app.domain.production import EditionProductionBatchItem

    return EditionProductionBatchItem(
        id=row.id,
        batch_id=row.batch_id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        position=row.position,
        created_at=row.created_at,
    )


