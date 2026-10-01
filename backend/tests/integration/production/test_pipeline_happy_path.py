"""Business-level coverage of Sources -> References -> Q2 -> Synthesis -> READY."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

import pytest

from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelRole,
    ModelUsage,
    SafeModelRequest,
)
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.production_repairs import ProductionRepairMaterializationService
from cti_app.application.production_synthesis import (
    SynthesisClaimProposalV1,
    SynthesisProposalV1,
    SynthesisSectionProposalV1,
    canonical_extraction_hash,
    production_synthesis_from_json,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionRunStatus,
    ProductionStage,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_synthesis import SynthesisSectionKind
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import parse_publication_document

from .support import ProductionScenario, grounded_editorial_proposal

pytestmark = pytest.mark.integration

SOURCE_URLS = (
    "https://example.test/core",
    "https://example.test/secondary",
)

Q1_RESPONSE = """# REFERENCES

## SOURCE S1

title: ExampleRAT core report
url: https://example.test/core
publisher: Core Labs
published-at: 2026-08-10
role: primary
kind: publication
reason: Primary reporting on the campaign

## SOURCE S2

title: ExampleRAT secondary analysis
url: https://example.test/secondary
publisher: Secondary Labs
published-at: 2026-08-11
role: independent
kind: publication
reason: Independent corroboration of the campaign

## EVENT R1

date: 2026-08-15
sources: S1, S2
text: The campaign used the same loader and command-and-control pattern.
"""

Q2_CORE_RESPONSE = """FACT malware
- ExampleRAT :: The report identifies the malware family.

IOC confirmed domain
    - core-c2.security-lab.io :: Command-and-control domain in the report.
