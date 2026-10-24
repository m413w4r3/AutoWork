from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
    ConfinedAnalysisWorkerAttestation,
)
from cti_app.application.evidence_recorder import EvidenceKind
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import (
    LOCAL_ANALYSIS_CAPABILITY,
    LocalSampleAnalysisService,
    register_acquisition_tools,
)
from cti_app.application.investigation_governance import (
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
)
from cti_app.application.jobs import DuplicateJobError, JobExecutionContext
from cti_app.application.rule_workbench import RuleWorkbench, rule_validation_reason
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.goodware import NonDiscriminantPatternRegistry
from cti_app.domain.investigation import SampleOrigin, ToolInvocationStatus
from cti_app.domain.jobs import JobStatus
from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.rule_workbench import (
    RuleConditionStrategy,
    RuleMotif,
    RuleMotifKind,
    RuleMotifProvenanceKind,
    RuleSampleProvenance,
    RuleTestCase,
    RuleTestCorpusRole,
    RuleValidationStatus,
)
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
)
from cti_app.infrastructure.confined_analysis_dispatch import (
    DramatiqConfinedAnalysisExecutionPort,
    _attestation_from_response,
)
from cti_app.workers.analysis_tasks import build_confined_analysis_job_handler
from cti_app.workers.confined_analysis import (
    ConfinedAnalysisArtifactMismatch,
    _extract_monodis_method_body,
    _parse_monodis_method_table,
    _run_capa,
    _run_floss,
    _run_local_command,
    build_analysis_worker_probe_payload,
    execute_confined_analysis_job,
)
from tests.confined_worker_harness import InProcessAnalysisDispatcher
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

_MARKER = "SAFE_CONFined_ANALYSIS_FIXTURE"


def _floss_parameters(*, max_output_bytes: int = 8_000_000) -> ConfinedAnalysisJobParameters:
    return ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256="a" * 64,
        operation_key="confined-analysis:" + "b" * 64,
        tool=ConfinedAnalysisTool.FLOSS,
        timeout_seconds=30,
        max_output_bytes=max_output_bytes,
        analysis_parameters={"max_strings": "128"},
    )


@pytest.mark.asyncio
async def test_floss_output_limit_is_a_partial_result_with_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def limited(*_args, **_kwargs):
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.OUTPUT_LIMIT, None, b"{", b"")

    monkeypatch.setattr("cti_app.workers.confined_analysis.shutil.which", lambda _name: "/floss")
    monkeypatch.setattr("cti_app.workers.confined_analysis.run_analysis_subprocess", limited)

    result = await _run_floss(
        _floss_parameters(),
        b"harmless static analysis fixture",
        Settings(_env_file=None),
    )

    assert result["status"] == "PARTIAL"
    assert result["truncated"] is True
    assert result["available_reason"] == "floss_output_truncated"


@pytest.mark.asyncio
async def test_floss_zip_is_classified_as_unsupported_without_running_floss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import zipfile

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", "harmless fixture")

    def should_not_run(*_args, **_kwargs):
        pytest.fail("FLOSS must not be invoked for ZIP input")

    monkeypatch.setattr("cti_app.workers.confined_analysis.shutil.which", lambda _name: "/floss")
    monkeypatch.setattr("cti_app.workers.confined_analysis.run_analysis_subprocess", should_not_run)

    result = await _run_floss(_floss_parameters(), archive.getvalue(), Settings(_env_file=None))

    assert result["status"] == "UNSUPPORTED_FORMAT"
    assert result["available_reason"] == "floss_unsupported_format_zip"


def test_monodis_metadata_and_cil_rows_select_one_bounded_method() -> None:
    method_table = """Method Table (1..3)
########## .Fixture
1: instance default void '.ctor' ()  (param: 1 impl_flags: cil managed )
2: default int32 Target ()  (param: 1 impl_flags: cil managed )
3: default int32 Target (int32 value)  (param: 1 impl_flags: cil managed )
"""
    methods = _parse_monodis_method_table(method_table)
    assert methods[2] == (".Fixture", "Target")
    assert methods[3] == (".Fixture", "Target")

    disassembly = """// method line 2
.method public static default int32 Target () cil managed
{
    IL_0000: ldc.i4.s 0x2a
    IL_0002: ret
} // end of method Fixture::Target

// method line 3
.method public static default int32 Target (int32 value) cil managed
{
    IL_0000: ldarg.0
    IL_0001: ldc.i4.1
    IL_0002: add
    IL_0003: ret
} // end of method Fixture::Target
"""
    body = _extract_monodis_method_body(disassembly, 3)
    assert body is not None
    assert "ldc.i4.1" in body
    assert "ldc.i4.s 0x2a" not in body


