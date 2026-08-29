from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    SubjectProductionStage,
    production_stages,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_ID = UUID("22222222-2222-4222-8222-222222222222")


def _artifact(stage: ProductionArtifactStage) -> ProductionArtifact:
    return ProductionArtifact(
        id=uuid4(),
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=stage,
        version=1,
        input_hash="a" * 64,
    )


class _Repository:
    def __init__(self, publication: ProductionArtifact | None, brief: ProductionArtifact | None):
        self.artifacts = {
            ProductionArtifactStage.PUBLICATION.value: publication,
            ProductionArtifactStage.BRIEF.value: brief,
        }
        self.calls: list[str] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        assert run_id == RUN_ID
        self.calls.append(stage)
        return self.artifacts[stage]


@pytest.mark.asyncio
async def test_publication_resolver_prefers_current_publication() -> None:
    publication = _artifact(ProductionArtifactStage.PUBLICATION)
    repository = _Repository(publication, _artifact(ProductionArtifactStage.BRIEF))

    assert await current_publication_artifact(repository, RUN_ID) is publication
    assert repository.calls == ["publication"]


@pytest.mark.asyncio
async def test_publication_resolver_falls_back_to_historical_brief() -> None:
    brief = _artifact(ProductionArtifactStage.BRIEF)
    repository = _Repository(None, brief)

    assert await current_publication_artifact(repository, RUN_ID) is brief
    assert repository.calls == ["publication", "brief"]


def test_current_pipeline_contains_only_the_five_article_stages() -> None:
    assert production_stages() == (
        SubjectProductionStage.SOURCES,
        SubjectProductionStage.REFERENCES,
        SubjectProductionStage.EXTRACTION,
        SubjectProductionStage.SYNTHESIS,
        SubjectProductionStage.ASSEMBLY,
    )
