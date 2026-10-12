"""Resolve canonical source artifact identities for rendered publication blocks."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)


async def publication_target_artifact_identities(
    production_artifacts: Any,
    publication_artifact: ProductionArtifact,
    *,
    expected_publication_version: int | None = None,
    expected_publication_input_hash: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve the exact synthesis/enrichment artifacts recorded by PUBLICATION.

    A rendered article title belongs to SYNTHESIS. Keyed enrichment blocks
    belong to EDITORIAL_ENRICHMENT. The publication artifact is the rendered
    container, not the correction fence for either target family.
    """
    if (
        publication_artifact.stage is not ProductionArtifactStage.PUBLICATION
        or publication_artifact.status is not ProductionArtifactStatus.VERIFIED
        or publication_artifact.canonical_blob_id is None
        or (
            expected_publication_version is not None
            and publication_artifact.version != expected_publication_version
        )
        or (
            expected_publication_input_hash is not None
            and publication_artifact.input_hash != expected_publication_input_hash
        )
    ):
        return {}

    refs = publication_artifact.metadata.get("input_artifacts")
    if not isinstance(refs, Mapping):
        return {}

    resolved: dict[str, dict[str, Any]] = {}
    for target_family, ref_name, expected_stage in (
        ("article_title", "synthesis_artifact_id", ProductionArtifactStage.SYNTHESIS),
        (
            "enrichment",
            "editorial_enrichment_artifact_id",
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        ),
    ):
        raw_id = refs.get(ref_name)
        try:
            artifact_id = UUID(str(raw_id))
        except (TypeError, ValueError, AttributeError):
            continue
        artifact = await production_artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.id != artifact_id
            or artifact.stage is not expected_stage
            or artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
            or artifact.subject_id != publication_artifact.subject_id
            or artifact.production_run_id != publication_artifact.production_run_id
        ):
            continue
        resolved[target_family] = {
            "subject_id": str(artifact.subject_id),
            "artifact_id": str(artifact.id),
            "artifact_version": artifact.version,
            "artifact_input_hash": artifact.input_hash,
        }
    return resolved
