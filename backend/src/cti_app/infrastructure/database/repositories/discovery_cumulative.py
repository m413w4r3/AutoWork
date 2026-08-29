from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.discovery import DiscoverySourceMode
from cti_app.domain.discovery_cumulative import (
    DiscoveryIdentityStatus,
    DiscoveryInputMode,
    DiscoveryIntake,
    DiscoveryMemberReference,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
    SubjectContribution,
    SubjectMergeEvent,
)
from cti_app.infrastructure.database.models.discovery import (
    DiscoveryIntakeRow,
    DiscoveryMergeRunRow,
    DiscoverySnapshotRow,
    DiscoverySubjectIdentityRow,
    SubjectContributionRow,
    SubjectMergeEventRow,
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
                DiscoveryMergeRunRow.validation_status == MergeValidationStatus.NEEDS_REVIEW.value,
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
            select(DiscoverySnapshotRow).where(DiscoverySnapshotRow.intake_id == intake_id)
        )
        return _discovery_snapshot_from_row(row) if row else None

    async def get_active(self, edition_id: UUID) -> DiscoverySnapshot | None:
        row = await self._session.scalar(
            select(DiscoverySnapshotRow).where(
                DiscoverySnapshotRow.edition_id == edition_id,
                DiscoverySnapshotRow.is_active.is_(True),
            )
        )
        return _discovery_snapshot_from_row(row) if row else None

    async def get_active_for_update(self, edition_id: UUID) -> DiscoverySnapshot | None:
        # Advisory lock: serializes reconciliation even pre-first-snapshot, when
        # there's no row yet for a row-level lock to protect concurrent bootstraps.
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:edition_id))"),
            {"edition_id": str(edition_id)},
        )
        row = await self._session.scalar(
            select(DiscoverySnapshotRow)
            .where(
                DiscoverySnapshotRow.edition_id == edition_id,
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
