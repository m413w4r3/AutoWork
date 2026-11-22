"""PostgreSQL oracle for the M24-03B initial G2 envelope chain.

The test proves the real persistence chain: proposal derived from the canonical
inventory, one human approval through ``InvestigationEnvelopeService`` (with
its AnalystDecision), then the M24-03A ``execute_batch`` running with the REAL
VT tool adapters and the REAL acquisition quarantine path against a local fake
VT port.  No test contacts a provider.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.analyst_handoff import AnalystPostSynthesisService
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import register_acquisition_tools
from cti_app.application.investigation_article_snapshot import (
    ProductionArticleSnapshotAdapter,
)
from cti_app.application.investigation_bootstrap import (
    BootstrapPlanState,
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
)
from cti_app.application.investigation_bootstrap_envelope import (
    InvestigationBootstrapEnvelopeService,
)
from cti_app.application.investigation_governance import SampleQualificationService
from cti_app.application.investigation_service import (
    InvestigationService,
    ProductionArtifactContextSnapshotRepository,
    ProductionArtifactObservationWriter,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
)
from cti_app.application.investigation_vt_tools import (
    register_virustotal_tools,
    virustotal_tool_capabilities,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ResearchContextBuilder
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.virustotal import VirusTotalCapabilities
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    InvestigationAgenda,
    InvestigationExecutionMode,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.domain.production import (
    AnalystInputPack,
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
)
from cti_app.domain.production_routing import (
    ProductionRoutingDecisionV1,
    RoutingReasonCode,
    RoutingStatus,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalObservation,
    VirusTotalOperation,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.edition_codes import reserve_edition_code
from tests.test_investigation_vt_tools import (
    FakeVirusTotalObservationService,
    FakeVirusTotalPort,
)

pytestmark = pytest.mark.integration

_SAMPLE_BYTES = b"synthetic-bootstrap-envelope-sample"
_SEED_SHA256 = hashlib.sha256(_SAMPLE_BYTES).hexdigest()
_FIXED_NOW = datetime(2026, 11, 22, 11, 0, tzinfo=UTC)


class _HashCheckedVtPort(FakeVirusTotalPort):
    """Local fake VT port; the only external surface is replaced, no network."""

    def __init__(self, sample_bytes: bytes) -> None:
        super().__init__()
        self.sample_bytes = sample_bytes
        self.download_calls: list[str] = []

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        self.download_calls.append(file_hash)
        if hashlib.sha256(self.sample_bytes).hexdigest() != file_hash:
            raise ValueError("fake payload does not match the requested SHA-256")
        sink.write(self.sample_bytes)


class _PersistedVtObservationService(FakeVirusTotalObservationService):
    """Store raw VT bodies in the canonical blob catalog (real FK target)."""

    def __init__(self, artifact_store: ProductionArtifactStore) -> None:
        super().__init__()
        self._artifact_store = artifact_store

    async def store_file_report(
        self,
        report: Any,
        *,
        subject_id: Any | None = None,
        observed_at: Any,
        checkpoint_id: str | None = None,
    ) -> VirusTotalObservation:
        blob_id = await self._artifact_store.put_bytes(
            report.raw_json,
            bucket="virustotal-raw",
            mime_type="application/json",
        )
        observation = VirusTotalObservation(
            operation=VirusTotalOperation.FILE_REPORT,
            capability=VirusTotalCapability.FILE_REPORT,
            source_identifier=report.file.lookup_value,
            safe_parameters={"transport": "proxy", "api_generation": "v3"},
            http_status=report.http_status,
            blob_id=blob_id,
            raw_sha256=hashlib.sha256(report.raw_json).hexdigest(),
            raw_size=len(report.raw_json),
            observed_at=observed_at,
        )
        self.raw_observations.append(observation)
        return observation


def _executor_factory(
    *,
    uow_factory: Any,
    catalog: BlobCatalogService,
    artifact_store: ProductionArtifactStore,
    article_snapshots: ProductionArticleSnapshotAdapter,
    fake_vt: _HashCheckedVtPort,
) -> Callable[[Any], Any]:
    def build(uow: Any) -> Any:
        registry = InMemoryToolRegistry()
        recorder = ProductionEvidenceRecorder(artifact_store, uow.evidence_observations)
        vt_capabilities = VirusTotalCapabilities(file_report=True, file_download=True)
        archive = _PersistedVtObservationService(artifact_store)

        async def resolve_vt_article(investigation_key: Any) -> Any:
            linked = await uow.analyst_investigations.get(investigation_key)
            if linked is None:
                raise ValueError("Investigation does not exist")
            return await article_snapshots.resolve(linked.synthesis_artifact_id)

        register_virustotal_tools(
            registry,
            virustotal=fake_vt,
            observations=archive,
            recorder=recorder,
            evidence_observations=uow.evidence_observations,
            findings=uow.investigation_findings,
            article_snapshot_resolver=resolve_vt_article,
            capabilities=vt_capabilities,
            evidence_payloads=artifact_store,
            max_pages=3,
            page_size=40,
            max_results=20,
            max_tool_seconds=120,
        )
        acquisition = VirusTotalSampleAcquisitionService(
            fake_vt,
            VirusTotalCapabilities(file_download=True),
            catalog,
            uow_factory,
            LocalIdentityProvider(),
        )
        hits = HitAcquisitionService(uow_factory, acquisition=acquisition, recorder=recorder)
        register_acquisition_tools(
            registry,
            hits=hits,
            qualifications=SampleQualificationService(uow_factory),
        )
        capabilities = set(virustotal_tool_capabilities(vt_capabilities))
        capabilities.add("vt.file_download")
        return InvestigationService(
            post_synthesis=AnalystPostSynthesisService(uow_factory, artifact_store),
            investigations=uow.analyst_investigations,
            states=uow.investigation_states,
            context_snapshots=ProductionArtifactContextSnapshotRepository(artifact_store),
            envelopes=uow.investigation_envelopes,
            context_builder=ResearchContextBuilder(
                article_snapshots=article_snapshots,
                observations=uow.evidence_observations,
                findings=uow.investigation_findings,
            ),
            bridge=BridgeInvestigator(ScriptedTurnTransport(())),
            registry=registry,
            dispatcher=RegistryToolDispatcher(registry),
            invocations=uow.tool_invocations,
            observations=uow.evidence_observations,
            findings=uow.investigation_findings,
            opinions=uow.analyst_opinions,
            sample_associations=uow.sample_associations,
            samples=uow.samples,
            source_documents=uow.source_documents,
            observation_writer=ProductionArtifactObservationWriter(
                artifact_store, uow.evidence_observations
            ),
            artifact_store=artifact_store,
            available_capabilities=frozenset(capabilities),
            no_progress_limit=3,
            commit_uow=uow.commit,
            rollback_uow=uow.rollback,
        )

    return build


@pytest.mark.asyncio
async def test_bootstrap_envelope_proposal_approve_and_batch_postgres(
    uow_factory, tmp_path
) -> None:
    catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / "bootstrap-envelope-blobs"), uow_factory
    )
    artifact_store = ProductionArtifactStore(catalog)
    suffix = uuid4().hex
    edition = Edition(
        country=f"Bootstrap envelope PG {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Bootstrap envelope persistence subject",
        slug=f"bootstrap-envelope-pg-{suffix}",
        tlp=TLP.AMBER,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    synthesis_payload = {
        "title": "Verified synthesis with one documented sample",
        "subject_id": str(subject.id),
        "lead": [{"text": "The article cites one sample."}],
        "sections": [],
        "timeline": [],
        "uncertainties": [],
    }
    synthesis_blob_id, synthesis_sha256 = await artifact_store.put_canonical_json(
        synthesis_payload, bucket="bootstrap-envelope-synthesis"
    )
    synthesis = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash=synthesis_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=synthesis_blob_id,
    )
    extraction_payload = {
        "sources": [
            {
                "indicators": [
                    {
                        "artifact_type": "hash",
                        "value": _SEED_SHA256,
                        "indicator_status": "confirmed_ioc",
                        "context": "IOC table row 1",
                        "evidence_quote": f"SHA-256 {_SEED_SHA256}",
                    }
                ]
            }
        ]
    }
    extraction_blob_id, extraction_sha256 = await artifact_store.put_canonical_json(
        extraction_payload, bucket="bootstrap-envelope-extraction"
    )
    extraction = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash=extraction_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=extraction_blob_id,
    )
    relevance_blob_id, relevance_sha256 = await artifact_store.put_canonical_json(
        {"items": []}, bucket="bootstrap-envelope-relevance"
    )
    relevance = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.RELEVANCE_PROJECTION,
        version=1,
        input_hash=relevance_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=relevance_blob_id,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis, budget=LoopBudget(max_vt_read_units=3, max_hits_acquired=1)
    )
    input_pack_payload = {
        "schema_version": "analyst-input-pack-v1",
        "investigation": {"id": str(investigation.id)},
        "synthesis_artifact": {
            "id": str(synthesis.id),
            "canonical_blob_id": str(synthesis.canonical_blob_id),
        },
        "retained_extraction_artifacts": [
            {
                "artifact_id": str(extraction.id),
                "canonical_blob_id": str(extraction.canonical_blob_id),
                "input_hash": extraction.input_hash,
                "version": extraction.version,
            }
        ],
        "file_indicators": [],
        "policy": {
            "tlp": "GREEN",
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
    decision = ProductionRoutingDecisionV1(
        decision_id=uuid4(),
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        extraction_artifact_id=extraction.id,
        extraction_artifact_hash=extraction_sha256,
        relevance_artifact_id=relevance.id,
        relevance_artifact_hash=relevance_sha256,
        input_hash=hashlib.sha256(f"routing-{suffix}".encode()).hexdigest(),
        policy_version="routing-policy-v1",
        status=RoutingStatus.DECIDED,
        publication_kind=PublicationKind.ARTICLE,
        reason_code=RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        confirmed_ioc_count=1,
        contextual_ioc_count=0,
        ambiguous_ioc_count=0,
        critical_source_diagnostics=(),
        decided_at=_FIXED_NOW,
        decision_hash=hashlib.sha256(f"decision-{suffix}".encode()).hexdigest(),
    )
    association = SampleAssociation(
        investigation_id=investigation.id,
        sample_sha256=_SEED_SHA256,
        origin=SampleOrigin.VT_PIVOT,
        qualification=SampleQualification.CANDIDATE,
    )
    state = InvestigationWorkingState(
        investigation_id=investigation.id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="Quels IOC sont encore joignables ?", potential=1.0, rank=1),
                AgendaItem(question="Quelle chaîne est confirmée ?", potential=0.9, rank=2),
            )
        ),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )
    async with uow_factory() as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        for artifact in (synthesis, extraction, relevance):
            await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.analyst_input_packs.append(input_pack)
        await uow.sample_associations.append_version(association, expected_version=0)
        await uow.investigation_states.save_if_version(state, expected_version=None)
        await uow.production_routing_decisions.append_and_adopt(
            decision, expected_version=run.version
        )
        await uow.commit()

    settings = Settings(
        _env_file=None,
        investigation_max_vt_read_units=3,
        investigation_max_hits_acquired=1,
        virustotal_download_max_bytes=1024,
    )
    article_snapshots = ProductionArticleSnapshotAdapter(uow_factory, artifact_store)
    bootstrap = InvestigationBootstrapService(
        uow_factory=uow_factory,
        article_snapshots=article_snapshots,
        artifact_store=artifact_store,
        capabilities=BootstrapVtCapabilities(file_report=True, download=True),
        clock=lambda: _FIXED_NOW,
    )
    envelope_service = InvestigationBootstrapEnvelopeService(
        uow_factory=uow_factory,
        bootstrap=bootstrap,
        article_snapshots=article_snapshots,
        settings=settings,
        clock=lambda: _FIXED_NOW,
    )

    proposal = await envelope_service.propose_for_bootstrap(investigation.id)
    assert proposal.approved is False
    assert proposal.allowed_providers == ("vt", "bridge")
    assert proposal.vt_reads.operations_planned == 1
    assert proposal.vt_reads.operations_covered == 1
    assert proposal.sample_acquisition.sample_count_covered == 1
    assert proposal.sample_acquisition.acquire_hits_allowed is True
    assert proposal.sample_acquisition.total_max_bytes == 1024
    budget_limits = {item.dimension: item.limit for item in proposal.costs.limits}
    assert budget_limits[BudgetDimension.VT_UNITS] == 3
    assert budget_limits[BudgetDimension.DOWNLOADS] == 1

    approval = await envelope_service.approve_for_bootstrap(
        investigation.id,
        actor_id="analyst:pg-bootstrap",
        reason="Approve the single bounded bootstrap envelope.",
        correlation_id="g2:pg-bootstrap:initial",
        expected_proposal_sha256=proposal.proposal_sha256,
    )
    assert approval.envelope_version == 1
    assert approval.approval_actor_id == "analyst:pg-bootstrap"
    assert approval.budget_limits[BudgetDimension.VT_UNITS.value] == 3
    assert approval.budget_limits[BudgetDimension.DOWNLOADS.value] == 1

    async with uow_factory() as uow:
        stored_envelope = await uow.investigation_envelopes.get(investigation.id)
        decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
    assert stored_envelope is not None
    assert stored_envelope.version == 1
    assert stored_envelope.allowed_providers == ("vt", "bridge")
    assert stored_envelope.acquire_hits_allowed is True
    assert len(decisions) == 1
    assert decisions[0].decision_type.value == "pivot_approve"
    assert decisions[0].actor_id == "analyst:pg-bootstrap"

    fake_vt = _HashCheckedVtPort(_SAMPLE_BYTES)
    snapshot = await bootstrap.execute_batch(
        investigation.id,
        idempotency_key="pg-bootstrap-envelope-batch",
        actor_id="analyst:pg-bootstrap",
        executor_factory=_executor_factory(
            uow_factory=uow_factory,
            catalog=catalog,
            artifact_store=artifact_store,
            article_snapshots=article_snapshots,
            fake_vt=fake_vt,
        ),
    )

    assert snapshot.plan_state is BootstrapPlanState.READY
    assert fake_vt.calls["file_report"] == 1
    assert fake_vt.download_calls == [_SEED_SHA256]
    operations = {
        operation.tool_name: operation for ioc in snapshot.iocs for operation in ioc.operations
    }
    assert operations["vt.file_report"].status is ToolInvocationStatus.SUCCEEDED
    download_operation = operations["vt.download_sample"]
    assert download_operation.status is ToolInvocationStatus.SUCCEEDED, (
        download_operation.reason,
        download_operation.error_code,
        download_operation.detail_code,
        download_operation.limitations,
        snapshot.limitations,
        fake_vt.download_calls,
    )

    async with uow_factory() as uow:
        stored_envelope = await uow.investigation_envelopes.get(investigation.id)
        stored_association = await uow.sample_associations.get(association.id)
        successful_attempt = await uow.sample_acquisition_attempts.find_successful(
            investigation.id, _SEED_SHA256
        )
        stored_sample = (
            await uow.samples.get(successful_attempt.sample_id)
            if successful_attempt is not None and successful_attempt.sample_id is not None
            else None
        )
        stored_blob = await uow.blobs.get(stored_sample.blob_id) if stored_sample else None
    assert stored_envelope is not None
    assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    assert 1 <= stored_envelope.budget.consumed[BudgetDimension.VT_UNITS] <= 3
    assert stored_association is not None
    assert stored_association.sample_sha256 == _SEED_SHA256
    assert stored_sample is not None
    assert stored_sample.expected_hash == _SEED_SHA256
    assert stored_blob is not None
    assert stored_blob.descriptor.logical_bucket == "samples-quarantine"
    assert stored_blob.descriptor.sha256 == _SEED_SHA256

    replayed = await bootstrap.execute_batch(
        investigation.id,
        idempotency_key="pg-bootstrap-envelope-batch",
        actor_id="analyst:pg-bootstrap",
        executor_factory=_executor_factory(
            uow_factory=uow_factory,
            catalog=catalog,
            artifact_store=artifact_store,
            article_snapshots=article_snapshots,
            fake_vt=fake_vt,
        ),
    )
    assert replayed.snapshot_id == snapshot.snapshot_id
    assert fake_vt.calls["file_report"] == 1
    assert fake_vt.download_calls == [_SEED_SHA256]
