from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.behavior_report_import import (
    MAX_SANDBOX_REPORT_BYTES,
    BehaviorReportImporter,
    BehaviorReportImportError,
)
from cti_app.application.investigation_extraction_tools import (
    SANDBOX_REPORT_IMPORT_TOOL,
    extraction_tool_capabilities,
    register_extraction_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.domain.behavior_evidence import BehaviorEvidenceSource
from cti_app.domain.investigation import ToolInvocationStatus


class _Associations:
    def __init__(self, sample_sha256: str) -> None:
        self.sample_sha256 = sample_sha256

    async def list_for_investigation(self, investigation_id: object) -> tuple[object, ...]:
        return (SimpleNamespace(sample_sha256=self.sample_sha256),)


class _Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def record_json(self, *, payload: Mapping[str, Any], **kwargs: object) -> SimpleNamespace:
        self.payloads.append(dict(payload))
        return SimpleNamespace(id=uuid4())


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        investigation_id=uuid4(),
        idempotency_key="sandbox-import-test",
        available_capabilities=frozenset({SANDBOX_REPORT_IMPORT_TOOL}),
    )


def test_cape_report_normalizes_process_drop_network_and_behavior_without_secrets() -> None:
    sample_sha256 = "a" * 64
    report = {
        "target": {"file": {"sha256": sample_sha256}},
        "info": {"started": "2026-10-13T10:00:00Z"},
        "behavior": {
            "processes": [
                {
                    "pid": 100,
                    "ppid": 1,
                    "process_name": "sample.exe",
                    "command_line": "sample.exe /home/analyst/input.bin TOKEN=private",
                    "first_seen": "2026-10-13T10:00:01Z",
                }
            ],
            "events": [
                {
                    "category": "file",
                    "description": "wrote /home/analyst/secret.bin api_key=hidden",
                    "pid": 100,
                    "timestamp": "2026-10-13T10:00:02Z",
                }
            ],
        },
        "dropped": [{"path": "/home/analyst/secret.bin", "sha256": "b" * 64, "size": 19}],
        "network": {
            "tcp": [
                {
                    "src": {"ip": "192.0.2.4", "port": 43210},
                    "dst": {"ip": "198.51.100.8", "port": 443},
                    "protocol": "tcp",
                    "first_seen": "2026-10-13T10:00:03Z",
                }
            ],
            "http": [
                {
                    "host": "C2.Example.COM",
                    "uri": "https://c2.example.com/poll?access_token=private",
                },
                {"uri": "https://[malformed"},
            ],
        },
    }
    raw = json.dumps(report, separators=(",", ":"))

    evidence = BehaviorReportImporter().parse(
        raw,
        source=BehaviorEvidenceSource.CAPE,
        expected_sample_sha256=sample_sha256,
    )

    assert evidence.schema_version == "1"
    assert evidence.sample_sha256 == sample_sha256
    assert evidence.source_report_sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert evidence.processes[0].parent_process_id == "1"
    assert evidence.dropped_files[0].sha256 == "b" * 64
    assert len(evidence.network_sessions) == 3
    assert evidence.network_sessions[0].destination_ip == "198.51.100.8"
    assert evidence.network_sessions[1].host == "c2.example.com"
    assert evidence.network_sessions[2].host is None
    assert evidence.network_sessions[2].uri is None
    assert "access_token=private" not in json.dumps(evidence, default=str)
    assert "/home/analyst" not in json.dumps(evidence, default=str)
    assert "api_key=hidden" not in json.dumps(evidence, default=str)
    assert evidence.behaviors[0].evidence_path.startswith("/")


def test_report_import_is_not_advertised_without_an_importer() -> None:
    assert SANDBOX_REPORT_IMPORT_TOOL not in extraction_tool_capabilities(
        floss=None, configuration=None
    )


