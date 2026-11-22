"""Confinement oracles for agent-authored analysis scripts (M24-05 H3).

The tests exercise the real worker handler and the real subprocess runner when
this sandbox can install the network seccomp filter. A filesystem-confinement
assertion is skipped only after an explicit bwrap capability probe, and the
skip reason is always printed. Otherwise the tests use the in-process worker
harness declared in ``tests/confined_worker_harness.py``.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisTool,
    ConfinedScriptJobSpec,
)
from cti_app.application.confined_scripts import (
    CONFINED_SCRIPT_CAPABILITY,
    CONFINED_SCRIPT_TOOL,
    ConfinedScriptInterpreter,
    ConfinedScriptLimits,
    ConfinedScriptRequest,
    ConfinedScriptService,
    build_run_script_tool_definition,
)
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_governance import (
    InvestigationBudget,
    InvestigationEnvelopeService,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
    tool_unavailability_reason,
)
from cti_app.application.jobs import JobExecutionContext
from cti_app.config import Settings
from cti_app.domain.investigation import EvidenceKind, SampleOrigin, ToolInvocationStatus
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessStatus,
    run_analysis_subprocess,
)
from cti_app.infrastructure.investigation_runtime_ports import register_confined_script_tool
from cti_app.workers import confined_analysis as worker_module
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

_SAFE_SCRIPT = "print('confined-script-ok')\n"
_ALLOWED_ENV = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "HOME",
        "TMPDIR",
        "AUTOWORK_SCRIPT_INPUT",
        "AUTOWORK_SCRIPT_SCRATCH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONUNBUFFERED",
        "PYTHONNOUSERSITE",
        # bubblewrap sets PWD when it applies --chdir.
        "PWD",
    }
)
_FORBIDDEN_ENV_TOKENS = (
    "POSTGRES",
    "DATABASE",
    "MINIO",
    "S3_",
    "REDIS",
    "AWS_",
    "VT_",
    "VIRUSTOTAL",
    "SHODAN",
    "BRIDGE",
    "OPERATOR_",
    "SECRET",
    "TOKEN",
    "PASSWORD",
)


class _ArtifactStore:
    """Content-addressed canonical artifact store used by the runner fixture."""

    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.payloads: dict[UUID, bytes] = {}
        self.put_calls = 0

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        payload = self.payloads[blob_id]
        if len(payload) > max_bytes:
            raise ValueError("artifact exceeds test read limit")
        return payload

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID:
        del mime_type
        self.put_calls += 1
        digest = hashlib.sha256(content).hexdigest()
        existing = next(
            (
                blob
                for blob in self.database.blobs.values()
                if blob.descriptor.logical_bucket == bucket and blob.descriptor.sha256 == digest
            ),
            None,
        )
        if existing is not None:
            self.payloads.setdefault(existing.id, content)
            return existing.id
        blob = FakeBlob(uuid4(), FakeBlobDescriptor(bucket, digest, len(content)))
        self.database.blobs[blob.id] = blob
        self.payloads[blob.id] = content
        return blob.id


@dataclass(slots=True)
class _ScriptFixture:
    database: FakeDatabase
    investigation_id: UUID
    sample_sha256: str
    sample_blob_id: UUID
    artifact_store: _ArtifactStore
    harness: Any
    service: ConfinedScriptService
    settings: Settings


async def _script_fixture(payload: bytes = b"harmless confined script input") -> _ScriptFixture:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    sha256 = hashlib.sha256(payload).hexdigest()
    envelope = await InvestigationEnvelopeService(lambda: FakeUow(database)).approve(
        make_approved_proposal(investigation),
        actor_id="analyst:test",
        reason="Create an inert confined script fixture.",
        correlation_id="g2:confined-script",
    )
    del envelope
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, FakeVirusTotalPort(payload)),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(
        association.id, purpose="Create harmless confined script fixture"
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
        required_families=("script",),
    )
    service = ConfinedScriptService(
        execution=harness.port,
        uow_factory=lambda: FakeUow(database),  # type: ignore[arg-type]
        archive=artifact_store,
        settings=harness.settings,
    )
    return _ScriptFixture(
        database=database,
        investigation_id=investigation.id,
        sample_sha256=sha256,
        sample_blob_id=sample.blob_id,
        artifact_store=artifact_store,
        harness=harness,
        service=service,
        settings=harness.settings,
    )


def _limits(**overrides: Any) -> ConfinedScriptLimits:
    values: dict[str, Any] = {
        "wall_seconds": 60.0,
        "cpu_seconds": 30,
        "memory_bytes": 256 * 1024 * 1024,
        "file_size_bytes": 1024 * 1024,
        "scratch_max_files": 64,
        "scratch_max_bytes": 4 * 1024 * 1024,
        "stdout_max_bytes": 64 * 1024,
        "stderr_max_bytes": 64 * 1024,
        "max_processes": 64,
    }
    values.update(overrides)
    return ConfinedScriptLimits(**values)


def _request(
    fixture: _ScriptFixture,
    script: str,
    *,
    limits: ConfinedScriptLimits | None = None,
    sample_sha256: str | None = None,
    capture_artifact_id: UUID | None = None,
) -> ConfinedScriptRequest:
    if capture_artifact_id is None:
        sample_sha256 = sample_sha256 or fixture.sample_sha256
    return ConfinedScriptRequest(
        investigation_id=fixture.investigation_id,
        script=script.encode("utf-8"),
        limits=limits or _limits(),
        sample_sha256=sample_sha256,
        capture_artifact_id=capture_artifact_id,
    )


def _require_script_sandbox() -> str:
    capability = worker_module.probe_script_sandbox()
    if capability.kind is None:
        reason = f"script sandbox unavailable in this sandbox: {capability.detail}"
        print(f"SKIP {reason}", file=sys.stderr)
        pytest.skip(reason)
    return capability.kind


async def _run_confined(fixture: _ScriptFixture, request: ConfinedScriptRequest) -> Any:
    """Run once; retry an infrastructure-only confinement refusal, then skip.

    A user namespace can refuse creation between the executed probe and the
    launch. ``UNAVAILABLE``/``confinement_unavailable`` means no script process
    started, so retrying cannot duplicate a measurement. The skip reason is
    always printed.
    """
    result = await fixture.service.run(request)
    if (
        result.status is ToolInvocationStatus.UNAVAILABLE
        and result.reason == "confinement_unavailable"
    ):
        result = await fixture.service.run(request)
    if (
        result.status is ToolInvocationStatus.UNAVAILABLE
        and result.reason == "confinement_unavailable"
    ):
        reason = "no confined process could start in this sandbox: confinement_unavailable"
        print(f"SKIP {reason}", file=sys.stderr)
        pytest.skip(reason)
    return result


def _read_tar(payload: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            assert member.isfile(), member.name
            assert member.mtime == 0
            assert member.uid == member.gid == 0
            assert member.mode == 0o600
            extracted = archive.extractfile(member)
            assert extracted is not None
            members[member.name] = extracted.read()
    return members


def _pid_alive(pid: int) -> bool:
    """A re-parented zombie is dead even though kill(pid, 0) still succeeds."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except OSError:
        return False
    return raw[raw.rfind(")") + 2] != "Z"


