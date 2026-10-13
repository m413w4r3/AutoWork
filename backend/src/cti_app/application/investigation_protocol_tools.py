"""ToolRegistry adapters for deterministic, local network protocol analysis.

Composition call contract:
register_protocol_tools(
    registry,
    service=NetworkProtocolWorkbench(network_workbench),
    recorder=evidence_recorder,
    model_store=ProductionProtocolModelStore(production_artifact_store),
)
This module registers definitions only. It does not modify investigation composition.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.network_protocol_model import (
    HypothesisNotReplayableError,
    NetworkProtocolWorkbench,
    ProtocolTraceError,
    ProtocolTraceRequestV1,
)
from cti_app.application.network_workbench import NetworkCaptureRejected
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.domain.investigation import BudgetDimension, EvidenceKind, ToolInvocationStatus
from cti_app.domain.network_protocol import NetworkCaptureFormat, NetworkCaptureRefV1
from cti_app.domain.protocol_model import (
    ProtocolHypothesisProposalV1,
    ProtocolModelV1,
)

NETWORK_PROTOCOL_COMPARE = "network.protocol.compare"
NETWORK_PROTOCOL_HYPOTHESIZE = "network.protocol.hypothesize"
NETWORK_PROTOCOL_VERIFY = "network.protocol.verify"
NETWORK_PROTOCOL_RENDER = "network.protocol.render"
PROTOCOL_TOOL_NAMES = frozenset(
    {
        NETWORK_PROTOCOL_COMPARE,
        NETWORK_PROTOCOL_HYPOTHESIZE,
        NETWORK_PROTOCOL_VERIFY,
        NETWORK_PROTOCOL_RENDER,
    }
)
_MAX_TOOL_SECONDS = 120
_MODEL_BUCKET = "network-protocol-models"
_FLOW_PATTERN = "^[0-9a-f]{24}$"
_SHA_PATTERN = "^[0-9a-f]{64}$"
_UUID_SCHEMA: dict[str, Any] = {"type": "string", "format": "uuid"}
_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blob_id": _UUID_SCHEMA,
        "sha256": {"type": "string", "pattern": _SHA_PATTERN},
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": 67_108_864},
        "format": {"type": "string", "enum": ["pcap", "pcapng"]},
    },
    "required": ["blob_id", "sha256", "size_bytes", "format"],
    "additionalProperties": False,
}
_TRACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capture_ref": _CAPTURE_SCHEMA,
        "flow_id": {"type": "string", "pattern": _FLOW_PATTERN},
        "sample_sha256": {"type": ["string", "null"], "pattern": _SHA_PATTERN},
        "report_id": {"type": ["string", "null"], "format": "uuid"},
        "holdout": {"type": "boolean"},
    },
    "required": ["capture_ref", "flow_id", "sample_sha256", "report_id", "holdout"],
    "additionalProperties": False,
}
_TEST_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["message_length", "field_value", "state_transition", "direction"],
        },
        "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "field_id": {"type": ["string", "null"], "maxLength": 128},
        "transition_id": {"type": ["string", "null"], "maxLength": 128},
    },
    "required": ["kind", "message_id", "field_id", "transition_id"],
    "additionalProperties": False,
}
_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "enum": ["ProtocolHypothesisProposalV1"]},
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "ordinal": {"type": "integer", "minimum": 1, "maximum": 10_000},
                    "direction": {
                        "type": "string",
                        "enum": [
                            "originator_to_responder",
                            "responder_to_originator",
                            "unresolved",
                        ],
                    },
                    "min_length": {"type": "integer", "minimum": 0, "maximum": 8_388_608},
                    "max_length": {"type": "integer", "minimum": 0, "maximum": 8_388_608},
                    "encrypted_opaque": {"type": "boolean"},
                },
                "required": [
                    "message_id",
                    "name",
                    "ordinal",
                    "direction",
                    "min_length",
                    "max_length",
                    "encrypted_opaque",
                ],
                "additionalProperties": False,
            },
        },
        "fields": {
            "type": "array",
            "maxItems": 256,
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 8_388_608},
                    "length": {"type": "integer", "minimum": 1, "maximum": 65_536},
                    "endianness": {
                        "type": ["string", "null"],
                        "enum": ["big", "little", "none", "unknown", None],
                    },
                    "encoding": {"type": ["string", "null"], "maxLength": 64},
                    "condition": {"type": ["string", "null"], "maxLength": 256},
                    "expected_hex": {
                        "type": ["string", "null"],
                        "pattern": "^(?:[0-9a-f]{2}){1,16}$",
                    },
                    "encrypted_opaque": {"type": "boolean"},
                },
                "required": [
                    "field_id",
                    "message_id",
                    "name",
                    "offset",
                    "length",
                    "endianness",
                    "encoding",
                    "condition",
                    "expected_hex",
                    "encrypted_opaque",
                ],
                "additionalProperties": False,
            },
        },
        "transitions": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "properties": {
                    "transition_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "from_state": {"type": "string", "minLength": 1, "maxLength": 128},
                    "to_state": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "trigger": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["transition_id", "from_state", "to_state", "message_id", "trigger"],
                "additionalProperties": False,
            },
        },
        "test_plan": {
            "type": "array",
            "maxItems": 256,
            "items": _TEST_STEP_SCHEMA,
        },
    },
    "required": ["schema_version", "messages", "fields", "transitions", "test_plan"],
    "additionalProperties": False,
}


class ProtocolModelBlobStore(Protocol):
    async def put_model(self, model: ProtocolModelV1) -> tuple[UUID, str]: ...

    async def get_model(self, blob_id: UUID) -> ProtocolModelV1: ...


class ProductionProtocolModelStore:
    """Store semantic models as immutable canonical JSON blobs."""

    def __init__(self, artifacts: ProductionArtifactStore) -> None:
        self._artifacts = artifacts

    async def put_model(self, model: ProtocolModelV1) -> tuple[UUID, str]:
        encoded = model.canonical_json_bytes()
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"protocol model exceeds {MAX_ARTIFACT_BYTES} bytes")
        blob_id = await self._artifacts.put_bytes(
            encoded,
            bucket=_MODEL_BUCKET,
            mime_type="application/json",
        )
        return blob_id, model.canonical_sha256

    async def get_model(self, blob_id: UUID) -> ProtocolModelV1:
        return ProtocolModelV1.from_dict(await self._artifacts.read_json(blob_id))


def protocol_tool_capabilities() -> frozenset[str]:
    """Return L6 capability keys; no optional binary is needed for local replay."""
    return PROTOCOL_TOOL_NAMES


def register_protocol_tools(
    registry: ToolRegistry,
    *,
    service: NetworkProtocolWorkbench,
    recorder: EvidenceRecorder,
    model_store: ProtocolModelBlobStore,
    tool_seconds: int = 65,
) -> None:
    if not 1 <= tool_seconds <= _MAX_TOOL_SECONDS:
        raise ValueError(f"tool_seconds must be between 1 and {_MAX_TOOL_SECONDS}")
    definitions = (
        (
            NETWORK_PROTOCOL_COMPARE,
            _operation_schema(
                "Compare observed messages and fields across captures.",
                properties={
                    "subject_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "traces": _trace_array_schema(),
                },
                required=["subject_id", "traces"],
            ),
        ),
        (
            NETWORK_PROTOCOL_HYPOTHESIZE,
            _operation_schema(
                "Replay a strict schema and state-machine hypothesis on training traces.",
                properties={
                    "subject_id": {"type": "string", "minLength": 1, "maxLength": 256},
                    "proposal": _PROPOSAL_SCHEMA,
                    "traces": _trace_array_schema(),
                },
                required=["subject_id", "proposal", "traces"],
            ),
        ),
        (
            NETWORK_PROTOCOL_VERIFY,
            _operation_schema(
                "Check a stored model against holdout captures.",
                properties={
                    "model_blob_id": _UUID_SCHEMA,
                    "traces": _trace_array_schema(),
                },
                required=["model_blob_id", "traces"],
            ),
        ),
        (
            NETWORK_PROTOCOL_RENDER,
            _operation_schema(
                "Produce semantic sequence, state-machine, and message-format specs.",
                properties={"model_blob_id": _UUID_SCHEMA},
                required=["model_blob_id"],
            ),
        ),
    )
    limitations = (
        "Les hypothèses sont des structures typées validées contre des flux locaux.",
        "Les octets bruts restent dans les blobs de capture immuables et ne sont jamais retournés.",
        "encrypted_opaque reste opaque; les champs et transitions citent leurs frames et offsets.",
    )
    for name, schema in definitions:
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=ToolEffect.LOCAL_COMPUTE,
                cost_model={BudgetDimension.TOOL_SECONDS: tool_seconds},
                confidentiality=ToolConfidentiality.RESTRICTED,
                limitations=limitations,
                capability_key=name,
            ),
            _ProtocolToolAdapter(
                name,
                service=service,
                recorder=recorder,
                model_store=model_store,
                reserved_seconds=tool_seconds,
            ),
        )


class _ProtocolToolAdapter:
    def __init__(
        self,
        name: str,
        *,
        service: NetworkProtocolWorkbench,
        recorder: EvidenceRecorder,
        model_store: ProtocolModelBlobStore,
        reserved_seconds: int,
    ) -> None:
        self.name = name
        self.service = service
        self.recorder = recorder
        self.model_store = model_store
        self.reserved_seconds = reserved_seconds

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        started = time.monotonic()
        try:
            if self.name == NETWORK_PROTOCOL_COMPARE:
                model_result = await self.service.compare(
                    subject_id=_required_str(arguments, "subject_id"),
                    investigation_id=context.investigation_id,
                    traces=_trace_requests(arguments.get("traces")),
                )
                model = model_result.model
                blob_id, stored_sha = await self.model_store.put_model(model)
                output = {
                    "schema": "ProtocolModelV1",
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "compatible": model_result.compatible,
                    "stable_field_ids": list(model_result.stable_field_ids),
                    "variant_field_ids": list(model_result.variant_field_ids),
                    "family_assurance": model.family_assurance.value,
                    "message_count": len(model.messages),
                }
                observation = await self._record(context, model, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation)
                return self._result(
                    context, started, ToolInvocationStatus.SUCCEEDED, output, (observation,)
                )

            if self.name == NETWORK_PROTOCOL_HYPOTHESIZE:
                proposal = ProtocolHypothesisProposalV1.from_dict(arguments.get("proposal"))
                model = await self.service.hypothesize(
                    subject_id=_required_str(arguments, "subject_id"),
                    investigation_id=context.investigation_id,
                    proposal=proposal,
                    traces=_trace_requests(arguments.get("traces")),
                )
                blob_id, stored_sha = await self.model_store.put_model(model)
                output = {
                    "schema": "ProtocolModelV1",
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "validation_status": model.validation_status.value,
                    "message_count": len(model.messages),
                    "field_count": len(model.fields),
                    "transition_count": len(model.transitions),
                    "test_plan": [step.to_dict() for step in model.test_plan],
                }
                observation = await self._record(context, model, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation)
                return self._result(
                    context, started, ToolInvocationStatus.SUCCEEDED, output, (observation,)
                )

            if self.name == NETWORK_PROTOCOL_VERIFY:
                model = await self.model_store.get_model(_uuid(arguments.get("model_blob_id")))
                verified = await self.service.verify(
                    model=model,
                    traces=_trace_requests(arguments.get("traces")),
                )
                blob_id, stored_sha = await self.model_store.put_model(verified)
                output = {
                    "schema": "ProtocolModelV1",
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "validation_status": verified.validation_status.value,
                    "finding_count": len(verified.verification_findings),
                    "contradiction_count": sum(
                        item.validation_status.value == "contradicted"
                        for item in verified.verification_findings
                    ),
                    "unresolved_count": sum(
                        item.validation_status.value == "unresolved"
                        for item in verified.verification_findings
                    ),
                }
                observation = await self._record(context, verified, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation)
                status = (
                    ToolInvocationStatus.PARTIAL
                    if verified.validation_status.value in {"contradicted", "unresolved"}
                    else ToolInvocationStatus.SUCCEEDED
                )
                return self._result(context, started, status, output, (observation,))

            model = await self.model_store.get_model(_uuid(arguments.get("model_blob_id")))
            rendered = self.service.render(model)
            observation = await self.recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.OTHER,
                payload=rendered.to_dict(),
                source=NETWORK_PROTOCOL_RENDER,
                source_version="1",
                object_type="protocol_diagram_specs",
                object_id=rendered.canonical_sha256,
                limitations=(
                    "Specs sémantiques; le lot L7 projette ces données vers DiagramSpecV1/D2.",
                    "Aucun D2 libre fourni par le modèle n'est compilé.",
                ),
                idempotency_key=context.idempotency_key,
            )
            output = {
                **rendered.to_dict(),
                "observation_ref": str(observation.id),
            }
            return self._result(
                context,
                started,
                ToolInvocationStatus.SUCCEEDED,
                output,
                (observation.id,),
            )
        except HypothesisNotReplayableError as exc:
            return self._result(
                context,
                started,
                ToolInvocationStatus.POLICY_DENIED,
                {"detail_code": "hypothesis_not_replayable", "detail": str(exc)[:256]},
            )
        except NetworkCaptureRejected as exc:
            status = (
                ToolInvocationStatus.UNAVAILABLE
                if exc.reason.value == "blob_unavailable"
                else ToolInvocationStatus.POLICY_DENIED
            )
            return self._result(
                context,
                started,
                status,
                {"detail_code": exc.reason.value},
            )
        except (ProtocolTraceError, ValueError, TypeError, KeyError) as exc:
            return self._result(
                context,
                started,
                ToolInvocationStatus.POLICY_DENIED,
                {"detail_code": "protocol_input_invalid", "detail": str(exc)[:256]},
            )

    async def _record(
        self,
        context: ToolExecutionContext,
        model: ProtocolModelV1,
        blob_id: UUID,
        stored_sha: str,
        summary: Mapping[str, Any],
    ) -> UUID:
        observation = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=EvidenceKind.OTHER,
            payload={
                "schema": "ProtocolModelV1",
                "model_blob_id": str(blob_id),
                "model_sha256": stored_sha,
                "model_canonical_sha256": model.canonical_sha256,
                "summary": dict(summary),
            },
            source=self.name,
            source_version="1",
            object_type="protocol_model",
            object_id=stored_sha,
            limitations=(
                "Le modèle complet est un blob JSON canonique immuable.",
                "Les identifiants de capture pointent vers les blobs PCAP immuables.",
            ),
            idempotency_key=context.idempotency_key,
        )
        return observation.id

    def _result(
        self,
        context: ToolExecutionContext,
        started: float,
        status: ToolInvocationStatus,
        output: Mapping[str, Any],
        observation_refs: tuple[UUID, ...] = (),
    ) -> ToolResult:
        consumed = min(
            self.reserved_seconds,
            max(0, math.ceil(time.monotonic() - started)),
        )
        return ToolResult(
            status=status,
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.TOOL_SECONDS: consumed},
            output={**output, "status": status.value},
        )


def _operation_schema(
    description: str,
    *,
    properties: Mapping[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": dict(properties),
        "required": required,
        "additionalProperties": False,
    }


def _trace_array_schema() -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "maxItems": 32, "items": _TRACE_SCHEMA}


def _required_str(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("UUID value must be a string")
    return UUID(value)


def _capture_ref(value: object) -> NetworkCaptureRefV1:
    if not isinstance(value, Mapping):
        raise ValueError("capture_ref must be an object")
    allowed = {"blob_id", "sha256", "size_bytes", "format"}
    if set(value) != allowed:
        raise ValueError("capture_ref has missing or unknown properties")
    size = value["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("capture size_bytes must be an integer")
    return NetworkCaptureRefV1(
        blob_id=_uuid(value["blob_id"]),
        sha256=_required_nested_str(value, "sha256"),
        size_bytes=size,
        format=NetworkCaptureFormat(_required_nested_str(value, "format")),
    )


def _trace_requests(value: object) -> tuple[ProtocolTraceRequestV1, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("traces must be a non-empty array")
    result: list[ProtocolTraceRequestV1] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("trace must be an object")
        allowed = {"capture_ref", "flow_id", "sample_sha256", "report_id", "holdout"}
        if set(item) != allowed:
            raise ValueError("trace has missing or unknown properties")
        sample_sha = item["sample_sha256"]
        if sample_sha is not None and not isinstance(sample_sha, str):
            raise ValueError("sample_sha256 must be a string or null")
        report_id = None if item["report_id"] is None else _uuid(item["report_id"])
        holdout = item["holdout"]
        if not isinstance(holdout, bool):
            raise ValueError("holdout must be a boolean")
        result.append(
            ProtocolTraceRequestV1(
                capture=_capture_ref(item["capture_ref"]),
                flow_id=_required_nested_str(item, "flow_id"),
                sample_sha256=sample_sha,
                report_id=report_id,
                holdout=holdout,
            )
        )
    return tuple(result)


def _required_nested_str(value: Mapping[str, Any], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return field
