"""PostgreSQL proof for canonical cross-run production artifact reuse."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
)
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationResult,
    ModelCapabilities,
    ModelGateway,
    ModelGatewayError,
    ModelRouter,
    SafeModelRequest,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_reuse import ProductionArtifactReuseService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
    build_editorial_enrichment_evidence_pack,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    validate_editorial_enrichment,
)
from cti_app.application.production_extraction import (
    extraction_input_hash,
    references_corpus_hash,
)
from cti_app.application.production_references import (
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_relevance import build_relevance_projection
from cti_app.application.production_relevance_model import ModelRelevanceClassifier
from cti_app.application.production_stages import EditorialEnrichmentService
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    build_synthesis_evidence_pack,
    canonical_extraction_hash,
    synthesis_access_policy_hash,
    synthesis_input_hash,
)
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _references_input_hash,
)
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.subject_production import (
    ProductionBatchService,
    SubjectProductionService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryMemberReference,
    DiscoveryMergeRun,
    DiscoveryPlannerKind,
    DiscoverySnapshot,
    DiscoverySubject,
    DiscoverySubjectIdentity,
    MergeValidationStatus,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.entities import SourceDocument, Subject
from cti_app.domain.model_runs import (
    ModelBackend,
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelTransport,
    ModelUsage,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchPhase,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionReuseInvalidation,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
    SourceExtraction,
    SourceExtractionStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    editorial_enrichment_from_json,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import (
    RelevanceProjectionV1,
    relevance_projection_to_json,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    SynthesisTimelineEntryV1,
    extraction_evidence_refs_v1,
    production_synthesis_from_json,
    production_synthesis_to_json,
)
from cti_app.domain.selection import (
    SelectionAction,
    SelectionDecision,
    SubjectDiscoveryOrigin,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore
from tests.discovery_support import make_discovery_run_for_edition
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.integration.production.support import (
    synthesis_prompt_records,
    synthesis_proposal_wire,
)

pytestmark = pytest.mark.integration


def _source_identity(extraction: SourceExtraction) -> dict[str, str]:
    return {
        "source_content_sha256": extraction.source_content_sha256,
        "profile": extraction.profile.value,
        "contract_version": extraction.contract_version,
        "prompt_version": extraction.prompt_version,
        "parser_version": extraction.parser_version,
        "verifier_version": extraction.verifier_version,
        "source_text_contract_version": extraction.source_text_contract_version,
        "model_policy_version": extraction.model_policy_version,
        "routing_policy_version": extraction.routing_policy_version,
        "profile_policy_version": extraction.profile_policy_version,
    }


@pytest.mark.asyncio
async def test_source_extraction_checkpoint_identity_is_durable(
    uow_factory: UnitOfWorkFactory,
) -> None:
    source = SourceExtraction(
        canonical_url="https://example.test/first",
        source_content_sha256="a" * 64,
        profile=ExtractionProfile.FULL,
        contract_version="contract-v1",
        prompt_version="prompt-v1",
        parser_version="parser-v1",
        verifier_version="verifier-v1",
        source_text_contract_version="text-v1",
        model_policy_version="model-v1",
        routing_policy_version="routing-v1",
        status=SourceExtractionStatus.VERIFIED,
    )
    async with uow_factory() as uow:
        assert await uow.source_extractions.claim(source)
        await uow.commit()

    async with uow_factory() as uow:
        found = await uow.source_extractions.get_by_identity(**_source_identity(source))
        assert found == source
        # A second URL with identical bytes and policy reuses this checkpoint.
        duplicate_url = replace(source, id=uuid4(), canonical_url="https://example.test/second")
        assert not await uow.source_extractions.claim(duplicate_url)
        assert (
            await uow.source_extractions.get_by_identity(**_source_identity(duplicate_url))
        ) == source

    changes: dict[str, str | ExtractionProfile] = {
        "source_content_sha256": "b" * 64,
        "profile": ExtractionProfile.IOC_RULES,
        "contract_version": "contract-v2",
        "prompt_version": "prompt-v2",
        "parser_version": "parser-v2",
        "verifier_version": "verifier-v2",
        "source_text_contract_version": "text-v2",
        "model_policy_version": "model-v2",
        "routing_policy_version": "routing-v2",
        "profile_policy_version": "profile-v2",
    }
    for field_name, value in changes.items():
        changed = replace(source, id=uuid4(), **{field_name: value})
        async with uow_factory() as uow:
            assert await uow.source_extractions.get_by_identity(**_source_identity(changed)) is None
            assert await uow.source_extractions.claim(changed)
            await uow.commit()
        async with uow_factory() as uow:
            found = await uow.source_extractions.get_by_identity(**_source_identity(changed))
            assert found == changed

    # The weaker IOC_RULES profile cannot satisfy a FULL lookup.
    ioc = replace(
        source,
        id=uuid4(),
        source_content_sha256="c" * 64,
        profile=ExtractionProfile.IOC_RULES,
    )
    async with uow_factory() as uow:
        assert await uow.source_extractions.claim(ioc)
        await uow.commit()
    async with uow_factory() as uow:
        found = await uow.source_extractions.get_by_identity(
            **{**_source_identity(ioc), "profile": ExtractionProfile.FULL.value}
        )
        assert found is None


def _synthesis_wire_from_canonical_prompt(prompt_text: str) -> str:
    """Answer the canonical drafting prompt with one grounded claim (text blocks)."""
    fact = next(
        record for record in synthesis_prompt_records(prompt_text) if record.get("kind") == "fact"
    )
    claim = (
        f"{fact['value']} is documented by the selected publication.",
        (fact["handle"],),
    )
    return synthesis_proposal_wire([claim], [("overview", [claim])])


class _CountingRetryModelAdapter:
    """A deterministic bridge-shaped adapter for the real retry workflow."""

    provider = ModelProvider.OPENAI
    backend = ModelBackend.CHATGPT_BRIDGE
    transport = ModelTransport.OPENAI_RESPONSES
    capabilities = ModelCapabilities(
        web_search=True, background=True, conversation=True, structured_output=True
    )
    requested_model = "fake-production-retry"
    is_external = False

    def __init__(self) -> None:
        self.calls: list[SafeModelRequest] = []
        self._extraction_text = "FACT actors\n- Example actor\n"

    async def invoke(
        self,
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: object = None,
    ) -> AdapterResult:
        del output_schema
        self.calls.append(request)
        if request.prompt_template_id == "production-synthesis":
            return AdapterResult(
                status=AdapterResultStatus.COMPLETED,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                response_id=f"retry-synthesis-{request.request_id}",
                output_text=_synthesis_wire_from_canonical_prompt(request.text),
            )
        if request.prompt_template_id in {
            "production-editorial-enrichment",
            "production-relevance-classifier",
        }:
            empty = (
                "NO USEFUL ENRICHMENT"
                if request.prompt_template_id == "production-editorial-enrichment"
                else "@@NONE@@"
            )
            return AdapterResult(
                status=AdapterResultStatus.COMPLETED,
                provider=self.provider,
                requested_model=self.requested_model,
                actual_model_version=self.requested_model,
                usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                response_id=f"retry-{request.prompt_template_id}-{request.request_id}",
                output_text=empty,
            )
        output_text = self._extraction_text
        conversation = request.conversation
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_text=output_text,
            conversation=(
                ConversationResult(
                    id=str(conversation.id),
                    mode=conversation.mode,
                    external_locator="https://chatgpt.com/fake-retry",
                    turn_id=f"retry-turn-{len(self.calls)}",
                    verified=True,
                )
                if conversation is not None
                else None
            ),
        )

    async def resume(
        self,
        response_id: str,
        *,
        role: ModelRole,
        output_schema: object = None,
    ) -> AdapterResult:
        del response_id, role, output_schema
        raise ModelGatewayError("retry test adapter does not support background responses")


def _edition(*, country: str = "France", country_code: str = "FR") -> Edition:
    return Edition(
        country=country,
        country_code=country_code,
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )


async def _persist_edition(uow_factory: UnitOfWorkFactory, edition: Edition) -> None:
    # add_if_absent may rebind `edition.id` to an existing logical edition, so
    # Subjects (whose edition_id is immutable) are only built afterwards.
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.commit()


def _production_context_entities(
    *,
    edition: Edition,
    subject: Subject,
    discovery_run_id: UUID,
    title: str = "Production subject",
) -> tuple[DiscoveryBatch, SourceCandidate]:
    source = SourceCandidate(
        url=f"https://example.test/{subject.slug}",
        title=f"{title} source",
        publisher="Example publisher",
        role=SourceRole.PRIMARY,
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
    )
    candidate = CandidateTopic(
        title=title,
        summary=f"A realistic selected subject for {title}.",
        novelty="new",
        technical_potential=3,
        uncertainties=(),
        relevance_reasons=("test",),
        actors=("Example actor",),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[source],
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
        actor_or_campaign="Example actor",
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        request_hash=(subject.id.hex * 2)[:64],
        complementary_axis="reuse integration",
        queries=(),
        citations=(),
        discovery_run_id=discovery_run_id,
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="public",
        external_llm_allowed=True,
        parser_version="test",
        candidates=[candidate],
        source_mode=DiscoverySourceMode.NATIVE_COMPLETE,
        source_coverage_complete=True,
        source_coverage_incomplete_reason=None,
    )
    return batch, source


async def _seed_subject_discovery_lineage(
    uow_factory: UnitOfWorkFactory,
    *,
    edition: Edition,
    subject_batches: tuple[tuple[Subject, DiscoveryBatch], ...],
) -> None:
    """Persist the lineage Production resolves to freeze a run's input.

    Production reads Subject → SubjectDiscoveryOrigin → canonical identity →
    active DiscoverySnapshot → DiscoveryCandidate, so every one of those rows
    must really exist here, with its foreign keys satisfied.

    The snapshot is intentionally shared by every subject passed in. This
    mirrors the Fusion projection for an edition and keeps all source runs
    anchored to the same active discovery identity.
    """
    assert subject_batches
    async with uow_factory() as uow:
        persisted_candidates: list[tuple[Subject, DiscoveryBatch, DiscoveryCandidate]] = []
        for subject, batch in subject_batches:
            candidates = await uow.discovery_candidates.list_for_batch(batch.id)
            assert len(candidates) == 1
            persisted_candidates.append((subject, batch, candidates[0]))

        merge_run = DiscoveryMergeRun(
            edition_id=edition.id,
            parent_snapshot_id=None,
            intake_id=None,
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            prompt_version="1",
            policy_version="1",
            blocking_version="1",
            merge_input_hash=hashlib.sha256(
                b"".join(
                    subject.id.bytes + batch.id.bytes for subject, batch, _ in persisted_candidates
                )
            ).hexdigest(),
            handle_map={},
            included_subject_ids=tuple(subject.id for subject, _, _ in persisted_candidates),
            excluded_subject_count=0,
            validation_status=MergeValidationStatus.VALID,
        )
        assert await uow.discovery_merge_runs.add_if_absent(merge_run)
        await uow.discovery_subject_identities.add_many_if_absent(
            [
                DiscoverySubjectIdentity(
                    edition_id=edition.id,
                    origin_key=f"subject:{subject.id}",
                    created_by_merge_run_id=merge_run.id,
                    id=subject.id,
                )
                for subject, _, _ in persisted_candidates
            ]
        )
        snapshot = DiscoverySnapshot(
            edition_id=edition.id,
            version=1,
            parent_snapshot_id=None,
            intake_id=None,
            merge_run_id=merge_run.id,
            planner_kind=DiscoveryPlannerKind.DETERMINISTIC_BOOTSTRAP,
            subjects=tuple(
                DiscoverySubject(
                    subject_id=subject.id,
                    candidate=discovery_candidate.to_candidate_topic(),
                    member_references=(DiscoveryMemberReference(discovery_candidate.id),),
                    created_at=discovery_candidate.created_at,
                )
                for subject, _, discovery_candidate in persisted_candidates
            ),
            snapshot_hash=hashlib.sha256(
                b"".join(batch.id.bytes for _, batch, _ in persisted_candidates)
            ).hexdigest(),
            is_active=True,
        )
        await uow.discovery_snapshots.append(snapshot)
        for subject, _, _ in persisted_candidates:
            decision = SelectionDecision(
                edition_id=edition.id,
                discovery_subject_id=subject.id,
                snapshot_id=snapshot.id,
                snapshot_version=snapshot.version,
                action=SelectionAction.SELECT,
                subject_id=subject.id,
                actor_id="reuse-integration",
                correlation_id="reuse-integration",
                idempotency_key=f"reuse-integration-{subject.id}",
            )
            await uow.selection_decisions.append(decision)
            await uow.subject_discovery_origins.add(
                SubjectDiscoveryOrigin(
                    subject_id=subject.id,
                    edition_id=edition.id,
                    discovery_subject_id=subject.id,
                    selection_decision_id=decision.id,
                    selected_snapshot_id=snapshot.id,
                    selected_snapshot_version=snapshot.version,
                )
            )
        await uow.commit()


async def _seed_computed_run(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    edition: Edition,
    subject: Subject,
    created_at: datetime,
) -> tuple[ProductionRun, dict[ProductionArtifactStage, ProductionArtifact]]:
    # The logical edition repository may replace a freshly generated ID with
    # an existing edition's ID, so persist these parents before constructing
    # the run that references the final edition ID.
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()

    run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=ProductionRunStatus.READY,
        current_stage=ProductionStage.ASSEMBLY,
        created_at=created_at,
        updated_at=created_at,
    )
    refs_blobs = await store.store_stage_payloads(
        raw="references raw",
        canonical={"stage": "references"},
    )
    extraction_blobs = await store.store_stage_payloads(
        raw="extraction raw",
        canonical={"stage": "extraction"},
    )
    synthesis_ref = ExtractionEvidenceRefV1(
        source_document_id=uuid4(),
        kind=EvidenceKind.FACT,
        evidence_key=hashlib.sha256(b"seeded-computed-run-fact").hexdigest(),
    )
    synthesis_paragraph = SynthesisParagraphV1(
        text="The canonical synthesis of this run is seeded for reuse.",
        evidence_refs=(synthesis_ref,),
    )
    synthesis_blobs = await store.store_stage_payloads(
        raw="synthesis raw",
        canonical=production_synthesis_to_json(
            ProductionSynthesisV1(
                schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
                subject_id=subject.id,
                production_input_hash="a" * 64,
                extraction_hash="b" * 64,
                publication_language="fr",
                synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
                title=subject.title,
                lead=(synthesis_paragraph,),
                sections=(),
                timeline=(),
                uncertainties=(),
                warnings=(),
            )
        ),
        rendered="synthesis rendered",
    )
    blobs = {
        ProductionArtifactStage.REFERENCES: refs_blobs,
        ProductionArtifactStage.EXTRACTION: extraction_blobs,
        ProductionArtifactStage.SYNTHESIS: synthesis_blobs,
    }
    hashes = {
        ProductionArtifactStage.REFERENCES: "a" * 64,
        ProductionArtifactStage.EXTRACTION: "b" * 64,
        ProductionArtifactStage.SYNTHESIS: "c" * 64,
    }
    artifacts = {
        stage: ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=stage,
            version=1,
            input_hash=hashes[stage],
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=payloads[0],
            canonical_blob_id=payloads[1],
            rendered_blob_id=payloads[2],
            created_at=created_at,
        )
        for stage, payloads in blobs.items()
    }
    async with uow_factory() as uow:
        await uow.production_runs.add(run)
        for artifact in artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()
    return run, artifacts


async def _prepare_reusable_article(
    uow_factory: UnitOfWorkFactory,
    *,
    edition: Edition,
    subject: Subject,
    title: str,
) -> tuple[DiscoveryBatch, SourceCandidate]:
    """Persist the discovery and archived-source context for one article."""
    discovery_run = await make_discovery_run_for_edition(
        uow_factory, edition, complementary_axis="reuse integration"
    )
    batch, source = _production_context_entities(
        edition=edition,
        subject=subject,
        discovery_run_id=discovery_run.id,
        title=title,
    )
    discovery_model_run = ModelRun(
        id=batch.discovery_model_run_id,
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="integration",
        prompt_template_version="1",
        authorized_input_hash=(subject.id.hex * 2)[:64],
        evidence_pack_hash=(batch.id.hex * 2)[:64],
        parameters={},
    )
    collection = SourceCollection(
        subject_id=subject.id,
        edition_id=edition.id,
        batch_id=batch.id,
        source_candidate_id=source.id,
        requested_url=source.url,
        canonical_url=source.canonical_url,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=True,
        proposed_role=source.role,
        origin_kind=SourceOriginKind.DISCOVERY,
        state=CollectionState.ARCHIVED,
    )
    async with uow_factory() as uow:
        await uow.model_runs.add(discovery_model_run)
        assert await uow.discovery_batches.add_if_absent(batch)
        assert await uow.source_collections.add_if_absent(collection)
        await uow.commit()
    return batch, source


def _canonical_synthesis(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    text: str,
) -> ProductionSynthesisV1:
    event_ref = next(
        ref for ref in extraction_evidence_refs_v1(extraction) if ref.kind is EvidenceKind.EVENT
    )
    paragraph = SynthesisParagraphV1(text=text, evidence_refs=(event_ref,))
    return ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=extraction.subject_id,
        production_input_hash=extraction.production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language=snapshot.publication_language,
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=snapshot.subject_title,
        lead=(paragraph,),
        sections=(
            SynthesisSectionV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="Campaign overview",
                paragraphs=(paragraph,),
            ),
        ),
        timeline=(
            SynthesisTimelineEntryV1(
                event_date=date(2026, 8, 5),
                date_text=None,
                text=text,
                evidence_refs=(event_ref,),
            ),
        ),
        uncertainties=(),
        warnings=(),
    )


async def _ensure_synthesis_source_document(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    subject: Subject,
    source: SourceCandidate,
    extraction: ProductionExtractionV1,
) -> None:
    """Persist the exact archived-source metadata the access policy folds."""
    document_id = extraction.sources[0].source_document_id
    async with uow_factory() as uow:
        if await uow.source_documents.get(document_id) is not None:
            return
        content = f"Canonical synthesis source {document_id}".encode()
        content_sha256 = hashlib.sha256(content).hexdigest()
        blob_id = await store.put_bytes(
            content, bucket="integration-synthesis-source", mime_type="text/plain"
        )
        document = SourceDocument(
            id=document_id,
            subject_id=subject.id,
            blob_id=blob_id,
            original_name=f"{subject.slug}.txt",
            origin=source.canonical_url,
            acquired_at=datetime(2026, 8, 5, tzinfo=UTC),
            license_restriction=None,
            tlp=source.tlp,
            do_not_submit=False,
            external_llm_allowed=True,
            decoded_blob_id=blob_id,
            title=source.title,
            publisher=source.publisher,
            published_at=source.published_at,
            final_url=source.canonical_url,
            detected_mime_type="text/plain",
            encoded_sha256=content_sha256,
            decoded_sha256=content_sha256,
            encoded_size=len(content),
            decoded_size=len(content),
        )
        await uow.source_documents.add(document)
        await uow.commit()


async def _store_canonical_first_pass(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    snapshot: ProductionInputSnapshot,
    subject: Subject,
    source: SourceCandidate,
    event_text: str,
    document_id: UUID | None = None,
    content_sha256: str = "d" * 64,
) -> SimpleNamespace:
    """Persist the REFERENCES and EXTRACTION payloads of one canonical first pass.

    REFERENCES is the AW-010 corpus plus its Q1 RAW; EXTRACTION is the AW-011
    ``ProductionExtractionV1``.  The returned hashes are the exact functional
    identities the orchestrator recomputes for a second run.
    """
    published_at = source.published_at or date(2026, 8, 5)
    raw = "\n".join(
        (
            "# REFERENCES",
            "",
            "## SOURCE S1",
            f"title: {source.title}",
            f"url: {source.canonical_url}",
            f"publisher: {source.publisher}",
            f"published-at: {published_at.isoformat()}",
            f"role: {source.role.value}",
            "kind: publication",
            "reason: Selected publication",
            "",
            "## EVENT R1",
            "date: 2026-08-05",
            "sources: S1",
            f"text: {event_text}",
        )
    )
    document_id = document_id or uuid4()
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject.id,
        research_date=snapshot.research_date,
        production_input_hash=snapshot.input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url=source.canonical_url,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=source.role,
                title=source.title,
                publisher=source.publisher,
                published_at=published_at,
                source_collection_id=None,
                source_document_id=document_id,
                discovery_candidate_ids=(),
                collection_state=CollectionState.ARCHIVED,
                content_sha256=content_sha256,
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
            ),
        ),
        warnings=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject.id,
        production_input_hash=snapshot.input_hash,
        references_corpus_hash=references_corpus_hash(corpus),
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=document_id,
                canonical_url=source.canonical_url,
                content_sha256=content_sha256,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=source.role,
                profile=ExtractionProfile.FULL,
                checkpoint_id=None,
                reuse_state=ExtractionReuseState.FRESH,
                facts=(),
                events=(
                    ExtractionEventV1(
                        event_date=date(2026, 8, 5),
                        date_text=None,
                        text=event_text,
                        context="Campaign chronology.",
                        evidence_quote=event_text,
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(document_id,),
                    ),
                ),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    refs_hash = _references_input_hash(snapshot=snapshot, research_date=snapshot.research_date)
    refs_raw_id, refs_blob_id, _ = await store.store_stage_payloads(
        raw=raw, canonical=production_reference_corpus_to_json(corpus)
    )
    _, extraction_blob_id, _ = await store.store_stage_payloads(
        canonical=production_extraction_to_json(extraction)
    )
    await _ensure_synthesis_source_document(
        uow_factory, store, subject=subject, source=source, extraction=extraction
    )
    # The canonical synthesis identity is the frozen snapshot, the canonical
    # extraction and the exact source access policy; no legacy report hash
    # participates any more.
    async with uow_factory() as uow:
        policy = await build_synthesis_access_policy(snapshot, extraction, uow.source_documents)
    # The production orchestrator classifies with the model classifier enabled by
    # default; its projection identity is part of every later stage identity.
    projection = build_relevance_projection(
        snapshot, extraction, classifier=ModelRelevanceClassifier(None)
    )
    evidence_pack = build_synthesis_evidence_pack(snapshot, extraction, projection)
    return SimpleNamespace(
        projection=projection,
        refs_hash=refs_hash,
        refs_raw_id=refs_raw_id,
        refs_blob_id=refs_blob_id,
        extraction_hash=extraction_input_hash(
            references_corpus_hash=references_corpus_hash(corpus)
        ),
        extraction=extraction,
        extraction_blob_id=extraction_blob_id,
        synthesis_hash=synthesis_input_hash(
            snapshot,
            extraction,
            evidence_pack,
            synthesis_access_policy_hash(policy),
        ),
    )


async def _store_empty_enrichment(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    run_id: UUID,
    subject_id: UUID,
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    projection: RelevanceProjectionV1,
) -> ProductionArtifact:
    enrichment = build_empty_editorial_enrichment(
        extraction=extraction,
        synthesis=synthesis,
    )
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
    async with uow_factory() as uow:
        access_policy = await build_synthesis_access_policy(
            snapshot, extraction, uow.source_documents
        )
    evidence_pack_hash = editorial_enrichment_evidence_pack_hash(
        build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis, projection)
    )
    access_policy_hash = synthesis_access_policy_hash(access_policy)
    return await EditorialEnrichmentService(uow_factory, store).store_editorial_enrichment_result(
        run_id=run_id,
        subject_id=subject_id,
        input_hash=compute_editorial_enrichment_input_hash(
            extraction=extraction,
            synthesis=synthesis,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=access_policy_hash,
            projection_hash=projection.projection_hash,
        ),
        enrichment=enrichment,
        extraction=extraction,
        synthesis=synthesis,
        raw_result=None,
        model_run_id=None,
        evidence_pack_hash=evidence_pack_hash,
        access_policy_hash=access_policy_hash,
        model_policy_version=EDITORIAL_ENRICHMENT_MODEL_POLICY_VERSION,
        routing_policy_version=EDITORIAL_ENRICHMENT_ROUTING_POLICY_VERSION,
        projection_hash=projection.projection_hash,
    )


async def _projection_artifact(
    store: ProductionArtifactStore,
    *,
    run_id: UUID,
    subject_id: UUID,
    projection: RelevanceProjectionV1,
) -> ProductionArtifact:
    _, canonical_id, _ = await store.store_stage_payloads(
        canonical=relevance_projection_to_json(projection)
    )
    return ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash=projection.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_id,
        metadata={
            "projection_hash": projection.projection_hash,
            "extraction_hash": projection.extraction_hash,
        },
    )


async def _assert_enrichment_lineage(
    store: ProductionArtifactStore,
    artifacts: dict[ProductionArtifactStage, ProductionArtifact],
    *,
    run_id: UUID,
    subject_id: UUID,
) -> None:
    enrichment_artifact = artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT]
    extraction_artifact = artifacts[ProductionArtifactStage.EXTRACTION]
    synthesis_artifact = artifacts[ProductionArtifactStage.SYNTHESIS]
    assert enrichment_artifact.production_run_id == run_id
    assert enrichment_artifact.subject_id == subject_id
    assert enrichment_artifact.status is ProductionArtifactStatus.VERIFIED
    if enrichment_artifact.raw_blob_id is not None:
        await store.read_bytes(enrichment_artifact.raw_blob_id)
    assert enrichment_artifact.rendered_blob_id is None
    assert enrichment_artifact.canonical_blob_id is not None
    assert extraction_artifact.canonical_blob_id is not None
    assert synthesis_artifact.canonical_blob_id is not None

    extraction = production_extraction_from_json(
        await store.read_json(extraction_artifact.canonical_blob_id)
    )
    synthesis = production_synthesis_from_json(
        await store.read_json(synthesis_artifact.canonical_blob_id)
    )
    enrichment = editorial_enrichment_from_json(
        await store.read_json(enrichment_artifact.canonical_blob_id)
    )
    validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
    assert enrichment.subject_id == subject_id
    assert enrichment.production_input_hash == synthesis.production_input_hash
    assert enrichment_artifact.input_hash == compute_editorial_enrichment_input_hash(
        extraction=extraction,
        synthesis=synthesis,
        evidence_pack_hash=enrichment_artifact.metadata["evidence_pack_hash"],
        access_policy_hash=enrichment_artifact.metadata["access_policy_hash"],
        projection_hash=enrichment_artifact.metadata["relevance_projection_hash"],
    )
    assert enrichment_artifact.metadata["extraction_hash"] == enrichment.extraction_hash
    assert enrichment_artifact.metadata["synthesis_hash"] == enrichment.synthesis_hash


async def _seed_reusable_article(
    uow_factory: UnitOfWorkFactory,
    store: ProductionArtifactStore,
    *,
    edition: Edition,
    subject: Subject,
    title: str,
    batch: DiscoveryBatch,
    source: SourceCandidate,
) -> tuple[
    ProductionRun,
    dict[ProductionArtifactStage, ProductionArtifact],
    ProductionArtifact,
]:
    """Create one complete first pass whose costly inputs can be reused."""
    production = SubjectProductionService(uow_factory)
    source_run, created = await production.create_run(subject.id, edition.id)
    assert created
    source_run = await production.start_run(source_run.id)
    async with uow_factory() as uow:
        persisted = await uow.production_runs.get_for_update(source_run.id)
        assert persisted is not None
        persisted.current_stage = ProductionStage.ASSEMBLY
        persisted.mark_ready()
        await uow.production_runs.save(persisted)
        await uow.commit()
        snapshot = await uow.production_input_snapshots.get_by_run(source_run.id)
    assert snapshot is not None

    first_pass = await _store_canonical_first_pass(
        uow_factory,
        store,
        snapshot=snapshot,
        subject=subject,
        source=source,
        event_text=f"{title} was reported.",
    )
    refs_hash = first_pass.refs_hash
    extraction_hash = first_pass.extraction_hash
    synthesis_hash = first_pass.synthesis_hash
    refs_raw_id, refs_blob_id = first_pass.refs_raw_id, first_pass.refs_blob_id
    extraction_raw_id, extraction_blob_id = None, first_pass.extraction_blob_id
    synthesis = _canonical_synthesis(snapshot, first_pass.extraction, f"{title} was reported.")
    _, synthesis_canonical_id, synthesis_blob_id = await store.store_stage_payloads(
        raw=f"{title} synthesis",
        canonical=production_synthesis_to_json(synthesis),
        rendered="Deliberately unrelated preview [S99].",
    )
    source_artifacts = {
        ProductionArtifactStage.REFERENCES: ProductionArtifact(
            production_run_id=source_run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=refs_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=refs_raw_id,
            canonical_blob_id=refs_blob_id,
        ),
        ProductionArtifactStage.EXTRACTION: ProductionArtifact(
            production_run_id=source_run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=extraction_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=extraction_raw_id,
            canonical_blob_id=extraction_blob_id,
        ),
        ProductionArtifactStage.SYNTHESIS: ProductionArtifact(
            production_run_id=source_run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=synthesis_hash,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=synthesis_canonical_id,
            rendered_blob_id=synthesis_blob_id,
            metadata={"relevance_projection_hash": first_pass.projection.projection_hash},
        ),
        ProductionArtifactStage.RELEVANCE_PROJECTION: await _projection_artifact(
            store,
            run_id=source_run.id,
            subject_id=subject.id,
            projection=first_pass.projection,
        ),
    }
    async with uow_factory() as uow:
        for artifact in source_artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    source_artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT] = await _store_empty_enrichment(
        uow_factory,
        store,
        run_id=source_run.id,
        subject_id=subject.id,
        snapshot=snapshot,
        extraction=first_pass.extraction,
        synthesis=synthesis,
        projection=first_pass.projection,
    )

    references = production_reference_corpus_from_json(await store.read_json(refs_blob_id))
    async with uow_factory() as uow:
        enrichment = editorial_enrichment_from_json(
            await store.read_json(
                source_artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT].canonical_blob_id
            )
        )
        publication = await PublicationAssemblyService(
            store, uow.production_artifacts
        ).assemble_publication(
            run=source_run,
            snapshot=snapshot,
            references=references,
            extraction=first_pass.extraction,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
            relevance_projection=first_pass.projection,
        )
        await uow.commit()
    return source_run, source_artifacts, publication


@pytest.mark.asyncio
async def test_postgres_run_b_reuses_all_costly_artifacts_from_run_a(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition = _edition()
    await _persist_edition(uow_factory, edition)
    subject = Subject(
        edition_id=edition.id,
        title="Reuse subject",
        slug=f"reuse-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    catalog = BlobCatalogService(blob_store, uow_factory)
    store = ProductionArtifactStore(catalog)
    created_at = datetime.now(UTC) - timedelta(minutes=1)
    source_run, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=created_at,
    )
    target_run = ProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        run_number=2,
        status=ProductionRunStatus.RUNNING,
        current_stage=ProductionStage.REFERENCES,
    )
    async with uow_factory() as uow:
        await uow.production_runs.add(target_run)
        await uow.commit()

    service = ProductionArtifactReuseService(uow_factory, store)
    for stage, source in source_artifacts.items():
        result = await service.find_or_reuse(
            run=target_run,
            stage=stage,
            input_hash=source.input_hash,
        )
        assert result is not None
        assert result.reused is True
        assert result.artifact.production_run_id == target_run.id
        assert result.artifact.production_run_id != source_run.id
        assert result.artifact.reused_from_artifact_id == source.id
        assert result.artifact.raw_blob_id == source.raw_blob_id
        assert result.artifact.canonical_blob_id == source.canonical_blob_id
        assert result.artifact.rendered_blob_id == source.rendered_blob_id

    async with uow_factory() as uow:
        source_rows = await uow.production_artifacts.list_for_run(source_run.id)
        target_rows = await uow.production_artifacts.list_for_run(target_run.id)
    assert {row.id for row in source_rows} == {
        artifact.id for artifact in source_artifacts.values()
    }
    assert {row.stage for row in target_rows} == set(source_artifacts)
    assert all(row.reused_from_artifact_id is not None for row in target_rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_stage", "references_allowed", "extraction_allowed"),
    (
        (ProductionStage.REFERENCES, False, False),
        (ProductionStage.EXTRACTION, True, False),
        (ProductionStage.SYNTHESIS, True, True),
    ),
)
async def test_postgres_invalidation_blocks_only_downstream_stages(
    uow_factory: UnitOfWorkFactory,
    tmp_path: Path,
    from_stage: ProductionStage,
    references_allowed: bool,
    extraction_allowed: bool,
) -> None:
    edition = _edition()
    await _persist_edition(uow_factory, edition)
    subject = Subject(
        edition_id=edition.id,
        title="Invalidation subject",
        slug=f"invalidate-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    source_run, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    occurred_at = datetime.now(UTC) - timedelta(minutes=1)
    async with uow_factory() as uow:
        await uow.production_reuse_invalidations.add(
            ProductionReuseInvalidation(
                edition_id=edition.id,
                subject_id=subject.id,
                from_stage=from_stage,
                actor_id="operator",
                correlation_id=str(uuid4()),
                occurred_at=occurred_at,
            )
        )
        target_run = ProductionRun(
            subject_id=subject.id,
            edition_id=edition.id,
            run_number=2,
            status=ProductionRunStatus.RUNNING,
            current_stage=ProductionStage.REFERENCES,
        )
        await uow.production_runs.add(target_run)
        await uow.commit()

    service = ProductionArtifactReuseService(uow_factory, store)
    references = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.REFERENCES,
        input_hash=source_artifacts[ProductionArtifactStage.REFERENCES].input_hash,
    )
    extraction = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.EXTRACTION,
        input_hash=source_artifacts[ProductionArtifactStage.EXTRACTION].input_hash,
    )
    synthesis = await service.find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.SYNTHESIS,
        input_hash=source_artifacts[ProductionArtifactStage.SYNTHESIS].input_hash,
    )

    assert (references is not None) is references_allowed
    assert (extraction is not None) is extraction_allowed
    assert synthesis is None
    async with uow_factory() as uow:
        old_rows = await uow.production_artifacts.list_for_run(source_run.id)
    assert len(old_rows) == 3


@pytest.mark.asyncio
async def test_postgres_persists_forced_editorial_enrichment_retry(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """The domain retry marker must satisfy the real ``ck_run_force_recompute_stage``."""
    edition = _edition()
    await _persist_edition(uow_factory, edition)
    subject = Subject(
        edition_id=edition.id,
        title="Editorial retry subject",
        slug=f"editorial-retry-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    run, _ = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    retry = await SubjectProductionService(uow_factory).retry_from_stage(
        run.id, ProductionStage.EDITORIAL_ENRICHMENT, force_recompute=True
    )

    assert retry.run.force_recompute_from_stage is ProductionStage.EDITORIAL_ENRICHMENT
    async with uow_factory() as uow:
        persisted = await uow.production_runs.get(run.id)
    assert persisted is not None
    assert persisted.status is ProductionRunStatus.RUNNING
    assert persisted.current_stage is ProductionStage.EDITORIAL_ENRICHMENT
    assert persisted.pipeline_generation == run.pipeline_generation + 1
    assert persisted.force_recompute_from_stage is ProductionStage.EDITORIAL_ENRICHMENT


@pytest.mark.asyncio
async def test_postgres_persists_editorial_enrichment_reuse_invalidation(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    edition = _edition()
    await _persist_edition(uow_factory, edition)
    subject = Subject(
        edition_id=edition.id,
        title="Editorial invalidation subject",
        slug=f"editorial-invalidate-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    _, source_artifacts = await _seed_computed_run(
        uow_factory,
        store,
        edition=edition,
        subject=subject,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    invalidation = ProductionReuseInvalidation(
        edition_id=edition.id,
        subject_id=subject.id,
        from_stage=ProductionStage.EDITORIAL_ENRICHMENT,
        actor_id="operator",
        correlation_id=str(uuid4()),
        occurred_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    async with uow_factory() as uow:
        await uow.production_reuse_invalidations.add(invalidation)
        target_run = ProductionRun(
            subject_id=subject.id,
            edition_id=edition.id,
            run_number=2,
            status=ProductionRunStatus.RUNNING,
            current_stage=ProductionStage.REFERENCES,
        )
        await uow.production_runs.add(target_run)
        await uow.commit()

    async with uow_factory() as uow:
        persisted = await uow.production_reuse_invalidations.list_for_subject(
            edition.id, subject.id
        )
    assert [item.from_stage for item in persisted] == [ProductionStage.EDITORIAL_ENRICHMENT]
    assert persisted[0].id == invalidation.id
    # The cutoff starts at EDITORIAL_ENRICHMENT: upstream synthesis stays reusable.
    synthesis = await ProductionArtifactReuseService(uow_factory, store).find_or_reuse(
        run=target_run,
        stage=ProductionArtifactStage.SYNTHESIS,
        input_hash=source_artifacts[ProductionArtifactStage.SYNTHESIS].input_hash,
    )
    assert synthesis is not None


@pytest.mark.asyncio
async def test_real_orchestrator_reuses_run_a_then_freezes_run_b_identity(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Run the canonical A -> B proof through the real SQL UoW and orchestrator."""
    edition = _edition(country="Germany", country_code="DE")
    await _persist_edition(uow_factory, edition)
    subject = Subject(
        edition_id=edition.id,
        title="Orchestrator subject",
        slug=f"orchestrator-{uuid4().hex}",
        tlp=TLP.AMBER,
    )
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    catalog = BlobCatalogService(blob_store, uow_factory)
    store = ProductionArtifactStore(catalog)

    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()

    discovery_run = await make_discovery_run_for_edition(
        uow_factory, edition, complementary_axis="reuse integration"
    )
    batch, source = _production_context_entities(
        edition=edition,
        subject=subject,
        discovery_run_id=discovery_run.id,
    )
    discovery_model_run = ModelRun(
        id=batch.discovery_model_run_id,
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="integration",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )
    collection = SourceCollection(
        subject_id=subject.id,
        edition_id=edition.id,
        batch_id=batch.id,
        source_candidate_id=source.id,
        requested_url=source.url,
        canonical_url=source.canonical_url,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        source_tlp=source.tlp,
        sensitivity=source.sensitivity,
        external_llm_allowed=True,
        proposed_role=source.role,
        origin_kind=SourceOriginKind.DISCOVERY,
        state=CollectionState.ARCHIVED,
    )

    # The retry extraction is deliberately live-model based, but its
    # structured output must still be checked against the exact archived
    # decoded bytes for this source.
    # Unique bytes: source checkpoints are content-addressed and shared by the
    # session-scoped integration database.
    archived_content = f"Example actor archived source evidence {uuid4().hex}".encode()
    raw_blob = await catalog.ingest(
        BytesIO(archived_content),
        logical_bucket="source-raw",
        mime_type="application/octet-stream",
    )
    decoded_blob = await catalog.ingest(
        BytesIO(archived_content),
        logical_bucket="source-decoded",
        mime_type="text/plain",
    )
    source_document = SourceDocument(
        subject_id=subject.id,
        blob_id=raw_blob.id,
        original_name="source.txt",
        origin=source.canonical_url,
        acquired_at=datetime(2026, 8, 5, tzinfo=UTC),
        license_restriction=None,
        tlp=source.tlp,
        do_not_submit=False,
        external_llm_allowed=True,
        source_collection_id=collection.id,
        source_candidate_id=source.id,
        decoded_blob_id=decoded_blob.id,
        title=source.title,
        publisher=source.publisher,
        published_at=source.published_at,
        final_url=source.canonical_url,
        detected_mime_type="text/plain",
        encoded_sha256=hashlib.sha256(archived_content).hexdigest(),
        decoded_sha256=hashlib.sha256(archived_content).hexdigest(),
        encoded_size=len(archived_content),
        decoded_size=len(archived_content),
    )
    collection.source_document_id = source_document.id
    collection.decoded_blob_id = decoded_blob.id

    async with uow_factory() as uow:
        await uow.model_runs.add(discovery_model_run)
        assert await uow.discovery_batches.add_if_absent(batch)
        await uow.source_documents.add(source_document)
        assert await uow.source_collections.add_if_absent(collection)
        await uow.commit()

    await _seed_subject_discovery_lineage(
        uow_factory,
        edition=edition,
        subject_batches=((subject, batch),),
    )
    production = SubjectProductionService(uow_factory)
    run_a, created_a = await production.create_run(subject.id, edition.id)
    assert created_a
    run_a = await production.start_run(run_a.id)
    async with uow_factory() as uow:
        persisted_a = await uow.production_runs.get_for_update(run_a.id)
        assert persisted_a is not None
        persisted_a.current_stage = ProductionStage.ASSEMBLY
        persisted_a.mark_ready()
        await uow.production_runs.save(persisted_a)
        await uow.commit()
        snapshot_a = await uow.production_input_snapshots.get_by_run(run_a.id)
    assert snapshot_a is not None

    first_pass = await _store_canonical_first_pass(
        uow_factory,
        store,
        snapshot=snapshot_a,
        subject=subject,
        source=source,
        event_text="The selected campaign was reported.",
        document_id=source_document.id,
        content_sha256=source_document.decoded_sha256,
    )
    refs_hash = first_pass.refs_hash
    extraction_hash = first_pass.extraction_hash
    synthesis_hash = first_pass.synthesis_hash
    refs_raw_id, refs_blob_id = first_pass.refs_raw_id, first_pass.refs_blob_id
    extraction_raw_id, extraction_blob_id = None, first_pass.extraction_blob_id
    synthesis = _canonical_synthesis(
        snapshot_a, first_pass.extraction, "The selected campaign was reported."
    )
    _, synthesis_canonical_id, synthesis_blob_id = await store.store_stage_payloads(
        raw="synthesis A",
        canonical=production_synthesis_to_json(synthesis),
        rendered="Deliberately unrelated preview [S99].",
    )
    source_artifacts = {
        ProductionArtifactStage.REFERENCES: ProductionArtifact(
            production_run_id=run_a.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash=refs_hash,
            raw_blob_id=refs_raw_id,
            canonical_blob_id=refs_blob_id,
        ),
        ProductionArtifactStage.EXTRACTION: ProductionArtifact(
            production_run_id=run_a.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash=extraction_hash,
            raw_blob_id=extraction_raw_id,
            canonical_blob_id=extraction_blob_id,
        ),
        ProductionArtifactStage.SYNTHESIS: ProductionArtifact(
            production_run_id=run_a.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=synthesis_hash,
            canonical_blob_id=synthesis_canonical_id,
            rendered_blob_id=synthesis_blob_id,
            metadata={"relevance_projection_hash": first_pass.projection.projection_hash},
        ),
        ProductionArtifactStage.RELEVANCE_PROJECTION: await _projection_artifact(
            store,
            run_id=run_a.id,
            subject_id=subject.id,
            projection=first_pass.projection,
        ),
    }
    async with uow_factory() as uow:
        for artifact in source_artifacts.values():
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    source_enrichment_artifact = await _store_empty_enrichment(
        uow_factory,
        store,
        run_id=run_a.id,
        subject_id=subject.id,
        snapshot=snapshot_a,
        extraction=first_pass.extraction,
        synthesis=synthesis,
        projection=first_pass.projection,
    )

    references_a = production_reference_corpus_from_json(
        await store.read_json(
            source_artifacts[ProductionArtifactStage.REFERENCES].canonical_blob_id
        )
    )
    extraction_a = production_extraction_from_json(
        await store.read_json(
            source_artifacts[ProductionArtifactStage.EXTRACTION].canonical_blob_id
        )
    )
    enrichment_a = editorial_enrichment_from_json(
        await store.read_json(source_enrichment_artifact.canonical_blob_id)
    )
    async with uow_factory() as uow:
        publication_a = await PublicationAssemblyService(
            store, uow.production_artifacts
        ).assemble_publication(
            run=run_a,
            snapshot=snapshot_a,
            references=references_a,
            extraction=extraction_a,
            synthesis=synthesis,
            editorial_enrichment=enrichment_a,
            relevance_projection=first_pass.projection,
        )
        await uow.commit()

    run_b, created_b = await production.create_run(subject.id, edition.id)
    assert created_b
    assert run_b.id != run_a.id
    assert run_b.run_number == run_a.run_number + 1
    async with uow_factory() as uow:
        snapshot_b = await uow.production_input_snapshots.get_by_run(run_b.id)
    assert snapshot_b is not None
    assert snapshot_b.reuse_basis_hash == snapshot_a.reuse_basis_hash
    assert snapshot_b.research_date == snapshot_a.research_date
    assert snapshot_b.input_hash == snapshot_a.input_hash

    await production.start_run(run_b.id)
    await production.advance_stage(run_b.id)

    class SentinelModelGateway:
        async def execute(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("model must not be called on reuse hit")

    orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_gateway=SentinelModelGateway(),  # type: ignore[arg-type]
        artifact_store=store,
    )
    for stage in (
        ProductionStage.REFERENCES,
        ProductionStage.EXTRACTION,
        ProductionStage.RELEVANCE_PROJECTION,
        ProductionStage.SYNTHESIS,
    ):
        result = await orchestrator.execute_stage(run_b.id, stage)
        assert result["status"] == "reused"
        if stage is not ProductionStage.SYNTHESIS:
            await production.advance_stage(run_b.id)

    await production.advance_stage(run_b.id)
    enrichment_result = await orchestrator.execute_stage(
        run_b.id, ProductionStage.EDITORIAL_ENRICHMENT
    )
    assert enrichment_result["status"] == "success"
    await production.advance_stage(run_b.id)
    assembly_result = await orchestrator.execute_stage(run_b.id, ProductionStage.ASSEMBLY)
    assert assembly_result["status"] == "success"

    async with uow_factory() as uow:
        artifacts_b = {
            artifact.stage: artifact
            for artifact in await uow.production_artifacts.list_for_run(run_b.id)
        }
        persisted_b = await uow.production_runs.get(run_b.id)
    assert persisted_b is not None
    assert persisted_b.status is ProductionRunStatus.READY
    assert source_enrichment_artifact.status is ProductionArtifactStatus.VERIFIED
    for stage, source_artifact in source_artifacts.items():
        reused = artifacts_b[stage]
        assert reused.id != source_artifact.id
        assert reused.production_run_id == run_b.id
        assert reused.reused_from_artifact_id == source_artifact.id
        assert reused.canonical_blob_id == source_artifact.canonical_blob_id
        assert reused.rendered_blob_id == source_artifact.rendered_blob_id
    await _assert_enrichment_lineage(
        store,
        artifacts_b,
        run_id=run_b.id,
        subject_id=subject.id,
    )
    publication_b = artifacts_b[ProductionArtifactStage.PUBLICATION]
    assert publication_b.production_run_id == run_b.id
    assert publication_b.status is ProductionArtifactStatus.VERIFIED
    assert publication_b.reused_from_artifact_id == publication_a.id
    assert publication_b.canonical_blob_id == publication_a.canonical_blob_id
    assert publication_b.id != publication_a.id

    retry = await production.retry_from_stage(run_b.id, ProductionStage.EXTRACTION)
    assert retry.previous_status is ProductionRunStatus.READY
    assert retry.run.status is ProductionRunStatus.RUNNING
    assert retry.run.current_stage is ProductionStage.EXTRACTION
    assert retry.run.pipeline_generation == persisted_b.pipeline_generation + 1
    assert retry.run.force_recompute_from_stage is ProductionStage.EXTRACTION
    assert retry.staled_artifacts == [
        "extraction",
        "relevance_projection",
        "synthesis",
        "editorial_enrichment",
        "publication",
    ]

    async with uow_factory() as uow:
        stale_artifacts = {
            artifact.stage: artifact
            for artifact in await uow.production_artifacts.list_for_run(run_b.id)
        }
    assert (
        stale_artifacts[ProductionArtifactStage.REFERENCES].status
        is ProductionArtifactStatus.VERIFIED
    )
    assert (
        stale_artifacts[ProductionArtifactStage.EXTRACTION].status is ProductionArtifactStatus.STALE
    )
    assert (
        stale_artifacts[ProductionArtifactStage.RELEVANCE_PROJECTION].status
        is ProductionArtifactStatus.STALE
    )
    assert (
        stale_artifacts[ProductionArtifactStage.SYNTHESIS].status is ProductionArtifactStatus.STALE
    )
    assert (
        stale_artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT].status
        is ProductionArtifactStatus.STALE
    )
    assert (
        stale_artifacts[ProductionArtifactStage.PUBLICATION].status
        is ProductionArtifactStatus.STALE
    )

    retry_adapter = _CountingRetryModelAdapter()
    retry_router = ModelRouter(
        openai_research=retry_adapter,
        openai_structured=retry_adapter,
        openai_drafting=retry_adapter,
        qwen=retry_adapter,
        fake=retry_adapter,
    )
    retry_gateway = ModelGateway(
        retry_router,
        uow_factory,
        BlobModelOutputStore(catalog),
    )
    retry_orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_gateway=retry_gateway,
        artifact_store=store,
    )

    extraction_retry = await retry_orchestrator.execute_stage(run_b.id, ProductionStage.EXTRACTION)
    assert extraction_retry["status"] == "success"
    extraction_technical_replay = await retry_orchestrator.execute_stage(
        run_b.id, ProductionStage.EXTRACTION
    )
    assert extraction_technical_replay["status"] == "cached"
    assert len(retry_adapter.calls) == 1
    await production.advance_stage(run_b.id)

    projection_retry = await retry_orchestrator.execute_stage(
        run_b.id, ProductionStage.RELEVANCE_PROJECTION
    )
    assert projection_retry["status"] == "success"
    assert len(retry_adapter.calls) == 2
    await production.advance_stage(run_b.id)

    synthesis_retry = await retry_orchestrator.execute_stage(run_b.id, ProductionStage.SYNTHESIS)
    assert synthesis_retry["status"] == "success"
    synthesis_technical_replay = await retry_orchestrator.execute_stage(
        run_b.id, ProductionStage.SYNTHESIS
    )
    assert synthesis_technical_replay["status"] == "cached"
    assert len(retry_adapter.calls) == 3
    await production.advance_stage(run_b.id)
    enrichment_retry = await retry_orchestrator.execute_stage(
        run_b.id, ProductionStage.EDITORIAL_ENRICHMENT
    )
    assert enrichment_retry["status"] == "success"
    await production.advance_stage(run_b.id)
    retry_assembly = await retry_orchestrator.execute_stage(run_b.id, ProductionStage.ASSEMBLY)
    assert retry_assembly["status"] == "success"

    async with uow_factory() as uow:
        artifacts_b = {
            artifact.stage: artifact
            for artifact in await uow.production_artifacts.list_for_run(run_b.id)
        }
        persisted_b = await uow.production_runs.get(run_b.id)
    assert persisted_b is not None
    assert persisted_b.status is ProductionRunStatus.READY
    assert artifacts_b[ProductionArtifactStage.REFERENCES].id == (
        stale_artifacts[ProductionArtifactStage.REFERENCES].id
    )
    assert artifacts_b[ProductionArtifactStage.EXTRACTION].id != (
        stale_artifacts[ProductionArtifactStage.EXTRACTION].id
    )
    assert artifacts_b[ProductionArtifactStage.SYNTHESIS].id != (
        stale_artifacts[ProductionArtifactStage.SYNTHESIS].id
    )
    assert artifacts_b[ProductionArtifactStage.EDITORIAL_ENRICHMENT].id != (
        stale_artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT].id
    )
    assert artifacts_b[ProductionArtifactStage.EXTRACTION].reused_from_artifact_id is None
    assert artifacts_b[ProductionArtifactStage.SYNTHESIS].reused_from_artifact_id is None
    assert artifacts_b[ProductionArtifactStage.EDITORIAL_ENRICHMENT].reused_from_artifact_id is None
    await _assert_enrichment_lineage(
        store,
        artifacts_b,
        run_id=run_b.id,
        subject_id=subject.id,
    )
    publication_b = artifacts_b[ProductionArtifactStage.PUBLICATION]
    assert publication_b.production_run_id == run_b.id
    assert publication_b.status is ProductionArtifactStatus.VERIFIED
    assert publication_b.reused_from_artifact_id is None

    review_batch = EditionProductionBatch(
        edition_id=edition.id,
        status="running",
        phase=ProductionBatchPhase.REVIEW,
    )
    review_item = EditionProductionBatchItem(
        batch_id=review_batch.id,
        subject_id=subject.id,
        production_run_id=run_b.id,
        position=1,
    )
    async with uow_factory() as uow:
        await uow.edition_production_batches.add(review_batch)
        await uow.commit()

    async with uow_factory() as uow:
        await uow.edition_production_batch_items.append_many((review_item,))
        await uow.commit()

    async with uow_factory() as uow:
        review_rows = await uow.edition_review_read_model.list_for_edition(edition.id)
    assert len(review_rows) == 1
    assert review_rows[0].run_id == run_b.id
    assert review_rows[0].pipeline_generation == persisted_b.pipeline_generation
    assert review_rows[0].document_artifact_id == publication_b.id

    accepted = await EditionPublicationService(uow_factory, store).accept(
        edition.id, actor_id="reviewer", correlation_id="reuse-integration"
    )
    assert accepted.manifest.entries[0].production_run_id == run_b.id
    assert accepted.manifest.entries[0].pipeline_generation == persisted_b.pipeline_generation
    assert accepted.manifest.entries[0].document_artifact_id == publication_b.id
    assert accepted.manifest.entries[0].document_artifact_id != publication_a.id


