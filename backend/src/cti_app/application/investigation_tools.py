"""Tool registry and dispatcher contracts for investigation tools."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import (
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    create_model,
)

from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
    ToolInvocationStatus,
    canonical_json_bytes,
    freeze_json,
    thaw_json,
)


class ToolEffect(StrEnum):
    LOCAL_READ = "local_read"
    EXTERNAL_READ = "external_read"
    LOCAL_COMPUTE = "local_compute"
    ACQUIRE = "acquire"
    EXTERNAL_ACTIVE = "external_active"
    # Discloses a bounded artefact to a provider and spends durable provider quota
    # (e.g. a reviewed YARA rule). Provider and disclosure policy apply like an
    # external read; the tool itself enforces its own explicit human authorization.
    EXTERNAL_SUBMIT = "external_submit"


class ToolConfidentiality(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "default",
        "examples",
        "deprecated",
        "format",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
    }
)


def _validate_registered_schema(schema: Mapping[str, Any], path: str = "input_schema") -> None:
    unsupported = set(schema) - _SUPPORTED_SCHEMA_KEYWORDS
    if unsupported:
        names = ", ".join(sorted(str(name) for name in unsupported))
        raise ValueError(f"Unsupported JSON Schema keywords at {path}: {names}")
    schema_type = schema.get("type")
    supported_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if schema_type is not None:
        types = schema_type if isinstance(schema_type, list) else [schema_type]
        if not types or any(item not in supported_types for item in types):
            raise ValueError(f"Invalid JSON Schema type at {path}")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ValueError(f"Invalid JSON Schema properties at {path}")
    for name, child in properties.items():
        if not isinstance(name, str) or not isinstance(child, Mapping):
            raise ValueError(f"Invalid property definition at {path}")
        _validate_registered_schema(child, f"{path}.{name}")
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, Mapping):
            raise ValueError(f"Invalid items schema at {path}")
        _validate_registered_schema(items, f"{path}[]")
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, (bool, Mapping)):
        raise ValueError(f"Invalid additionalProperties schema at {path}")
    if isinstance(additional, Mapping):
        _validate_registered_schema(additional, f"{path}.*")
    required = schema.get("required", ())
    if not isinstance(required, list | tuple) or not all(
        isinstance(item, str) for item in required
    ):
        raise ValueError(f"Invalid required list at {path}")
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword, ())
        if not isinstance(branches, list | tuple) or not all(
            isinstance(branch, Mapping) for branch in branches
        ):
            raise ValueError(f"Invalid {keyword} schema at {path}")
        for index, branch in enumerate(branches):
            _validate_registered_schema(branch, f"{path}.{keyword}[{index}]")
    not_schema = schema.get("not")
    if not_schema is not None:
        if not isinstance(not_schema, Mapping):
            raise ValueError(f"Invalid not schema at {path}")
        _validate_registered_schema(not_schema, f"{path}.not")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (TypeError, re.error) as exc:
            raise ValueError(f"Invalid regular expression at {path}") from exc


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    schema_version: str
    input_schema: Mapping[str, Any]
    effect: ToolEffect
    cost_model: Mapping[BudgetDimension, int]
    confidentiality: ToolConfidentiality
    limitations: tuple[str, ...]
    capability_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.effect, ToolEffect):
            raise TypeError("effect must use ToolEffect")
        if not isinstance(self.confidentiality, ToolConfidentiality):
            raise TypeError("confidentiality must use ToolConfidentiality")
        if not all(
            value.strip() for value in (self.name, self.schema_version, self.capability_key)
        ):
            raise ValueError("Tool name, schema version, and capability key are required")
        if not isinstance(self.input_schema, Mapping) or self.input_schema.get("type") != "object":
            raise ValueError("Tool input_schema must be a JSON Schema object schema")
        if self.input_schema.get("$schema") not in (
            None,
            "https://json-schema.org/draft/2020-12/schema",
        ):
            raise ValueError("Unsupported JSON Schema draft")
        _validate_registered_schema(self.input_schema)
        costs: dict[BudgetDimension, int] = {}
        for dimension, amount in self.cost_model.items():
            if not isinstance(dimension, BudgetDimension):
                raise TypeError("Tool costs must use BudgetDimension")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError("Tool costs must be non-negative integers")
            costs[dimension] = amount
        object.__setattr__(self, "input_schema", freeze_json(self.input_schema))
        object.__setattr__(self, "cost_model", freeze_json(costs))
        object.__setattr__(self, "limitations", tuple(self.limitations))


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: ToolInvocationStatus
    observation_refs: tuple[UUID, ...] = ()
    budget_consumed: Mapping[BudgetDimension, int] = field(default_factory=dict)
    suggested_alternatives: tuple[str, ...] = ()
    output: Mapping[str, Any] = field(default_factory=dict)
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.status, ToolInvocationStatus):
            raise TypeError("status must use ToolInvocationStatus")
        if not isinstance(self.output, Mapping):
            raise TypeError("output must be a JSON object")
        normalized: dict[BudgetDimension, int] = {}
        for dimension, amount in self.budget_consumed.items():
            if not isinstance(dimension, BudgetDimension):
                raise TypeError("Consumed budget dimensions must use BudgetDimension")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ValueError("Consumed budget must be non-negative integers")
            normalized[dimension] = amount
        object.__setattr__(self, "observation_refs", tuple(self.observation_refs))
        object.__setattr__(self, "budget_consumed", freeze_json(normalized))
        object.__setattr__(self, "suggested_alternatives", tuple(self.suggested_alternatives))
        object.__setattr__(self, "output", freeze_json(self.output))


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    investigation_id: UUID
    envelope: InvestigationEnvelope | None
    budget: InvestigationBudget
    idempotency_key: str
    available_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("Tool execution context requires an idempotency key")
        if self.envelope is not None:
            if self.investigation_id != self.envelope.investigation_id:
                raise ValueError("Tool execution context and envelope investigations must match")
            if self.budget is not self.envelope.budget:
                raise ValueError(
                    "Tool execution budget must be the budget approved in its envelope"
                )
        object.__setattr__(self, "available_capabilities", frozenset(self.available_capabilities))


class ToolAdapter(Protocol):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult: ...


class ToolRegistry(Protocol):
    def register(self, definition: ToolDefinition, adapter: ToolAdapter) -> None: ...

    def unregister(self, name: str, schema_version: str | None = None) -> bool: ...

    def list_available(self, context: ToolExecutionContext) -> tuple[ToolDefinition, ...]: ...

    def get(self, name: str, schema_version: str) -> tuple[ToolDefinition, ToolAdapter] | None: ...


class InMemoryToolRegistry:
    """Small injectable registry; availability is resolved from live capabilities."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], tuple[ToolDefinition, ToolAdapter]] = {}

    def register(self, definition: ToolDefinition, adapter: ToolAdapter) -> None:
        key = (definition.name, definition.schema_version)
        if key in self._tools:
            raise ValueError(
                f"Tool {definition.name}@{definition.schema_version} is already registered"
            )
        self._tools[key] = (definition, adapter)

    def unregister(self, name: str, schema_version: str | None = None) -> bool:
        keys = tuple(
            key
            for key in self._tools
            if key[0] == name and (schema_version is None or key[1] == schema_version)
        )
        for key in keys:
            del self._tools[key]
        return bool(keys)

    def list_available(self, context: ToolExecutionContext) -> tuple[ToolDefinition, ...]:
        return tuple(
            definition
            for definition, _adapter in self._tools.values()
            if definition.capability_key in context.available_capabilities
        )

    def get(self, name: str, schema_version: str) -> tuple[ToolDefinition, ToolAdapter] | None:
        return self._tools.get((name, schema_version))


