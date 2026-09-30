"""Canonical, side-effect-free description of production stage dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from cti_app.domain.production import ProductionArtifactStage, ProductionStage


@dataclass(frozen=True, slots=True)
class ProductionStageSpec:
    stage: ProductionStage
    artifact_stage: ProductionArtifactStage | None


PRODUCTION_PIPELINE: tuple[ProductionStageSpec, ...] = (
    ProductionStageSpec(ProductionStage.SOURCES, None),
    ProductionStageSpec(ProductionStage.REFERENCES, ProductionArtifactStage.REFERENCES),
    ProductionStageSpec(ProductionStage.EXTRACTION, ProductionArtifactStage.EXTRACTION),
    ProductionStageSpec(ProductionStage.SYNTHESIS, ProductionArtifactStage.SYNTHESIS),
    ProductionStageSpec(
        ProductionStage.EDITORIAL_ENRICHMENT,
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
    ),
    ProductionStageSpec(ProductionStage.ASSEMBLY, ProductionArtifactStage.PUBLICATION),
)


def _validate_pipeline() -> None:
    stages = tuple(spec.stage for spec in PRODUCTION_PIPELINE)
    artifacts = tuple(
        spec.artifact_stage for spec in PRODUCTION_PIPELINE if spec.artifact_stage is not None
    )
    if len(stages) != len(set(stages)):
        raise RuntimeError("Production pipeline stages must be unique")
    if set(stages) != set(ProductionStage):
        raise RuntimeError("Every production stage must appear exactly once in the pipeline")
    if len(artifacts) != len(set(artifacts)):
        raise RuntimeError("Production pipeline artifact stages must be unique")
    if set(artifacts) != set(ProductionArtifactStage):
        raise RuntimeError("Every production artifact stage must map to one pipeline stage")
    stages_without_artifacts = tuple(
        spec.stage for spec in PRODUCTION_PIPELINE if spec.artifact_stage is None
    )
    if stages_without_artifacts != (ProductionStage.SOURCES,):
        raise RuntimeError("SOURCES must be the only production stage without an artifact")
    if not PRODUCTION_PIPELINE or PRODUCTION_PIPELINE[-1].stage is not ProductionStage.ASSEMBLY:
        raise RuntimeError("ASSEMBLY must be the final production stage")
    if PRODUCTION_PIPELINE[-1].artifact_stage is not ProductionArtifactStage.PUBLICATION:
        raise RuntimeError("ASSEMBLY must produce the PUBLICATION artifact")


_validate_pipeline()


def production_stages() -> tuple[ProductionStage, ...]:
    return tuple(spec.stage for spec in PRODUCTION_PIPELINE)


def production_artifact_stages() -> tuple[ProductionArtifactStage, ...]:
    return tuple(
        spec.artifact_stage for spec in PRODUCTION_PIPELINE if spec.artifact_stage is not None
    )


def stage_spec(stage: ProductionStage) -> ProductionStageSpec:
    if not isinstance(stage, ProductionStage):
        raise TypeError("stage_spec requires a ProductionStage")
    for spec in PRODUCTION_PIPELINE:
        if spec.stage is stage:
            return spec
    raise ValueError(f"Stage {stage.value} is not valid for the production pipeline")


def artifact_stage_for(stage: ProductionStage) -> ProductionArtifactStage | None:
    return stage_spec(stage).artifact_stage


def prerequisite_artifact_for(stage: ProductionStage) -> ProductionArtifactStage | None:
    spec = stage_spec(stage)
    index = PRODUCTION_PIPELINE.index(spec)
    for previous in reversed(PRODUCTION_PIPELINE[:index]):
        if previous.artifact_stage is not None:
            return previous.artifact_stage
    return None


def pipeline_stage_for_artifact(
    artifact_stage: ProductionArtifactStage,
) -> ProductionStage:
    if not isinstance(artifact_stage, ProductionArtifactStage):
        raise TypeError("pipeline_stage_for_artifact requires a ProductionArtifactStage")
    for spec in PRODUCTION_PIPELINE:
        if spec.artifact_stage is artifact_stage:
            return spec.stage
    raise ValueError(f"Artifact stage {artifact_stage.value} is not in the production pipeline")


def next_stage(stage: ProductionStage) -> ProductionStage | None:
    if not isinstance(stage, ProductionStage):
        raise TypeError("next_stage requires a ProductionStage")
    stages = production_stages()
    try:
        index = stages.index(stage)
    except ValueError as exc:
        raise ValueError(f"Stage {stage.value} is not valid for the production pipeline") from exc
    return stages[index + 1] if index + 1 < len(stages) else None


def downstream_artifacts_from_pipeline_stage(
    stage: ProductionStage,
    *,
    inclusive: bool = False,
) -> tuple[ProductionArtifactStage, ...]:
    if not isinstance(stage, ProductionStage):
        raise TypeError("downstream helper requires a ProductionStage")
    pipeline_stages = production_stages()
    try:
        start = pipeline_stages.index(stage) + (0 if inclusive else 1)
    except ValueError as exc:
        raise ValueError(f"Stage {stage.value} is not valid for the production pipeline") from exc
    return tuple(
        spec.artifact_stage
        for spec in PRODUCTION_PIPELINE[start:]
        if spec.artifact_stage is not None
    )


def downstream_artifacts_from_artifact_stage(
    stage: ProductionArtifactStage,
    *,
    inclusive: bool = False,
) -> tuple[ProductionArtifactStage, ...]:
    if not isinstance(stage, ProductionArtifactStage):
        raise TypeError("downstream helper requires a ProductionArtifactStage")
    pipeline_stages = production_artifact_stages()
    try:
        start = pipeline_stages.index(stage) + (0 if inclusive else 1)
    except ValueError as exc:
        raise ValueError(f"Artifact stage {stage.value} is not in the production pipeline") from exc
    return pipeline_stages[start:]


__all__ = [
    "PRODUCTION_PIPELINE",
    "ProductionStageSpec",
    "artifact_stage_for",
    "downstream_artifacts_from_artifact_stage",
    "downstream_artifacts_from_pipeline_stage",
    "next_stage",
    "pipeline_stage_for_artifact",
    "prerequisite_artifact_for",
    "production_artifact_stages",
    "production_stages",
    "stage_spec",
]
