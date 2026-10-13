from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_document import (
    EditionDocumentArtifactRef,
    build_edition_document,
)
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.domain.classification import TLP
from cti_app.domain.edition_publication import EditionDocumentV2
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
    SemanticTextV1,
)

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_IDS = (
    UUID("22222222-2222-4222-8222-222222222222"),
    UUID("33333333-3333-4333-8333-333333333333"),
)
RUN_IDS = (
    UUID("44444444-4444-4444-8444-444444444444"),
    UUID("55555555-5555-4555-8555-555555555555"),
)
ARTIFACT_IDS = (
    UUID("66666666-6666-4666-8666-666666666666"),
    UUID("77777777-7777-4777-8777-777777777777"),
)
BLOB_IDS = (uuid4(), uuid4())
LEGACY_POLICY_VERSION = "semantic-annotation-policy-v4-technical-literal-globs"


def _publication(subject_id: UUID, policy_version: str) -> PublicationDocumentV5:
    title = "Mandiant published the report."
    base = PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=subject_id,
        publication_language="fr",
        title=title,
        lead=(),
        sections=(),
        timeline=(),
        indicators=(),
        sources=(),
        uncertainties=(),
        tables=(),
        diagrams=(),
        figures=(),
    )
    return PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=policy_version,
            paragraphs=(
                SemanticParagraphV1(
                    anchor="title",
                    spans=(
                        SemanticTextSpanV1(SemanticRole.ACTOR, "Mandiant"),
                        SemanticTextSpanV1(SemanticRole.TEXT, " published the report."),
                    ),
                ),
            ),
        ),
    )


class _Repository:
    def __init__(self, values: dict[UUID, object]) -> None:
        self.values = values

    async def get(self, identity: UUID) -> object | None:
        return self.values.get(identity)


class _ArtifactStore:
    def __init__(self, payloads: dict[UUID, dict[str, object]]) -> None:
        self.payloads = payloads

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.payloads[blob_id]


@pytest.mark.asyncio
async def test_build_edition_document_preserves_v6_opinion_and_v5_article() -> None:
    base_documents = (
        _publication(SUBJECT_IDS[0], LEGACY_POLICY_VERSION),
        _publication(SUBJECT_IDS[1], SEMANTIC_ANNOTATION_POLICY_VERSION),
    )
    from tests.test_publication_document_v6 import _approved_opinion

    opinion, citation_context = _approved_opinion(base_documents[0])
    opinion = replace(
        opinion,
        base_publication_artifact_id=ARTIFACT_IDS[0],
        base_publication_sha256=publication_document_v5_sha256(base_documents[0]),
        content_sha256="",
    )
    citation_context = replace(
        citation_context,
        base_publication_artifact_id=ARTIFACT_IDS[0],
        base_publication_sha256=publication_document_v5_sha256(base_documents[0]),
    )
    v6_document = augment_publication_with_opinion(base_documents[0], opinion, citation_context)
    documents = (v6_document, base_documents[1])
    artifacts = {
        ARTIFACT_IDS[index]: SimpleNamespace(
            id=ARTIFACT_IDS[index],
            production_run_id=RUN_IDS[index],
            subject_id=SUBJECT_IDS[index],
            stage=ProductionArtifactStage.PUBLICATION,
            version=1,
            input_hash=f"{index + 1:064x}",
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=BLOB_IDS[index],
        )
        for index in range(2)
    }
    runs = {
        RUN_IDS[index]: SimpleNamespace(
            id=RUN_IDS[index],
            edition_id=EDITION_ID,
            subject_id=SUBJECT_IDS[index],
            pipeline_generation=3,
        )
        for index in range(2)
    }
    refs = tuple(
        EditionDocumentArtifactRef(
            position=index + 1,
            subject_id=SUBJECT_IDS[index],
            production_run_id=RUN_IDS[index],
            pipeline_generation=3,
            artifact_id=ARTIFACT_IDS[index],
            artifact_version=1,
            input_hash=f"{index + 1:064x}",
        )
        for index in range(2)
    )
    uow = SimpleNamespace(
        production_runs=_Repository(runs),
        production_artifacts=_Repository(artifacts),
    )
    store = _ArtifactStore(
        {BLOB_IDS[index]: serialize_publication_document(documents[index]) for index in range(2)}
    )
    edition = Edition(
        id=EDITION_ID,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        state=EditionStatus.OPEN,
    )

    result = await build_edition_document(
        uow,
        store,
        edition,
        refs,
        require_current=False,
    )

    assert isinstance(result, EditionDocumentV2)
    assert isinstance(result.publications[0].document, PublicationDocumentV6)
    assert isinstance(result.publications[1].document, PublicationDocumentV5)
    assert [
        item.document.semantic_text.policy_version
        for item in result.publications
        if isinstance(item.document, PublicationDocumentV5)
    ] == [SEMANTIC_ANNOTATION_POLICY_VERSION]
    assert result.publications[0].document == v6_document
    assert result.publications[1].document == base_documents[1]

    preserved = result.publications[0].document
    assert isinstance(preserved, PublicationDocumentV6)
    assert preserved.base_publication == base_documents[0]
    assert preserved.provenance.base_publication_artifact_id == ARTIFACT_IDS[0]
    assert preserved.provenance.base_publication_sha256 == opinion.base_publication_sha256
    assert preserved.provenance.opinion_id == opinion.id
    assert preserved.provenance.opinion_version == opinion.version
    assert preserved.provenance.opinion_content_sha256 == opinion.content_sha256
    assert preserved.rendered_opinion is not None

    round_tripped = EditionDocumentV2.from_json(result.to_json())
    assert round_tripped == result