class ToolDispatcher(Protocol):
    async def dispatch(
        self,
        context: ToolExecutionContext,
        *,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        operation_key: str,
    ) -> ToolResult: ...


def _schema_type(schema: Mapping[str, Any], name: str) -> Any:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        supported_types = {"object", "array", "string", "integer", "number", "boolean", "null"}
        if not all(item in supported_types for item in schema_type):
            raise ValueError(f"Unsupported JSON Schema type union at {name}")
        return Any
    if (
        schema_type is None
        and not any(keyword in schema for keyword in ("properties", "required"))
        and any(keyword in schema for keyword in ("anyOf", "oneOf", "allOf", "not"))
    ):
        return Any
    if schema_type is None and not any(
        keyword in schema
        for keyword in ("properties", "required", "anyOf", "oneOf", "allOf", "not")
    ):
        return Any
    if schema_type in (None, "object"):
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ValueError(f"Invalid properties schema at {name}")
        required = schema.get("required", ())
        if not isinstance(required, (list, tuple)) or not all(
            isinstance(item, str) for item in required
        ):
            raise ValueError(f"Invalid required list at {name}")
        fields: dict[str, tuple[Any, Any]] = {}
        for property_name, property_schema in properties.items():
            if not isinstance(property_schema, Mapping):
                raise ValueError(f"Invalid property schema at {name}.{property_name}")
            annotation = _schema_type(property_schema, f"{name}.{property_name}")
            fields[property_name] = (annotation, ... if property_name in required else None)
        additional = schema.get("additionalProperties", False)
        extra_mode: Literal["forbid", "allow"] = "forbid" if additional is False else "allow"
        return create_model(
            f"Input_{re.sub(r'[^A-Za-z0-9_]', '_', name)}",
            __config__=ConfigDict(strict=True, extra=extra_mode),
            **cast(Any, fields),
        )
    if schema_type == "array":
        items = schema.get("items", {})
        if not isinstance(items, Mapping):
            raise ValueError(f"Invalid array item schema at {name}")
        return list[Any]
    if schema_type == "string":
        return StrictStr
    if schema_type == "integer":
        return StrictInt
    if schema_type == "number":
        return int | float
    if schema_type == "boolean":
        return StrictBool
    if schema_type == "null":
        return type(None)
    raise ValueError(f"Unsupported JSON Schema type {schema_type!r} at {name}")


