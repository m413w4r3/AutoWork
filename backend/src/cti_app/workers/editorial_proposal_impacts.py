"""Dramatiq-facing adapter for the durable editorial impact outbox."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from cti_app.application.editorial_revision_impact import EditorialProposalImpactService
from cti_app.domain.editorial_proposal_impact import EditorialProposalImpact


class EditorialProposalImpactWorker:
    def __init__(self, service: EditorialProposalImpactService) -> None:
        self._service = service

    async def process(self, impact_id: UUID) -> EditorialProposalImpact | None:
        return await self._service.process(impact_id)

    async def dispatchable_ids(self, *, limit: int = 100) -> Sequence[UUID]:
        return await self._service.dispatchable_ids(limit=limit)
