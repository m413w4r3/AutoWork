from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import (
    ScriptedTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.investigation import (
    BudgetDimension,
    FindingAssessment,
    FindingKind,
    FrameworkPreSubmissionProof,
    NoveltyClass,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.investigation_runtime_ports import build_investigation_runtime_ports
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter
from tests.workflow_emulator.investigation_support import (
    DEFAULT_IOC as _IOC,
)
from tests.workflow_emulator.investigation_support import (
    DEFAULT_SEED_HASH as _SEED_HASH,
)
from tests.workflow_emulator.investigation_support import (
    FakeShodanPort,
)
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal as _HashCheckedVirusTotal,
)
from tests.workflow_emulator.investigation_support import (
    ScriptedInvestigationTransport as _VerticalScriptedTransport,
)
from tests.workflow_emulator.investigation_support import (
    SyntheticSampleMaterial as _SyntheticSampleMaterial,
)
from tests.workflow_emulator.investigation_support import (
    approve_envelope_http as _approve_envelope_http,
)
from tests.workflow_emulator.investigation_support import (
    build_investigation_app as _build_app,
)
from tests.workflow_emulator.investigation_support import (
    composition_factory as _composition_factory,
)
from tests.workflow_emulator.investigation_support import (
    decision as _decision,
)
from tests.workflow_emulator.investigation_support import (
    investigation_settings as _settings,
)
from tests.workflow_emulator.investigation_support import (
    read_composition as _read_composition,
)
from tests.workflow_emulator.investigation_support import (
    read_manifest as _read_manifest,
)
from tests.workflow_emulator.investigation_support import (
    run_http_job as _run_http_job,
)
from tests.workflow_emulator.investigation_support import (
    seed_verified_article as _seed_verified_article,
)
from tests.workflow_emulator.investigation_support import (
    start_investigation_http as _start_http,
)
from tests.workflow_emulator.investigation_support import (
    vertical_final_findings as _vertical_final_findings,
)

pytestmark = pytest.mark.integration


class _PartialDownloadFailureStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial-synthetic-sample"
        raise httpx.ReadError("synthetic connection ended after partial bytes")

    async def aclose(self) -> None:
        return None


def _unique_sample_material() -> _SyntheticSampleMaterial:
    """Keep hash-indexed PostgreSQL state isolated between integration cases."""
    generated = _SyntheticSampleMaterial.unique()
    return _SyntheticSampleMaterial(
        seed_hash=_SEED_HASH,
        sample_bytes=generated.sample_bytes,
        sample_hash=generated.sample_hash,
        ip=_IOC,
    )


async def _associate_vt_file_source(uow_factory, investigation_id: UUID, sample_hash: str) -> UUID:
    association_id = uuid4()
    async with uow_factory() as uow:
        await uow.sample_associations.append_version(
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=sample_hash,
                origin=SampleOrigin.MANUAL,
                id=association_id,
            ),
            expected_version=0,
        )
        await uow.commit()
    return association_id


def _real_vt_runtime_ports(
    *,
    database_url: str,
    uow_factory,
    blob_store: FilesystemBlobStore,
    blob_catalog: BlobCatalogService,
    settings,
    virustotal: VirusTotalHttpAdapter,
    capabilities: VirusTotalCapabilities,
):
    engine = create_postgres_engine(database_url)
    runtime = build_investigation_runtime_ports(
        settings,
        uow_factory=uow_factory,
        session_factory=create_session_factory(engine),
        blob_store=blob_store,
        blob_catalog=blob_catalog,
        vt_adapter=virustotal,
        vt_capabilities=capabilities,
        vt_observations=VirusTotalObservationService(blob_catalog, uow_factory),
        shodan_adapter=None,
        ghidra_adapter=None,
    )
    return engine, runtime.optional_tool_ports


