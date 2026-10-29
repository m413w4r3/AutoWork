from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application import investigation_acquisition_tools as acquisition_tools
from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisRequest,
    ConfinedAnalysisResult,
    ConfinedAnalysisTool,
    ConfinedAnalysisWorkerAttestation,
    archive_member_transfer_bucket,
)
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import (
    ARCHIVE_EXTRACT_MEMBER_CAPABILITY,
    LOCAL_ANALYSIS_CAPABILITY,
    LOCAL_QUALIFICATION_CAPABILITY,
    VT_FILE_DOWNLOAD_CAPABILITY,
    ArchiveExtractMemberAdapter,
    LocalSampleAnalysisService,
    SampleAnalysisOutcome,
    register_acquisition_tools,
)
from cti_app.application.investigation_governance import (
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.investigation_persistence import InMemorySampleAssociationRepository
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
    validate_json_schema,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.application.virustotal import VirusTotalHttpError
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SampleState
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    EvidenceKind,
    InvestigationBudget,
    SampleAssociation,
    SampleOrigin,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_state import StopReason
from tests.investigation_c2_fakes import (
    FakeBlob,
    FakeBlobDescriptor,
    FakeDatabase,
    FakeUow,
    FakeVirusTotalPort,
    make_approved_proposal,
    make_evidence_recorder,
    make_investigation,
    make_sample_acquisition_service,
)
from tests.test_investigation_service import _decision, _Fixture


class _AttestedAnalysisWorker:
    def __init__(self, worker_result: dict[str, Any]) -> None:
        self.available = True
        self.available_reason = None
        self.isolation_profile = "analysis-worker-v1"
        self.family_statuses = {
            "sample_analysis": ConfinedAnalysisFamilyStatus(
                "sample_analysis", True, "static+capa+smda"
            )
        }
        self.attestation = ConfinedAnalysisWorkerAttestation(
            reachable=True,
            confined=True,
            isolation_profile=self.isolation_profile,
            versions={"static": "static-v1", "capa": "9.4.0", "smda": "smda-v1"},
            families=self.family_statuses,
        )
        self.worker_result = worker_result
        self.requests: list[ConfinedAnalysisRequest] = []

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        self.requests.append(request)
        encoded = json.dumps(
            self.worker_result, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        operation_digest = hashlib.sha256(
            f"{request.sha256}:{request.tool.value}".encode()
        ).hexdigest()
        return ConfinedAnalysisResult(
            ToolInvocationStatus.SUCCEEDED,
            f"confined-analysis:{operation_digest}",
            request.artifact_id,
            request.sha256,
            result_blob_id=uuid4(),
            result_sha256=hashlib.sha256(encoded).hexdigest(),
            payload={"result": self.worker_result},
        )


class _ArchiveMemberWorker:
    def __init__(
        self,
        database: FakeDatabase,
        member: bytes,
        *,
        barrier: asyncio.Barrier | None = None,
    ) -> None:
        self.database = database
        self.member = member
        self.barrier = barrier
        self.requests: list[ConfinedAnalysisRequest] = []
        self.transfer_blob_ids: list[UUID] = []

    def seed_blob(self, content: bytes, bucket: str) -> UUID:
        digest = hashlib.sha256(content).hexdigest()
        blob_id = uuid5(UUID("6c3db404-61fe-4d1f-b1eb-638aa7b15b0a"), f"{bucket}:{digest}")
        self.database.blobs[blob_id] = FakeBlob(
            blob_id,
            FakeBlobDescriptor(bucket, digest, len(content)),
        )
        self.database.blob_bytes[blob_id] = content
        return blob_id

    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        self.requests.append(request)
        transfer_nonce = request.analysis_parameters["transfer_nonce"]
        transfer_bucket = archive_member_transfer_bucket(transfer_nonce)
        transfer_blob_id = self.seed_blob(self.member, transfer_bucket)
        self.transfer_blob_ids.append(transfer_blob_id)
        if self.barrier is not None:
            await self.barrier.wait()
        child_sha256 = hashlib.sha256(self.member).hexdigest()
        payload = {
            "investigation_id": str(request.investigation_id),
            "association_id": str(request.association_id),
            "sample_id": str(request.sample_id),
            "artifact_sha256": request.sha256,
            "result": {
                "member_status": "extracted",
                "member_path": request.analysis_parameters["member_path"],
                "member_sha256": child_sha256,
                "member_size": len(self.member),
                "transfer_blob_id": str(transfer_blob_id),
            },
        }
        return ConfinedAnalysisResult(
            ToolInvocationStatus.SUCCEEDED,
            "confined-analysis:archive-member",
            request.artifact_id,
            request.sha256,
            payload=payload,
        )


class _RaisingArchiveMemberWorker:
    async def execute(self, request: ConfinedAnalysisRequest) -> ConfinedAnalysisResult:
        del request
        raise RuntimeError("synthetic worker failure")


def test_archive_member_transfer_bucket_fits_blob_address_limit() -> None:
    bucket = archive_member_transfer_bucket("a" * 64)

    assert len(bucket) <= 63
    assert (
        BlobDescriptor(
            sha256="b" * 64,
            size=1,
            mime_type="application/octet-stream",
            logical_bucket=bucket,
        ).logical_bucket
        == bucket
    )


class _FakeQuarantineArtifactStore:
    def __init__(
        self, database: FakeDatabase, *, after_put: Callable[[], None] | None = None
    ) -> None:
        self.database = database
        self.after_put = after_put
        self.deleted_blob_ids: list[UUID] = []

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.database.blob_bytes[blob_id]
        if len(content) > max_bytes:
            raise ValueError("blob exceeds requested test bound")
        return content

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        del mime_type
        blob_id = uuid5(
            UUID("6c3db404-61fe-4d1f-b1eb-638aa7b15b0a"),
            f"{bucket}:{hashlib.sha256(content).hexdigest()}",
        )
        self.database.blobs[blob_id] = FakeBlob(
            blob_id,
            FakeBlobDescriptor(bucket, hashlib.sha256(content).hexdigest(), len(content)),
        )
        self.database.blob_bytes[blob_id] = content
        if self.after_put is not None:
            self.after_put()
        return blob_id

    async def delete_unreferenced(self, blob_id: UUID) -> None:
        self.deleted_blob_ids.append(blob_id)
        self.database.blobs.pop(blob_id, None)
        self.database.blob_bytes.pop(blob_id, None)


class _ResolvingSampleAnalyzer:
    def __init__(self, hits: HitAcquisitionService) -> None:
        self.hits = hits
        self.resolved = None

    async def analyze(
        self, *, association_id: UUID, invocation_ref: UUID | None
    ) -> SampleAnalysisOutcome:
        del invocation_ref
        self.resolved = await self.hits.resolve_quarantined_sample(association_id)
        return SampleAnalysisOutcome(
            ToolInvocationStatus.SUCCEEDED,
            (),
            {"sample_sha256": self.resolved.sha256},
            "Resolved quarantined sample for analysis.",
        )


def _worker_sample_result(*, status: str, string_value: str) -> dict[str, Any]:
    return {
        "status": status,
        "families": {
            "static": {
                "status": "SUCCEEDED",
                "version": "static-v1",
                "strings_observed": 1,
                "strings": [{"value": string_value, "encoding": "ascii", "offset": 4}],
            },
            "capa": {"status": "SUCCEEDED", "version": "9.4.0", "capabilities": []},
            "smda": {
                "status": "SUCCEEDED",
                "version": "smda-v1",
                "architecture": "x86",
                "code_features": {"packing": {"packed": False}, "ngrams": []},
            },
            "floss": {"status": "SUCCEEDED", "version": "3.1.1", "strings": []},
        },
    }


def _register(database: FakeDatabase, hits: HitAcquisitionService):
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
    )
    return registry


class _RouteRefusedPort(FakeVirusTotalPort):
    async def file_download(self, file_hash: str, *, sink) -> None:
        self.download_calls.append(file_hash)
        raise VirusTotalHttpError(
            "synthetic route absent",
            code="virustotal_not_found",
            status_code=404,
        )


class _PartialDownloadTimeoutPort(FakeVirusTotalPort):
    async def file_download(self, file_hash: str, *, sink) -> None:
        self.download_calls.append(file_hash)
        sink.write(self.sample_bytes[:8])
        raise TimeoutError("synthetic partial download timeout")


class _NoExistingSamples:
    async def list_by_subject_and_sha256(self, subject_id: UUID, sha256: str) -> tuple[Any, ...]:
        del subject_id, sha256
        return ()


@pytest.mark.asyncio
async def test_tool_capabilities_and_unavailable_analysis_are_explicit() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve bounded VT access.",
        correlation_id="g2:tools",
    )
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=None,
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    download_definition, _ = registry.get("vt.download_sample", "1")
    analyze_definition, _ = registry.get("sample.analyze", "1")
    qualification_definition, _ = registry.get("sample.set_qualification", "1")
    assert download_definition.capability_key == VT_FILE_DOWNLOAD_CAPABILITY
    assert download_definition.effect is ToolEffect.ACQUIRE
    assert analyze_definition.capability_key == LOCAL_ANALYSIS_CAPABILITY
    assert qualification_definition.capability_key == LOCAL_QUALIFICATION_CAPABILITY
    validate_json_schema(
        {
            "association_id": str(UUID(int=1)),
            "qualification": "uncertain",
            "reason": "Keep the relationship tentative.",
            "evidence_refs": [],
        },
        qualification_definition.input_schema,
    )
    with pytest.raises(ValueError):
        validate_json_schema(
            {
                "association_id": str(UUID(int=1)),
                "qualification": "confirmed",
                "reason": "The agent cannot make G3.",
                "evidence_refs": [],
            },
            qualification_definition.input_schema,
        )

    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:analyze:unavailable",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )
    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(UUID(int=2)),
            "purpose": "Analyze quarantined sample",
            "evidence_refs": [],
        },
        purpose="Analyze quarantined sample",
        operation_key="op:sample:analyze:unavailable",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    missing_capability_context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:analyze:missing-capability",
        available_capabilities=frozenset(),
    )
    missing_capability_result = await RegistryToolDispatcher(registry).dispatch(
        missing_capability_context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(UUID(int=2)),
            "purpose": "Capability absence check",
            "evidence_refs": [],
        },
        purpose="Capability absence check",
        operation_key="op:sample:analyze:missing-capability",
    )
    assert missing_capability_result.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_archive_member_registration_is_candidate_and_resolves_for_sample_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:archive-test",
        reason="Approve bounded local archive analysis.",
        correlation_id="g2:archive-member",
    )
    parent_bytes = b"quarantined archive fixture"
    parent_sha256 = hashlib.sha256(parent_bytes).hexdigest()
    parent_blob_id = uuid4()
    database.blobs[parent_blob_id] = FakeBlob(
        parent_blob_id,
        FakeBlobDescriptor(SAMPLES_QUARANTINE_BUCKET, parent_sha256, len(parent_bytes)),
    )
    database.blob_bytes[parent_blob_id] = parent_bytes
    parent_sample = Sample(
        subject_id=investigation.subject_id,
        blob_id=parent_blob_id,
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
    database.samples[parent_sample.id] = parent_sample
    parent_association = SampleAssociation(
        investigation_id=investigation.id,
        sample_sha256=parent_sha256,
        origin=SampleOrigin.RELATIONSHIP,
    )
    database.associations[parent_association.id] = parent_association

    member_bytes = b"static member fixture bytes"
    member_sha256 = hashlib.sha256(member_bytes).hexdigest()
    preexisting_child_blob_id = uuid5(
        UUID("6c3db404-61fe-4d1f-b1eb-638aa7b15b0a"),
        f"{SAMPLES_QUARANTINE_BUCKET}:{member_sha256}",
    )
    database.blobs[preexisting_child_blob_id] = FakeBlob(
        preexisting_child_blob_id,
        FakeBlobDescriptor(SAMPLES_QUARANTINE_BUCKET, member_sha256, len(member_bytes)),
    )
    database.blob_bytes[preexisting_child_blob_id] = member_bytes
    preexisting_child = Sample(
        subject_id=investigation.subject_id,
        blob_id=preexisting_child_blob_id,
        original_name="preexisting-red-child.bin",
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
    database.samples[preexisting_child.id] = preexisting_child

    hits = HitAcquisitionService(
        lambda: FakeUow(database), acquisition=None, recorder=make_evidence_recorder(database)
    )
    worker = _ArchiveMemberWorker(database, member_bytes, barrier=asyncio.Barrier(2))
    store = _FakeQuarantineArtifactStore(database)
    extractor = ArchiveExtractMemberAdapter(
        hits=hits,
        uow_factory=lambda: FakeUow(database),
        artifact_store=cast(ProductionArtifactStore, store),
        execution=cast(Any, worker),
        timeout_seconds=20,
        max_output_bytes=4096,
    )
    analyzer = _ResolvingSampleAnalyzer(hits)
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
        archive_extractor=extractor,
    )
    definition, _ = registry.get("archive.extract_member", "1")
    assert definition.schema_version == "1"
    assert definition.capability_key == ARCHIVE_EXTRACT_MEMBER_CAPABILITY
    assert definition.effect is ToolEffect.LOCAL_COMPUTE
    assert set(definition.input_schema["required"]) == {
        "association_id",
        "member_path",
        "reason",
    }

    dispatcher = RegistryToolDispatcher(registry)
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="archive-member-first",
        available_capabilities=frozenset(
            {ARCHIVE_EXTRACT_MEMBER_CAPABILITY, LOCAL_ANALYSIS_CAPABILITY}
        ),
        actor_id="analyst:archive-test",
    )
    arguments = {
        "association_id": str(parent_association.id),
        "member_path": "bin/loader.dll",
        "reason": "Inspect the archive member as a static candidate.",
        "expected_sha256": hashlib.sha256(member_bytes).hexdigest(),
    }
    first, second = await asyncio.gather(
        extractor.execute(arguments, context),
        extractor.execute(
            {
                **arguments,
                "reason": "Compare the same member against a separate analyst question.",
            },
            replace(context, actor_id="analyst:archive-reviewer"),
        ),
    )

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert second.status is ToolInvocationStatus.SUCCEEDED
    assert first.output["qualification"] == "candidate"
    assert second.output["association_id"] == first.output["association_id"]
    assert second.output["sample_id"] == first.output["sample_id"]
    # The two calls run concurrently on a fake without row locks, so which one observes the
    # other's association is unspecified; sequential reuse is asserted by the PostgreSQL test.
    assert isinstance(second.output["reused_association"], bool)
    assert (
        worker.requests[0].analysis_parameters["transfer_nonce"]
        == worker.requests[1].analysis_parameters["transfer_nonce"]
    )
    assert len(set(worker.transfer_blob_ids)) == 1
    transfer_blob_id = worker.transfer_blob_ids[0]
    assert transfer_blob_id in database.blobs
    assert await store.read_bytes(transfer_blob_id, max_bytes=len(member_bytes)) == member_bytes
    assert store.deleted_blob_ids == []
    derived_sha256 = hashlib.sha256(member_bytes).hexdigest()
    derived_associations = [
        item
        for item in database.associations.values()
        if item.investigation_id == investigation.id and item.sample_sha256 == derived_sha256
    ]
    assert len(derived_associations) == 1
    derived_sample = database.samples[UUID(str(first.output["sample_id"]))]
    assert derived_sample.state is SampleState.QUARANTINED
    assert derived_sample.tlp is TLP.RED
    assert derived_sample.do_not_submit is True
    assert derived_sample.external_llm_allowed is False
    assert derived_sample.license_restriction == "internal-only"
    provenance = [
        item
        for item in database.provenance
        if item.aggregate_id == derived_sample.id
        and item.event_type == "sample_derived_from_archive_member"
    ]
    assert len(provenance) == 2
    assert all(item.tlp is TLP.RED for item in provenance)
    assert all(item.payload["parent_sha256"] == parent_sha256 for item in provenance)
    assert all(item.payload["member_path"] == "bin/loader.dll" for item in provenance)
    assert {(item.actor_id, item.payload["reason"]) for item in provenance} == {
        ("analyst:archive-test", arguments["reason"]),
        (
            "analyst:archive-reviewer",
            "Compare the same member against a separate analyst question.",
        ),
    }

    failed_member = b"member invalidated by a changed envelope"
    failed_worker = _ArchiveMemberWorker(database, failed_member)
    failed_store = _FakeQuarantineArtifactStore(
        database,
        after_put=lambda: database.envelopes.__setitem__(
            investigation.id, replace(envelope, version=envelope.version + 1)
        ),
    )
    failing_extractor = ArchiveExtractMemberAdapter(
        hits=hits,
        uow_factory=lambda: FakeUow(database),
        artifact_store=cast(ProductionArtifactStore, failed_store),
        execution=cast(Any, failed_worker),
        timeout_seconds=20,
        max_output_bytes=4096,
    )
    failed_result = await failing_extractor.execute(
        {
            "association_id": str(parent_association.id),
            "member_path": "bin/revoked.dll",
            "reason": "This extraction must not persist after a policy change.",
        },
        replace(context, idempotency_key="archive-member-revoked"),
    )
    assert failed_result.status is ToolInvocationStatus.NEEDS_REVIEW
    failed_quarantine_id = uuid5(
        UUID("6c3db404-61fe-4d1f-b1eb-638aa7b15b0a"),
        f"{SAMPLES_QUARANTINE_BUCKET}:{hashlib.sha256(failed_member).hexdigest()}",
    )
    assert failed_quarantine_id in database.blobs
    assert database.blob_bytes[failed_quarantine_id] == failed_member
    assert failed_quarantine_id not in failed_store.deleted_blob_ids

    analyzed = await dispatcher.dispatch(
        replace(context, idempotency_key="sample-analyze-derived"),
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(first.output["association_id"]),
            "purpose": "Resolve the derived sample through the analysis tool.",
            "evidence_refs": [],
        },
        purpose="Resolve the derived sample through the analysis tool.",
        operation_key="sample-analyze-derived",
    )
    assert analyzed.status is ToolInvocationStatus.SUCCEEDED
    assert analyzer.resolved is not None
    assert analyzer.resolved.sha256 == derived_sha256

    other_investigation = make_investigation()
    database.investigations[other_investigation.id] = other_investigation
    other_parent_bytes = b"parent sample owned by investigation B"
    other_parent_sha256 = hashlib.sha256(other_parent_bytes).hexdigest()
    other_parent_blob_id = uuid4()
    database.blobs[other_parent_blob_id] = FakeBlob(
        other_parent_blob_id,
        FakeBlobDescriptor(
            SAMPLES_QUARANTINE_BUCKET,
            other_parent_sha256,
            len(other_parent_bytes),
        ),
    )
    database.blob_bytes[other_parent_blob_id] = other_parent_bytes
    other_parent_sample = Sample(
        subject_id=other_investigation.subject_id,
        blob_id=other_parent_blob_id,
        original_name="other-parent.zip",
        origin="operator_import",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=False,
        external_llm_allowed=True,
        origin_kind=EntitySampleOrigin.MANUAL,
        state=SampleState.QUARANTINED,
        source_service="operator_import",
        source_object_id=other_parent_sha256,
        expected_hash=other_parent_sha256,
    )
    database.samples[other_parent_sample.id] = other_parent_sample
    other_parent_association = SampleAssociation(
        investigation_id=other_investigation.id,
        sample_sha256=other_parent_sha256,
        origin=SampleOrigin.RELATIONSHIP,
    )
    database.associations[other_parent_association.id] = other_parent_association
    cross_investigation_context = replace(
        context,
        envelope=None,
        idempotency_key="archive-member-cross-investigation",
        budget=InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 1}),
    )
    worker_calls_before_cross_investigation = len(worker.requests)
    cross_investigation_result = await dispatcher.dispatch(
        cross_investigation_context,
        tool_name="archive.extract_member",
        schema_version="1",
        arguments={**arguments, "association_id": str(other_parent_association.id)},
        purpose="Reject an association owned by another investigation.",
        operation_key="archive-member:cross-investigation",
    )
    assert cross_investigation_result.status is ToolInvocationStatus.FAILED
    assert (
        cross_investigation_result.error_code
        == "archive_extract_member_association_not_in_investigation"
    )
    assert len(worker.requests) == worker_calls_before_cross_investigation

    failing_extractor = ArchiveExtractMemberAdapter(
        hits=hits,
        uow_factory=lambda: FakeUow(database),
        artifact_store=cast(ProductionArtifactStore, store),
        execution=cast(Any, _RaisingArchiveMemberWorker()),
        timeout_seconds=20,
        max_output_bytes=4096,
    )
    failing_registry = InMemoryToolRegistry()
    register_acquisition_tools(
        failing_registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        archive_extractor=failing_extractor,
    )
    monotonic_values = iter((100.0, 102.1))
    monkeypatch.setattr(
        acquisition_tools,
        "time",
        SimpleNamespace(monotonic=lambda: next(monotonic_values)),
    )
    failure_budget = InvestigationBudget(limits={BudgetDimension.TOOL_SECONDS: 1})
    failure_result = await RegistryToolDispatcher(failing_registry).dispatch(
        replace(
            context,
            envelope=None,
            budget=failure_budget,
            idempotency_key="archive-member-worker-failure",
        ),
        tool_name="archive.extract_member",
        schema_version="1",
        arguments=arguments,
        purpose="Account for worker time when execution raises.",
        operation_key="archive-member:worker-failure",
    )
    assert failure_result.status is ToolInvocationStatus.FAILED
    assert failure_result.exception_class == "RuntimeError"
    assert failure_budget.reservations[0].committed_amounts[BudgetDimension.TOOL_SECONDS] == 3