@pytest.mark.asyncio
async def test_monodis_cil_uses_fixed_commands_and_returns_bounded_method_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    method_table = (
        b"Method Table (1..2)\n########## .Fixture\n"
        b"2: default int32 Target ()  (param: 1 impl_flags: cil managed )\n"
    )
    disassembly = (
        b"// method line 2\n.method public static default int32 Target () cil managed\n"
        b"{\n    IL_0000: ldc.i4.s 0x2a\n    IL_0002: ret\n}\n"
        b"} // end of method Fixture::Target\n"
    )

    async def run(argv, **_kwargs):
        calls.append(list(argv))
        output = method_table if "--method" in argv else disassembly
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, output, b"")

    monkeypatch.setattr(
        "cti_app.workers.confined_analysis.shutil.which", lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        "cti_app.workers.confined_analysis._monodis_package_version",
        lambda: "6.8.0.105+dfsg-3.3+deb12u1",
    )
    monkeypatch.setattr("cti_app.workers.confined_analysis.run_analysis_subprocess", run)
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256="a" * 64,
        operation_key="confined-analysis:" + "d" * 64,
        tool=ConfinedAnalysisTool.LOCAL_COMMAND,
        arguments=("monodis",),
        timeout_seconds=30,
        max_output_bytes=16 * 1024,
        analysis_parameters={
            "operation": "cil",
            "method_rid": "2",
            "offset": "8",
            "length": "16",
        },
    )

    result = await _run_local_command(parameters, _pe_fixture(), Settings(_env_file=None))

    assert result["status"] == "SUCCEEDED"
    assert result["operation"] == "cil"
    assert result["method_rid"] == 2
    assert result["method"] == "Fixture.Target"
    assert result["offset"] == 8
    assert result["length"] == 16
    assert result["stdout"] == disassembly.decode()[8:24]
    assert calls[0][1] == "--method"
    assert calls[1][1] == "--show-method-tokens"
    assert all(call[-1].endswith("/sample.bin") for call in calls)
    assert all("/etc/passwd" not in " ".join(call) for call in calls)


class _RecordingCapabilitiesService:
    def __init__(self, *_args, **_kwargs) -> None:
        self.persisted = []

    async def persist_worker_result(self, feature_set) -> None:
        self.persisted.append(feature_set)


@pytest.mark.asyncio
async def test_capa_reports_bounded_sanitized_subprocess_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted = _RecordingCapabilitiesService()

    async def fail_with_readonly_home(self, **_kwargs):
        return (
            "9.4.0",
            AnalysisSubprocessResult(
                AnalysisSubprocessStatus.NON_ZERO_EXIT,
                12,
                b"",
                b"ERROR capa: [Errno 30] Read-only file system: '/home/analysis' "
                b"access_token=secret-value",
            ),
        )

    monkeypatch.setattr(
        "cti_app.workers.confined_analysis.ruleset_manifest", lambda _path: "a" * 64
    )
    monkeypatch.setattr("cti_app.workers.confined_analysis.CapaRunner.run", fail_with_readonly_home)
    monkeypatch.setattr(
        "cti_app.workers.confined_analysis.CapabilitiesService",
        lambda *_args, **_kwargs: persisted,
    )
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        sample_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256="a" * 64,
        operation_key="confined-analysis:" + "b" * 64,
        tool=ConfinedAnalysisTool.CAPA,
        timeout_seconds=60,
        max_output_bytes=100_000,
    )

    result = await _run_capa(
        parameters, _pe_fixture(), object(), lambda: None, Settings(_env_file=None)
    )

    assert result["engine_status"] == "NON_ZERO_EXIT"
    assert "Read-only file system" in result["available_reason"]
    assert "/home/analysis" not in result["available_reason"]
    assert "secret-value" not in result["available_reason"]
    assert persisted.persisted[0].errors


@pytest.mark.asyncio
async def test_capa_classifies_zip_as_unsupported_format_without_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = BytesIO()
    import zipfile

    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", "harmless fixture")

    async def should_not_run(self, **_kwargs):
        pytest.fail("capa must not be invoked for ZIP input")

    persisted = _RecordingCapabilitiesService()
    monkeypatch.setattr(
        "cti_app.workers.confined_analysis.ruleset_manifest", lambda _path: "a" * 64
    )
    monkeypatch.setattr("cti_app.workers.confined_analysis.CapaRunner.run", should_not_run)
    monkeypatch.setattr(
        "cti_app.workers.confined_analysis.CapabilitiesService",
        lambda *_args, **_kwargs: persisted,
    )
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        sample_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256="a" * 64,
        operation_key="confined-analysis:" + "c" * 64,
        tool=ConfinedAnalysisTool.CAPA,
        timeout_seconds=60,
        max_output_bytes=100_000,
    )

    result = await _run_capa(
        parameters, archive.getvalue(), object(), lambda: None, Settings(_env_file=None)
    )

    assert result["engine_status"] == "UNSUPPORTED_FORMAT"
    assert result["available_reason"] == "capa_unsupported_format_zip"