def _check_constraints(value: Any, schema: Mapping[str, Any], path: str) -> None:
    schema_type = schema.get("type")
    allowed_types = schema_type if isinstance(schema_type, list) else (schema_type,)
    if schema_type is not None and not any(
        (item == "object" and isinstance(value, Mapping))
        or (item == "array" and isinstance(value, list))
        or (item == "string" and isinstance(value, str))
        or (item == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (item == "number" and isinstance(value, int | float) and not isinstance(value, bool))
        or (item == "boolean" and isinstance(value, bool))
        or (item == "null" and value is None)
        for item in allowed_types
    ):
        raise ValueError(f"{path} does not match the declared JSON type")
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if branches is None:
            continue
        if not isinstance(branches, list | tuple) or not all(
            isinstance(branch, Mapping) for branch in branches
        ):
            raise ValueError(f"{path} has an invalid {keyword} schema")
        matched = 0
        for branch in branches:
            try:
                TypeAdapter(_schema_type(branch, path)).validate_python(value, strict=True)
                _check_constraints(value, branch, path)
                matched += 1
            except (ValidationError, ValueError, TypeError, re.error):
                continue
        if keyword == "allOf" and matched != len(branches):
            raise ValueError(f"{path} does not satisfy every allOf schema")
        if keyword == "anyOf" and matched == 0:
            raise ValueError(f"{path} does not satisfy any anyOf schema")
        if keyword == "oneOf" and matched != 1:
            raise ValueError(f"{path} must satisfy exactly one oneOf schema")
    not_schema = schema.get("not")
    if isinstance(not_schema, Mapping):
        try:
            TypeAdapter(_schema_type(not_schema, path)).validate_python(value, strict=True)
            _check_constraints(value, not_schema, path)
        except (ValidationError, ValueError, TypeError, re.error):
            pass
        else:
            raise ValueError(f"{path} matches a forbidden schema")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of the declared enum values")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} does not match the declared const value")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path} is shorter than minLength")
        if len(value) > schema.get("maxLength", 2**63):
            raise ValueError(f"{path} is longer than maxLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ValueError(f"{path} does not match pattern")
    elif isinstance(value, bool):
        pass
    elif isinstance(value, int | float):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path} is not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValueError(f"{path} is not below exclusiveMaximum")
        if "multipleOf" in schema and value % schema["multipleOf"] != 0:
            raise ValueError(f"{path} is not a multipleOf the declared value")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 2**63):
            raise ValueError(f"{path} item count is outside its bounds")
        if schema.get("uniqueItems") and len(
            {canonical_json_bytes({"value": item}) for item in value}
        ) != len(value):
            raise ValueError(f"{path} contains duplicate items")
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            TypeAdapter(_schema_type(item_schema, f"{path}[{index}]")).validate_python(
                item, strict=True
            )
            _check_constraints(item, item_schema, f"{path}[{index}]")
    elif isinstance(value, Mapping):
        if len(value) < schema.get("minProperties", 0):
            raise ValueError(f"{path} has fewer properties than minProperties")
        if len(value) > schema.get("maxProperties", 2**63):
            raise ValueError(f"{path} has more properties than maxProperties")
        required = schema.get("required", ())
        missing = set(required) - set(value)
        if missing:
            raise ValueError(f"{path} is missing required properties: {', '.join(sorted(missing))}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", False)
        for key, item in value.items():
            if key in properties:
                TypeAdapter(_schema_type(properties[key], f"{path}.{key}")).validate_python(
                    item, strict=True
                )
                _check_constraints(item, properties[key], f"{path}.{key}")
            elif isinstance(additional, Mapping):
                TypeAdapter(_schema_type(additional, f"{path}.{key}")).validate_python(
                    item, strict=True
                )
                _check_constraints(item, additional, f"{path}.{key}")
            elif additional is False:
                raise ValueError(f"{path}.{key} is not allowed")


