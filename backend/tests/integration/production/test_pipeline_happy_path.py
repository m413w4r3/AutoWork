"""Business-level coverage of Sources -> References -> Q2 -> Synthesis -> READY."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping

import pytest

from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.domain.collection import CollectionState
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionStage,
    SubjectProductionStatus,
)

from .support import ProductionScenario

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

## SOURCE S2

title: ExampleRAT secondary analysis
url: https://example.test/secondary
publisher: Secondary Labs
published-at: 2026-08-11
role: independent

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

Q2_SECONDARY_RESPONSE = """FACT infection_chain
- Script launcher executes ExampleRAT :: The report describes the execution chain.

IOC contextual domain
- secondary-c2.security-lab.io :: A related infrastructure domain is discussed.
"""

Q4_RESPONSE = (
    "ExampleRAT is launched through a script and reaches core-c2.security-lab.io [S1]. "
    "The secondary analysis corroborates secondary-c2.security-lab.io and the execution chain [S2]."
)


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
    scenario.model.script.q2(
        source_url=SOURCE_URLS[0], access_mode="live_url", response=Q2_CORE_RESPONSE
    )
    scenario.model.script.q2(
        source_url=SOURCE_URLS[1], access_mode="live_url", response=Q2_SECONDARY_RESPONSE
    )
    scenario.model.script.synthesis(Q4_RESPONSE)
    return scenario


@pytest.mark.asyncio
async def test_complete_production_pipeline_reaches_ready(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = await _configured_scenario(production_scenario_factory)
    initial = await scenario.start()
    assert initial.status is SubjectProductionStatus.RUNNING
    assert initial.current_stage is SubjectProductionStage.SOURCES

    run = await scenario.run_until_terminal()

    assert run.status is SubjectProductionStatus.READY
    assert run.current_stage is SubjectProductionStage.ASSEMBLY
    assert run.reconciliation is None
    assert run.error_code is None
    assert run.extraction_progress is not None
    completed_source_ids = {
        source["source_id"]
        for source in run.extraction_progress["sources"]
        if source["status"] == "succeeded"
    }
    assert completed_source_ids == {"S1", "S2"}
    assert run.extraction_progress["skipped_sources"] == 0
    assert (
        ProductionRecoveryPolicyV1.disposition_for_run(run)
        is ProductionRecoveryPolicyV1.MANUAL_ONLY
    )

    async with scenario.uow_factory() as uow:
        persisted_run = await uow.subject_production_runs.get(run.id)
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        collections = list(await uow.source_collections.list_for_subject(run.subject_id))
        documents = list(await uow.source_documents.list_for_subject(run.subject_id))
        attempts = [
            attempt
            for collection in collections
            for attempt in await uow.collection_attempts.list_for_collection(collection.id)
        ]
        artifacts = list(await uow.production_artifacts.list_for_run(run.id))
        turns = []
        for conversation_id in (
            persisted_run.references_conversation_id if persisted_run else None,
            persisted_run.synthesis_conversation_id if persisted_run else None,
        ):
            if conversation_id is not None:
                turns.extend(
                    await uow.model_conversation_turns.list_for_conversation(conversation_id)
                )

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
        ProductionArtifactStage.PUBLICATION,
    }
    assert all(artifact.status is ProductionArtifactStatus.VERIFIED for artifact in artifacts)
    assert [by_stage[stage].version for stage in by_stage] == [1, 1, 1, 1]
    assert all(len(artifact.input_hash) == 64 for artifact in artifacts)
    assert by_stage[ProductionArtifactStage.REFERENCES].metadata["warnings"] == []
    assert by_stage[ProductionArtifactStage.EXTRACTION].metadata["warnings"] == []
    assert (
        by_stage[ProductionArtifactStage.SYNTHESIS].metadata["diagnostics"][
            "unknown_citation_count"
        ]
        == 0
    )
    verification = by_stage[ProductionArtifactStage.EXTRACTION].metadata[
        "deterministic_verification"
    ]
    assert set(verification["completed_source_ids"]) == {"S1", "S2"}
    assert verification["failed_source_ids"] == []
    assert verification["skipped_source_ids"] == []

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
    assert model_calls[0].stage == "references"
    assert model_calls[-1].stage == "synthesis"
    assert all(call.stage == "extraction" for call in model_calls[1:-1])
    covered_q2_urls = tuple(url for call in q2_calls for url in call.source_urls)
    assert set(covered_q2_urls) == set(SOURCE_URLS)
    assert covered_q2_urls == SOURCE_URLS
    assert len(model_calls) == 2 + len(q2_calls)
    assert all(call.web_search for call in model_calls)
    assert model_calls[0].conversation_id is not None
    assert model_calls[-1].conversation_id is not None
    assert all(call.conversation_id is None for call in q2_calls)
    assert all(call.prompt_version for call in model_calls)
    assert all(call.model_run_id is not None for call in model_calls)

    async with scenario.uow_factory() as uow:
        model_runs = {
            call.model_run_id: await uow.model_runs.get(call.model_run_id)
            for call in model_calls
            if call.model_run_id is not None
        }
        references_payload = await scenario.artifact_store.read_json(
            by_stage[ProductionArtifactStage.REFERENCES].canonical_blob_id  # type: ignore[arg-type]
        )
        extraction_payload = await scenario.artifact_store.read_json(
            by_stage[ProductionArtifactStage.EXTRACTION].canonical_blob_id  # type: ignore[arg-type]
        )
        synthesis_text = await scenario.artifact_store.read_text(
            by_stage[ProductionArtifactStage.SYNTHESIS].rendered_blob_id  # type: ignore[arg-type]
        )
        publication_payload = await scenario.artifact_store.read_json(
            by_stage[ProductionArtifactStage.PUBLICATION].canonical_blob_id  # type: ignore[arg-type]
        )

    assert all(model_run is not None for model_run in model_runs.values())
    assert all(model_run.status is ModelRunStatus.SUCCEEDED for model_run in model_runs.values())
    assert all(model_run.raw_output_sha256 for model_run in model_runs.values())
    assert len(references_payload["sources"]) == 2
    reference_source_ids = {source["id"] for source in references_payload["sources"]}
    assert {source["canonical_url"] for source in references_payload["sources"]} == set(SOURCE_URLS)
    extraction_items = extraction_payload["items"]
    assert any(item["value"] == "core-c2.security-lab.io" for item in extraction_items), (
        extraction_items
    )
    assert any(item["value"] == "secondary-c2.security-lab.io" for item in extraction_items), (
        extraction_items
    )
    assert any(
        item["value"] == "secondary-c2.security-lab.io" and item["indicator_status"] == "contextual"
        for item in extraction_items
    )
    q2_model_run_ids = {str(call.model_run_id) for call in q2_calls}
    assert {
        model_run_id for item in extraction_items for model_run_id in item["model_run_ids"]
    } == q2_model_run_ids
    assert "core-c2.security-lab.io" in synthesis_text
    assert "secondary-c2.security-lab.io" in synthesis_text
    assert "core-c2.security-lab.io" in str(publication_payload)
    assert "secondary-c2.security-lab.io" in str(publication_payload)
    assert {
        source["source_id"] for source in publication_payload["sources"]
    } == reference_source_ids
    extraction_indicator_values = {
        item["value"]
        for item in extraction_items
        if item["artifact_type"] is not None
        and item["indicator_status"] == "confirmed_ioc"
        and item["display_policy"] in {"ioc_section", "both"}
    }
    publication_indicator_values = {
        value["value"] for group in publication_payload["indicators"] for value in group["values"]
    }
    assert publication_indicator_values == extraction_indicator_values
    assert all(source_id in model_calls[-1].request.text for source_id in reference_source_ids)
    assert all(value in model_calls[-1].request.text for value in extraction_indicator_values)
    assert all(turn.status.value == "succeeded" for turn in turns)

    async with scenario.uow_factory() as uow:
        refreshed = await uow.subject_production_runs.get(run.id)
        refreshed_artifacts = await uow.production_artifacts.list_for_run(run.id)
    assert refreshed is not None
    assert refreshed.status is SubjectProductionStatus.READY
    assert {artifact.stage for artifact in refreshed_artifacts} == set(by_stage)


@pytest.mark.asyncio
async def test_invalid_q2_response_cannot_reach_ready(
    production_scenario_factory: Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario],
) -> None:
    scenario = production_scenario_factory(_sources())
    scenario.model.script.references(Q1_RESPONSE)
    for source_url in SOURCE_URLS:
        scenario.model.script.q2(
            source_url=source_url,
            access_mode="live_url",
            response="not a Q2 response",
        )

    await scenario.start()
    run = await scenario.run_until_terminal()

    assert run.status is not SubjectProductionStatus.READY
    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert run.current_stage is SubjectProductionStage.EXTRACTION
    assert run.error_code == "q2_source_coverage_failed"
    assert run.reconciliation is None
    assert scenario.model.calls[-1].stage == "extraction"
    assert not any(call.stage == "synthesis" for call in scenario.model.calls)

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
