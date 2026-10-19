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
from cti_app.infrastructure.confined_suricata import (
    ConfinedSuricataAdapter,
    validate_suricata_rule_source,
)
from cti_app.infrastructure.tshark_adapter import TsharkAdapter

NETWORK_CAPTURE_INSPECT = "network.capture.inspect"
NETWORK_STREAM_REASSEMBLE = "network.stream.reassemble"
NETWORK_MESSAGES_SEGMENT = "network.messages.segment"
NETWORK_SURICATA_VALIDATE_RULES = "network.suricata_validate_rules"
NETWORK_SURICATA_REPLAY = "network.suricata_replay"
NETWORK_TOOL_NAMES = frozenset(
    {NETWORK_CAPTURE_INSPECT, NETWORK_STREAM_REASSEMBLE, NETWORK_MESSAGES_SEGMENT}
)
NETWORK_SURICATA_TOOL_NAMES = frozenset({NETWORK_SURICATA_VALIDATE_RULES, NETWORK_SURICATA_REPLAY})
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
    names = set(
        NETWORK_TOOL_NAMES
        if tshark.available
        else {NETWORK_STREAM_REASSEMBLE, NETWORK_MESSAGES_SEGMENT}
    )
    suricata = _suricata_adapter(tshark)
    if suricata is not None and suricata.available:
        names.update(NETWORK_SURICATA_TOOL_NAMES)
    return frozenset(names)