def _pe_fixture() -> bytes:
    marker = _MARKER.encode()
    dos_header = bytearray(64)
    dos_header[:2] = b"MZ"
    dos_header[0x3C:0x40] = (0x80).to_bytes(4, "little")
    coff_header = struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 224, 0x0102)
    optional_values = [
        0x10B,
        0,
        0,
        0x200,
        0,
        0,
        0x1000,
        0x1000,
        0x1000,
        0x400000,
        0x1000,
        0x200,
        6,
        0,
        0,
        0,
        6,
        0,
        0,
        0x2000,
        0x200,
        0,
        3,
        0,
        0x100000,
        0x1000,
        0x100000,
        0x1000,
        0,
        16,
    ]
    optional_header = struct.pack(
        "<HBB" + "I" * 9 + "H" * 6 + "I" * 4 + "H" * 2 + "I" * 6,
        *optional_values,
    ) + bytes(16 * 8)
    section_header = struct.pack(
        "<8sIIIIIIHHI",
        b".rdata",
        len(marker),
        0x1000,
        0x200,
        0x200,
        0,
        0,
        0,
        0,
        0x40000040,
    )
    headers = (
        bytes(dos_header)
        + bytes(0x80 - len(dos_header))
        + b"PE\0\0"
        + coff_header
        + optional_header
        + section_header
    )
    section_data = marker + bytes(0x200 - len(marker))
    return headers + bytes(0x200 - len(headers)) + section_data


def _attestation(
    *,
    reachable: bool = True,
    confined: bool = True,
    families: dict[str, ConfinedAnalysisFamilyStatus] | None = None,
    available_reason: str | None = None,
) -> ConfinedAnalysisWorkerAttestation:
    versions = {
        "static": "static-v1",
        "capa": "9.4.0",
        "smda": "4.5.0",
        "capa_ruleset": "a" * 64,
    }
    default_families = {
        name: ConfinedAnalysisFamilyStatus(name, True, version)
        for name, version in versions.items()
    }
    default_families["sample_analysis"] = ConfinedAnalysisFamilyStatus(
        "sample_analysis", True, "sample-analysis.v1"
    )
    return ConfinedAnalysisWorkerAttestation(
        reachable=reachable,
        confined=confined,
        isolation_profile="analysis-worker-v1",
        versions=versions,
        families=families or default_families,
        dependencies_ready=True,
        available_reason=available_reason,
    )


def _worker_probe_payload(
    settings: Settings, *, parser_version: str | None = None
) -> dict[str, Any]:
    versions: dict[str, str] = {
        "static": "autowork-static-v1",
        "capa": "9.4.0",
        "smda": "4.5.0",
        "capa_ruleset_revision": settings.analysis_capa_ruleset_revision,
        "capa_ruleset_sha256": "a" * 64,
        "floss": "3.1.1",
        "mwcp": "3.16.1",
        "yara": "4.5.0",
    }
    if parser_version is not None:
        versions["mwcp_parser:AutoworkSafeFixture"] = parser_version
    return {
        "uid": 10001,
        "no_new_privileges": True,
        "effective_capabilities": "0000000000000000",
        "root_read_only": True,
        "isolation_profile": settings.analysis_worker_expected_isolation_profile,
        "postgres_access": True,
        "minio_access": True,
        "supported_families": [
            "static",
            "capa",
            "smda",
            "sample_analysis",
            "floss",
            "mwcp",
            "yara",
        ],
        "versions": versions,
    }


def _request(
    *,
    tool: ConfinedAnalysisTool = ConfinedAnalysisTool.CAPA,
    timeout_seconds: float = 10,
    max_output_bytes: int = 200_000,
) -> ConfinedAnalysisRequest:
    return ConfinedAnalysisRequest(
        investigation_id=uuid4(),
        association_id=uuid4(),
        sample_id=uuid4(),
        artifact_id=uuid4(),
        sha256="b" * 64,
        tool=tool,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )


@pytest.mark.parametrize(
    "analysis_parameters",
    (
        {"operation": "userstrings"},
        {"operation": "typedef"},
        {"operation": "methods"},
        {"operation": "cil", "method_name": "Main"},
        {"operation": "cil", "method_rid": "1", "offset": "0", "length": "8192"},
    ),
)
def test_monodis_request_and_durable_job_contract_accept_supported_operations(
    analysis_parameters: dict[str, str],
) -> None:
    request = ConfinedAnalysisRequest(
        investigation_id=uuid4(),
        association_id=uuid4(),
        sample_id=uuid4(),
        artifact_id=uuid4(),
        sha256="b" * 64,
        tool=ConfinedAnalysisTool.LOCAL_COMMAND,
        arguments=("monodis",),
        analysis_parameters=analysis_parameters,
        timeout_seconds=30,
        max_output_bytes=16 * 1024,
    )
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=request.investigation_id,
        association_id=request.association_id,
        sample_id=request.sample_id,
        artifact_id=request.artifact_id,
        artifact_sha256=request.sha256,
        operation_key="confined-analysis:" + "c" * 64,
        tool=request.tool,
        timeout_seconds=request.timeout_seconds,
        max_output_bytes=request.max_output_bytes,
        arguments=request.arguments,
        analysis_parameters=dict(request.analysis_parameters),
    )

    assert parameters.analysis_parameters == analysis_parameters


def test_monodis_request_contract_rejects_unpaired_range_and_path_selector() -> None:
    base = {
        "investigation_id": uuid4(),
        "association_id": uuid4(),
        "sample_id": uuid4(),
        "artifact_id": uuid4(),
        "sha256": "b" * 64,
        "tool": ConfinedAnalysisTool.LOCAL_COMMAND,
        "arguments": ("monodis",),
        "timeout_seconds": 30,
        "max_output_bytes": 16 * 1024,
    }
    for analysis_parameters in (
        {"operation": "cil", "method_rid": "1", "offset": "0"},
        {"operation": "cil", "method_name": "/etc/passwd"},
    ):
        with pytest.raises(ValueError):
            ConfinedAnalysisRequest(**base, analysis_parameters=analysis_parameters)


