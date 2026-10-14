"""ToolRegistry adapters for passive network capture workbench operations."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.network_workbench import (
    NetworkAnalysisError,
    NetworkCaptureRejected,
    NetworkWorkbench,
)
from cti_app.domain.investigation import BudgetDimension, EvidenceKind, ToolInvocationStatus
from cti_app.domain.network_protocol import NetworkCaptureFormat, NetworkCaptureRefV1
from cti_app.infrastructure.tshark_adapter import TsharkAdapter

NETWORK_CAPTURE_INSPECT = "network.capture.inspect"
NETWORK_STREAM_REASSEMBLE = "network.stream.reassemble"
NETWORK_MESSAGES_SEGMENT = "network.messages.segment"
NETWORK_TOOL_NAMES = frozenset(
    {NETWORK_CAPTURE_INSPECT, NETWORK_STREAM_REASSEMBLE, NETWORK_MESSAGES_SEGMENT}
)
_FLOW_ID = re.compile(r"^[0-9a-f]{24}$")
_MAX_TOOL_SECONDS = 120

_CAPTURE_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blob_id": {"type": "string", "format": "uuid"},
        "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": 67_108_864},
        "format": {"type": "string", "enum": ["pcap", "pcapng"]},
    },
    "required": ["blob_id", "sha256", "size_bytes", "format"],
    "additionalProperties": False,
}


def network_tool_capabilities(tshark: TsharkAdapter) -> frozenset[str]:
    """Discover L5 registry keys without importing or starting optional tools."""
    return (
        NETWORK_TOOL_NAMES
        if tshark.available
        else frozenset({NETWORK_STREAM_REASSEMBLE, NETWORK_MESSAGES_SEGMENT})
    )


def register_network_tools(
    registry: ToolRegistry,
    *,
    workbench: NetworkWorkbench,
    recorder: EvidenceRecorder,
    tool_seconds: int = 65,
) -> None:
    if not 1 <= tool_seconds <= _MAX_TOOL_SECONDS:
        raise ValueError(f"tool_seconds must be between 1 and {_MAX_TOOL_SECONDS}")
    definitions: tuple[tuple[str, Mapping[str, Any], ToolEffect, str], ...] = (
        (
            NETWORK_CAPTURE_INSPECT,
            _capture_schema("Inspect a quarantined PCAP/PCAPNG capture without network access"),
            ToolEffect.LOCAL_COMPUTE,
            "The bounded output contains metadata and selected protocol fields only.",
        ),
        (
            NETWORK_STREAM_REASSEMBLE,
            _flow_schema("Reassemble observed TCP/UDP bytes for one capture conversation"),
            ToolEffect.LOCAL_COMPUTE,
            "Reassembly uses only bytes present in captured frames and preserves gaps/overlaps.",
        ),
        (
            NETWORK_MESSAGES_SEGMENT,
            _flow_schema("Propose message boundaries and field candidates with frame evidence"),
            ToolEffect.LOCAL_COMPUTE,
            "A candidate is never reported as decoded unless its bytes and support are measured.",
        ),
    )
    adapters: tuple[ToolAdapter, ...] = (
        _CaptureInspectAdapter(workbench, recorder, tool_seconds),
        _StreamReassembleAdapter(workbench, recorder, tool_seconds),
        _MessagesSegmentAdapter(workbench, recorder, tool_seconds),
    )
    for (name, schema, effect, limitation), adapter in zip(definitions, adapters, strict=True):
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=effect,
                cost_model={BudgetDimension.TOOL_SECONDS: tool_seconds},
                confidentiality=ToolConfidentiality.RESTRICTED,
                limitations=(
                    limitation,
                    "Le PCAP complet reste dans le blob immuable; "
                    "aucun payload brut n'est transmis au modèle.",
                    "Aucune capture live, exécution de malware ou interaction C2 n'est possible.",
                ),
                capability_key=name,
            ),
            adapter,
        )


class _NetworkToolAdapter:
    def __init__(
        self,
        workbench: NetworkWorkbench,
        recorder: EvidenceRecorder,
        reserved_seconds: int,
    ) -> None:
        self.workbench = workbench
        self.recorder = recorder
        self.reserved_seconds = reserved_seconds

    def _capture_ref(self, value: object) -> NetworkCaptureRefV1:
        if not isinstance(value, Mapping):
            raise NetworkAnalysisError("capture_ref_invalid")
        try:
            blob_id = UUID(str(value["blob_id"]))
            capture_format = NetworkCaptureFormat(str(value["format"]))
            sha256 = str(value["sha256"])
            size_bytes = value["size_bytes"]
            if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
                raise ValueError("size_bytes")
            return NetworkCaptureRefV1(blob_id, sha256, size_bytes, capture_format)
        except (KeyError, TypeError, ValueError) as exc:
            raise NetworkAnalysisError("capture_ref_invalid") from exc

    async def _record(
        self,
        context: ToolExecutionContext,
        *,
        source: str,
        object_type: str,
        object_id: str,
        payload: Mapping[str, Any],
    ) -> UUID:
        observation = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=EvidenceKind.OTHER,
            payload=payload,
            source=source,
            source_version="1",
            object_type=object_type,
            object_id=object_id,
            limitations=(
                "Les sorties sont des observations bornées, pas un décodage affirmé.",
                "Les octets complets restent référencés par le blob de capture immuable.",
            ),
            idempotency_key=context.idempotency_key,
        )
        return observation.id

    def _tool_result(
        self,
        context: ToolExecutionContext,
        *,
        status: ToolInvocationStatus,
        started: float,
        output: Mapping[str, Any],
        observation_refs: tuple[UUID, ...] = (),
    ) -> ToolResult:
        seconds = min(self.reserved_seconds, max(0, math.ceil(time.monotonic() - started)))
        normalized_output = dict(output)
        normalized_output["status"] = status.value
        alternatives = (
            (NETWORK_STREAM_REASSEMBLE, NETWORK_MESSAGES_SEGMENT)
            if status
            in {
                ToolInvocationStatus.UNAVAILABLE,
                ToolInvocationStatus.FAILED,
                ToolInvocationStatus.PARTIAL,
            }
            else ()
        )
        return ToolResult(
            status=status,
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.TOOL_SECONDS: seconds},
            suggested_alternatives=alternatives,
            output=normalized_output,
        )


class _CaptureInspectAdapter(_NetworkToolAdapter):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        try:
            reference = self._capture_ref(arguments.get("capture_ref"))
            outcome = await self.workbench.inspect(
                reference, investigation_id=context.investigation_id
            )
        except NetworkCaptureRejected as exc:
            return self._rejected(context, started, exc)
        except NetworkAnalysisError as exc:
            return self._tool_result(
                context,
                status=ToolInvocationStatus.POLICY_DENIED,
                started=started,
                output={"detail_code": exc.code},
            )
        if outcome.report is None:
            return self._tool_result(
                context,
                status=outcome.status,
                started=started,
                output={"detail_code": outcome.detail_code},
            )
        report = outcome.report
        observation_id = await self._record(
            context,
            source=NETWORK_CAPTURE_INSPECT,
            object_type="network_capture_report",
            object_id=report.capture.sha256,
            payload={"schema": "NetworkCaptureReportV1", **report.to_dict()},
        )
        return self._tool_result(
            context,
            status=outcome.status,
            started=started,
            observation_refs=(observation_id,),
            output={
                "report": report.to_dict(),
                "report_sha256": report.canonical_sha256,
                "observation_ref": str(observation_id),
            },
        )

    def _rejected(
        self,
        context: ToolExecutionContext,
        started: float,
        error: NetworkCaptureRejected,
    ) -> ToolResult:
        status = (
            ToolInvocationStatus.UNAVAILABLE
            if error.reason.value == "blob_unavailable"
            else ToolInvocationStatus.POLICY_DENIED
        )
        return self._tool_result(
            context,
            status=status,
            started=started,
            output={"detail_code": error.reason.value},
        )


class _StreamReassembleAdapter(_NetworkToolAdapter):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        try:
            reference = self._capture_ref(arguments.get("capture_ref"))
            flow_id = _flow_id(arguments.get("flow_id"))
            outcome = await self.workbench.reassemble(
                reference, flow_id, investigation_id=context.investigation_id
            )
        except NetworkCaptureRejected as exc:
            status = (
                ToolInvocationStatus.UNAVAILABLE
                if exc.reason.value == "blob_unavailable"
                else ToolInvocationStatus.POLICY_DENIED
            )
            return self._tool_result(
                context,
                status=status,
                started=started,
                output={"detail_code": exc.reason.value},
            )
        except NetworkAnalysisError as exc:
            return self._tool_result(
                context,
                status=ToolInvocationStatus.POLICY_DENIED,
                started=started,
                output={"detail_code": exc.code},
            )
        if not outcome.streams:
            return self._tool_result(
                context,
                status=outcome.status,
                started=started,
                output={"detail_code": outcome.detail_code, "streams": []},
            )
        payload = {
            "schema": "ReassembledStreamV1[]",
            "capture_sha256": reference.sha256,
            "flow_id": flow_id,
            "streams": [stream.to_dict() for stream in outcome.streams],
        }
        observation_id = await self._record(
            context,
            source=NETWORK_STREAM_REASSEMBLE,
            object_type="network_stream",
            object_id=f"{reference.sha256}:{flow_id}",
            payload=payload,
        )
        return self._tool_result(
            context,
            status=outcome.status,
            started=started,
            observation_refs=(observation_id,),
            output={**payload, "observation_ref": str(observation_id)},
        )


class _MessagesSegmentAdapter(_NetworkToolAdapter):
    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        try:
            reference = self._capture_ref(arguments.get("capture_ref"))
            flow_id = _flow_id(arguments.get("flow_id"))
            outcome = await self.workbench.segment(
                reference, flow_id, investigation_id=context.investigation_id
            )
        except NetworkCaptureRejected as exc:
            status = (
                ToolInvocationStatus.UNAVAILABLE
                if exc.reason.value == "blob_unavailable"
                else ToolInvocationStatus.POLICY_DENIED
            )
            return self._tool_result(
                context,
                status=status,
                started=started,
                output={"detail_code": exc.reason.value},
            )
        except NetworkAnalysisError as exc:
            return self._tool_result(
                context,
                status=ToolInvocationStatus.POLICY_DENIED,
                started=started,
                output={"detail_code": exc.code},
            )
        if outcome.evidence_pack is None:
            return self._tool_result(
                context,
                status=outcome.status,
                started=started,
                output={"detail_code": outcome.detail_code, "segmentations": []},
            )
        payload = {
            "schema": "MessageSegmentationV1[]",
            "capture_sha256": reference.sha256,
            "flow_id": flow_id,
            "segmentations": [item.to_dict() for item in outcome.segmentations],
            "protocol_evidence_pack": outcome.evidence_pack.to_dict(),
        }
        observation_id = await self._record(
            context,
            source=NETWORK_MESSAGES_SEGMENT,
            object_type="protocol_evidence_pack",
            object_id=f"{reference.sha256}:{flow_id}",
            payload=payload,
        )
        return self._tool_result(
            context,
            status=outcome.status,
            started=started,
            observation_refs=(observation_id,),
            output={**payload, "observation_ref": str(observation_id)},
        )


def _capture_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "capture_ref": _CAPTURE_REF_SCHEMA,
            "description": {"type": "string", "maxLength": 256},
        },
        "required": ["capture_ref"],
        "additionalProperties": False,
        "description": description,
    }


def _flow_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "capture_ref": _CAPTURE_REF_SCHEMA,
            "flow_id": {"type": "string", "pattern": "^[0-9a-f]{24}$"},
        },
        "required": ["capture_ref", "flow_id"],
        "additionalProperties": False,
        "description": description,
    }


def _flow_id(value: object) -> str:
    if not isinstance(value, str) or _FLOW_ID.fullmatch(value) is None:
        raise NetworkAnalysisError("flow_id_invalid")
    return value
