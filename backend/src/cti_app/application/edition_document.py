"""Shared, read-only construction of the canonical edition document."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionPublicationV2,
)
from cti_app.domain.editions import Edition
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication import PublicationDocumentV2, publication_document_from_json


@dataclass(frozen=True, slots=True)
class EditionDocumentArtifactRef:
    """The artifact identity permitted to contribute one edition article."""

    position: int
    subject_id: UUID
    production_run_id: UUID
    pipeline_generation: int
    artifact_id: UUID
    artifact_version: int
    input_hash: str


class EditionDocumentBuildError(ValueError):
    """A referenced publication artifact cannot form the canonical document."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def edition_metadata_projection(edition: Edition) -> dict[str, Any]:
    """Return the stable metadata shared by preview and final publication."""
    return {
        "id": str(edition.id),
        "country": edition.country,
        "country_code": edition.country_code,
        "period_start": edition.period_start.isoformat(),
        "period_end": edition.period_end.isoformat(),
        "tlp": edition.tlp.value,
        "languages": list(edition.languages),
        "previous_edition_id": (
            str(edition.previous_edition_id) if edition.previous_edition_id else None
        ),
        "source_profile": edition.source_profile,
    }


async def build_edition_document(
    uow: Any,
    artifact_store: ProductionArtifactStore,
    edition: Edition,
    refs: tuple[EditionDocumentArtifactRef, ...],
    *,
    require_current: bool,
) -> EditionDocumentV2:
    """Read verified publication artifacts and build the one canonical model.

    Preview passes ``require_current=True``. Final assembly passes ``False``
    because its manifest already pins the immutable artifact identities.
    Both paths use this same constructor and ordering contract.
    """
    publications: list[EditionPublicationV2] = []
    for ref in refs:
        run = await uow.subject_production_runs.get(ref.production_run_id)
        artifact = await uow.production_artifacts.get(ref.artifact_id)
        current = (
            await uow.production_artifacts.get_current(
                ref.production_run_id, ProductionArtifactStage.PUBLICATION.value
            )
            if require_current
            else artifact
        )
        if (
            run is None
            or artifact is None
            or current is None
            or (require_current and current.id != artifact.id)
            or run.edition_id != edition.id
            or run.subject_id != ref.subject_id
            or run.pipeline_generation != ref.pipeline_generation
            or artifact.production_run_id != ref.production_run_id
            or artifact.subject_id != ref.subject_id
            or artifact.version != ref.artifact_version
            or artifact.input_hash != ref.input_hash
            or artifact.stage is not ProductionArtifactStage.PUBLICATION
            or artifact.status is not ProductionArtifactStatus.VERIFIED
            or artifact.canonical_blob_id is None
        ):
            raise EditionDocumentBuildError("edition_document_artifact_mismatch")

        try:
            payload = await artifact_store.read_json(artifact.canonical_blob_id)
            publication = publication_document_from_json(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise EditionDocumentBuildError("publication_document_invalid") from exc
        if not isinstance(publication, PublicationDocumentV2):
            raise EditionDocumentBuildError("publication_document_schema_mismatch")
        publications.append(
            EditionPublicationV2(
                position=ref.position,
                subject_id=ref.subject_id,
                document=publication,
            )
        )

    try:
        return EditionDocumentV2(
            edition=edition_metadata_projection(edition),
            publications=tuple(publications),
        )
    except ValueError as exc:
        raise EditionDocumentBuildError("edition_document_invalid") from exc


__all__ = [
    "EditionDocumentArtifactRef",
    "EditionDocumentBuildError",
    "build_edition_document",
    "edition_metadata_projection",
]