@pytest.mark.asyncio
async def test_real_postgres_http_bridge_vertical_investigation_and_v6_publication(
    uow_factory, tmp_path: Path
) -> None:
    blob_catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / "vertical-blob-storage"), uow_factory
    )
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    assert seeded.seed_observation is not None
    material = _unique_sample_material()
    candidate_hash = material.sample_hash
    vt = _HashCheckedVirusTotal(material=material, include_candidate=True)
    shodan = FakeShodanPort()

    async def final_response_builder(observations):
        async with uow_factory() as uow:
            findings = tuple(
                await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
            )
        supported_findings = tuple(
            item
            for item in findings
            if item.assessment is FindingAssessment.SUPPORTED and item.observation_refs
        )
        if not supported_findings:
            response = await _vertical_final_findings(observations, material=vt.material)
            response["next_action"] = "continue"
            return response

        evidence_refs = tuple(
            sorted(
                {ref for item in supported_findings for ref in item.observation_refs},
                key=lambda ref: ref.hex,
            )
        )
        response = _decision(
            "Draft a cited opinion block from the persisted supported findings.",
            next_action="finish",
        )
        response["opinion_delta"] = {
            "blocks": [
                {
                    "block_type": OpinionBlockType.KEY_FINDINGS.value,
                    "text": (
                        "The bounded VT search returned an article-relative candidate, supported "
                        "by the persisted sample observation."
                    ),
                    "finding_refs": [str(item.id) for item in supported_findings],
                    "evidence_refs": [str(item) for item in evidence_refs],
                    "qualification": OpinionClaimQualification.FACT.value,
                    "certainty": 0.9,
                }
            ]
        }
        return response

    transport = _VerticalScriptedTransport(
        [
            _decision(
                "Read the seed report first.",
                tool_call={
                    "tool_name": "vt.file_report",
                    "schema_version": "1",
                    "arguments": {"file_hash": _SEED_HASH},
                    "purpose": "Review the documented article seed.",
                },
            ),
            _decision(
                "Use VT intelligence search to find a candidate.",
                tool_call={
                    "tool_name": "vt.intelligence_search",
                    "schema_version": "1",
                    "arguments": {
                        "query": "type:peexe",
                        "justification": "Find related file candidates for the documented seed.",
                        "evidence_refs": [str(seeded.seed_observation.id)],
                    },
                    "purpose": "Pivot from the cited seed into VT Intelligence Search.",
                },
            ),
            _decision(
                "Acquire the article-absent S2 candidate under the approved envelope.",
                tool_call={
                    "tool_name": "vt.download_sample",
                    "schema_version": "1",
                    "arguments": {
                        "sha256": candidate_hash,
                        "purpose": "Inspect S2 returned by the cited VT pivot.",
                        "evidence_refs": [str(seeded.seed_observation.id)],
                    },
                    "purpose": (
                        "Download the candidate into quarantine without G3 membership approval."
                    ),
                },
            ),
            _decision(
                "Check the article IOC against Shodan's passive index.",
                tool_call={
                    "tool_name": "shodan.host",
                    "schema_version": "1",
                    "arguments": {"ip": _IOC},
                    "purpose": "Check the article IOC in Shodan's passive host index.",
                },
            ),
        ],
        uow_factory=uow_factory,
        investigation_id=seeded.investigation.id,
        material=vt.material,
        final_response_builder=final_response_builder,
    )
    settings = _settings(providers=True, local_analysis=True)
    composition_factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=transport,
        virustotal=vt,
        shodan=shodan,
        include_local_analysis=True,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=composition_factory,
    )
    before_composition = await _read_composition(uow_factory, seeded.edition.id)
    before_manifest = await _read_manifest(uow_factory, seeded.edition.id)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await _start_http(client, seeded)
        investigation_id = seeded.investigation.id
        envelope = await client.post(
            f"/api/investigations/{investigation_id}/envelope/approve",
            json={
                "allowed_providers": ["vt", "shodan"],
                "budget_limits": {
                    "bridge_turns": 12,
                    "vt_units": 24,
                    "shodan_credits": 4,
                    "downloads": 1,
                },
                "acquire_hits_allowed": True,
                "max_acquire_volume": 1,
                "disclosure": {
                    "tlp": "clear",
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                },
                "reason": "Approve the bounded synthetic VT and Shodan investigation.",
                "correlation_id": f"g2:{investigation_id}",
            },
            headers={"Idempotency-Key": f"g2-approve-{investigation_id}"},
        )
        assert envelope.status_code == 200, envelope.text
        assert envelope.json()["approved"] is True
        assert envelope.json()["allowed_providers"] == ["vt", "shodan"]
        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

        state_view = await client.get(f"/api/investigations/{investigation_id}")
        assert state_view.status_code == 200, state_view.text
        state_payload = state_view.json()
        assert state_payload["status"] == "completed", (
            f"status={state_payload['status']} stop_reason={state_payload['stop_reason']} "
            f"findings={len(state_payload['findings'])} "
            f"opinion_draft={state_payload['opinion_draft'] is not None} "
            f"bridge_submissions={transport.submission_count}"
        )
        assert state_payload["stop_reason"] == "agent_finished"
        assert state_payload["opinion_draft"] is not None
        sample_list = await client.get(f"/api/investigations/{investigation_id}/samples")
        assert sample_list.status_code == 200, sample_list.text
        sample_views = sample_list.json()
        assert {item["sample_sha256"] for item in sample_views} == {candidate_hash}
        sample_view = next(item for item in sample_views if item["sample_sha256"] == candidate_hash)
        assert sample_view["sample_sha256"] == candidate_hash
        assert sample_view["qualification"] == "candidate"

        async with uow_factory() as uow:
            association = await uow.sample_associations.get(UUID(sample_view["id"]))
            assert association is not None
            assert association.qualification is SampleQualification.CANDIDATE
            attempts = await uow.sample_acquisition_attempts.find_successful(
                investigation_id, candidate_hash
            )
            assert attempts is not None and attempts.sample_id is not None
            acquired = await uow.samples.get(attempts.sample_id)
            assert acquired is not None
            assert acquired.state.value == "review_candidate"
            quarantine_blob = await uow.blobs.get(acquired.blob_id)
            assert quarantine_blob is not None
            assert quarantine_blob.descriptor.sha256 == candidate_hash
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
            findings = await uow.investigation_findings.list_for_investigation(investigation_id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            invocations = await uow.tool_invocations.list_for_investigation(investigation_id)
            candidate_finding = next(
                item for item in findings if item.kind is FindingKind.NEW_SAMPLE
            )
            claim_finding = next(
                item for item in findings if item.kind is FindingKind.CLAIM_VERIFICATION
            )
            assert candidate_finding.novelty is NoveltyClass.NEW_TO_ARTICLE
            assert any("absence mondiale" in item for item in candidate_finding.limitations)
            assert claim_finding.assessment is FindingAssessment.REFUTED
            assert claim_finding.claim_refs == ("L1.1",)
            assert any(
                item.source.casefold() == "shodan" and item.object_id == _IOC
                for item in observations
            )
            assert {item.tool_name for item in invocations} >= {
                "vt.file_report",
                "vt.intelligence_search",
                "vt.download_sample",
                "sample.analyze",
                "shodan.host",
            }
            # The article seed is canonical hash evidence without a sample association or
            # quarantined bytes; acquired candidates may be partial when optional extractors
            # are not provisioned.
            failed_analysis = tuple(
                item
                for item in invocations
                if item.tool_name == "sample.analyze" and item.status is ToolInvocationStatus.FAILED
            )
            assert len(failed_analysis) <= 1
            assert all(
                item.status is ToolInvocationStatus.SUCCEEDED
                or (
                    item.tool_name == "sample.analyze"
                    and item.status is ToolInvocationStatus.PARTIAL
                )
                or (
                    item.tool_name == "sample.analyze"
                    and item.status is ToolInvocationStatus.FAILED
                    and item.normalized_parameters["association_id"] == str(association.id)
                )
                for item in invocations
            )
            assert any(
                item.decision_type is AnalystDecisionType.PIVOT_APPROVE for item in decisions
            )
        assert await blob_catalog.read(acquired.blob_id, max_bytes=1024) == material.sample_bytes
        assert vt.calls["file_report"] == 1

        assert vt.calls["intelligence_search"] == 1
        assert vt.download_calls == 1
        assert shodan.calls == [("host", (_IOC,))]
        submissions_before_replay = transport.submission_count
        assert submissions_before_replay == 7

        # Re-dispatching the succeeded durable job does not download again or submit
        # another Bridge turn.
        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))
        assert vt.download_calls == 1
        assert transport.submission_count == submissions_before_replay

        qualification = await client.post(
            f"/api/investigations/{investigation_id}/samples/{association.id}/qualify",
            json={
                "expected_version": association.version,
                "qualification": "confirmed",
                "reason": "The analyst reviewed S2 and its cited quarantine evidence.",
                "evidence_refs": [
                    str(item.id) for item in observations if item.object_id == candidate_hash
                ],
                "correlation_id": f"g3:{association.id}",
            },
            headers={"Idempotency-Key": f"g3-confirm-{association.id}"},
        )
        assert qualification.status_code == 200, qualification.text
        assert qualification.json()["association"]["qualification"] == "confirmed"

        composed = await client.post(
            f"/api/investigations/{investigation_id}/opinion/compose",
            json={},
            headers={"Idempotency-Key": f"opinion-compose-{investigation_id}"},
        )
        assert composed.status_code == 200, composed.text
        draft = composed.json()["draft"]
        assert draft["status"] == "draft"
        assert any(
            item["type"] == OpinionBlockType.ADDITIONAL_SAMPLES.value for item in draft["blocks"]
        )
        claim_block = next(
            item
            for item in draft["blocks"]
            if item["type"] == OpinionBlockType.CLAIM_VERIFICATION.value
        )
        assert "contredisent" in claim_block["text"]
        assert claim_block["evidence_refs"]

        approved_opinion = await client.post(
            f"/api/opinions/{draft['id']}/approve",
            json={
                "version": draft["version"],
                "expected_sha256": draft["content_sha256"],
                "reason": "Approve the cited article-relative update.",
            },
            headers={"Idempotency-Key": f"opinion-approve-{draft['id']}-{draft['version']}"},
        )
        assert approved_opinion.status_code == 200, approved_opinion.text
        approved_body = approved_opinion.json()["approved"]
        published = await client.post(
            f"/api/opinions/{draft['id']}/publish",
            json={
                "version": approved_body["version"],
                "expected_base_publication_sha256": draft["provenance"]["base_publication_sha256"],
            },
            headers={
                "Idempotency-Key": f"opinion-publish-{draft['id']}-{approved_body['version']}"
            },
        )
        assert published.status_code == 200, published.text
        publication_payload = published.json()["publication"]
        assert publication_payload["version"] == 2
        assert published.json()["composition_repin_required"] is True

    async with uow_factory() as uow:
        stored_base = await uow.production_artifacts.get(seeded.publication.id)
        stored_new = await uow.production_artifacts.get(UUID(publication_payload["artifact_id"]))
        run_artifacts = await uow.production_artifacts.list_for_run(seeded.run.id)
        stored_association = await uow.sample_associations.get(association.id)
    assert stored_base == seeded.publication
    assert stored_association is not None
    assert stored_association.qualification is SampleQualification.CONFIRMED
    assert stored_new is not None
    assert stored_new.stage is ProductionArtifactStage.PUBLICATION
    assert stored_new.status is ProductionArtifactStatus.VERIFIED
    assert (
        len([item for item in run_artifacts if item.stage is ProductionArtifactStage.PUBLICATION])
        == 2
    )
    assert (
        await artifact_store.read_bytes(seeded.publication.canonical_blob_id)
        == seeded.publication_bytes
    )
    new_bytes = await artifact_store.read_bytes(stored_new.canonical_blob_id)
    parsed = publication_document_from_json(json.loads(new_bytes))
    assert isinstance(parsed, PublicationDocumentV6)
    base_document = publication_document_from_json(json.loads(seeded.publication_bytes))
    assert parsed.base_publication == base_document
    assert await _read_composition(uow_factory, seeded.edition.id) == before_composition
    assert await _read_manifest(uow_factory, seeded.edition.id) == before_manifest


