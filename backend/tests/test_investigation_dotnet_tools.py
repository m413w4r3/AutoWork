from __future__ import annotations

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
            payload={
                "result": {
                    "status": "SUCCEEDED",
                    "operation": "methods",
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
            },
        )


class _Recorder:
    def __init__(self) -> None:
        self.calls = []
        self.record_id = uuid4()

    async def record_json(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=self.record_id)


@pytest.mark.asyncio
async def test_methods_tool_uses_canonical_blob_and_persists_versioned_evidence() -> None:
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
    assert evidence["source"] == "dotnet.ilspycmd"
    assert ILSPYCMD_VERSION in evidence["source_version"]
    assert ILSPYCMD_PACKAGE_SHA512 in evidence["source_version"]
    assert evidence["payload"]["schema_version"] == "dotnet-analysis-observation.v1"
    assert evidence["payload"]["sample_sha256"] == SAMPLE_SHA256
    assert any("C2" in item for item in evidence["limitations"])


async def _list_associations(requested_id, expected_id, association):
    return (association,) if requested_id == expected_id else ()


async def _get_investigation(requested_id, expected_id, subject_id):
    return SimpleNamespace(subject_id=subject_id) if requested_id == expected_id else None


async def _return(value):
    return value
