"""PostgreSQL persistence for immutable production routing decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.application.persistence import (
    ProductionRoutingDecisionConflictError,
    StaleProductionRoutingGenerationError,
    StaleProductionRunVersionError,
)
from cti_app.domain.production_references import ProductionSourceCriticality
from cti_app.domain.production_routing import (
    ProductionRoutingDecisionV1,
    RoutingCriticalSourceDiagnosticV1,
    RoutingReasonCode,
    RoutingStatus,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.database.models.production import (
    ProductionArtifactRow,
    ProductionRunRow,
)
from cti_app.infrastructure.database.models.production_routing import (
    ProductionRoutingDecisionRow,
)


class SqlAlchemyProductionRoutingDecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append_and_adopt(
        self,
        decision: ProductionRoutingDecisionV1,
        *,
        expected_version: int,
    ) -> ProductionRoutingDecisionV1:
        """Append idempotently, then adopt through a version+generation SQL CAS."""
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")

        run = await self._session.execute(
            select(
                ProductionRunRow.version,
                ProductionRunRow.pipeline_generation,
                ProductionRunRow.routing_decision_id,
            ).where(ProductionRunRow.id == decision.production_run_id)
        )
        run_row = run.one_or_none()
        if run_row is None:
            raise ValueError("production routing run does not exist")
        if run_row.pipeline_generation != decision.pipeline_generation:
            raise StaleProductionRoutingGenerationError(
                "routing decision generation is not current"
            )

        await self._validate_artifact_ownership(decision)

        values = _decision_values(decision)
        inserted = await self._session.execute(
            insert(ProductionRoutingDecisionRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=(
                    ProductionRoutingDecisionRow.production_run_id,
                    ProductionRoutingDecisionRow.pipeline_generation,
                    ProductionRoutingDecisionRow.input_hash,
                )
            )
            .returning(ProductionRoutingDecisionRow.id)
        )
        inserted_id = inserted.scalar_one_or_none()
        if inserted_id is not None:
            stored = decision
        else:
            existing = await self._session.scalar(
                select(ProductionRoutingDecisionRow).where(
                    ProductionRoutingDecisionRow.production_run_id == decision.production_run_id,
                    ProductionRoutingDecisionRow.pipeline_generation
                    == decision.pipeline_generation,
                    ProductionRoutingDecisionRow.input_hash == decision.input_hash,
                )
            )
            if existing is None:
                raise RuntimeError("routing decision conflict row was not visible")
            if existing.decision_hash != decision.decision_hash:
                raise ProductionRoutingDecisionConflictError(
                    "same routing input key has a different decision hash"
                )
            stored = _decision_from_row(existing)

        if run_row.routing_decision_id == stored.decision_id:
            return stored

        cas = await self._session.execute(
            update(ProductionRunRow)
            .where(
                ProductionRunRow.id == decision.production_run_id,
                ProductionRunRow.version == expected_version,
                ProductionRunRow.pipeline_generation == decision.pipeline_generation,
            )
            .values(
                routing_decision_id=stored.decision_id,
                version=ProductionRunRow.version + 1,
                updated_at=datetime.now(UTC),
            )
        )
        if cast(Any, cas).rowcount != 1:
            current_run = await self._session.execute(
                select(
                    ProductionRunRow.pipeline_generation,
                    ProductionRunRow.routing_decision_id,
                ).where(ProductionRunRow.id == decision.production_run_id)
            )
            current_row = current_run.one_or_none()
            if current_row is None:
                raise ValueError("production routing run no longer exists")
            if current_row.pipeline_generation != decision.pipeline_generation:
                raise StaleProductionRoutingGenerationError(
                    "production run advanced while routing decision was being adopted"
                )
            if current_row.routing_decision_id == stored.decision_id:
                return stored
            raise StaleProductionRunVersionError(
                "production run version changed before routing decision adoption"
            )
        return stored

    async def get(self, decision_id: UUID) -> ProductionRoutingDecisionV1 | None:
        row = await self._session.get(ProductionRoutingDecisionRow, decision_id)
        return _decision_from_row(row) if row is not None else None

    async def list_for_run(self, run_id: UUID) -> tuple[ProductionRoutingDecisionV1, ...]:
        rows = await self._session.scalars(
            select(ProductionRoutingDecisionRow)
            .where(ProductionRoutingDecisionRow.production_run_id == run_id)
            .order_by(
                ProductionRoutingDecisionRow.pipeline_generation,
                ProductionRoutingDecisionRow.decided_at,
                ProductionRoutingDecisionRow.id,
            )
        )
        return tuple(_decision_from_row(row) for row in rows)

    async def _validate_artifact_ownership(self, decision: ProductionRoutingDecisionV1) -> None:
        # A review decision may preserve a bad cross-run artifact identity as
        # evidence of the provenance failure. A route that selects a format
        # must only cite exact artifacts owned by this run and stage.
        if decision.status is RoutingStatus.NEEDS_REVIEW:
            return
        for artifact_id, expected_stage in (
            (decision.extraction_artifact_id, "extraction"),
            (decision.relevance_artifact_id, "relevance_projection"),
        ):
            if artifact_id is None:
                raise ValueError("a decided route requires both source artifacts")
            artifact_exists = await self._session.scalar(
                select(ProductionArtifactRow.id).where(
                    ProductionArtifactRow.id == artifact_id,
                    ProductionArtifactRow.production_run_id == decision.production_run_id,
                    ProductionArtifactRow.stage == expected_stage,
                    ProductionArtifactRow.status == "verified",
                )
            )
            if artifact_exists is None:
                raise ValueError("routing artifact ownership or stage is invalid")


def _decision_values(decision: ProductionRoutingDecisionV1) -> dict[str, Any]:
    return {
        "id": decision.decision_id,
        "production_run_id": decision.production_run_id,
        "pipeline_generation": decision.pipeline_generation,
        "extraction_artifact_id": decision.extraction_artifact_id,
        "extraction_artifact_hash": decision.extraction_artifact_hash,
        "relevance_artifact_id": decision.relevance_artifact_id,
        "relevance_artifact_hash": decision.relevance_artifact_hash,
        "input_hash": decision.input_hash,
        "policy_version": decision.policy_version,
        "status": decision.status.value,
        "publication_kind": (
            decision.publication_kind.value if decision.publication_kind is not None else None
        ),
        "reason_code": decision.reason_code.value,
        "confirmed_ioc_count": decision.confirmed_ioc_count,
        "contextual_ioc_count": decision.contextual_ioc_count,
        "ambiguous_ioc_count": decision.ambiguous_ioc_count,
        "critical_source_diagnostics": [
            item.to_json() for item in decision.critical_source_diagnostics
        ],
        "decided_at": decision.decided_at,
        "decision_hash": decision.decision_hash,
    }


def _decision_from_row(row: ProductionRoutingDecisionRow) -> ProductionRoutingDecisionV1:
    diagnostics = tuple(
        RoutingCriticalSourceDiagnosticV1(
            source_id=item["source_id"],
            source_url=item["source_url"],
            failure_code=item["failure_code"],
            tier=item["tier"],
            criticality=ProductionSourceCriticality(item["criticality"]),
            criticality_reason=item["criticality_reason"],
            policy_version=item["policy_version"],
        )
        for item in row.critical_source_diagnostics
    )
    return ProductionRoutingDecisionV1(
        decision_id=row.id,
        production_run_id=row.production_run_id,
        pipeline_generation=row.pipeline_generation,
        extraction_artifact_id=row.extraction_artifact_id,
        extraction_artifact_hash=row.extraction_artifact_hash,
        relevance_artifact_id=row.relevance_artifact_id,
        relevance_artifact_hash=row.relevance_artifact_hash,
        input_hash=row.input_hash,
        policy_version=row.policy_version,
        status=RoutingStatus(row.status),
        publication_kind=(
            PublicationKind(row.publication_kind) if row.publication_kind is not None else None
        ),
        reason_code=RoutingReasonCode(row.reason_code),
        confirmed_ioc_count=row.confirmed_ioc_count,
        contextual_ioc_count=row.contextual_ioc_count,
        ambiguous_ioc_count=row.ambiguous_ioc_count,
        critical_source_diagnostics=diagnostics,
        decided_at=row.decided_at,
        decision_hash=row.decision_hash,
    )
