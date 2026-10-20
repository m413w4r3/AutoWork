from __future__ import annotations

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.confined_analysis import ConfinedAnalysisFamilyStatus
from cti_app.application.dotnet_analysis import ILSPYCMD_PACKAGE_SHA512, ILSPYCMD_VERSION
from cti_app.application.investigation_dotnet_tools import (
    DOTNET_TOOL_NAMES,
    dotnet_tool_capabilities,
    register_dotnet_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry, ToolExecutionContext
from cti_app.config import Settings
from cti_app.domain.entities import SampleState
from cti_app.domain.investigation import EvidenceKind, InvestigationBudget, ToolInvocationStatus
from tests.dotnet_fixture import MINIMAL_DOTNET_ASSEMBLY

SAMPLE_SHA256 = "a" * 64


class _UowContext(AbstractAsyncContextManager):
    def __init__(self, uow: SimpleNamespace) -> None:
        self.uow = uow

    async def __aenter__(self) -> SimpleNamespace:
        return self.uow

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Execution:
    available = True

    def __init__(self) -> None:
        self.requests = []
        self.result = {
            "status": "SUCCEEDED",
            "operation": "methods",
            "version": "6.8.0.105",
            "source": {"sha256": SAMPLE_SHA256},
            "records": [
                {
                    "rid": 1,
                    "token": "0x06000001",
                    "type_name": "Fixture",
                    "method_name": "Main",
                    "signature": "int32 Main()",
                }
            ],
            "total_items": 1,
            "offset": 0,
            "next_offset": None,
            "truncated": False,
        }
        self.family_statuses = {
            "dotnet": ConfinedAnalysisFamilyStatus("dotnet", True, "9.1.0.7988"),
            "local_command": ConfinedAnalysisFamilyStatus(
                "local_command", True, "local-command-catalog-v1"
            ),
        }

    async def execute(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            status=ToolInvocationStatus.SUCCEEDED,
            available_reason=None,
            payload={"result": self.result},
        )


class _Recorder:
    def __init__(self) -> None:
        self.calls = []
        self.record_id = uuid4()

    async def record_json(self, **kwargs):
        self.calls.append(kwargs)
        encoded = json.dumps(
            kwargs["payload"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return SimpleNamespace(id=self.record_id, sha256=hashlib.sha256(encoded).hexdigest())


@pytest.mark.asyncio
async def test_dotnet_tools_use_canonical_blob_and_report_the_actual_engine() -> None:
    investigation_id = uuid4()
    association_id = uuid4()
    sample_id = uuid4()
    blob_id = uuid4()
    subject_id = uuid4()
    association = SimpleNamespace(id=association_id, sample_sha256=SAMPLE_SHA256)
    associations = SimpleNamespace(
        list_for_investigation=lambda requested_id: _list_associations(
            requested_id, investigation_id, association
        )
    )
    blob = SimpleNamespace(
        id=blob_id,
        descriptor=SimpleNamespace(sha256=SAMPLE_SHA256, size=len(MINIMAL_DOTNET_ASSEMBLY)),
    )
    sample = SimpleNamespace(id=sample_id, blob_id=blob_id, state=SampleState.QUARANTINED)
    uow = SimpleNamespace(
        analyst_investigations=SimpleNamespace(
            get=lambda requested_id: _get_investigation(requested_id, investigation_id, subject_id)
        ),
        blobs=SimpleNamespace(get_by_address=lambda _bucket, _sha: _return(blob)),
        samples=SimpleNamespace(get_by_subject_and_blob=lambda _subject, _blob: _return(sample)),
    )

    def uow_factory() -> _UowContext:
        return _UowContext(uow)

    execution = _Execution()
    recorder = _Recorder()
    registry = InMemoryToolRegistry()

    assert dotnet_tool_capabilities(
        execution=execution,
        sample_associations=associations,
        recorder=recorder,
    ) == frozenset({"investigation.dotnet"})
    register_dotnet_tools(
        registry,
        execution=execution,
        sample_associations=associations,
        recorder=recorder,
        uow_factory=uow_factory,
        settings=Settings(_env_file=None),
    )
    assert {definition.name for definition in registry.list_registered()} == set(DOTNET_TOOL_NAMES)
    found = registry.get("methods.list", "1")
    assert found is not None
    _, adapter = found
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(),
        idempotency_key="dotnet-methods-fixture",
        available_capabilities=frozenset({"investigation.dotnet"}),
    )

    result = await adapter.execute(
        {"sample_sha256": SAMPLE_SHA256, "offset": 0, "limit": 20}, context
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["sample_sha256"] == SAMPLE_SHA256
    assert result.output["result"]["records"][0]["token"] == "0x06000001"
    assert result.output["source"]["engine"] == "monodis"
    assert result.output["source"]["version"] == "6.8.0.105"
    assert "package_sha512" not in result.output["source"]
    assert result.observation_refs == (recorder.record_id,)
    assert execution.requests[0].artifact_id == blob_id
    assert execution.requests[0].association_id == association_id
    assert execution.requests[0].sample_id == sample_id
    assert execution.requests[0].analysis_parameters == {
        "operation": "methods",
        "offset": "0",
        "limit": "20",
    }
    evidence = recorder.calls[0]
    assert evidence["kind"] is EvidenceKind.BINARY_ANALYSIS
    assert evidence["source"] == "dotnet.monodis"
    assert evidence["source_version"] == "dotnet-analysis-observation.v1:monodis:6.8.0.105"
    assert evidence["payload"]["source"]["engine"] == "monodis"
    assert evidence["payload"]["schema_version"] == "dotnet-analysis-observation.v1"
    assert evidence["payload"]["sample_sha256"] == SAMPLE_SHA256
    assert any("C2" in item for item in evidence["limitations"])

    execution.result = {
        "status": "PARTIAL",
        "operation": "decompile",
        "engine": "monodis-fallback",
        "version": "6.8.0.105",
        "method_rid": 1,
        "method_token": "0x06000001",
        "type_name": "Fixture",
        "method_name": "Main",
        "stdout": "IL_0000: ldc.i4.7\nIL_0001: ret",
        "offset": 0,
        "length_chars": 25,
        "total_chars": 25,
        "next_offset": None,
        "truncated": False,
        "interpretation": "Fallback CIL disassembly; decompilation was unavailable.",
    }
    decompile_found = registry.get("method.decompile", "1")
    assert decompile_found is not None
    _, decompile_adapter = decompile_found
    decompile_result = await decompile_adapter.execute(
        {"sample_sha256": SAMPLE_SHA256, "method_rid": 1}, context
    )

    assert decompile_result.status is ToolInvocationStatus.SUCCEEDED
    assert decompile_result.output["source"]["engine"] == "monodis-fallback"
    assert decompile_result.output["source"]["version"] == "6.8.0.105"
    assert decompile_result.output["result"]["engine"] == "monodis-fallback"

    full_cil = "".join(f"IL_{index:04x}: nop\n" for index in range(2_600))
    execution.result = {
        "status": "SUCCEEDED",
        "operation": "decompile",
        "engine": "monodis-fallback",
        "version": "6.8.0.105",
        "method_rid": 32,
        "method_token": "0x06000020",
        "type_name": "Blackwood",
        "method_name": "FixtureMethod",
        "stdout": full_cil,
        "offset": 0,
        "length_chars": len(full_cil),
        "total_chars": len(full_cil),
        "next_offset": None,
        "truncated": False,
    }
    full_result = await decompile_adapter.execute(
        {"sample_sha256": SAMPLE_SHA256, "method_rid": 32, "max_chars": 12_000}, context
    )
    full_evidence = recorder.calls[-1]["payload"]
    full_json = "".join(full_evidence["result_json_chunks"])
    full_json_bytes = full_json.encode("utf-8")
    assert len(full_cil.encode("utf-8")) > 32_000
    assert json.loads(full_json)["stdout"] == full_cil
    assert full_evidence["result_json_sha256"] == hashlib.sha256(full_json_bytes).hexdigest()
    assert max(map(len, full_evidence["result_json_chunks"])) <= 4_000
    assert full_evidence["result_truncated_by_tool_boundary"] is False
    assert full_result.output["result_truncated_by_response"] is True
    assert full_result.output["evidence_observation_id"] == str(recorder.record_id)
    assert full_result.output["evidence_observation_url"].endswith(
        f"{recorder.record_id}?offset=0&length=16384"
    )
    assert execution.requests[-1].analysis_parameters["max_chars"] == "12000"

    execution.result = {
        "status": "SUCCEEDED",
        "operation": "decompile",
        "engine": "ilspycmd",
        "version": ILSPYCMD_VERSION,
        "package_sha512": ILSPYCMD_PACKAGE_SHA512,
        "method_rid": 1,
        "method_token": "0x06000001",
        "type_name": "Fixture",
        "method_name": "Main",
        "stdout": "IL_0000: ldc.i4.7\nIL_0001: ret",
        "offset": 0,
        "length_chars": 25,
        "total_chars": 25,
        "next_offset": None,
        "truncated": False,
    }
    ilspy_result = await decompile_adapter.execute(
        {"sample_sha256": SAMPLE_SHA256, "method_rid": 1}, context
    )

    assert ilspy_result.status is ToolInvocationStatus.SUCCEEDED
    assert ilspy_result.output["source"]["engine"] == "ilspycmd"
    assert ilspy_result.output["source"]["version"] == ILSPYCMD_VERSION
    assert ilspy_result.output["source"]["package_sha512"] == ILSPYCMD_PACKAGE_SHA512
    assert recorder.calls[-1]["source"] == "dotnet.ilspycmd"


async def _list_associations(requested_id, expected_id, association):
    return (association,) if requested_id == expected_id else ()


async def _get_investigation(requested_id, expected_id, subject_id):
    return SimpleNamespace(subject_id=subject_id) if requested_id == expected_id else None


async def _return(value):
    return value