@pytest.mark.asyncio
async def test_postgres_bridge_pre_submission_exhaustion_resumes_with_new_attempt(
    uow_factory, tmp_path: Path
) -> None:
    blob_catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / "pre-submit-blob-storage"), uow_factory
    )
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await _seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
        include_seed=False,
    )
    failure = TurnSubmission(
        TurnSubmissionStatus.UNAVAILABLE,
        detail="composer not ready before submission",
        error_code="bridge_server_error",
        submission_state="pre_submission",
        retryable=True,
    )
    transport = ScriptedTurnTransport(
        [
            failure,
            failure,
            failure,
            _decision(
                "The composer recovered; finish this synthetic investigation.",
                next_action="finish",
            ),
        ]
    )
    settings = _settings(providers=False)
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=transport,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await _start_http(client, seeded)
        investigation_id = seeded.investigation.id
        approved = await _approve_envelope_http(
            client,
            investigation_id,
            providers=(),
            budget_limits={"bridge_turns": 4, "vt_units": 0, "shodan_credits": 0, "downloads": 0},
            allow_acquisition=False,
        )
        assert approved.status_code == 200, approved.text
        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

        stopped = await client.get(f"/api/investigations/{investigation_id}")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "awaiting_review"
        assert stopped.json()["stop_reason"] == "submission_needs_review"
        assert transport.submission_count == 3
        operator_status = await client.get(
            f"/api/investigations/{investigation_id}/operator-status"
        )
        assert operator_status.status_code == 200, operator_status.text
        assert operator_status.json()["last_operation"]["status"] == (
            "pre_submission_retry_exhausted"
        )
        assert operator_status.json()["next_idempotent_action"]["code"] == ("resume_investigation")
        async with uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(investigation_id)
        assert envelope is not None
        assert envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 0

        resumed = await client.post(
            f"/api/investigations/{investigation_id}/resume",
            json={"expected_version": stopped.json()["version"]},
            headers={"Idempotency-Key": f"pre-submit-resume-{investigation_id}"},
        )
        assert resumed.status_code == 200, resumed.text
        requested = await client.post(
            f"/api/investigations/{investigation_id}/requests",
            json={
                "text": "Retry this investigation after the simulated composer recovery.",
                "expected_version": resumed.json()["investigation"]["version"],
            },
            headers={"Idempotency-Key": f"pre-submit-request-{investigation_id}"},
        )
        assert requested.status_code == 200, requested.text
        assert requested.json()["job_id"] == resumed.json()["job_id"]
        await _run_http_job(
            app,
            uow_factory,
            registry,
            dispatcher,
            UUID(requested.json()["job_id"]),
        )

        completed = await client.get(f"/api/investigations/{investigation_id}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"
        assert completed.json()["stop_reason"] == "agent_finished"
        assert transport.submission_count == 4
        assert any(key.endswith(":attempt=3") for key in transport._by_key)
        async with uow_factory() as uow:
            final_envelope = await uow.investigation_envelopes.get(investigation_id)
        assert final_envelope is not None
        assert final_envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1


@pytest.mark.asyncio
async def test_real_postgres_tool_path_classifies_vt_download_route_404_as_needs_review(
    uow_factory, migrated_postgres_url: str, tmp_path: Path
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "download-refusal-blobs")
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    assert seeded.seed_observation is not None
    request_paths: list[str] = []
    material = _unique_sample_material()
    await _associate_vt_file_source(uow_factory, seeded.investigation.id, material.sample_hash)

    def not_found(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        return httpx.Response(404, json={"error": "NotFoundError"}, request=request)

    vt_client = httpx.AsyncClient(transport=httpx.MockTransport(not_found))
    vt = VirusTotalHttpAdapter(
        client=vt_client,
        base_url="https://www.virustotal.com/api/v3",
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
        download_allowed_ports=frozenset({443}),
    )
    settings = _settings(providers=True)
    runtime_engine, runtime_ports = _real_vt_runtime_ports(
        database_url=migrated_postgres_url,
        uow_factory=uow_factory,
        blob_store=blob_store,
        blob_catalog=blob_catalog,
        settings=settings,
        virustotal=vt,
        capabilities=VirusTotalCapabilities(file_download=True),
    )
    transport = _VerticalScriptedTransport(
        [
            _decision(
                "Try one G2-approved synthetic sample download.",
                tool_call={
                    "tool_name": "vt.download_sample",
                    "schema_version": "1",
                    "arguments": {
                        "sha256": material.sample_hash,
                        "purpose": "Test the synthetic download route refusal.",
                        "evidence_refs": [str(seeded.seed_observation.id)],
                    },
                    "purpose": "Acquire the article-absent synthetic sample under G2.",
                },
            ),
            _decision("Finish after the definitive provider refusal.", next_action="finish"),
        ],
        uow_factory=uow_factory,
        investigation_id=seeded.investigation.id,
        material=material,
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=transport,
        virustotal=cast(Any, vt),
        additional_ports=runtime_ports,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = await _start_http(client, seeded)
            investigation_id = seeded.investigation.id
            approved = await client.post(
                f"/api/investigations/{investigation_id}/envelope/approve",
                json={
                    "allowed_providers": ["vt"],
                    "budget_limits": {
                        "bridge_turns": 4,
                        "vt_units": 4,
                        "shodan_credits": 0,
                        "downloads": 1,
                    },
                    "acquire_hits_allowed": True,
                    "max_acquire_volume": 1,
                    "disclosure": {
                        "tlp": "clear",
                        "do_not_submit": False,
                        "external_llm_allowed": True,
                    },
                    "reason": "Approve one bounded synthetic VT download.",
                    "correlation_id": f"g2:{investigation_id}",
                },
                headers={"Idempotency-Key": f"g2-download-refusal-{investigation_id}"},
            )
            assert approved.status_code == 200, approved.text
            await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

        async with uow_factory() as uow:
            invocations = tuple(
                await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
            )
            envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
            observations = await uow.evidence_observations.list_for_investigation(
                seeded.investigation.id
            )
            failed_attempt = await uow.sample_acquisition_attempts.find_latest_error(
                seeded.investigation.id, material.sample_hash
            )
        invocation = next(item for item in invocations if item.tool_name == "vt.download_sample")
        assert request_paths == [
            f"/api/v3/files/{material.sample_hash}/download",
            f"/api/v3/files/{material.sample_hash}/download_url",
        ], (
            "unexpected VT transport path; persisted acquisition "
            f"code={getattr(failed_attempt, 'error_code', None)!r}"
        )
        # The 404 body cannot be attributed to VT or its proxy, so the external
        # outcome is ambiguous and must be reconciled before any retry. Only a
        # framework diagnostic is persisted; the adapter output is not.
        assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
        assert len(invocation.response_refs) == 1
        diagnostic_observation = next(
            item for item in observations if item.id == invocation.response_refs[0]
        )
        diagnostic = await artifact_store.read_json(diagnostic_observation.blob_id)
        assert diagnostic["result"]["outcome"] == "external_effect_unconfirmed"
        assert diagnostic["result"]["adapter_output_archived"] is False
        assert invocation.consumed_budget == {}
        # Its plain ledger code (rather than an opaque refusal-cache marker)
        # keeps this 404 diagnostic and retryable after the route/config changes.
        assert failed_attempt is not None
        assert failed_attempt.error_code == "virustotal_download_404_origin_unknown"
        assert envelope is not None
        # The ambiguous reservation is retained until reconciliation so a retry
        # cannot consume the same budget a second time.
        assert any(item.status.value == "reserved" for item in envelope.budget.reservations)
    finally:
        await vt_client.aclose()
        await runtime_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_vt_units", "consumed_vt_units", "max_hits", "consumed_hits"),
    [(1, 1, 20, 1), (24, 0, 1, 1)],
    ids=["vt-units-exhausted", "hit-count-exhausted"],
)
async def test_real_postgres_exhausted_loop_acquisition_budget_is_not_ambiguous(
    uow_factory,
    tmp_path: Path,
    max_vt_units: int,
    consumed_vt_units: int,
    max_hits: int,
    consumed_hits: int,
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "loop-budget-blobs"), uow_factory)
    )
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    assert seeded.seed_observation is not None
    async with uow_factory() as uow:
        persisted = await uow.analyst_investigations.get(seeded.investigation.id)
        assert persisted is not None
        persisted.budget.max_hits_acquired = max_hits
        persisted.budget.consumed_hits_acquired = consumed_hits
        persisted.budget.max_vt_read_units = max_vt_units
        persisted.budget.consumed_vt_read_units = consumed_vt_units
        persisted.version += 1
        await uow.analyst_investigations.save(persisted)
        await uow.commit()
    async with uow_factory() as uow:
        persisted_after_save = await uow.analyst_investigations.get(seeded.investigation.id)
        assert persisted_after_save is not None
        assert persisted_after_save.budget.max_hits_acquired == max_hits
        assert persisted_after_save.budget.consumed_hits_acquired == consumed_hits
        assert persisted_after_save.budget.max_vt_read_units == max_vt_units
        assert persisted_after_save.budget.consumed_vt_read_units == consumed_vt_units

    request_paths: list[str] = []
    material = _unique_sample_material()
    await _associate_vt_file_source(uow_factory, seeded.investigation.id, material.sample_hash)

    def not_found(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        return httpx.Response(404, json={"error": "NotFoundError"}, request=request)

    vt_client = httpx.AsyncClient(transport=httpx.MockTransport(not_found))
    vt = VirusTotalHttpAdapter(
        client=vt_client,
        base_url="https://www.virustotal.com/api/v3",
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
        download_allowed_ports=frozenset({443}),
    )
    transport = _VerticalScriptedTransport(
        [
            _decision(
                "Request one sample under the approved envelope.",
                tool_call={
                    "tool_name": "vt.download_sample",
                    "schema_version": "1",
                    "arguments": {
                        "sha256": material.sample_hash,
                        "purpose": "Attempt one bounded synthetic sample acquisition.",
                        "evidence_refs": [str(seeded.seed_observation.id)],
                    },
                    "purpose": "Test local acquisition-budget exhaustion under G2.",
                },
            ),
            _decision("Finish after the local budget refusal.", next_action="finish"),
        ],
        uow_factory=uow_factory,
        investigation_id=seeded.investigation.id,
        material=material,
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=_settings(providers=True),
        transport=transport,
        virustotal=cast(Any, vt),
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = await _start_http(client, seeded)
            investigation_id = seeded.investigation.id
            assert UUID(started["investigation"]["id"]) == investigation_id
            async with uow_factory() as uow:
                persisted_before_job = await uow.analyst_investigations.get(investigation_id)
            assert persisted_before_job is not None
            assert persisted_before_job.budget.max_hits_acquired == max_hits
            assert persisted_before_job.budget.consumed_hits_acquired == consumed_hits
            assert persisted_before_job.budget.max_vt_read_units == max_vt_units
            assert persisted_before_job.budget.consumed_vt_read_units == consumed_vt_units
            approved = await client.post(
                f"/api/investigations/{investigation_id}/envelope/approve",
                json={
                    "allowed_providers": ["vt"],
                    "budget_limits": {
                        "bridge_turns": 4,
                        "vt_units": 4,
                        "shodan_credits": 0,
                        "downloads": 1,
                    },
                    "acquire_hits_allowed": True,
                    "max_acquire_volume": 1,
                    "disclosure": {
                        "tlp": "clear",
                        "do_not_submit": False,
                        "external_llm_allowed": True,
                    },
                    "reason": "Approve one bounded synthetic VT download.",
                    "correlation_id": f"g2:{investigation_id}",
                },
                headers={"Idempotency-Key": f"g2-loop-budget-{investigation_id}"},
            )
            assert approved.status_code == 200, approved.text
            await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

        async with uow_factory() as uow:
            invocations = tuple(
                await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
            )
            observations = await uow.evidence_observations.list_for_investigation(
                seeded.investigation.id
            )
            envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
            persisted = await uow.analyst_investigations.get(seeded.investigation.id)
        invocation = next(item for item in invocations if item.tool_name == "vt.download_sample")
        assert request_paths == [], (
            f"provider request ran with persisted VT budget="
            f"{persisted.budget.max_vt_read_units if persisted is not None else None}/"
            f"{persisted.budget.consumed_vt_read_units if persisted is not None else None}"
        )
        # The adapter's deterministic local refusal is not framework-attested:
        # the dispatcher cannot prove adapter non-entry, so the invocation is
        # conservatively reconciled as ambiguous and records only the framework
        # diagnostic, never the adapter output.
        assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
        assert len(invocation.response_refs) == 1
        diagnostic = next(item for item in observations if item.id == invocation.response_refs[0])
        payload = await artifact_store.read_json(diagnostic.blob_id)
        assert payload["result"]["outcome"] == "external_effect_unconfirmed"
        assert payload["result"]["adapter_output_archived"] is False
        assert not any(
            item.object_type == FrameworkPreSubmissionProof.BUDGET_REJECTED.value
            for item in observations
        )
        assert all(amount == 0 for amount in invocation.consumed_budget.values())
        assert envelope is not None
        # The ambiguous reservation is held until reconciliation even though the
        # adapter made no provider call; releasing it early would allow a double
        # spend if the adapter's local refusal were later contradicted.
        assert any(item.status.value == "reserved" for item in envelope.budget.reservations)
        assert persisted is not None
        assert persisted.budget.consumed_vt_read_units == consumed_vt_units
        assert persisted.budget.consumed_hits_acquired == consumed_hits
    finally:
        await vt_client.aclose()


@pytest.mark.asyncio
async def test_real_postgres_ambiguous_download_logs_safe_terminal_diagnostics(
    uow_factory,
    migrated_postgres_url: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blob_store = FilesystemBlobStore(tmp_path / "partial-download-blobs")
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    assert seeded.seed_observation is not None
    material = _unique_sample_material()
    await _associate_vt_file_source(uow_factory, seeded.investigation.id, material.sample_hash)

    def partial_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_PartialDownloadFailureStream(), request=request)

    vt_client = httpx.AsyncClient(transport=httpx.MockTransport(partial_response))
    vt = VirusTotalHttpAdapter(
        client=vt_client,
        base_url="https://www.virustotal.com/api/v3",
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
        download_allowed_ports=frozenset({443}),
    )
    settings = _settings(providers=True)
    runtime_engine, runtime_ports = _real_vt_runtime_ports(
        database_url=migrated_postgres_url,
        uow_factory=uow_factory,
        blob_store=blob_store,
        blob_catalog=blob_catalog,
        settings=settings,
        virustotal=vt,
        capabilities=VirusTotalCapabilities(file_download=True),
    )
    transport = _VerticalScriptedTransport(
        [
            _decision(
                "Request a synthetic sample download that will fail after partial bytes.",
                tool_call={
                    "tool_name": "vt.download_sample",
                    "schema_version": "1",
                    "arguments": {
                        "sha256": material.sample_hash,
                        "purpose": "Exercise ambiguous partial-stream reconciliation.",
                        "evidence_refs": [str(seeded.seed_observation.id)],
                    },
                    "purpose": "Test the ambiguous sample path through the real tool stack.",
                },
            )
        ],
        uow_factory=uow_factory,
        investigation_id=seeded.investigation.id,
        material=material,
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=transport,
        virustotal=cast(Any, vt),
        additional_ports=runtime_ports,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    caplog.set_level("WARNING", logger="cti_app.application.investigation_service")

    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            started = await _start_http(client, seeded)
            investigation_id = seeded.investigation.id
            approved = await client.post(
                f"/api/investigations/{investigation_id}/envelope/approve",
                json={
                    "allowed_providers": ["vt"],
                    "budget_limits": {
                        "bridge_turns": 4,
                        "vt_units": 4,
                        "shodan_credits": 0,
                        "downloads": 1,
                    },
                    "acquire_hits_allowed": True,
                    "max_acquire_volume": 1,
                    "disclosure": {
                        "tlp": "clear",
                        "do_not_submit": False,
                        "external_llm_allowed": True,
                    },
                    "reason": "Approve one bounded synthetic VT download.",
                    "correlation_id": f"g2:{investigation_id}",
                },
                headers={"Idempotency-Key": f"g2-partial-download-{investigation_id}"},
            )
            assert approved.status_code == 200, approved.text
            await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

        async with uow_factory() as uow:
            invocations = tuple(
                await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
            )
            envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
        invocation = next(item for item in invocations if item.tool_name == "vt.download_sample")
        assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
        assert invocation.consumed_budget == {}
        assert envelope is not None
        assert any(item.status.value == "reserved" for item in envelope.budget.reservations)

        warning = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "investigation_tool_invocation_terminal_failure"
        )
        assert warning.levelname == "WARNING"
        assert warning.tool_name == "vt.download_sample"
        assert warning.exception_class == "SampleAcquisitionDownloadError"
        assert warning.error_code == "virustotal_external_effect_needs_review"
        assert warning.investigation_id == str(seeded.investigation.id)
        assert warning.invocation_id == str(invocation.id)
        assert material.sample_hash not in warning.getMessage()
    finally:
        await vt_client.aclose()
        await runtime_engine.dispose()


@pytest.mark.asyncio
async def test_provider_absence_still_composes_a_local_opinion_postgres(
    uow_factory, tmp_path: Path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "local-only-blobs"), uow_factory)
    )
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    transport = ScriptedTurnTransport(
        [
            _decision(
                "Use the local documented seed evidence.",
                next_action="finish",
                finding_proposals=[
                    {
                        "kind": FindingKind.CAPABILITY.value,
                        "assessment": FindingAssessment.SUPPORTED.value,
                        "statement": "The local fixture records a bounded seed observation.",
                        "observation_refs": [str(seeded.seed_observation.id)],
                        "claim_refs": [],
                        "novelty": None,
                        "scope": "Synthetic evidence local to this investigation.",
                        "limitations": ["Synthetic fixture only."],
                    }
                ],
            )
        ]
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=_settings(providers=False),
        transport=transport,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await _start_http(client, seeded)
        approved = await client.post(
            f"/api/investigations/{seeded.investigation.id}/envelope/approve",
            json={
                "allowed_providers": [],
                "budget_limits": {"bridge_turns": 12},
                "acquire_hits_allowed": False,
                "max_acquire_volume": 0,
                "disclosure": {
                    "tlp": "clear",
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                },
                "reason": "Approve a local-only integration investigation.",
                "correlation_id": f"local:{seeded.investigation.id}",
            },
            headers={"Idempotency-Key": f"local-approve-{seeded.investigation.id}"},
        )
        assert approved.status_code == 200, approved.text
        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))
        composed = await client.post(
            f"/api/investigations/{seeded.investigation.id}/opinion/compose",
            json={},
            headers={"Idempotency-Key": f"local-compose-{seeded.investigation.id}"},
        )
        assert composed.status_code == 200, composed.text
        assert composed.json()["draft"]["blocks"]
    async with uow_factory() as uow:
        invocations = await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
        findings = await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
    assert not any(item.tool_name.startswith(("vt.", "shodan.")) for item in invocations)
    assert findings and transport.submission_count == 1