@dataclass
class _Job:
    id: UUID
    parameters: ConfinedAnalysisJobParameters
    idempotency_key: str
    status: JobStatus = JobStatus.QUEUED
    output_reference: str | None = None


class _MemoryJobService:
    def __init__(self) -> None:
        self.jobs: dict[UUID, _Job] = {}
        self.by_key: dict[str, UUID] = {}
        self.submissions = 0

    async def submit(self, **kwargs: Any) -> _Job:
        self.submissions += 1
        key = str(kwargs["idempotency_key"])
        if key in self.by_key:
            raise DuplicateJobError(self.by_key[key])
        parameters = ConfinedAnalysisJobParameters.model_validate(kwargs["input_parameters"])
        job = _Job(uuid4(), parameters, key)
        self.jobs[job.id] = job
        self.by_key[key] = job.id
        return job

    async def get(self, job_id: UUID) -> _Job:
        return self.jobs[job_id]


class _MemoryBlobCatalog:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.payloads: dict[UUID, bytes] = {}

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        payload = self.payloads[blob_id]
        if len(payload) > max_bytes:
            raise ValueError("blob exceeds read budget")
        return payload

    async def ingest(self, stream: BytesIO, *, logical_bucket: str, mime_type: str) -> FakeBlob:
        del mime_type
        payload = stream.read()
        digest = hashlib.sha256(payload).hexdigest()
        blob = FakeBlob(uuid4(), FakeBlobDescriptor(logical_bucket, digest, len(payload)))
        self.database.blobs[blob.id] = blob
        self.payloads[blob.id] = payload
        return blob


