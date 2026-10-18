from __future__ import annotations

import hashlib
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.confined_analysis import ConfinedAnalysisFamilyStatus
from cti_app.application.investigation_extraction_tools import (
    MWCP_TOOL,
    _configuration_value_json,
    extraction_tool_capabilities,
    register_extraction_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.extraction import ExtractionStatus
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.infrastructure.mwcp_adapter import MwcpAdapter, _normalize_configuration


class _CanonicalBlobs:
    def __init__(self, sample_sha256: str, size: int, *, exists: bool = True) -> None:
        self.sample_sha256 = sample_sha256
        self.blob = SimpleNamespace(
            id=uuid4(),
            descriptor=SimpleNamespace(
                sha256=sample_sha256,
                size=size,
                logical_bucket=SAMPLES_QUARANTINE_BUCKET,
            ),
        )
        self.exists = exists

    async def get_by_address(self, bucket: str, sha256: str) -> object | None:
        if self.exists and bucket == SAMPLES_QUARANTINE_BUCKET and sha256 == self.sample_sha256:
            return self.blob
        return None


class _Uow:
    def __init__(self, blobs: _CanonicalBlobs) -> None:
        self.blobs = blobs

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _WorkerPort:
    available = True
    available_reason = None

    def __init__(self, result: Mapping[str, Any], parser_versions: Mapping[str, str]) -> None:
        family_statuses = {"mwcp": ConfinedAnalysisFamilyStatus("mwcp", True, "3.16.1")}
        family_statuses.update(
            {
                f"mwcp_parser:{parser_id}": ConfinedAnalysisFamilyStatus(
                    f"mwcp_parser:{parser_id}", True, version
                )
                for parser_id, version in parser_versions.items()
            }
        )
        self.family_statuses = family_statuses
        self.result = dict(result)
        self.requests: list[object] = []

    async def execute(self, request: object) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(
            status=ToolInvocationStatus.SUCCEEDED,
            available_reason=None,
            payload={"result": self.result},
        )


class _Associations:
    def __init__(self, sample_sha256: str) -> None:
        self.sample_sha256 = sample_sha256

    async def list_for_investigation(self, _investigation_id: object) -> tuple[object, ...]:
        return (SimpleNamespace(sample_sha256=self.sample_sha256),)


class _Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def record_json(
        self, *, payload: Mapping[str, Any], **_kwargs: object
    ) -> SimpleNamespace:
        self.payloads.append(dict(payload))
        return SimpleNamespace(id=uuid4())


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        investigation_id=uuid4(),
        idempotency_key="mwcp-test-invocation",
        available_capabilities=frozenset({MWCP_TOOL}),
    )


def _worker_result(
    parser_id: str,
    parser_version: str,
    report: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "parser_id": parser_id,
        "parser_version": parser_version,
        "mwcp_version": "3.16.1",
        "output_sha256": hashlib.sha256(b"canonical mwcp json proof").hexdigest(),
        "output_bytes": 128,
        "exit_code": 0,
        "report": dict(report),
        "truncated": False,
    }


def _registered(
    result: Mapping[str, Any],
    parser_versions: Mapping[str, str],
    sample_sha256: str,
    *,
    size: int = 16,
    exists: bool = True,
) -> tuple[InMemoryToolRegistry, MwcpAdapter, _Recorder, _WorkerPort]:
    blobs = _CanonicalBlobs(sample_sha256, size, exists=exists)
    worker = _WorkerPort(result, parser_versions)
    adapter = MwcpAdapter(
        parser_versions=parser_versions,
        enabled=True,
        confined_analysis=worker,  # type: ignore[arg-type]
        uow_factory=lambda: _Uow(blobs),  # type: ignore[arg-type]
    )
    recorder = _Recorder()
    registry = InMemoryToolRegistry()
    register_extraction_tools(
        registry,
        sample_associations=_Associations(sample_sha256),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        configuration=adapter,
    )
    return registry, adapter, recorder, worker