@pytest.mark.asyncio
async def test_worker_verifies_script_and_input_hashes_before_execution() -> None:
    fixture = await _script_fixture()
    request = _request(fixture, "print('must-not-run')\n")
    identity = await fixture.service._resolve_input(request)
    assert not isinstance(identity, str)
    _, input_sha256, _, association_id, sample_id, sample_blob_id = identity
    limits = _limits()
    script_blob_id = await fixture.artifact_store.put_bytes(
        request.script, bucket="confined-script-sources", mime_type="text/x-python"
    )
    spec = ConfinedScriptJobSpec(
        script_blob_id=script_blob_id,
        script_sha256=hashlib.sha256(request.script).hexdigest(),
        interpreter="python3",
        wall_seconds=limits.wall_seconds,
        cpu_limit_seconds=limits.cpu_seconds,
        memory_limit_bytes=limits.memory_bytes,
        file_size_limit_bytes=limits.file_size_bytes,
        scratch_max_files=limits.scratch_max_files,
        scratch_max_bytes=limits.scratch_max_bytes,
        stdout_max_bytes=limits.stdout_max_bytes,
        stderr_max_bytes=limits.stderr_max_bytes,
        max_processes=limits.max_processes,
    )

    def parameters(
        artifact_sha256: str, script: ConfinedScriptJobSpec
    ) -> ConfinedAnalysisJobParameters:
        return ConfinedAnalysisJobParameters(
            investigation_id=fixture.investigation_id,
            artifact_id=sample_blob_id,
            artifact_sha256=artifact_sha256,
            operation_key="confined-analysis:" + "a" * 64,
            tool=ConfinedAnalysisTool.SCRIPT,
            timeout_seconds=limits.wall_seconds + 15,
            max_output_bytes=1_000_000,
            artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
            association_id=association_id,
            sample_id=sample_id,
            script=script,
        )

    handler = fixture.harness.dispatcher._handler
    with pytest.raises(
        worker_module.ConfinedAnalysisArtifactMismatch,
        match="script_source_hash_mismatch",
    ):
        await handler(
            parameters(input_sha256, spec.model_copy(update={"script_sha256": "0" * 64})),
            JobExecutionContext(uuid4(), fixture.harness.dispatcher._uow_factory),
        )
    with pytest.raises(
        worker_module.ConfinedAnalysisArtifactMismatch,
        match="analysis_artifact_identity_mismatch",
    ):
        await handler(
            parameters("0" * 64, spec),
            JobExecutionContext(uuid4(), fixture.harness.dispatcher._uow_factory),
        )