class _CompletingDispatcher:
    def __init__(self, jobs: _MemoryJobService, blobs: _MemoryBlobCatalog) -> None:
        self.jobs = jobs
        self.blobs = blobs
        self.calls: list[UUID] = []

    async def dispatch(self, job_id: UUID) -> None:
        self.calls.append(job_id)
        job = self.jobs.jobs[job_id]
        payload = {
            "schema": "confined-analysis-result.v2",
            "operation_key": job.parameters.operation_key,
            "artifact_id": str(job.parameters.artifact_id),
            "artifact_kind": job.parameters.artifact_kind.value,
            "artifact_sha256": job.parameters.artifact_sha256,
            "tool": job.parameters.tool.value,
            "result": {"status": "SUCCEEDED"},
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        result_blob = await self.blobs.ingest(
            BytesIO(encoded),
            logical_bucket="confined-analysis-results",
            mime_type="application/json",
        )
        job.output_reference = (
            f"confined-analysis-result://{result_blob.id}/{result_blob.descriptor.sha256}"
        )
        job.status = JobStatus.SUCCEEDED


def _port(
    *,
    attestation: ConfinedAnalysisWorkerAttestation | None = None,
    settings: Settings | None = None,
    jobs: _MemoryJobService | None = None,
    dispatcher: Any | None = None,
    blobs: _MemoryBlobCatalog | None = None,
    sleep: Any = asyncio.sleep,
    monotonic: Any | None = None,
) -> tuple[DramatiqConfinedAnalysisExecutionPort, _MemoryJobService, _MemoryBlobCatalog, Any]:
    database = FakeDatabase()
    job_service = jobs or _MemoryJobService()
    blob_catalog = blobs or _MemoryBlobCatalog(database)
    actual_dispatcher = dispatcher or _CompletingDispatcher(job_service, blob_catalog)
    kwargs: dict[str, Any] = {
        "settings": settings or Settings(_env_file=None, analysis_worker_enabled=True),
        "uow_factory": lambda: FakeUow(database),
        "blob_catalog": blob_catalog,
        "attestation": attestation or _attestation(),
        "dispatcher": actual_dispatcher,
        "job_service": job_service,
        "sleep": sleep,
    }
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return (
        DramatiqConfinedAnalysisExecutionPort(**kwargs),
        job_service,
        blob_catalog,
        actual_dispatcher,
    )


@pytest.mark.asyncio
async def test_worker_unreachable_and_unattested_version_are_explicit() -> None:
    request = _request()
    port, jobs, _, dispatcher = _port(
        attestation=_attestation(
            reachable=False,
            available_reason="analysis_worker_unreachable",
        )
    )

    unreachable = await port.execute(request)

    assert unreachable.status is ToolInvocationStatus.UNAVAILABLE
    assert unreachable.available_reason == "analysis_worker_unreachable"
    assert jobs.submissions == 0
    assert dispatcher.calls == []

    families = {
        "capa": ConfinedAnalysisFamilyStatus(
            "capa", False, "9.3.0", "analysis_tool_version_unattested"
        )
    }
    port, jobs, _, dispatcher = _port(attestation=_attestation(families=families))
    unattested = await port.execute(request)

    assert unattested.status is ToolInvocationStatus.UNAVAILABLE
    assert unattested.available_reason == "analysis_tool_version_unattested"
    assert jobs.submissions == 0
    assert dispatcher.calls == []


def test_mwcp_family_and_parser_attestations_are_honest() -> None:
    settings = Settings(_env_file=None)
    no_parser = _attestation_from_response(
        json.dumps(_worker_probe_payload(settings)).encode(), settings
    )
    assert no_parser.families["mwcp"].available is True
    assert no_parser.families["mwcp"].available_reason == "no_authorized_parser"

    configured = Settings(
        _env_file=None,
        mwcp_parser_versions={"AutoworkSafeFixture": "fixture-v1"},
    )
    missing = _attestation_from_response(
        json.dumps(_worker_probe_payload(configured)).encode(), configured
    )
    assert missing.families["mwcp"].available is True
    assert missing.families["mwcp"].available_reason == "allowlisted_parser_unavailable"
    assert missing.families["mwcp_parser:AutoworkSafeFixture"].available is False
    assert (
        missing.families["mwcp_parser:AutoworkSafeFixture"].available_reason
        == "mwcp_parser_unavailable_or_unregistered"
    )

    registered = _attestation_from_response(
        json.dumps(_worker_probe_payload(configured, parser_version="fixture-v1")).encode(),
        configured,
    )
    assert registered.families["mwcp"].available_reason is None
    assert registered.families["mwcp_parser:AutoworkSafeFixture"] == (
        ConfinedAnalysisFamilyStatus("mwcp_parser:AutoworkSafeFixture", True, "fixture-v1")
    )


@pytest.mark.asyncio
async def test_analysis_budgets_timeout_and_result_hash_are_enforced() -> None:
    port, jobs, _, _ = _port()
    over_budget = await port.execute(_request(timeout_seconds=121))
    assert over_budget.status is ToolInvocationStatus.POLICY_DENIED
    assert over_budget.available_reason == "analysis_budget_exceeded"
    assert jobs.submissions == 0
    over_output = await port.execute(_request(max_output_bytes=1_000_001))
    assert over_output.status is ToolInvocationStatus.POLICY_DENIED
    assert over_output.available_reason == "analysis_output_budget_exceeded"
    assert jobs.submissions == 0

    clock = [0.0]

    async def advance(seconds: float) -> None:
        clock[0] += seconds

    pending_jobs = _MemoryJobService()

    class PendingDispatcher:
        async def dispatch(self, _job_id: UUID) -> None:
            return None

    port, _, _, _ = _port(
        jobs=pending_jobs,
        dispatcher=PendingDispatcher(),
        sleep=advance,
        monotonic=lambda: clock[0],
    )
    timed_out = await port.execute(_request(timeout_seconds=1))
    assert timed_out.status is ToolInvocationStatus.UNAVAILABLE
    assert timed_out.available_reason == "analysis_worker_job_timeout"

    class CorruptingBlobCatalog(_MemoryBlobCatalog):
        async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
            del blob_id, max_bytes
            return b"wrong result bytes"

    catalog = CorruptingBlobCatalog(FakeDatabase())
    port, _, _, _ = _port(blobs=catalog)
    corrupted = await port.execute(_request())
    assert corrupted.status is ToolInvocationStatus.FAILED
    assert corrupted.available_reason == "analysis_result_integrity_failed"


@pytest.mark.asyncio
async def test_operation_key_makes_replay_idempotent() -> None:
    jobs = _MemoryJobService()
    port, _, _, dispatcher = _port(jobs=jobs)
    request = _request()

    first = await port.execute(request)
    second = await port.execute(request)

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert second.status is ToolInvocationStatus.SUCCEEDED
    assert first.operation_key == second.operation_key
    assert jobs.submissions == 2  # second submit is resolved as DuplicateJobError
    assert len(dispatcher.calls) == 1


@pytest.mark.asyncio
async def test_handler_rejects_canonical_artifact_with_wrong_hash() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = _pe_fixture()
    sha256 = hashlib.sha256(content).hexdigest()
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:test",
        reason="Approve bounded sample acquisition for a harmless fixture.",
        correlation_id="g2:confined-analysis-hash",
    )
    del envelope
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, FakeVirusTotalPort(content)),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(association.id, purpose="Create harmless test artifact")
    assert acquired.sample_id is not None
    blob_id = database.samples[acquired.sample_id].blob_id
    blobs = _MemoryBlobCatalog(database)
    blobs.payloads[blob_id] = content
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=investigation.id,
        association_id=association.id,
        sample_id=acquired.sample_id,
        artifact_id=blob_id,
        artifact_sha256="c" * 64,
        operation_key="confined-analysis:" + "d" * 64,
        tool=ConfinedAnalysisTool.CAPA,
        timeout_seconds=5,
        max_output_bytes=50_000,
        artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
    )

    with pytest.raises(ConfinedAnalysisArtifactMismatch, match="identity_mismatch"):
        await execute_confined_analysis_job(
            parameters,
            blobs=blobs,  # type: ignore[arg-type]
            uow_factory=lambda: FakeUow(database),
            settings=Settings(_env_file=None),
        )