@pytest.mark.asyncio
async def test_invalid_download_arguments_are_definitive_local_failures() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:synthetic",
        reason="Approve bounded synthetic VT access.",
        correlation_id="g2:invalid-download-arguments",
    )
    port = FakeVirusTotalPort(b"synthetic payload")
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    dispatcher = RegistryToolDispatcher(registry)
    valid_hash = hashlib.sha256(b"synthetic payload").hexdigest()
    cases = (
        {"sha256": valid_hash, "purpose": ["first", "second"], "evidence_refs": []},
        {"sha256": valid_hash, "purpose": "Review this sample."},
        {
            "sha256": valid_hash,
            "purpose": "Review this sample.",
            "evidence_refs": [],
            "unexpected": True,
        },
    )

    for index, arguments in enumerate(cases):
        result = await dispatcher.dispatch(
            ToolExecutionContext(
                investigation_id=investigation.id,
                envelope=envelope,
                budget=envelope.budget,
                idempotency_key=f"invalid-download-{index}",
                available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
            ),
            tool_name="vt.download_sample",
            schema_version="1",
            arguments=arguments,
            purpose="Validate the real acquisition schema.",
            operation_key=f"invalid-download-{index}",
        )
        assert result.status is ToolInvocationStatus.FAILED
        assert result.error_code == "invalid_tool_arguments"
        assert result.output["validation_errors"]

    assert port.download_calls == []