def test_interpreter_allowlist_only() -> None:
    assert [item.value for item in ConfinedScriptInterpreter] == ["python3"]
    with pytest.raises(ValueError):
        ConfinedScriptInterpreter("bash")
    with pytest.raises(ValueError):
        ConfinedScriptInterpreter("/bin/sh")
    request = ConfinedScriptRequest(
        investigation_id=uuid4(),
        script=_SAFE_SCRIPT.encode(),
        interpreter="python3",
        limits=_limits(),
        sample_sha256="a" * 64,
    )
    assert request.interpreter is ConfinedScriptInterpreter.PYTHON3
    with pytest.raises(ValueError):
        ConfinedScriptRequest(
            investigation_id=uuid4(),
            script=_SAFE_SCRIPT.encode(),
            interpreter="bash",
            limits=_limits(),
            sample_sha256="a" * 64,
        )
    schema = build_run_script_tool_definition(Settings(_env_file=None)).input_schema
    assert list(schema["properties"]["interpreter"]["enum"]) == ["python3"]


@pytest.mark.asyncio
async def test_script_has_no_network() -> None:
    _require_script_sandbox()
    fixture = await _script_fixture()
    script = (
        "import errno, socket\n"
        "try:\n"
        "    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "except OSError as exc:\n"
        "    print('network_blocked:%s' % exc.errno)\n"
        "else:\n"
        "    print('network_allowed')\n"
    )
    result = await _run_confined(fixture, _request(fixture, script))
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert "network_allowed" not in result.stdout
    assert f"network_blocked:{errno.EPERM}" in result.stdout