@pytest.mark.asyncio
async def test_worker_rejects_result_that_cannot_fit_output_budget() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    blobs = _MemoryBlobCatalog(database)
    payload = b"small harmless YARA sample"
    input_blob = await blobs.ingest(
        BytesIO(payload),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=investigation.id,
        artifact_id=input_blob.id,
        artifact_sha256=input_blob.descriptor.sha256,
        artifact_kind=ConfinedAnalysisArtifactKind.CORPUS_SAMPLE,
        operation_key=f"confined-analysis:{hashlib.sha256(payload).hexdigest()}",
        tool=ConfinedAnalysisTool.YARA,
        timeout_seconds=10,
        max_output_bytes=128,
        analysis_parameters={"rule_source": "rule safe { condition: true }"},
    )

    with pytest.raises(ValueError, match="analysis_result_exceeds_output_budget"):
        await execute_confined_analysis_job(
            parameters,
            blobs=blobs,  # type: ignore[arg-type]
            uow_factory=lambda: FakeUow(database),
            settings=Settings(_env_file=None),
        )


@pytest.mark.asyncio
async def test_tool_registry_dispatch_runs_capa_and_strings_through_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = _pe_fixture()
    sha256 = hashlib.sha256(content).hexdigest()
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:test",
        reason="Approve bounded sample acquisition for a harmless fixture.",
        correlation_id="g2:confined-analysis-e2e",
    )
    recorder = make_evidence_recorder(database)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, FakeVirusTotalPort(content)),
        recorder=recorder,
    )
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(association.id, purpose="Create harmless test artifact")
    assert acquired.sample_id is not None
    sample = database.samples[acquired.sample_id]

    rules = tmp_path / "rules"
    rules.mkdir()
    signatures_root = tmp_path / "capa-signature-root"
    (signatures_root / "sigs").mkdir(parents=True)
    capa_wrapper_dir = tmp_path / "capa-bin"
    capa_wrapper_dir.mkdir()
    capa_wrapper = capa_wrapper_dir / "capa"
    capa_wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import capa.main\n"
        f"capa.main.get_default_root = lambda: Path({str(signatures_root)!r})\n"
        "capa.main.main()\n",
        encoding="utf-8",
    )
    capa_wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{capa_wrapper_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    (rules / "safe-marker.yml").write_text(
        "rule:\n"
        "  meta:\n"
        "    name: Safe fixture marker\n"
        "    namespace: autowork/test\n"
        "    authors:\n"
        "      - AutoWork\n"
        "    scopes:\n"
        "      static: file\n"
        "      dynamic: process\n"
        "  features:\n"
        f"    - string: {_MARKER}\n",
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        analysis_worker_enabled=True,
        analysis_capa_rules_path=rules,
        analysis_worker_job_timeout_seconds=90,
        analysis_worker_result_max_bytes=200_000,
    )
    blobs = _MemoryBlobCatalog(database)
    blobs.payloads[sample.blob_id] = content
    jobs = _MemoryJobService()
    handler = build_confined_analysis_job_handler(
        blobs=blobs,  # type: ignore[arg-type]
        uow_factory=lambda: FakeUow(database),
        settings=settings,
    )

    dispatcher = InProcessAnalysisDispatcher(
        jobs,
        handler,
        lambda: FakeUow(database),  # type: ignore[arg-type]
    )
    port = DramatiqConfinedAnalysisExecutionPort(
        settings=settings,
        uow_factory=lambda: FakeUow(database),
        blob_catalog=blobs,  # type: ignore[arg-type]
        attestation=_attestation(),
        dispatcher=dispatcher,
        job_service=jobs,  # type: ignore[arg-type]
    )
    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: FakeUow(database),
        recorder,
        confined_analysis=port,
        analysis_timeout_seconds=45,
        max_output_bytes=200_000,
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
        idempotency_key="tool:sample:confined-analysis",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Run bounded capa and string analysis",
            "evidence_refs": [],
        },
        purpose="Run bounded capa and string analysis",
        operation_key="op:sample:confined-analysis-e2e",
    )

    assert result.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
    assert result.output["sample_sha256"] == sha256
    assert len(dispatcher.calls) == 1
    assert jobs.submissions == 1
    assert len(result.observation_refs) == 2
    strings = next(
        item for item in database.observations.values() if item.kind is EvidenceKind.BINARY_STRING
    )
    assert strings.source == "autowork.analysis_worker.static"
    assert strings.object_id == sha256
    analysis = next(
        item for item in database.observations.values() if item.kind is EvidenceKind.BINARY_ANALYSIS
    )
    proof_bytes = blobs.payloads[analysis.blob_id]
    proof = json.loads(proof_bytes)
    assert proof["schema"] == "confined-analysis-result.v2"
    assert proof["artifact_sha256"] == sha256
    assert proof["result"]["families"]["static"]["strings"]
    assert _MARKER in {item["value"] for item in proof["result"]["families"]["static"]["strings"]}
    assert proof["result"]["families"]["capa"]["status"] in {"SUCCEEDED", "PARTIAL"}
    assert proof["result"]["families"]["capa"]["version"] == "9.4.0"
    assert proof["result"]["families"]["capa"]["ruleset_sha256"]
    assert proof["result"]["families"]["capa"]["status"] == "SUCCEEDED", proof["result"][
        "families"
    ]["capa"]
    assert proof["result"]["families"]["smda"]["version"] == "4.5.0"
    assert proof["result"]["families"]["smda"]["code_features"]["status"] == "SUCCEEDED"
    assert proof["result"]["families"]["smda"]["code_features"]["sample_id"] == str(
        acquired.sample_id
    )
    assert len(proof["result"]["families"]["smda"]["code_features_sha256"]) == 64
    assert proof["result"]["families"]["floss"]["status"] == "SUCCEEDED"
    assert proof["result"]["families"]["floss"]["version"] == "3.1.1"
    assert proof["result"]["families"]["floss"]["output_sha256"]
    assert _MARKER in {item["value"] for item in proof["result"]["families"]["floss"]["strings"]}
    assert any(
        item.kind is EvidenceKind.BINARY_ANALYSIS
        and item.sha256 == hashlib.sha256(proof_bytes).hexdigest()
        for item in database.observations.values()
    )


