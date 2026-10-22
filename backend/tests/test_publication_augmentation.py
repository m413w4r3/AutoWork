from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_augmentation import (
    PublicationAugmentationArtifactService,
    PublicationAugmentationError,
    PublicationAugmentationErrorCode,
    augment_publication_with_opinion,
)
from cti_app.application.typst_rendering import project_publication_to_typst_model
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    serialize_publication_document,
)
from tests.test_publication_document_v6 import _approved_opinion, _base_v5


def test_augmentation_projects_ordered_blocks_and_keeps_v5_bytes_unchanged() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    base_payload = serialize_publication_document(base)
    base_bytes = json.dumps(
        base_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    augmented = augment_publication_with_opinion(base, opinion, context)
    model = project_publication_to_typst_model(augmented)

    assert augmented.base_publication is base
    assert serialize_publication_document(base) == base_payload
    base_bytes_after = json.dumps(
        serialize_publication_document(base),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    opinion_section = next(
        section for section in model.content_sections if section["type"] == "analyst_opinion"
    )
    assert opinion_section["blocks"][0]["number"] == "1.1"
    assert opinion_section["blocks"][0]["qualification_label"] == "Fait étayé"
    assert model.content_sections[-1]["type"] == "technical_annex"
    assert serialize_publication_document(augmented)["base_publication"] == base_payload
    assert base_bytes_after == base_bytes


def test_augmentation_rejects_an_unverifiable_citation() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    missing_id = UUID(int=908)
    changed_block = replace(opinion.blocks[0], finding_refs=(missing_id,))
    changed_opinion = replace(opinion, blocks=(changed_block,), content_sha256="")

    with pytest.raises(PublicationAugmentationError) as caught:
        augment_publication_with_opinion(base, changed_opinion, context)

    assert caught.value.code is PublicationAugmentationErrorCode.CITATIONS_INVALID
    assert any(item.reference_id == missing_id for item in caught.value.citation_violations)

    foreign_finding = replace(
        context.findings[0],
        investigation_id=UUID(int=999),
    )
    with pytest.raises(PublicationAugmentationError) as foreign_error:
        augment_publication_with_opinion(
            base,
            opinion,
            replace(context, findings=(foreign_finding,)),
        )
    assert foreign_error.value.code is PublicationAugmentationErrorCode.CITATIONS_INVALID


def test_augmentation_rejects_draft_and_declared_disclosure_violations() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    draft = replace(
        opinion,
        status=AnalystOpinionStatus.DRAFT,
        reviewer_id=None,
        content_sha256="",
    )
    with pytest.raises(PublicationAugmentationError) as draft_error:
        augment_publication_with_opinion(base, draft, context)
    assert draft_error.value.code is PublicationAugmentationErrorCode.OPINION_NOT_APPROVED

    with pytest.raises(PublicationAugmentationError) as tlp_error:
        augment_publication_with_opinion(base, opinion, replace(context, tlp_violation=True))
    assert tlp_error.value.code is PublicationAugmentationErrorCode.TLP_VIOLATION

    with pytest.raises(PublicationAugmentationError) as submit_error:
        augment_publication_with_opinion(base, opinion, replace(context, do_not_submit=True))
    assert submit_error.value.code is PublicationAugmentationErrorCode.DO_NOT_SUBMIT


def test_empty_opinions_do_not_create_an_empty_render_section() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    empty = replace(
        opinion,
        blocks=(replace(opinion.blocks[0], text=" "),),
        content_sha256="",
    )
    with pytest.raises(PublicationAugmentationError) as caught:
        augment_publication_with_opinion(base, empty, context)
    assert caught.value.code is PublicationAugmentationErrorCode.OPINION_EMPTY

    assert isinstance(base, PublicationDocumentV5)
    v5_model = project_publication_to_typst_model(base)
    assert all(section["type"] != "analyst_opinion" for section in v5_model.content_sections)


class _ArtifactRepository:
    def __init__(self, artifacts: tuple[ProductionArtifact, ...]) -> None:
        self.items = list(artifacts)

    async def get_current_for_revision(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        matches = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(matches, key=lambda item: item.version) if matches else None

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return next((item for item in self.items if item.id == artifact_id), None)

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)


class _InvestigationRepository:
    def __init__(self, investigation: object) -> None:
        self.investigation = investigation

    async def get(self, investigation_id: UUID) -> object:
        del investigation_id
        return self.investigation


class _AugmentationUow:
    def __init__(self, artifacts: _ArtifactRepository, investigation: object) -> None:
        self.production_artifacts = artifacts
        self.analyst_investigations = _InvestigationRepository(investigation)
        self.committed = False

    async def __aenter__(self) -> _AugmentationUow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _AugmentationStore:
    def __init__(self, blobs: dict[UUID, bytes]) -> None:
        self.blobs = blobs

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self.blobs[blob_id]

    async def store_stage_payloads(
        self, *, canonical: dict[str, object]
    ) -> tuple[None, UUID, None]:
        blob_id = UUID(int=len(self.blobs) + 1000)
        self.blobs[blob_id] = ProductionArtifactStore.canonical_json_bytes(canonical)
        return None, blob_id, None


class _PreviewRenderer:
    def __init__(self) -> None:
        self.artifact_ids: list[UUID] = []

    async def render_preview(self, publication_artifact_id: UUID) -> None:
        self.artifact_ids.append(publication_artifact_id)


@pytest.mark.asyncio
async def test_artifact_service_appends_a_linked_version_without_changing_the_v5_blob() -> None:
    base = _base_v5()
    opinion, context = _approved_opinion(base)
    synthesis_bytes = b"verified canonical synthesis"
    opinion = replace(
        opinion,
        synthesis_sha256=hashlib.sha256(synthesis_bytes).hexdigest(),
        content_sha256="",
    )
    run_id = UUID(int=911)
    base_blob_id = UUID(int=912)
    synthesis_blob_id = UUID(int=913)
    base_payload = serialize_publication_document(base)
    base_bytes = ProductionArtifactStore.canonical_json_bytes(base_payload)
    assert hashlib.sha256(base_bytes).hexdigest() == context.base_publication_sha256
    blobs = {base_blob_id: base_bytes, synthesis_blob_id: synthesis_bytes}
    base_artifact = ProductionArtifact(
        id=context.base_publication_artifact_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=4,
        input_hash="c" * 64,
        canonical_blob_id=base_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        id=opinion.synthesis_artifact_id,
        production_run_id=run_id,
        subject_id=base.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=3,
        input_hash="d" * 64,
        canonical_blob_id=synthesis_blob_id,
    )
    repository = _ArtifactRepository((base_artifact, synthesis_artifact))
    investigation = SimpleNamespace(
        production_run_id=run_id,
        subject_id=base.subject_id,
        synthesis_artifact_id=opinion.synthesis_artifact_id,
    )
    uow = _AugmentationUow(repository, investigation)
    store = _AugmentationStore(blobs)
    preview = _PreviewRenderer()
    service = PublicationAugmentationArtifactService(
        uow_factory=lambda: uow,
        artifact_store=store,  # type: ignore[arg-type]
        preview_renderer=preview,
    )

    publication = await service.publish(
        base_publication_artifact_id=base_artifact.id,
        opinion=opinion,
        citation_context=context,
    )

    assert publication.stage is ProductionArtifactStage.PUBLICATION
    assert publication.version == 5
    assert publication.status is ProductionArtifactStatus.VERIFIED
    assert publication.metadata["previous_publication_artifact_id"] == str(base_artifact.id)
    assert publication.canonical_blob_id != base_blob_id
    assert blobs[base_blob_id] == base_bytes
    assert uow.committed is True
    assert preview.artifact_ids == [publication.id]
