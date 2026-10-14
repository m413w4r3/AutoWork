from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolResult,
    validate_json_schema,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    InvestigationBudget,
    InvestigationEnvelope,
    ToolInvocationStatus,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments, context) -> ToolResult:
        self.calls += 1
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            budget_consumed={BudgetDimension.VT_UNITS: 1},
            output={"queried": arguments["sha256"]},
        )


class AmbiguousFailureAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments, context) -> ToolResult:
        self.calls += 1
        raise TimeoutError("remote result may be unknown")


def _definition(name="vt.file_report", effect=ToolEffect.EXTERNAL_READ):
    return ToolDefinition(
        name=name,
        schema_version="1",
        input_schema={
            "type": "object",
            "properties": {"sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "required": ["sha256"],
            "additionalProperties": False,
        },
        effect=effect,
        cost_model={BudgetDimension.VT_UNITS: 1},
        confidentiality=ToolConfidentiality.INTERNAL,
        limitations=("provider coverage may be incomplete",),
        capability_key="vt.report",
    )


def _tool_seconds_definition(name: str, seconds: int) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        schema_version="1",
        input_schema={
            "type": "object",
            "properties": {"sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "required": ["sha256"],
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_COMPUTE,
        cost_model={BudgetDimension.TOOL_SECONDS: seconds},
        confidentiality=ToolConfidentiality.INTERNAL,
        limitations=("Bounded local computation.",),
        capability_key="vt.report",
    )


def _context(
    *,
    capabilities=frozenset({"vt.report"}),
    vt_limit=4,
    tool_seconds_limit: int | None = None,
    allowed=("vt",),
):
    investigation_id = uuid4()
    limits = {BudgetDimension.VT_UNITS: vt_limit}
    if tool_seconds_limit is not None:
        limits[BudgetDimension.TOOL_SECONDS] = tool_seconds_limit
    budget = InvestigationBudget(limits=limits)
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=allowed,
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(external_llm_allowed=True),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst:1",
        approved_at=datetime.now(UTC),
    )
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key="turn-1",
        available_capabilities=capabilities,
    )


@pytest.mark.asyncio
async def test_unknown_tool_and_absent_capability_return_local_unavailable() -> None:
    registry = InMemoryToolRegistry()
    adapter = FakeAdapter()
    registry.register(_definition(), adapter)
    dispatcher = RegistryToolDispatcher(registry)
    arguments = {"sha256": "a" * 64}

    unknown = await dispatcher.dispatch(
        _context(),
        tool_name="vt.not_registered",
        schema_version="1",
        arguments=arguments,
        purpose="test",
        operation_key="unknown-op",
    )
    unavailable = await dispatcher.dispatch(
        _context(capabilities=frozenset()),
        tool_name="vt.file_report",
        schema_version="1",
        arguments=arguments,
        purpose="test",
        operation_key="no-capability-op",
    )
    assert unknown.status is ToolInvocationStatus.UNAVAILABLE
    assert unavailable.status is ToolInvocationStatus.UNAVAILABLE
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_registered_schema_is_validated_before_adapter_execution() -> None:
    registry = InMemoryToolRegistry()
    adapter = FakeAdapter()
    registry.register(_definition(), adapter)
    result = await RegistryToolDispatcher(registry).dispatch(
        _context(),
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"sha256": "not-a-hash", "unexpected": True},
        purpose="test",
        operation_key="invalid-args-op",
    )
    assert result.status is ToolInvocationStatus.FAILED
    assert adapter.calls == 0


def test_json_schema_combinators_and_nested_array_types_are_enforced() -> None:
    schema = {
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            }
        },
        "required": ["values"],
        "additionalProperties": False,
    }
    validate_json_schema({"values": ["marker", 3]}, schema)
    with pytest.raises(ValueError, match="anyOf"):
        validate_json_schema({"values": [True]}, schema)


def test_unsupported_schema_keywords_fail_at_registration() -> None:
    with pytest.raises(ValueError, match="Unsupported JSON Schema keywords"):
        ToolDefinition(
            name="local.lookup",
            schema_version="1",
            input_schema={"type": "object", "$ref": "#/$defs/input"},
            effect=ToolEffect.LOCAL_READ,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=(),
            capability_key="local.lookup",
        )


@pytest.mark.asyncio
async def test_registry_capability_discovery_and_add_remove_need_no_dispatcher_changes() -> None:
    registry = InMemoryToolRegistry()
    adapter = FakeAdapter()
    definition = _definition()
    dispatcher = RegistryToolDispatcher(registry)
    registry.register(definition, adapter)
    context = _context()
    assert [tool.name for tool in registry.list_available(context)] == [definition.name]
    registry.unregister(definition.name, definition.schema_version)
    assert registry.list_available(context) == ()
    assert registry.get(definition.name, definition.schema_version) is None
    result = await dispatcher.dispatch(
        context,
        tool_name=definition.name,
        schema_version=definition.schema_version,
        arguments={"sha256": "a" * 64},
        purpose="after removal",
        operation_key="removed-tool-op",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_dispatch_reserves_budget_and_replays_idempotently() -> None:
    registry = InMemoryToolRegistry()
    adapter = FakeAdapter()
    registry.register(_definition(), adapter)
    dispatcher = RegistryToolDispatcher(registry)
    context = _context()
    kwargs = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"sha256": "b" * 64},
        "purpose": "check claim C1",
        "operation_key": "deterministic-op",
    }
    first = await dispatcher.dispatch(context, **kwargs)
    replay = await dispatcher.dispatch(context, **kwargs)
    collision = await dispatcher.dispatch(
        context,
        **{**kwargs, "arguments": {"sha256": "c" * 64}},
    )
    assert first == replay
    assert collision.status is ToolInvocationStatus.FAILED
    assert adapter.calls == 1
    assert context.budget.consumed[BudgetDimension.VT_UNITS] == 1


