"""PostgreSQL repositories for the canonical edition composition."""

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import delete, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.edition_composition import (
    CompositionReferenceSnapshot,
    CompositionReferenceState,
)
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    CompositionMutationKind,
    CompositionPreparationStatus,
    EditionComposition,
    EditionCompositionEntry,
    EditionCompositionEvent,
)
from cti_app.domain.production import ProductionArtifactStatus, ProductionRunStatus
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.discovery import (
    DiscoverySnapshotRow,
    DiscoverySubjectIdentityRow,
)
from cti_app.infrastructure.database.models.edition_composition import (
    EditionCompositionEntryRow,
    EditionCompositionEventRow,
    EditionCompositionRow,
)
from cti_app.infrastructure.database.models.production import (
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    ProductionInputSnapshotRow,
    ProductionRunRow,
)
from cti_app.infrastructure.database.models.publication_review import (
    PublicationReviewDecisionRow,
)


class SqlAlchemyEditionCompositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_edition(self, edition_id: UUID) -> EditionComposition | None:
        row = await self._session.scalar(
            select(EditionCompositionRow).where(EditionCompositionRow.edition_id == edition_id)
        )
        if row is None:
            return None
        entries = await self._load_entries(row.id)
        return _composition_from_rows(row, entries)

    async def get_for_edition_for_update(self, edition_id: UUID) -> EditionComposition | None:
        row = await self._session.scalar(
            select(EditionCompositionRow)
            .where(EditionCompositionRow.edition_id == edition_id)
            .with_for_update()
        )
        if row is None:
            return None
        entries = await self._load_entries(row.id, lock=True)
        return _composition_from_rows(row, entries)

    async def save(self, composition: EditionComposition) -> None:
        row = await self._session.get(EditionCompositionRow, composition.id)
        values = {
            "edition_id": composition.edition_id,
            "version": composition.version,
            "preparation_status": composition.preparation_status.value,
            "actor_id": composition.actor_id,
            "created_at": composition.created_at,
            "updated_at": composition.updated_at,
        }
        if row is None:
            self._session.add(EditionCompositionRow(id=composition.id, **values))
        else:
            for name, value in values.items():
                setattr(row, name, value)
        await self._session.flush()

    async def replace_entries(
        self, composition_id: UUID, entries: Sequence[EditionCompositionEntry]
    ) -> None:
        await self._session.execute(
            delete(EditionCompositionEntryRow).where(
                EditionCompositionEntryRow.composition_id == composition_id
            )
        )
        await self._session.flush()
        self._session.add_all(
            [
                EditionCompositionEntryRow(
                    id=entry.id,
                    composition_id=composition_id,
                    subject_id=entry.subject_id,
                    disposition=entry.disposition.value,
                    position=entry.position,
                    production_run_id=entry.production_run_id,
                    pipeline_generation=entry.pipeline_generation,
                    artifact_id=entry.artifact_id,
                    artifact_version=entry.artifact_version,
                    artifact_input_hash=entry.artifact_input_hash,
                    provenance_batch_id=entry.provenance_batch_id,
                    review_decision_id=entry.review_decision_id,
                    document_sha256=entry.document_sha256,
                    preparation_status=entry.preparation_status.value,
                )
                for entry in entries
            ]
        )
        await self._session.flush()

    async def list_subject_ids(self, edition_id: UUID) -> Sequence[UUID]:
        return tuple(
            await self._session.scalars(
                select(SubjectRow.id)
                .where(SubjectRow.edition_id == edition_id)
                .order_by(SubjectRow.id)
            )
        )

    async def validate_references(
        self,
        edition_id: UUID,
        entries: Sequence[EditionCompositionEntry],
        *,
        lock: bool,
    ) -> CompositionReferenceSnapshot:
        subject_ids = {entry.subject_id for entry in entries}
        run_ids = {
            entry.production_run_id for entry in entries if entry.production_run_id is not None
        }
        artifact_ids = {entry.artifact_id for entry in entries if entry.artifact_id is not None}
        batch_ids_by_run: dict[UUID, set[UUID]] = defaultdict(set)
        if run_ids:
            linked_batch_rows = await self._session.execute(
                select(
                    EditionProductionBatchItemRow.production_run_id,
                    EditionProductionBatchRow.id,
                )
                .join(
                    EditionProductionBatchRow,
                    EditionProductionBatchRow.id == EditionProductionBatchItemRow.batch_id,
                )
                .where(
                    EditionProductionBatchItemRow.production_run_id.in_(run_ids),
                    EditionProductionBatchRow.edition_id == edition_id,
                )
            )
            for run_id, batch_id in linked_batch_rows:
                batch_ids_by_run[run_id].add(batch_id)

        # Lock hierarchy: the caller already locked Edition; then batches; then runs.
        if lock and batch_ids_by_run:
            await self._session.scalars(
                select(EditionProductionBatchRow.id)
                .where(EditionProductionBatchRow.id.in_(set().union(*batch_ids_by_run.values())))
                .order_by(EditionProductionBatchRow.id)
                .with_for_update()
            )

        if lock and run_ids:
            await self._session.scalars(
                select(ProductionRunRow.id)
                .where(ProductionRunRow.id.in_(run_ids))
                .order_by(ProductionRunRow.id)
                .with_for_update()
            )

        subject_statement = (
            select(SubjectRow).where(SubjectRow.id.in_(subject_ids))
            if subject_ids
            else select(SubjectRow).where(false())
        )
        subject_rows = await self._session.scalars(subject_statement)
        subjects = {row.id: row for row in subject_rows if row.edition_id == edition_id}
        run_rows = list(
            await self._session.scalars(
                select(ProductionRunRow).where(ProductionRunRow.id.in_(run_ids))
                if run_ids
                else select(ProductionRunRow).where(false())
            )
        )
        runs = {row.id: row for row in run_rows}
        input_snapshots = list(
            await self._session.scalars(
                select(ProductionInputSnapshotRow).where(
                    ProductionInputSnapshotRow.production_run_id.in_(run_ids)
                )
                if run_ids
                else select(ProductionInputSnapshotRow).where(false())
            )
        )
        input_snapshots_by_run = {row.production_run_id: row for row in input_snapshots}
        active_discovery_snapshot = await self._session.scalar(
            select(DiscoverySnapshotRow).where(
                DiscoverySnapshotRow.edition_id == edition_id,
                DiscoverySnapshotRow.is_active.is_(True),
            )
        )
        discovery_identities = list(
            await self._session.scalars(
                select(DiscoverySubjectIdentityRow).where(
                    DiscoverySubjectIdentityRow.edition_id == edition_id
                )
            )
        )
        merged_into = {row.id: row.merged_into_id for row in discovery_identities}

        if lock and artifact_ids:
            await self._session.scalars(
                select(ProductionArtifactRow.id)
                .where(ProductionArtifactRow.id.in_(artifact_ids))
                .order_by(ProductionArtifactRow.id)
                .with_for_update()
            )
        artifact_rows = list(
            await self._session.scalars(
                select(ProductionArtifactRow).where(ProductionArtifactRow.id.in_(artifact_ids))
                if artifact_ids
                else select(ProductionArtifactRow).where(false())
            )
        )
        artifacts = {row.id: row for row in artifact_rows}

        latest_publications = (
            select(
                ProductionRunRow.subject_id.label("subject_id"),
                ProductionRunRow.id.label("run_id"),
                ProductionArtifactRow.version.label("artifact_version"),
                func.row_number()
                .over(
                    partition_by=(ProductionRunRow.subject_id, ProductionRunRow.id),
                    order_by=(
                        ProductionArtifactRow.version.desc(),
                        ProductionArtifactRow.id.desc(),
                    ),
                )
                .label("artifact_rank"),
            )
            .join(
                ProductionArtifactRow,
                ProductionArtifactRow.production_run_id == ProductionRunRow.id,
            )
            .where(
                ProductionRunRow.edition_id == edition_id,
                ProductionRunRow.subject_id.in_(subject_ids),
                ProductionRunRow.status == ProductionRunStatus.READY.value,
                ProductionArtifactRow.stage == "publication",
                ProductionArtifactRow.status == ProductionArtifactStatus.VERIFIED.value,
            )
            .subquery("current_composition_publications")
        )
        latest_rows = await self._session.execute(
            select(
                latest_publications.c.subject_id,
                latest_publications.c.run_id,
                latest_publications.c.artifact_version,
            ).where(latest_publications.c.artifact_rank == 1)
        )
        latest_by_run = {
            (subject_id, run_id): (run_id, artifact_version)
            for subject_id, run_id, artifact_version in latest_rows
        }

        decision_ids = {
            entry.review_decision_id for entry in entries if entry.review_decision_id is not None
        }
        decision_rows = list(
            await self._session.scalars(
                select(PublicationReviewDecisionRow).where(
                    PublicationReviewDecisionRow.id.in_(decision_ids)
                )
                if decision_ids
                else select(PublicationReviewDecisionRow).where(false())
            )
        )
        decisions = {row.id: row for row in decision_rows}
        supplied_batch_ids = {
            entry.provenance_batch_id for entry in entries if entry.provenance_batch_id is not None
        }
        supplied_batches = set(
            await self._session.scalars(
                select(EditionProductionBatchRow.id).where(
                    EditionProductionBatchRow.id.in_(supplied_batch_ids),
                    EditionProductionBatchRow.edition_id == edition_id,
                )
                if supplied_batch_ids
                else select(EditionProductionBatchRow.id).where(false())
            )
        )

        state_by_subject: dict[UUID, CompositionReferenceState] = {}
        for entry in entries:
            run = runs.get(entry.production_run_id) if entry.production_run_id else None
            artifact = artifacts.get(entry.artifact_id) if entry.artifact_id else None
            decision = decisions.get(entry.review_decision_id) if entry.review_decision_id else None
            latest_publication = latest_by_run.get((entry.subject_id, entry.production_run_id))
            linked_batch_ids = (
                batch_ids_by_run.get(entry.production_run_id, set())
                if entry.production_run_id
                else set()
            )
            linked_batch_id = next(iter(linked_batch_ids)) if len(linked_batch_ids) == 1 else None
            status = None
            if artifact is not None:
                try:
                    status = ProductionArtifactStatus(artifact.status)
                except ValueError:
                    status = None
            run_status = None
            if run is not None:
                try:
                    run_status = ProductionRunStatus(run.status)
                except ValueError:
                    run_status = None
            decision_matches_identity = decision is not None and (
                decision.edition_id == edition_id
                and decision.subject_id == entry.subject_id
                and (
                    (
                        entry.artifact_id is None
                        and decision.document_artifact_id is None
                        and decision.document_artifact_version is None
                        and decision.document_input_hash is None
                    )
                    or (
                        entry.artifact_id is not None
                        and decision.production_run_id == entry.production_run_id
                        and decision.pipeline_generation == entry.pipeline_generation
                        and decision.document_artifact_id == entry.artifact_id
                        and decision.document_artifact_version == entry.artifact_version
                        and decision.document_input_hash == entry.artifact_input_hash
                    )
                )
            )
            state_by_subject[entry.subject_id] = CompositionReferenceState(
                subject_exists=entry.subject_id in subjects,
                run_exists=run is not None,
                run_belongs_to_edition=run is not None and run.edition_id == edition_id,
                run_subject_matches=run is not None and run.subject_id == entry.subject_id,
                run_pipeline_generation=run.pipeline_generation if run is not None else None,
                run_status=run_status,
                artifact_exists=artifact is not None,
                artifact_run_matches=(
                    artifact is not None and artifact.production_run_id == entry.production_run_id
                ),
                artifact_subject_matches=(
                    artifact is not None and artifact.subject_id == entry.subject_id
                ),
                artifact_stage=artifact.stage if artifact is not None else None,
                artifact_status=status,
                artifact_version=artifact.version if artifact is not None else None,
                artifact_input_hash=artifact.input_hash if artifact is not None else None,
                latest_artifact_run_id=latest_publication[0] if latest_publication else None,
                latest_artifact_version=latest_publication[1] if latest_publication else None,
                linked_batch_id=linked_batch_id,
                supplied_batch_exists_in_edition=(
                    entry.provenance_batch_id in supplied_batches
                    if entry.provenance_batch_id is not None
                    else False
                ),
                supplied_batch_is_linked=(
                    entry.provenance_batch_id in linked_batch_ids
                    if entry.provenance_batch_id is not None
                    else False
                ),
                discovery_update_available=(
                    _discovery_update_available(
                        input_snapshots_by_run.get(entry.production_run_id),
                        active_discovery_snapshot,
                        merged_into,
                    )
                    if entry.production_run_id is not None
                    else None
                ),
                decision_matches_identity=decision_matches_identity,
                decision_is_valid_exclusion=(
                    decision_matches_identity
                    and decision is not None
                    and decision.decision == "exclude"
                ),
            )
        return CompositionReferenceSnapshot(
            subject_ids=frozenset(subjects), entries=state_by_subject
        )

    async def _load_entries(
        self, composition_id: UUID, *, lock: bool = False
    ) -> Sequence[EditionCompositionEntry]:
        statement = (
            select(EditionCompositionEntryRow)
            .where(EditionCompositionEntryRow.composition_id == composition_id)
            .order_by(
                EditionCompositionEntryRow.position.nulls_last(), EditionCompositionEntryRow.id
            )
        )
        if lock:
            statement = statement.with_for_update()
        rows = await self._session.scalars(statement)
        return tuple(_entry_from_row(row) for row in rows)


