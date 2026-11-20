"""Production wiring for repair materialization and shared editorial effects."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.editorial_revision_effects import EditorialRevisionEffects
from cti_app.application.editorial_revision_impact import EditorialProposalImpactService
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.model_gateway import ModelGateway
from cti_app.application.persistence import ProductionUnitOfWorkFactory, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import ProductionSynthesisService
from cti_app.application.production_synthesis_revision import ProductionSynthesisRevisionService


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


def create_editorial_proposal_impact_service(
    uow_factory: UnitOfWorkFactory,
    *,
    artifact_store: ProductionArtifactStore | None,
    model_gateway: ModelGateway,
    resource_search_enabled: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> EditorialProposalImpactService:
    """Build impact processing with the existing synthesis-enrichment rebase writer."""
    if artifact_store is None:
        raise RuntimeError("editorial_proposal_impact_artifact_store_unavailable")
    workspace_service = EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    revision_effects = EditorialRevisionEffects(workspace_service=workspace_service)
    synthesis_persistence = SynthesisService(uow_factory, artifact_store)
    synthesis_service = ProductionSynthesisService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_gateway=model_gateway,
        synthesis_service=synthesis_persistence,
    )
    synthesis_rebaser = ProductionSynthesisRevisionService(
        synthesis_service=synthesis_service,
        synthesis_persistence=synthesis_persistence,
        enrichment_persistence=EditorialEnrichmentService(uow_factory, artifact_store),
        publication_render_service=None,
        revision_effects=revision_effects,
        resource_search_enabled=resource_search_enabled,
    )
    return EditorialProposalImpactService(
        uow_factory=uow_factory,
        workspace_service=workspace_service,
        revision_effects=revision_effects,
        synthesis_rebaser=synthesis_rebaser,
        clock=clock,
    )