@pytest.mark.asyncio
async def test_ambiguous_adapter_failure_is_not_resubmitted_on_replay() -> None:
    registry = InMemoryToolRegistry()
    adapter = AmbiguousFailureAdapter()
    registry.register(_definition(), adapter)
    dispatcher = RegistryToolDispatcher(registry)
    context = _context()
    kwargs = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"sha256": "0" * 64},
        "purpose": "check ambiguous request",
        "operation_key": "ambiguous-op",
    }
    first = await dispatcher.dispatch(context, **kwargs)
    replay = await dispatcher.dispatch(context, **kwargs)
    assert first.status is ToolInvocationStatus.FAILED
    assert replay == first
    assert adapter.calls == 1
    assert context.budget.consumed[BudgetDimension.VT_UNITS] == 1


@pytest.mark.asyncio
async def test_budget_and_envelope_policy_refuse_only_the_selected_tool() -> None:
    registry = InMemoryToolRegistry()
    adapter = FakeAdapter()
    registry.register(_definition(), adapter)
    dispatcher = RegistryToolDispatcher(registry)
    exhausted = _context(vt_limit=0)
    over_budget = await dispatcher.dispatch(
        exhausted,
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"sha256": "c" * 64},
        purpose="test",
        operation_key="budget-op",
    )
    disallowed_provider = _context(allowed=())
    policy_denied = await dispatcher.dispatch(
        disallowed_provider,
        tool_name="vt.file_report",
        schema_version="1",
        arguments={"sha256": "d" * 64},
        purpose="test",
        operation_key="policy-op",
    )
    assert over_budget.status is ToolInvocationStatus.NEEDS_REVIEW
    assert policy_denied.status is ToolInvocationStatus.POLICY_DENIED
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_tool_seconds_overage_refuses_only_the_selected_operation() -> None:
    class ToolSecondsAdapter:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, arguments, context) -> ToolResult:
            del arguments, context
            self.calls += 1
            return ToolResult(
                ToolInvocationStatus.SUCCEEDED,
                budget_consumed={BudgetDimension.TOOL_SECONDS: 1},
            )

    registry = InMemoryToolRegistry()
    over_budget_adapter = ToolSecondsAdapter()
    available_adapter = ToolSecondsAdapter()
    registry.register(_tool_seconds_definition("local.large", 2), over_budget_adapter)
    registry.register(_tool_seconds_definition("local.small", 1), available_adapter)
    dispatcher = RegistryToolDispatcher(registry)
    context = _context(tool_seconds_limit=1)
    arguments = {"sha256": "f" * 64}

    refused = await dispatcher.dispatch(
        context,
        tool_name="local.large",
        schema_version="1",
        arguments=arguments,
        purpose="Run bounded local computation.",
        operation_key="local-large-op",
    )
    allowed = await dispatcher.dispatch(
        context,
        tool_name="local.small",
        schema_version="1",
        arguments=arguments,
        purpose="Run smaller local computation.",
        operation_key="local-small-op",
    )

    assert refused.status is ToolInvocationStatus.NEEDS_REVIEW
    assert over_budget_adapter.calls == 0
    assert allowed.status is ToolInvocationStatus.SUCCEEDED
    assert available_adapter.calls == 1


@pytest.mark.asyncio
async def test_acquisition_and_active_effects_have_distinct_policy_outcomes() -> None:
    registry = InMemoryToolRegistry()
    acquire = _definition("vt.download_sample", ToolEffect.ACQUIRE)
    active = _definition("shodan.scan", ToolEffect.EXTERNAL_ACTIVE)
    registry.register(acquire, FakeAdapter())
    registry.register(active, FakeAdapter())
    dispatcher = RegistryToolDispatcher(registry)
    context = _context()
    acquire_result = await dispatcher.dispatch(
        context,
        tool_name=acquire.name,
        schema_version="1",
        arguments={"sha256": "e" * 64},
        purpose="inspect candidate",
        operation_key="acquire-op",
    )
    active_result = await dispatcher.dispatch(
        context,
        tool_name=active.name,
        schema_version="1",
        arguments={"sha256": "f" * 64},
        purpose="probe service",
        operation_key="active-op",
    )
    assert acquire_result.status is ToolInvocationStatus.POLICY_DENIED
    assert active_result.status is ToolInvocationStatus.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_external_submit_follows_provider_policy_and_is_not_an_active_probe() -> None:
    """A reviewed-artefact submission is governed like an external read, not always paused."""
    registry = InMemoryToolRegistry()
    submit = _definition("vt.retrohunt.submit", ToolEffect.EXTERNAL_SUBMIT)
    adapter = FakeAdapter()
    registry.register(submit, adapter)
    dispatcher = RegistryToolDispatcher(registry)
    arguments = {"sha256": "a" * 64}

    allowed = await dispatcher.dispatch(
        _context(),
        tool_name=submit.name,
        schema_version="1",
        arguments=arguments,
        purpose="submit the reviewed rule",
        operation_key="submit-allowed",
    )
    denied = await dispatcher.dispatch(
        _context(allowed=("shodan",)),
        tool_name=submit.name,
        schema_version="1",
        arguments=arguments,
        purpose="submit the reviewed rule",
        operation_key="submit-denied",
    )

    assert allowed.status is ToolInvocationStatus.SUCCEEDED
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert adapter.calls == 1
