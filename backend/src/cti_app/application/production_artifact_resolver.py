"""Current publication artifact resolution."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage


class _ArtifactRepository(Protocol):
    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None: ...


async def current_publication_artifact(
    repository: _ArtifactRepository, run_id: UUID
) -> ProductionArtifact | None:
    """Resolve the current PUBLICATION artifact only."""

    return await repository.get_current(run_id, ProductionArtifactStage.PUBLICATION.value)
