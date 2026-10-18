from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.confined_analysis import ConfinedAnalysisFamilyStatus
from cti_app.application.investigation_extraction_tools import (
    FLOSS_TOOL,
    extraction_tool_capabilities,
    register_extraction_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.extraction import ExtractionStatus
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.infrastructure.floss_adapter import FlossAdapter, _parse_floss_payload


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
        self.lookups: list[tuple[str, str]] = []

    async def get_by_address(self, bucket: str, sha256: str) -> object | None:
        self.lookups.append((bucket, sha256))
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

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.family_statuses = {"floss": ConfinedAnalysisFamilyStatus("floss", True, "3.1.1")}
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
        idempotency_key="floss-test-invocation",
        available_capabilities=frozenset({FLOSS_TOOL}),
    )


def _strings_result() -> dict[str, Any]:
    return {
        "status": "SUCCEEDED",
        "version": "3.1.1",
        "output_sha256": hashlib.sha256(b"floss-json-proof").hexdigest(),
        "output_bytes": 16,
        "exit_code": 0,
        "strings_observed": 2,
        "strings": [
            {
                "value": "decoded text",
                "kind": "decoded",
                "encoding": "UTF-8",
                "offset": 32,
                "address": "0x401020",
                "function": "decode_config",
                "context": "called from entry",
                "evidence_path": "decoded_strings/0",
            },
            {
                "value": "TOKEN=do-not-persist /home/analyst/private.bin",
                "kind": "stack",
                "encoding": "ASCII",
            },
        ],
        "truncated": False,
    }


def _registered(
    result: Mapping[str, Any], sample_sha256: str, *, size: int = 16, exists: bool = True
) -> tuple[InMemoryToolRegistry, FlossAdapter, _Recorder, _WorkerPort, _CanonicalBlobs]:
    blobs = _CanonicalBlobs(sample_sha256, size, exists=exists)
    worker = _WorkerPort(result)
    adapter = FlossAdapter(
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
        floss=adapter,
    )
    return registry, adapter, recorder, worker, blobs


@pytest.mark.asyncio
async def test_floss_extraction_uses_attested_worker_and_normalizes_evidence() -> None:
    sample_sha256 = hashlib.sha256(b"harmless fixture").hexdigest()
    registry, port, recorder, worker, blobs = _registered(_strings_result(), sample_sha256)

    assert port.available
    assert port.version == "3.1.1"
    assert FLOSS_TOOL in extraction_tool_capabilities(
        floss=port, configuration=None, report_import_enabled=False
    )
    definition, adapter = registry.get(FLOSS_TOOL, "1")  # type: ignore[misc]
    result = await adapter.execute({"sample_sha256": sample_sha256, "max_strings": 16}, _context())

    assert result.status is ToolInvocationStatus.SUCCEEDED
    strings = result.output["strings"]
    assert isinstance(strings, Sequence) and isinstance(strings[0], Mapping)
    assert strings[0]["type"] == "decoded"
    assert strings[0]["encoding"] == "UTF-8"
    assert strings[0]["offset"] == 32
    assert strings[0]["address"] == "0x401020"
    assert result.output["untrusted_data"] is True
    rendered = repr(result.output)
    assert "do-not-persist" not in rendered
    assert "/home/analyst" not in rendered
    assert definition.schema_version == "1"
    assert recorder.payloads[0]["raw_output_complete"] is True
    assert recorder.payloads[0]["raw_output_sha256"] == _strings_result()["output_sha256"]
    assert blobs.lookups == [(SAMPLES_QUARANTINE_BUCKET, sample_sha256)]
    request = worker.requests[0]
    assert request.tool.value == "floss"
    assert request.sha256 == sample_sha256
    assert request.analysis_parameters == {"max_strings": "16"}


@pytest.mark.asyncio
async def test_floss_worker_output_limit_and_timeout_are_partial_proofs() -> None:
    sample_sha256 = hashlib.sha256(b"safe fixture").hexdigest()
    partial = {
        "status": "OUTPUT_LIMIT",
        "version": "3.1.1",
        "output_sha256": hashlib.sha256(b"bounded partial output").hexdigest(),
        "output_bytes": 512,
        "exit_code": None,
        "strings_observed": 0,
        "strings": [],
        "truncated": True,
    }
    registry, _port, recorder, _worker, _blobs = _registered(partial, sample_sha256)
    _definition, adapter = registry.get(FLOSS_TOOL, "1")  # type: ignore[misc]
    limited = await adapter.execute({"sample_sha256": sample_sha256}, _context())
    assert limited.status is ToolInvocationStatus.PARTIAL
    assert limited.output["raw_output_complete"] is False
    assert limited.output["raw_output_sha256"] == partial["output_sha256"]
    assert recorder.payloads[0]["output_truncated"] is True

    timed_out = {**partial, "status": "TIMED_OUT", "truncated": False, "output_sha256": None}
    timeout_registry, _, _, _, _ = _registered(timed_out, sample_sha256)
    _definition, timeout_adapter = timeout_registry.get(FLOSS_TOOL, "1")  # type: ignore[misc]
    timeout = await timeout_adapter.execute({"sample_sha256": sample_sha256}, _context())
    assert timeout.status is ToolInvocationStatus.PARTIAL


@pytest.mark.asyncio
async def test_floss_refuses_missing_or_oversized_canonical_artifacts() -> None:
    sample = b"larger than configured bound"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    _registry, oversized, _recorder, worker, _blobs = _registered(
        _strings_result(), sample_sha256, size=len(sample)
    )
    oversized._max_sample_bytes = 4
    denied = await oversized.extract(sample_sha256, max_strings=4, investigation_id=uuid4())
    assert denied.status is ExtractionStatus.POLICY_DENIED
    assert denied.detail_code == "sample_too_large"
    assert worker.requests == []

    _registry, absent, _recorder, absent_worker, _blobs = _registered(
        _strings_result(), sample_sha256, exists=False
    )
    unavailable = await absent.extract(sample_sha256, max_strings=4, investigation_id=uuid4())
    assert unavailable.status is ExtractionStatus.UNAVAILABLE
    assert absent_worker.requests == []


@pytest.mark.asyncio
async def test_floss_never_runs_legacy_local_command_without_worker() -> None:
    class NeverRun:
        calls = 0

        async def run(self, *_args: object, **_kwargs: object) -> None:
            self.calls += 1
            raise AssertionError("FLOSS cannot execute outside analysis-worker")

    legacy = NeverRun()
    adapter = FlossAdapter(legacy, None, enabled=True)  # type: ignore[arg-type]
    sample_sha256 = hashlib.sha256(b"safe fixture").hexdigest()

    result = await adapter.extract(sample_sha256, max_strings=8, investigation_id=uuid4())

    assert not adapter.available
    assert adapter.unavailable_reason == "analysis_worker_unavailable"
    assert result.status is ExtractionStatus.UNAVAILABLE
    assert legacy.calls == 0


def test_floss_parser_accepts_real_nested_json_string_families() -> None:
    strings, observed = _parse_floss_payload(
        {
            "strings": {
                "static_strings": [{"string": "plain text", "offset": 8}],
                "decoded_strings": [{"string": "decoded text", "offset": 32}],
                "stack_strings": [{"string": "stack text", "address": "0x401020"}],
                "tight_strings": [{"string": "tight text", "function": "sub_1"}],
            }
        },
        max_strings=8,
    )

    assert observed == 4
    assert {item.kind.value for item in strings} == {"static", "decoded", "stack", "tight"}