def test_triage_report_maps_process_tree_and_network_sessions() -> None:
    sample_sha256 = "c" * 64
    raw = json.dumps(
        {
            "sample": {"sha256": sample_sha256},
            "created_at": "2026-10-13T10:30:00Z",
            "process_tree": [
                {
                    "pid": 25,
                    "parent_pid": 1,
                    "name": "loader",
                    "children": [{"pid": 26, "parent_pid": 25, "name": "payload"}],
                }
            ],
            "dropped_files": [{"filename": "drop.bin", "sha256": "d" * 64, "size": 5}],
            "network_sessions": [
                {"protocol": "udp", "destination_ip": "203.0.113.7", "destination_port": 53}
            ],
            "behaviors": [{"name": "dns_lookup", "description": "queried example.org"}],
        },
        separators=(",", ":"),
    )

    evidence = BehaviorReportImporter().parse(
        raw,
        source=BehaviorEvidenceSource.TRIAGE,
        expected_sample_sha256=sample_sha256,
    )

    assert [item.process_id for item in evidence.processes] == ["25", "26"]
    assert evidence.processes[1].parent_process_id == "25"
    assert evidence.dropped_files[0].path == "drop.bin"
    assert evidence.network_sessions[0].destination_port == 53
    assert evidence.behaviors[0].category == "dns_lookup"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["operator", 17, None])
async def test_sandbox_report_tool_correlates_sha_and_persists_passive_evidence(
    source: str | int | None,
) -> None:
    sample_sha256 = "e" * 64
    report_json = json.dumps(
        {
            "sample_sha256": sample_sha256,
            "process_tree": [{"pid": 1, "name": "fixture"}],
            "behaviors": [],
        },
        separators=(",", ":"),
    )
    recorder = _Recorder()
    registry = InMemoryToolRegistry()
    register_extraction_tools(
        registry,
        sample_associations=_Associations(sample_sha256),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        report_importer=BehaviorReportImporter(),
    )
    assert SANDBOX_REPORT_IMPORT_TOOL in extraction_tool_capabilities(
        floss=None, configuration=None, report_import_enabled=True
    )
    definition, adapter = registry.get(SANDBOX_REPORT_IMPORT_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "source": source, "report_json": report_json},
        _context(),
    )

    if not isinstance(source, str):
        assert result.status is ToolInvocationStatus.POLICY_DENIED
        assert result.output["reason"] == "unsupported_report_source"
        assert recorder.payloads == []
        return
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["schema"] == "BehaviorEvidenceV1"
    assert result.output["untrusted_data"] is True
    assert recorder.payloads[0]["sample_sha256"] == sample_sha256
    assert definition.effect.value == "local_read"
    assert "never executes malware" in " ".join(definition.limitations)


def test_report_rejects_missing_or_mismatched_source_hash_and_size_limit() -> None:
    importer = BehaviorReportImporter()
    with pytest.raises(BehaviorReportImportError, match="report_sample_sha256_missing"):
        importer.parse(
            '{"behaviors":[]}',
            source=BehaviorEvidenceSource.OPERATOR,
            expected_sample_sha256="f" * 64,
        )
    with pytest.raises(BehaviorReportImportError, match="report_sample_sha256_mismatch"):
        importer.parse(
            json.dumps({"sample_sha256": "1" * 64}),
            source=BehaviorEvidenceSource.OPERATOR,
            expected_sample_sha256="2" * 64,
        )
    with pytest.raises(BehaviorReportImportError, match="report_too_large"):
        importer.parse(
            "{" + (" " * MAX_SANDBOX_REPORT_BYTES) + "}",
            source=BehaviorEvidenceSource.OPERATOR,
            expected_sample_sha256="3" * 64,
        )


def test_report_bounds_collections_and_marks_truncation() -> None:
    sample_sha256 = "9" * 64
    raw = json.dumps(
        {
            "sample_sha256": sample_sha256,
            "process_tree": [{"pid": index} for index in range(70)],
        }
    )
    evidence = BehaviorReportImporter().parse(
        raw,
        source=BehaviorEvidenceSource.OPERATOR,
        expected_sample_sha256=sample_sha256,
    )
    assert len(evidence.processes) == 64
    assert evidence.truncated is True
