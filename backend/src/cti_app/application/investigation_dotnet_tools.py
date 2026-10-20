"""Persisted, read-only .NET inspection tools backed by the confined worker."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from math import ceil
from typing import Any
from uuid import UUID

from cti_app.application.confined_analysis import (
    ConfinedAnalysisArtifactKind,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
    confined_analysis_operation_cache_identity,
)
from cti_app.application.dotnet_analysis import (
    ILSPYCMD_LICENSE,
    ILSPYCMD_PACKAGE_SHA512,
    ILSPYCMD_PROJECT_URL,
    ILSPYCMD_SOURCE_COMMIT,
    ILSPYCMD_VERSION,
)
from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
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
from cti_app.application.native_analysis import MAX_NATIVE_RESULT_ITEMS, normalize_native_data
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.entities import SampleState
from cti_app.domain.investigation import BudgetDimension, EvidenceKind, ToolInvocationStatus

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
DOTNET_TOOL_NAMES = (
    "assembly.inspect",
    "types.list",
    "methods.list",
    "method.decompile",
    "references.imports",
    "strings.list",
    "resources.list",
)
_TOOL_OUTPUT_BYTES = 24 * 1024
_PERSISTED_RESULT_BYTES = 48 * 1024
_OPERATOR_RESULT_BYTES = 8 * 1024
_RESULT_JSON_CHUNK_CHARS = 4_000
_OBSERVATION_PAGE_LENGTH = 16 * 1024
_NATIVE_STRING_LIMIT_CHARS = 16_384
_TOOL_SECONDS = 60
_MAX_SAMPLE_BYTES = 64 * 1024 * 1024
_LIMITATIONS = (
    "Lecture statique uniquement : le code de l'assembly n'est ni chargé ni exécuté.",
    ("Le worker est confine, en lecture seule et sans reseau; aucun restore NuGet a l'execution."),
    (
        "ILSpyCmd décompile le CIL lorsqu'il trouve le corps; monodis lit les métadonnées "
        "et peut fournir un repli CIL."
    ),
    (
        "Les references/imports seuls ne prouvent pas un C2; chaines obfusquees et blobs chiffres "
        "restent non resolus."
    ),
    (
        "Un PE mixte suit aussi le chemin natif pour ses sections natives; les metadonnees "
        "gerees restent sur .NET."
    ),
)


def dotnet_tool_capabilities(
    *,
    execution: ConfinedAnalysisExecutionPort | None,
    sample_associations: SampleAssociationRepository | None,
    recorder: EvidenceRecorder | None,
) -> frozenset[str]:
    if (
        execution is None
        or not execution.available
        or sample_associations is None
        or recorder is None
    ):
        return frozenset()
    family = execution.family_statuses.get("dotnet")
    return (
        frozenset({"investigation.dotnet"})
        if family is not None and family.available
        else frozenset()
    )


def register_dotnet_tools(
    registry: ToolRegistry,
    *,
    execution: ConfinedAnalysisExecutionPort | None,
    sample_associations: SampleAssociationRepository | None,
    recorder: EvidenceRecorder | None,
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
) -> None:
    adapters_ready = (
        execution is not None and sample_associations is not None and recorder is not None
    )
    operation_schemas = _tool_schemas()
    for name in DOTNET_TOOL_NAMES:
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=operation_schemas[name],
                effect=ToolEffect.LOCAL_READ,
                cost_model={BudgetDimension.TOOL_SECONDS: _TOOL_SECONDS},
                confidentiality=ToolConfidentiality.RESTRICTED,
                limitations=_LIMITATIONS,
                capability_key="investigation.dotnet",
            ),
            _DotnetToolAdapter(
                name=name,
                execution=execution if adapters_ready else None,
                sample_associations=sample_associations,
                recorder=recorder,
                uow_factory=uow_factory,
                settings=settings,
            ),
        )


def _tool_schemas() -> dict[str, Mapping[str, Any]]:
    sha = {
        "type": "string",
        "minLength": 64,
        "maxLength": 64,
        "pattern": r"^[0-9a-f]{64}$",
    }
    offset = {"type": "integer", "minimum": 0, "maximum": 10000000}
    limit = {"type": "integer", "minimum": 1, "maximum": 200}
    max_chars = {
        "type": "integer",
        "minimum": 1,
        "maximum": 16384,
        "description": (
            "Worker output cap in characters. The normalized observation is preserved in "
            "verified 4,000-character chunks, up to 48 KiB; the direct response is a preview. "
            "Read further through the returned observation URL using offset and length."
        ),
    }
    rid = {"type": "integer", "minimum": 1, "maximum": 16777215}
    specs: dict[str, tuple[dict[str, Any], tuple[str, ...], str]] = {
        "assembly.inspect": (
            {"sample_sha256": sha, "offset": offset, "limit": limit, "max_chars": max_chars},
            ("sample_sha256",),
            "Inspect PE/CLI metadata, machine, architecture and PDB indicators",
        ),
        "types.list": (
            {"sample_sha256": sha, "offset": offset, "limit": limit},
            ("sample_sha256",),
            "List .NET type definitions by stable metadata token",
        ),
        "methods.list": (
            {"sample_sha256": sha, "offset": offset, "limit": limit},
            ("sample_sha256",),
            "List .NET methods by stable MethodDef token and RID",
        ),
        "method.decompile": (
            {
                "sample_sha256": sha,
                "method_rid": rid,
                "offset": offset,
                "max_chars": max_chars,
            },
            ("sample_sha256", "method_rid"),
            "Decompile one .NET method selected by stable MethodDef RID",
        ),
        "references.imports": (
            {"sample_sha256": sha, "offset": offset, "limit": limit, "max_chars": max_chars},
            ("sample_sha256",),
            "List managed assembly, member and native import references",
        ),
        "strings.list": (
            {"sample_sha256": sha, "offset": offset, "limit": limit, "max_chars": max_chars},
            ("sample_sha256",),
            "List the managed user-string heap with unresolved values preserved",
        ),
        "resources.list": (
            {"sample_sha256": sha, "offset": offset, "limit": limit, "max_chars": max_chars},
            ("sample_sha256",),
            "List embedded resource manifest metadata without extracting resource bytes",
        ),
    }
    return {
        name: {
            "$schema": JSON_SCHEMA_2020_12,
            "type": "object",
            "title": title,
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        }
        for name, (properties, required, title) in specs.items()
    }


class _DotnetToolAdapter(ToolAdapter):
    def __init__(
        self,
        *,
        name: str,
        execution: ConfinedAnalysisExecutionPort | None,
        sample_associations: SampleAssociationRepository | None,
        recorder: EvidenceRecorder | None,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
    ) -> None:
        self._name = name
        self._execution = execution
        self._sample_associations = sample_associations
        self._recorder = recorder
        self._uow_factory = uow_factory
        self._settings = settings
        self._operation = {
            "assembly.inspect": "assembly",
            "types.list": "types",
            "methods.list": "methods",
            "method.decompile": "decompile",
            "references.imports": "references",
            "strings.list": "strings",
            "resources.list": "resources",
        }[name]

    @property
    def operation_cache_identity(self) -> Mapping[str, Any]:
        return confined_analysis_operation_cache_identity(
            self._execution,
            result_schema_version="dotnet-analysis-result.v3",
        )

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()
        sample_sha256 = arguments.get("sample_sha256")
        if not isinstance(sample_sha256, str):
            return ToolResult(ToolInvocationStatus.POLICY_DENIED, message="invalid_sha256")
        if (
            self._execution is None
            or not self._execution.available
            or self._recorder is None
            or self._sample_associations is None
        ):
            return ToolResult(ToolInvocationStatus.UNAVAILABLE, message="dotnet_worker_unavailable")
        family = self._execution.family_statuses.get("dotnet")
        if family is None or not family.available:
            return ToolResult(
                ToolInvocationStatus.UNAVAILABLE,
                message=(family.available_reason or "dotnet_worker_unavailable")
                if family is not None
                else "dotnet_worker_unavailable",
            )

        identity = await self._canonical_sample(context.investigation_id, sample_sha256)
        if isinstance(identity, str):
            return ToolResult(
                ToolInvocationStatus.UNAVAILABLE
                if identity.endswith("unavailable")
                else ToolInvocationStatus.POLICY_DENIED,
                message=identity,
            )
        association_id, sample_id, blob_id, size_bytes = identity
        if size_bytes > min(
            _MAX_SAMPLE_BYTES, self._settings.analysis_local_monodis_max_sample_bytes
        ):
            return ToolResult(ToolInvocationStatus.POLICY_DENIED, message="dotnet_sample_too_large")

        params: dict[str, str] = {"operation": self._operation}
        for key in ("method_rid", "offset", "limit", "max_chars"):
            value = arguments.get(key)
            if value is not None:
                params[key] = str(value)
        request = ConfinedAnalysisRequest(
            investigation_id=context.investigation_id,
            association_id=association_id,
            sample_id=sample_id,
            artifact_id=blob_id,
            sha256=sample_sha256,
            artifact_kind=ConfinedAnalysisArtifactKind.SAMPLE,
            tool=ConfinedAnalysisTool.DOTNET_ILSPY,
            timeout_seconds=min(_TOOL_SECONDS, self._settings.analysis_worker_job_timeout_seconds),
            max_output_bytes=min(
                _TOOL_OUTPUT_BYTES * 8, self._settings.analysis_worker_result_max_bytes
            ),
            analysis_parameters=params,
        )
        result = await self._execution.execute(request)
        raw_result: Mapping[str, Any]
        if result.status is not ToolInvocationStatus.SUCCEEDED or not isinstance(
            result.payload, Mapping
        ):
            raw_result = {
                "status": result.status.value,
                "available_reason": result.available_reason or "dotnet_worker_result_unavailable",
                "operation": self._operation,
            }
        else:
            nested = result.payload.get("result")
            raw_result = (
                nested
                if isinstance(nested, Mapping)
                else {
                    "status": "FAILED",
                    "available_reason": "dotnet_worker_result_invalid",
                    "operation": self._operation,
                }
            )
        sanitizer_truncated = _result_exceeds_sanitizer_limits(raw_result)
        safe_result = _sanitize_json_value(dict(raw_result))
        chunked_result, long_text_key = _preserve_long_text_fields(safe_result)
        normalized_result, normalized_truncated = normalize_native_data(
            chunked_result,
            max_output_bytes=_PERSISTED_RESULT_BYTES,
            max_items=MAX_NATIVE_RESULT_ITEMS,
        )
        persisted_result = _restore_long_text_fields(normalized_result, long_text_key)
        result_truncated_by_tool = (
            sanitizer_truncated or normalized_truncated or raw_result.get("truncated") is True
        )
        result_json = json.dumps(
            persisted_result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        result_json_bytes = result_json.encode("utf-8")
        result_json_sha256 = hashlib.sha256(result_json_bytes).hexdigest()
        result_json_chunks = _text_chunks(result_json, _RESULT_JSON_CHUNK_CHARS)
        operator_result, response_truncated = normalize_native_data(
            persisted_result,
            max_output_bytes=_OPERATOR_RESULT_BYTES,
            max_items=MAX_NATIVE_RESULT_ITEMS,
        )
        worker_status = str(raw_result.get("status", "FAILED")).upper()
        analysis_status = (
            "obfusqué"
            if worker_status == "OBFUSCATED"
            else "tronqué"
            if result_truncated_by_tool
            else "partiel"
            if worker_status in {"PARTIAL", "TIMEOUT"}
            else "complet"
            if worker_status == "SUCCEEDED"
            else "échec"
        )
        local_command_family = self._execution.family_statuses.get("local_command")
        engine_value = raw_result.get("engine")
        engine = (
            engine_value
            if isinstance(engine_value, str) and engine_value in {"ilspycmd", "monodis-fallback"}
            else "monodis"
        )
        version_value = raw_result.get("version")
        engine_version = version_value if isinstance(version_value, str) and version_value else None
        if engine == "ilspycmd" and engine_version is None:
            engine_version = ILSPYCMD_VERSION
        if engine_version is None and local_command_family is not None:
            engine_version = local_command_family.version
        observation: dict[str, Any] = {
            "schema_version": "dotnet-analysis-observation.v1",
            "tool": self._name,
            "operation": self._operation,
            "sample_sha256": sample_sha256,
            "sample_size_bytes": size_bytes,
            "status": analysis_status,
            "source": {
                "engine": engine,
                "version": engine_version,
                "monodis_version": local_command_family.version
                if local_command_family is not None
                else None,
            },
            "result": operator_result,
            "result_json_encoding": "utf-8-json",
            "result_json_byte_length": len(result_json_bytes),
            "result_json_sha256": result_json_sha256,
            "result_json_chunks": result_json_chunks,
            "result_truncated_by_tool_boundary": result_truncated_by_tool,
            "result_truncated_by_operator_response": response_truncated or result_truncated_by_tool,
            "limitations": list(_LIMITATIONS),
        }
        identity_payload = json.dumps(
            {"tool": self._name, "arguments": dict(arguments)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request_fingerprint = hashlib.sha256(identity_payload).hexdigest()
        source_version = f"dotnet-analysis-observation.v1:{engine}:{engine_version or 'unknown'}"
        if engine == "ilspycmd":
            observation["source"].update(
                {
                    "package_sha512": ILSPYCMD_PACKAGE_SHA512,
                    "source_commit": ILSPYCMD_SOURCE_COMMIT,
                    "project_url": ILSPYCMD_PROJECT_URL,
                    "license": ILSPYCMD_LICENSE,
                }
            )
            source_version = f"{source_version}:{ILSPYCMD_PACKAGE_SHA512}"
        try:
            recorded = await self._recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.BINARY_ANALYSIS,
                payload=observation,
                source=f"dotnet.{engine}",
                source_version=source_version,
                object_type="dotnet_analysis",
                object_id=f"{sample_sha256}:{self._operation}:{request_fingerprint}",
                limitations=_LIMITATIONS,
                idempotency_key=(f"dotnet:{context.idempotency_key}:{request_fingerprint}")[:512],
            )
        except Exception as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message=f"dotnet_evidence_persist_failed:{type(exc).__name__}",
            )

        elapsed = min(_TOOL_SECONDS, max(1, ceil(time.monotonic() - started)))
        output = {
            "schema_version": "dotnet-analysis-response.v1",
            "sample_sha256": sample_sha256,
            "tool": self._name,
            "operation": self._operation,
            "status": analysis_status,
            "source": observation["source"],
            "result": operator_result,
            "result_byte_length": len(result_json_bytes),
            "result_json_sha256": result_json_sha256,
            "result_truncated_by_response": response_truncated or result_truncated_by_tool,
            "evidence_observation_id": str(recorded.id),
            "evidence_observation_sha256": recorded.sha256,
            "evidence_observation_url": (
                f"/api/investigations/{context.investigation_id}/operator/observations/"
                f"{recorded.id}?offset=0&length={_OBSERVATION_PAGE_LENGTH}"
            ),
            "limitations": list(_LIMITATIONS),
        }
        invocation_status = (
            ToolInvocationStatus.UNAVAILABLE
            if worker_status == "UNAVAILABLE"
            else ToolInvocationStatus.POLICY_DENIED
            if worker_status == "POLICY_DENIED"
            else ToolInvocationStatus.FAILED
            if worker_status in {"FAILED", "TIMEOUT"}
            else ToolInvocationStatus.SUCCEEDED
        )
        return ToolResult(
            status=invocation_status,
            observation_refs=(recorded.id,),
            budget_consumed={BudgetDimension.TOOL_SECONDS: elapsed},
            output=output,
            message=f"dotnet_analysis_{analysis_status}",
        )

    async def _canonical_sample(
        self, investigation_id: UUID, sample_sha256: str
    ) -> tuple[UUID, UUID, UUID, int] | str:
        sample_associations = self._sample_associations
        if sample_associations is None:
            return "sample_association_repository_unavailable"
        try:
            async with self._uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(investigation_id)
                associations = await sample_associations.list_for_investigation(investigation_id)
                association = next(
                    (item for item in associations if item.sample_sha256 == sample_sha256), None
                )
                if investigation is None or association is None:
                    return "sample_not_associated"
                blob = await uow.blobs.get_by_address(SAMPLES_QUARANTINE_BUCKET, sample_sha256)
                if blob is None:
                    return "sample_bytes_unavailable"
                sample = await uow.samples.get_by_subject_and_blob(
                    investigation.subject_id, blob.id
                )
                if (
                    sample is None
                    or sample.blob_id != blob.id
                    or sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                    or blob.descriptor.sha256 != sample_sha256
                ):
                    return "sample_not_quarantined"
                return association.id, sample.id, blob.id, blob.descriptor.size
        except Exception:
            return "sample_identity_unavailable"


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_evidence_text(value, max_chars=_PERSISTED_RESULT_BYTES)
    if isinstance(value, Mapping):
        return {str(key): _sanitize_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json_value(item) for item in value[:MAX_NATIVE_RESULT_ITEMS]]
    return value


def _result_exceeds_sanitizer_limits(value: Any) -> bool:
    if isinstance(value, str):
        return len(value) > _PERSISTED_RESULT_BYTES
    if isinstance(value, Mapping):
        return any(_result_exceeds_sanitizer_limits(item) for item in value.values())
    if isinstance(value, list | tuple):
        return len(value) > MAX_NATIVE_RESULT_ITEMS or any(
            _result_exceeds_sanitizer_limits(item) for item in value[:MAX_NATIVE_RESULT_ITEMS]
        )
    return False


def _preserve_long_text_fields(value: Any) -> tuple[Any, str]:
    occupied_keys: set[str] = set()

    def collect_keys(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if isinstance(key, str):
                    occupied_keys.add(key)
                collect_keys(child)
        elif isinstance(item, list | tuple):
            for child in item:
                collect_keys(child)

    collect_keys(value)
    marker = "__autowork_dotnet_full_text_chunks__"
    while marker in occupied_keys:
        marker += "_"

    def chunk(item: Any) -> Any:
        if isinstance(item, str) and len(item) > _NATIVE_STRING_LIMIT_CHARS:
            parts = {
                f"{index:08d}": item[start : start + _NATIVE_STRING_LIMIT_CHARS]
                for index, start in enumerate(range(0, len(item), _NATIVE_STRING_LIMIT_CHARS))
            }
            return {marker: parts}
        if isinstance(item, Mapping):
            return {str(key): chunk(child) for key, child in item.items()}
        if isinstance(item, list | tuple):
            return [chunk(child) for child in item]
        return item

    return chunk(value), marker


def _restore_long_text_fields(value: Any, marker: str) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {marker} and isinstance(value[marker], Mapping):
            parts = value[marker]
            return "".join(str(parts[key]) for key in sorted(parts))
        return {str(key): _restore_long_text_fields(child, marker) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_restore_long_text_fields(child, marker) for child in value]
    return value


def _text_chunks(value: str, chunk_chars: int) -> list[str]:
    return [value[index : index + chunk_chars] for index in range(0, len(value), chunk_chars)]
