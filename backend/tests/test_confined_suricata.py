from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisWorkerAttestation,
)
from cti_app.application.evidence_recorder import EvidenceKind
from cti_app.application.investigation_network_tools import (
    NETWORK_SURICATA_REPLAY,
    network_tool_capabilities,
    register_network_tools,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
)
from cti_app.application.network_workbench import NetworkWorkbench
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.investigation import InvestigationBudget
from cti_app.domain.network_protocol import NetworkCaptureFormat, NetworkCaptureRefV1
from cti_app.infrastructure.analysis_subprocess import (
    AnalysisSubprocessResult,
    AnalysisSubprocessStatus,
)
from cti_app.infrastructure.confined_analysis_dispatch import (
    DramatiqConfinedAnalysisExecutionPort,
    _family_statuses,
)
from cti_app.infrastructure.confined_suricata import validate_suricata_rule_source
from cti_app.workers import confined_analysis as confined_worker
from cti_app.workers.analysis_tasks import build_confined_analysis_job_handler
from tests.confined_worker_harness import InProcessAnalysisDispatcher, _HarnessJobService
from tests.investigation_c2_fakes import (
    FakeBlob,
    FakeBlobDescriptor,
    FakeDatabase,
    FakeUow,
    make_evidence_recorder,
    make_investigation,
)

FIXED_RULE = (
    'alert tcp any any -> any any (msg:"AUTOWORK benign replay marker"; '
    'content:"AUTOWORK_SURICATA_BENIGN"; sid:9900001; rev:1;)'
)
VERSIONED_RULE = (
    'alert tcp any any -> any any (msg:"Harmless candidate marker"; '
    'content:"AUTOWORK_SURICATA_BENIGN"; sid:9900042; rev:3;)'
)
_RULESET_SHA256 = "66f69f0cf586a87f739d4f63c6bb9d23a7d303f8adf23c2773a31839ea7f9f59"


class _MemoryBlobCatalog:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.payloads: dict[UUID, bytes] = {}

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        payload = self.payloads[blob_id]
        if len(payload) > max_bytes:
            raise ValueError("blob exceeds read limit")
        return payload

    async def ingest(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> FakeBlob:
        del mime_type
        payload = source.read()
        digest = hashlib.sha256(payload).hexdigest()
        blob = FakeBlob(uuid4(), FakeBlobDescriptor(logical_bucket, digest, len(payload)))
        self.database.blobs[blob.id] = blob
        self.payloads[blob.id] = payload
        return blob


class _CaptureBlobStore:
    def __init__(self, blobs: _MemoryBlobCatalog) -> None:
        self.blobs = blobs

    async def read_capture(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return await self.blobs.read(blob_id, max_bytes=max_bytes)


def _synthetic_pcap() -> bytes:
    payload = b"AUTOWORK_SURICATA_BENIGN"
    ethernet = b"\x00" * 12 + b"\x08\x00"
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        40 + len(payload),
        7,
        0,
        64,
        6,
        0,
        bytes((192, 0, 2, 10)),
        bytes((198, 51, 100, 20)),
    )
    tcp_header = struct.pack("!HHIIHHHH", 43123, 80, 1, 0, 0x5018, 65535, 0, 0)
    frame = ethernet + ip_header + tcp_header + payload
    return (
        struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1, 0, len(frame), len(frame))
        + frame
    )


