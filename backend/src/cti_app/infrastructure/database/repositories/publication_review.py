"""PostgreSQL persistence and read model for publication review."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import String, and_, case, cast, exists, func, literal, or_, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from cti_app.application.edition_review import (
    EditionReviewDiagnostic,
    EditionReviewDiagnosticCode,
    EditionReviewReadItem,
    EditionReviewReadScope,
    requires_reconciliation,
    review_item_can_retry,
)
from cti_app.application.production_resume import resolve_retry_stage
from cti_app.domain.edition_composition import CompositionDisposition
from cti_app.domain.model_runs import ModelSubmissionState
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRunStatus,
    ProductionStage,
    ProductionSubmissionReconciliation,
)
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.edition_composition import (
    EditionCompositionEntryRow,
    EditionCompositionRow,
)
from cti_app.infrastructure.database.models.production import (
    ProductionArtifactRow,
    ProductionInputSnapshotRow,
    ProductionRunRow,
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

    async def list_for_edition(self, edition_id: UUID) -> EditionReviewReadScope:
        composition = (
            select(
                EditionCompositionRow.id.label("id"),
                EditionCompositionRow.version.label("version"),
            )
            .where(EditionCompositionRow.edition_id == edition_id)
            .cte("current_review_composition")
        )

        current_extraction = (
            select(
                ProductionArtifactRow.production_run_id.label("run_id"),
                func.coalesce(
                    ProductionArtifactRow.artifact_metadata["q2_rejected_ioc_count"].as_integer(),
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
                    ProductionArtifactRow.artifact_metadata["q2_rejected_ioc_count"].as_integer(),
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
        # Which stages still hold a current artifact, per run.  The review has
        # to answer "what is missing", not merely "is the document verified":
        # a repair stales SYNTHESIS and PUBLICATION together, and the retry it
        # offers must aim at the first gap, not at the run's last stage.
        live_stages = (
            select(
                ProductionArtifactRow.production_run_id.label("run_id"),
                func.array_agg(ProductionArtifactRow.stage.distinct()).label("stages"),
            )
            .where(ProductionArtifactRow.status != ProductionArtifactStatus.STALE.value)
            .group_by(ProductionArtifactRow.production_run_id)
            .subquery("current_review_live_stages")
        )
        latest_publications = (
            select(
                ProductionRunRow.subject_id.label("subject_id"),
                ProductionRunRow.id.label("run_id"),
                ProductionArtifactRow.id.label("artifact_id"),
                ProductionArtifactRow.version.label("artifact_version"),
                func.row_number()
                .over(
                    partition_by=(ProductionRunRow.subject_id, ProductionRunRow.id),
                    order_by=(
                        ProductionRunRow.run_number.desc(),
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
                ProductionRunRow.status == ProductionRunStatus.READY.value,
                ProductionArtifactRow.stage == ProductionArtifactStage.PUBLICATION.value,
                ProductionArtifactRow.status == ProductionArtifactStatus.VERIFIED.value,
            )
            .subquery("latest_review_publications")
        )
        pinned_artifact = aliased(ProductionArtifactRow, name="pinned_review_artifact")
        subject_title = (
            select(SubjectRow.title)
            .where(SubjectRow.id == EditionCompositionEntryRow.subject_id)
            .correlate(EditionCompositionEntryRow)
            .scalar_subquery()
        )

        ranked = (
            select(
                composition.c.version.label("composition_version"),
                EditionCompositionEntryRow.position.label("position"),
                EditionCompositionEntryRow.subject_id.label("subject_id"),
                func.coalesce(
                    ProductionInputSnapshotRow.subject_title,
                    subject_title,
                    cast(EditionCompositionEntryRow.subject_id, String),
                ).label("title"),
                EditionCompositionEntryRow.production_run_id.label("run_id"),
                EditionCompositionEntryRow.pipeline_generation.label("pipeline_generation"),
                ProductionRunRow.pipeline_generation.label("current_pipeline_generation"),
                ProductionRunRow.status.label("run_status"),
                ProductionRunRow.current_stage.label("current_stage"),
                EditionCompositionEntryRow.artifact_id.label("artifact_id"),
                EditionCompositionEntryRow.artifact_version.label("artifact_version"),
                EditionCompositionEntryRow.artifact_input_hash.label("artifact_hash"),
                pinned_artifact.status.label("artifact_status"),
                latest_publications.c.run_id.label("latest_artifact_run_id"),
                latest_publications.c.artifact_id.label("latest_artifact_id"),
                latest_publications.c.artifact_version.label("latest_artifact_version"),
                live_stages.c.stages.label("live_artifact_stages"),
                func.coalesce(current_extraction.c.rejected_indicator_count, 0).label(
                    "rejected_indicator_count"
                ),
                func.coalesce(current_extraction.c.rejected_ioc_count, 0).label(
                    "rejected_ioc_count"
                ),
                func.coalesce(current_extraction.c.rejected_other_artifact_count, 0).label(
                    "rejected_other_artifact_count"
                ),
                func.coalesce(current_extraction.c.rejected_rule_count, 0).label(
                    "rejected_rule_count"
                ),
                func.coalesce(current_extraction.c.published_rule_count, 0).label(
                    "published_rule_count"
                ),
                ProductionRunRow.error_code.label("error_code"),
                ProductionRunRow.error_message.label("error_message"),
                ProductionRunRow.reconciliation_model_run_id.label("reconciliation_model_run_id"),
                ProductionRunRow.reconciliation_stage.label("reconciliation_stage"),
                ProductionRunRow.reconciliation_bridge_response_id.label(
                    "reconciliation_bridge_response_id"
                ),
                ProductionRunRow.reconciliation_bridge_request_id.label(
                    "reconciliation_bridge_request_id"
                ),
                ProductionRunRow.reconciliation_submission_state.label(
                    "reconciliation_submission_state"
                ),
                ProductionRunRow.reconciliation_phase.label("reconciliation_phase"),
                ProductionRunRow.reconciliation_output_sha256.label("reconciliation_output_sha256"),
                ProductionRunRow.reconciliation_provenance.label("reconciliation_provenance"),
                PublicationReviewDecisionRow.id.label("decision_id"),
                PublicationReviewDecisionRow.decision.label("decision"),
                func.row_number()
                .over(
                    partition_by=EditionCompositionEntryRow.id,
                    order_by=(
                        PublicationReviewDecisionRow.occurred_at.desc().nullslast(),
                        PublicationReviewDecisionRow.id.desc().nullslast(),
                    ),
                )
                .label("decision_rank"),
            )
            .select_from(EditionCompositionEntryRow)
            .join(composition, composition.c.id == EditionCompositionEntryRow.composition_id)
            .join(
                ProductionRunRow,
                ProductionRunRow.id == EditionCompositionEntryRow.production_run_id,
            )
            .outerjoin(
                ProductionInputSnapshotRow,
                ProductionInputSnapshotRow.production_run_id
                == EditionCompositionEntryRow.production_run_id,
            )
            .outerjoin(
                pinned_artifact,
                and_(
                    pinned_artifact.id == EditionCompositionEntryRow.artifact_id,
                    pinned_artifact.production_run_id
                    == EditionCompositionEntryRow.production_run_id,
                    pinned_artifact.version == EditionCompositionEntryRow.artifact_version,
                    pinned_artifact.input_hash == EditionCompositionEntryRow.artifact_input_hash,
                    pinned_artifact.stage == ProductionArtifactStage.PUBLICATION.value,
                ),
            )
            .outerjoin(
                current_extraction,
                and_(
                    current_extraction.c.run_id == EditionCompositionEntryRow.production_run_id,
                    current_extraction.c.artifact_rank == 1,
                ),
            )
            .outerjoin(
                live_stages,
                live_stages.c.run_id == EditionCompositionEntryRow.production_run_id,
            )
            .outerjoin(
                latest_publications,
                and_(
                    latest_publications.c.subject_id == EditionCompositionEntryRow.subject_id,
                    latest_publications.c.run_id == EditionCompositionEntryRow.production_run_id,
                    latest_publications.c.artifact_rank == 1,
                ),
            )
            .outerjoin(
                PublicationReviewDecisionRow,
                and_(
                    PublicationReviewDecisionRow.edition_id == edition_id,
                    PublicationReviewDecisionRow.subject_id
                    == EditionCompositionEntryRow.subject_id,
                    PublicationReviewDecisionRow.production_run_id
                    == EditionCompositionEntryRow.production_run_id,
                    PublicationReviewDecisionRow.pipeline_generation
                    == EditionCompositionEntryRow.pipeline_generation,
                    PublicationReviewDecisionRow.document_artifact_id
                    == EditionCompositionEntryRow.artifact_id,
                    PublicationReviewDecisionRow.document_artifact_version
                    == EditionCompositionEntryRow.artifact_version,
                    PublicationReviewDecisionRow.document_input_hash
                    == EditionCompositionEntryRow.artifact_input_hash,
                ),
            )
            .where(
                EditionCompositionEntryRow.disposition == "included",
            )
            .subquery("ranked_review_items")
        )
        item_columns = (
            "position",
            "subject_id",
            "title",
            "run_id",
            "pipeline_generation",
            "current_pipeline_generation",
            "run_status",
            "current_stage",
            "artifact_id",
            "artifact_version",
            "artifact_hash",
            "artifact_status",
            "latest_artifact_run_id",
            "latest_artifact_id",
            "latest_artifact_version",
            "live_artifact_stages",
            "rejected_indicator_count",
            "rejected_ioc_count",
            "rejected_other_artifact_count",
            "rejected_rule_count",
            "published_rule_count",
            "error_code",
            "error_message",
            "reconciliation_model_run_id",
            "reconciliation_stage",
            "reconciliation_bridge_response_id",
            "reconciliation_bridge_request_id",
            "reconciliation_submission_state",
            "reconciliation_phase",
            "reconciliation_output_sha256",
            "reconciliation_provenance",
            "decision_id",
            "decision",
            "decision_rank",
        )
        diagnostic_columns = (
            "diagnostic_code",
            "diagnostic_subject_id",
            "diagnostic_disposition",
            "diagnostic_run_id",
            "diagnostic_title",
            "diagnostic_decision_id",
        )
        item_query = select(
            literal("item").label("row_kind"),
            ranked.c.composition_version.label("composition_version"),
            *(ranked.c[column].label(column) for column in item_columns),
            *(literal(None).label(column) for column in diagnostic_columns),
        ).where(ranked.c.decision_rank == 1)

        diagnostic_decision = aliased(PublicationReviewDecisionRow, name="excluded_review_decision")
        diagnostic_query = (
            select(
                literal("diagnostic").label("row_kind"),
                composition.c.version.label("composition_version"),
                *(literal(None).label(column) for column in item_columns),
                case(
                    (EditionCompositionEntryRow.id.is_(None), "subject_unaddressed"),
                    (EditionCompositionEntryRow.disposition == "deferred", "subject_deferred"),
                    else_="subject_excluded",
                ).label("diagnostic_code"),
                SubjectRow.id.label("subject_id"),
                EditionCompositionEntryRow.disposition.label("diagnostic_disposition"),
                EditionCompositionEntryRow.production_run_id.label("diagnostic_run_id"),
                SubjectRow.title.label("diagnostic_title"),
                diagnostic_decision.id.label("diagnostic_decision_id"),
            )
            .select_from(SubjectRow)
            .join(composition, true())
            .outerjoin(
                EditionCompositionEntryRow,
                and_(
                    EditionCompositionEntryRow.composition_id == composition.c.id,
                    EditionCompositionEntryRow.subject_id == SubjectRow.id,
                ),
            )
            .outerjoin(
                diagnostic_decision,
                and_(
                    diagnostic_decision.id == EditionCompositionEntryRow.review_decision_id,
                    diagnostic_decision.edition_id == edition_id,
                    diagnostic_decision.subject_id == SubjectRow.id,
                    diagnostic_decision.decision == "exclude",
                    or_(
                        and_(
                            EditionCompositionEntryRow.artifact_id.is_(None),
                            diagnostic_decision.document_artifact_id.is_(None),
                            diagnostic_decision.document_artifact_version.is_(None),
                            diagnostic_decision.document_input_hash.is_(None),
                        ),
                        and_(
                            EditionCompositionEntryRow.artifact_id.is_not(None),
                            diagnostic_decision.production_run_id
                            == EditionCompositionEntryRow.production_run_id,
                            diagnostic_decision.pipeline_generation
                            == EditionCompositionEntryRow.pipeline_generation,
                            diagnostic_decision.document_artifact_id
                            == EditionCompositionEntryRow.artifact_id,
                            diagnostic_decision.document_artifact_version
                            == EditionCompositionEntryRow.artifact_version,
                            diagnostic_decision.document_input_hash
                            == EditionCompositionEntryRow.artifact_input_hash,
                        ),
                    ),
                ),
            )
            .where(
                SubjectRow.edition_id == edition_id,
                or_(
                    EditionCompositionEntryRow.id.is_(None),
                    EditionCompositionEntryRow.disposition.in_(
                        (
                            CompositionDisposition.DEFERRED.value,
                            CompositionDisposition.EXCLUDED.value,
                        )
                    ),
                ),
            )
        )
        metadata_query = select(
            literal("metadata").label("row_kind"),
            composition.c.version.label("composition_version"),
            *(literal(None).label(column) for column in item_columns),
            *(literal(None).label(column) for column in diagnostic_columns),
        ).select_from(composition)
        missing_query = select(
            literal("diagnostic").label("row_kind"),
            literal(None).label("composition_version"),
            *(literal(None).label(column) for column in item_columns),
            literal("composition_missing").label("diagnostic_code"),
            *(literal(None).label(column) for column in diagnostic_columns[1:]),
        ).where(~exists(select(composition.c.id)))
        combined = union_all(item_query, diagnostic_query, metadata_query, missing_query)
        combined_rows = combined.subquery()
        result_query = select(combined_rows).order_by(
            combined_rows.c.row_kind,
            combined_rows.c.position.nullslast(),
            combined_rows.c.diagnostic_subject_id,
        )
        rows = (await self._session.execute(result_query)).mappings()
        items: list[EditionReviewReadItem] = []
        diagnostics: list[EditionReviewDiagnostic] = []
        composition_version: int | None = None
        for row in rows:
            if row["composition_version"] is not None:
                composition_version = row["composition_version"]
            if row["row_kind"] == "item":
                items.append(
                    _read_item_from_row(row, composition_version=row["composition_version"])
                )
                continue
            code = row["diagnostic_code"]
            if code is None:
                continue
            diagnostic_code = EditionReviewDiagnosticCode(code)
            subject_id = row["diagnostic_subject_id"]
            disposition = row["diagnostic_disposition"]
            diagnostics.append(
                EditionReviewDiagnostic(
                    code=diagnostic_code,
                    subject_id=subject_id,
                    disposition=CompositionDisposition(disposition) if disposition else None,
                    run_id=row["diagnostic_run_id"],
                    details={"title": row["diagnostic_title"]} if row["diagnostic_title"] else None,
                )
            )
            if (
                diagnostic_code is EditionReviewDiagnosticCode.SUBJECT_EXCLUDED
                and row["diagnostic_decision_id"] is None
            ):
                diagnostics.append(
                    EditionReviewDiagnostic(
                        code=EditionReviewDiagnosticCode.EXCLUSION_DECISION_MISSING,
                        subject_id=subject_id,
                        disposition=CompositionDisposition.EXCLUDED,
                        run_id=row["diagnostic_run_id"],
                        details={"title": row["diagnostic_title"]},
                    )
                )

        return EditionReviewReadScope(
            items=tuple(items),
            composition_version=composition_version,
            diagnostics=tuple(diagnostics),
        )


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
        stage=ProductionStage(stage),
        bridge_response_id=row["reconciliation_bridge_response_id"],
        bridge_request_id=row["reconciliation_bridge_request_id"],
        submission_state=ModelSubmissionState(submission_state),
        phase=phase,
        output_sha256=row["reconciliation_output_sha256"],
        provenance=row["reconciliation_provenance"],
    )


def _read_item_from_row(row: Any, *, composition_version: int) -> EditionReviewReadItem:
    run_status = ProductionRunStatus(row["run_status"])
    live_artifact_stages = frozenset(row["live_artifact_stages"] or ())
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
        live_artifact_stages=live_artifact_stages,
        retry_stage=(
            resolve_retry_stage(
                live_artifact_stages,
                current_stage=ProductionStage(row["current_stage"]),
            )
            if can_retry
            else None
        ),
        reconciliation=reconciliation,
        rejected_indicator_count=row["rejected_indicator_count"] or 0,
        rejected_ioc_count=row["rejected_ioc_count"] or 0,
        rejected_other_artifact_count=row["rejected_other_artifact_count"] or 0,
        rejected_rule_count=row["rejected_rule_count"] or 0,
        published_rule_count=row["published_rule_count"] or 0,
        composition_version=composition_version,
        pinned_generation_current=(
            row["current_pipeline_generation"] == row["pipeline_generation"]
        ),
        superseded_artifact_available=(
            row["latest_artifact_id"] is not None
            and (
                row["latest_artifact_id"] != row["artifact_id"]
                or row["latest_artifact_run_id"] != row["run_id"]
            )
        ),
        latest_artifact_run_id=row["latest_artifact_run_id"],
        latest_artifact_id=row["latest_artifact_id"],
        latest_artifact_version=row["latest_artifact_version"],
    )
