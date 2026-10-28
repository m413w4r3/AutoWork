from __future__ import annotations

import base64
import hashlib
import shutil
import struct
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisTool,
)
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_governance import InvestigationEnvelopeService
from cti_app.application.local_analysis import (
    LocalCommandCatalog,
    _validate_local_command_range_options,
    default_local_command_specs,
    normalize_command_options,
)
from cti_app.config import Settings
from cti_app.domain.investigation import SampleOrigin
from cti_app.domain.local_analysis import LocalCommandId, LocalCommandStatus
from cti_app.infrastructure.confined_local_commands import ConfinedLocalCommands
from cti_app.workers.confined_analysis import (
    _monodis_package_version,
    _run_monodis,
    _validate_local_command_arguments,
)
from tests.confined_worker_harness import build_confined_worker_harness
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


@pytest.fixture(autouse=True)
def _clear_mwcp_parser_probe_cache():
    from cti_app.workers.confined_analysis import _registered_mwcp_parsers

    _registered_mwcp_parsers.cache_clear()
    yield
    _registered_mwcp_parsers.cache_clear()


class _Execution:
    available = True
    available_reason = None
    isolation_profile = "test-in-process"
    attestation = None

    def __init__(self, family: ConfinedAnalysisFamilyStatus) -> None:
        self.family_statuses = {"local_command": family}
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        raise AssertionError("the request should not reach the worker")


def _catalog() -> LocalCommandCatalog:
    spec = default_local_command_specs()[0]
    return LocalCommandCatalog(
        specs=(spec,),
        which=lambda _binary: "/usr/bin/file",
    )


def _full_catalog() -> LocalCommandCatalog:
    return LocalCommandCatalog(
        specs=tuple(
            replace(spec, requires_filesystem_confinement=True)
            for spec in default_local_command_specs()
        ),
        which=lambda name: f"/usr/bin/{name}",
    )


@pytest.mark.asyncio
async def test_unknown_options_are_denied_before_sample_resolution_or_worker_dispatch() -> None:
    execution = _Execution(ConfinedAnalysisFamilyStatus("local_command", True, "v1"))
    lookups = 0

    def uow_factory():
        nonlocal lookups
        lookups += 1
        raise AssertionError("invalid options must be rejected before canonical lookup")

    service = ConfinedLocalCommands(
        execution=execution,
        catalog=_catalog(),
        uow_factory=uow_factory,
        settings=Settings(_env_file=None),
        enabled=True,
        max_sample_bytes=200 * 1024 * 1024,
    )

    result = await service.run_for_investigation(
        uuid4(), LocalCommandId.FILE, "a" * 64, {"filename": "/host/etc/passwd"}
    )

    assert result.status is LocalCommandStatus.POLICY_DENIED
    assert result.detail_code == "option_not_allowed"
    assert lookups == 0
    assert execution.requests == []


@pytest.mark.asyncio
async def test_xxd_unknown_option_is_denied_before_sample_resolution() -> None:
    execution = _Execution(ConfinedAnalysisFamilyStatus("local_command", True, "v1"))
    service = ConfinedLocalCommands(
        execution=execution,
        catalog=_full_catalog(),
        uow_factory=lambda: pytest.fail("unknown options must be rejected before blob lookup"),
        settings=Settings(_env_file=None),
        enabled=True,
        max_sample_bytes=200 * 1024 * 1024,
    )

    result = await service.run_for_investigation(
        uuid4(),
        "xxd",
        "a" * 64,
        {"offset": 0, "length": 16, "filename": "/host/etc/passwd"},
    )

    assert result.status is LocalCommandStatus.POLICY_DENIED
    assert result.detail_code == "option_not_allowed"


@pytest.mark.asyncio
async def test_archive_tools_require_their_own_worker_attestation_before_blob_lookup() -> None:
    execution = _Execution(ConfinedAnalysisFamilyStatus("local_command", True, "v1"))
    service = ConfinedLocalCommands(
        execution=execution,
        catalog=_full_catalog(),
        uow_factory=lambda: pytest.fail("unattested archive tools must not resolve the sample"),
        settings=Settings(_env_file=None),
        enabled=True,
        max_sample_bytes=200 * 1024 * 1024,
    )

    for command_id in (
        LocalCommandId.ZIP_MEMBERS,
        LocalCommandId.ARCHIVE_LIST,
        LocalCommandId.ARCHIVE_EXTRACT_HASH,
    ):
        result = await service.run_for_investigation(uuid4(), command_id, "a" * 64, {})
        assert result.status is LocalCommandStatus.UNAVAILABLE
        assert result.detail_code == "analysis_family_not_attested"
    assert execution.requests == []


