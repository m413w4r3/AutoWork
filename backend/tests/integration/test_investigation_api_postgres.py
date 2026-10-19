from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from cti_app.api.analyst_opinion import router as analyst_opinion_router
from cti_app.api.investigation import router
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ScriptedTurnTransport
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_composition import InvestigationCompositionFactory
from cti_app.application.investigation_contracts import parse_agent_turn_decision
from cti_app.application.investigation_jobs import make_investigation_runner
from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    canonical_json_bytes,
)
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")


class _DeferredDispatcher:
    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.job_ids.append(job_id)


@pytest.mark.asyncio
async def test_verified_synthesis_http_job_finding_and_opinion_postgres(
    uow_factory, tmp_path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "investigation-api-blobs"), uow_factory)
    )
    suffix = uuid4().hex
    edition = Edition(
        country=f"Investigation API {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Investigation API integration subject",
        slug=f"investigation-api-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis_payload = {
        "title": "A verified integration synthesis",
        "subject_id": str(subject.id),
        "lead": [{"text": "The fixture records one bounded claim."}],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="investigation-api-synthesis"
    )
    synthesis_bytes = artifact_store.canonical_json_bytes(synthesis_payload)
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        canonical_blob_id=synthesis_blob_id,
    )
    publication_payload = {"fixture": "unchanged verified publication"}
    publication_blob_id, publication_sha256 = await artifact_store.put_canonical_json(
        publication_payload, bucket="investigation-api-publication"
    )
    publication = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        input_hash=publication_sha256,
        canonical_blob_id=publication_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(),
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [],
        "file_indicators": [],
        "policy": {
            "tlp": "AMBER",
            "do_not_submit": False,
            "external_llm_allowed": True,
        },
    }
    input_pack_blob_id, input_pack_sha256 = await artifact_store.put_canonical_json(
        input_pack_payload, bucket="analyst-input-packs"
    )
    investigation.input_pack_blob_id = input_pack_blob_id
    investigation.input_sha256 = input_pack_sha256
    input_pack = AnalystInputPack(
        investigation_id=investigation.id,
        blob_id=input_pack_blob_id,
        sha256=input_pack_sha256,
        schema_version="analyst-input-pack-v1",
    )
    observation_bytes = b"simulated local observation for the integration test"
    observation_blob_id = await artifact_store.put_bytes(
        observation_bytes,
        bucket="investigation-api-observations",
        mime_type="application/octet-stream",
    )
    observation = EvidenceObservation(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=observation_blob_id,
        sha256=hashlib.sha256(observation_bytes).hexdigest(),
        source="simulated-local-tool",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="test_observation",
        object_id=f"observation-{suffix}",
        limitations=("Synthetic integration evidence only.",),
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(synthesis)
        await uow.production_artifacts.append(publication)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()

    proposal = {
        "kind": "capability",
        "assessment": "supported",
        "statement": "The simulated evidence records the bounded capability.",
        "observation_refs": [str(observation.id)],
        "subject_ref": observation.object_id,
        "claim_refs": [],
        "novelty": None,
        "scope": "simulated local observation",
        "limitations": ["Synthetic integration evidence only."],
    }
    parsed = parse_agent_turn_decision(
        {
            "reasoning_summary": "A local observation supports one bounded finding.",
            "goal": "Record the cited finding and draft its opinion block.",
            "finding_proposals": [proposal],
            "next_action": "finish",
        }
    )
    proposal_sha256 = hashlib.sha256(
        canonical_json_bytes(
            parsed.finding_proposals[0].model_dump(mode="json", exclude={"hypothesis_refs"})
        )
    ).hexdigest()
    finding_id = uuid5(_FINDING_NAMESPACE, f"{investigation.id}:{proposal_sha256}")
    turn_transport = ScriptedTurnTransport(
        [
            {
                "reasoning_summary": "A local observation supports one bounded finding.",
                "goal": "Record the cited finding and draft its opinion block.",
                "hypothesis_refs": [],
                "proposed_tool_calls": [],
                "finding_proposals": [proposal],
                "opinion_delta": {
                    "blocks": [
                        {
                            "block_type": OpinionBlockType.KEY_FINDINGS.value,
                            "text": "The simulated observation supports the finding.",
                            "finding_refs": [str(finding_id)],
                            "evidence_refs": [str(observation.id)],
                            "qualification": OpinionClaimQualification.FACT.value,
                            "certainty": 0.9,
                        }
                    ],
                    "unresolved_points": [],
                },
                "next_action": "finish",
            }
        ]
    )
    settings = Settings.model_construct()
    composition_factory = InvestigationCompositionFactory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=settings,
        turn_transport=turn_transport,
    )
    runner = make_investigation_runner(uow_factory, composition_factory)
    registry = create_job_registry(investigation_runner=runner)
    job_service = JobService(uow_factory, registry)
    dispatcher = _DeferredDispatcher()
    app = FastAPI()
    app.include_router(router)
    app.include_router(analyst_opinion_router)
    app.state.uow_factory = uow_factory
    app.state.investigation_composition_factory = composition_factory
    app.state.job_service = job_service
    app.state.job_dispatcher = dispatcher
    app.state.identity_provider = LocalIdentityProvider("analyst:integration")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            f"/api/subjects/{subject.id}/investigations",
            json={
                "production_run_id": str(run.id),
                "synthesis_artifact_id": str(synthesis.id),
                "synthesis_sha256": hashlib.sha256(synthesis_bytes).hexdigest(),
            },
        )
        assert started.status_code == 200, started.text
        started_body = started.json()
        assert started_body["investigation"]["id"] == str(investigation.id)
        assert started_body["investigation"]["envelope_approved"] is False
        assert dispatcher.job_ids == [UUID(started_body["job_id"])]
        approved = await client.post(
            f"/api/investigations/{investigation.id}/envelope/approve",
            json={
                "allowed_providers": [],
                "budget_limits": {"bridge_turns": 12},
                "acquire_hits_allowed": False,
                "max_acquire_volume": 0,
                "disclosure": {
                    "tlp": "amber",
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                },
                "reason": "Approve the bounded synthetic investigation.",
                "correlation_id": f"g2:{investigation.id}",
            },
            headers={"Idempotency-Key": f"g2-approve-{investigation.id}"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["approved"] is True

        await SynchronousJobDispatcher(JobExecutor(uow_factory, registry)).dispatch(
            dispatcher.job_ids[0]
        )
        read = await client.get(f"/api/investigations/{investigation.id}")
        pivots = await client.get(f"/api/investigations/{investigation.id}/pivots")

    assert read.status_code == 200, read.text
    result = read.json()
    assert result["status"] == "completed"
    assert result["findings"][0]["assessment"] == FindingAssessment.SUPPORTED.value
    assert result["findings"][0]["observation_refs"] == [str(observation.id)]
    assert result["opinion_draft"]["status"] == "draft"
    assert result["opinion_draft"]["blocks"][0]["finding_refs"] == [str(finding_id)]
    assert result["opinion_draft"]["blocks"][0]["evidence_refs"] == [str(observation.id)]
    assert turn_transport.submission_count == 1
    assert str(observation_blob_id) not in read.text
    assert pivots.status_code == 200, pivots.text
    assert pivots.json()["schema_version"] == "investigation-pivot-frontier.v1"
    assert pivots.json()["investigation_id"] == str(investigation.id)
    assert pivots.json()["nodes"] == []

    async with uow_factory() as uow:
        persisted_publication = await uow.production_artifacts.get(publication.id)
    assert persisted_publication == publication


@pytest.mark.asyncio
async def test_protocol_model_blob_from_another_investigation_is_not_read_postgres(
    uow_factory, tmp_path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(
            FilesystemBlobStore(tmp_path / "investigation-protocol-guard"), uow_factory
        )
    )
    suffix = uuid4().hex
    edition = Edition(
        country=f"Protocol guard {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subjects = [
        Subject(
            edition_id=edition.id,
            title=f"Protocol guard subject {index}",
            slug=f"protocol-guard-{suffix}-{index}",
            tlp=TLP.AMBER,
        )
        for index in range(2)
    ]
    investigations: list[AnalystInvestigation] = []
    runs: list[ProductionRun] = []
    syntheses: list[ProductionArtifact] = []
    for index, subject in enumerate(subjects):
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        payload = {
            "title": f"Protocol guard synthesis {index} {suffix}",
            "subject_id": str(subject.id),
            "lead": [{"text": "A synthetic verified report."}],
            "sections": [],
            "timeline": [],
            "uncertainties": [],
        }
        blob_id, sha256 = await artifact_store.put_canonical_json(
            payload, bucket=f"protocol-guard-synthesis-{index}"
        )
        synthesis = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash=sha256,
            canonical_blob_id=blob_id,
        )
        runs.append(run)
        syntheses.append(synthesis)
        investigations.append(
            AnalystInvestigation.from_verified_synthesis(
                synthesis=synthesis,
                budget=LoopBudget(),
            )
        )

    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        for subject in subjects:
            await uow.subjects.add(subject)
        for run, synthesis, investigation in zip(runs, syntheses, investigations, strict=True):
            await uow.production_runs.add(run)
            await uow.production_artifacts.append(synthesis)
            await uow.analyst_investigations.add(investigation)
        await uow.commit()

    model_bytes = b"synthetic foreign protocol model blob"
    model_blob_id = await artifact_store.put_bytes(
        model_bytes,
        bucket="network-protocol-models",
        mime_type="application/json",
    )
    model_observation = EvidenceObservation(
        investigation_id=investigations[0].id,
        kind=EvidenceKind.OTHER,
        blob_id=model_blob_id,
        sha256=hashlib.sha256(model_bytes).hexdigest(),
        source="network.protocol.compare",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="protocol_model",
        object_id=hashlib.sha256(model_bytes).hexdigest(),
        limitations=("Synthetic guard fixture.",),
    )
    async with uow_factory() as uow:
        await uow.evidence_observations.append_if_absent(model_observation)
        await uow.commit()

    app = FastAPI()
    app.include_router(router)
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = artifact_store
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/investigations/{investigations[1].id}/protocol-models/{model_blob_id}"
        )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "investigation_artifact_not_found"