@pytest.mark.asyncio
async def test_input_read_only_and_scratch_quota() -> None:
    fixture = await _script_fixture()
    script = (
        "import os\n"
        "path = os.environ['AUTOWORK_SCRIPT_INPUT']\n"
        "try:\n"
        "    with open(path, 'ab') as handle:\n"
        "        handle.write(b'written')\n"
        "except OSError as exc:\n"
        "    print('input_write_blocked:%s' % exc.errno)\n"
        "else:\n"
        "    print('input_write_allowed')\n"
        "scratch = os.environ['AUTOWORK_SCRIPT_SCRATCH']\n"
        "index = 0\n"
        "while True:\n"
        "    with open(os.path.join(scratch, 'f%03d.bin' % index), 'wb') as handle:\n"
        "        handle.write(b'x' * 4096)\n"
        "    index += 1\n"
    )
    limits = _limits(scratch_max_files=8, scratch_max_bytes=32 * 1024)
    result = await _run_confined(fixture, _request(fixture, script, limits=limits))
    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.reason == "script_scratch_quota_exceeded"
    assert "input_write_allowed" not in result.stdout
    assert "input_write_blocked:" in result.stdout
    assert len(result.output_files) <= 8
    assert result.outputs_size_bytes is not None
    assert result.outputs_size_bytes <= limits.scratch_max_bytes + 1024 * 1024
    entry = fixture.database.blobs[fixture.sample_blob_id]
    assert entry.descriptor.sha256 == fixture.sample_sha256


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    _require_script_sandbox()
    pid_file = tmp_path / "grandchildren.pids"
    script = tmp_path / "fork.py"
    script.write_text(
        "import os, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        f"    with open({str(pid_file)!r}, 'a') as handle:\n"
        "        handle.write('%d\\n' % os.getpid())\n"
        "        handle.flush()\n"
        "    time.sleep(8)\n"
        "    os._exit(0)\n"
        "time.sleep(8)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = await run_analysis_subprocess(
        [sys.executable, str(script)],
        timeout_seconds=2,
        output_limit=64 * 1024,
        memory_limit_bytes=256 * 1024 * 1024,
        deny_network=True,
    )
    elapsed = time.monotonic() - started
    assert result.status is AnalysisSubprocessStatus.TIMED_OUT
    assert elapsed < 4, f"timeout did not kill the process group (elapsed {elapsed:.1f}s)"
    pids = [int(item) for item in pid_file.read_text().split()]
    assert pids
    for pid in pids:
        assert not _pid_alive(pid), f"grandchild {pid} survived the process-group kill"