async def _make_worker_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    eve_event: dict[str, Any] | None = None,
) -> tuple[
    DramatiqConfinedAnalysisExecutionPort,
    _MemoryBlobCatalog,
    FakeDatabase,
    Any,
    list[list[str]],
    bytes,
    UUID,
]:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    capture = _synthetic_pcap()
    capture_sha256 = hashlib.sha256(capture).hexdigest()
    capture_id = uuid4()
    database.blobs[capture_id] = FakeBlob(
        capture_id,
        FakeBlobDescriptor("network-captures-quarantine", capture_sha256, len(capture)),
    )
    recorder = make_evidence_recorder(database)
    # The production worker requires a canonical quarantine observation for capture inputs.
    await recorder.record_existing_blob(
        investigation_id=investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=capture_id,
        sha256=capture_sha256,
        source="network.quarantine",
        source_version="1",
        object_type="network_capture",
        object_id=capture_sha256,
        limitations=(),
    )
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "autowork.rules").write_text(FIXED_RULE, encoding="utf-8")
    settings = Settings(
        _env_file=None,
        analysis_worker_enabled=True,
        analysis_suricata_rules_path=rules_dir,
        analysis_worker_job_timeout_seconds=60,
        analysis_worker_result_max_bytes=1_000_000,
    )
    blobs = _MemoryBlobCatalog(database)
    blobs.payloads[capture_id] = capture
    jobs = _HarnessJobService()
    handler = build_confined_analysis_job_handler(
        blobs=cast(Any, blobs),
        uow_factory=lambda: FakeUow(database),
        settings=settings,
    )
    dispatcher = InProcessAnalysisDispatcher(
        jobs,
        handler,
        lambda: FakeUow(database),  # type: ignore[arg-type]
    )
    versions = {
        "suricata": "7.0.10",
        "suricata_ruleset_revision": settings.analysis_suricata_ruleset_revision,
        "suricata_ruleset_sha256": settings.analysis_suricata_ruleset_sha256,
    }
    family = ConfinedAnalysisFamilyStatus("suricata", True, "7.0.10")
    attestation = ConfinedAnalysisWorkerAttestation(
        reachable=True,
        confined=True,
        isolation_profile="test-in-process",
        versions=versions,
        families={"suricata": family},
        dependencies_ready=True,
    )
    port = DramatiqConfinedAnalysisExecutionPort(
        settings=settings,
        uow_factory=lambda: FakeUow(database),  # type: ignore[arg-type]
        blob_catalog=cast(Any, blobs),
        attestation=attestation,
        dispatcher=dispatcher,
        job_service=jobs,  # type: ignore[arg-type]
    )
    commands: list[list[str]] = []

    async def fake_run(argv: Sequence[str], **_kwargs: Any) -> AnalysisSubprocessResult:
        command = list(argv)
        commands.append(command)
        if "-r" in command:
            output_dir = Path(command[command.index("-l") + 1])
            event = eve_event or {
                "event_type": "alert",
                "src_ip": "192.0.2.10",
                "dest_ip": "198.51.100.20",
                "alert": {"signature": "Harmless candidate marker", "signature_id": 9900042},
            }
            (output_dir / "eve.json").write_text(
                json.dumps(event, separators=(",", ":")) + "\n", encoding="utf-8"
            )
        return AnalysisSubprocessResult(AnalysisSubprocessStatus.SUCCEEDED, 0, b"", b"")

    monkeypatch.setattr(confined_worker, "run_analysis_subprocess", fake_run)
    monkeypatch.setattr(confined_worker, "_probe_suricata_version", lambda _exe: "7.0.10")
    monkeypatch.setattr(
        confined_worker.shutil,
        "which",
        lambda name: "/usr/bin/suricata" if name == "suricata" else None,
    )
    return port, blobs, database, recorder, commands, capture, investigation.id


def test_suricata_candidate_rules_are_versioned_and_confined() -> None:
    large_rule_source = "#" + "x" * 90_000 + "\n" + VERSIONED_RULE
    assert (
        validate_suricata_rule_source(
            VERSIONED_RULE, candidate_id="candidate-a", candidate_version="3"
        )
        == VERSIONED_RULE
    )
    assert validate_suricata_rule_source(large_rule_source) == large_rule_source
    for source, expected in (
        (VERSIONED_RULE.replace("sid:9900042", "sid:1"), "reserved_range"),
        (VERSIONED_RULE.replace("rev:3", "rev:2"), "revision_mismatch"),
        ("include: /etc/passwd", "forbidden_directive"),
        (VERSIONED_RULE + "\n" + VERSIONED_RULE, "duplicate"),
    ):
        with pytest.raises(ValueError, match=expected):
            validate_suricata_rule_source(source, candidate_id="candidate-a", candidate_version="3")


