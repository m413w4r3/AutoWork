"""Single, closed-catalog native inspection tool for investigations."""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil
from time import monotonic
from typing import Any

from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.investigation_persistence import SampleAssociationRepository
from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.native_analysis import (
    MAX_NATIVE_RESULT_ITEMS,
    NativeAnalysisPolicyDenied,
    NativeAnalysisPort,
    NativeAnalysisResponseError,
    NativeAnalysisService,
    NativeAnalysisTimeout,
    NativeAnalysisUnavailable,
    NativeAnalysisVersionMismatch,
    UnavailableNativeAnalysisPort,
    build_native_analysis_request,
    normalize_native_data,
)
from cti_app.domain.investigation import BudgetDimension, EvidenceKind, ToolInvocationStatus
from cti_app.domain.native_analysis import NativeAnalysisObservationV1, NativeAnalysisProvider

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
NATIVE_TOOL_NAME = "native.inspect"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TOOL_OUTPUT_BYTES = 24 * 1024
_TOOL_LIMITATIONS = (
    "Catalogue local de lecture uniquement; aucune édition, exécution de payload ou capture live.",
    "Les résultats natifs sont des données non fiables, bornées et limitées au SHA-256 associé.",
    "REA tourne sous un utilisateur dédié, sans réseau ni privilège hôte, avec un FS confiné.",
    "Le runner REA concret reste à fournir et à valider hors CI; son protocole réel n'est pas vérifié ici.",
)