def validate_json_schema(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate a registered JSON Schema using strict Pydantic types and constraints."""
    if not isinstance(arguments, Mapping):
        raise ValueError("Tool arguments must be a JSON object")
    thawed = thaw_json(freeze_json(arguments))
    try:
        TypeAdapter(_schema_type(schema, "arguments")).validate_python(thawed, strict=True)
        _check_constraints(thawed, schema, "arguments")
    except (ValidationError, ValueError, TypeError, re.error) as exc:
        raise ValueError(str(exc)) from exc


class RegistryToolDispatcher:
    """Dispatch only registered, capable, policy-approved and budgeted tools."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._completed: dict[str, tuple[bytes, ToolResult]] = {}
        self._lock = asyncio.Lock()

    async def dispatch(
        self,
        context: ToolExecutionContext,
        *,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        operation_key: str,
    ) -> ToolResult:
        async with self._lock:
            if not operation_key.strip() or not purpose.strip():
                return self._local_result(
                    ToolInvocationStatus.FAILED,
                    "Tool purpose and operation key must not be empty.",
                )
            try:
                identity = canonical_json_bytes(
                    {
                        "investigation_id": str(context.investigation_id),
                        "idempotency_key": operation_key,
                        "tool_name": tool_name,
                        "schema_version": schema_version,
                        "arguments": arguments,
                        "purpose": purpose,
                    }
                )
            except (TypeError, ValueError):
                return self._local_result(
                    ToolInvocationStatus.FAILED, "Tool arguments must contain only JSON values."
                )
            return await self._dispatch_once(
                replace(context, idempotency_key=operation_key),
                tool_name=tool_name,
                schema_version=schema_version,
                arguments=arguments,
                purpose=purpose,
                operation_key=operation_key,
                identity=identity,
            )

    async def _dispatch_once(
        self,
        context: ToolExecutionContext,
        *,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        operation_key: str,
        identity: bytes,
    ) -> ToolResult:
        replay = self._completed.get(operation_key)
        if replay is not None:
            if replay[0] != identity:
                return self._local_result(
                    ToolInvocationStatus.FAILED,
                    "Operation key was reused with different tool input.",
                )
            return replay[1]
        alternatives = self._suggested_alternatives(context, exclude_name=tool_name)
        registered = self._registry.get(tool_name, schema_version)
        if registered is None:
            return self._local_result(
                ToolInvocationStatus.UNAVAILABLE, "Tool is not registered.", alternatives
            )
        definition, adapter = registered
        if definition.capability_key not in context.available_capabilities:
            return self._local_result(
                ToolInvocationStatus.UNAVAILABLE,
                f"Capability {definition.capability_key} is unavailable.",
                alternatives,
            )
        policy_status = self._policy_status(definition, context.envelope)
        if policy_status is not None:
            return self._local_result(
                policy_status,
                f"Policy does not allow {definition.name}.",
                alternatives,
                request_permission=True,
            )
        try:
            validate_json_schema(arguments, thaw_json(definition.input_schema))
        except ValueError as exc:
            return self._local_result(ToolInvocationStatus.FAILED, f"Invalid tool arguments: {exc}")

        try:
            context.budget.reserve(operation_key, thaw_json(definition.cost_model))
        except BudgetExceeded as exc:
            return self._local_result(
                ToolInvocationStatus.NEEDS_REVIEW,
                f"Budget limit reached for {exc.dimension.value}.",
                alternatives,
                request_permission=True,
            )
        try:
            result = await adapter.execute(arguments, context)
        except Exception as exc:
            # A failed external call may have completed remotely; charge its reservation
            # and cache the result so replay cannot submit it a second time.
            context.budget.commit(operation_key)
            result = self._local_result(
                ToolInvocationStatus.FAILED, f"Tool execution failed: {type(exc).__name__}"
            )
            self._completed[operation_key] = (identity, result)
            return result
        try:
            consumed = result.budget_consumed or thaw_json(definition.cost_model)
            context.budget.commit(operation_key, consumed)
        except (KeyError, ValueError):
            context.budget.commit(operation_key)
            result = self._local_result(
                ToolInvocationStatus.FAILED,
                "Tool returned a budget amount outside its reservation.",
            )
        self._completed[operation_key] = (identity, result)
        return result

    @staticmethod
    def _policy_status(
        definition: ToolDefinition, envelope: InvestigationEnvelope | None
    ) -> ToolInvocationStatus | None:
        if definition.effect is ToolEffect.EXTERNAL_ACTIVE:
            return ToolInvocationStatus.NEEDS_REVIEW
        if definition.effect in (
            ToolEffect.ACQUIRE,
            ToolEffect.EXTERNAL_READ,
            ToolEffect.EXTERNAL_SUBMIT,
        ):
            if envelope is None:
                return ToolInvocationStatus.POLICY_DENIED
            provider = definition.capability_key.partition(".")[0]
            if provider not in envelope.allowed_providers:
                return ToolInvocationStatus.POLICY_DENIED
            if envelope.disclosure.tlp is TLPLevel.RED or envelope.disclosure.do_not_submit:
                return ToolInvocationStatus.POLICY_DENIED
            if definition.effect is ToolEffect.ACQUIRE and not envelope.acquire_hits_allowed:
                return ToolInvocationStatus.POLICY_DENIED
        return None

    def _suggested_alternatives(
        self, context: ToolExecutionContext, *, exclude_name: str
    ) -> tuple[str, ...]:
        remaining = context.budget.remaining
        alternatives = []
        for definition in self._registry.list_available(context):
            if definition.name == exclude_name:
                continue
            if self._policy_status(definition, context.envelope) is not None:
                continue
            exceeds_remaining = False
            for dimension, amount in definition.cost_model.items():
                available = remaining[dimension]
                if available is not None and amount > available:
                    exceeds_remaining = True
                    break
            if exceeds_remaining:
                continue
            alternatives.append(definition.name)
        return tuple(alternatives)

    @staticmethod
    def _local_result(
        status: ToolInvocationStatus,
        message: str,
        alternatives: tuple[str, ...] = (),
        *,
        request_permission: bool = False,
    ) -> ToolResult:
        output = {"request_permission": True} if request_permission else {}
        return ToolResult(
            status,
            suggested_alternatives=alternatives,
            output=output,
            message=message,
        )


__all__ = [
    "InMemoryToolRegistry",
    "RegistryToolDispatcher",
    "ToolAdapter",
    "ToolConfidentiality",
    "ToolDefinition",
    "ToolDispatcher",
    "ToolEffect",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "validate_json_schema",
]
