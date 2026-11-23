"""F4-D B4 oracles for the canonical article-anchor read.

The route is exercised through the real FastAPI application and the real
:class:`InvestigationArticleAnchorsService`; only the identity provider is a
local test identity.  No provider, Bridge or CTI system is contacted.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.investigation_article import router as article_anchors_router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.opinion_note_proposals import (
    NoteArticleElementFactV1,
    NoteProposalErrorCode,
    note_article_element_facts,
    note_article_element_refusal,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.publication_builder import (
    build_article_format_v1,
    build_publication_document_v5,
)
from cti_app.config import Settings
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
from cti_app.domain.publication_document import (
    PublicationDocumentV7,
    PublicationDocumentV9,
    ResolvedPublicationPlacementV1,
    publication_element_sha256,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_analyst_opinion_service import OpinionHarness, make_opinion_harness
from tests.test_opinion_note_proposals import base_v5_with_sections
from tests.test_publication_builder_v4 import _canonical_inputs
from tests.test_publication_builder_v4 import _table as _enrichment_table
from tests.test_publication_document_v6 import _base_v5
from tests.test_publication_document_v7 import _v6


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class _SyncClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def get(self, path: str) -> httpx.Response:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.get(path)

        return asyncio.run(send())


def _client(harness: OpinionHarness) -> _SyncClient:
    app = FastAPI()
    app.include_router(article_anchors_router)
    app.state.uow_factory = harness.uow_factory
    app.state.production_artifact_store = harness.store
    app.state.identity_provider = LocalIdentityProvider("analyst:test")
    app.state.settings = Settings(_env_file=None)
    return _SyncClient(app)


def _path(harness: OpinionHarness) -> str:
    return f"/api/investigations/{harness.investigation.id}/article-anchors"


def _install_base(harness: OpinionHarness, document: Any) -> None:
    base_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(document)
    )
    harness.store.blobs[harness.base_artifact.canonical_blob_id] = base_bytes
    base_sha256 = _sha256(base_bytes)
    harness.artifacts.items = [
        replace(item, input_hash=base_sha256) if item.id == harness.base_artifact.id else item
        for item in harness.artifacts.items
    ]
    harness.base_artifact = replace(harness.base_artifact, input_hash=base_sha256)
    harness.base_bytes = base_bytes


def _newer_publication(harness: OpinionHarness) -> None:
    newer_document = base_v5_with_sections(
        ("Campagne initiale.", "Paragraphe publié après la note.")
    )
    newer_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(newer_document)
    )
    blob_id = uuid4()
    harness.store.blobs[blob_id] = newer_bytes
    newer = ProductionArtifact(
        id=uuid4(),
        production_run_id=harness.base_artifact.production_run_id,
        subject_id=harness.base_artifact.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=2,
        input_hash=_sha256(newer_bytes),
        canonical_blob_id=blob_id,
        status=ProductionArtifactStatus.VERIFIED,
    )

    async def append() -> None:
        async with harness.uow_factory() as uow:
            await uow.production_artifacts.append(newer)

    asyncio.run(append())


def test_anchor_route_returns_exact_base_identity() -> None:
    harness = make_opinion_harness()
    client = _client(harness)

    response = client.get(_path(harness))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["base_publication_artifact_id"] == str(harness.base_artifact.id)
    assert payload["base_publication_version"] == harness.base_artifact.version
    assert payload["base_publication_sha256"] == _sha256(harness.base_bytes)
    assert payload["source"] == "current_base"
    assert payload["truncated"] is False
    anchors = {item["anchor"]: item for item in payload["anchors"]}
    lead = anchors["lead:0001"]
    assert lead["text"] == _base_v5().document.lead[0].text
    assert lead["text_sha256"] == _sha256(lead["text"].encode("utf-8"))
    assert lead["section_index"] is None
    assert lead["paragraph_index"] == 0


def test_anchor_route_prefers_the_pinned_opinion_base() -> None:
    harness = make_opinion_harness()
    client = _client(harness)

    async def compose() -> None:
        await harness.service.compose_from_findings(harness.investigation.id)

    asyncio.run(compose())
    _newer_publication(harness)
    response = client.get(_path(harness))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["source"] == "pinned_opinion_base"
    assert payload["base_publication_artifact_id"] == str(harness.base_artifact.id)
    assert payload["base_publication_version"] == harness.base_artifact.version
    assert payload["base_publication_sha256"] == _sha256(harness.base_bytes)


def test_anchor_route_lists_after_paragraph_elements_only_when_resolved() -> None:
    harness = make_opinion_harness()
    client = _client(harness)
    snapshot, references, extraction, synthesis = _canonical_inputs()
    anchor_block_id = UUID(int=777)
    table = replace(
        _enrichment_table(
            extraction_evidence_refs_v1(extraction)[0],
            key="after-registered-paragraph",
        ),
        placement=EnrichmentPlacementV1(
            kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor_block_id,
        ),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(table,),
    )
    base = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    # A legacy V5 cannot resolve an explicit paragraph anchor: the element is
    # not rendered, so the catalog reports it as non-proposable with the code
    # the block validator would raise.
    _install_base(harness, base)
    published = base.document.tables[0]
    legacy = client.get(_path(harness)).json()
    assert legacy["elements"] == [
        {
            "kind": "table",
            "key": published.key,
            "title": published.title,
            "caption": published.caption,
            "element_sha256": publication_element_sha256(
                base.document, kind="table", key=published.key
            ),
            "anchor": None,
            "proposable": False,
            "refusal_reason": "note_block_ref_stale",
        }
    ]

    v9 = PublicationDocumentV9(
        base_publication=PublicationDocumentV7(
            base_publication=_v6(base),
            publication_kind=PublicationKind.ARTICLE,
            routing_decision_hash="a" * 64,
            routing_policy_version="routing-policy-v1",
        ),
        article_format=build_article_format_v1(base, extraction),
        registry_hash="b" * 64,
        synthesis_canonical_sha256="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        resolved_placements=(
            ResolvedPublicationPlacementV1(
                element_kind="table",
                element_key=table.key,
                element_position=0,
                anchor_block_id=anchor_block_id,
                anchor_pointer="/lead/0",
                anchor_canonical_position=0,
                paragraph_kind="lead",
                section_index=None,
                paragraph_index=0,
            ),
        ),
    )
    _install_base(harness, v9)
    resolved = client.get(_path(harness)).json()

    assert resolved["elements"] == [
        {
            "kind": "table",
            "key": published.key,
            "title": published.title,
            "caption": published.caption,
            "element_sha256": publication_element_sha256(
                base.document, kind="table", key=published.key
            ),
            "anchor": "lead:0001",
            "proposable": True,
            "refusal_reason": None,
        }
    ]
    # The route offers exactly the facts the block validator resolves against.
    assert [
        (
            item["kind"],
            item["key"],
            item["element_sha256"],
            item["anchor"],
        )
        for item in resolved["elements"]
    ] == [
        (fact.kind, fact.key, fact.element_sha256, fact.anchor)
        for fact in note_article_element_facts(v9)
    ]


def _element_fact(**overrides: Any) -> NoteArticleElementFactV1:
    fact = NoteArticleElementFactV1(
        kind="table",
        key="observed-commands",
        element_sha256="a" * 64,
        anchor="lead:0001",
        evidence_ref_count=1,
        source_document_known=True,
        title="Observed commands",
        caption=None,
    )
    return replace(fact, **overrides)


def test_element_proposability_matches_the_block_validator_rules() -> None:
    assert note_article_element_refusal(_element_fact()) is None
    assert (
        note_article_element_refusal(_element_fact(anchor=None))
        is NoteProposalErrorCode.NOTE_BLOCK_REF_STALE
    )
    assert (
        note_article_element_refusal(_element_fact(kind="chart", evidence_ref_count=0))
        is NoteProposalErrorCode.NOTE_CHART_WITHOUT_EVIDENCE
    )
    assert (
        note_article_element_refusal(_element_fact(kind="figure", source_document_known=False))
        is NoteProposalErrorCode.NOTE_FIGURE_SOURCE_MISSING
    )
    assert note_article_element_refusal(_element_fact(kind="chart")) is None
    assert note_article_element_refusal(_element_fact(kind="figure")) is None


__all__ = [
    "test_anchor_route_lists_after_paragraph_elements_only_when_resolved",
    "test_anchor_route_prefers_the_pinned_opinion_base",
    "test_anchor_route_returns_exact_base_identity",
    "test_element_proposability_matches_the_block_validator_rules",
]