@pytest.mark.asyncio
async def test_unapproved_envelope_denies_external_tool_and_loop_continues_postgres(
    uow_factory, tmp_path: Path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "unapproved-blobs"), uow_factory)
    )
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    vt = _HashCheckedVirusTotal(include_candidate=False)
    transport = ScriptedTurnTransport(
        [
            _decision(
                "Try the configured VT report, then respect the policy outcome.",
                tool_call={
                    "tool_name": "vt.file_report",
                    "schema_version": "1",
                    "arguments": {"file_hash": _SEED_HASH},
                    "purpose": "Request an external report before G2 approval.",
                },
            ),
            _decision(
                "Continue with the persisted local context after policy denial.",
                next_action="finish",
            ),
        ]
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=_settings(providers=True),
        transport=transport,
        virustotal=vt,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await _start_http(client, seeded)
        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))
        state = await client.get(f"/api/investigations/{seeded.investigation.id}")
        assert state.status_code == 200
        # Without an approved envelope the Bridge-led loop cannot continue:
        # external tools and Bridge submissions are denied before transport.
        assert state.json()["status"] == "exhausted"
    async with uow_factory() as uow:
        invocations = await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
        persisted_envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
    external = [item for item in invocations if item.tool_name == "vt.file_report"]
    # The disclosure refusal may happen before a durable invocation is created;
    # any persisted attempt must still be a policy denial.
    assert all(item.status is ToolInvocationStatus.POLICY_DENIED for item in external)
    assert persisted_envelope is None
    assert vt.calls["file_report"] == 0
    assert transport.submission_count == 0
    assert transport.prompts == []