@pytest.mark.asyncio
async def test_invalid_download_retry_is_bounded() -> None:
    database = FakeDatabase()
    fixture = _Fixture(
        responses=[
            _decision(
                goal="Validate sample arguments.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": {
                            "sha256": "a" * 64,
                            "purpose": ["first phrase", "second phrase"],
                            "evidence_refs": [],
                        },
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Retry the same invalid sample arguments.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": {
                            "sha256": "a" * 64,
                            "purpose": ["first phrase", "second phrase"],
                            "evidence_refs": [],
                        },
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Repeat the same invalid request after two local rejections.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": {
                            "sha256": "a" * 64,
                            "purpose": ["first phrase", "second phrase"],
                            "evidence_refs": [],
                        },
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Finish after the repeated rejection is reported.",
                next_action="finish",
            ),
        ],
        tools=(
            (
                "vt.download_sample",
                "vt",
                ToolEffect.ACQUIRE,
                VT_FILE_DOWNLOAD_CAPABILITY,
                {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1},
            ),
        ),
        no_progress_limit=6,
    )
    fixture.envelope = replace(fixture.envelope, acquire_hits_allowed=True)
    database.investigations[fixture.investigation_id] = make_investigation()
    port = FakeVirusTotalPort(b"synthetic sample")
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    actual_registry = _register(database, hits)
    definition, adapter = actual_registry.get("vt.download_sample", "1")
    fixture.registry.unregister("vt.download_sample", "1")
    fixture.registry.register(definition, adapter)

    initial = await fixture.start()
    state = await fixture.service.run(initial.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(fixture.investigation_id)
    assert state.stop_reason is StopReason.AGENT_FINISHED
    assert len(invocations) == 2, [
        (item.operation_key, item.normalized_parameters, item.status, item.response_refs)
        for item in invocations
    ]
    assert all(item.status is ToolInvocationStatus.FAILED for item in invocations)
    assert all(item.response_refs == () for item in invocations)
    assert all(item.consumed_budget == {} for item in invocations)
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert envelope is not None
    reservations = {item.operation_key: item for item in envelope.budget.reservations}
    assert all(
        reservations[item.operation_key].status is BudgetReservationStatus.RELEASED
        for item in invocations
    )
    assert port.download_calls == []
    assert len(fixture.bridge_transport.prompts) == 4
    assert "invalid_tool_arguments" in fixture.bridge_transport.prompts[1]
    assert "purpose" in fixture.bridge_transport.prompts[1]
    assert "expected string" in fixture.bridge_transport.prompts[1]
    assert "failed definitively twice" in fixture.bridge_transport.prompts[3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reported_status",
    (ToolInvocationStatus.NEEDS_REVIEW, ToolInvocationStatus.FAILED),
)
async def test_ambiguous_download_failure_stops_without_persisting_adapter_output(
    reported_status: ToolInvocationStatus,
) -> None:
    database = FakeDatabase()
    sample = b"synthetic acquisition timeout sample"
    sha256 = hashlib.sha256(sample).hexdigest()
    arguments = {"sha256": sha256, "purpose": "Request the synthetic sample.", "evidence_refs": []}
    fixture = _Fixture(
        responses=[
            _decision(
                goal="Request the synthetic sample.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": arguments,
                        "purpose": "Request the synthetic sample.",
                    },
                ),
            ),
            _decision(
                goal="Retry the same sample download.",
                next_action="continue",
                tools=(
                    {
                        "tool_name": "vt.download_sample",
                        "schema_version": "1",
                        "arguments": arguments,
                        "purpose": "Retry the synthetic sample.",
                    },
                ),
            ),
        ],
        tools=(
            (
                "vt.download_sample",
                "vt",
                ToolEffect.ACQUIRE,
                VT_FILE_DOWNLOAD_CAPABILITY,
                {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1},
            ),
        ),
    )
    fixture.envelope = replace(fixture.envelope, acquire_hits_allowed=True)
    source_associations = InMemorySampleAssociationRepository()
    fixture.service._sample_associations = source_associations
    fixture.service._samples = cast(Any, _NoExistingSamples())
    database.investigations[fixture.investigation_id] = make_investigation()
    port = _PartialDownloadTimeoutPort(sample)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(fixture.investigation_id, sha256, SampleOrigin.VT_PIVOT)
    await source_associations.append_version(association, expected_version=0)
    actual_registry = _register(database, hits)
    definition, adapter = actual_registry.get("vt.download_sample", "1")
    fixture.registry.unregister("vt.download_sample", "1")

    class _PersistedEnvelopeAdapter:
        async def execute(
            self, call_arguments: Mapping[str, Any], context: ToolExecutionContext
        ) -> ToolResult:
            # Production repositories share this versioned envelope snapshot.
            database.envelopes[context.investigation_id] = context.envelope
            result = cast(ToolResult, await adapter.execute(call_arguments, context))
            if reported_status is ToolInvocationStatus.FAILED:
                return replace(
                    result,
                    status=reported_status,
                    output={**result.output, "adapter_detail": "partial response is ambiguous"},
                )
            return result

    fixture.registry.register(definition, _PersistedEnvelopeAdapter())

    initial = await fixture.start()
    state = await fixture.service.run(initial.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(fixture.investigation_id)
    envelope = await fixture.envelopes.get(fixture.investigation_id)
    assert state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.NEEDS_REVIEW
    # The framework records only a fixed diagnostic; the adapter's partial
    # output is never promoted into evidence.
    refs = invocations[0].response_refs
    assert len(refs) == 1
    diagnostics = await fixture.observations.list_for_investigation(fixture.investigation_id)
    assert len(diagnostics) == 1
    assert diagnostics[0].id == refs[0]
    assert diagnostics[0].object_type in {"tool_call", "tool_result"}
    assert diagnostics[0].source == "vt.download_sample"
    assert invocations[0].consumed_budget == {}
    assert envelope is not None
    reservation = next(
        item
        for item in envelope.budget.reservations
        if item.operation_key == invocations[0].operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert reservation.committed_amounts == {}
    assert envelope.budget.reserved[BudgetDimension.VT_UNITS] == 1
    assert envelope.budget.reserved[BudgetDimension.DOWNLOADS] == 1
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0
    assert port.download_calls == [sha256]
    assert fixture.bridge_transport.prompts
    assert len(fixture.bridge_transport.prompts) == 1


@pytest.mark.asyncio
async def test_download_tool_requests_permission_outside_g2_envelope() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"request-permission sample"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation, allowed_providers=(), acquire_hits_allowed=False),
        actor_id="analyst:alice",
        reason="Local analysis only; defer VT acquisition.",
        correlation_id="g2:local-only",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:download:out-of-envelope",
        available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
    )
    _definition, adapter = registry.get("vt.download_sample", "1")

    adapter_result = await adapter.execute(
        {
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "Request an approved acquisition",
            "evidence_refs": [],
        },
        context,
    )
    dispatched_result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments={
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "Request an approved acquisition",
            "evidence_refs": [],
        },
        purpose="Request an approved acquisition",
        operation_key="op:download:out-of-envelope",
    )

    assert adapter_result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert adapter_result.output["request_permission"] is True
    # The existing dispatcher rejects ACQUIRE before reaching the adapter.
    assert dispatched_result.status is ToolInvocationStatus.POLICY_DENIED
    assert port.download_calls == []