@pytest.mark.parametrize(
    ("command_id", "options", "detail_code"),
    (
        ("xxd", {"offset": 0, "length": 4097}, "integer_option_above_maximum"),
        ("sha256sum", {"offset": 0}, "range_options_must_be_paired"),
    ),
)
@pytest.mark.asyncio
async def test_invalid_hex_and_hash_ranges_are_denied_before_sample_resolution(
    command_id: str,
    options: dict[str, object],
    detail_code: str,
) -> None:
    execution = _Execution(ConfinedAnalysisFamilyStatus("local_command", True, "v1"))
    service = ConfinedLocalCommands(
        execution=execution,
        catalog=_full_catalog(),
        uow_factory=lambda: pytest.fail("invalid ranges must be rejected before blob lookup"),
        settings=Settings(_env_file=None),
        enabled=True,
        max_sample_bytes=200 * 1024 * 1024,
    )

    result = await service.run_for_investigation(uuid4(), command_id, "a" * 64, options)

    assert result.status is LocalCommandStatus.POLICY_DENIED
    assert result.detail_code == detail_code
    assert execution.requests == []


@pytest.mark.asyncio
async def test_unattested_worker_family_stays_unavailable_without_submitting_a_job() -> None:
    execution = _Execution(
        ConfinedAnalysisFamilyStatus(
            "local_command", False, None, "analysis_tool_version_unattested"
        )
    )
    service = ConfinedLocalCommands(
        execution=execution,
        catalog=_catalog(),
        uow_factory=lambda: pytest.fail("unattested tools must not resolve a sample"),
        settings=Settings(_env_file=None),
        enabled=True,
        max_sample_bytes=200 * 1024 * 1024,
    )

    result = await service.run_for_investigation(uuid4(), LocalCommandId.FILE, "a" * 64, {})

    assert result.status is LocalCommandStatus.UNAVAILABLE
    assert result.detail_code == "analysis_tool_version_unattested"
    assert execution.requests == []


class _ArtifactStore:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.payloads: dict[UUID, bytes] = {}

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        payload = self.payloads[blob_id]
        if len(payload) > max_bytes:
            raise ValueError("artifact exceeds test limit")
        return payload

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        del mime_type
        digest = hashlib.sha256(content).hexdigest()
        blob = FakeBlob(uuid4(), FakeBlobDescriptor(bucket, digest, len(content)))
        self.database.blobs[blob.id] = blob
        self.payloads[blob.id] = content
        return blob.id


def _elf_fixture() -> bytes:
    ident = b"\x7fELF" + bytes((2, 1, 1, 0)) + bytes(8)
    header = struct.pack(
        "<HHIQQQIHHHHHH",
        2,
        62,
        1,
        0x400000,
        0,
        0,
        0,
        64,
        0,
        0,
        0,
        0,
        0,
    )
    marker = f"/tmp/autowork-local-command-{uuid4().hex}"
    return (
        ident
        + header
        + b"\0SAFE_LOCAL_WORKER_MARKER ; touch "
        + marker.encode()
        + b" $(touch "
        + marker.encode()
        + b") curl https://example.invalid/forbidden\0"
    )


async def _local_worker_fixture(payload: bytes | None = None):
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    payload = payload if payload is not None else _elf_fixture()
    marker = (
        payload.split(b"; touch ", 1)[1].split(b" ", 1)[0].decode()
        if b"; touch " in payload
        else f"/tmp/autowork-local-command-{uuid4().hex}"
    )
    sha256 = hashlib.sha256(payload).hexdigest()
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:test",
        reason="Create an inert local command worker fixture.",
        correlation_id="g2:confined-local-command",
    )
    del envelope
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, FakeVirusTotalPort(payload)),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(
        association.id, purpose="Create harmless local command fixture"
    )
    assert acquired.sample_id is not None
    sample = database.samples[acquired.sample_id]
    artifact_store = _ArtifactStore(database)
    artifact_store.payloads[sample.blob_id] = payload
    settings = Settings(_env_file=None, analysis_worker_enabled=True)
    harness = build_confined_worker_harness(
        settings=settings,
        uow_factory=lambda: FakeUow(database),  # type: ignore[arg-type]
        artifact_store=artifact_store,  # type: ignore[arg-type]
        required_families=("local_command",),
    )
    local_catalog = LocalCommandCatalog(
        specs=tuple(
            replace(spec, requires_filesystem_confinement=True)
            for spec in default_local_command_specs()
        ),
        which=lambda name: f"/usr/bin/{name}",
        timeout_seconds=30,
        max_output_bytes=1_000_000,
        cpu_limit_seconds=30,
        memory_limit_bytes=512 * 1024 * 1024,
        file_size_limit_bytes=1_048_576,
    )
    service = ConfinedLocalCommands(
        execution=harness.port,
        catalog=local_catalog,
        uow_factory=lambda: FakeUow(database),  # type: ignore[arg-type]
        settings=harness.settings,
        enabled=True,
        max_sample_bytes=200 * 1024 * 1024,
    )
    return service, investigation.id, sha256, artifact_store, harness, Path(marker)