def register_network_tools(
    registry: ToolRegistry,
    *,
    workbench: NetworkWorkbench,
    recorder: EvidenceRecorder,
    tool_seconds: int = 65,
) -> None:
    if not 1 <= tool_seconds <= _MAX_TOOL_SECONDS:
        raise ValueError(f"tool_seconds must be between 1 and {_MAX_TOOL_SECONDS}")
    definitions: list[tuple[str, Mapping[str, Any], ToolEffect, str]] = [
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
    ]
    adapters: list[ToolAdapter] = [
        _CaptureInspectAdapter(workbench, recorder, tool_seconds),
        _StreamReassembleAdapter(workbench, recorder, tool_seconds),
        _MessagesSegmentAdapter(workbench, recorder, tool_seconds),
    ]
    suricata = _suricata_adapter(workbench.tshark, timeout_seconds=tool_seconds)
    if suricata is not None and suricata.available:
        definitions.extend(
            (
                (
                    NETWORK_SURICATA_VALIDATE_RULES,
                    _suricata_schema(
                        "Validate a versioned offline Suricata ruleset for an associated capture"
                    ),
                    ToolEffect.LOCAL_COMPUTE,
                    "Runs Suricata configuration and rules validation with -T; "
                    "does not read packet contents.",
                ),
                (
                    NETWORK_SURICATA_REPLAY,
                    _suricata_schema("Replay an associated quarantined PCAP offline with Suricata"),
                    ToolEffect.LOCAL_COMPUTE,
                    "Runs one offline -r replay and archives bounded EVE JSON as a canonical blob.",
                ),
            )
        )
        adapters.extend(
            (
                _SuricataToolAdapter(workbench, recorder, tool_seconds, suricata, replay=False),
                _SuricataToolAdapter(workbench, recorder, tool_seconds, suricata, replay=True),
            )
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

    async def _accept_capture(
        self,
        reference: NetworkCaptureRefV1,
        context: ToolExecutionContext,
    ) -> NetworkCaptureRefV1:
        resolver = getattr(self.workbench.quarantine, "is_bound_to_investigation", None)
        if not callable(resolver) or not resolver(context.investigation_id):
            return reference
        accepted = await self.workbench.accept_quarantined_capture(
            f"sha256:{reference.sha256}",
            investigation_id=context.investigation_id,
            idempotency_key=(
                f"network-capture-adoption:{context.investigation_id}:{reference.sha256}"
            ),
        )
        if (
            accepted.sha256 != reference.sha256
            or accepted.size_bytes != reference.size_bytes
            or accepted.format is not reference.format
        ):
            raise NetworkAnalysisError("capture_ref_identity_mismatch")
        return accepted

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
            reference = await self._accept_capture(
                self._capture_ref(arguments.get("capture_ref")), context
            )
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
            reference = await self._accept_capture(
                self._capture_ref(arguments.get("capture_ref")), context
            )
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
            reference = await self._accept_capture(
                self._capture_ref(arguments.get("capture_ref")), context
            )
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


class _SuricataToolAdapter(_NetworkToolAdapter):
    def __init__(
        self,
        workbench: NetworkWorkbench,
        recorder: EvidenceRecorder,
        reserved_seconds: int,
        suricata: ConfinedSuricataAdapter,
        *,
        replay: bool,
    ) -> None:
        super().__init__(workbench, recorder, reserved_seconds)
        self.suricata = suricata
        self.replay = replay
        self.name = NETWORK_SURICATA_REPLAY if replay else NETWORK_SURICATA_VALIDATE_RULES

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        try:
            reference = await self._accept_capture(
                self._capture_ref(arguments.get("capture_ref")), context
            )
            parameters = self._candidate_parameters(arguments.get("candidate"))
            outcome = (
                await self.workbench.replay_suricata(
                    reference,
                    investigation_id=context.investigation_id,
                    adapter=self.suricata,
                    analysis_parameters=parameters,
                )
                if self.replay
                else await self.workbench.validate_suricata_rules(
                    reference,
                    investigation_id=context.investigation_id,
                    adapter=self.suricata,
                    analysis_parameters=parameters,
                )
            )
        except NetworkCaptureRejected as exc:
            status = (
                ToolInvocationStatus.UNAVAILABLE
                if exc.reason.value == "blob_unavailable"
                else ToolInvocationStatus.POLICY_DENIED
            )
            return self._tool_result(
                context, status=status, started=started, output={"detail_code": exc.reason.value}
            )
        except (NetworkAnalysisError, ValueError) as exc:
            detail = exc.code if isinstance(exc, NetworkAnalysisError) else str(exc)
            return self._tool_result(
                context,
                status=ToolInvocationStatus.POLICY_DENIED,
                started=started,
                output={"detail_code": detail[:160]},
            )

        if outcome.status is ToolInvocationStatus.UNAVAILABLE:
            return self._tool_result(
                context,
                status=ToolInvocationStatus.UNAVAILABLE,
                started=started,
                output={
                    "engine_status": "unavailable",
                    "available_reason": outcome.available_reason or "analysis_tool_unavailable",
                },
            )
        if outcome.payload is None:
            return self._tool_result(
                context,
                status=outcome.status,
                started=started,
                output={
                    "detail_code": outcome.available_reason or "suricata_result_unavailable",
                },
            )
        envelope = outcome.payload or {}
        engine = envelope.get("result") if isinstance(envelope, Mapping) else None
        if not isinstance(engine, Mapping):
            engine = {}
        engine_status = str(engine.get("status", "FAILED"))
        status = {
            "SUCCEEDED": ToolInvocationStatus.SUCCEEDED,
            "PARTIAL": ToolInvocationStatus.PARTIAL,
            "unavailable": ToolInvocationStatus.UNAVAILABLE,
            "POLICY_DENIED": ToolInvocationStatus.POLICY_DENIED,
        }.get(engine_status, ToolInvocationStatus.FAILED)
        if status is ToolInvocationStatus.UNAVAILABLE:
            return self._tool_result(
                context,
                status=status,
                started=started,
                output={
                    "engine_status": "unavailable",
                    "available_reason": engine.get("available_reason", "analysis_tool_unavailable"),
                },
            )
        payload = {
            "schema": "SuricataObservationV1",
            "capture_blob_id": str(reference.blob_id),
            "capture_sha256": reference.sha256,
            "operation_key": outcome.operation_key,
            "tool": self.name,
            "engine": dict(engine),
        }
        object_id = ":".join(
            str(value)
            for value in (
                reference.sha256,
                engine.get("ruleset_revision", "fixed"),
                engine.get("rule_sha256", "unknown"),
            )
        )
        observation_id = await self._record(
            context,
            source=self.name,
            object_type="suricata_observation",
            object_id=object_id,
            payload=payload,
        )
        return self._tool_result(
            context,
            status=status,
            started=started,
            observation_refs=(observation_id,),
            output={
                "engine_status": engine_status.lower(),
                "engine": dict(engine),
                "observation_ref": str(observation_id),
            },
        )

    @staticmethod
    def _candidate_parameters(value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping) or set(value) != {
            "candidate_id",
            "candidate_version",
            "rule_source",
        }:
            raise ValueError("suricata_candidate_invalid")
        candidate_id = value["candidate_id"]
        candidate_version = value["candidate_version"]
        rule_source = value["rule_source"]
        if (
            not isinstance(candidate_id, str)
            or isinstance(candidate_version, bool)
            or not isinstance(candidate_version, int)
            or not isinstance(rule_source, str)
        ):
            raise ValueError("suricata_candidate_invalid")
        rule_source = validate_suricata_rule_source(
            rule_source,
            candidate_id=candidate_id,
            candidate_version=str(candidate_version),
        )
        return {
            "candidate_id": candidate_id,
            "candidate_version": str(candidate_version),
            "rule_source": rule_source,
        }


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


def _suricata_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "capture_ref": _CAPTURE_REF_SCHEMA,
            "candidate": {
                "type": "object",
                "properties": {
                    "candidate_id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
                    },
                    "candidate_version": {"type": "integer", "minimum": 1, "maximum": 100_000},
                    "rule_source": {"type": "string", "minLength": 1, "maxLength": 65_536},
                },
                "required": ["candidate_id", "candidate_version", "rule_source"],
                "additionalProperties": False,
            },
        },
        "required": ["capture_ref"],
        "additionalProperties": False,
        "description": description,
    }


def _suricata_adapter(
    tshark: TsharkAdapter, *, timeout_seconds: int = 60
) -> ConfinedSuricataAdapter | None:
    execution = getattr(tshark, "execution", None)
    if execution is None or not hasattr(execution, "family_statuses"):
        return None
    return ConfinedSuricataAdapter(execution, timeout_seconds=min(timeout_seconds, 60))


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