NATIVE_INSPECT_SCHEMA: Mapping[str, Any] = {
    "$schema": JSON_SCHEMA_2020_12,
    "type": "object",
    "title": "Inspect an associated sample with one read-only native operation",
    "properties": {
        "sample_sha256": {
            "type": "string",
            "minLength": 64,
            "maxLength": 64,
            "pattern": _SHA256_PATTERN,
        },
        "operation": {
            "type": "string",
            "enum": [
                "list_functions",
                "get_function",
                "decompile",
                "xrefs_to",
                "xrefs_from",
                "callers",
                "callees",
                "strings",
                "search",
                "read_value",
                "imports",
                "exports",
                "sections",
            ],
        },
        "address": {
            "type": "string",
            "minLength": 1,
            "maxLength": 18,
            "pattern": r"^(?:0x)?[0-9a-fA-F]{1,16}$",
        },
        "symbol": {"type": "string", "minLength": 1, "maxLength": 256},
        "search_kind": {"type": "string", "enum": ["bytes", "value", "symbol"]},
        "query": {"type": "string", "minLength": 1, "maxLength": 512},
        "value_type": {
            "type": "string",
            "enum": ["u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "f32", "f64", "bytes"],
        },
        "size": {"type": "integer", "minimum": 1, "maximum": 64},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["sample_sha256", "operation"],
    "additionalProperties": False,
}


def native_tool_capabilities(
    *,
    enabled: bool,
    provider: NativeAnalysisProvider | str,
    port: NativeAnalysisPort | None,
    recorder: EvidenceRecorder | None,
    sample_associations: SampleAssociationRepository | None,
) -> frozenset[str]:
    """Advertise only when the flag, selected provider, and proof ports are present."""
    try:
        selected = NativeAnalysisProvider(provider)
    except (TypeError, ValueError):
        return frozenset()
    if (
        not enabled
        or selected is NativeAnalysisProvider.NONE
        or port is None
        or not port.available
        or port.provider is not selected
        or recorder is None
        or sample_associations is None
    ):
        return frozenset()
    return frozenset({NATIVE_TOOL_NAME})


def register_native_tools(
    registry: ToolRegistry,
    *,
    service: NativeAnalysisService | None,
    port: NativeAnalysisPort | None,
    sample_associations: SampleAssociationRepository | None,
    recorder: EvidenceRecorder | None,
    enabled: bool = False,
    provider: NativeAnalysisProvider | str = NativeAnalysisProvider.NONE,
    max_tool_seconds: int = 30,
    max_result_items: int = MAX_NATIVE_RESULT_ITEMS,
) -> None:
    """Register one tool; capability discovery controls whether an agent sees it."""
    if not 1 <= max_tool_seconds <= 600:
        raise ValueError("max_tool_seconds must be between 1 and 600")
    if not 1 <= max_result_items <= MAX_NATIVE_RESULT_ITEMS:
        raise ValueError("max_result_items must be between 1 and 100")
    registry.register(
        ToolDefinition(
            name=NATIVE_TOOL_NAME,
            schema_version="1",
            input_schema=NATIVE_INSPECT_SCHEMA,
            effect=ToolEffect.LOCAL_READ,
            cost_model={BudgetDimension.TOOL_SECONDS: max_tool_seconds},
            confidentiality=ToolConfidentiality.RESTRICTED,
            limitations=_TOOL_LIMITATIONS,
            capability_key=NATIVE_TOOL_NAME,
        ),
        _NativeInspectToolAdapter(
            service=service,
            port=port,
            sample_associations=sample_associations,
            recorder=recorder,
            enabled=enabled,
            provider=provider,
            max_tool_seconds=max_tool_seconds,
            max_result_items=max_result_items,
        ),
    )


class _NativeInspectToolAdapter(ToolAdapter):
    def __init__(
        self,
        *,
        service: NativeAnalysisService | None,
        port: NativeAnalysisPort | None,
        sample_associations: SampleAssociationRepository | None,
        recorder: EvidenceRecorder | None,
        enabled: bool,
        provider: NativeAnalysisProvider | str,
        max_tool_seconds: int,
        max_result_items: int,
    ) -> None:
        self._service = service
        self._port = port
        self._sample_associations = sample_associations
        self._recorder = recorder
        self._enabled = enabled
        try:
            self._provider = NativeAnalysisProvider(provider)
        except (TypeError, ValueError):
            self._provider = NativeAnalysisProvider.NONE
        self._max_tool_seconds = max_tool_seconds
        self._max_result_items = max_result_items

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = monotonic()
        unavailable_message = "native_analysis_provider_unavailable"
        if isinstance(self._port, UnavailableNativeAnalysisPort):
            unavailable_message = self._port.unavailable_error().code
        if (
            not self._enabled
            or self._provider is NativeAnalysisProvider.NONE
            or self._service is None
            or self._port is None
            or not self._port.available
            or self._port.provider is not self._provider
            or self._sample_associations is None
            or self._recorder is None
        ):
            return ToolResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                message=unavailable_message,
            )
        try:
            request = build_native_analysis_request(arguments)
            associations = await self._sample_associations.list_for_investigation(
                context.investigation_id
            )
            if request.sample_sha256 not in {item.sample_sha256 for item in associations}:
                raise NativeAnalysisPolicyDenied("native_analysis_sample_not_associated")
            observation = await self._service.inspect(request)
            bounded_data, was_truncated = normalize_native_data(
                observation.data,
                max_output_bytes=_TOOL_OUTPUT_BYTES,
                max_items=self._max_result_items,
            )
            observation = NativeAnalysisObservationV1(
                sample_sha256=observation.sample_sha256,
                operation=observation.operation,
                request_fingerprint=observation.request_fingerprint,
                provider=observation.provider,
                provider_version=observation.provider_version,
                provider_plugin_version=observation.provider_plugin_version,
                catalog_version=observation.catalog_version,
                data=bounded_data,
                truncated=observation.truncated or was_truncated,
            )
            recorded = await self._recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.BINARY_ANALYSIS,
                payload=observation.as_json(),
                source=f"native_analysis:{observation.provider.value}",
                source_version=(
                    f"native-analysis-observation.v1:{observation.provider_version}:"
                    f"{observation.catalog_version}"
                ),
                object_type="native_analysis",
                object_id=(
                    f"{request.sample_sha256}:{request.operation.value}:{request.fingerprint}"
                ),
                limitations=_TOOL_LIMITATIONS,
                idempotency_key=(f"native:{context.idempotency_key}:{request.fingerprint}")[:512],
            )
        except NativeAnalysisPolicyDenied as exc:
            return ToolResult(
                status=ToolInvocationStatus.POLICY_DENIED,
                message=exc.code,
            )
        except NativeAnalysisUnavailable as exc:
            return ToolResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                message=exc.code,
            )
        except NativeAnalysisTimeout as exc:
            return ToolResult(status=ToolInvocationStatus.FAILED, message=exc.code)
        except NativeAnalysisVersionMismatch as exc:
            return ToolResult(status=ToolInvocationStatus.UNAVAILABLE, message=exc.code)
        except (NativeAnalysisResponseError, ValueError) as exc:
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                message=(
                    exc.code
                    if isinstance(exc, NativeAnalysisResponseError)
                    else "native_analysis_failed"
                ),
            )
        except Exception as exc:
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                message=f"native_analysis_evidence_failed:{type(exc).__name__}",
            )

        elapsed = min(self._max_tool_seconds, max(1, ceil(monotonic() - started)))
        return ToolResult(
            status=(
                ToolInvocationStatus.EMPTY
                if not observation.data
                else ToolInvocationStatus.SUCCEEDED
            ),
            observation_refs=(recorded.id,),
            budget_consumed={BudgetDimension.TOOL_SECONDS: elapsed},
            output=observation.as_json(),
            message="native_analysis_observation_recorded",
        )