@pytest.mark.parametrize(
    ("command_id", "binary", "options"),
    (
        (LocalCommandId.FILE, "file", {}),
        (LocalCommandId.STRINGS, "strings", {"min_length": 4, "max_strings": 16}),
        (LocalCommandId.READELF, "readelf", {"mode": "header"}),
        (LocalCommandId.OBJDUMP, "objdump", {"mode": "file_header"}),
        (
            LocalCommandId.OBJDUMP,
            "objdump",
            {"mode": "disassemble", "offset": 0x400000, "length": 64},
        ),
        ("xxd", "xxd", {"offset": 64, "length": 24}),
        ("sha256sum", "sha256sum", {}),
        ("sha256sum", "sha256sum", {"offset": 64, "length": 24}),
    ),
)
@pytest.mark.asyncio
async def test_real_catalogue_binaries_run_through_confined_dispatcher_worker(
    command_id: LocalCommandId | str,
    binary: str,
    options: dict[str, object],
) -> None:
    if shutil.which(binary) is None:
        pytest.skip(f"analysis-worker fixture binary absent locally: {binary}")

    (
        service,
        investigation_id,
        sha256,
        artifact_store,
        harness,
        marker_path,
    ) = await _local_worker_fixture()
    result = await service.run_for_investigation(
        investigation_id,
        command_id,
        sha256,
        options,
    )

    assert harness.jobs.submissions == 1
    assert result.status in {
        LocalCommandStatus.SUCCEEDED,
        LocalCommandStatus.EMPTY,
        LocalCommandStatus.OUTPUT_TRUNCATED,
        LocalCommandStatus.FAILED,
    }
    assert result.status is not LocalCommandStatus.UNAVAILABLE
    assert result.normalized_command[0] == binary
    assert all("/tmp/cti-local-command-" not in item for item in result.normalized_command)
    assert artifact_store.payloads
    assert not marker_path.exists()
    if binary == "xxd":
        assert result.output.startswith("00000040:")
    elif binary == "sha256sum":
        payload = next(iter(artifact_store.payloads.values()))
        offset = int(str(options.get("offset", 0)))
        length = int(str(options.get("length", len(payload) - offset)))
        expected_hash = hashlib.sha256(payload[offset : offset + length]).hexdigest()
        assert expected_hash in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "selector"),
    (
        ("userstrings", {}),
        ("typedef", {}),
        ("methods", {}),
        ("cil", {"method_name": "Main"}),
    ),
)
async def test_monodis_operations_pass_through_confined_dispatcher_and_worker(
    operation: str,
    selector: dict[str, object],
) -> None:
    if shutil.which("monodis") is None:
        pytest.skip("monodis is absent; run this test in the analysis-worker image")

    (
        service,
        investigation_id,
        sha256,
        artifact_store,
        harness,
        _marker,
    ) = await _local_worker_fixture(_MINIMAL_DOTNET_ASSEMBLY)
    result = await service.run_for_investigation(
        investigation_id,
        LocalCommandId.MONODIS,
        sha256,
        {"operation": operation, **selector},
    )

    assert harness.jobs.submissions == 1
    assert result.status is LocalCommandStatus.SUCCEEDED
    assert result.normalized_command[0] == "monodis"
    assert f"operation={operation}" in result.output
    assert artifact_store.payloads


@pytest.mark.asyncio
async def test_hex_and_hash_ranges_cannot_extend_past_the_quarantined_blob() -> None:
    (
        service,
        investigation_id,
        sha256,
        artifact_store,
        harness,
        _marker_path,
    ) = await _local_worker_fixture()
    payload = next(iter(artifact_store.payloads.values()))
    invalid_ranges = (
        ("xxd", {"offset": len(payload) - 1, "length": 2}),
        ("sha256sum", {"offset": len(payload) - 1, "length": 2}),
    )

    for command_id, options in invalid_ranges:
        result = await service.run_for_investigation(investigation_id, command_id, sha256, options)
        assert result.status is LocalCommandStatus.POLICY_DENIED
        assert result.detail_code == "range_out_of_bounds"
    assert harness.jobs.submissions == 0