@pytest.mark.asyncio
async def test_download_tool_does_not_cache_a_404_with_unknown_response_origin() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:synthetic",
        reason="Approve bounded synthetic VT acquisition.",
        correlation_id="g2:synthetic-route-refusal",
    )
    content = b"synthetic sample for download route refusal"
    port = _RouteRefusedPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    _definition, adapter = registry.get("vt.download_sample", "1")
    arguments = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "purpose": "Review a synthetic hit",
        "evidence_refs": [],
    }

    first = await adapter.execute(
        arguments,
        ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="synthetic-download-attempt-1",
            available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
        ),
    )
    second = await adapter.execute(
        arguments,
        ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=envelope,
            budget=envelope.budget,
            idempotency_key="synthetic-download-attempt-2",
            available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
        ),
    )

    assert first.status is ToolInvocationStatus.UNAVAILABLE
    assert first.output["error_code"] == "virustotal_download_404_origin_unknown"
    assert first.output["reason"] == "provider_download_404_origin_unknown"
    assert first.output["confirmed_non_execution"] is True
    assert "synthetic route absent" not in first.message
    assert second.output.get("cached", False) is False
    assert port.download_calls == [arguments["sha256"], arguments["sha256"]]


@pytest.mark.asyncio
async def test_dispatcher_download_reuses_one_g2_reservation_on_replay() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"dispatcher idempotency fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve one hash-verified hit download.",
        correlation_id="g2:dispatcher-download",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    dispatcher = RegistryToolDispatcher(registry)
    operation_key = "vt.download_sample:stable-operation-1"
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key=operation_key,
        available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
    )
    arguments = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "purpose": "Review this VT pivot.",
        "evidence_refs": [],
    }

    first = await dispatcher.dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Review this VT pivot.",
        operation_key=operation_key,
    )
    replay = await dispatcher.dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Review this VT pivot.",
        operation_key=operation_key,
    )

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert replay == first
    assert port.download_calls == [arguments["sha256"]]
    assert len(database.attempts) == 1
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    assert len(database.decisions) == 1