def test_mwcp_parser_probe_cache_is_keyed_by_parser_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_directory = (Path(__file__).parent / "fixtures").resolve()
    settings = Settings(
        _env_file=None,
        mwcp_parser_versions={"AutoworkSafeFixture": "fixture-v1"},
    )

    monkeypatch.delenv("ANALYSIS_MWCP_PARSER_DIR", raising=False)
    without_fixture = build_analysis_worker_probe_payload(settings)
    assert "mwcp_parser:AutoworkSafeFixture" not in without_fixture["versions"]

    monkeypatch.setenv("ANALYSIS_MWCP_PARSER_DIR", str(fixture_directory))
    with_fixture = build_analysis_worker_probe_payload(settings)
    assert with_fixture["versions"]["mwcp_parser:AutoworkSafeFixture"] == "fixture-v1"


@pytest.mark.asyncio
async def test_mwcp_allowlisted_fixture_parser_is_invoked_in_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_directory = (Path(__file__).parent / "fixtures").resolve()
    monkeypatch.setenv("ANALYSIS_MWCP_PARSER_DIR", str(fixture_directory))
    settings = Settings(
        _env_file=None,
        mwcp_parser_versions={"AutoworkSafeFixture": "fixture-v1"},
        analysis_worker_result_max_bytes=200_000,
    )
    probe = build_analysis_worker_probe_payload(settings)
    assert probe["versions"]["mwcp_parser:AutoworkSafeFixture"] == "fixture-v1"

    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    blobs = _MemoryBlobCatalog(database)
    content = b"AUTOWORK_MWCP_SAFE_FIXTURE"
    input_blob = await blobs.ingest(
        BytesIO(content),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    handler = build_confined_analysis_job_handler(
        blobs=blobs,  # type: ignore[arg-type]
        uow_factory=lambda: FakeUow(database),
        settings=settings,
    )
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=investigation.id,
        artifact_id=input_blob.id,
        artifact_sha256=input_blob.descriptor.sha256,
        artifact_kind=ConfinedAnalysisArtifactKind.CORPUS_SAMPLE,
        operation_key=f"confined-analysis:{hashlib.sha256(content).hexdigest()}",
        tool=ConfinedAnalysisTool.MWCP,
        timeout_seconds=30,
        max_output_bytes=200_000,
        analysis_parameters={
            "parser_id": "AutoworkSafeFixture",
            "parser_version": "fixture-v1",
        },
    )

    reference = await handler(
        parameters,
        JobExecutionContext(uuid4(), lambda: FakeUow(database)),  # type: ignore[arg-type]
    )

    result_blob_id = UUID(reference.split("/")[2])
    proof = json.loads(blobs.payloads[result_blob_id])
    result = proof["result"]
    assert proof["artifact_sha256"] == input_blob.descriptor.sha256
    assert result["status"] == "SUCCEEDED"
    assert result["parser_id"] == "AutoworkSafeFixture"
    assert result["parser_version"] == "fixture-v1"
    assert result["mwcp_version"] == "3.16.1"
    assert result["output_sha256"]
    assert result["report"]["metadata"] == [
        {
            "type": "other",
            "tags": [],
            "key": "safe_fixture",
            "value": "invoked",
            "value_format": "string",
        }
    ]