@pytest.mark.parametrize(
    "injection_point",
    ("before_context_save", "after_bridge_submission", "before_tool", "after_tool"),
)
@pytest.mark.asyncio
async def test_operator_request_racing_investigation_turn_is_rebased_without_replay(
    uow_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injection_point: str
) -> None:
    """A concurrent operator request must survive any durable turn boundary."""
    blob_catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / f"request-race-{injection_point}"), uow_factory
    )
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    assert seeded.seed_observation is not None
    await _associate_vt_file_source(
        uow_factory, seeded.investigation.id, seeded.seed_observation.object_id
    )
    vt = _HashCheckedVirusTotal(include_candidate=False)
    transport = _VerticalScriptedTransport(
        [
            _decision(
                "Read the synthetic seed report.",
                tool_call={
                    "tool_name": "vt.file_report",
                    "schema_version": "1",
                    "arguments": {"file_hash": seeded.seed_observation.object_id},
                    "purpose": "Check the documented synthetic seed.",
                },
            ),
            _decision("Finish after the requested evidence check.", next_action="finish"),
        ],
        uow_factory=uow_factory,
        investigation_id=seeded.investigation.id,
        material=vt.material,
        inject_analysis_at_turn=None,
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=_settings(providers=True),
        transport=transport,
        virustotal=vt,
    )
    app, registry, dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await _start_http(client, seeded)
        investigation_id = seeded.investigation.id
        approved = await _approve_envelope_http(
            client,
            investigation_id,
            providers=("vt",),
            budget_limits={
                "bridge_turns": 4,
                "vt_units": 4,
                "shodan_credits": 0,
                "downloads": 0,
            },
            allow_acquisition=False,
        )
        assert approved.status_code == 200, approved.text

        injected = False

        async def inject_request() -> None:
            nonlocal injected
            if injected:
                return
            injected = True
            async with uow_factory() as uow:
                state = await uow.investigation_states.get(investigation_id)
                assert state is not None
                expected_version = state.version
            response = await client.post(
                f"/api/investigations/{investigation_id}/requests",
                json={
                    "expected_version": expected_version,
                    "text": "Check this synthetic provider result before finishing.",
                },
                headers={"Idempotency-Key": f"request-race-{injection_point}"},
            )
            assert response.status_code == 200, response.text

        if injection_point == "before_context_save":
            async with uow_factory() as uow:
                repository_type = type(uow.investigation_states)
            original_save_if_version = repository_type.save_if_version

            async def save_with_request(repository, updated, *, expected_version):
                if (
                    not injected
                    and updated.pending_context_snapshot_id is not None
                    and updated.pending_context_sha256 is not None
                ):
                    await inject_request()
                return await original_save_if_version(
                    repository, updated, expected_version=expected_version
                )

            monkeypatch.setattr(repository_type, "save_if_version", save_with_request)
        elif injection_point == "after_bridge_submission":
            original_submit = transport.submit

            async def submit_then_request(**kwargs):
                result = await original_submit(**kwargs)
                await inject_request()
                return result

            monkeypatch.setattr(transport, "submit", submit_then_request)
        elif injection_point in {"before_tool", "after_tool"}:
            original_file_report = vt.file_report

            async def report_with_request(file_hash: str):
                if injection_point == "before_tool":
                    await inject_request()
                result = await original_file_report(file_hash)
                if injection_point == "after_tool":
                    await inject_request()
                return result

            monkeypatch.setattr(vt, "file_report", report_with_request)

        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))

        async with uow_factory() as uow:
            job = await uow.jobs.get(UUID(started["job_id"]))
            state = await uow.investigation_states.get(investigation_id)
            invocations = await uow.tool_invocations.list_for_investigation(investigation_id)
        assert job is not None and job.status.value == "succeeded"
        assert state is not None and injected
        assert len([item for item in invocations if item.tool_name == "vt.file_report"]) == 1
        assert vt.calls["file_report"] == 1
        assert transport.submission_count == 2
        assert "Check this synthetic provider result before finishing." in transport.prompts[1]