def _discovery_update_available(
    production_snapshot: ProductionInputSnapshotRow | None,
    active_snapshot: DiscoverySnapshotRow | None,
    merged_into: dict[UUID, UUID | None],
) -> bool | None:
    """Compare exact Discovery identity and member IDs without touching production state."""
    if production_snapshot is None:
        return None
    if (
        active_snapshot is None
        or active_snapshot.version <= production_snapshot.discovery_snapshot_version
    ):
        return False

    current_identity = production_snapshot.canonical_discovery_subject_id
    visited: set[UUID] = set()
    while (next_identity := merged_into.get(current_identity)) is not None:
        if current_identity in visited:
            return None
        visited.add(current_identity)
        current_identity = next_identity

    try:
        current_subject = next(
            subject
            for subject in active_snapshot.subjects
            if UUID(subject["subject_id"]) == current_identity
        )
        current_member_ids = {
            UUID(reference["candidate_id"]) for reference in current_subject["member_references"]
        }
        previous_member_ids = {UUID(value) for value in production_snapshot.member_candidate_ids}
        current_summary = current_subject["candidate"].get("summary")
    except (KeyError, StopIteration, TypeError, ValueError):
        # A newer snapshot which cannot be tied to the pinned canonical identity
        # needs editorial attention; never guess by title or similarity.
        return True

    return (
        current_member_ids != previous_member_ids
        or current_summary != production_snapshot.discovery_summary
    )


class SqlAlchemyEditionCompositionEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(
        self, composition_id: UUID, idempotency_key: str
    ) -> EditionCompositionEvent | None:
        row = await self._session.scalar(
            select(EditionCompositionEventRow).where(
                EditionCompositionEventRow.composition_id == composition_id,
                EditionCompositionEventRow.idempotency_key == idempotency_key,
            )
        )
        return _event_from_row(row) if row else None

    async def append(self, event: EditionCompositionEvent) -> None:
        self._session.add(
            EditionCompositionEventRow(
                id=event.id,
                composition_id=event.composition_id,
                edition_id=event.edition_id,
                kind=event.kind.value,
                expected_version=event.expected_version,
                result_version=event.result_version,
                idempotency_key=event.idempotency_key,
                request_fingerprint=event.request_fingerprint,
                actor_id=event.actor_id,
                correlation_id=event.correlation_id,
                payload=event.payload,
                result_snapshot=event.result_snapshot,
                occurred_at=event.occurred_at,
            )
        )
        await self._session.flush()


def _composition_from_rows(
    row: EditionCompositionRow, entries: Sequence[EditionCompositionEntry]
) -> EditionComposition:
    return EditionComposition(
        id=row.id,
        edition_id=row.edition_id,
        version=row.version,
        entries=tuple(entries),
        actor_id=row.actor_id,
        preparation_status=CompositionPreparationStatus(row.preparation_status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _entry_from_row(row: EditionCompositionEntryRow) -> EditionCompositionEntry:
    return EditionCompositionEntry(
        id=row.id,
        subject_id=row.subject_id,
        disposition=CompositionDisposition(row.disposition),
        position=row.position,
        production_run_id=row.production_run_id,
        pipeline_generation=row.pipeline_generation,
        artifact_id=row.artifact_id,
        artifact_version=row.artifact_version,
        artifact_input_hash=row.artifact_input_hash,
        provenance_batch_id=row.provenance_batch_id,
        review_decision_id=row.review_decision_id,
        document_sha256=row.document_sha256,
        preparation_status=CompositionPreparationStatus(row.preparation_status),
    )


def _event_from_row(row: EditionCompositionEventRow) -> EditionCompositionEvent:
    return EditionCompositionEvent(
        id=row.id,
        composition_id=row.composition_id,
        edition_id=row.edition_id,
        kind=CompositionMutationKind(row.kind),
        expected_version=row.expected_version,
        result_version=row.result_version,
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        actor_id=row.actor_id,
        correlation_id=row.correlation_id,
        payload=row.payload,
        result_snapshot=row.result_snapshot,
        occurred_at=row.occurred_at,
    )