@pytest.mark.asyncio
async def test_sample_analyze_records_bounded_binary_observations() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"MZ harmless static analysis fixture strings"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow bounded candidate acquisition and local analysis.",
        correlation_id="g2:analysis",
    )
    port = FakeVirusTotalPort(content)
    recorder = make_evidence_recorder(database)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=recorder,
    )
    sha256 = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(association.id, purpose="Review candidate fixture")
    assert acquired.sample_id is not None
    worker = _AttestedAnalysisWorker(
        _worker_sample_result(status="PARTIAL", string_value="harmless static analysis fixture")
    )

    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: FakeUow(database),
        recorder,
        confined_analysis=worker,  # type: ignore[arg-type]
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
    )
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:sample:analysis",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Extract local static strings",
            "evidence_refs": [],
        },
        purpose="Extract local static strings",
        operation_key="op:sample:analysis",
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["sample_sha256"] == sha256
    assert result.output["qualification"] == "candidate"
    assert len(worker.requests) == 1
    assert worker.requests[0].tool is ConfinedAnalysisTool.SAMPLE_ANALYSIS
    assert worker.requests[0].sha256 == sha256
    assert result.output["extractors"]["static"]["version"] == "static-v1"
    assert result.output["engines_executed"] == ("capa", "floss", "smda", "static")
    assert result.output["engine_statuses"]["static"] == "SUCCEEDED"
    assert result.output["partial_reason"] == (
        "analysis_worker_status:partial; "
        "goodware_measurement_unavailable:goodware_measurement_service_unavailable"
    )
    assert result.output["strings_recorded"] == 1
    assert result.output["goodware"]["available"] is False
    assert {
        item.kind
        for item in database.observations.values()
        if item.kind in {EvidenceKind.BINARY_STRING, EvidenceKind.BINARY_ANALYSIS}
    } == {
        EvidenceKind.BINARY_STRING,
        EvidenceKind.BINARY_ANALYSIS,
    }
    assert len(result.observation_refs) == 2


