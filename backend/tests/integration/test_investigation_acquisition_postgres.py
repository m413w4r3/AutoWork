from __future__ import annotations

import asyncio
import hashlib
import struct
from dataclasses import replace
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisRequest,
    ConfinedAnalysisResult,
    ConfinedAnalysisWorkerAttestation,
    archive_member_transfer_bucket,
)
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_acquisition import (
    HitAcquisitionService,
    sample_association_id,
)
from cti_app.application.investigation_acquisition_tools import (
    ARCHIVE_EXTRACT_MEMBER_CAPABILITY,
    LOCAL_QUALIFICATION_CAPABILITY,
    SAMPLE_ANALYSIS_CAPABILITY,
    VT_FILE_DOWNLOAD_CAPABILITY,
    ArchiveExtractMemberAdapter,
    LocalSampleAnalysisService,
    register_acquisition_tools,
)
from cti_app.application.investigation_contracts import ArticleContextV1
from cti_app.application.investigation_governance import (
    InvestigationActor,
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.investigation_rule_tools import QuarantineRuleCorpusBytes
from cti_app.application.investigation_service import ProductionArtifactObservationWriter
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
)
from cti_app.application.network_capture_import import NetworkCaptureImportService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import (
    ArticleSnapshot,
    CanonicalEvidenceSummaryPort,
    ResearchContextBuilder,
)
from cti_app.application.sample_acquisition import (
    SAMPLES_QUARANTINE_BUCKET,
    SampleAcquisitionCacheScope,
    SampleAcquisitionResponseOrigin,
    VirusTotalSampleAcquisitionService,
)
from cti_app.application.virustotal import VirusTotalCapabilities, VirusTotalHttpError
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Sample, SampleState, Subject
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    DisclosureBounds,
    EvidenceKind,
    InvestigationAgenda,
    InvestigationBudget,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.repositories.investigation import (
    SqlAlchemyInvestigationEnvelopeRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.confined_worker_harness import build_confined_worker_harness
from tests.fixtures.binary_factory import build_elf64
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


class _HashCheckedPort:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        assert hashlib.sha256(self.payload).hexdigest() == file_hash
        self.calls += 1
        sink.write(self.payload)


class _RouteRefusedPort:
    def __init__(self) -> None:
        self.calls = 0
        self.sample_acquisition_cache_scope = SampleAcquisitionCacheScope(
            provider="virustotal",
            account_capability_fingerprint="postgres-test-account-capability",
            route_fingerprint="/api/v3/files/{hash}/download",
            transport="proxy-v3",
            configuration_fingerprint="proxy-test-config-v1",
        )

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        del file_hash, sink
        self.calls += 1
        error = VirusTotalHttpError(
            "synthetic provider refused download",
            code="virustotal_not_authorized",
            status_code=403,
            route="/files/{hash}/download",
        )
        error.response_origin = SampleAcquisitionResponseOrigin.PROVIDER
        raise error


class _AlreadyDocumentedNovelty:
    async def classify(self, **kwargs: Any) -> Any:
        del kwargs
        return SimpleNamespace(
            novelty=SimpleNamespace(value="not_confirmed"),
            reason=SimpleNamespace(code=SimpleNamespace(value="already_documented")),
            scope="article",
        )


class _PostgresEvidenceRecorder:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def record_json(self, **kwargs: Any) -> Any:
        if self._artifact_store is None:
            raise AssertionError("record_json requires a canonical artifact store")
        async with self._uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(self._artifact_store, uow.evidence_observations)
            observation = await recorder.record_json(**kwargs)
            await uow.commit()
            return observation

    async def record_existing_blob(self, **kwargs: Any) -> Any:
        async with self._uow_factory() as uow:
            recorder = ProductionEvidenceRecorder(
                cast(ProductionArtifactStore, object()), uow.evidence_observations
            )
            observation = await recorder.record_existing_blob(**kwargs)
            await uow.commit()
            return observation


class _PcapAnalysisWorker:
    def __init__(self) -> None:
        self.family_statuses = {
            "sample_analysis": ConfinedAnalysisFamilyStatus(
                "sample_analysis", True, "static-test-v1"
            )
        }
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return ConfinedAnalysisResult(
            status=ToolInvocationStatus.SUCCEEDED,
            operation_key=f"confined-analysis:{request.sha256}",
            artifact_id=request.artifact_id,
            input_sha256=request.sha256,
            result_blob_id=request.artifact_id,
            result_sha256=request.sha256,
            payload={"result": {"status": "SUCCEEDED", "families": {}}},
        )


class _ArchiveMemberIntegrationWorker:
    def __init__(self, catalog: BlobCatalogService, member: bytes) -> None:
        self.catalog = catalog
        self.member = member
        self.family_statuses = {
            "archive_tools": ConfinedAnalysisFamilyStatus("archive_tools", True, "16.02")
        }
        self.attestation = ConfinedAnalysisWorkerAttestation(
            reachable=True,
            confined=True,
            isolation_profile="analysis-worker-test-v1",
            versions={"local_7z": "16.02"},
            families=self.family_statuses,
        )
        self.available = True
        self.available_reason = None
        self.isolation_profile = "analysis-worker-test-v1"
        self.requests: list[ConfinedAnalysisRequest] = []

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        self.requests.append(request)
        member_sha256 = hashlib.sha256(self.member).hexdigest()
        transfer = await self.catalog.ingest(
            BytesIO(self.member),
            logical_bucket=archive_member_transfer_bucket(
                request.analysis_parameters["transfer_nonce"]
            ),
            mime_type="application/octet-stream",
        )
        return ConfinedAnalysisResult(
            status=ToolInvocationStatus.SUCCEEDED,
            operation_key=f"confined-analysis:{member_sha256}",
            artifact_id=request.artifact_id,
            input_sha256=request.sha256,
            payload={
                "investigation_id": str(request.investigation_id),
                "association_id": str(request.association_id),
                "sample_id": str(request.sample_id),
                "artifact_sha256": request.sha256,
                "result": {
                    "member_status": "extracted",
                    "member_path": request.analysis_parameters["member_path"],
                    "member_sha256": member_sha256,
                    "member_size": len(self.member),
                    "transfer_blob_id": str(transfer.id),
                },
            },
        )


@pytest.mark.asyncio
async def test_g2_acquisition_quarantine_and_g3_decisions_persist_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    payload = b"harmless PostgreSQL acquisition fixture"
    requested_sha256 = hashlib.sha256(payload).hexdigest()
    port = _HashCheckedPort(payload)
    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "blob-storage"), uow_factory)
    acquisition = VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    proposal = InvestigationEnvelopeProposal(
        investigation_id=investigation.id,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={BudgetDimension.VT_UNITS: 4, BudgetDimension.DOWNLOADS: 2}
        ),
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=False,
        ),
    )
    try:
        approved = await envelope_service.approve(
            proposal,
            actor_id="analyst:integration",
            reason="Approve two hash-verified VT hit downloads.",
            correlation_id=f"g2:{investigation.id}",
        )
        recorder = cast(Any, _PostgresEvidenceRecorder(uow_factory))
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=recorder,
            novelty=_AlreadyDocumentedNovelty(),  # type: ignore[arg-type]
        )
        association = await hits.register_hit(
            investigation.id, requested_sha256, SampleOrigin.VT_PIVOT
        )
        result = await hits.acquire_hit(
            association.id,
            purpose="Review a VT pivot under the approved G2 envelope.",
            operation_key=f"vt.download_sample:{investigation.id}:{association.id}",
        )

        assert result.status is ToolInvocationStatus.SUCCEEDED
        assert result.novelty is not None
        assert result.novelty.reason.code.value == "already_documented"
        assert result.sample_id is not None
        assert result.association.qualification is SampleQualification.CANDIDATE
        assert port.calls == 1
        qualification_service = SampleQualificationService(uow_factory)
        confirmed = await qualification_service.qualify(
            association.id,
            SampleQualification.CONFIRMED,
            InvestigationActor("analyst:integration", QualificationActorKind.HUMAN),
            "The analyst separately reviewed the candidate evidence.",
            result.observation_refs,
            correlation_id=f"g3:{association.id}",
        )
        assert confirmed.qualification is SampleQualification.CONFIRMED

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored_envelope = await uow.investigation_envelopes.get(investigation.id)
            stored_association = await uow.sample_associations.get(association.id)
            sample = await uow.samples.get(result.sample_id)
            assert stored_envelope is not None
            assert stored_envelope.version == approved.version + 2
            assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
            assert stored_association is not None
            assert stored_association.qualification is SampleQualification.CONFIRMED
            assert stored_association.changes[-1].actor_kind is QualificationActorKind.HUMAN
            assert sample is not None
            assert sample.state.value == "review_candidate"
            blob = await uow.blobs.get(sample.blob_id)
            assert blob is not None
            assert blob.descriptor.sha256 == requested_sha256
            assert await catalog.read(sample.blob_id, max_bytes=1024) == payload
            observations = await uow.evidence_observations.list_for_investigation(investigation.id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
            attempts = await uow.sample_acquisition_attempts.find_successful(
                investigation.id, requested_sha256
            )
            assert any(item.kind is EvidenceKind.OTHER for item in observations)
            assert {item.decision_type.value for item in decisions} == {
                "pivot_approve",
                "member_validate",
            }
            assert attempts is not None and attempts.sample_id == result.sample_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_g1_registry_download_result_reaches_research_context_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    payload = b"MZ\x00bounded PostgreSQL context fixture"
    requested_sha256 = hashlib.sha256(payload).hexdigest()
    port = _HashCheckedPort(payload)
    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "g1-context-storage"), uow_factory)
    artifact_store = ProductionArtifactStore(catalog)
    acquisition = VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    approved = await envelope_service.approve(
        InvestigationEnvelopeProposal(
            investigation_id=investigation.id,
            allowed_providers=("vt",),
            budget=InvestigationBudget(
                limits={
                    BudgetDimension.BRIDGE_TURNS: 4,
                    BudgetDimension.VT_UNITS: 4,
                    BudgetDimension.DOWNLOADS: 1,
                }
            ),
            acquire_hits_allowed=True,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.CLEAR,
                do_not_submit=False,
                external_llm_allowed=False,
            ),
        ),
        actor_id="analyst:g1-integration",
        reason="Approve one hash-verified synthetic G1 download.",
        correlation_id=f"g1-context:{investigation.id}",
    )
    hits = HitAcquisitionService(
        uow_factory,
        acquisition=acquisition,
        recorder=cast(Any, _PostgresEvidenceRecorder(uow_factory)),
    )
    analyzer = LocalSampleAnalysisService(
        hits,
        uow_factory,
        cast(Any, _PostgresEvidenceRecorder(uow_factory)),
        confined_analysis=_PcapAnalysisWorker(),  # type: ignore[arg-type]
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(uow_factory),
        analyzer=analyzer,
    )
    dispatcher = RegistryToolDispatcher(registry)
    capabilities = frozenset(
        {
            VT_FILE_DOWNLOAD_CAPABILITY,
            SAMPLE_ANALYSIS_CAPABILITY,
            LOCAL_QUALIFICATION_CAPABILITY,
        }
    )
    tool_context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=approved,
        budget=approved.budget,
        idempotency_key="g1-postgres-download",
        available_capabilities=capabilities,
    )
    async with uow_factory() as uow:
        recorder = ProductionEvidenceRecorder(artifact_store, uow.evidence_observations)
        seed_observation = await recorder.record_json(
            investigation_id=investigation.id,
            kind=EvidenceKind.OTHER,
            payload={"sample_sha256": requested_sha256},
            source="g1-integration-fixture",
            source_version="1",
            object_type="sample",
            object_id=requested_sha256,
        )
        await uow.commit()

    arguments = {
        "sha256": requested_sha256,
        "purpose": "Acquire the synthetic sample for context visibility.",
        "evidence_refs": [str(seed_observation.id)],
    }
    operation_key = f"g1-download:{investigation.id}:{requested_sha256}"
    result = await dispatcher.dispatch(
        tool_context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Acquire a verified test sample and return its association id.",
        operation_key=operation_key,
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    invocation = ToolInvocation(
        investigation_id=investigation.id,
        operation_key=operation_key,
        tool_name="vt.download_sample",
        schema_version="1",
        normalized_parameters=arguments,
        purpose="Acquire a verified test sample and return its association id.",
    ).transition(ToolInvocationStatus.RUNNING)
    async with uow_factory() as uow:
        await uow.tool_invocations.add_if_absent(invocation)
        await uow.commit()
    async with uow_factory() as uow:
        writer = ProductionArtifactObservationWriter(artifact_store, uow.evidence_observations)
        result_observations = await writer.archive(invocation, result)
        await uow.commit()
    completed = invocation.transition(
        result.status,
        response_refs=(*result.observation_refs, *(item.id for item in result_observations)),
        consumed_budget=result.budget_consumed,
    )
    async with uow_factory() as uow:
        await uow.tool_invocations.update_if_version(completed, expected_version=invocation.version)
        await uow.commit()

    snapshot = ArticleSnapshot(
        subject_id=investigation.subject_id,
        production_run_id=investigation.production_run_id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256="f" * 64,
        article=ArticleContextV1(
            short_summary="Synthetic integration article.",
            claims=(),
            documented_samples=(requested_sha256,),
            known_chain=(),
            open_questions=("Analyze the sample.", "Verify the recorded sample identity."),
        ),
    )

    class _SnapshotPort:
        async def resolve(self, _artifact_id):
            return snapshot

    class _RepositoryReader:
        def __init__(self, repository_name: str) -> None:
            self._repository_name = repository_name

        async def list_for_investigation(self, investigation_id):
            async with uow_factory() as uow:
                repository = getattr(uow, self._repository_name)
                return tuple(await repository.list_for_investigation(investigation_id))

    class _EmptyFindings:
        async def list_for_investigation(self, _investigation_id):
            return ()

        async def version_count_for_investigation(self, _investigation_id):
            return 0

    builder = ResearchContextBuilder(
        article_snapshots=_SnapshotPort(),
        observations=_RepositoryReader("evidence_observations"),
        findings=_EmptyFindings(),
        registry=registry,
        evidence_summaries=CanonicalEvidenceSummaryPort(artifact_store),
        invocations=_RepositoryReader("tool_invocations"),
    )
    context = await builder.build(
        investigation_id=investigation.id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        state=InvestigationWorkingState(
            investigation_id=investigation.id,
            agenda=InvestigationAgenda(
                items=(
                    AgendaItem(question="Analyze the sample.", potential=1.0, rank=1),
                    AgendaItem(question="Check its recorded identity.", potential=0.9, rank=2),
                )
            ),
        ),
        tool_context=tool_context,
        budget_remaining={"bridge_turns": 3},
    )

    sample = context.investigation.sample_deltas[0]
    latest_result = context.investigation.latest_tool_results[-1]
    assert sample["association_id"] == result.output["association_id"]
    assert sample["sha256"] == requested_sha256
    assert sample["byte_size"] == len(payload)
    assert sample["provenance"] == "vt_download"
    assert sample["analysis_status"] == "not_analyzed"
    assert latest_result.tool_name == "vt.download_sample"
    assert latest_result.output_ids["association_id"] == result.output["association_id"]
    assert port.calls == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_subject_sample_reuse_through_registered_tools_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    first_investigation = await _make_investigation(session_factory)
    second_investigation = await _make_investigation_for_subject(
        session_factory, first_investigation
    )
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    payload = b"same-subject sample reuse from the original quarantine blob"
    requested_sha256 = hashlib.sha256(payload).hexdigest()
    port = _HashCheckedPort(payload)
    catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / "same-subject-reuse-storage"), uow_factory
    )
    acquisition = VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    try:

        async def approve(investigation_id, correlation_id):
            return await envelope_service.approve(
                InvestigationEnvelopeProposal(
                    investigation_id=investigation_id,
                    allowed_providers=("vt",),
                    budget=InvestigationBudget(
                        limits={BudgetDimension.VT_UNITS: 4, BudgetDimension.DOWNLOADS: 2}
                    ),
                    acquire_hits_allowed=True,
                    disclosure=DisclosureBounds(
                        tlp=TLPLevel.CLEAR,
                        do_not_submit=False,
                        external_llm_allowed=True,
                    ),
                ),
                actor_id="analyst:integration",
                reason="Approve hash-verified acquisition and same-subject quarantine reuse.",
                correlation_id=correlation_id,
            )

        await approve(first_investigation.id, "g2:reuse:first")
        second_envelope = await approve(second_investigation.id, "g2:reuse:second")
        recorder = cast(Any, _PostgresEvidenceRecorder(uow_factory))
        first_hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=recorder,
            novelty=_AlreadyDocumentedNovelty(),  # type: ignore[arg-type]
        )
        first_association = await first_hits.register_hit(
            first_investigation.id, requested_sha256, SampleOrigin.VT_PIVOT
        )
        original = await first_hits.acquire_hit(
            first_association.id,
            purpose="Acquire the sample for the first investigation.",
            operation_key="same-subject:first-download",
        )
        assert original.status is ToolInvocationStatus.SUCCEEDED
        assert original.sample_id is not None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            original_sample = await uow.samples.get(original.sample_id)
        assert original_sample is not None

        second_hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=recorder,
            novelty=_AlreadyDocumentedNovelty(),  # type: ignore[arg-type]
        )
        worker = _PcapAnalysisWorker()
        analyzer = LocalSampleAnalysisService(
            second_hits,
            uow_factory,
            recorder,
            confined_analysis=worker,  # type: ignore[arg-type]
        )
        registry = InMemoryToolRegistry()
        register_acquisition_tools(
            registry,
            hits=second_hits,
            qualifications=SampleQualificationService(uow_factory),
            analyzer=analyzer,
        )
        dispatcher = RegistryToolDispatcher(registry)
        context = ToolExecutionContext(
            investigation_id=second_investigation.id,
            envelope=second_envelope,
            budget=second_envelope.budget,
            idempotency_key="same-subject:second-investigation",
            available_capabilities=frozenset(
                {
                    VT_FILE_DOWNLOAD_CAPABILITY,
                    SAMPLE_ANALYSIS_CAPABILITY,
                    LOCAL_QUALIFICATION_CAPABILITY,
                }
            ),
        )
        arguments = {
            "sha256": requested_sha256,
            "purpose": "Reuse the same-subject quarantined sample.",
            "evidence_refs": [],
        }
        reused = await dispatcher.dispatch(
            context,
            tool_name="vt.download_sample",
            schema_version="1",
            arguments=arguments,
            purpose="Reuse a verified sample already quarantined for this subject.",
            operation_key="same-subject:second-reuse",
        )

        assert reused.status is ToolInvocationStatus.SUCCEEDED
        assert reused.output["reused_existing_sample"] is True
        assert reused.output["sample_id"] == str(original.sample_id)
        assert reused.output["association_id"] != str(first_association.id)
        assert reused.budget_consumed == {
            BudgetDimension.VT_UNITS: 0,
            BudgetDimension.DOWNLOADS: 0,
        }
        assert port.calls == 1
        replay = await dispatcher.dispatch(
            context,
            tool_name="vt.download_sample",
            schema_version="1",
            arguments=arguments,
            purpose="Replay same-subject quarantine reuse idempotently.",
            operation_key="same-subject:second-reuse-replay",
        )
        assert replay.status is ToolInvocationStatus.SUCCEEDED
        assert replay.output["association_id"] == reused.output["association_id"]
        assert replay.output["sample_id"] == reused.output["sample_id"]
        assert replay.budget_consumed == reused.budget_consumed
        assert port.calls == 1
        assert context.budget.consumed[BudgetDimension.VT_UNITS] == 0
        assert context.budget.consumed[BudgetDimension.DOWNLOADS] == 0

        analysis = await dispatcher.dispatch(
            context,
            tool_name="sample.analyze",
            schema_version="1",
            arguments={
                "association_id": reused.output["association_id"],
                "purpose": "Analyze the reused quarantined sample locally.",
                "evidence_refs": [],
            },
            purpose="Analyze the reused quarantine sample.",
            operation_key="same-subject:analyze-reused",
        )
        assert analysis.status is ToolInvocationStatus.PARTIAL
        assert worker.requests[0].sample_id == original.sample_id
        assert worker.requests[0].artifact_id == original_sample.blob_id
        assert await catalog.read(original_sample.blob_id, max_bytes=1024) == payload

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored_envelope = await uow.investigation_envelopes.get(second_investigation.id)
            events = await uow.provenance.list_for_aggregate("sample", original.sample_id)
            reuse_events = [
                event for event in events if event.event_type == "reused_from_subject_sample"
            ]
            second_attempt = await uow.sample_acquisition_attempts.find_successful(
                second_investigation.id, requested_sha256
            )
            assert stored_envelope is not None
            assert stored_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
            assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0
            assert second_attempt is None
            assert original_sample.origin == "virustotal_file_download"
            assert len(reuse_events) == 1
            assert reuse_events[0].payload["investigation_id"] == str(second_investigation.id)
            assert reuse_events[0].payload["association_id"] == reused.output["association_id"]
            assert reuse_events[0].payload["sample_id"] == str(original.sample_id)
            assert reuse_events[0].payload["sample_origin"] == original_sample.origin
            assert reuse_events[0].payload["new_download"] is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_definitive_download_refusal_is_cached_and_budget_is_resolved_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    port = _RouteRefusedPort()
    catalog = BlobCatalogService(
        FilesystemBlobStore(tmp_path / "refused-download-storage"), uow_factory
    )
    acquisition = VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
        cache_scope_provider=port,
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    requested_sha256 = "a" * 64
    try:
        await envelope_service.approve(
            InvestigationEnvelopeProposal(
                investigation_id=investigation.id,
                allowed_providers=("vt",),
                budget=InvestigationBudget(
                    limits={BudgetDimension.VT_UNITS: 8, BudgetDimension.DOWNLOADS: 4}
                ),
                acquire_hits_allowed=True,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.CLEAR,
                    do_not_submit=False,
                    external_llm_allowed=False,
                ),
            ),
            actor_id="analyst:integration",
            reason="Approve bounded synthetic download failure coverage.",
            correlation_id=f"g2:download-refusal:{investigation.id}",
        )
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=cast(Any, _PostgresEvidenceRecorder(uow_factory)),
        )
        association = await hits.register_hit(
            investigation.id, requested_sha256, SampleOrigin.VT_PIVOT
        )

        first = await hits.acquire_hit(
            association.id,
            purpose="Review a synthetic VT hit.",
            operation_key="synthetic:download:first",
        )
        replay = await hits.acquire_hit(
            association.id,
            purpose="Retry the same synthetic VT hit.",
            operation_key="synthetic:download:second",
        )

        assert first.status is ToolInvocationStatus.UNAVAILABLE
        assert first.error_code == "virustotal_download_authorization_refused"
        assert first.budget_consumed == {
            BudgetDimension.VT_UNITS: 1,
            BudgetDimension.DOWNLOADS: 0,
        }
        assert replay.status is ToolInvocationStatus.UNAVAILABLE
        assert replay.cached is True
        assert replay.budget_consumed == {}
        assert port.calls == 1
        port.sample_acquisition_cache_scope = SampleAcquisitionCacheScope(
            provider="virustotal",
            account_capability_fingerprint="postgres-test-account-capability",
            route_fingerprint="/vtapi/v2/file/download",
            transport="direct-v2",
            configuration_fingerprint="proxy-test-config-v2",
        )
        changed_route = await hits.acquire_hit(
            association.id,
            purpose="Retry after the approved route configuration changes.",
            operation_key="synthetic:download:changed-route",
        )
        assert changed_route.status is ToolInvocationStatus.UNAVAILABLE
        assert changed_route.cached is False
        assert port.calls == 2

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            latest_error = await uow.sample_acquisition_attempts.find_latest_error(
                investigation.id, requested_sha256
            )
            stored_envelope = await uow.investigation_envelopes.get(investigation.id)
            assert latest_error is not None
            assert latest_error.error_code.startswith("rc1a")
            assert len(latest_error.error_code) == 64
            assert stored_envelope is not None
            assert stored_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 2
            assert stored_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0
            assert stored_envelope.budget.reserved[BudgetDimension.VT_UNITS] == 0
            assert stored_envelope.budget.reserved[BudgetDimension.DOWNLOADS] == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_operator_imported_pcap_is_quarantined_sample_visible_to_analyzer_and_rules(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "pcap-import-storage"), uow_factory)
    capture_header = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
    service = NetworkCaptureImportService(
        uow_factory=uow_factory,
        blob_catalog=catalog,
        settings=SimpleNamespace(network_capture_max_bytes=1024),  # type: ignore[arg-type]
    )
    try:
        imported = await service.import_capture(
            investigation_id=investigation.id,
            content=capture_header,
            operator_id="analyst:integration",
        )
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=None,
            recorder=cast(Any, _PostgresEvidenceRecorder(uow_factory)),
        )
        resolved = await hits.resolve_quarantined_sample(imported.association_id)
        worker = _PcapAnalysisWorker()
        analysis = await LocalSampleAnalysisService(
            hits,
            uow_factory,
            cast(Any, _PostgresEvidenceRecorder(uow_factory)),
            confined_analysis=worker,  # type: ignore[arg-type]
        ).analyze(association_id=imported.association_id, invocation_ref=None)
        rule_bytes = await QuarantineRuleCorpusBytes(
            uow_factory, FilesystemBlobStore(tmp_path / "pcap-import-storage")
        ).get_bytes_for_sample(imported.sha256)

        assert resolved.sample_id == imported.sample_id
        assert resolved.sha256 == imported.sha256 == hashlib.sha256(capture_header).hexdigest()
        assert await catalog.read(imported.blob_id, max_bytes=1024) == capture_header
        assert analysis.status is ToolInvocationStatus.PARTIAL
        assert "membership remains candidate" in analysis.message
        assert worker.requests[0].artifact_id == imported.blob_id
        assert rule_bytes == capture_header
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            sample = await uow.samples.get(imported.sample_id)
            association = await uow.sample_associations.get(imported.association_id)
            events = await uow.provenance.list_for_aggregate("sample", imported.sample_id)
            assert sample is not None and sample.state is SampleState.QUARANTINED
            assert sample.origin == "operator_import"
            assert sample.source_service == "operator_import"
            assert association is not None
            assert association.qualification is SampleQualification.CANDIDATE
            assert any(
                event.payload.get("provenance") == "operator_import"
                and event.payload.get("import_source") == "network_capture"
                for event in events
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_registered_sample_analysis_uses_worker_and_persists_feature_sets_in_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    blob_store = FilesystemBlobStore(tmp_path / "confined-sample-analysis-storage")
    catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(catalog)
    payload = build_elf64()
    requested_sha256 = hashlib.sha256(payload).hexdigest()
    settings = Settings(
        _env_file=None,
        analysis_worker_enabled=True,
        local_analysis_enabled=True,
        rule_workbench_enabled=True,
    )
    worker = build_confined_worker_harness(
        settings=settings,
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        required_families=("sample_analysis",),
    )
    acquisition = VirusTotalSampleAcquisitionService(
        _HashCheckedPort(payload),
        VirusTotalCapabilities(file_download=True),
        catalog,
        uow_factory,
        LocalIdentityProvider(),
    )
    envelope_service = InvestigationEnvelopeService(uow_factory)
    proposal = InvestigationEnvelopeProposal(
        investigation_id=investigation.id,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={BudgetDimension.VT_UNITS: 4, BudgetDimension.DOWNLOADS: 1}
        ),
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=False,
        ),
    )
    try:
        approved = await envelope_service.approve(
            proposal,
            actor_id="analyst:integration",
            reason="Approve one harmless synthetic ELF fixture.",
            correlation_id=f"confined-sample-analysis:{investigation.id}",
        )
        recorder = cast(Any, _PostgresEvidenceRecorder(uow_factory, artifact_store))
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=acquisition,
            recorder=recorder,
            novelty=_AlreadyDocumentedNovelty(),  # type: ignore[arg-type]
        )
        association = await hits.register_hit(
            investigation.id, requested_sha256, SampleOrigin.VT_PIVOT
        )
        acquired = await hits.acquire_hit(
            association.id,
            purpose="Analyze a harmless synthetic ELF fixture.",
            operation_key=f"confined-sample-analysis:{investigation.id}:{association.id}",
        )
        assert acquired.sample_id is not None

        analyzer = LocalSampleAnalysisService(
            hits,
            uow_factory,
            recorder,
            confined_analysis=worker.port,
            analysis_timeout_seconds=worker.settings.analysis_worker_job_timeout_seconds,
            max_output_bytes=worker.settings.analysis_worker_result_max_bytes,
        )
        registry = InMemoryToolRegistry()
        register_acquisition_tools(
            registry,
            hits=hits,
            qualifications=SampleQualificationService(uow_factory),
            analyzer=analyzer,
        )
        dispatcher = RegistryToolDispatcher(registry)
        context = ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=approved,
            budget=approved.budget,
            idempotency_key="confined-sample-analysis",
            available_capabilities=frozenset({SAMPLE_ANALYSIS_CAPABILITY}),
        )
        result = await dispatcher.dispatch(
            context,
            tool_name="sample.analyze",
            schema_version="1",
            arguments={
                "association_id": str(association.id),
                "purpose": "Analyze the already quarantined synthetic ELF.",
                "evidence_refs": [],
            },
            purpose="Run static analysis in the attested worker.",
            operation_key=f"sample.analyze:{investigation.id}:{association.id}",
        )

        assert result.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
        assert len(worker.dispatcher.calls) == 1
        analysis = result.output["analysis"]
        assert {"static", "capa", "smda"} <= set(analysis["families"])
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            sample = await uow.samples.get(acquired.sample_id)
            observations = await uow.evidence_observations.list_for_investigation(investigation.id)
            static_features = await uow.sample_feature_sets.list_for_samples((acquired.sample_id,))
            capabilities = await uow.capability_sets.list_for_samples((acquired.sample_id,))
            code_features = await uow.code_feature_sets.list_for_samples((acquired.sample_id,))
            assert sample is not None and sample.blob_id is not None
            assert await catalog.read(sample.blob_id, max_bytes=len(payload)) == payload
            assert any(item.kind is EvidenceKind.BINARY_ANALYSIS for item in observations)
            assert static_features, analysis
            assert capabilities
            assert code_features
    finally:
        await engine.dispose()


