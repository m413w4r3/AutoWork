from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.analyst_opinion_preview import AnalystOpinionPreviewService
from cti_app.application.analyst_opinion_service import OpinionError, OpinionErrorCode
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_title import publication_title_matches_synthesis
from cti_app.application.typst_compilation import CompiledTypstDocument
from cti_app.application.typst_rendering import TypstDraftPreviewDocument
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_synthesis import production_synthesis_from_json
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_from_json,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
)
from tests.test_analyst_opinion_service import make_opinion_harness


def _correction_artifact(
    harness,
    original_document: PublicationDocumentV5,
    synthesis_title: str,
    replacement_title: str,
) -> tuple[PublicationDocumentV5, ProductionArtifact, bytes]:
    semantic_text = replace(
        original_document.semantic_text,
        paragraphs=tuple(
            SemanticParagraphV1(
                paragraph.anchor,
                (SemanticTextSpanV1(SemanticRole.TEXT, replacement_title),),
            )
            if paragraph.anchor == "title"
            else paragraph
            for paragraph in original_document.semantic_text.paragraphs
        ),
    )
    corrected_document = replace(
        original_document,
        document=replace(original_document.document, title=replacement_title),
        semantic_text=semantic_text,
    )
    payload = serialize_publication_document(corrected_document)
    canonical_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
    parent = harness.base_artifact
    correction = {
        "schema": "publication-text-correction.v1",
        "parent_artifact_id": str(parent.id),
        "parent_version": parent.version,
        "parent_input_hash": parent.input_hash,
        "before_sha256": hashlib.sha256(harness.base_bytes).hexdigest(),
        "before_v5_sha256": hashlib.sha256(harness.base_bytes).hexdigest(),
        "target_path": "title",
        "before_text_sha256": hashlib.sha256(synthesis_title.encode("utf-8")).hexdigest(),
        "after_text_sha256": hashlib.sha256(replacement_title.encode("utf-8")).hexdigest(),
        "after_sha256": hashlib.sha256(canonical_bytes).hexdigest(),
        "semantic_spans_before": [{"role": "text", "text": synthesis_title}],
        "semantic_spans_after": [{"role": "text", "text": replacement_title}],
        "validation": {
            "method": "canonical_publication_qa_v5_with_declared_text_edit",
            "passed": True,
            "field_path": "title",
            "checks_before": {"exact_projection": True, "title_format": True},
            "checks_after": {"exact_projection": False, "title_format": True},
            "accepted_exception": "exact_projection",
            "warnings": [],
        },
    }
    blob_id = uuid4()
    artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=parent.production_run_id,
        subject_id=parent.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=parent.version + 1,
        input_hash=hashlib.sha256(canonical_bytes).hexdigest(),
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=blob_id,
        metadata={"manual_publication_text_correction": correction},
    )
    harness.store.blobs[blob_id] = canonical_bytes
    harness.artifacts.items.append(artifact)
    return corrected_document, artifact, canonical_bytes


class _PreviewRenderer:
    def __init__(self) -> None:
        self.base_artifact: ProductionArtifact | None = None

    async def render_draft_preview(
        self,
        document,
        opinion,
        synthesis,
        *,
        base_publication_identity=None,
        base_publication_artifact=None,
        infrastructure_graph=None,
        findings=(),
        evidence_observations=(),
    ) -> CompiledTypstDocument:
        del (
            document,
            opinion,
            synthesis,
            base_publication_identity,
            infrastructure_graph,
            findings,
            evidence_observations,
        )
        self.base_artifact = base_publication_artifact
        content = b"%PDF-1.4\nverified corrected title\n"
        return CompiledTypstDocument(
            media_type="application/pdf",
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            compiler="typst",
            compiler_version="0.15.1",
        )


