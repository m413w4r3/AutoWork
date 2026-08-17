"""Evidence extraction service for production workflow."""
from __future__ import annotations

from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWork
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage


class SubjectEvidenceService:
    """Extracts evidence (claims and indicators) from source documents."""

    def __init__(self, uow_factory: ProductionUnitOfWork) -> None:
        self._uow_factory = uow_factory

    async def extract_source(self, collection_id: UUID) -> None:
        """Extract evidence from a single archived source collection.

        Creates:
        - DerivedArtifact with parsed text
        - Claims with exact quotes
        - Indicators with deterministic extraction

        Idempotent: skips if artifact already exists.
        """
        # Implementation will be added when integrating
        # with actual EvidenceExtractionService
        pass

    async def extract_subject(
        self,
        subject_id: UUID,
        only_pending: bool = True,
    ) -> None:
        """Extract evidence from all source collections for a subject.

        Idempotent: skips sources that already have extracted evidence.
        """
        # Implementation will be added when integrating
        # with actual evidence extraction workflow
        pass
