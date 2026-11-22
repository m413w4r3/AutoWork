"""Closed MCP tool catalogue for the M24-05 per-session tool shim (H2).

This module is deliberately dependency-free (stdlib only) and is the single
source of the shim's callable surface. Every entry carries a JSON-Schema
input, argument/result caps and one fixed ``(method, path template, body)``
mapping. No entry accepts a URL, HTTP method, host path, header, proxy or
shell command; route parameters are canonical UUIDs validated locally.

The catalogue mirrors the broker tool names registered by the backend at this
HEAD. Per-session narrowing is the backend tool manifest's job (ADR-0011 D8 /
SESSION-RUNNER §7); the shim never invents a tool name at runtime.

Normative sources: ``docs/design/investigation-workshop/SESSION-RUNNER.md``
§7 and ``docs/adr/ADR-0011-investigation-workshop-coordination.md`` D7.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MCP_PROTOCOL_VERSION = "2025-06-18"
# The design names no MCP revision; accept the published revisions a real CLI
# may request and answer with the newest common one.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)

MAX_FRAME_BYTES = 1024 * 1024
MAX_ARGUMENTS_BYTES = 256 * 1024
MAX_TOOL_RESULT_BYTES = 256 * 1024
MAX_RESPONSE_READ_BYTES = 16 * 1024 * 1024

BROKER_TOOL_PATH = "/api/investigations/{investigation_id}/agent-sessions/{session_id}/tool-calls"
TASK_COMMAND_PATH = "/api/investigations/{investigation_id}/tasks/{task_id}/commands"
HANDOFF_PATH = "/api/investigations/{investigation_id}/handoffs"

CONSULTATION_TYPES: tuple[str, ...] = (
    "plan_investigation",
    "pivot_strategy",
    "yara_draft",
    "review_text",
    "draft_text",
    "suricata_draft",
    "figure_plan",
    "interpretation",
    "critique",
)

_UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_VT_HASH_PATTERN = r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REVISION_PATTERN = r"^rev-[0-9a-f]{32}$"
_NO_CONTROL_PATTERN = r"^[^\x00-\x1f\x7f]*$"
_SAFE_TEXT_PATTERN = (
    r"^(?!.*https?://)"
    r"(?!.*\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|authorization|"
    r"credential|password|token)\b\s*[:=])"
    r"(?!.*[\x00-\x1f\x7f])[\s\S]*$"
)
_SAFE_TOKEN_PATTERN = r"^[A-Za-z0-9._:-]+$"

_BROKER_TOOL_TIMEOUT_SECONDS = 600.0
_BRIDGE_TIMEOUT_SECONDS = 1800.0
_COMMAND_TIMEOUT_SECONDS = 120.0


class SchemaValidationError(ValueError):
    """A locally invalid tool argument; never sent to the backend."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def canonical_json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ToolSpec:
    """One fixed MCP tool: schema, caps and backend route."""

    mcp_name: str
    broker_name: str
    schema_version: str
    description: str
    input_schema: Mapping[str, Any]
    path_template: str
    path_params: tuple[str, ...]
    body_kind: str
    command: str | None = None
    strip_fields: tuple[str, ...] = ()
    max_arguments_bytes: int = MAX_ARGUMENTS_BYTES
    max_response_bytes: int = MAX_TOOL_RESULT_BYTES
    timeout_seconds: float = _BROKER_TOOL_TIMEOUT_SECONDS
    method: str = "POST"

    def mcp_tool(self) -> dict[str, object]:
        return {
            "name": self.mcp_name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }


def _uuid() -> dict[str, Any]:
    return {"type": "string", "pattern": _UUID_PATTERN}


