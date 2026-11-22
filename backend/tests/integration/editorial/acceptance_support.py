"""Fixture wiring for the M23 F5 chained acceptance oracle.

The acceptance chain needs a *complete* production run: a six-paragraph
synthesis, a confirmed subject IOC when the routed publication is an ARTICLE,
four distinct archived figures, the references/relevance artifacts, an adopted
routing decision and a READY run so the real publication assembly and the real
Typst renderer can run through the HTTP routes.

The shared editorial integration seeds stop short of that (two paragraphs, at
most three archived figures, a BRIEF-only routing assertion), so this module
builds the same stages from the same helpers with explicit controls. It does not
replace any existing seed: the ordinary PG oracles keep using theirs.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api.main import create_app
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.editorial_pdf_preview import PublicationPreviewRenderer
from cti_app.application.editorial_workspace.registry import registry_hash
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.media_assets import MediaAssetStore, SourceFigureIngestor
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_routing import decide_and_adopt_production_routing
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
)
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.publication_builder import build_article_format_v1
from cti_app.application.publication_rendering import (
    PublicationRenderService,
)
from cti_app.application.source_figure_inventory import load_archived_source_figure_inventory
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editorial_workspace import (
    EditorialSourceStage,
    bootstrap_block_registry,
)
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ProductionEvidenceBasis,
    production_extraction_to_json,
)
from cti_app.domain.production_relevance import relevance_projection_to_json
from cti_app.domain.production_routing import ROUTING_POLICY_VERSION
from cti_app.domain.production_synthesis import (
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    PublicationDocumentV8,
    parse_publication_document,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from tests.integration.editorial.test_enrichment_visual_revision_postgres import (
    _configure_app,
    _seed_ready_workspace_snapshot,
)
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _enrichment_seed_content,
    _EnrichmentSeedContent,
    _seed,
    _Seeded,
)
from tests.integration.editorial.test_table_insertion_postgres import (
    _CANONICAL_BUCKET,
    _brief_v8_payload_for_subject,
    _NoModelCalls,
)

# Four distinct archived images. A BRIEF integrates at most three figures, and
# replacement requires one archive entry that no other element uses yet.
ARCHIVED_FIGURE_PATHS: tuple[str, ...] = (
    "chpTypst/UTILS/chap.png",
    "docs/mission01/typst-captures/current/page-1.png",
    "docs/mission01/typst-captures/current/page-2.png",
    "docs/mission01/typst-captures/historical/page-3.png",
)

SUBJECT_IOC_VALUE = "malicious.acceptance.test"


@dataclass(frozen=True)
class AcceptanceSeed:
    seeded: _Seeded
    app: FastAPI
    model_gateway: _NoModelCalls
    enrichment_content: _EnrichmentSeedContent
    workspace_service: EditorialWorkspaceService
    synthesis: Any
    extraction: Any
    archived_blob_shas: tuple[tuple[UUID, str], ...]
    figure_source_ids: tuple[UUID, ...]


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _local_archived_figure_assets(count: int) -> tuple[tuple[str, bytes], ...]:
    if not 1 <= count <= len(ARCHIVED_FIGURE_PATHS):
        raise ValueError("archived_figure_count_out_of_bounds")
    root = _repository_root()
    return tuple(
        (
            f"https://media.example.test/acceptance-figure-{index + 1}.png",
            (root / path).read_bytes(),
        )
        for index, path in enumerate(ARCHIVED_FIGURE_PATHS[:count])
    )


def _rich_synthesis(synthesis: Any, evidence_ref: Any) -> Any:
    """Six body paragraphs: two lead paragraphs then one four-paragraph section.

    The title must change too: the workspace registry preserves the block ID of
    an unchanged title across a synthesis revision, while the acceptance seed
    later bootstraps the registry from the new version. A title identical to the
    base version would make those two registries disagree on the title
    occurrence ID, and the enrichment evidence-pack hash includes it.
    """
    lead = (
        SynthesisParagraphV1("Acceptance lead paragraph one.", (evidence_ref,)),
        SynthesisParagraphV1("Acceptance lead paragraph two.", (evidence_ref,)),
    )
    paragraphs = tuple(
        SynthesisParagraphV1(f"Acceptance section paragraph {number}.", (evidence_ref,))
        for number in ("three", "four", "five", "six")
    )
    sections = (
        SynthesisSectionV1(
            SynthesisSectionKind.TECHNICAL,
            "Acceptance observations",
            paragraphs,
        ),
    )
    return replace(
        synthesis,
        title="[Synthetic actor] Acceptance report",
        lead=lead,
        sections=sections,
    )


def _acceptance_extraction(extraction: Any, *, article: bool) -> Any:
    """Optionally add one confirmed subject IOC so routing decides ARTICLE."""
    if not article:
        return extraction
    source = extraction.sources[0]
    indicator = ExtractionIndicatorV1(
        value=SUBJECT_IOC_VALUE,
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="Synthetic subject infrastructure.",
        evidence_quote=(
            "The synthetic report names malicious.acceptance.test as subject infrastructure."
        ),
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source.source_document_id,),
    )
    return replace(extraction, sources=(replace(source, indicators=(indicator,)),))


def _publication_fixture(subject_id: UUID, *, kind: PublicationKind, extraction: Any) -> bytes:
    publication_content = _brief_v8_payload_for_subject(subject_id)
    if kind is PublicationKind.ARTICLE:
        brief_document = parse_publication_document(publication_content)
        if not isinstance(brief_document, PublicationDocumentV8):
            raise TypeError("synthetic_publication_fixture_invalid")
        article_v7 = replace(
            brief_document.base_publication, publication_kind=PublicationKind.ARTICLE
        )
        article_document = PublicationDocumentV8(
            base_publication=article_v7,
            article_format=build_article_format_v1(
                article_v7.base_publication.base_publication,
                extraction,
            ),
        )
        publication_content = serialize_publication_document(article_document)
    return ProductionArtifactStore.canonical_json_bytes(publication_content)


async def _seed_acceptance_artifacts(
    uow_factory: Any,
    *,
    publication_kind: PublicationKind,
) -> tuple[_Seeded, FastAPI, _NoModelCalls, _EnrichmentSeedContent]:
    """Mirror the shared manual-route seed, with the acceptance inputs.

    This is intentionally a local copy: the shared helper caps archived figures
    at three and fixes the two-paragraph synthesis, while the acceptance chain
    needs four archived figures and six paragraphs.
    """
    seeded = await _seed(
        uow_factory,
        proposal_count=0,
        seed_discovery_lineage=True,
        bar_chart_facts=True,
    )
    evidence_ref = extraction_evidence_refs_v1(seeded.extraction)[0]
    extraction = _acceptance_extraction(
        seeded.extraction, article=publication_kind is PublicationKind.ARTICLE
    )
    synthesis = replace(
        _rich_synthesis(seeded.synthesis, evidence_ref),
        extraction_hash=canonical_extraction_hash(extraction),
    )
    seeded = replace(seeded, synthesis=synthesis, extraction=extraction)
    enrichment_content = _enrichment_seed_content(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
    )
    enrichment_bytes = ProductionArtifactStore.canonical_json_bytes(enrichment_content.base_payload)
    extraction_bytes = ProductionArtifactStore.canonical_json_bytes(
        production_extraction_to_json(seeded.extraction)
    )
    source_figure_assets = _local_archived_figure_assets(len(ARCHIVED_FIGURE_PATHS))
    source_bytes = (
        "<article><p>Synthetic report body.</p>"
        + "".join(
            f'<figure><img src="{url}" alt="Synthetic archived figure {index + 1}"></figure>'
            for index, (url, _content) in enumerate(source_figure_assets)
        )
        + "</article>"
    ).encode()
    publication_bytes = _publication_fixture(
        seeded.subject.id, kind=publication_kind, extraction=seeded.extraction
    )

    def blob_record(content: bytes, mime_type: str, bucket: str) -> BlobRecord:
        return BlobRecord(
            descriptor=BlobDescriptor(
                hashlib.sha256(content).hexdigest(), len(content), mime_type, bucket
            )
        )

    enrichment_blob = blob_record(enrichment_bytes, "application/json", _CANONICAL_BUCKET)
    extraction_blob = blob_record(extraction_bytes, "application/json", _CANONICAL_BUCKET)
    source_blob = blob_record(source_bytes, "text/plain", "source-documents")
    publication_blob = blob_record(publication_bytes, "application/json", _CANONICAL_BUCKET)
    image_blobs = tuple(
        blob_record(content, "image/png", "source-documents")
        for _url, content in source_figure_assets
    )
    for blob, content in (
        (enrichment_blob, enrichment_bytes),
        (extraction_blob, extraction_bytes),
        (source_blob, source_bytes),
        (publication_blob, publication_bytes),
    ):
        seeded.blob_store.seed(blob.descriptor, content)
    for blob, (_url, content) in zip(image_blobs, source_figure_assets, strict=True):
        seeded.blob_store.seed(blob.descriptor, content)

    figure_source_ids: list[UUID] = []
    async with uow_factory() as uow:
        stored_enrichment_blob = await uow.blobs.add_if_absent(enrichment_blob)
        stored_extraction_blob = await uow.blobs.add_if_absent(extraction_blob)
        stored_source_blob = await uow.blobs.add_if_absent(source_blob)
        stored_publication_blob = await uow.blobs.add_if_absent(publication_blob)
        await uow.production_input_snapshots.add(seeded.production_input)
        await uow.source_documents.add(
            SourceDocument(
                id=seeded.extraction.sources[0].source_document_id,
                subject_id=seeded.subject.id,
                blob_id=stored_source_blob.id,
                original_name="synthetic-report.html",
                origin="synthetic acceptance fixture",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.AMBER,
                do_not_submit=False,
                external_llm_allowed=True,
                final_url=seeded.extraction.sources[0].canonical_url,
                detected_mime_type="text/html",
            )
        )
        for (url, _content), image_blob in zip(source_figure_assets, image_blobs, strict=True):
            stored_image_blob = await uow.blobs.add_if_absent(image_blob)
            document = SourceDocument(
                subject_id=seeded.subject.id,
                blob_id=stored_image_blob.id,
                original_name=url.rsplit("/", 1)[-1],
                origin=url,
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.AMBER,
                do_not_submit=False,
                external_llm_allowed=True,
                final_url=url,
                detected_mime_type="image/png",
            )
            figure_source_ids.append(document.id)
            await uow.source_documents.add(document)
        await uow.production_artifacts.append(
            ProductionArtifact(
                production_run_id=seeded.run.id,
                subject_id=seeded.subject.id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=1,
                input_hash=seeded.production_input.input_hash,
                canonical_blob_id=stored_extraction_blob.id,
            )
        )
        await uow.commit()

    blob_store = cast(BlobStore, seeded.blob_store)
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    app = create_app()
    model_gateway = _NoModelCalls()
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    app.state.model_gateway = model_gateway
    app.state.publication_render_service = object()
    collection_service = editorial_workspace_api._collection_service_for_request(
        SimpleNamespace(app=app)
    )
    media_archiver = getattr(collection_service, "source_media_archiver", None)
    media_policy_sha256 = getattr(media_archiver, "policy_sha256", None)
    if not isinstance(media_policy_sha256, str):
        media_policy_sha256 = None
    synthesis_payload = production_synthesis_to_json(seeded.synthesis)
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    enrichment_workspace_view = SimpleNamespace(
        subject_id=seeded.subject.id,
        production_run_id=seeded.run.id,
        synthesis_artifact_id=seeded.base_artifact.id,
        synthesis_version=seeded.base_artifact.version,
        synthesis_canonical_sha256=synthesis_sha256,
        block_registry=(),
    )
    async with uow_factory() as uow:
        access_policy = await build_synthesis_access_policy(
            seeded.production_input,
            seeded.extraction,
            uow.source_documents,
            uow.source_collections,
        )
        inventory = await load_archived_source_figure_inventory(
            subject_id=seeded.subject.id,
            extraction_sources=seeded.extraction.sources,
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=artifact_store,
            source_media_repository=uow.source_media_candidates,
            media_policy_sha256=media_policy_sha256,
        )
    access_policy_hash = synthesis_access_policy_hash(access_policy)
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        seeded.extraction,
        seeded.synthesis,
        workspace_record=enrichment_workspace_view,
    )
    evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    inventory_hash = inventory.functional_hash()
    enrichment_input_hash = compute_editorial_enrichment_input_hash(
        extraction=seeded.extraction,
        synthesis=seeded.synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        source_figure_inventory_hash=inventory_hash,
        resource_search_enabled=False,
    )
    enrichment_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=1,
        input_hash=enrichment_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "access_policy_hash": access_policy_hash,
            "synthesis_access_policy_hash": access_policy_hash,
            "evidence_pack_hash": evidence_pack_hash,
            "source_figure_inventory_hash": inventory_hash,
            "routing_policy_version": "synthetic-routing-policy",
        },
        canonical_blob_id=stored_enrichment_blob.id,
    )
    publication_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        input_hash=hashlib.sha256(
            f"{seeded.base_artifact.id}:{enrichment_artifact.id}".encode()
        ).hexdigest(),
        metadata={
            "input_artifacts": {
                "synthesis_artifact_id": str(seeded.base_artifact.id),
                "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
            }
        },
        canonical_blob_id=stored_publication_blob.id,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(enrichment_artifact)
        await uow.production_artifacts.append(publication_artifact)
        await uow.commit()

    app.state.identity_provider = LocalIdentityProvider("dev-analyst")
    return seeded, app, model_gateway, enrichment_content


async def _seed_complete_production(
    seeded: _Seeded,
    app: FastAPI,
    uow_factory: Any,
    *,
    expected_kind: PublicationKind,
) -> tuple[_Seeded, Any, Any]:
    """Persist the references/relevance artifacts, a current enrichment and the
    adopted routing decision, then finish the run READY and assemble one real
    publication. Adapted from ``test_editorial_pdf_postgres._seed_current_inputs``
    with a routed-kind parameter instead of a hard-coded BRIEF assertion.
    """
    artifact_store = cast(ProductionArtifactStore, app.state.production_artifact_store)
    source = seeded.extraction.sources[0]
    references = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=seeded.subject.id,
        research_date=seeded.production_input.research_date,
        production_input_hash=seeded.production_input.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url=source.canonical_url,
                tier=source.tier,
                kind=source.kind,
                role=source.role,
                title="Synthetic acceptance source",
                publisher="Synthetic publisher",
                published_at=None,
                source_collection_id=None,
                source_document_id=source.source_document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.EXTRACTED,
                content_sha256=source.content_sha256,
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
                editorial_role=source.editorial_role,
            ),
        ),
        warnings=(),
    )
    references = production_reference_corpus_from_json(
        production_reference_corpus_to_json(references)
    )
    extraction = replace(
        seeded.extraction,
        references_corpus_hash=references_corpus_hash(references),
    )
    projection = build_relevance_projection(seeded.production_input, extraction)
    synthesis = replace(
        seeded.synthesis,
        extraction_hash=canonical_extraction_hash(extraction),
    )
    enrichment = _enrichment_seed_content(seeded.production_input, extraction, synthesis)
    synthesis_payload = production_synthesis_to_json(synthesis)
    synthesis_artifact_id = uuid4()

    references_blob_id = await artifact_store.put_json(
        production_reference_corpus_to_json(references), bucket=_CANONICAL_BUCKET
    )
    extraction_blob_id = await artifact_store.put_json(
        production_extraction_to_json(extraction), bucket=_CANONICAL_BUCKET
    )
    projection_blob_id = await artifact_store.put_json(
        relevance_projection_to_json(projection), bucket=_CANONICAL_BUCKET
    )
    synthesis_blob_id = await artifact_store.put_json(synthesis_payload, bucket=_CANONICAL_BUCKET)

    references_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=references_blob_id,
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=2,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob_id,
    )
    projection_artifact = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash=projection.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=projection_blob_id,
    )
    synthesis_artifact = ProductionArtifact(
        id=synthesis_artifact_id,
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=2,
        input_hash=seeded.production_input.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(references_artifact)
        await uow.production_artifacts.append(extraction_artifact)
        await uow.production_artifacts.append(projection_artifact)
        await uow.production_artifacts.append(synthesis_artifact)
        await uow.commit()

    workspace_service = EditorialWorkspaceService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
    )
    async with uow_factory() as uow:
        workspace_record = await workspace_service.snapshot_record_in_uow(uow, seeded.subject.id)
        await uow.commit()
    assert workspace_record.synthesis_artifact_id == synthesis_artifact.id
    assert workspace_record.synthesis_version == synthesis_artifact.version
    assert workspace_record.enrichment_artifact_id is not None

    # The evidence-pack hash includes the synthesis paragraph occurrence IDs.
    # ``_seed_ready_workspace_snapshot`` later persists a *fresh* bootstrap
    # registry (all occurrences READY) for the same pins, while the record just
    # computed above reconciles against the previous synthesis version and can
    # mark changed paragraphs NEEDS_REVIEW (no anchor IDs). The fence at
    # proposal time reads the persisted fresh registry, so the artifact
    # metadata must be hashed against that same registry projection.
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_payload)
    ).hexdigest()
    evidence_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact.id,
        canonical_sha256=synthesis_sha256,
        source_stage=EditorialSourceStage.SYNTHESIS,
        canonical_content=synthesis_payload,
    )
    evidence_workspace_record = replace(
        workspace_record,
        synthesis_canonical_sha256=synthesis_sha256,
        block_registry=evidence_entries,
        registry_hash=registry_hash(evidence_entries),
    )

    collection_service = editorial_workspace_api._collection_service_for_request(
        SimpleNamespace(app=app)
    )
    media_archiver = getattr(collection_service, "source_media_archiver", None)
    media_policy_sha256 = getattr(media_archiver, "policy_sha256", None)
    if not isinstance(media_policy_sha256, str):
        media_policy_sha256 = None
    async with uow_factory() as uow:
        access_policy = await build_synthesis_access_policy(
            seeded.production_input,
            extraction,
            uow.source_documents,
            uow.source_collections,
        )
        inventory = await load_archived_source_figure_inventory(
            subject_id=seeded.subject.id,
            extraction_sources=extraction.sources,
            source_document_repository=uow.source_documents,
            blob_repository=uow.blobs,
            artifact_store=artifact_store,
            source_media_repository=uow.source_media_candidates,
            media_policy_sha256=media_policy_sha256,
        )
    # The real enrichment generation ingests every accepted archived figure
    # into the media asset store; manual insertion later renders those bytes.
    # This synthetic seed bypasses that stage, so ingest them here exactly like
    # ``ProductionEditorialEnrichmentService._ingest_source_figures`` does.
    figure_ingestor = SourceFigureIngestor(MediaAssetStore(artifact_store, uow_factory))
    for accepted_figure in inventory.accepted:
        assert accepted_figure.blob_id is not None
        accepted_bytes = await artifact_store.read_bytes(accepted_figure.blob_id)
        await figure_ingestor.ingest(accepted_figure, accepted_bytes)
    access_policy_hash = synthesis_access_policy_hash(access_policy)
    evidence_pack = build_editorial_enrichment_evidence_pack(
        seeded.production_input,
        extraction,
        synthesis,
        projection,
        evidence_workspace_record,
    )
    evidence_pack_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
    inventory_hash = inventory.functional_hash()
    enrichment_input_hash = compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        projection_hash=projection.projection_hash,
        source_figure_inventory_hash=inventory_hash,
        resource_search_enabled=False,
    )

    enrichment_blob_id = await artifact_store.put_json(
        enrichment.base_payload, bucket=_CANONICAL_BUCKET
    )
    enrichment_artifact = ProductionArtifact(
        id=uuid4(),
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash=enrichment_input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "access_policy_hash": access_policy_hash,
            "synthesis_access_policy_hash": access_policy_hash,
            "evidence_pack_hash": evidence_pack_hash,
            "source_figure_inventory_hash": inventory_hash,
            "routing_policy_version": ROUTING_POLICY_VERSION,
        },
        canonical_blob_id=enrichment_blob_id,
    )

    async with uow_factory() as uow:
        await uow.production_artifacts.append(enrichment_artifact)
        run = await uow.production_runs.get(seeded.run.id)
        assert run is not None
        expected_version = run.version
        await uow.commit()

    routing_decision = await decide_and_adopt_production_routing(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        production_run_id=seeded.run.id,
        expected_version=expected_version,
    )
    assert routing_decision.publication_kind is expected_kind

    async with uow_factory() as uow:
        run = await uow.production_runs.get(seeded.run.id)
        current_enrichment = await uow.production_artifacts.get_current(
            seeded.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        assert run is not None and current_enrichment is not None
        assembly = PublicationAssemblyService(artifact_store, uow.production_artifacts)
        await assembly.assemble_publication(
            run=run,
            snapshot=seeded.production_input,
            references=references,
            extraction=extraction,
            relevance_projection=projection,
            synthesis=synthesis,
            editorial_enrichment=enrichment.base_enrichment,
            publication_kind=routing_decision.publication_kind,
            editorial_enrichment_artifact_id=current_enrichment.id,
            editorial_enrichment_artifact_input_hash=current_enrichment.input_hash,
            routing_decision_hash=routing_decision.decision_hash,
            routing_policy_version=routing_decision.policy_version,
            metadata_extra={
                "input_artifacts": {
                    "references_artifact_id": str(references_artifact.id),
                    "extraction_artifact_id": str(extraction_artifact.id),
                    "relevance_projection_artifact_id": str(projection_artifact.id),
                    "synthesis_artifact_id": str(synthesis_artifact.id),
                    "editorial_enrichment_artifact_id": str(current_enrichment.id),
                }
            },
        )
        await uow.commit()
    return seeded, extraction, synthesis


async def _finish_run_ready(
    seeded: _Seeded, uow_factory: Any, *, publication_kind: PublicationKind
) -> _Seeded:
    from cti_app.domain.production import ProductionRunStatus, ProductionStage

    async with uow_factory() as uow:
        run = await uow.production_runs.get(seeded.run.id)
        assert run is not None
        assert run.status is ProductionRunStatus.QUEUED
        now = datetime.now(UTC)
        run.start_running(now=now)
        while run.current_stage is not ProductionStage.ASSEMBLY:
            run.advance_stage(publication_kind=publication_kind, now=now)
        run.mark_ready(now=now)
        await uow.production_runs.save(run)
        await uow.commit()
        persisted = await uow.production_runs.get(seeded.run.id)
    assert persisted is not None
    assert persisted.status is ProductionRunStatus.READY
    return replace(seeded, run=persisted)


def _require_typst_binary() -> str:
    binary = os.environ.get("TYPST_BINARY")
    if not binary:
        raise RuntimeError(
            "TYPST_BINARY is required: the F5 acceptance chain compiles the assembled "
            "publication with real Typst"
        )
    return binary


def _publication_renderer(
    app: FastAPI, uow_factory: Any, artifact_store: ProductionArtifactStore
) -> PublicationPreviewRenderer:
    binary = _require_typst_binary()
    media_assets = MediaAssetStore(artifact_store, uow_factory)
    app.state.media_asset_store = media_assets
    chp_root, font_root, font_lock = typst_bundle_paths()
    return PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        media_asset_store=media_assets,
        renderer=TypstRenderer(),
        compiler=TypstSubprocessCompiler(binary=binary),
        chp_typst_root=chp_root,
        font_bundle_root=font_root,
        typst_fonts_lock_path=font_lock,
    )


async def _archived_blob_shas(
    uow_factory: Any, figure_source_ids: tuple[UUID, ...]
) -> tuple[tuple[UUID, str], ...]:
    shas: list[tuple[UUID, str]] = []
    async with uow_factory() as uow:
        for source_id in figure_source_ids:
            document = await uow.source_documents.get(source_id)
            assert document is not None
            blob = await uow.blobs.get(document.blob_id)
            assert blob is not None
            shas.append((document.blob_id, blob.descriptor.sha256))
    return tuple(sorted(shas))


async def seed_acceptance_workspace(
    uow_factory: Any,
    monkeypatch: Any,
    *,
    publication_kind: PublicationKind,
) -> AcceptanceSeed:
    """Build a READY run whose current snapshot routes to ``publication_kind``."""
    _configure_app(monkeypatch)
    seeded, app, model_gateway, enrichment_content = await _seed_acceptance_artifacts(
        uow_factory, publication_kind=publication_kind
    )
    seeded, extraction, synthesis = await _seed_complete_production(
        seeded, app, uow_factory, expected_kind=publication_kind
    )
    seeded = await _finish_run_ready(seeded, uow_factory, publication_kind=publication_kind)
    artifact_store = cast(ProductionArtifactStore, app.state.production_artifact_store)
    app.state.publication_render_service = _publication_renderer(app, uow_factory, artifact_store)
    workspace_service = editorial_workspace_api._service(type("_Request", (), {"app": app})())
    await _seed_ready_workspace_snapshot(
        workspace_service=workspace_service,
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        subject_id=seeded.subject.id,
    )
    snapshot = await workspace_service.snapshot(seeded.subject.id)
    assert snapshot["publication_kind"] == publication_kind.value
    assert snapshot["capabilities"]["read_only_reason"] is None
    figure_source_ids = await _figure_source_ids(uow_factory, seeded.subject.id)
    archived_blob_shas = await _archived_blob_shas(uow_factory, figure_source_ids)
    return AcceptanceSeed(
        seeded=seeded,
        app=app,
        model_gateway=model_gateway,
        enrichment_content=enrichment_content,
        workspace_service=workspace_service,
        synthesis=synthesis,
        extraction=extraction,
        archived_blob_shas=archived_blob_shas,
        figure_source_ids=figure_source_ids,
    )


async def _figure_source_ids(uow_factory: Any, subject_id: UUID) -> tuple[UUID, ...]:
    async with uow_factory() as uow:
        documents = await uow.source_documents.list_for_subject(subject_id)
    return tuple(
        sorted(
            (
                document.id
                for document in documents
                if (document.detected_mime_type or "").startswith("image/")
            ),
            key=lambda value: value.hex,
        )
    )


__all__ = [
    "ARCHIVED_FIGURE_PATHS",
    "SUBJECT_IOC_VALUE",
    "AcceptanceSeed",
    "seed_acceptance_workspace",
]