def test_suricata_attestation_gates_tools_on_version_rules_and_isolation() -> None:
    settings = Settings(_env_file=None)
    versions = {
        "suricata": settings.analysis_suricata_expected_version,
        "suricata_ruleset_revision": settings.analysis_suricata_ruleset_revision,
        "suricata_ruleset_sha256": settings.analysis_suricata_ruleset_sha256,
    }
    family = _family_statuses(
        versions,
        worker_confined=True,
        dependencies_ready=True,
        supported_families=frozenset({"suricata"}),
        settings=settings,
    )["suricata"]
    assert family.available

    changed_rules = _family_statuses(
        {**versions, "suricata_ruleset_sha256": "0" * 64},
        worker_confined=True,
        dependencies_ready=True,
        supported_families=frozenset({"suricata"}),
        settings=settings,
    )["suricata"]
    unconfined = _family_statuses(
        versions,
        worker_confined=False,
        dependencies_ready=True,
        supported_families=frozenset({"suricata"}),
        settings=settings,
    )["suricata"]
    assert not changed_rules.available
    assert changed_rules.available_reason == "analysis_suricata_ruleset_unattested"
    assert not unconfined.available
    assert unconfined.available_reason == "analysis_worker_isolation_unattested"

    execution = SimpleNamespace(
        available=True,
        family_statuses={
            "suricata": ConfinedAnalysisFamilyStatus(
                "suricata", False, available_reason="analysis_tool_unavailable"
            )
        },
    )
    tshark = SimpleNamespace(available=False, execution=execution)
    registry = InMemoryToolRegistry()
    register_network_tools(
        registry,
        workbench=NetworkWorkbench(
            quarantine=SimpleNamespace(),
            blobs=SimpleNamespace(),  # type: ignore[arg-type]
            tshark=cast(Any, tshark),
        ),
        recorder=cast(Any, object()),
    )
    assert NETWORK_SURICATA_REPLAY not in network_tool_capabilities(cast(Any, tshark))
    assert registry.get(NETWORK_SURICATA_REPLAY, "1") is None


@pytest.mark.asyncio
async def test_confined_suricata_operations_use_the_real_job_dispatcher_and_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (
        port,
        blobs,
        _database,
        _recorder,
        commands,
        capture,
        investigation_id,
    ) = await _make_worker_port(tmp_path, monkeypatch)
    from cti_app.infrastructure.confined_suricata import ConfinedSuricataAdapter

    suricata = ConfinedSuricataAdapter(port)
    capture_id = next(blob_id for blob_id, content in blobs.payloads.items() if content == capture)
    capture_sha256 = hashlib.sha256(capture).hexdigest()
    validation = await suricata.validate_rules(
        artifact_id=capture_id,
        investigation_id=investigation_id,
        capture_sha256=capture_sha256,
    )
    replay = await suricata.replay(
        artifact_id=capture_id,
        investigation_id=investigation_id,
        capture_sha256=capture_sha256,
    )
    replay_again = await suricata.replay(
        artifact_id=capture_id,
        investigation_id=investigation_id,
        capture_sha256=capture_sha256,
    )

    assert (
        validation.payload is not None and validation.payload["result"]["mode"] == "validate_rules"
    )
    assert replay.payload is not None
    result = replay.payload["result"]
    assert result["status"] == "SUCCEEDED"
    assert result["eve_blob_id"] in {str(blob_id) for blob_id in blobs.payloads}
    assert result["event_counts"]["alert"] == 1
    assert result["events"][0]["alert"]["signature"] == "Harmless candidate marker"
    archived = blobs.payloads[UUID(result["eve_blob_id"])]
    assert hashlib.sha256(archived).hexdigest() == result["eve_sha256"]
    assert replay_again.operation_key == replay.operation_key
    assert len(commands) == 2
    assert "-T" in commands[0] and "-r" not in commands[0]
    assert "-r" in commands[1] and "-k" in commands[1]
    assert commands[1][commands[1].index("-k") + 1] == "none"
    assert commands[1][-2:] == ["--runmode", "single"]


@pytest.mark.asyncio
async def test_suricata_network_tool_is_attested_and_persists_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, blobs, database, recorder, _commands, capture, investigation_id = await _make_worker_port(
        tmp_path, monkeypatch
    )
    capture_id = next(blob_id for blob_id, value in blobs.payloads.items() if value == capture)
    capture_sha256 = hashlib.sha256(capture).hexdigest()
    reference = NetworkCaptureRefV1(
        capture_id, capture_sha256, len(capture), NetworkCaptureFormat.PCAP
    )
    tshark = SimpleNamespace(available=False, execution=port)
    workbench = NetworkWorkbench(
        quarantine=SimpleNamespace(),
        blobs=_CaptureBlobStore(blobs),  # type: ignore[arg-type]
        tshark=cast(Any, tshark),
        recorder=recorder,
    )
    workbench._remember_capture_association(investigation_id, capture_id, capture_sha256)
    registry = InMemoryToolRegistry()
    register_network_tools(
        registry,
        workbench=workbench,
        recorder=recorder,
        tool_seconds=65,  # type: ignore[arg-type]
    )
    available = network_tool_capabilities(cast(Any, tshark))
    assert NETWORK_SURICATA_REPLAY in available
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(),
        idempotency_key="suricata-tool-test",
        available_capabilities=available,
    )
    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name=NETWORK_SURICATA_REPLAY,
        schema_version="1",
        arguments={"capture_ref": reference.to_dict()},
        purpose="Replay a synthetic harmless capture",
        operation_key="op:suricata-tool-test",
    )

    assert result.status.value == "succeeded", result
    assert result.observation_refs
    observation = next(
        item for item in database.observations.values() if item.source == NETWORK_SURICATA_REPLAY
    )
    assert observation.source == NETWORK_SURICATA_REPLAY
    assert observation.object_type == "suricata_observation"
    engine = result.output["engine"]
    assert engine["eve_blob_id"]