@pytest.mark.asyncio
async def test_two_article_cached_edition_is_sequential_and_uses_new_publications(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    """Mirror the low-cost manual batch with two real PostgreSQL-backed runs."""
    edition = _edition(country="Italy", country_code="IT")
    await _persist_edition(uow_factory, edition)
    subjects = [
        Subject(
            edition_id=edition.id,
            title=f"Article {label}",
            slug=f"two-article-{label.lower()}-{uuid4().hex}",
            tlp=TLP.AMBER,
        )
        for label in ("A", "B")
    ]
    store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    )
    async with uow_factory() as uow:
        await uow.editions.add_if_absent(edition)
        for subject in subjects:
            await uow.subjects.add(subject)
        await uow.commit()

    source_publications: list[ProductionArtifact] = []
    source_enrichments: list[ProductionArtifact] = []
    prepared_articles: list[tuple[DiscoveryBatch, SourceCandidate]] = []
    for subject, title in zip(subjects, ("Article A", "Article B"), strict=True):
        prepared_articles.append(
            await _prepare_reusable_article(
                uow_factory,
                edition=edition,
                subject=subject,
                title=title,
            )
        )

    await _seed_subject_discovery_lineage(
        uow_factory,
        edition=edition,
        subject_batches=tuple(
            (subject, batch)
            for subject, (batch, _) in zip(subjects, prepared_articles, strict=True)
        ),
    )

    for subject, title, (article_batch, source) in zip(
        subjects, ("Article A", "Article B"), prepared_articles, strict=True
    ):
        _source_run, source_artifacts, source_publication = await _seed_reusable_article(
            uow_factory,
            store,
            edition=edition,
            subject=subject,
            title=title,
            batch=article_batch,
            source=source,
        )
        source_publications.append(source_publication)
        source_enrichments.append(source_artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT])

    batch_service = ProductionBatchService(uow_factory)
    created = await batch_service.create(
        edition.id,
        [subject.id for subject in subjects],
        idempotency_key=f"reuse-postgres-{edition.id.hex}",
    )
    batch = created.batch
    first = await batch_service.start_next(batch.id)
    assert first is not None
    assert first.subject_id == subjects[0].id

    async with uow_factory() as uow:
        batch_items = await uow.edition_production_batch_items.list_for_batch(batch.id)
        queued_runs = [
            await uow.production_runs.get(item.production_run_id) for item in batch_items
        ]
    assert [run.status for run in queued_runs if run is not None] == [
        ProductionRunStatus.RUNNING,
        ProductionRunStatus.QUEUED,
    ]
    second_id = batch_items[1].production_run_id

    class SentinelModelGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.calls += 1
            raise AssertionError("no model call expected during cached edition smoke")

    sentinel = SentinelModelGateway()
    orchestrator = ProductionWorkflowOrchestrator(
        uow_factory,
        model_gateway=sentinel,  # type: ignore[arg-type]
        artifact_store=store,
    )
    cached_results: list[dict[str, object]] = []
    enrichment_results: list[dict[str, object]] = []

    async def execute_cached(run_id: UUID) -> None:
        await SubjectProductionService(uow_factory).advance_stage(run_id)
        for stage in (
            ProductionStage.REFERENCES,
            ProductionStage.EXTRACTION,
            ProductionStage.RELEVANCE_PROJECTION,
            ProductionStage.SYNTHESIS,
        ):
            result = await orchestrator.execute_stage(run_id, stage)
            cached_results.append(result)
            assert result["status"] == "reused"
            if stage is not ProductionStage.SYNTHESIS:
                await SubjectProductionService(uow_factory).advance_stage(run_id)
        await SubjectProductionService(uow_factory).advance_stage(run_id)
        enrichment_result = await orchestrator.execute_stage(
            run_id, ProductionStage.EDITORIAL_ENRICHMENT
        )
        enrichment_results.append(enrichment_result)
        assert enrichment_result["status"] == "success"
        await SubjectProductionService(uow_factory).advance_stage(run_id)
        assembly_result = await orchestrator.execute_stage(run_id, ProductionStage.ASSEMBLY)
        assert assembly_result["status"] == "success"

    await execute_cached(first.id)
    second = await batch_service.on_subject_terminal(batch.id, first.id)
    assert second is not None
    assert second.id == second_id
    assert second.status is ProductionRunStatus.RUNNING
    await execute_cached(second.id)
    assert await batch_service.on_subject_terminal(batch.id, second.id) is None
    assert len(cached_results) == 8
    assert len(enrichment_results) == 2
    assert sentinel.calls == 0

    async with uow_factory() as uow:
        persisted_edition = await uow.editions.get(edition.id)
        review_rows = await uow.edition_review_read_model.list_for_edition(edition.id)
        target_publications = []
        target_artifact_sets = []
        for row in review_rows:
            artifacts = await uow.production_artifacts.list_for_run(row.run_id)
            target_artifact_sets.append({artifact.stage: artifact for artifact in artifacts})
            target_publications.append(
                next(
                    artifact
                    for artifact in artifacts
                    if artifact.stage is ProductionArtifactStage.PUBLICATION
                    and artifact.status is ProductionArtifactStatus.VERIFIED
                )
            )
    assert persisted_edition is not None
    assert persisted_edition.state is EditionStatus.OPEN
    assert [row.position for row in review_rows] == [1, 2]
    assert [row.run_id for row in review_rows] == [first.id, second.id]
    assert [artifact.id for artifact in target_publications] != [
        publication.id for publication in source_publications
    ]
    target_enrichments = [
        artifacts[ProductionArtifactStage.EDITORIAL_ENRICHMENT]
        for artifacts in target_artifact_sets
    ]
    assert [artifact.id for artifact in target_enrichments] != [
        artifact.id for artifact in source_enrichments
    ]
    for run, artifacts in zip((first, second), target_artifact_sets, strict=True):
        await _assert_enrichment_lineage(
            store,
            artifacts,
            run_id=run.id,
            subject_id=run.subject_id,
        )

    accepted = await EditionPublicationService(uow_factory, store).accept(
        edition.id, actor_id="reviewer", correlation_id="two-article-cached-smoke"
    )
    assert [entry.production_run_id for entry in accepted.manifest.entries] == [
        first.id,
        second.id,
    ]
    assert [entry.document_artifact_id for entry in accepted.manifest.entries] == [
        artifact.id for artifact in target_publications
    ]

    release = await EditionAssemblyService(uow_factory, store).assemble(accepted.manifest.id)
    edition_json = await store.read_json(release.edition_document_blob_id)
    assert all(item["document"]["schema_version"] == "5" for item in edition_json["publications"])
    assert [item["document"]["title"] for item in edition_json["publications"]] == [
        "Article A",
        "Article B",
    ]