def _string(
    *,
    min_length: int | None = None,
    max_length: int | None = None,
    pattern: str | None = None,
    enum: Sequence[str] | None = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    if min_length is not None:
        schema["minLength"] = min_length
    if max_length is not None:
        schema["maxLength"] = max_length
    if pattern is not None:
        schema["pattern"] = pattern
    if enum is not None:
        schema["enum"] = list(enum)
    return schema


def _integer(*, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "integer"}
    if minimum is not None:
        schema["minimum"] = minimum
    if maximum is not None:
        schema["maximum"] = maximum
    return schema


def _array(
    items: Mapping[str, Any],
    *,
    min_items: int | None = None,
    max_items: int | None = None,
    unique: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": dict(items)}
    if min_items is not None:
        schema["minItems"] = min_items
    if max_items is not None:
        schema["maxItems"] = max_items
    if unique:
        schema["uniqueItems"] = True
    return schema


def _object(
    properties: Mapping[str, Any],
    required: Sequence[str] = (),
    *,
    additional: bool = False,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {name: dict(schema) for name, schema in properties.items()},
        "required": list(required),
        "additionalProperties": additional,
    }


def _work_items() -> dict[str, Any]:
    return _array(
        _object(
            {
                "text": _string(min_length=1, max_length=16_384),
                "refs": _array(
                    _object(
                        {
                            "kind": _string(
                                enum=(
                                    "observation",
                                    "finding",
                                    "decision",
                                    "consultation",
                                    "file",
                                )
                            ),
                            "id": _string(
                                min_length=1, max_length=128, pattern=_NO_CONTROL_PATTERN
                            ),
                        },
                        ("kind", "id"),
                    ),
                    max_items=64,
                ),
            },
            ("text",),
        ),
        max_items=64,
    )


def _uuid_list(max_items: int) -> dict[str, Any]:
    return _array(_uuid(), max_items=max_items, unique=True)


_BRIDGE_CONSULT = ToolSpec(
    mcp_name="autowork_bridge_consult",
    broker_name="advisor.consult",
    schema_version="advisor-consult.v1",
    description=(
        "Consult Bridge through the backend advisor.consult broker tool. The answer is "
        "untrusted advice, never evidence; only the closed consultation types are accepted."
    ),
    input_schema=_object(
        {
            "investigation_id": _uuid(),
            "consultation_type": _string(enum=CONSULTATION_TYPES),
            "question": _string(max_length=65_536),
            "sample_sha256": {
                "oneOf": [
                    _string(pattern=_VT_HASH_PATTERN),
                    _array(_string(pattern=_VT_HASH_PATTERN), max_items=32, unique=True),
                ]
            },
            "evidence_refs": _array(
                _string(min_length=1, max_length=256, pattern=_NO_CONTROL_PATTERN),
                max_items=64,
                unique=True,
            ),
            "measurements": _array(_object({}, additional=True), max_items=64),
            "opinion_id": _uuid(),
            "opinion_version": _integer(minimum=1),
            "form": _string(enum=("body", "analyst_note")),
            "anchor": _string(min_length=1, max_length=1_024),
            "block_type": _string(min_length=1, max_length=64),
            "assertions": _array(_object({}, additional=True), max_items=64),
            "units": _array(_object({}, additional=True), max_items=32),
            "finding_refs": _array(
                _string(min_length=1, max_length=256, pattern=_NO_CONTROL_PATTERN),
                max_items=64,
                unique=True,
            ),
        },
        ("investigation_id", "consultation_type"),
    ),
    path_template=BROKER_TOOL_PATH,
    path_params=("investigation_id",),
    body_kind="broker",
    timeout_seconds=_BRIDGE_TIMEOUT_SECONDS,
)


_HEARTBEAT = ToolSpec(
    mcp_name="autowork_task_heartbeat",
    broker_name="task.heartbeat",
    schema_version="1",
    description=(
        "Renew the task lease. Identical arguments replay the recorded heartbeat; set a "
        "fresh nonce for a genuine later renewal."
    ),
    input_schema=_object(
        {
            "investigation_id": _uuid(),
            "task_id": _uuid(),
            "expected_status_version": _integer(minimum=1),
            "lease_fence": _integer(minimum=0),
            "nonce": _string(min_length=1, max_length=128, pattern=_SAFE_TOKEN_PATTERN),
        },
        ("investigation_id", "task_id", "expected_status_version", "lease_fence"),
    ),
    path_template=TASK_COMMAND_PATH,
    path_params=("investigation_id", "task_id"),
    body_kind="task_command",
    command="heartbeat",
    strip_fields=("nonce",),
    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
)


_SUBMIT_RESULT = ToolSpec(
    mcp_name="autowork_task_submit_result",
    broker_name="task.submit_result",
    schema_version="1",
    description=(
        "Submit the task result refs for human review (task command submit). Identical "
        "arguments replay; set a fresh nonce only for a genuinely new submission."
    ),
    input_schema=_object(
        {
            "investigation_id": _uuid(),
            "task_id": _uuid(),
            "expected_status_version": _integer(minimum=1),
            "lease_fence": _integer(minimum=0),
            "result_refs": _array(_object({}, additional=True), max_items=128),
            "nonce": _string(min_length=1, max_length=128, pattern=_SAFE_TOKEN_PATTERN),
        },
        ("investigation_id", "task_id", "expected_status_version", "result_refs"),
    ),
    path_template=TASK_COMMAND_PATH,
    path_params=("investigation_id", "task_id"),
    body_kind="task_command",
    command="submit",
    strip_fields=("nonce",),
    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
)


_HANDOFF_SUBMIT = ToolSpec(
    mcp_name="autowork_handoff_submit",
    broker_name="handoff.submit",
    schema_version="investigation-handoff.v1",
    description=(
        "Create an investigation-handoff.v1 for human review. References are canonical "
        "ids only; the backend resolves and hashes them."
    ),
    input_schema=_object(
        {
            "investigation_id": _uuid(),
            "task_id": _uuid(),
            "to_actor": _object(
                {
                    "kind": _string(enum=("human", "operator")),
                    "actor_id": _string(min_length=1, max_length=255),
                },
                ("kind", "actor_id"),
            ),
            "work": _object(
                {
                    "hypotheses": _work_items(),
                    "attempts": _work_items(),
                    "established_results": _work_items(),
                    "partial_results": _work_items(),
                    "negative_results": _work_items(),
                    "limits": _work_items(),
                }
            ),
            "observation_ids": _uuid_list(64),
            "finding_version_ids": _uuid_list(64),
            "decision_ids": _uuid_list(64),
            "consultation_invocation_ids": _uuid_list(64),
            "file_revision_ids": _array(
                _string(pattern=_REVISION_PATTERN), max_items=64, unique=True
            ),
            "next_action": _string(min_length=1, max_length=16_384),
        },
        ("investigation_id", "task_id", "to_actor", "next_action"),
    ),
    path_template=HANDOFF_PATH,
    path_params=("investigation_id",),
    body_kind="handoff",
    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
)


def _broker_tool(
    broker_name: str,
    description: str,
    properties: Mapping[str, Any],
    required: Sequence[str],
) -> ToolSpec:
    return ToolSpec(
        mcp_name="autowork_" + broker_name.replace(".", "_"),
        broker_name=broker_name,
        schema_version="1",
        description=description,
        input_schema=_object(
            {"investigation_id": _uuid(), **properties}, ("investigation_id", *required)
        ),
        path_template=BROKER_TOOL_PATH,
        path_params=("investigation_id",),
        body_kind="broker",
        timeout_seconds=_BROKER_TOOL_TIMEOUT_SECONDS,
    )


# The static broker catalogue. A tool that is absent here is not callable at
# all; the backend manifest may narrow this list per session but can never
# widen it. Human-only tools (opinion.*, sample.set_qualification, finding.*)
# and the dynamic/local.run_command schema are deliberately absent.
_BROKER_CATALOGUE: tuple[ToolSpec, ...] = (
    _broker_tool(
        "vt.file_report",
        "VirusTotal file report for an investigation sample hash.",
        {"file_hash": _string(min_length=32, max_length=64, pattern=_VT_HASH_PATTERN)},
        ("file_hash",),
    ),
    _broker_tool(
        "vt.object_report",
        "VirusTotal IP, domain or URL report.",
        {
            "object_type": _string(enum=("ip_addresses", "domains", "urls")),
            "object_id": _string(min_length=1, max_length=2_048, pattern=_NO_CONTROL_PATTERN),
        },
        ("object_type", "object_id"),
    ),
    _broker_tool(
        "vt.relationships",
        "VirusTotal object relationships.",
        {
            "object_type": _string(enum=("file", "ip_address", "domain", "url")),
            "object_id": _string(min_length=1, max_length=2_048, pattern=_NO_CONTROL_PATTERN),
            "relation": _string(min_length=1, max_length=64, pattern=_NO_CONTROL_PATTERN),
            "cursor": _string(min_length=1, max_length=2_048, pattern=_NO_CONTROL_PATTERN),
        },
        ("object_type", "object_id", "relation"),
    ),
    _broker_tool(
        "vt.intelligence_search",
        "VirusTotal Intelligence search with investigation evidence refs.",
        {
            "query": _string(min_length=1, max_length=2_000, pattern=_SAFE_TEXT_PATTERN),
            "justification": _string(min_length=8, max_length=1_000, pattern=_SAFE_TEXT_PATTERN),
            "evidence_refs": _uuid_list(16),
            "cursor": _string(min_length=1, max_length=2_048, pattern=_NO_CONTROL_PATTERN),
            "order": _string(min_length=1, max_length=128, pattern=_NO_CONTROL_PATTERN),
        },
        ("query", "justification", "evidence_refs"),
    ),
    _broker_tool(
        "vt.behaviour",
        "VirusTotal file behaviour reports.",
        {
            "file_hash": _string(min_length=32, max_length=64, pattern=_VT_HASH_PATTERN),
            "cursor": _string(min_length=1, max_length=2_048, pattern=_NO_CONTROL_PATTERN),
        },
        ("file_hash",),
    ),
    _broker_tool(
        "vt.behaviour_pcap",
        "VirusTotal sandbox capture for a persisted behaviour report.",
        {
            "file_hash": _string(min_length=32, max_length=64, pattern=_VT_HASH_PATTERN),
            "sandbox_id": _string(min_length=1, max_length=256, pattern=_SAFE_TOKEN_PATTERN),
        },
        ("file_hash", "sandbox_id"),
    ),
    _broker_tool(
        "vt.download_sample",
        "Hash-verified VirusTotal quarantine acquisition (G2-gated, not G3).",
        {
            "sha256": _string(min_length=64, max_length=64, pattern=_SHA256_PATTERN),
            "purpose": _string(min_length=1, max_length=500),
            "evidence_refs": _array(
                _string(min_length=36, max_length=36, pattern=_UUID_PATTERN),
                max_items=32,
                unique=True,
            ),
        },
        ("sha256", "purpose", "evidence_refs"),
    ),
    _broker_tool(
        "sample.analyze",
        "Bounded static analysis of an already quarantined sample.",
        {
            "association_id": _string(min_length=36, max_length=36, pattern=_UUID_PATTERN),
            "purpose": _string(min_length=1, max_length=500),
            "evidence_refs": _array(
                _string(min_length=36, max_length=36, pattern=_UUID_PATTERN),
                max_items=32,
                unique=True,
            ),
        },
        ("association_id", "purpose", "evidence_refs"),
    ),
    _broker_tool(
        "archive.extract_member",
        "Bounded static extraction of one member from a quarantined archive.",
        {
            "association_id": _string(min_length=36, max_length=36, pattern=_UUID_PATTERN),
            "member_path": _string(min_length=1, max_length=1_024),
            "reason": _string(min_length=1, max_length=1_000),
            "expected_sha256": _string(pattern=_SHA256_PATTERN),
            "archive_preset": _string(enum=("none", "infected", "malware")),
        },
        ("association_id", "member_path", "reason"),
    ),
    _broker_tool(
        "shodan.host",
        "Passive Shodan host lookup.",
        {"ip": _string(min_length=2, max_length=45)},
        ("ip",),
    ),
    _broker_tool(
        "shodan.domain",
        "Passive Shodan domain lookup.",
        {"domain": _string(min_length=3, max_length=253)},
        ("domain",),
    ),
    _broker_tool(
        "shodan.count",
        "Passive Shodan count with investigation evidence refs.",
        {
            "query": _string(min_length=1, max_length=500),
            "justification": _string(min_length=1, max_length=1_000),
            "evidence_refs": _array(
                _string(min_length=36, max_length=36, pattern=_UUID_PATTERN),
                max_items=32,
                unique=True,
            ),
        },
        ("query", "justification", "evidence_refs"),
    ),
    _broker_tool(
        "shodan.search",
        "Passive Shodan search with investigation evidence refs.",
        {
            "query": _string(min_length=1, max_length=500),
            "justification": _string(min_length=1, max_length=1_000),
            "evidence_refs": _array(
                _string(min_length=36, max_length=36, pattern=_UUID_PATTERN),
                max_items=32,
                unique=True,
            ),
            "page": _integer(minimum=1, maximum=100),
        },
        ("query", "justification", "evidence_refs"),
    ),
)


CATALOGUE: tuple[ToolSpec, ...] = (
    _BRIDGE_CONSULT,
    _HEARTBEAT,
    _SUBMIT_RESULT,
    _HANDOFF_SUBMIT,
    *_BROKER_CATALOGUE,
)


def by_mcp_name(
    catalogue: Sequence[ToolSpec] = CATALOGUE,
) -> dict[str, ToolSpec]:
    resolved: dict[str, ToolSpec] = {}
    for spec in catalogue:
        if spec.mcp_name in resolved:
            raise ValueError(f"duplicate MCP tool name {spec.mcp_name}")
        resolved[spec.mcp_name] = spec
    return resolved


def route_path(spec: ToolSpec, session_id: str, arguments: Mapping[str, object]) -> str:
    values: dict[str, object] = {"session_id": session_id}
    for name in spec.path_params:
        values[name] = arguments[name]
    return spec.path_template.format(**values)


def build_body(spec: ToolSpec, arguments: Mapping[str, object]) -> dict[str, object]:
    """Build the fixed request body; route parameters and nonces never leak in."""
    stripped = {
        name: value
        for name, value in arguments.items()
        if name not in spec.path_params and name not in spec.strip_fields
    }
    if spec.body_kind == "broker":
        return {
            "tool_name": spec.broker_name,
            "schema_version": spec.schema_version,
            "arguments": stripped,
        }
    if spec.body_kind == "task_command":
        body: dict[str, object] = {
            "command": spec.command,
            "expected_status_version": stripped["expected_status_version"],
        }
        for optional in ("lease_fence", "result_refs"):
            if optional in stripped:
                body[optional] = stripped[optional]
        return body
    if spec.body_kind == "handoff":
        return dict(stripped)
    raise ValueError(f"unknown body kind {spec.body_kind}")


def _is_type(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaValidationError("$", f"unsupported_type:{expected}")


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def validate_arguments(schema: Mapping[str, Any], value: object) -> None:
    """Validate the JSON-Schema subset used by the catalogue.

    Raises :class:`SchemaValidationError` with the offending field and a
    stable reason. Unknown keywords are not interpreted; every catalogue
    schema is exercised by the H2 tests.
    """
    _validate(schema, value, "")


def _validate(schema: Mapping[str, Any], value: object, path: str) -> None:
    branches = schema.get("oneOf")
    if isinstance(branches, list):
        matches = 0
        for branch in branches:
            if not isinstance(branch, Mapping):
                raise SchemaValidationError(path or "$", "invalid_schema")
            try:
                _validate(branch, value, path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(path or "$", "one_of")
        return
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, str(item)) for item in types):
            raise SchemaValidationError(path or "$", "type")
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        raise SchemaValidationError(path or "$", "enum")
    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise SchemaValidationError(path or "$", "min_length")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            raise SchemaValidationError(path or "$", "max_length")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise SchemaValidationError(path or "$", "pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise SchemaValidationError(path or "$", "minimum")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise SchemaValidationError(path or "$", "maximum")
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise SchemaValidationError(path or "$", "min_items")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            raise SchemaValidationError(path or "$", "max_items")
        if schema.get("uniqueItems") is True:
            seen = {canonical_json_bytes(item) for item in value}
            if len(seen) != len(value):
                raise SchemaValidationError(path or "$", "unique_items")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate(items, item, f"{path}[{index}]" if path else f"[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties")
        property_map = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if name not in value:
                    raise SchemaValidationError(_join(path, str(name)), "required")
        additional = schema.get("additionalProperties")
        for name, item in value.items():
            if name in property_map:
                item_schema = property_map[name]
                if isinstance(item_schema, Mapping):
                    _validate(item_schema, item, _join(path, str(name)))
            elif additional is False:
                raise SchemaValidationError(_join(path, str(name)), "additional_property")
            elif isinstance(additional, Mapping):
                _validate(additional, item, _join(path, str(name)))


def _validate_catalogue(catalogue: Sequence[ToolSpec]) -> None:
    names: set[str] = set()
    for spec in catalogue:
        if spec.mcp_name in names:
            raise ValueError(f"duplicate MCP tool name {spec.mcp_name}")
        names.add(spec.mcp_name)
        if spec.method not in {"POST"}:
            raise ValueError(f"{spec.mcp_name} must use a fixed POST route")
        if spec.body_kind not in {"broker", "task_command", "handoff"}:
            raise ValueError(f"{spec.mcp_name} has no fixed body builder")
        if not spec.path_template.startswith("/api/investigations/"):
            raise ValueError(f"{spec.mcp_name} must stay under /api/investigations/")
        placeholders = set(re.findall(r"\{([a-z_]+)\}", spec.path_template))
        allowed = set(spec.path_params) | {"session_id"}
        if placeholders - allowed:
            raise ValueError(f"{spec.mcp_name} has an unbounded path placeholder")
        if set(spec.path_params) - placeholders:
            raise ValueError(f"{spec.mcp_name} declares an unused path parameter")
        required = spec.input_schema.get("required")
        required_names = set(required) if isinstance(required, list) else set()
        if not set(spec.path_params) <= required_names:
            raise ValueError(f"{spec.mcp_name} must require every path parameter")
        if spec.input_schema.get("additionalProperties") is not False:
            raise ValueError(f"{spec.mcp_name} must reject unknown arguments")
        if spec.max_arguments_bytes <= 0 or spec.max_response_bytes <= 0:
            raise ValueError(f"{spec.mcp_name} must bound arguments and responses")
        if spec.timeout_seconds <= 0:
            raise ValueError(f"{spec.mcp_name} must bound the backend timeout")


_validate_catalogue(CATALOGUE)


__all__ = [
    "BROKER_TOOL_PATH",
    "CATALOGUE",
    "CONSULTATION_TYPES",
    "HANDOFF_PATH",
    "MAX_ARGUMENTS_BYTES",
    "MAX_FRAME_BYTES",
    "MAX_RESPONSE_READ_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "MCP_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "TASK_COMMAND_PATH",
    "SchemaValidationError",
    "ToolSpec",
    "build_body",
    "by_mcp_name",
    "canonical_json_bytes",
    "canonical_json_text",
    "route_path",
    "validate_arguments",
]