@pytest.mark.parametrize(
    ("command_id", "parameters"),
    (
        ("xxd", {"offset": "0", "length": "4097"}),
        ("xxd", {"offset": "18446744073709551615", "length": "2"}),
        ("sha256sum", {"offset": "0"}),
        ("sha256sum", {"offset": "0", "length": "1048577"}),
    ),
)
def test_worker_rejects_invalid_hex_and_hash_ranges(
    command_id: str,
    parameters: dict[str, str],
) -> None:
    with pytest.raises(ValueError):
        _validate_local_command_arguments((command_id,), parameters, max_range_bytes=1_048_576)


def test_monodis_catalog_options_are_typed_and_reject_paths() -> None:
    spec = next(item for item in default_local_command_specs() if item.id.value == "monodis")
    options, error = normalize_command_options(
        spec,
        {
            "operation": "cil",
            "method_name": "RuntimeBroker.InitializeNewDomain",
            "offset": 0,
            "length": 256,
        },
    )
    assert error is None
    assert _validate_local_command_range_options(spec.id, options) is None

    invalid, error = normalize_command_options(
        spec,
        {"operation": "cil", "method_name": "/etc/passwd", "offset": 0, "length": 64},
    )
    assert error is None
    assert _validate_local_command_range_options(spec.id, invalid) == "method_selector_invalid"

    default_page, error = normalize_command_options(
        spec,
        {"operation": "cil", "method_rid": 1},
    )
    assert error is None
    assert _validate_local_command_range_options(spec.id, default_page) is None