@pytest.mark.asyncio
async def test_env_never_contains_store_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = {
        "POSTGRES_DSN": "postgresql://worker:store-secret@db:5432/cti",
        "MINIO_ACCESS_KEY": "minio-access-secret",
        "MINIO_SECRET_KEY": "minio-secret-value",
        "REDIS_URL": "redis://:redis-secret@broker:6379/0",
        "VT_API_KEY": "vt-api-secret",
        "SHODAN_API_KEY": "shodan-api-secret",
        "OPERATOR_CLIENT_TOKEN": "operator-secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    fixture = await _script_fixture()
    script = (
        "import json, os\n"
        "print('ENV_KEYS=' + json.dumps(sorted(os.environ)))\n"
        "print('ENV_PATH=' + os.environ.get('PATH', ''))\n"
        "try:\n"
        "    raw = open('/proc/self/environ', 'rb').read()\n"
        "except OSError:\n"
        "    print('PROC_ENV=unavailable')\n"
        "else:\n"
        "    names = sorted(\n"
        "        item.split(b'=', 1)[0].decode()\n"
        "        for item in raw.split(b'\\x00')\n"
        "        if item\n"
        "    )\n"
        "    print('PROC_ENV=' + json.dumps(names))\n"
    )
    result = await _run_confined(fixture, _request(fixture, script))
    assert result.status is ToolInvocationStatus.SUCCEEDED
    lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    environment_names = set(json.loads(lines["ENV_KEYS"]))
    assert environment_names <= _ALLOWED_ENV
    if lines["PROC_ENV"] != "unavailable":
        assert set(json.loads(lines["PROC_ENV"])) <= _ALLOWED_ENV
    for forbidden in _FORBIDDEN_ENV_TOKENS:
        assert all(forbidden not in name for name in environment_names)
    for value in secrets.values():
        assert value not in result.stdout
        assert value not in result.stderr


@pytest.mark.asyncio
async def test_outputs_archived_and_bounded() -> None:
    fixture = await _script_fixture()
    script = (
        "import os\n"
        "scratch = os.environ['AUTOWORK_SCRIPT_SCRATCH']\n"
        "with open(os.path.join(scratch, 'alpha.txt'), 'w') as handle:\n"
        "    handle.write('alpha-output')\n"
        "with open(os.path.join(scratch, 'beta.bin'), 'wb') as handle:\n"
        "    handle.write(bytes(range(256)))\n"
        "print('y' * 200000)\n"
    )
    limits = _limits(stdout_max_bytes=4096, stderr_max_bytes=4096)
    result = await _run_confined(fixture, _request(fixture, script, limits=limits))
    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.reason == "script_output_limit"
    assert result.stdout_truncated is True
    assert len(result.stdout.encode("utf-8")) <= limits.stdout_max_bytes
    assert result.stdout_bytes >= limits.stdout_max_bytes
    assert result.outputs_blob_id is not None
    assert result.outputs_sha256 is not None
    payload = fixture.artifact_store.payloads[result.outputs_blob_id]
    assert hashlib.sha256(payload).hexdigest() == result.outputs_sha256
    assert result.outputs_size_bytes == len(payload)
    members = _read_tar(payload)
    assert list(members) == sorted(members)
    assert members["alpha.txt"] == b"alpha-output"
    assert members["beta.bin"] == bytes(range(256))
    assert len(members["stdout.txt"]) <= limits.stdout_max_bytes
    assert "stdout.txt" in members
    archived = {item["path"]: item for item in result.output_files}
    assert set(archived) == {"alpha.txt", "beta.bin"}
    assert archived["alpha.txt"]["sha256"] == hashlib.sha256(b"alpha-output").hexdigest()
    source_blob = fixture.database.blobs[result.script_blob_id]  # type: ignore[index]
    assert source_blob.descriptor.logical_bucket == "confined-script-sources"
    assert source_blob.descriptor.sha256 == result.script_sha256


@pytest.mark.asyncio
async def test_capture_artifact_is_the_canonical_input() -> None:
    fixture = await _script_fixture()
    capture = b"\xd4\xc3\xb2\xa1" + b"synthetic-capture-bytes"
    sha256 = hashlib.sha256(capture).hexdigest()
    blob = FakeBlob(
        uuid4(), FakeBlobDescriptor("network-captures-quarantine", sha256, len(capture))
    )
    fixture.database.blobs[blob.id] = blob
    fixture.artifact_store.payloads[blob.id] = capture
    await make_evidence_recorder(fixture.database).record_existing_blob(
        investigation_id=fixture.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=sha256,
        source="network.quarantine",
        source_version="network.quarantine.v1",
        object_type="network_capture",
        object_id=sha256,
        limitations=(),
        idempotency_key="confined-script-capture-fixture",
    )
    script = (
        "import hashlib, os\n"
        "payload = open(os.environ['AUTOWORK_SCRIPT_INPUT'], 'rb').read()\n"
        "print('INPUT_SHA=' + hashlib.sha256(payload).hexdigest())\n"
    )
    result = await _run_confined(
        fixture, _request(fixture, script, sample_sha256=None, capture_artifact_id=blob.id)
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.input_kind == "capture"
    assert f"INPUT_SHA={sha256}" in result.stdout


@pytest.mark.asyncio
async def test_foreign_capture_artifact_is_refused_before_dispatch() -> None:
    fixture = await _script_fixture()
    capture = b"\xd4\xc3\xb2\xa1" + b"another-investigation-capture"
    sha256 = hashlib.sha256(capture).hexdigest()
    blob = FakeBlob(
        uuid4(), FakeBlobDescriptor("network-captures-quarantine", sha256, len(capture))
    )
    fixture.database.blobs[blob.id] = blob
    fixture.artifact_store.payloads[blob.id] = capture
    other = make_investigation()
    fixture.database.investigations[other.id] = other
    await make_evidence_recorder(fixture.database).record_existing_blob(
        investigation_id=other.id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=sha256,
        source="network.quarantine",
        source_version="network.quarantine.v1",
        object_type="network_capture",
        object_id=sha256,
        limitations=(),
        idempotency_key="confined-script-foreign-capture",
    )

    result = await fixture.service.run(
        _request(fixture, _SAFE_SCRIPT, sample_sha256=None, capture_artifact_id=blob.id)
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.reason == "script_input_not_associated"
    assert result.executed is False
    assert fixture.harness.jobs.submissions == 0


@pytest.mark.asyncio
async def test_unassociated_capture_blob_is_refused_before_dispatch() -> None:
    fixture = await _script_fixture()
    capture = b"\xd4\xc3\xb2\xa1" + b"no-observation-capture"
    sha256 = hashlib.sha256(capture).hexdigest()
    blob = FakeBlob(
        uuid4(), FakeBlobDescriptor("network-captures-quarantine", sha256, len(capture))
    )
    fixture.database.blobs[blob.id] = blob
    fixture.artifact_store.payloads[blob.id] = capture

    result = await fixture.service.run(
        _request(fixture, _SAFE_SCRIPT, sample_sha256=None, capture_artifact_id=blob.id)
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.reason == "script_input_not_associated"


@pytest.mark.asyncio
async def test_non_capture_blob_is_refused_even_with_an_observation() -> None:
    fixture = await _script_fixture()
    note = b"# note, not a capture\n"
    sha256 = hashlib.sha256(note).hexdigest()
    blob = FakeBlob(uuid4(), FakeBlobDescriptor("work-contributions", sha256, len(note)))
    fixture.database.blobs[blob.id] = blob
    fixture.artifact_store.payloads[blob.id] = note
    await make_evidence_recorder(fixture.database).record_existing_blob(
        investigation_id=fixture.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=sha256,
        source="network.quarantine",
        source_version="network.quarantine.v1",
        object_type="network_capture",
        object_id=sha256,
        limitations=(),
        idempotency_key="confined-script-non-capture-blob",
    )

    result = await fixture.service.run(
        _request(fixture, _SAFE_SCRIPT, sample_sha256=None, capture_artifact_id=blob.id)
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.reason == "script_input_not_associated"


@pytest.mark.asyncio
async def test_replay_does_not_reexecute() -> None:
    fixture = await _script_fixture()
    request = _request(fixture, _SAFE_SCRIPT)
    first = await _run_confined(fixture, request)
    second = await _run_confined(fixture, request)
    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert second.status is ToolInvocationStatus.SUCCEEDED
    assert first.operation_key == second.operation_key
    assert first.outputs_sha256 == second.outputs_sha256
    assert len(fixture.harness.dispatcher.calls) == 1
    assert fixture.harness.jobs.submissions == 2


@pytest.mark.asyncio
async def test_unattested_worker_is_unavailable() -> None:
    class _Unattested:
        available = False
        available_reason = "analysis_worker_isolation_unattested"
        isolation_profile = None
        attestation = None
        family_statuses: ClassVar[dict[str, ConfinedAnalysisFamilyStatus]] = {
            "script": ConfinedAnalysisFamilyStatus(
                "script", False, None, "analysis_worker_isolation_unattested"
            )
        }

        async def execute(self, request: Any) -> Any:
            raise AssertionError("an unattested worker must never receive a script")

    service = ConfinedScriptService(
        execution=_Unattested(),
        uow_factory=lambda: pytest.fail("unattested workers must not resolve canonical input"),
        archive=None,  # type: ignore[arg-type]
        settings=Settings(_env_file=None),
    )
    request = ConfinedScriptRequest(
        investigation_id=uuid4(),
        script=_SAFE_SCRIPT.encode(),
        limits=_limits(),
        sample_sha256="a" * 64,
    )
    result = await service.run(request)
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.reason == "confinement_unavailable"


@pytest.mark.asyncio
async def test_secret_in_output_is_redacted() -> None:
    fixture = await _script_fixture()
    script = (
        "print('POSTGRES_DSN=postgresql://worker:store-secret@db:5432/cti')\n"
        "print('MINIO_SECRET_KEY=minio-secret-value')\n"
        "print('VT_API_KEY=vt-api-secret')\n"
        "print('https://bucket.example/object?X-Amz-Signature=signature&X-Amz-Credential=cred')\n"
    )
    result = await _run_confined(fixture, _request(fixture, script))
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert "store-secret" not in result.stdout
    assert "minio-secret-value" not in result.stdout
    assert "vt-api-secret" not in result.stdout
    assert "X-Amz-Signature=signature" not in result.stdout
    assert "[redacted]" in result.stdout
    assert "[URL omitted]" in result.stdout
    payload = fixture.artifact_store.payloads[result.outputs_blob_id]  # type: ignore[index]
    assert b"store-secret" not in _read_tar(payload)["stdout.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "script"),
    (
        (
            "write_outside",
            "import os\n"
            "attempts = [\n"
            "    '/etc/autowork-script-escape',\n"
            "    '../escape.txt',\n"
            "    '/tmp/autowork-script-escape',\n"
            "]\n"
            "result = {}\n"
            "for path in attempts:\n"
            "    try:\n"
            "        with open(path, 'wb') as handle:\n"
            "            handle.write(b'escape')\n"
            "    except OSError:\n"
            "        result[path] = 'blocked'\n"
            "    else:\n"
            "        result[path] = 'written'\n"
            "print('OUTSIDE=' + repr(sorted(result.items())))\n",
        ),
        (
            "huge_output",
            "import sys\nsys.stdout.write('z' * 2000000)\nsys.stdout.flush()\n",
        ),
        (
            "symlink_trick",
            "import os\n"
            "scratch = os.environ['AUTOWORK_SCRIPT_SCRATCH']\n"
            "os.symlink('/etc/passwd', os.path.join(scratch, 'passwd-link'))\n"
            "os.symlink('/etc', os.path.join(scratch, 'etc-dir'))\n"
            "with open(os.path.join(scratch, 'real.txt'), 'w') as handle:\n"
            "    handle.write('real-output')\n"
            "print('SYMLINKS_READY')\n",
        ),
        (
            "fork_bomb",
            "import os, time\n"
            "pidfile = os.path.join(os.environ['AUTOWORK_SCRIPT_SCRATCH'], 'pids.txt')\n"
            "failures = 0\n"
            "while True:\n"
            "    try:\n"
            "        pid = os.fork()\n"
            "    except OSError:\n"
            "        failures += 1\n"
            "        if failures > 20:\n"
            "            time.sleep(1)\n"
            "            os._exit(3)\n"
            "        time.sleep(0.01)\n"
            "        continue\n"
            "    if pid == 0:\n"
            "        time.sleep(2)\n"
            "        os._exit(0)\n"
            "    with open(pidfile, 'a') as handle:\n"
            "        handle.write('%d\\n' % pid)\n"
            "        handle.flush()\n"
            "    time.sleep(0.01)\n",
        ),
        (
            "proc_environ",
            "import os\n"
            "print('SELF_ENV=' + ','.join(sorted(os.environ)))\n"
            "try:\n"
            "    raw = open('/proc/1/environ', 'rb').read().decode('latin-1')\n"
            "except OSError:\n"
            "    print('PID1_ENV=unreadable')\n"
            "else:\n"
            "    print('PID1_ENV=' + raw.replace(chr(0), '|'))\n",
        ),
    ),
)
async def test_malicious_script_corpus_is_contained_or_killed(
    case: str, script: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_script_sandbox()
    if case == "fork_bomb":
        # A PID namespace hides host PIDs from the script. Force the
        # seccomp-only profile so the recorded child PIDs can be checked for
        # survivors on the host; the outer process-group kill fixes both modes.
        monkeypatch.setattr(
            worker_module,
            "probe_script_sandbox",
            lambda: worker_module.ScriptSandboxCapability(
                "seccomp", "forced_seccomp_host_pid_oracle"
            ),
        )
    fixture = await _script_fixture()
    limits = _limits(
        stdout_max_bytes=8192,
        stderr_max_bytes=8192,
        scratch_max_files=16,
        scratch_max_bytes=1024 * 1024,
        max_processes=8,
    )
    started = time.monotonic()
    result = await _run_confined(fixture, _request(fixture, script, limits=limits))
    elapsed = time.monotonic() - started
    assert elapsed < 55, f"{case} was not killed by a quota or wall budget"
    assert len(result.stdout.encode("utf-8")) <= limits.stdout_max_bytes
    assert len(result.stderr.encode("utf-8")) <= limits.stderr_max_bytes
    assert result.outputs_size_bytes is None or (
        result.outputs_size_bytes <= limits.scratch_max_bytes + 1024 * 1024
    )
    if case == "write_outside":
        assert "OUTSIDE=" in result.stdout
        assert "('/etc/autowork-script-escape', 'blocked')" in result.stdout
        assert not Path("/etc/autowork-script-escape").exists()  # noqa: ASYNC240
        host_tmp_escape = Path("/tmp/autowork-script-escape")
        try:
            if result.sandbox == "bwrap":
                # /tmp is a private tmpfs and ../escape stays in the private
                # mount namespace: neither path exists on the host.
                assert not host_tmp_escape.exists()  # noqa: ASYNC240
            else:
                print(
                    "NOTE write_outside: seccomp-only mode leaves host /tmp visible; "
                    "the analysis-worker read-only rootfs is the production boundary",
                    file=sys.stderr,
                )
        finally:
            if host_tmp_escape.exists():  # noqa: ASYNC240
                host_tmp_escape.unlink()  # noqa: ASYNC240
    elif case == "symlink_trick":
        assert result.outputs_blob_id is not None
        members = _read_tar(fixture.artifact_store.payloads[result.outputs_blob_id])
        assert "passwd-link" not in members
        assert "etc-dir" not in members
        assert members["real.txt"] == b"real-output"
        assert all(b"root:x:" not in content for content in members.values())
    elif case == "fork_bomb":
        # Containment may come from RLIMIT_NPROC (forks refused, script exits
        # non-zero), the group sampler killing the burst, or the wall budget.
        assert result.status in {
            ToolInvocationStatus.PARTIAL,
            ToolInvocationStatus.FAILED,
        }
        assert result.reason in {
            "script_process_limit_exceeded",
            "script_timed_out",
            "script_exit_non_zero",
        }
        assert elapsed < 30
        peak = result.resource_usage.get("peak_process_count")
        assert isinstance(peak, int)
        # The sampler runs every 100 ms; a fast burst may overshoot the quota
        # before the group is killed, but it stays bounded and every recorded
        # child must be dead after the job.
        assert peak <= limits.max_processes + 24
        assert result.outputs_blob_id is not None
        members = _read_tar(fixture.artifact_store.payloads[result.outputs_blob_id])
        pids = [int(item) for item in members.get("pids.txt", b"").split()]
        assert pids
        for pid in pids:
            assert not _pid_alive(pid), f"forked child {pid} survived the quota kill"
    elif case == "proc_environ":
        lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        assert set(lines["SELF_ENV"].split(",")) <= _ALLOWED_ENV
        if result.sandbox == "bwrap" and lines.get("PID1_ENV") != "unreadable":
            pid1 = lines["PID1_ENV"]
            for forbidden in _FORBIDDEN_ENV_TOKENS:
                assert forbidden not in pid1


@pytest.mark.asyncio
async def test_registered_adapter_is_absent_without_its_capability() -> None:
    fixture = await _script_fixture()
    recorded = []

    class _Recorder:
        async def record_json(self, **kwargs: Any) -> Any:
            recorded.append(kwargs)
            raise AssertionError("an unavailable tool must not record an execution")

    registry = InMemoryToolRegistry()
    available = register_confined_script_tool(
        registry,
        execution=fixture.harness.port,
        uow_factory=lambda: FakeUow(fixture.database),  # type: ignore[arg-type]
        artifact_store=fixture.artifact_store,  # type: ignore[arg-type]
        recorder=_Recorder(),  # type: ignore[arg-type]
        settings=fixture.settings,
    )
    assert available is True
    dispatcher = RegistryToolDispatcher(registry)
    context = ToolExecutionContext(
        investigation_id=fixture.investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="confined-script-capability-gate",
        available_capabilities=frozenset(),
    )
    assert CONFINED_SCRIPT_TOOL not in {item.name for item in registry.list_available(context)}
    definition = next(
        item for item in registry.list_registered() if item.name == CONFINED_SCRIPT_TOOL
    )
    assert tool_unavailability_reason(definition, context) == (
        f"capability_unavailable:{CONFINED_SCRIPT_CAPABILITY}"
    )
    result = await dispatcher.dispatch(
        context,
        tool_name=CONFINED_SCRIPT_TOOL,
        schema_version="1",
        arguments={
            "interpreter": "python3",
            "script": _SAFE_SCRIPT,
            "sample_sha256": fixture.sample_sha256,
        },
        purpose="Attempt an unadvertised confined script.",
        operation_key="confined-script-capability-gate",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert recorded == []
    assert registry.list_registered()[0].capability_key == CONFINED_SCRIPT_CAPABILITY