class _EveBlobCatalog:
    def __init__(self) -> None:
        self.content: dict[UUID, bytes] = {}
        self.records: dict[UUID, BlobRecord] = {}

    async def ingest(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobRecord:
        content = source.read()
        record = BlobRecord(
            descriptor=BlobDescriptor(
                sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                mime_type=mime_type,
                logical_bucket=logical_bucket,
            )
        )
        self.content[record.id] = content
        self.records[record.id] = record
        return record


@pytest.mark.skipif(
    confined_worker.shutil.which("suricata") is None,
    reason=(
        "Suricata binary absent locally; run the image test with "
        "ANALYSIS_WORKER_IMAGE_TEST=1 after building analysis-worker."
    ),
)
@pytest.mark.asyncio
async def test_real_suricata_replay_emits_alert_in_eve(tmp_path: Path) -> None:
    capture = _synthetic_pcap()
    candidate = {
        "candidate_id": "benign-fixture",
        "candidate_version": "3",
        "rule_source": VERSIONED_RULE,
    }
    parameters = confined_worker.ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=hashlib.sha256(capture).hexdigest(),
        operation_key=f"confined-analysis:{'a' * 64}",
        tool=confined_worker.ConfinedAnalysisTool.SURICATA_REPLAY,
        timeout_seconds=30,
        max_output_bytes=512_000,
        artifact_kind=confined_worker.ConfinedAnalysisArtifactKind.CAPTURE,
        analysis_parameters=candidate,
    )
    blobs = _EveBlobCatalog()
    validation_parameters = parameters.model_copy(
        update={"tool": confined_worker.ConfinedAnalysisTool.SURICATA_VALIDATE_RULES}
    )
    validation = await confined_worker._run_suricata(
        validation_parameters,
        capture,
        blobs=cast(Any, blobs),
        settings=Settings(_env_file=None),
    )
    assert validation["status"] == "SUCCEEDED", validation
    result = await confined_worker._run_suricata(
        parameters, capture, blobs=cast(Any, blobs), settings=Settings(_env_file=None)
    )

    assert result["status"] == "SUCCEEDED", result
    assert result["event_counts"]["alert"] >= 1
    assert result["engine_stats"]
    assert any(event.get("event_type") == "alert" for event in result["events"])
    assert result["eve_sha256"]
    assert result["eve_blob_id"]
    rule_blob_id = UUID(result["candidate_rule_blob_id"])
    assert blobs.content[rule_blob_id] == VERSIONED_RULE.encode("utf-8")
    eve_blob_id = UUID(result["eve_blob_id"])
    assert hashlib.sha256(blobs.content[eve_blob_id]).hexdigest() == result["eve_sha256"]


@pytest.mark.asyncio
async def test_missing_suricata_is_reported_as_unavailable_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _synthetic_pcap()
    parameters = confined_worker.ConfinedAnalysisJobParameters(
        investigation_id=uuid4(),
        artifact_id=uuid4(),
        artifact_sha256=hashlib.sha256(capture).hexdigest(),
        operation_key=f"confined-analysis:{'b' * 64}",
        tool=confined_worker.ConfinedAnalysisTool.SURICATA_VALIDATE_RULES,
        timeout_seconds=10,
        max_output_bytes=128_000,
        artifact_kind=confined_worker.ConfinedAnalysisArtifactKind.CAPTURE,
    )
    monkeypatch.setattr(confined_worker.shutil, "which", lambda _name: None)

    result = await confined_worker._run_suricata(
        parameters,
        capture,
        blobs=cast(Any, _EveBlobCatalog()),
        settings=Settings(_env_file=None),
    )

    assert result == {
        "status": "unavailable",
        "available_reason": "suricata_binary_unavailable",
    }