@pytest.mark.asyncio
async def test_sample_analyze_injects_code_features_and_goodware_measurement() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"MZ harmless code feature and goodware fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow bounded candidate acquisition and local analysis.",
        correlation_id="g2:code-features-goodware",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(
        investigation.id,
        hashlib.sha256(content).hexdigest(),
        SampleOrigin.VT_PIVOT,
    )
    acquired = await hits.acquire_hit(association.id, purpose="Review candidate fixture")
    assert acquired.sample_id is not None

    baseline_id = uuid4()

    class BaselineLookup:
        async def get(self, _investigation_id):
            return baseline_id

    class AnalysisUow(FakeUow):
        async def __aenter__(self):
            await super().__aenter__()
            self.investigation_goodware_baselines = BaselineLookup()
            return self

    class ExistingGoodwareService:
        prepared = None
        features = ()

        async def prepare(self, baseline):
            self.prepared = baseline

        async def lookup_batch(self, baseline, features):
            self.features = tuple(features)
            return {self.features[0]: 4} if self.features else {}

    goodware = ExistingGoodwareService()
    worker = _AttestedAnalysisWorker(
        _worker_sample_result(status="SUCCEEDED", string_value="fixture string")
    )
    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: AnalysisUow(database),
        make_evidence_recorder(database),
        goodware=goodware,
        confined_analysis=worker,  # type: ignore[arg-type]
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
    )
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:sample:code-features-goodware",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Compare local code features with the bound goodware corpus",
            "evidence_refs": [],
        },
        purpose="Run local feature extraction and goodware comparison.",
        operation_key="op:sample:code-features-goodware",
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["extractors"]["smda"]["version"] == "smda-v1"
    assert result.output["code_features"]["packing"]["packed"] is False
    assert result.output["goodware"]["available"] is True
    assert result.output["goodware"]["features_present_in_goodware"] == 1
    assert worker.requests[0].tool is ConfinedAnalysisTool.SAMPLE_ANALYSIS
    assert goodware.prepared == baseline_id
    assert goodware.features == (("string", "fixture string"),)
    analysis_observation = next(
        database.observations[reference]
        for reference in result.observation_refs
        if database.observations[reference].kind is EvidenceKind.BINARY_ANALYSIS
    )
    assert analysis_observation.source_version != "unknown"