"""

Q2_SECONDARY_RESPONSE = """IOC contextual domain
- secondary-c2.security-lab.io :: A related infrastructure domain is discussed.
"""


def _sources() -> dict[str, dict[str, object]]:
    return {
        SOURCE_URLS[0]: {
            "status": 200,
            "mime": "text/html",
            "body": (
                "<html><body><h1>Core report</h1>"
                "ExampleRAT uses core-c2.security-lab.io for command and control."
                "</body></html>"
            ),
        },
        SOURCE_URLS[1]: {
            "status": 200,
            "mime": "text/html",
            "body": (
                "<html><body><h1>Secondary analysis</h1>"
                "The loader reaches secondary-c2.security-lab.io during execution."
                "</body></html>"
            ),
        },
    }


async def _configured_scenario(
    factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> ProductionScenario:
    scenario = factory(_sources())
    scenario.model.script.references(Q1_RESPONSE)
    scenario.model.script.q2(source_url=SOURCE_URLS[0], response=Q2_CORE_RESPONSE)
    scenario.model.script.q2(source_url=SOURCE_URLS[1], response=Q2_SECONDARY_RESPONSE)
    return scenario


def _canonical_proposal(request_text: str) -> SynthesisProposalV1:
    """Answer the canonical structured draft with grounded, atomic claims."""
    payload = json.loads(request_text)
    pack = payload["current_evidence_pack"]
    facts = [record for record in pack["narrative_evidence"] if record["kind"] == "fact"]
    assert facts, "The canonical evidence pack must expose the narrative fact"
    fact = facts[0]
    lead = SynthesisClaimProposalV1(
        text=f"{fact['value']} is documented by the selected publications.",
        evidence_handles=(fact["handle"],),
    )
    domains = tuple(
        SynthesisClaimProposalV1(
            text=f"The publications list the infrastructure domain {record['value']}.",
            evidence_handles=(record["handle"],),
        )
        for record in pack["technical_evidence"]
        if record["kind"] == "indicator"
    )
    sections = (
        SynthesisSectionProposalV1(
            kind=SynthesisSectionKind.INFRASTRUCTURE,
            heading="Infrastructure",
            claims=domains or (lead,),
        ),
    )
    return SynthesisProposalV1(lead=(lead,), sections=sections)


def _install_canonical_synthesis(scenario: ProductionScenario) -> list[SafeModelRequest]:
    """Answer canonical drafting requests and fail on any forbidden request."""
    adapter = scenario.model._adapter
    requests: list[SafeModelRequest] = []
    base_invoke = adapter.invoke

    async def invoke(
        request: SafeModelRequest,
        *,
        role: ModelRole,
        output_schema: type[Any] | None = None,
    ) -> AdapterResult:
        if request.prompt_template_id != "production-synthesis":
            return await base_invoke(request, role=role, output_schema=output_schema)
        if request.web_search:
            raise AssertionError("Canonical Synthesis must never enable web search")
        if request.conversation is not None:
            raise AssertionError("Canonical Synthesis must be stateless")
        requests.append(request)
        scenario.model.provider_calls.append(request)
        proposal = _canonical_proposal(request.text)
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=adapter.provider,
            requested_model=str(adapter.requested_model),
            actual_model_version=str(adapter.requested_model),
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            # model_runs.response_id is globally unique: derive it from the
            # gateway's per-attempt request id, never from a per-test counter.
            response_id=f"canonical-synthesis-{request.request_id}",
            output_text=proposal.model_dump_json(),
            structured_output=proposal,
        )

    adapter.invoke = invoke  # type: ignore[method-assign]
    return requests


@pytest.mark.asyncio
async def test_complete_production_pipeline_reaches_ready(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = await _configured_scenario(production_scenario_factory)
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    drafts = _install_canonical_synthesis(scenario)
    initial = await scenario.start()
    assert initial.status is ProductionRunStatus.RUNNING
    assert initial.current_stage is ProductionStage.SOURCES

    run = await scenario.run_until_terminal()

    assert len(drafts) == 1
    assert run.status is ProductionRunStatus.READY
    assert run.current_stage is ProductionStage.ASSEMBLY
    assert run.reconciliation is None
    assert run.error_code is None
    assert run.extraction_progress is not None
    completed_source_urls = {
        source["canonical_url"]
        for source in run.extraction_progress["sources"]
        if source["status"] == "succeeded"
    }
    assert completed_source_urls == set(SOURCE_URLS)
    assert run.extraction_progress["skipped_sources"] == 0
    assert (
        ProductionRecoveryPolicyV1.disposition_for_run(run)
        is ProductionRecoveryPolicyV1.MANUAL_ONLY
    )

    async with scenario.uow_factory() as uow:
        persisted_run = await uow.production_runs.get(run.id)
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        collections = list(await uow.source_collections.list_for_subject(run.subject_id))
        documents = list(await uow.source_documents.list_for_subject(run.subject_id))
        attempts = [
            attempt
            for collection in collections
            for attempt in await uow.collection_attempts.list_for_collection(collection.id)
        ]
        artifacts = list(await uow.production_artifacts.list_for_run(run.id))

    assert persisted_run is not None
    assert snapshot is not None
    assert {source.canonical_url for source in snapshot.core_sources} == set(SOURCE_URLS)
    assert len(collections) == 2
    assert all(collection.state is CollectionState.ARCHIVED for collection in collections)
    assert len(attempts) == len(collections)
    assert all(attempt.outcome.value == "succeeded" for attempt in attempts)
    assert len(documents) == len(collections)
    assert all(document.decoded_blob_id is not None for document in documents)
    assert all(document.decoded_sha256 and document.encoded_sha256 for document in documents)
    for document in documents:
        encoded = await scenario.artifact_store.read_bytes(document.blob_id)
        decoded = await scenario.artifact_store.read_bytes(document.decoded_blob_id)
        assert document.encoded_sha256 == hashlib.sha256(encoded).hexdigest()
        assert document.decoded_sha256 == hashlib.sha256(decoded).hexdigest()

    by_stage = {artifact.stage: artifact for artifact in artifacts}
    assert set(by_stage) == {
        ProductionArtifactStage.REFERENCES,
        ProductionArtifactStage.EXTRACTION,
        ProductionArtifactStage.SYNTHESIS,
        ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        ProductionArtifactStage.PUBLICATION,
    }
    assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in artifacts)
    assert [by_stage[stage].version for stage in by_stage] == [1, 1, 1, 1, 1]
    assert all(len(artifact.input_hash) == 64 for artifact in artifacts)
    assert by_stage[ProductionArtifactStage.REFERENCES].metadata["warnings"] == []
    assert by_stage[ProductionArtifactStage.EXTRACTION].metadata["warnings"] == []
    synthesis_artifact = by_stage[ProductionArtifactStage.SYNTHESIS]
    assert synthesis_artifact.canonical_blob_id is not None
    assert synthesis_artifact.rendered_blob_id is not None
    assert synthesis_artifact.conversation_turn_id is None
    assert synthesis_artifact.metadata["schema_version"] == 1
    assert synthesis_artifact.metadata["language"] == "fr"
    assert synthesis_artifact.metadata["mode"] == "fresh"
    extraction_metadata = by_stage[ProductionArtifactStage.EXTRACTION].metadata
    assert extraction_metadata["source_count"] == 2
    assert extraction_metadata["full_source_count"] == 2
    assert extraction_metadata["fresh_source_count"] == 2
    assert extraction_metadata["omitted_source_count"] == 0
    # The run-level artifact names no single model run and keeps no RAW.
    assert by_stage[ProductionArtifactStage.EXTRACTION].model_run_id is None
    assert by_stage[ProductionArtifactStage.EXTRACTION].raw_blob_id is None

    blob_ids = {
        blob_id
        for artifact in artifacts
        for blob_id in (
            artifact.raw_blob_id,
            artifact.canonical_blob_id,
            artifact.rendered_blob_id,
        )
        if blob_id is not None
    }
    assert blob_ids
    async with scenario.uow_factory() as uow:
        blobs = {blob_id: await uow.blobs.get(blob_id) for blob_id in blob_ids}
    assert all(blob is not None for blob in blobs.values())
    for blob_id, blob in blobs.items():
        assert blob is not None
        content = await scenario.artifact_store.read_bytes(blob_id)
        assert blob.descriptor.sha256 == hashlib.sha256(content).hexdigest()

    model_calls = scenario.model.calls
    q2_calls = [call for call in model_calls if call.stage == "extraction"]
    synthesis_requests = [
        call
        for call in scenario.model.provider_calls
        if call.prompt_template_id == "production-synthesis"
    ]
    editorial_requests = [
        call
        for call in scenario.model.provider_calls
        if call.prompt_template_id == "production-editorial-enrichment"
    ]
    assert model_calls[0].stage == "references"
    assert model_calls[-1].stage == "editorial_enrichment"
    assert all(call.stage == "extraction" for call in model_calls[1:-2])
    assert model_calls[-2].stage == "synthesis"
    # Canonical Synthesis is one stateless structured draft through the gateway.
    assert len(synthesis_requests) == 1
    assert synthesis_requests[0].web_search is False
    assert synthesis_requests[0].conversation is None
    assert len(editorial_requests) == 1
    assert editorial_requests[0].web_search is False
    assert editorial_requests[0].conversation is None
    covered_q2_urls = tuple(url for call in q2_calls for url in call.source_urls)
    assert set(covered_q2_urls) == set(SOURCE_URLS)
    assert covered_q2_urls == SOURCE_URLS
    assert len(model_calls) == 3 + len(q2_calls)
    # Only the References stage searches the web; extraction and synthesis are
    # stateless and offline.
    assert all(call.web_search is (call.stage == "references") for call in model_calls)
    assert all(call.conversation_id is None for call in model_calls)
    assert all(call.prompt_version for call in model_calls)
    assert all(call.model_run_id is not None for call in model_calls)

    async with scenario.uow_factory() as uow:
        model_runs = {
            call.model_run_id: await uow.model_runs.get(call.model_run_id)
            for call in model_calls
            if call.model_run_id is not None
        }
        assert synthesis_artifact.model_run_id is not None
        synthesis_draft_run = await uow.model_runs.get(synthesis_artifact.model_run_id)
        model_runs[synthesis_artifact.model_run_id] = synthesis_draft_run
        references_payload = await scenario.artifact_store.read_json(
            by_stage[ProductionArtifactStage.REFERENCES].canonical_blob_id  # type: ignore[arg-type]
        )
        extraction_payload = await scenario.artifact_store.read_json(
            by_stage[ProductionArtifactStage.EXTRACTION].canonical_blob_id  # type: ignore[arg-type]
        )
        synthesis_text = await scenario.artifact_store.read_text(
            synthesis_artifact.rendered_blob_id  # type: ignore[arg-type]
        )
        synthesis_payload = await scenario.artifact_store.read_json(
            synthesis_artifact.canonical_blob_id  # type: ignore[arg-type]
        )
        editorial_artifact = by_stage[ProductionArtifactStage.EDITORIAL_ENRICHMENT]
        assert editorial_artifact.model_run_id is not None
        editorial_payload = await scenario.artifact_store.read_json(
            editorial_artifact.canonical_blob_id  # type: ignore[arg-type]
        )
        publication_payload = await scenario.artifact_store.read_json(
            by_stage[ProductionArtifactStage.PUBLICATION].canonical_blob_id  # type: ignore[arg-type]
        )

    assert all(model_run is not None for model_run in model_runs.values())
    assert all(model_run.status is ModelRunStatus.SUCCEEDED for model_run in model_runs.values())
    assert all(model_run.raw_output_sha256 for model_run in model_runs.values())
    assert editorial_payload["source_figures"] == []
    assert len(editorial_payload["tables"]) == 1
    assert len(editorial_payload["diagrams"]) == 1
    assert model_calls[0].model_run_id == by_stage[ProductionArtifactStage.REFERENCES].model_run_id
    assert len(references_payload["sources"]) == 2
    assert {source["canonical_url"] for source in references_payload["sources"]} == set(SOURCE_URLS)
    reference_source_ids = {
        source["source_document_id"]
        for source in references_payload["sources"]
        if source["source_document_id"] is not None
    }
    indicators = {
        indicator["value"]: indicator
        for source in extraction_payload["sources"]
        for indicator in source["indicators"]
    }
    assert indicators["core-c2.security-lab.io"]["indicator_status"] == "confirmed_ioc"
    assert indicators["secondary-c2.security-lab.io"]["indicator_status"] == "contextual"
    assert all(indicator["evidence_quote"] for indicator in indicators.values())
    # Model provenance is source-level: each source names its checkpoint, whose
    # ModelRun is one of the extraction calls.
    async with scenario.uow_factory() as uow:
        checkpoint_runs = set()
        for source in extraction_payload["sources"]:
            rows = await uow.source_extractions.list_for_url(source["canonical_url"])
            row = next(row for row in rows if str(row.id) == source["checkpoint_id"])
            checkpoint_runs.add(row.model_run_id)
        checkpoint_model_runs = [await uow.model_runs.get(run_id) for run_id in checkpoint_runs]
    assert len(checkpoint_runs) == len(extraction_payload["sources"])
    assert all(
        model_run is not None and model_run.status is ModelRunStatus.SUCCEEDED
        for model_run in checkpoint_model_runs
    )
    assert "core-c2.security-lab.io" in synthesis_text
    assert "secondary-c2.security-lab.io" in synthesis_text
    assert "core-c2.security-lab.io" in str(publication_payload)
    assert "secondary-c2.security-lab.io" in str(publication_payload)
    assert publication_payload["schema_version"] == "4"
    assert publication_payload["title"] == synthesis_payload["title"]
    assert publication_payload["lead"] == synthesis_payload["lead"]
    assert publication_payload["sections"] == synthesis_payload["sections"]
    assert publication_payload["timeline"] == synthesis_payload["timeline"]
    assert {
        source["source_document_id"] for source in publication_payload["sources"]
    } == reference_source_ids
    assert "[S1]" not in str(publication_payload)
    extraction_indicator_values = {
        value
        for value, indicator in indicators.items()
        if indicator["indicator_status"] == "confirmed_ioc"
    }
    publication_indicator_values = {
        value["value"]
        for group in publication_payload["indicators"]
        for value in group["indicators"]
    }
    assert publication_indicator_values == extraction_indicator_values
    synthesis = production_synthesis_from_json(synthesis_payload)
    extraction = production_extraction_from_json(extraction_payload)
    assert synthesis.title == snapshot.subject_title
    assert synthesis.publication_language == snapshot.publication_language == "fr"
    assert synthesis.production_input_hash == snapshot.input_hash
    assert synthesis.extraction_hash == canonical_extraction_hash(extraction)
    assert synthesis.lead and all(paragraph.evidence_refs for paragraph in synthesis.lead)
    assert synthesis_draft_run is not None
    assert synthesis_draft_run.prompt_template_id == "production-synthesis"
    # The canonical prompt is evidence-only: no legacy local source ids survive.
    synthesis_prompt = synthesis_requests[0].text
    assert all(value in synthesis_prompt for value in extraction_indicator_values)
    assert "[S1]" not in synthesis_prompt
    for source in extraction_payload["sources"]:
        assert source["source_document_id"] not in synthesis_prompt

    async with scenario.uow_factory() as uow:
        refreshed = await uow.production_runs.get(run.id)
        refreshed_artifacts = await uow.production_artifacts.list_for_run(run.id)
    assert refreshed is not None
    assert refreshed.status is ProductionRunStatus.READY
    assert {artifact.stage for artifact in refreshed_artifacts} == set(by_stage)


@pytest.mark.asyncio
async def test_ioc_only_repair_reuses_synthesis_and_rebuilds_publication(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = await _configured_scenario(production_scenario_factory)
    _install_canonical_synthesis(scenario)
    await scenario.start()
    run = await scenario.run_until_terminal()
    assert run.status is ProductionRunStatus.READY
    calls_before_repair = len(scenario.model.provider_calls)
    async with scenario.uow_factory() as uow:
        extraction_a = await uow.production_artifacts.get_current(run.id, "extraction")
        synthesis_a = await uow.production_artifacts.get_current(run.id, "synthesis")
        publication_a = await uow.production_artifacts.get_current(run.id, "publication")
    assert extraction_a is not None and extraction_a.canonical_blob_id is not None
    assert synthesis_a is not None
    assert publication_a is not None and publication_a.canonical_blob_id is not None
    document_a = parse_publication_document(
        await scenario.artifact_store.read_json(publication_a.canonical_blob_id)
    )
    extraction = production_extraction_from_json(
        await scenario.artifact_store.read_json(extraction_a.canonical_blob_id)
    )
    source = extraction.sources[0]
    added_ioc = ExtractionIndicatorV1(
        value="new-c2.security-lab.io",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="",
        evidence_quote="The source names the new command-and-control domain.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source.source_document_id,),
    )
    extraction_b = replace(
        extraction,
        sources=(
            replace(source, indicators=(*source.indicators, added_ioc)),
            *extraction.sources[1:],
        ),
    )
    _, blob_id, _ = await scenario.artifact_store.store_stage_payloads(
        canonical=production_extraction_to_json(extraction_b)
    )
    assert blob_id is not None
    repaired_extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=extraction_a.version + 1,
        input_hash=canonical_extraction_hash(extraction_b),
        canonical_blob_id=blob_id,
    )
    repair = ProductionRepairMaterializationService(
        scenario.uow_factory,
        artifact_store=scenario.artifact_store,
    )
    async with scenario.uow_factory() as uow:
        await uow.production_artifacts.append(repaired_extraction)
        publication_b, qa = await repair._materialize_canonical_publication_in_uow(
            uow,
            run=run,
            extraction=repaired_extraction,
            repair_materialization=None,
        )
        await uow.commit()
    assert qa["passed"] is True
    assert publication_b.id != publication_a.id
    assert publication_b.input_hash != publication_a.input_hash
    assert publication_b.canonical_blob_id is not None
    document_b = parse_publication_document(
        await scenario.artifact_store.read_json(publication_b.canonical_blob_id)
    )
    assert (document_b.title, document_b.lead, document_b.sections, document_b.timeline) == (
        document_a.title,
        document_a.lead,
        document_a.sections,
        document_a.timeline,
    )
    assert {
        item.normalized_value for group in document_b.indicators for item in group.indicators
    } == {"core-c2.security-lab.io", "new-c2.security-lab.io"}
    async with scenario.uow_factory() as uow:
        synthesis_b = await uow.production_artifacts.get_current(run.id, "synthesis")
    assert synthesis_b is not None
    assert synthesis_b.id != synthesis_a.id
    assert synthesis_b.reused_from_artifact_id == synthesis_a.id
    assert len(scenario.model.provider_calls) == calls_before_repair


@pytest.mark.asyncio
async def test_invalid_q2_response_cannot_reach_ready(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = production_scenario_factory(_sources())
    scenario.model.script.references(Q1_RESPONSE)
    for source_url in SOURCE_URLS:
        scenario.model.script.q2(
            source_url=source_url,
            response="not a Q2 response",
        )

    await scenario.start()
    run = await scenario.run_until_terminal()

    assert run.status is not ProductionRunStatus.READY
    assert run.status is ProductionRunStatus.NEEDS_REVIEW
    assert run.current_stage is ProductionStage.EXTRACTION
    assert run.error_code == "extraction_core_source_failed"
    assert run.error_details is not None
    assert run.error_details["source_failure_code"] == "extraction_source_output_invalid"
    assert run.reconciliation is None
    assert scenario.model.calls[-1].stage == "extraction"
    assert not any(
        call.prompt_template_id == "production-synthesis" for call in scenario.model.provider_calls
    )

    async with scenario.uow_factory() as uow:
        extraction = await uow.production_artifacts.get_current(run.id, "extraction")
        publication = await uow.production_artifacts.get_current(run.id, "publication")
        persisted_collections = await uow.source_collections.list_for_subject(run.subject_id)
    assert extraction is None
    assert publication is None
    assert all(collection.state is CollectionState.ARCHIVED for collection in persisted_collections)
    assert (
        ProductionRecoveryPolicyV1.disposition_for_run(run)
        is ProductionRecoveryPolicyV1.MANUAL_ONLY
    )
