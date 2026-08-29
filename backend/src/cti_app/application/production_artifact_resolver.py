"""Current publication artifact resolution with historical fallback."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage


class _ArtifactRepository(Protocol):
    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None: ...


async def current_publication_artifact(
    repository: _ArtifactRepository, run_id: UUID
) -> ProductionArtifact | None:
    """Resolve PUBLICATION first and BRIEF only for historical runs."""

    current = await repository.get_current(run_id, ProductionArtifactStage.PUBLICATION.value)
    if current is not None:
        return current
    return await repository.get_current(run_id, ProductionArtifactStage.BRIEF.value)