class _NoGraph:
    async def project(self, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_opinion_preview_accepts_only_an_exact_accepted_title_correction() -> None:
    harness = make_opinion_harness()
    original_document = publication_document_from_json(json.loads(harness.base_bytes))
    assert isinstance(original_document, PublicationDocumentV5)
    synthesis_artifact = next(
        item for item in harness.artifacts.items if item.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert synthesis_artifact.canonical_blob_id is not None
    synthesis = production_synthesis_from_json(
        json.loads(harness.store.blobs[synthesis_artifact.canonical_blob_id])
    )
    assert synthesis.title == original_document.title
    corrected_document, corrected_artifact, corrected_bytes = _correction_artifact(
        harness,
        original_document,
        synthesis.title,
        "[Example publisher] WebSockets campaign in Region X",
    )
    opinion = await harness.service.compose_from_findings(harness.investigation.id)
    renderer = _PreviewRenderer()
    preview_service = AnalystOpinionPreviewService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        renderer=renderer,  # type: ignore[arg-type]
        graph_source=_NoGraph(),  # type: ignore[arg-type]
    )

    preview = await preview_service.preview_pdf(
        opinion.id,
        version=opinion.version,
        expected_opinion_sha256=opinion.content_sha256,
        expected_base_publication_sha256=hashlib.sha256(corrected_bytes).hexdigest(),
    )

    assert preview.content.startswith(b"%PDF-")
    assert renderer.base_artifact == corrected_artifact
    assert publication_title_matches_synthesis(corrected_artifact, corrected_document, synthesis)

    invalid_metadata = dict(corrected_artifact.metadata)
    invalid_correction = dict(invalid_metadata["manual_publication_text_correction"])
    invalid_correction["target_path"] = "lead[0].text"
    invalid_metadata["manual_publication_text_correction"] = invalid_correction
    invalid_artifact = replace(corrected_artifact, metadata=invalid_metadata)
    harness.artifacts.items[-1] = invalid_artifact
    assert not publication_title_matches_synthesis(invalid_artifact, corrected_document, synthesis)
    with pytest.raises(OpinionError) as rejected:
        await preview_service.preview_pdf(
            opinion.id,
            version=opinion.version,
            expected_opinion_sha256=opinion.content_sha256,
            expected_base_publication_sha256=hashlib.sha256(corrected_bytes).hexdigest(),
        )
    assert rejected.value.code is OpinionErrorCode.STALE_BASE


@pytest.mark.asyncio
async def test_typst_draft_preview_uses_the_same_title_provenance_gate() -> None:
    harness = make_opinion_harness()
    original_document = publication_document_from_json(json.loads(harness.base_bytes))
    assert isinstance(original_document, PublicationDocumentV5)
    synthesis_artifact = next(
        item for item in harness.artifacts.items if item.stage is ProductionArtifactStage.SYNTHESIS
    )
    assert synthesis_artifact.canonical_blob_id is not None
    synthesis = production_synthesis_from_json(
        json.loads(harness.store.blobs[synthesis_artifact.canonical_blob_id])
    )
    corrected_document, corrected_artifact, _ = _correction_artifact(
        harness,
        original_document,
        synthesis.title,
        "[Example publisher] WebSockets campaign in Region X",
    )
    opinion = await harness.service.compose_from_findings(harness.investigation.id)
    opinion = replace(
        opinion,
        base_publication_artifact_id=corrected_artifact.id,
        base_publication_sha256=publication_document_v5_sha256(corrected_document),
        content_sha256="",
    )

    preview = TypstDraftPreviewDocument(
        corrected_document,
        opinion,
        synthesis,
        base_publication_body_sha256=publication_document_v5_sha256(corrected_document),
        base_publication_artifact=corrected_artifact,
    )

    assert preview.base_publication.title == corrected_document.title
    with pytest.raises(ValueError, match="synthesis does not match"):
        TypstDraftPreviewDocument(
            corrected_document,
            opinion,
            synthesis,
            base_publication_body_sha256=publication_document_v5_sha256(corrected_document),
        )