async def _make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Acquisition integration {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Investigation acquisition integration fixture",
            slug=f"acquisition-integration-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="f" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(max_vt_read_units=4, max_hits_acquired=2),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


async def _make_investigation_for_subject(
    session_factory: async_sessionmaker[AsyncSession],
    source: AnalystInvestigation,
) -> AnalystInvestigation:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        source_run = await uow.production_runs.get_for_update(source.production_run_id)
        assert source_run is not None
        source_run.mark_ready()
        await uow.production_runs.save(source_run)
        run = ProductionRun(
            subject_id=source.subject_id,
            edition_id=source_run.edition_id,
            run_number=source_run.run_number + 1,
        )
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=source.subject_id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="e" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=source.subject_id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(max_vt_read_units=4, max_hits_acquired=2),
        )
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


@pytest.mark.asyncio
async def test_archive_member_ingestion_is_idempotent_and_resolves_from_postgres(
    migrated_postgres_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    uow_factory = cast(UnitOfWorkFactory, lambda: SqlAlchemyUnitOfWork(session_factory))
    try:
        investigation = await _make_investigation(session_factory)
        envelope_service = InvestigationEnvelopeService(uow_factory)
        approved = await envelope_service.approve(
            InvestigationEnvelopeProposal(
                investigation_id=investigation.id,
                allowed_providers=("vt",),
                budget=InvestigationBudget(
                    limits={BudgetDimension.VT_UNITS: 2, BudgetDimension.DOWNLOADS: 1}
                ),
                acquire_hits_allowed=True,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.CLEAR,
                    do_not_submit=False,
                    external_llm_allowed=False,
                ),
            ),
            actor_id="analyst:integration",
            reason="Approve bounded local archive extraction for the test.",
            correlation_id=f"archive-member-g2:{investigation.id}",
        )
        catalog = BlobCatalogService(
            FilesystemBlobStore(tmp_path / "archive-member-blobs"), uow_factory
        )
        artifact_store = ProductionArtifactStore(catalog)
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=None,
            recorder=cast(Any, _PostgresEvidenceRecorder(uow_factory, artifact_store)),
        )
        parent_bytes = b"quarantined archive parent fixture"
        parent_sha256 = hashlib.sha256(parent_bytes).hexdigest()
        parent_blob = await catalog.ingest(
            BytesIO(parent_bytes),
            logical_bucket=SAMPLES_QUARANTINE_BUCKET,
            mime_type="application/octet-stream",
        )
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            parent_sample = Sample(
                subject_id=investigation.subject_id,
                blob_id=parent_blob.id,
                original_name="parent.zip",
                origin="operator_import",
                acquired_at=datetime.now(UTC),
                license_restriction="internal-only",
                tlp=TLP.AMBER,
                do_not_submit=True,
                external_llm_allowed=False,
                origin_kind=EntitySampleOrigin.MANUAL,
                state=SampleState.QUARANTINED,
                source_service="operator_import",
                source_object_id=parent_sha256,
                expected_hash=parent_sha256,
            )
            parent_association = SampleAssociation(
                id=sample_association_id(investigation.id, parent_sha256),
                investigation_id=investigation.id,
                sample_sha256=parent_sha256,
                origin=SampleOrigin.RELATIONSHIP,
            )
            await uow.samples.add(parent_sample)
            await uow.sample_associations.append_version(parent_association, expected_version=0)
            await uow.sample_associations.link_sample(
                parent_association.id, parent_sample.id, expected_sha256=parent_sha256
            )
            await uow.commit()

        member_bytes = b"static child member fixture"
        member_sha256 = hashlib.sha256(member_bytes).hexdigest()
        child_blob, duplicate_child_blob = await asyncio.gather(
            catalog.ingest(
                BytesIO(member_bytes),
                logical_bucket=SAMPLES_QUARANTINE_BUCKET,
                mime_type="application/octet-stream",
            ),
            catalog.ingest(
                BytesIO(member_bytes),
                logical_bucket=SAMPLES_QUARANTINE_BUCKET,
                mime_type="application/octet-stream",
            ),
        )
        assert child_blob.id == duplicate_child_blob.id

        def candidate_sample() -> Sample:
            return Sample(
                subject_id=investigation.subject_id,
                blob_id=child_blob.id,
                original_name="preexisting-child.bin",
                origin="operator_import",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.RED,
                do_not_submit=False,
                external_llm_allowed=True,
                origin_kind=EntitySampleOrigin.MANUAL,
                state=SampleState.QUARANTINED,
                source_service="operator_import",
                source_object_id=member_sha256,
                expected_hash=member_sha256,
            )

        async def add_candidate(sample: Sample) -> Sample:
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                stored = await uow.samples.add_if_absent(sample)
                await uow.commit()
                return stored

        sample_one, sample_two = await asyncio.gather(
            add_candidate(candidate_sample()), add_candidate(candidate_sample())
        )
        assert sample_one.id == sample_two.id
        worker = _ArchiveMemberIntegrationWorker(catalog, member_bytes)
        extractor = ArchiveExtractMemberAdapter(
            hits=hits,
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            execution=cast(Any, worker),
            timeout_seconds=20,
            max_output_bytes=4096,
        )
        registry = InMemoryToolRegistry()
        register_acquisition_tools(
            registry,
            hits=hits,
            qualifications=SampleQualificationService(uow_factory),
            archive_extractor=extractor,
        )
        definition, _ = registry.get("archive.extract_member", "1")
        assert definition.schema_version == "1"
        assert "member_path" in definition.input_schema["properties"]
        context = ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=approved,
            budget=approved.budget,
            idempotency_key="archive-member-postgres-first",
            available_capabilities=frozenset({ARCHIVE_EXTRACT_MEMBER_CAPABILITY}),
            actor_id="analyst:integration",
        )
        arguments = {
            "association_id": str(parent_association.id),
            "member_path": "bin/loader.dll",
            "reason": "Retain the extracted member as a static candidate.",
        }
        dispatcher = RegistryToolDispatcher(registry)
        first = await dispatcher.dispatch(
            context,
            tool_name="archive.extract_member",
            schema_version="1",
            arguments=arguments,
            purpose="Extract a member for static analysis.",
            operation_key="archive-member-postgres:first",
        )
        second = await dispatcher.dispatch(
            replace(context, idempotency_key="archive-member-postgres-second"),
            tool_name="archive.extract_member",
            schema_version="1",
            arguments=arguments,
            purpose="Retry extraction after a confirmed result.",
            operation_key="archive-member-postgres:second",
        )

        child_sha256 = member_sha256
        assert first.status is ToolInvocationStatus.SUCCEEDED, first
        assert second.status is ToolInvocationStatus.SUCCEEDED, second
        assert second.output["association_id"] == first.output["association_id"]
        assert second.output["sample_id"] == first.output["sample_id"]
        assert second.output["reused_association"] is True
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            child_sample = await uow.samples.get(UUID(str(first.output["sample_id"])))
            child_blob = await uow.blobs.get(child_sample.blob_id) if child_sample else None
            associations = await uow.sample_associations.list_for_investigation(investigation.id)
            child_events = (
                await uow.provenance.list_for_aggregate("sample", child_sample.id)
                if child_sample is not None
                else ()
            )
        resolved = await hits.resolve_quarantined_sample(UUID(str(first.output["association_id"])))
        assert child_sample is not None and child_sample.state is SampleState.QUARANTINED
        assert (
            child_blob is not None
            and child_blob.descriptor.logical_bucket == SAMPLES_QUARANTINE_BUCKET
        )
        assert child_blob.descriptor.sha256 == child_sha256
        assert resolved.sha256 == child_sha256 and resolved.sample_id == child_sample.id
        assert sum(item.sample_sha256 == child_sha256 for item in associations) == 1
        derivations = [
            event
            for event in child_events
            if event.event_type == "sample_derived_from_archive_member"
        ]
        assert len(derivations) == 1
        assert derivations[0].payload["parent_sha256"] == parent_sha256
        assert derivations[0].tlp is TLP.RED
        assert derivations[0].payload["member_path"] == "bin/loader.dll"
        assert derivations[0].actor_id == "analyst:integration"
        assert derivations[0].payload["reason"] == arguments["reason"]
        assert child_sample.do_not_submit is True and child_sample.external_llm_allowed is False
        assert child_sample.tlp is TLP.RED
        assert child_sample.license_restriction == "internal-only"

        # A hash present only under another subject must not prelink the new
        # investigation association to that unrelated sample.
        foreign_investigation = await _make_investigation(session_factory)
        foreign_member = b"member initially known only to another subject"
        foreign_member_sha256 = hashlib.sha256(foreign_member).hexdigest()
        foreign_member_blob = await catalog.ingest(
            BytesIO(foreign_member),
            logical_bucket=SAMPLES_QUARANTINE_BUCKET,
            mime_type="application/octet-stream",
        )
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            foreign_sample = Sample(
                subject_id=foreign_investigation.subject_id,
                blob_id=foreign_member_blob.id,
                original_name="foreign-member.dll",
                origin="operator_import",
                acquired_at=datetime.now(UTC),
                license_restriction=None,
                tlp=TLP.AMBER,
                do_not_submit=False,
                external_llm_allowed=True,
                origin_kind=EntitySampleOrigin.MANUAL,
                state=SampleState.QUARANTINED,
                source_service="operator_import",
                source_object_id=foreign_member_sha256,
                expected_hash=foreign_member_sha256,
            )
            await uow.samples.add(foreign_sample)
            foreign_association = SampleAssociation(
                id=sample_association_id(foreign_investigation.id, foreign_member_sha256),
                investigation_id=foreign_investigation.id,
                sample_sha256=foreign_member_sha256,
                origin=SampleOrigin.RELATIONSHIP,
            )
            await uow.sample_associations.append_version(foreign_association, expected_version=0)
            await uow.sample_associations.link_sample(
                foreign_association.id,
                foreign_sample.id,
                expected_sha256=foreign_member_sha256,
            )
            await uow.commit()
        foreign_worker = _ArchiveMemberIntegrationWorker(catalog, foreign_member)
        foreign_extractor = ArchiveExtractMemberAdapter(
            hits=hits,
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            execution=cast(Any, foreign_worker),
            timeout_seconds=20,
            max_output_bytes=4096,
        )
        foreign_registry = InMemoryToolRegistry()
        register_acquisition_tools(
            foreign_registry,
            hits=hits,
            qualifications=SampleQualificationService(uow_factory),
            archive_extractor=foreign_extractor,
        )
        foreign_context = replace(context, idempotency_key="archive-member-postgres-foreign-hash")
        foreign_result = await RegistryToolDispatcher(foreign_registry).dispatch(
            foreign_context,
            tool_name="archive.extract_member",
            schema_version="1",
            arguments={
                **arguments,
                "member_path": "bin/foreign-collision.dll",
                "reason": "Associate the child with the current investigation subject.",
            },
            purpose="Extract a member whose hash exists for another subject.",
            operation_key="archive-member-postgres:foreign-hash",
        )
        assert foreign_result.status is ToolInvocationStatus.SUCCEEDED, foreign_result
        foreign_derived_id = UUID(str(foreign_result.output["sample_id"]))
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            foreign_derived_sample = await uow.samples.get(foreign_derived_id)
        assert foreign_derived_sample is not None
        assert foreign_derived_sample.subject_id == investigation.subject_id
        assert foreign_derived_sample.id != foreign_sample.id
        resolved_foreign = await hits.resolve_quarantined_sample(
            UUID(str(foreign_result.output["association_id"]))
        )
        assert resolved_foreign.sample_id == foreign_derived_id
        assert resolved_foreign.sha256 == foreign_member_sha256

        foreign_worker_calls = len(foreign_worker.requests)
        cross_investigation_result = await RegistryToolDispatcher(foreign_registry).dispatch(
            replace(
                context,
                envelope=None,
                idempotency_key="archive-member-postgres-cross-investigation",
            ),
            tool_name="archive.extract_member",
            schema_version="1",
            arguments={
                **arguments,
                "association_id": str(foreign_association.id),
                "member_path": "bin/cross-investigation.dll",
                "reason": "Refuse an association owned by investigation B.",
            },
            purpose="Verify archive association scope.",
            operation_key="archive-member-postgres:cross-investigation",
        )
        assert cross_investigation_result.status is ToolInvocationStatus.FAILED
        assert (
            cross_investigation_result.error_code
            == "archive_extract_member_association_not_in_investigation"
        )
        assert len(foreign_worker.requests) == foreign_worker_calls

        race_member = b"member whose commit is serialized with G2 revocation"
        race_worker = _ArchiveMemberIntegrationWorker(catalog, race_member)
        race_extractor = ArchiveExtractMemberAdapter(
            hits=hits,
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            execution=cast(Any, race_worker),
            timeout_seconds=20,
            max_output_bytes=4096,
        )
        race_registry = InMemoryToolRegistry()
        register_acquisition_tools(
            race_registry,
            hits=hits,
            qualifications=SampleQualificationService(uow_factory),
            archive_extractor=race_extractor,
        )
        lock_acquired = asyncio.Event()
        release_archive_commit = asyncio.Event()
        revoke_save_attempted = asyncio.Event()
        archive_backend_pid: int | None = None
        revoke_backend_pid: int | None = None
        original_get_for_update = SqlAlchemyInvestigationEnvelopeRepository.get_for_update
        original_save_if_version = SqlAlchemyInvestigationEnvelopeRepository.save_if_version

        async def pause_after_envelope_lock(
            repository: SqlAlchemyInvestigationEnvelopeRepository,
            investigation_id: UUID,
        ) -> Any:
            nonlocal archive_backend_pid
            result = await original_get_for_update(repository, investigation_id)
            task = asyncio.current_task()
            if (
                investigation_id == investigation.id
                and task is not None
                and task.get_name() == "archive-extract-awaiting-revoke"
            ):
                archive_backend_pid = await repository._session.scalar(
                    text("SELECT pg_backend_pid()")
                )
                lock_acquired.set()
                await release_archive_commit.wait()
            return result

        async def observe_revoke_save(
            repository: SqlAlchemyInvestigationEnvelopeRepository,
            envelope: Any,
            *,
            expected_version: int | None,
        ) -> Any:
            nonlocal revoke_backend_pid
            if envelope.investigation_id == investigation.id:
                revoke_backend_pid = await repository._session.scalar(
                    text("SELECT pg_backend_pid()")
                )
                revoke_save_attempted.set()
            return await original_save_if_version(
                repository, envelope, expected_version=expected_version
            )

        monkeypatch.setattr(
            SqlAlchemyInvestigationEnvelopeRepository,
            "get_for_update",
            pause_after_envelope_lock,
        )
        monkeypatch.setattr(
            SqlAlchemyInvestigationEnvelopeRepository,
            "save_if_version",
            observe_revoke_save,
        )
        race_context = replace(
            context,
            idempotency_key="archive-member-postgres-revoke-race",
        )
        race_task = asyncio.create_task(
            RegistryToolDispatcher(race_registry).dispatch(
                race_context,
                tool_name="archive.extract_member",
                schema_version="1",
                arguments={
                    **arguments,
                    "member_path": "bin/revoke-race.dll",
                    "reason": "Serialize this commit with a concurrent envelope revocation.",
                },
                purpose="Exercise the PostgreSQL envelope row lock.",
                operation_key="archive-member-postgres:revoke-race",
            ),
            name="archive-extract-awaiting-revoke",
        )
        revoke_task: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(lock_acquired.wait(), timeout=5)
            revoke_task = asyncio.create_task(
                envelope_service.revoke(
                    investigation.id,
                    expected_version=approved.version,
                    actor_id="analyst:integration-revoker",
                    reason="Revoke while archive derivation is committing.",
                    correlation_id=f"archive-member-revoke:{investigation.id}",
                )
            )
            await asyncio.wait_for(revoke_save_attempted.wait(), timeout=5)
            assert archive_backend_pid is not None
            assert revoke_backend_pid is not None
            loop = asyncio.get_running_loop()
            lock_wait_deadline = loop.time() + 5
            async with session_factory() as observer:
                blocking_pids: list[int] = []
                while (
                    archive_backend_pid not in blocking_pids
                    and loop.time() < lock_wait_deadline
                    and not revoke_task.done()
                ):
                    blocking_pids = list(
                        await observer.scalar(
                            text("SELECT pg_blocking_pids(:pid)"),
                            {"pid": revoke_backend_pid},
                        )
                        or []
                    )
                    if archive_backend_pid not in blocking_pids:
                        await asyncio.sleep(0.01)
            assert archive_backend_pid in blocking_pids
            assert not revoke_task.done()
        finally:
            release_archive_commit.set()
            pending_tasks = [race_task]
            if revoke_task is not None:
                pending_tasks.append(revoke_task)
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        assert race_task.result().status is ToolInvocationStatus.SUCCEEDED
        assert revoke_task is not None
        assert revoke_task.result().version == approved.version + 1
    finally:
        await engine.dispose()
