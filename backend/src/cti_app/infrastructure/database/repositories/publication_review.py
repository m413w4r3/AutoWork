"""PostgreSQL persistence and read model for publication review."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.edition_review import (
    EditionReviewReadItem,
    requires_reconciliation,
    review_item_can_retry,
)
from cti_app.domain.model_runs import ModelSubmissionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionSubmissionReconciliation,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision
from cti_app.infrastructure.database.models.editorial import EditorialGroupRow
from cti_app.infrastructure.database.models.production import (
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionArtifactRow,
    ProductionInputSnapshotRow,
    SubjectProductionRunRow,
)
from cti_app.infrastructure.database.models.publication_review import (
    PublicationReviewDecisionRow,
)


class SqlAlchemyPublicationReviewDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, decision: PublicationReviewDecision) -> None:
        self._session.add(
            PublicationReviewDecisionRow(
                id=decision.id,
                edition_id=decision.edition_id,
                subject_id=decision.subject_id,
                production_run_id=decision.production_run_id,
                pipeline_generation=decision.pipeline_generation,
                document_artifact_id=decision.document_artifact_id,
                document_artifact_version=decision.document_artifact_version,
                document_input_hash=decision.document_input_hash,
                decision=decision.decision.value,
                actor_id=decision.actor_id,
                reason=decision.reason,
                occurred_at=decision.occurred_at,
            )
        )
        await self._session.flush()

    async def list_for_edition(self, edition_id: UUID) -> Sequence[PublicationReviewDecision]:
        rows = await self._session.scalars(
            select(PublicationReviewDecisionRow)
            .where(PublicationReviewDecisionRow.edition_id == edition_id)
            .order_by(
                PublicationReviewDecisionRow.occurred_at,
                PublicationReviewDecisionRow.id,
            )
        )
        return [_decision_from_row(row) for row in rows]


class SqlAlchemyEditionReviewReadRepository:
    """One set-based query for the current batch and applicable decisions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionReviewReadItem]:
        latest_batch_id = (
            select(EditionProductionBatchRow.id)
            .where(EditionProductionBatchRow.edition_id == edition_id)
            .order_by(
                EditionProductionBatchRow.created_at.desc(),
                EditionProductionBatchRow.id.desc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        current_artifacts = (
            select(
                ProductionArtifactRow.production_run_id.label("run_id"),
                ProductionArtifactRow.id.label("artifact_id"),
                ProductionArtifactRow.version.label("artifact_version"),
                ProductionArtifactRow.input_hash.label("artifact_hash"),
                ProductionArtifactRow.status.label("artifact_status"),
                func.row_number()
                .over(
                    partition_by=ProductionArtifactRow.production_run_id,
                    order_by=(
                        case(
                            (ProductionArtifactRow.stage == "publication", 1),
                            else_=0,
                        ).desc(),
                        ProductionArtifactRow.version.desc(),
                        ProductionArtifactRow.id.desc(),
                    ),
                )
                .label("artifact_rank"),
            )
            .where(
                ProductionArtifactRow.stage == ProductionArtifactStage.PUBLICATION.value,
                ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value,
            )
            .subquery("current_review_artifacts")
        )
        current_extraction = (
            select(
                ProductionArtifactRow.production_run_id.label("run_id"),
                func.coalesce(
                    ProductionArtifactRow.artifact_metadata[
                        "q2_rejected_ioc_count"
                    ].as_integer(),
                    ProductionArtifactRow.artifact_metadata["deterministic_verification"][
                        "q2_rejected_ioc_count"
                    ].as_integer(),
                    ProductionArtifactRow.artifact_metadata[
                        "q2_rejected_artifact_count"
                    ].as_integer(),
                    ProductionArtifactRow.artifact_metadata["deterministic_verification"][
                        "q2_rejected_artifact_count"
                    ].as_integer(),
                ).label("rejected_indicator_count"),
                func.coalesce(
                    ProductionArtifactRow.artifact_metadata[
                        "q2_rejected_ioc_count"
                    ].as_integer(),
                    ProductionArtifactRow.artifact_metadata["deterministic_verification"][
                        "q2_rejected_ioc_count"
                    ].as_integer(),
                    0,
                ).label("rejected_ioc_count"),
                func.coalesce(
                    ProductionArtifactRow.artifact_metadata[
                        "q2_rejected_other_artifact_count"
                    ].as_integer(),
                    ProductionArtifactRow.artifact_metadata["deterministic_verification"][
                        "q2_rejected_other_artifact_count"
                    ].as_integer(),
                    0,
                ).label("rejected_other_artifact_count"),
                func.coalesce(
                    ProductionArtifactRow.artifact_metadata["q2_rejected_rule_count"].as_integer(),
                    ProductionArtifactRow.artifact_metadata["deterministic_verification"][
                        "q2_rejected_rule_count"
                    ].as_integer(),
                ).label("rejected_rule_count"),
                func.coalesce(
                    ProductionArtifactRow.artifact_metadata["rules_count"].as_integer(),
                    ProductionArtifactRow.artifact_metadata["deterministic_verification"][
                        "rules_count"
                    ].as_integer(),
                    ProductionArtifactRow.artifact_metadata["element_counts"]["rules"].as_integer(),
                ).label("published_rule_count"),
                func.row_number()
                .over(
                    partition_by=ProductionArtifactRow.production_run_id,
                    order_by=(
                        case(
                            (
                                ProductionArtifactRow.stage
                                == ProductionArtifactStage.EXTRACTION.value,
                                1,
                            ),
                            else_=0,
                        ).desc(),
                        ProductionArtifactRow.version.desc(),
                        ProductionArtifactRow.id.desc(),
                    ),
                )
                .label("artifact_rank"),
            )
            .where(
                ProductionArtifactRow.stage == ProductionArtifactStage.EXTRACTION.value,
                ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value,
            )
            .subquery("current_review_extraction")
        )
        group_title = (
            select(EditorialGroupRow.title)
            .where(
                EditorialGroupRow.edition_id == edition_id,
                EditorialGroupRow.subject_id == EditionProductionBatchItemRow.subject_id,
            )
            .order_by(EditorialGroupRow.created_at.desc(), EditorialGroupRow.id.desc())
            .limit(1)
            .correlate(EditionProductionBatchItemRow)
            .scalar_subquery()
        )

        ranked = (
            select(
                EditionProductionBatchItemRow.position.label("position"),
                EditionProductionBatchItemRow.subject_id.label("subject_id"),
                func.coalesce(
                    ProductionInputSnapshotRow.subject_title,
                    group_title,
                    cast(EditionProductionBatchItemRow.subject_id, String),
                ).label("title"),
                SubjectProductionRunRow.id.label("run_id"),
                SubjectProductionRunRow.pipeline_generation.label("pipeline_generation"),
                SubjectProductionRunRow.status.label("run_status"),
                SubjectProductionRunRow.current_stage.label("current_stage"),
                current_artifacts.c.artifact_id,
                current_artifacts.c.artifact_version,
                current_artifacts.c.artifact_hash,
                current_artifacts.c.artifact_status,
                func.coalesce(current_extraction.c.rejected_indicator_count, 0).label(
                    "rejected_indicator_count"
                ),
                func.coalesce(current_extraction.c.rejected_ioc_count, 0).label(
                    "rejected_ioc_count"
                ),
                func.coalesce(
                    current_extraction.c.rejected_other_artifact_count, 0
                ).label("rejected_other_artifact_count"),
                func.coalesce(current_extraction.c.rejected_rule_count, 0).label(
                    "rejected_rule_count"
                ),
                func.coalesce(current_extraction.c.published_rule_count, 0).label(
                    "published_rule_count"
                ),
                SubjectProductionRunRow.error_code.label("error_code"),
                SubjectProductionRunRow.error_message.label("error_message"),
                SubjectProductionRunRow.reconciliation_model_run_id.label(
                    "reconciliation_model_run_id"
                ),
                SubjectProductionRunRow.reconciliation_stage.label("reconciliation_stage"),
                SubjectProductionRunRow.reconciliation_bridge_response_id.label(
                    "reconciliation_bridge_response_id"
                ),
                SubjectProductionRunRow.reconciliation_submission_state.label(
                    "reconciliation_submission_state"
                ),
                SubjectProductionRunRow.reconciliation_phase.label("reconciliation_phase"),
                SubjectProductionRunRow.reconciliation_output_sha256.label(
                    "reconciliation_output_sha256"
                ),
                SubjectProductionRunRow.reconciliation_provenance.label(
                    "reconciliation_provenance"
                ),
                PublicationReviewDecisionRow.id.label("decision_id"),
                PublicationReviewDecisionRow.decision.label("decision"),
                func.row_number()
                .over(
                    partition_by=EditionProductionBatchItemRow.id,
                    order_by=(
                        PublicationReviewDecisionRow.occurred_at.desc().nullslast(),
                        PublicationReviewDecisionRow.id.desc().nullslast(),
                    ),
                )
                .label("decision_rank"),
            )
            .select_from(EditionProductionBatchItemRow)
            .join(
                SubjectProductionRunRow,
                SubjectProductionRunRow.id == EditionProductionBatchItemRow.production_run_id,
            )
            .outerjoin(
                ProductionInputSnapshotRow,
                ProductionInputSnapshotRow.production_run_id
                == EditionProductionBatchItemRow.production_run_id,
            )
            .outerjoin(
                current_artifacts,
                and_(
                    current_artifacts.c.run_id == EditionProductionBatchItemRow.production_run_id,
                    current_artifacts.c.artifact_rank == 1,
                ),
            )
            .outerjoin(
                current_extraction,
                and_(
                    current_extraction.c.run_id == EditionProductionBatchItemRow.production_run_id,
                    current_extraction.c.artifact_rank == 1,
                ),
            )
            .outerjoin(
                PublicationReviewDecisionRow,
                and_(
                    PublicationReviewDecisionRow.edition_id == edition_id,
                    PublicationReviewDecisionRow.subject_id
                    == EditionProductionBatchItemRow.subject_id,
                    PublicationReviewDecisionRow.production_run_id == SubjectProductionRunRow.id,
                    PublicationReviewDecisionRow.pipeline_generation
                    == SubjectProductionRunRow.pipeline_generation,
                    or_(
                        and_(
                            current_artifacts.c.artifact_id.is_(None),
                            PublicationReviewDecisionRow.document_artifact_id.is_(None),
                            PublicationReviewDecisionRow.document_artifact_version.is_(None),
                            PublicationReviewDecisionRow.document_input_hash.is_(None),
                        ),
                        and_(
                            PublicationReviewDecisionRow.document_artifact_id
                            == current_artifacts.c.artifact_id,
                            PublicationReviewDecisionRow.document_artifact_version
                            == current_artifacts.c.artifact_version,
                            PublicationReviewDecisionRow.document_input_hash
                            == current_artifacts.c.artifact_hash,
                        ),
                    ),
                ),
            )
            .where(EditionProductionBatchItemRow.batch_id == latest_batch_id)
            .subquery("ranked_review_items")
        )
        query = select(ranked).where(ranked.c.decision_rank == 1).order_by(ranked.c.position)
        rows = (await self._session.execute(query)).mappings()
        return [_read_item_from_row(row) for row in rows]


def _decision_from_row(row: PublicationReviewDecisionRow) -> PublicationReviewDecision:
    return PublicationReviewDecision(
        id=row.id,
        edition_id=row.edition_id,
        subject_id=row.subject_id,
        production_run_id=row.production_run_id,
        pipeline_generation=row.pipeline_generation,
        document_artifact_id=row.document_artifact_id,
        document_artifact_version=row.document_artifact_version,
        document_input_hash=row.document_input_hash,
        decision=PublicationDecision(row.decision),
        actor_id=row.actor_id,
        reason=row.reason,
        occurred_at=row.occurred_at,
    )


def _reconciliation_from_row(row: Any, run_id: UUID) -> ProductionSubmissionReconciliation | None:
    """Rebuild the exact submission identity persisted next to the run."""
    model_run_id = row["reconciliation_model_run_id"]
    stage = row["reconciliation_stage"]
    submission_state = row["reconciliation_submission_state"]
    phase = row["reconciliation_phase"]
    if model_run_id is None or stage is None or submission_state is None or not phase:
        return None
    return ProductionSubmissionReconciliation(
        production_run_id=run_id,
        model_run_id=model_run_id,
        stage=SubjectProductionStage(stage),
        bridge_response_id=row["reconciliation_bridge_response_id"],
        submission_state=ModelSubmissionState(submission_state),
        phase=phase,
        output_sha256=row["reconciliation_output_sha256"],
        provenance=row["reconciliation_provenance"],
    )


def _read_item_from_row(row: Any) -> EditionReviewReadItem:
    run_status = SubjectProductionStatus(row["run_status"])
    artifact_status = (
        ProductionArtifactStatus(row["artifact_status"])
        if row["artifact_status"] is not None
        else None
    )
    artifact_verified = artifact_status is ProductionArtifactStatus.VERIFIED
    reconciliation = _reconciliation_from_row(row, row["run_id"])
    can_retry = review_item_can_retry(
        run_status,
        artifact_verified=artifact_verified,
        reconciliation_required=requires_reconciliation(
            run_status, row["error_code"], reconciliation
        ),
    )
    return EditionReviewReadItem(
        position=row["position"],
        subject_id=row["subject_id"],
        title=row["title"],
        run_id=row["run_id"],
        pipeline_generation=row["pipeline_generation"],
        run_status=run_status,
        document_artifact_id=row["artifact_id"],
        document_artifact_version=row["artifact_version"],
        document_input_hash=row["artifact_hash"],
        document_artifact_status=artifact_status,
        error_code=row["error_code"],
        error_message=row["error_message"],
        effective_decision_id=row["decision_id"],
        effective_decision=(
            PublicationDecision(row["decision"]) if row["decision"] is not None else None
        ),
        retry_stage=SubjectProductionStage(row["current_stage"]) if can_retry else None,
        reconciliation=reconciliation,
        rejected_indicator_count=row["rejected_indicator_count"] or 0,
        rejected_ioc_count=row["rejected_ioc_count"] or 0,
        rejected_other_artifact_count=row["rejected_other_artifact_count"] or 0,
        rejected_rule_count=row["rejected_rule_count"] or 0,
        published_rule_count=row["published_rule_count"] or 0,
    )
