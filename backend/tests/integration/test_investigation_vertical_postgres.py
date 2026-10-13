from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ScriptedTurnTransport
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.analyst_opinion import OpinionBlockType
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.investigation import (
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    publication_document_from_json,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.workflow_emulator.investigation_support import (
    DEFAULT_IOC as _IOC,
)
from tests.workflow_emulator.investigation_support import (
    DEFAULT_SAMPLE_BYTES as _S2_BYTES,
)
from tests.workflow_emulator.investigation_support import (
    DEFAULT_SAMPLE_HASH as _S2_HASH,
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

pytestmark = pytest.mark.integration


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
    vt = _HashCheckedVirusTotal(include_candidate=True)
    shodan = FakeShodanPort()
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
                        "sha256": _S2_HASH,
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
        include_external_pivot=True,
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
        assert state_view.json()["status"] == "completed"
        sample_list = await client.get(f"/api/investigations/{investigation_id}/samples")
        assert sample_list.status_code == 200, sample_list.text
        assert len(sample_list.json()) == 1
        sample_view = sample_list.json()[0]
        assert sample_view["sample_sha256"] == _S2_HASH
        assert sample_view["qualification"] == "candidate"

        async with uow_factory() as uow:
            association = await uow.sample_associations.get(UUID(sample_view["id"]))
            assert association is not None
            assert association.qualification is SampleQualification.CANDIDATE
            attempts = await uow.sample_acquisition_attempts.find_successful(
                investigation_id, _S2_HASH
            )
            assert attempts is not None and attempts.sample_id is not None
            acquired = await uow.samples.get(attempts.sample_id)
            assert acquired is not None
            assert acquired.state.value == "review_candidate"
            quarantine_blob = await uow.blobs.get(acquired.blob_id)
            assert quarantine_blob is not None
            assert quarantine_blob.descriptor.sha256 == _S2_HASH
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
            # Local analysis is PARTIAL when optional extractors (code features, goodware
            # corpus) are not provisioned in the harness; every other tool must succeed.
            assert all(
                item.status is ToolInvocationStatus.SUCCEEDED
                or (
                    item.tool_name == "sample.analyze"
                    and item.status is ToolInvocationStatus.PARTIAL
                )
                for item in invocations
            )
            assert any(
                item.decision_type is AnalystDecisionType.PIVOT_APPROVE for item in decisions
            )
        assert await blob_catalog.read(acquired.blob_id, max_bytes=1024) == _S2_BYTES
        assert vt.calls["file_report"] == 1
        assert vt.calls["intelligence_search"] == 1
        assert vt.download_calls == 1
        assert shodan.calls == [("host", (_IOC,))]
        assert transport.submission_count == 6

        # Re-dispatching the succeeded durable job does not download again or submit
        # another Bridge turn.
        await _run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))
        assert vt.download_calls == 1
        assert transport.submission_count == 6

        qualification = await client.post(
            f"/api/investigations/{investigation_id}/samples/{association.id}/qualify",
            json={
                "expected_version": association.version,
                "qualification": "confirmed",
                "reason": "The analyst reviewed S2 and its cited quarantine evidence.",
                "evidence_refs": [
                    str(item.id) for item in observations if item.object_id == _S2_HASH
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
        assert state.json()["status"] == "completed"
    async with uow_factory() as uow:
        invocations = await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
        persisted_envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
    denied = next(item for item in invocations if item.tool_name == "vt.file_report")
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert persisted_envelope is None
    assert vt.calls["file_report"] == 0
    assert transport.submission_count == 2
    assert "request_permission" in transport.prompts[1]
    assert "article.get_claims" in transport.prompts[1]