@pytest.mark.asyncio
async def test_unresolved_investigation_state_conflict_schedules_retryable_job(
    uow_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "repeated-state-conflicts"), uow_factory)
    )
    seeded = await _seed_verified_article(uow_factory, artifact_store, suffix=uuid4().hex[:10])
    transport = ScriptedTurnTransport(
        [_decision("Finish this synthetic investigation.", next_action="finish")]
    )
    factory = _composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=_settings(providers=False),
        transport=transport,
    )
    app, registry, _dispatcher = _build_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await _start_http(client, seeded)
        investigation_id = seeded.investigation.id
        approved = await _approve_envelope_http(
            client,
            investigation_id,
            providers=("bridge",),
            budget_limits={"bridge_turns": 1, "tool_seconds": 20_000},
            allow_acquisition=False,
        )
        assert approved.status_code == 200, approved.text
        async with uow_factory() as uow:
            repository_type = type(uow.investigation_states)
        original_save_if_version = repository_type.save_if_version
        conflict_count = 0

        async def repeatedly_conflicting_save(repository, updated, *, expected_version):
            nonlocal conflict_count
            if updated.pending_context_snapshot_id is not None and conflict_count < 4:
                conflict_count += 1
                from cti_app.application.investigation_state import (
                    InvestigationStateVersionConflict,
                )

                raise InvestigationStateVersionConflict("Synthetic persistent CAS conflict")
            return await original_save_if_version(
                repository, updated, expected_version=expected_version
            )

        monkeypatch.setattr(repository_type, "save_if_version", repeatedly_conflicting_save)
        from cti_app.application.jobs import JobExecutor

        await JobExecutor(uow_factory, registry).execute(UUID(started["job_id"]))

    async with uow_factory() as uow:
        job = await uow.jobs.get(UUID(started["job_id"]))
        state = await uow.investigation_states.get(investigation_id)
    assert conflict_count == 4
    assert job is not None and job.status.value == "queued"
    assert job.error_code == "investigation_state_conflict"
    assert job.next_retry_at is not None
    assert state is not None
    assert state.pending_context_snapshot_id is None
    assert transport.submission_count == 0