@pytest.mark.asyncio
async def test_confined_rule_workbench_marks_worker_test_fixtures_synthetic() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    blobs = _MemoryBlobCatalog(database)
    positive_bytes = b"AUTOWORK_YARA_SAFE_FIXTURE positive sample"
    goodware_bytes = b"ordinary harmless goodware sample"
    positive = await blobs.ingest(
        BytesIO(positive_bytes),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    goodware = await blobs.ingest(
        BytesIO(goodware_bytes),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    empty_pcap_bytes = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
    empty_pcap = await blobs.ingest(
        BytesIO(empty_pcap_bytes),
        logical_bucket="network-captures-quarantine",
        mime_type="application/vnd.tcpdump.pcap",
    )
    artifacts = {
        positive.descriptor.sha256: positive.id,
        goodware.descriptor.sha256: goodware.id,
        empty_pcap.descriptor.sha256: empty_pcap.id,
    }
    settings = Settings(_env_file=None, analysis_worker_enabled=True)
    jobs = _MemoryJobService()
    handler = build_confined_analysis_job_handler(
        blobs=blobs,  # type: ignore[arg-type]
        uow_factory=lambda: FakeUow(database),
        settings=settings,
    )

    class InProcessYaraDispatcher:
        async def dispatch(self, job_id: UUID) -> None:
            job = jobs.jobs[job_id]
            job.status = JobStatus.RUNNING
            job.output_reference = await handler(
                job.parameters,
                JobExecutionContext(job.id, lambda: FakeUow(database)),  # type: ignore[arg-type]
            )
            job.status = JobStatus.SUCCEEDED

    family = ConfinedAnalysisFamilyStatus("yara", True, "4.5.0")
    port = DramatiqConfinedAnalysisExecutionPort(
        settings=settings,
        uow_factory=lambda: FakeUow(database),
        blob_catalog=blobs,  # type: ignore[arg-type]
        attestation=_attestation(families={"yara": family}),
        dispatcher=InProcessYaraDispatcher(),
        job_service=jobs,  # type: ignore[arg-type]
    )

    class CanonicalCorpus:
        async def get_bytes_for_sample(self, _sha256: str) -> bytes:
            raise AssertionError("API process must not read corpus bytes for YARA")

        async def get_artifact_id_for_sample(self, sample_sha256: str) -> UUID:
            return artifacts[sample_sha256]

    workbench = RuleWorkbench(
        corpus_bytes=CanonicalCorpus(),  # type: ignore[arg-type]
        yara_engine=None,
        confined_analysis=port,
        detection_candidates=object(),  # type: ignore[arg-type]
        non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
        legitimate_services=LegitimateServiceRegistry(()),
    )
    proposal = workbench.build_rule(
        (
            RuleMotif(
                kind=RuleMotifKind.STRING,
                value="AUTOWORK_YARA_SAFE_FIXTURE",
                provenance_kind=RuleMotifProvenanceKind.P10_PROPOSAL,
                provenance_id="safe-worker-fixture",
            ),
        ),
        RuleConditionStrategy.ANY,
    )
    assert proposal.valid and proposal.source is not None
    report = await workbench.compile_and_test(
        proposal.source,
        (
            RuleTestCase(
                sample_sha256=positive.descriptor.sha256,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
                provenance=RuleSampleProvenance.SYNTHETIC,
            ),
            RuleTestCase(
                sample_sha256=goodware.descriptor.sha256,
                role=RuleTestCorpusRole.GOODWARE,
                provenance=RuleSampleProvenance.SYNTHETIC,
            ),
            RuleTestCase(
                sample_sha256=empty_pcap.descriptor.sha256,
                role=RuleTestCorpusRole.CONFIRMED_POSITIVE,
                provenance=RuleSampleProvenance.SYNTHETIC,
            ),
        ),
        investigation_id=investigation.id,
        selection_bias="Synthetic positive and benign fixture bytes exercise the worker route.",
        coverage_statement="Both synthetic fixture byte streams were tested by analysis-worker.",
    )

    assert report.execution_status.value == "ok"
    assert report.validation_status is RuleValidationStatus.SYNTHETIC_ONLY
    assert rule_validation_reason(report).value == "positive_samples_are_synthetic"
    positive_report = next(
        role for role in report.roles if role.role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    )
    assert positive_report.non_measurable == (empty_pcap.descriptor.sha256,)
    assert len(positive_report.errors) == 1
    assert positive_report.errors[0].code.value == "invalid_sample"
    assert "YARA" in positive_report.errors[0].message
    assert report.false_positive_sha256s == ()
    assert report.false_negative_sha256s == ()
    assert jobs.submissions == 3


@pytest.mark.asyncio
async def test_sample_analysis_does_not_fall_back_to_local_execution() -> None:
    class Hits:
        async def resolve_quarantined_sample(self, _association_id: UUID) -> Any:
            return type(
                "Sample",
                (),
                {
                    "investigation_id": uuid4(),
                    "sample_id": uuid4(),
                    "association_id": uuid4(),
                    "blob_id": uuid4(),
                    "sha256": "b" * 64,
                },
            )()

    class NeverLocal:
        def __init__(self) -> None:
            self.calls = 0

        async def analyze(self, *_args: Any, **_kwargs: Any) -> None:
            self.calls += 1
            raise AssertionError("sample bytes must not be analyzed outside the port")

    local = NeverLocal()
    analyzer = LocalSampleAnalysisService(
        Hits(),  # type: ignore[arg-type]
        lambda: None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        static_analysis=local,  # type: ignore[arg-type]
        capabilities=local,  # type: ignore[arg-type]
        code_features=local,  # type: ignore[arg-type]
    )

    outcome = await analyzer.analyze(association_id=uuid4(), invocation_ref=None)

    assert outcome.status is ToolInvocationStatus.UNAVAILABLE
    assert outcome.output["available_reason"] == "analysis_worker_unavailable"
    assert local.calls == 0


def test_local_unconfined_and_attested_worker_are_distinct_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cti_app.infrastructure.local_command_runner as local_runner_module
    from cti_app.infrastructure.local_command_runner import LocalCommandRunner

    local_runner_module._detect_network_namespace.cache_clear()
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    local = LocalCommandRunner(require_filesystem_confinement=True)
    port, _, _, _ = _port(attestation=_attestation())

    assert local.filesystem_confined is False
    assert port.available is True
    assert port.family_statuses["sample_analysis"].available is True
    assert port.isolation_profile == "analysis-worker-v1"