@pytest.mark.asyncio
async def test_mwcp_uses_attested_worker_parser_and_normalizes_configuration() -> None:
    sample_sha256 = hashlib.sha256(b"harmless fixture").hexdigest()
    parser_versions = {"demo.config": "1.4"}
    raw_report = {
        "configuration": {
            "c2": "HTTPS://C2.Example.COM/path?access_token=hidden",
            "callback": "https://[2001:db8::1]/poll?token=hidden-ip-token",
            "malformed_endpoint": "https://[malformed",
            "port": 443,
            "enabled": True,
            "token": "do-not-persist",
            "/home/analyst/private": "safe value",
        }
    }
    result_payload = _worker_result("demo.config", "1.4", raw_report)
    registry, port, recorder, worker = _registered(result_payload, parser_versions, sample_sha256)

    assert port.available
    assert MWCP_TOOL in extraction_tool_capabilities(
        floss=None, configuration=port, report_import_enabled=False
    )
    definition, adapter = registry.get(MWCP_TOOL, "1")  # type: ignore[misc]
    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "parser_id": "demo.config"}, _context()
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert definition.schema_version == "1"
    assert result.output["parser_id"] == "demo.config"
    assert result.output["parser_version"] == "1.4"
    indicators = result.output["network_indicators"]
    assert indicators[0]["normalized_value"] == "https c2.example.com/path"
    assert indicators[0]["scheme"] == "https"
    assert indicators[0]["host"] == "c2.example.com"
    assert indicators[1]["host"] == "2001:db8::1"
    assert indicators[1]["path"] == "/poll"
    assert len(indicators) == 2
    serialized = repr(result.output)
    assert "hidden" not in serialized
    assert "hidden-ip-token" not in serialized
    assert "do-not-persist" not in serialized
    assert "/home/analyst" not in serialized
    assert recorder.payloads[0]["raw_output_sha256"] == result_payload["output_sha256"]
    assert (
        "An absent extraction does not establish the absence of configuration."
        in result.output["limitations"]
    )
    request = worker.requests[0]
    assert request.tool.value == "mwcp"
    assert request.analysis_parameters == {
        "parser_id": "demo.config",
        "parser_version": "1.4",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("parser_id", ["unauthorized.parser", 17, None])
async def test_mwcp_rejects_parser_outside_allowlist_before_worker_execution(
    parser_id: str | int | None,
) -> None:
    sample_sha256 = hashlib.sha256(b"safe").hexdigest()
    parser_versions = {"only.this.parser": "1.0"}
    result_payload = _worker_result("only.this.parser", "1.0", {"configuration": {}})
    registry, _port, recorder, worker = _registered(result_payload, parser_versions, sample_sha256)
    _definition, adapter = registry.get(MWCP_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "parser_id": parser_id}, _context()
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["reason"] == "parser_not_allowlisted"
    assert worker.requests == []
    assert recorder.payloads == []


@pytest.mark.asyncio
async def test_mwcp_empty_extraction_does_not_prove_configuration_absence() -> None:
    sample_sha256 = hashlib.sha256(b"safe").hexdigest()
    parser_versions = {"empty.parser": "2"}
    result_payload = _worker_result("empty.parser", "2", {"configuration": {}})
    registry, _port, _recorder, _worker = _registered(
        result_payload, parser_versions, sample_sha256
    )
    _definition, adapter = registry.get(MWCP_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "parser_id": "empty.parser"}, _context()
    )

    assert result.status is ToolInvocationStatus.EMPTY
    assert (
        "An absent extraction does not establish the absence of configuration."
        in result.output["limitations"]
    )


def test_mwcp_field_truncation_is_marked_and_counted() -> None:
    values, indicators, truncated = _normalize_configuration(
        {"configuration": {"payload": "x" * 5_000, "server": "c" * 2_200}}
    )

    assert truncated is True
    assert indicators == ()
    payload_value = next(item for item in values if item.key == "payload")
    server_value = next(item for item in values if item.key == "server")
    assert payload_value.truncated_fields == ("value",)
    assert server_value.truncated_fields == ("network_indicator",)
    serialized = _configuration_value_json(payload_value)
    assert serialized["truncated_fields"] == ["value"]
    assert serialized["truncated"] is True
    assert sum(len(item.truncated_fields) for item in values) == 2


@pytest.mark.asyncio
async def test_mwcp_refuses_oversized_bytes_and_unavailable_worker() -> None:
    sample_sha256 = hashlib.sha256(b"safe fixture larger than bound").hexdigest()
    parser_versions = {"allowed.parser": "1.0"}
    payload = _worker_result("allowed.parser", "1.0", {"configuration": {}})
    _registry, oversized, _recorder, worker = _registered(
        payload, parser_versions, sample_sha256, size=128
    )
    oversized._max_sample_bytes = 4
    denied = await oversized.extract(
        sample_sha256, parser_id="allowed.parser", investigation_id=uuid4()
    )
    assert denied.status is ExtractionStatus.POLICY_DENIED
    assert denied.detail_code == "sample_too_large"
    assert worker.requests == []

    legacy = MwcpAdapter(parser_versions=parser_versions, enabled=True)
    assert not legacy.available
    assert legacy.unavailable_reason == "analysis_worker_unavailable"
    unavailable = await legacy.extract(sample_sha256, parser_id="allowed.parser")
    assert unavailable.status is ExtractionStatus.UNAVAILABLE