@pytest.mark.parametrize(
    "parameters",
    (
        {"operation": "cil", "method_rid": "1", "offset": "0"},
        {"operation": "cil", "method_rid": "1", "offset": "0", "length": "65537"},
        {
            "operation": "cil",
            "method_name": "Main",
            "method_rid": "1",
            "offset": "0",
            "length": "32",
        },
        {"operation": "userstrings", "method_name": "Main"},
        {"operation": "cil", "method_name": "/etc/passwd", "offset": "0", "length": "32"},
    ),
)
def test_worker_rejects_invalid_monodis_arguments(parameters: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        _validate_local_command_arguments(("monodis",), parameters, max_range_bytes=1_048_576)


def test_worker_accepts_cil_with_documented_default_page() -> None:
    _validate_local_command_arguments(
        ("monodis",),
        {"operation": "cil", "method_rid": "1"},
        max_range_bytes=1_048_576,
    )


_MINIMAL_DOTNET_ASSEMBLY = base64.b64decode(
    "TVqQAAMAAAAEAAAA//8AALgAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAAA4fug4AtAnNIbgBTM0hVGhpcyBwcm9ncmFtIGNhbm5vdCBiZSBydW4gaW4gRE9TIG1vZGUuDQ0KJAAAAAAAAABQRQAATAEDAAAAAAAAAAAAAAAAAOAAAgELAQgAAAQAAAAGAAAAAAAAjiIAAAAgAAAAQAAAAABAAAAgAAAAAgAABAAAAAAAAAAEAAAAAAAAAACAAAAAAgAAAAAAAAMAQIUAABAAABAAAAAAEAAAEAAAAAAAABAAAAAAAAAAAAAAAEAiAABLAAAAAEAAAOACAAAAAAAAAAAAAAAAAAAAAAAAAGAAAAwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAACAAAAAAAAAAAAAAACCAAAEgAAAAAAAAAAAAAAC50ZXh0AAAAlAIAAAAgAAAABAAAAAIAAAAAAAAAAAAAAAAAACAAAGAucnNyYwAAAOACAAAAQAAAAAQAAAAGAAAAAAAAAAAAAAAAAABAAABALnJlbG9jAAAMAAAAAGAAAAACAAAACgAAAAAAAAAAAAAAAAAAQAAAQgAAAAAAAAAAAAAAAAAAAABwIgAAAAAAAEgAAAACAAUAVCAAAOgBAAABAAAAAQAABgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAodKgBCU0pCAQABAAAAAAAMAAAAdjQuMC4zMDMxOQAAAAAFAGwAAACwAAAAI34AABwBAACAAAAAI1N0cmluZ3MAAAAAnAEAAAgAAAAjVVMApAEAABAAAAAjR1VJRAAAALQBAAA0AAAAI0Jsb2IAAAAAAAAAAgAAEEcUAAAJAAAAAPoBMwAWAAABAAAAAgAAAAIAAAABAAAAAQAAAAEAAAABAAAAAQAAAAAAcgABAAAAAAAGABcAHgAGACUAQwAAAAAAAQAAAAAAAQABAIEBEAAKAAAABQABAAEAUCAAAAAAlgASAAEAAQARAGMABQAuAAsACQAEgAAAAAAAAAAAAAAAAAAAAAAKAAAABAAAAAAAAAAAAAAAKABpAAAAAAAAAAA8TW9kdWxlPgBGaXh0dXJlAE1haW4AT2JqZWN0AFN5c3RlbQBSdW50aW1lQ29tcGF0aWJpbGl0eUF0dHJpYnV0ZQBTeXN0ZW0uUnVudGltZS5Db21waWxlclNlcnZpY2VzAC5jdG9yAG1zY29ybGliAEZpeHR1cmUuZXhlAAAAAAMgAAAAAACJQR7MXNNsQYmP4MyOxNNeAAMAAAgDIAABHgEAAQBUAhZXcmFwTm9uRXhjZXB0aW9uVGhyb3dzAQi3elxWGTTgiQAAAAAAAABoIgAAAAAAAAAAAAB+IgAAACAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcCIAAAAAAAAAAF9Db3JFeGVNYWluAG1zY29yZWUuZGxsAAAAAAD/JQAgQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABABAAAAAYAACAAAAAAAAAAAAAAAAAAAABAAEAAAAwAACAAAAAAAAAAAAAAAAAAAABAAAAAABIAAAAWEAAAIgCAAAAAAAAAAAAAIgCNAAAAFYAUwBfAFYARQBSAFMASQBPAE4AXwBJAE4ARgBPAAAAAAC9BO/+AAABAAAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAQAAAACAAAAAAAAAAAAAAAAAAAARAAAAAEAVgBhAHIARgBpAGwAZQBJAG4AZgBvAAAAAAAkAAQAAABUAHIAYQBuAHMAbABhAHQAaQBvAG4AAAAAAH8AsAToAQAAAQBTAHQAcgBpAG4AZwBGAGkAbABlAEkAbgBmAG8AAADEAQAAAQAwADAANwBmADAANABiADAAAAAcAAIAAQBDAG8AbQBtAG4AdABzAAAgAAAAJAACAAEAQwBvAG0AcABhAG4AeQBOAGEAbQBlAAAAAAAgAAAALAACAAEARgBpAGwAZQBEAGUAcwBjAHIAaQBwAHQAaQBvAG4AAAAAACAAAAAwAAgAAQBGAGkAbABlAFYAZQByAHMAaQBvAG4AAAAAADAALgAwAC4AMAAuADAAAAAwAAgAAQBJAG4AdABlAHIAbgBhAGwATgBhAG0AZQAAAEYAaQB4AHQAdQByAGUAAAAoAAIAAQBMAGUAZwBhAGwAQwBvAHAAeQByAGkAZwBoAHQAAAAgAAAALAACAAEATABlAGcAYQBsAFQAcgBhAGQAZQBtAGEAcgBrAHMAAAAAACAAAABAAAwAAQBPAHIAaQBnAGkAbgBhAGwARgBpAGwAZQBuAGEAbQBlAAAARgBpAHgAdAB1AHIAZQAuAGUAeABlAAAAJAACAAEAUAByAG8AZAB1AGMAdABOAGEAbQBlAAAAAAAgAAAAKAACAAEAUAByAG8AZAB1AGMAdABWAGUAcgBzAGkAbwBuAAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAMAAAAkDIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
)


@pytest.mark.image
@pytest.mark.asyncio
async def test_image_monodis_reads_minimal_dotnet_fixture_with_default_cil_page() -> None:
    if shutil.which("monodis") is None:
        pytest.skip("monodis is absent; run this test in the analysis-worker image")
    package_version = _monodis_package_version()
    if package_version is None:
        pytest.skip("mono-utils package version is unavailable")
    sample_sha256 = hashlib.sha256(_MINIMAL_DOTNET_ASSEMBLY).hexdigest()
    parameters = ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=sample_sha256,
        operation_key=f"confined-analysis:{'a' * 64}",
        tool=ConfinedAnalysisTool.LOCAL_COMMAND,
        timeout_seconds=30,
        max_output_bytes=16_384,
        arguments=("monodis",),
        analysis_parameters={"operation": "cil", "method_name": "Main"},
    )
    settings = Settings(_env_file=None).model_copy(
        update={"analysis_local_monodis_expected_version": package_version}
    )

    result = await _run_monodis(parameters, _MINIMAL_DOTNET_ASSEMBLY, settings)

    assert result["status"] == "SUCCEEDED", result
    assert result["method"] == "Fixture.Main"
    assert result["offset"] == 0
    assert result["length"] <= 8 * 1024
    assert "IL_0001:  ret" in result["stdout"]
