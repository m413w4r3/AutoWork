from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.investigation_service import (
    InvestigationService,
    _operation_key_matches,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
)
from cti_app.domain.investigation import InvestigationBudget, ToolInvocationStatus


class _VersionedAdapter:
    def __init__(self, version: str) -> None:
        self.worker_version = version
        self.result_schema_version = "confined-analysis-result.v2"
        self.calls = 0

    @property
    def operation_cache_identity(self) -> Mapping[str, Any]:
        return {
            "result_schema_version": self.result_schema_version,
            "worker_attestation": {"versions": {"capa": self.worker_version}},
        }

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        self.calls += 1
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            output={"run": self.calls},
        )


def _service_for(registry: InMemoryToolRegistry) -> InvestigationService:
    service = object.__new__(InvestigationService)
    service._registry = registry
    return service


def _local_definition() -> ToolDefinition:
    return ToolDefinition(
        name="sample.analyze",
        schema_version="1",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=ToolEffect.LOCAL_COMPUTE,
        cost_model={},
        confidentiality=ToolConfidentiality.RESTRICTED,
        limitations=(),
        capability_key="investigation.sample_analysis",
    )


@pytest.mark.asyncio
async def test_confined_operation_reuses_same_worker_version_and_runs_on_version_change() -> None:
    registry = InMemoryToolRegistry()
    adapter = _VersionedAdapter("capa-9.4.0")
    registry.register(_local_definition(), adapter)
    service = _service_for(registry)
    investigation_id = uuid4()
    arguments: Mapping[str, Any] = {}

    def operation_key() -> str:
        return service._tool_operation_key(
            investigation_id=investigation_id,
            tool_name="sample.analyze",
            schema_version="1",
            normalized_parameters=arguments,
            purpose="static analysis",
        )

    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(),
        idempotency_key="analysis-v1",
        available_capabilities=frozenset({"investigation.sample_analysis"}),
    )
    dispatcher = RegistryToolDispatcher(registry)

    async def dispatch(key: str) -> ToolResult:
        return await dispatcher.dispatch(
            context,
            tool_name="sample.analyze",
            schema_version="1",
            arguments=arguments,
            purpose="static analysis",
            operation_key=key,
        )

    first_key = operation_key()
    assert first_key == operation_key()
    first = await dispatch(first_key)

    assert first.output["run"] == 1
    assert (await dispatch(first_key)).output["run"] == 1
    assert adapter.calls == 1

    adapter.worker_version = "capa-9.5.0"
    second_key = operation_key()
    assert second_key != first_key
    assert not _operation_key_matches(first_key, second_key)
    assert _operation_key_matches(first_key, first_key)
    second = await dispatch(second_key)

    assert second.output["run"] == 2
    assert adapter.calls == 2

    adapter.result_schema_version = "confined-analysis-result.v3"
    third_key = operation_key()
    assert third_key != second_key
    assert (await dispatch(third_key)).output["run"] == 3
    assert adapter.calls == 3


def test_external_tool_operation_key_does_not_include_worker_identity() -> None:
    registry = InMemoryToolRegistry()
    adapter = _VersionedAdapter("worker-a")
    definition = ToolDefinition(
        name="vt.file_report",
        schema_version="1",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=ToolEffect.EXTERNAL_READ,
        cost_model={},
        confidentiality=ToolConfidentiality.RESTRICTED,
        limitations=(),
        capability_key="virustotal.file_report",
    )
    registry.register(definition, adapter)
    service = _service_for(registry)
    investigation_id = uuid4()

    first = service._tool_operation_key(
        investigation_id=investigation_id,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={},
        purpose="passive report",
    )
    adapter.worker_version = "worker-b"
    second = service._tool_operation_key(
        investigation_id=investigation_id,
        tool_name="vt.file_report",
        schema_version="1",
        normalized_parameters={},
        purpose="passive report",
    )

    assert first == second
