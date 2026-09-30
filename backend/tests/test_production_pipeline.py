from __future__ import annotations

import pytest

from cti_app.domain.production import ProductionArtifactStage, ProductionStage
from cti_app.domain.production_pipeline import (
    PRODUCTION_PIPELINE,
    ProductionStageSpec,
    artifact_stage_for,
    downstream_artifacts_from_artifact_stage,
    downstream_artifacts_from_pipeline_stage,
    next_stage,
    pipeline_stage_for_artifact,
    prerequisite_artifact_for,
    production_artifact_stages,
    production_stages,
    stage_spec,
)


def test_production_stage_order_and_stage_artifact_mapping() -> None:
    assert production_stages() == (
        ProductionStage.SOURCES,
        ProductionStage.REFERENCES,
        ProductionStage.EXTRACTION,
        ProductionStage.SYNTHESIS,
        ProductionStage.EDITORIAL_ENRICHMENT,
        ProductionStage.ASSEMBLY,
    )
    assert tuple(artifact_stage_for(stage) for stage in production_stages()) == (
        None,
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        ProductionArtifactStage.PUBLICATION,
    )
    assert production_artifact_stages() == (
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        ProductionArtifactStage.PUBLICATION,
    )
    assert stage_spec(ProductionStage.SOURCES) == ProductionStageSpec(ProductionStage.SOURCES, None)


@pytest.mark.parametrize(
    ("artifact_stage", "pipeline_stage"),
    [
        (ProductionArtifactStage.REFERENCES, ProductionStage.REFERENCES),
        (ProductionArtifactStage.EXTRACTION, ProductionStage.EXTRACTION),
        (ProductionArtifactStage.SYNTHESIS, ProductionStage.SYNTHESIS),
        (ProductionArtifactStage.EDITORIAL_ENRICHMENT, ProductionStage.EDITORIAL_ENRICHMENT),
        (ProductionArtifactStage.PUBLICATION, ProductionStage.ASSEMBLY),
    ],
)
def test_reverse_artifact_mapping(
    artifact_stage: ProductionArtifactStage, pipeline_stage: ProductionStage
) -> None:
    assert pipeline_stage_for_artifact(artifact_stage) is pipeline_stage


def test_prerequisite_artifacts_follow_the_pipeline() -> None:
    assert tuple(prerequisite_artifact_for(stage) for stage in production_stages()) == (
        None,
        None,
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
    )


@pytest.mark.parametrize(
    ("stage", "successor"),
    [
        (ProductionStage.SOURCES, ProductionStage.REFERENCES),
        (ProductionStage.REFERENCES, ProductionStage.EXTRACTION),
        (ProductionStage.EXTRACTION, ProductionStage.SYNTHESIS),
        (ProductionStage.SYNTHESIS, ProductionStage.EDITORIAL_ENRICHMENT),
        (ProductionStage.EDITORIAL_ENRICHMENT, ProductionStage.ASSEMBLY),
        (ProductionStage.ASSEMBLY, None),
    ],
)
def test_next_stage(stage: ProductionStage, successor: ProductionStage | None) -> None:
    assert next_stage(stage) is successor


@pytest.mark.parametrize(
    ("stage", "exclusive", "inclusive"),
    [
        (
            ProductionStage.SOURCES,
            (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
            (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionStage.REFERENCES,
            (
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
            (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionStage.EXTRACTION,
            (
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
            (
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionStage.SYNTHESIS,
            (ProductionArtifactStage.EDITORIAL_ENRICHMENT, ProductionArtifactStage.PUBLICATION),
            (
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionStage.EDITORIAL_ENRICHMENT,
            (ProductionArtifactStage.PUBLICATION,),
            (ProductionArtifactStage.EDITORIAL_ENRICHMENT, ProductionArtifactStage.PUBLICATION),
        ),
        (
            ProductionStage.ASSEMBLY,
            (),
            (ProductionArtifactStage.PUBLICATION,),
        ),
    ],
)
def test_downstream_artifacts_from_pipeline_stage(
    stage: ProductionStage,
    exclusive: tuple[ProductionArtifactStage, ...],
    inclusive: tuple[ProductionArtifactStage, ...],
) -> None:
    assert downstream_artifacts_from_pipeline_stage(stage) == exclusive
    assert downstream_artifacts_from_pipeline_stage(stage, inclusive=True) == inclusive


@pytest.mark.parametrize(
    ("stage", "exclusive", "inclusive"),
    [
        (
            ProductionArtifactStage.REFERENCES,
            (
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
            (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionArtifactStage.EXTRACTION,
            (
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
            (
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionArtifactStage.SYNTHESIS,
            (ProductionArtifactStage.EDITORIAL_ENRICHMENT, ProductionArtifactStage.PUBLICATION),
            (
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
        ),
        (
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
            (ProductionArtifactStage.PUBLICATION,),
            (ProductionArtifactStage.EDITORIAL_ENRICHMENT, ProductionArtifactStage.PUBLICATION),
        ),
        (
            ProductionArtifactStage.PUBLICATION,
            (),
            (ProductionArtifactStage.PUBLICATION,),
        ),
    ],
)
def test_downstream_artifacts_from_artifact_stage(
    stage: ProductionArtifactStage,
    exclusive: tuple[ProductionArtifactStage, ...],
    inclusive: tuple[ProductionArtifactStage, ...],
) -> None:
    assert downstream_artifacts_from_artifact_stage(stage) == exclusive
    assert downstream_artifacts_from_artifact_stage(stage, inclusive=True) == inclusive


def test_helpers_are_projections_of_pipeline_specs_for_future_stage_insertions() -> None:
    expected_stages = tuple(spec.stage for spec in PRODUCTION_PIPELINE)
    expected_artifacts = tuple(
        spec.artifact_stage for spec in PRODUCTION_PIPELINE if spec.artifact_stage is not None
    )
    assert production_stages() == expected_stages
    assert production_artifact_stages() == expected_artifacts
    for index, spec in enumerate(PRODUCTION_PIPELINE):
        expected_downstream = tuple(
            item.artifact_stage
            for item in PRODUCTION_PIPELINE[index:]
            if item.artifact_stage is not None
        )
        assert downstream_artifacts_from_pipeline_stage(spec.stage, inclusive=True) == (
            expected_downstream
        )
