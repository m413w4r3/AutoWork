"""Production wiring for repair materialization and shared editorial effects."""

from __future__ import annotations

from typing import Any, cast

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.editorial_revision_effects import EditorialRevisionEffects
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.persistence import ProductionUnitOfWorkFactory, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionService,
)


def create_production_repair_materialization_service(
    uow_factory: ProductionUnitOfWorkFactory,
    *,
    artifact_store: ProductionArtifactStore | None,
    projection_service: ProductionRepairProjectionService | None = None,
    checkpoint_service: Any | None = None,
    diagnostics: DiagnosticsLog | None = None,
) -> ProductionRepairMaterializationService:
    """Build the production repair service with mandatory editorial effects."""
    if artifact_store is None:
        raise RuntimeError("production_repair_revision_effects_unavailable")
    revision_effects = EditorialRevisionEffects(
        workspace_service=EditorialWorkspaceService(
            uow_factory=cast(UnitOfWorkFactory, uow_factory),
            artifact_store=artifact_store,
        )
    )
    return ProductionRepairMaterializationService(
        uow_factory,
        projection_service=projection_service,
        checkpoint_service=checkpoint_service,
        artifact_store=artifact_store,
        diagnostics=diagnostics,
        revision_effects=revision_effects,
    )
