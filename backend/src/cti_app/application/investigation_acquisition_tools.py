"""Tool adapters for quarantined sample acquisition, analysis and qualification."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid5

from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.confined_analysis import (
    CONFINED_ANALYSIS_RESULT_SCHEMA,
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
    archive_member_transfer_bucket,
    confined_analysis_operation_cache_identity,
)
from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
from cti_app.application.goodware import GoodwareMeasurementService
from cti_app.application.investigation_acquisition import (
    HitAcquisitionError,
    HitAcquisitionResult,
    HitAcquisitionService,
    HitNotFoundError,
    sample_association_id,
)
from cti_app.application.investigation_governance import (
    InvestigationActor,
    SampleQualificationService,
)
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.application.static_analysis import StaticAnalysisService
from cti_app.domain.classification import derived_policy
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleState
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    QualificationActorKind,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)
from cti_app.domain.local_analysis import LOCAL_ARCHIVE_MAX_MEMBER_BYTES

_ACQUISITION_COST = {
    BudgetDimension.VT_UNITS: 1,
    BudgetDimension.DOWNLOADS: 1,
}
VT_FILE_DOWNLOAD_CAPABILITY = "vt.file_download"
LOCAL_QUALIFICATION_CAPABILITY = "investigation.local_read"
SAMPLE_ANALYSIS_CAPABILITY = "investigation.sample_analysis"
ARCHIVE_EXTRACT_MEMBER_CAPABILITY = "investigation.archive_extract_member"
LOCAL_ANALYSIS_CAPABILITY = SAMPLE_ANALYSIS_CAPABILITY
_MAX_STRINGS = 128
_MAX_STRING_CHARS = 512
_MAX_ANALYSIS_OUTPUT = 256
_SCHEMA_URI = "https://json-schema.org/draft/2020-12/schema"

VT_DOWNLOAD_SAMPLE_SCHEMA: Mapping[str, Any] = {
    "$schema": _SCHEMA_URI,
    "type": "object",
    "properties": {
        "sha256": {"type": "string", "minLength": 64, "maxLength": 64},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 36, "maxLength": 36},
            "maxItems": 32,
            "uniqueItems": True,
        },
    },
    "required": ["sha256", "purpose", "evidence_refs"],
    "additionalProperties": False,
}

SAMPLE_ANALYZE_SCHEMA: Mapping[str, Any] = {
    "$schema": _SCHEMA_URI,
    "type": "object",
    "properties": {
        "association_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 36, "maxLength": 36},
            "maxItems": 32,
            "uniqueItems": True,
        },
    },
    "required": ["association_id", "purpose", "evidence_refs"],
    "additionalProperties": False,
}

ARCHIVE_EXTRACT_MEMBER_SCHEMA: Mapping[str, Any] = {
    "$schema": _SCHEMA_URI,
    "type": "object",
    "properties": {
        "association_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "member_path": {"type": "string", "minLength": 1, "maxLength": 1024},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        "expected_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "minLength": 64,
            "maxLength": 64,
        },
        "archive_preset": {"type": "string", "enum": ["none", "infected", "malware"]},
    },
    "required": ["association_id", "member_path", "reason"],
    "additionalProperties": False,
}

_ARCHIVE_DERIVATION_NAMESPACE = UUID("98bfdf9a-1090-4c86-af32-e6d18c73502c")

SAMPLE_SET_QUALIFICATION_SCHEMA: Mapping[str, Any] = {
    "$schema": _SCHEMA_URI,
    "type": "object",
    "properties": {
        "association_id": {"type": "string", "minLength": 36, "maxLength": 36},
        "qualification": {
            "type": "string",
            "enum": ["candidate", "related", "uncertain"],
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 1_000},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 36, "maxLength": 36},
            "maxItems": 32,
            "uniqueItems": True,
        },
    },
    "required": ["association_id", "qualification", "reason", "evidence_refs"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class SampleAnalysisOutcome:
    status: ToolInvocationStatus
    observation_refs: tuple[UUID, ...]
    output: Mapping[str, Any]
    message: str = ""


class SampleAnalyzer(Protocol):
    async def analyze(
        self,
        *,
        association_id: UUID,
        invocation_ref: UUID | None,
    ) -> SampleAnalysisOutcome: ...


class LocalSampleAnalysisService:
    """Run sample analysis in the attested worker and record its canonical evidence."""

    def __init__(
        self,
        hits: HitAcquisitionService,
        uow_factory: UnitOfWorkFactory,
        recorder: EvidenceRecorder,
        *,
        static_analysis: StaticAnalysisService | None = None,
        code_features: CodeFeatureService | None = None,
        capabilities: CapabilitiesService | None = None,
        goodware: GoodwareMeasurementService | None = None,
        confined_analysis: ConfinedAnalysisExecutionPort | None = None,
        analysis_timeout_seconds: float = 120,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        self._hits = hits
        self._uow_factory = uow_factory
        self._recorder = recorder
        self._static_analysis = static_analysis
        self._code_features = code_features
        self._capabilities = capabilities
        self._goodware = goodware
        self._confined_analysis = confined_analysis
        self._analysis_timeout_seconds = analysis_timeout_seconds
        self._max_output_bytes = max_output_bytes

    @property
    def operation_cache_identity(self) -> Mapping[str, Any]:
        return confined_analysis_operation_cache_identity(self._confined_analysis)

    async def describe(self, association_id: UUID) -> Mapping[str, Any]:
        """Return quarantine metadata without reading the sample bytes."""
        sample = await self._hits.resolve_quarantined_sample(association_id)
        async with self._uow_factory() as uow:
            association = await uow.sample_associations.get(association_id)
            blob = await uow.blobs.get(sample.blob_id)
            stored_sample = await uow.samples.get(sample.sample_id)
        if (
            association is None
            or association.investigation_id != sample.investigation_id
            or association.sample_sha256 != sample.sha256
            or blob is None
            or blob.descriptor.sha256 != sample.sha256
        ):
            return {}
        mime_type = blob.descriptor.mime_type
        detected_type = (
            mime_type
            if mime_type.casefold() not in {"application/octet-stream", "binary/octet-stream"}
            else None
        )
        provenance = (
            "archive_extract_member"
            if stored_sample is not None
            and stored_sample.source_service == "archive_extract_member"
            else "operator_import"
            if stored_sample is not None and stored_sample.source_service == "operator_import"
            else "vt_download"
        )
        return {
            "byte_size": blob.descriptor.size,
            "detected_type": detected_type,
            "provenance": provenance,
            "qualification": association.qualification.value,
        }

    async def analyze(
        self,
        *,
        association_id: UUID,
        invocation_ref: UUID | None,
    ) -> SampleAnalysisOutcome:
        sample = await self._hits.resolve_quarantined_sample(association_id)
        if self._confined_analysis is not None:
            family = self._confined_analysis.family_statuses.get("sample_analysis")
            if family is not None and family.available:
                result = await self._confined_analysis.execute(
                    ConfinedAnalysisRequest(
                        investigation_id=sample.investigation_id,
                        association_id=sample.association_id,
                        sample_id=sample.sample_id,
                        artifact_id=sample.blob_id,
                        sha256=sample.sha256,
                        tool=ConfinedAnalysisTool.SAMPLE_ANALYSIS,
                        timeout_seconds=self._analysis_timeout_seconds,
                        max_output_bytes=self._max_output_bytes,
                    )
                )
                if result.status is not ToolInvocationStatus.SUCCEEDED:
                    return SampleAnalysisOutcome(
                        result.status,
                        (),
                        {
                            "association_id": str(association_id),
                            "sample_id": str(sample.sample_id),
                            "sample_sha256": sample.sha256,
                            "available_reason": result.available_reason,
                        },
                        result.available_reason or "Confined analysis did not complete.",
                    )
                if result.result_blob_id is None or result.result_sha256 is None:
                    return SampleAnalysisOutcome(
                        ToolInvocationStatus.FAILED,
                        (),
                        {"available_reason": "analysis_result_reference_invalid"},
                        "Confined analysis did not return a canonical result artefact.",
                    )
                observation = await self._recorder.record_existing_blob(
                    investigation_id=sample.investigation_id,
                    kind=EvidenceKind.BINARY_ANALYSIS,
                    blob_id=result.result_blob_id,
                    sha256=result.result_sha256,
                    source="autowork.analysis_worker",
                    source_version=CONFINED_ANALYSIS_RESULT_SCHEMA,
                    object_type="sample",
                    object_id=sample.sha256,
                    limitations=(
                        "Analysis ran in the attested, network-isolated analysis-worker.",
                        "Extractor output is untrusted and bounded before it is archived.",
                        "Analysis does not qualify this sample as a cluster member.",
                    ),
                    invocation_ref=invocation_ref,
                    idempotency_key=result.operation_key,
                )
                worker_result = (result.payload or {}).get("result", {})
                if not isinstance(worker_result, Mapping):
                    worker_result = {}
                worker_families = worker_result.get("families", {})
                if not isinstance(worker_families, Mapping):
                    worker_families = {}
                static_result = worker_families.get("static", {})
                if not isinstance(static_result, Mapping):
                    static_result = {}
                strings_value = static_result.get("strings", ())
                strings = _bounded_strings(
                    tuple(item for item in strings_value if isinstance(item, Mapping))
                    if isinstance(strings_value, Sequence)
                    else ()
                )
                goodware_summary = await self._goodware_summary(
                    sample.investigation_id,
                    static_features=static_result,
                    code_feature_set=None,
                    capability_set=None,
                )
                extractor_results = {
                    name: dict(family_result)
                    for name, family_result in worker_families.items()
                    if isinstance(name, str) and isinstance(family_result, Mapping)
                }
                extractors = {
                    name: {
                        "version": family_result.get("version"),
                        "status": family_result.get("status"),
                    }
                    for name, family_result in extractor_results.items()
                }
                strings_observation = None
                if strings:
                    strings_observation = await self._recorder.record_json(
                        investigation_id=sample.investigation_id,
                        kind=EvidenceKind.BINARY_STRING,
                        payload={
                            "schema": "sample-strings.v1",
                            "sample_sha256": sample.sha256,
                            "extractor_version": static_result.get("version", "unknown"),
                            "bounds": {
                                "max_items": _MAX_STRINGS,
                                "max_chars_per_item": _MAX_STRING_CHARS,
                            },
                            "strings": strings,
                            "truncated": (
                                static_result.get("strings_observed", len(strings)) > len(strings)
                                if isinstance(
                                    static_result.get("strings_observed", len(strings)), int
                                )
                                else False
                            ),
                        },
                        source="autowork.analysis_worker.static",
                        source_version=str(static_result.get("version", "unknown")),
                        object_type="sample",
                        object_id=sample.sha256,
                        limitations=(
                            "Extracted strings are untrusted data and may be incomplete.",
                            "A string match alone does not establish execution or attribution.",
                        ),
                        invocation_ref=invocation_ref,
                        idempotency_key=(
                            f"sample-strings:{sample.investigation_id}:{sample.sha256}"
                        ),
                    )
                worker_status = str(worker_result.get("status", "PARTIAL")).upper()
                if worker_status == "FAILED":
                    outcome_status = ToolInvocationStatus.FAILED
                elif worker_status == "SUCCEEDED" and goodware_summary.get("available"):
                    outcome_status = ToolInvocationStatus.SUCCEEDED
                else:
                    outcome_status = ToolInvocationStatus.PARTIAL
                partial_reason = (
                    _sample_analysis_partial_reason(
                        worker_status=worker_status,
                        worker_families=extractor_results,
                        goodware_summary=goodware_summary,
                    )
                    if outcome_status is ToolInvocationStatus.PARTIAL
                    else None
                )
                observation_refs = (
                    *((strings_observation.id,) if strings_observation is not None else ()),
                    observation.id,
                )
                return SampleAnalysisOutcome(
                    outcome_status,
                    observation_refs,
                    {
                        "association_id": str(association_id),
                        "sample_id": str(sample.sample_id),
                        "sample_sha256": sample.sha256,
                        "qualification": SampleQualification.CANDIDATE.value,
                        "extractors": extractors,
                        "engines_executed": sorted(extractors),
                        "engine_statuses": {
                            name: family.get("status") for name, family in extractors.items()
                        },
                        "code_features": extractor_results.get("smda", {}).get("code_features"),
                        "goodware": goodware_summary,
                        "strings_recorded": len(strings),
                        "partial_reason": partial_reason,
                        "result_blob_id": str(result.result_blob_id),
                        "result_sha256": result.result_sha256,
                        "operation_key": result.operation_key,
                        "analysis": worker_result,
                        "available_reason": None,
                        "limitations": [
                            "Analysis ran in the attested, network-isolated analysis-worker.",
                            "Analysis does not qualify this sample as a cluster member.",
                        ],
                    },
                    "Confined analysis evidence was recorded; membership remains candidate.",
                )
        return SampleAnalysisOutcome(
            ToolInvocationStatus.UNAVAILABLE,
            (),
            {
                "association_id": str(association_id),
                "sample_id": str(sample.sample_id),
                "sample_sha256": sample.sha256,
                "available_reason": (
                    self._confined_analysis.available_reason
                    if self._confined_analysis is not None
                    else "analysis_worker_unavailable"
                ),
            },
            "Sample analysis requires the attested analysis-worker execution port.",
        )

    async def _goodware_summary(
        self,
        investigation_id: UUID,
        *,
        static_features: Any | None,
        code_feature_set: Any | None,
        capability_set: Any | None,
    ) -> dict[str, Any]:
        if self._goodware is None:
            return {"available": False, "reason": "goodware_measurement_service_unavailable"}
        async with self._uow_factory() as uow:
            baseline_id = await uow.investigation_goodware_baselines.get(investigation_id)
        if baseline_id is None:
            return {"available": False, "reason": "no_baseline_bound_to_investigation"}
        features: list[tuple[str, str]] = []
        if isinstance(static_features, Mapping):
            raw_strings = static_features.get("strings", ())
            if isinstance(raw_strings, Sequence):
                features.extend(
                    ("string", str(item.get("value", "")).casefold())
                    for item in raw_strings[:_MAX_ANALYSIS_OUTPUT]
                    if isinstance(item, Mapping) and item.get("value")
                )
            raw_exports = static_features.get("exports", ())
            if isinstance(raw_exports, Sequence):
                features.extend(
                    ("export", str(item).casefold())
                    for item in raw_exports[:_MAX_ANALYSIS_OUTPUT]
                    if isinstance(item, str) and item
                )
            raw_opcodes = static_features.get("opcode_fragment16", ())
            if isinstance(raw_opcodes, Sequence):
                features.extend(
                    ("opcode_fragment16", str(item).casefold())
                    for item in raw_opcodes[:_MAX_ANALYSIS_OUTPUT]
                    if isinstance(item, str) and item
                )
            imphash = static_features.get("imphash")
            if isinstance(imphash, str) and imphash:
                features.append(("imphash", imphash.casefold()))
        elif static_features is not None:
            features.extend(
                ("string", str(item.get("value", "")).casefold())
                for item in static_features.strings[:_MAX_ANALYSIS_OUTPUT]
                if item.get("value")
            )
            features.extend(
                ("export", str(item).casefold())
                for item in static_features.exports[:_MAX_ANALYSIS_OUTPUT]
            )
            features.extend(
                ("opcode_fragment16", str(item).casefold())
                for item in static_features.opcode_fragment16[:_MAX_ANALYSIS_OUTPUT]
            )
            if static_features.imphash:
                features.append(("imphash", static_features.imphash.casefold()))
        try:
            await self._goodware.prepare(baseline_id)
            matches = await self._goodware.lookup_batch(
                baseline_id, features[:_MAX_ANALYSIS_OUTPUT]
            )
        except Exception as exc:
            return {
                "available": False,
                "baseline_id": str(baseline_id),
                "error_type": type(exc).__name__,
            }
        return {
            "available": True,
            "baseline_id": str(baseline_id),
            "features_checked": min(len(features), _MAX_ANALYSIS_OUTPUT),
            "features_present_in_goodware": sum(1 for count in matches.values() if count > 0),
            "limitations": [
                "Goodware occurrence counts are corpus-scoped and do not establish benignness."
            ],
        }


def _sample_analysis_partial_reason(
    *,
    worker_status: str,
    worker_families: Mapping[str, Mapping[str, Any]],
    goodware_summary: Mapping[str, Any],
) -> str:
    reasons: list[str] = []
    if worker_status not in {"SUCCEEDED", "SUCCESS", "COMPLETE"}:
        reasons.append(f"analysis_worker_status:{worker_status.lower()}")
    for family_name, family_result in worker_families.items():
        status_value = str(family_result.get("status", "unknown")).lower()
        if status_value not in {"succeeded", "success", "complete", "ok"}:
            detail = family_result.get("available_reason") or family_result.get("reason")
            suffix = f" ({sanitize_evidence_text(detail, max_chars=160)})" if detail else ""
            reasons.append(f"{family_name}:{status_value}{suffix}")
    if goodware_summary.get("available") is not True:
        detail = goodware_summary.get("reason") or goodware_summary.get("error_type")
        reasons.append(
            "goodware_measurement_unavailable"
            + (f":{sanitize_evidence_text(detail, max_chars=160)}" if detail else "")
        )
    return "; ".join(reasons)[:512] or "analysis_partial_reason_unavailable"


class _DownloadSampleAdapter:
    def __init__(self, hits: HitAcquisitionService, analyzer: SampleAnalyzer | None = None) -> None:
        self._hits = hits
        self._analyzer = analyzer

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            evidence_refs = _uuid_refs(arguments.get("evidence_refs", ()))
            association = await self._hits.register_hit(
                context.investigation_id,
                str(arguments["sha256"]),
                origin=SampleOrigin.VT_PIVOT,
                evidence_refs=evidence_refs,
            )
            result = await self._hits.acquire_hit(
                association.id,
                purpose=str(arguments["purpose"]),
                evidence_refs=evidence_refs,
                operation_key=context.idempotency_key,
                envelope=context.envelope,
                external_effect_authority=context.external_effect_authority,
                external_effect_evidence_refs=context.external_effect_evidence_refs,
            )
        except (ValueError, HitAcquisitionError) as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message=f"Hit acquisition input was rejected ({type(exc).__name__}).",
                budget_consumed={},
                exception_class=type(exc).__name__,
                error_code=getattr(exc, "code", None) or "hit_acquisition_rejected",
            )
        tool_result = _acquisition_tool_result(result)
        describe = getattr(self._analyzer, "describe", None)
        if result.status is ToolInvocationStatus.SUCCEEDED and callable(describe):
            try:
                metadata = await describe(result.association.id)
            except Exception:
                metadata = None
            if isinstance(metadata, Mapping):
                output = dict(tool_result.output)
                for key in ("byte_size", "detected_type", "provenance", "qualification"):
                    value = metadata.get(key)
                    if value is not None:
                        output[key] = value
                tool_result = replace(tool_result, output=output)
        return tool_result


class _SampleAnalyzeAdapter:
    def __init__(self, hits: HitAcquisitionService, analyzer: SampleAnalyzer | None) -> None:
        self._hits = hits
        self._analyzer = analyzer

    @property
    def operation_cache_identity(self) -> Mapping[str, Any]:
        identity = getattr(self._analyzer, "operation_cache_identity", None)
        if isinstance(identity, Mapping):
            return identity
        return confined_analysis_operation_cache_identity(None)

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            association_id = UUID(str(arguments["association_id"]))
            evidence_refs = _uuid_refs(arguments.get("evidence_refs", ()))
            if self._analyzer is None:
                return ToolResult(
                    ToolInvocationStatus.UNAVAILABLE,
                    suggested_alternatives=("vt.file_report", "vt.relationships"),
                    message="No local sample analysis service is registered.",
                    output={"association_id": str(association_id)},
                )
            await self._hits.validate_evidence_refs(context.investigation_id, evidence_refs)
            association = await self._hits.require_association(
                context.investigation_id, association_id
            )
            outcome = await self._analyzer.analyze(
                association_id=association.id,
                invocation_ref=None,
            )
        except (ValueError, HitAcquisitionError) as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message=f"Sample analysis input was rejected ({type(exc).__name__}).",
            )
        output = dict(outcome.output)
        describe = getattr(self._analyzer, "describe", None)
        if callable(describe):
            try:
                metadata = await describe(association.id)
            except Exception:
                metadata = None
            if isinstance(metadata, Mapping):
                for key in ("byte_size", "detected_type", "provenance", "qualification"):
                    value = metadata.get(key)
                    if value is not None:
                        output.setdefault(key, value)
        return ToolResult(
            status=outcome.status,
            observation_refs=outcome.observation_refs,
            output=output,
            suggested_alternatives=(
                ("vt.file_report", "vt.relationships")
                if outcome.status
                in {ToolInvocationStatus.UNAVAILABLE, ToolInvocationStatus.PARTIAL}
                else ()
            ),
            message=outcome.message,
            budget_consumed={},
        )


class ArchiveExtractMemberAdapter:
    """Extract one verified member in analysis-worker and ingest it to quarantine."""

    def __init__(
        self,
        *,
        hits: HitAcquisitionService,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        execution: ConfinedAnalysisExecutionPort,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> None:
        self._hits = hits
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._execution = execution
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    @property
    def operation_cache_identity(self) -> Mapping[str, Any]:
        return confined_analysis_operation_cache_identity(self._execution)

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = time.monotonic()

        def consumed() -> Mapping[BudgetDimension, int]:
            return {BudgetDimension.TOOL_SECONDS: max(1, math.ceil(time.monotonic() - started))}

        try:
            parent_association_id = UUID(str(arguments["association_id"]))
            await self._hits.require_association(context.investigation_id, parent_association_id)
            parent = await self._hits.resolve_quarantined_sample(parent_association_id)
            if parent.investigation_id != context.investigation_id:
                raise HitNotFoundError("Sample association does not belong to this investigation")
            member_path = str(arguments["member_path"])
            reason = str(arguments["reason"]).strip()
            if not reason:
                raise ValueError("reason must not be empty")
        except HitNotFoundError:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message="The sample association is not available in the current investigation.",
                exception_class="HitNotFoundError",
                error_code="archive_extract_member_association_not_in_investigation",
                budget_consumed=consumed(),
            )
        except (ValueError, HitAcquisitionError) as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message=f"Archive member extraction input was rejected ({type(exc).__name__}).",
                exception_class=type(exc).__name__,
                error_code="archive_extract_member_input_rejected",
                budget_consumed=consumed(),
            )

        transfer_nonce = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
        parameters = {
            "member_path": member_path,
            "archive_preset": str(arguments.get("archive_preset", "none")),
            "transfer_nonce": transfer_nonce,
        }
        transfer_bucket = archive_member_transfer_bucket(transfer_nonce)
        expected_sha256 = arguments.get("expected_sha256")
        if isinstance(expected_sha256, str):
            parameters["expected_sha256"] = expected_sha256
        request = ConfinedAnalysisRequest(
            investigation_id=parent.investigation_id,
            artifact_id=parent.blob_id,
            sha256=parent.sha256,
            tool=ConfinedAnalysisTool.ARCHIVE_EXTRACT_MEMBER,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=self._max_output_bytes,
            association_id=parent.association_id,
            sample_id=parent.sample_id,
            analysis_parameters=parameters,
        )
        try:
            result = await self._execution.execute(request)
        except Exception as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message=f"Confined archive extraction failed ({type(exc).__name__}).",
                exception_class=type(exc).__name__,
                error_code=getattr(exc, "code", None) or "tool_execution_failed",
                budget_consumed=consumed(),
            )
        if result.status is not ToolInvocationStatus.SUCCEEDED or result.payload is None:
            return ToolResult(
                result.status,
                output={
                    "parent_association_id": str(parent.association_id),
                    "parent_sha256": parent.sha256,
                    "available_reason": result.available_reason,
                },
                message=result.available_reason or "Confined archive extraction did not complete.",
                error_code=result.available_reason or "archive_extract_member_failed",
                budget_consumed=consumed(),
            )
        payload = result.payload
        if (
            payload.get("investigation_id") != str(parent.investigation_id)
            or payload.get("association_id") != str(parent.association_id)
            or payload.get("sample_id") != str(parent.sample_id)
            or payload.get("artifact_sha256") != parent.sha256
        ):
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message="Confined archive extraction returned a mismatched parent identity.",
                error_code="archive_extract_member_identity_mismatch",
                budget_consumed=consumed(),
            )
        worker_result = payload.get("result")
        if (
            not isinstance(worker_result, Mapping)
            or worker_result.get("member_status") != "extracted"
        ):
            status = (
                str(worker_result.get("member_status", "extract_failed"))
                if isinstance(worker_result, Mapping)
                else "extract_failed"
            )
            return ToolResult(
                ToolInvocationStatus.FAILED,
                output={"parent_sha256": parent.sha256, "member_path": member_path},
                message=f"Archive member extraction failed ({status}).",
                error_code=f"archive_member_{status}",
                budget_consumed=consumed(),
            )
        quarantine_blob_id: UUID | None = None
        try:
            transfer_blob_id = UUID(str(worker_result["transfer_blob_id"]))
            async with self._uow_factory() as uow:
                transfer_blob = await uow.blobs.get(transfer_blob_id)
                current_envelope = await uow.investigation_envelopes.get(parent.investigation_id)
            if transfer_blob is None or transfer_blob.descriptor.logical_bucket != transfer_bucket:
                raise ValueError("worker transfer blob identity mismatch")
            try:
                member_bytes = await self._artifact_store.read_bytes(
                    transfer_blob_id, max_bytes=LOCAL_ARCHIVE_MAX_MEMBER_BYTES
                )
            finally:
                await self._artifact_store.delete_unreferenced(transfer_blob_id)
            member_sha256 = str(worker_result["member_sha256"])
            member_size = int(worker_result["member_size"])
            if (
                worker_result.get("member_path") != member_path
                or not re.fullmatch(r"[0-9a-f]{64}", member_sha256)
                or not 0 <= member_size <= LOCAL_ARCHIVE_MAX_MEMBER_BYTES
                or transfer_blob.descriptor.sha256 != member_sha256
                or transfer_blob.descriptor.size != member_size
                or len(member_bytes) != member_size
                or hashlib.sha256(member_bytes).hexdigest() != member_sha256
                or (isinstance(expected_sha256, str) and member_sha256 != expected_sha256)
            ):
                raise ValueError("worker transfer bytes failed SHA-256 verification")
            if context.envelope is not None and (
                current_envelope is None
                or current_envelope.approval_decision_id != context.envelope.approval_decision_id
                or current_envelope.version != context.envelope.version
            ):
                return ToolResult(
                    ToolInvocationStatus.NEEDS_REVIEW,
                    message="The approved investigation envelope changed during extraction.",
                    error_code="investigation_envelope_changed",
                    budget_consumed=consumed(),
                )
            quarantine_blob_id = await self._artifact_store.put_bytes(
                member_bytes,
                bucket=SAMPLES_QUARANTINE_BUCKET,
                mime_type="application/octet-stream",
            )
            async with self._uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(parent.investigation_id)
                parent_association = await uow.sample_associations.get(parent.association_id)
                parent_sample = await uow.samples.get(parent.sample_id)
                parent_blob = (
                    await uow.blobs.get(parent_sample.blob_id)
                    if parent_sample is not None
                    else None
                )
                quarantine_blob = await uow.blobs.get(quarantine_blob_id)
                current_envelope = (
                    await uow.investigation_envelopes.get_for_update(parent.investigation_id)
                    if context.envelope is not None
                    else await uow.investigation_envelopes.get(parent.investigation_id)
                )
                if (
                    investigation is None
                    or parent_association is None
                    or parent_association.investigation_id != parent.investigation_id
                    or parent_association.sample_sha256 != parent.sha256
                    or parent_sample is None
                    or parent_sample.subject_id != investigation.subject_id
                    or parent_sample.blob_id != parent.blob_id
                    or parent_sample.state
                    not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
                    or parent_blob is None
                    or parent_blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
                    or parent_blob.descriptor.sha256 != parent.sha256
                    or quarantine_blob is None
                    or quarantine_blob.descriptor.logical_bucket != SAMPLES_QUARANTINE_BUCKET
                    or quarantine_blob.descriptor.sha256 != member_sha256
                    or quarantine_blob.descriptor.size != member_size
                    or (
                        context.envelope is not None
                        and (
                            current_envelope is None
                            or current_envelope.approval_decision_id
                            != context.envelope.approval_decision_id
                            or current_envelope.version != context.envelope.version
                        )
                    )
                ):
                    raise ValueError(
                        "The parent, quarantine blob or investigation policy changed "
                        "during extraction"
                    )
                candidate_sample = Sample(
                    subject_id=investigation.subject_id,
                    blob_id=quarantine_blob_id,
                    original_name=member_path[-512:],
                    origin="archive_extract_member",
                    acquired_at=datetime.now(UTC),
                    license_restriction=parent_sample.license_restriction,
                    tlp=parent_sample.tlp,
                    do_not_submit=parent_sample.do_not_submit,
                    external_llm_allowed=parent_sample.external_llm_allowed,
                    origin_kind=EntitySampleOrigin.MANUAL,
                    state=SampleState.QUARANTINED,
                    source_service="archive_extract_member",
                    source_object_id=member_sha256,
                    expected_hash=member_sha256,
                )
                sample = await uow.samples.add_if_absent(candidate_sample)
                if sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}:
                    raise ValueError("derived sample already exists outside quarantine")
                policy = derived_policy((sample, parent_sample))
                policy_changed = (
                    sample.tlp is not policy.tlp
                    or sample.do_not_submit != policy.do_not_submit
                    or sample.external_llm_allowed != policy.external_llm_allowed
                )
                if policy_changed:
                    sample.tlp = policy.tlp
                    sample.do_not_submit = policy.do_not_submit
                    sample.external_llm_allowed = policy.external_llm_allowed
                if (
                    sample.license_restriction is None
                    and parent_sample.license_restriction is not None
                ):
                    sample.license_restriction = parent_sample.license_restriction
                    policy_changed = True
                elif (
                    sample.license_restriction is not None
                    and parent_sample.license_restriction is not None
                    and sample.license_restriction != parent_sample.license_restriction
                ):
                    raise ValueError("derived sample has conflicting license restrictions")
                if policy_changed:
                    await uow.samples.save(sample)

                # Persist or lock the subject-scoped sample before creating the
                # association. The SQL repository can otherwise prelink a new
                # association to the only globally matching hash, even when that
                # sample belongs to another subject. link_sample below binds this
                # association explicitly to the validated subject sample.
                associations = await uow.sample_associations.list_for_investigation(
                    parent.investigation_id
                )
                association = next(
                    (item for item in associations if item.sample_sha256 == member_sha256),
                    None,
                )
                reused_association = association is not None
                if association is None:
                    association = await uow.sample_associations.append_version(
                        SampleAssociation(
                            id=sample_association_id(parent.investigation_id, member_sha256),
                            investigation_id=parent.investigation_id,
                            sample_sha256=member_sha256,
                            origin=SampleOrigin.RELATIONSHIP,
                        ),
                        expected_version=0,
                    )
                else:
                    association = await uow.sample_associations.append_version(
                        association, expected_version=association.version
                    )
                await uow.sample_associations.link_sample(
                    association.id, sample.id, expected_sha256=member_sha256
                )
                derivation_material = (
                    f"{parent.investigation_id}\0{parent.sha256}\0{member_path}\0{member_sha256}"
                )
                derivation_id = hashlib.sha256(derivation_material.encode()).hexdigest()
                actor_id = context.actor_id or "agent:investigation"
                attribution_material = f"{derivation_id}\0{actor_id}\0{reason}"
                attribution_id = hashlib.sha256(attribution_material.encode()).hexdigest()
                existing_events = await uow.provenance.list_for_aggregate("sample", sample.id)
                if not any(
                    item.event_type == "sample_derived_from_archive_member"
                    and item.payload.get("attribution_id") == attribution_id
                    for item in existing_events
                ):
                    await uow.provenance.append(
                        ProvenanceEvent(
                            id=uuid5(_ARCHIVE_DERIVATION_NAMESPACE, attribution_id),
                            aggregate_type="sample",
                            aggregate_id=sample.id,
                            subject_id=sample.subject_id,
                            event_type="sample_derived_from_archive_member",
                            actor_id=actor_id,
                            tlp=policy.tlp,
                            payload={
                                "provenance": "archive_extract_member",
                                "derivation_id": derivation_id,
                                "attribution_id": attribution_id,
                                "investigation_id": str(parent.investigation_id),
                                "association_id": str(association.id),
                                "parent_sample_id": str(parent.sample_id),
                                "parent_sha256": parent.sha256,
                                "member_path": member_path,
                                "member_sha256": member_sha256,
                                "member_size": member_size,
                                "actor_id": actor_id,
                                "reason": reason,
                                "purpose": context.purpose or "archive member extraction",
                            },
                        )
                    )
                await uow.commit()
        except (ValueError, RuntimeError) as exc:
            if quarantine_blob_id is not None:
                try:
                    await self._artifact_store.delete_unreferenced(quarantine_blob_id)
                except Exception:
                    # A concurrent derivation may already reference the content; the
                    # catalog refuses deletion while any canonical reference exists.
                    pass
            return ToolResult(
                ToolInvocationStatus.NEEDS_REVIEW,
                message=f"Derived sample could not be linked safely ({type(exc).__name__}).",
                error_code="archive_extract_member_persistence_failed",
                budget_consumed=consumed(),
            )
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            output={
                "association_id": str(association.id),
                "sample_id": str(sample.id),
                "sample_sha256": member_sha256,
                "parent_association_id": str(parent.association_id),
                "parent_sha256": parent.sha256,
                "member_path": member_path,
                "size_bytes": member_size,
                "qualification": association.qualification.value,
                "reused_association": reused_association,
                "quarantined": True,
            },
            message="Verified archive member was linked as a quarantined candidate sample.",
            budget_consumed=consumed(),
        )


class _QualificationAdapter:
    def __init__(self, qualifications: SampleQualificationService) -> None:
        self._qualifications = qualifications

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            association_id = UUID(str(arguments["association_id"]))
            qualification = SampleQualification(str(arguments["qualification"]))
            if qualification in {SampleQualification.CONFIRMED, SampleQualification.REJECTED}:
                return ToolResult(
                    ToolInvocationStatus.POLICY_DENIED,
                    message="G3 membership confirmation or rejection requires an identified human.",
                    output={
                        "request_permission": True,
                        "suggested_alternatives": ["sample.set_qualification"],
                    },
                )
            association = await self._qualifications.qualify(
                association_id,
                qualification,
                InvestigationActor(
                    actor_id="agent:bridge-investigator",
                    kind=QualificationActorKind.AGENT,
                ),
                str(arguments["reason"]),
                _uuid_refs(arguments.get("evidence_refs", ())),
                correlation_id=context.idempotency_key,
            )
        except (ValueError, RuntimeError) as exc:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                message=f"Sample qualification input was rejected ({type(exc).__name__}).",
            )
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            output={
                "association_id": str(association.id),
                "sample_sha256": association.sample_sha256,
                "qualification": association.qualification.value,
                "version": association.version,
            },
            message="The sample association was updated with an append-only justification.",
            budget_consumed={},
        )


def register_acquisition_tools(
    registry: ToolRegistry,
    *,
    hits: HitAcquisitionService,
    qualifications: SampleQualificationService,
    analyzer: SampleAnalyzer | None = None,
    archive_extractor: ArchiveExtractMemberAdapter | None = None,
) -> None:
    """Register the VT acquisition and local sample-analysis/qualification tools."""
    registry.register(
        ToolDefinition(
            name="vt.download_sample",
            schema_version="1",
            input_schema=VT_DOWNLOAD_SAMPLE_SCHEMA,
            effect=ToolEffect.ACQUIRE,
            cost_model=_ACQUISITION_COST,
            confidentiality=ToolConfidentiality.RESTRICTED,
            limitations=(
                "New bytes use VirusTotal's hash-verified quarantine path; verified reuse of a "
                "same-subject quarantined Sample does not call VirusTotal.",
                "On success the output includes association_id; pass that value to sample.analyze.",
                "Acquisition leaves membership in candidate state; it is not G3.",
                "A request outside G2 returns needs_review and asks for permission.",
            ),
            capability_key=VT_FILE_DOWNLOAD_CAPABILITY,
        ),
        _DownloadSampleAdapter(hits, analyzer),
    )
    registry.register(
        ToolDefinition(
            name="sample.analyze",
            schema_version="1",
            input_schema=SAMPLE_ANALYZE_SCHEMA,
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.RESTRICTED,
            limitations=(
                "Only a sample already in quarantine can be analyzed.",
                "The analysis is bounded and static; it does not establish cluster membership.",
            ),
            capability_key=SAMPLE_ANALYSIS_CAPABILITY,
        ),
        _SampleAnalyzeAdapter(hits, analyzer),
    )
    if archive_extractor is not None:
        registry.register(
            ToolDefinition(
                name="archive.extract_member",
                schema_version="1",
                input_schema=ARCHIVE_EXTRACT_MEMBER_SCHEMA,
                effect=ToolEffect.LOCAL_COMPUTE,
                cost_model={BudgetDimension.TOOL_SECONDS: 1},
                confidentiality=ToolConfidentiality.RESTRICTED,
                limitations=(
                    "Accepts only a quarantined archive associated with the current investigation.",
                    "Extraction is bounded, static and performed in analysis-worker "
                    "without execution.",
                    "The derived sample remains quarantined and in candidate qualification.",
                    "The output association_id can be passed to sample.analyze and local "
                    "analyzers.",
                ),
                capability_key=ARCHIVE_EXTRACT_MEMBER_CAPABILITY,
            ),
            archive_extractor,
        )
    registry.register(
        ToolDefinition(
            name="sample.set_qualification",
            schema_version="1",
            input_schema=SAMPLE_SET_QUALIFICATION_SCHEMA,
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=(
                "The agent may propose candidate, related or uncertain only.",
                "confirmed/rejected membership requires a separate human G3 decision.",
            ),
            capability_key=LOCAL_QUALIFICATION_CAPABILITY,
        ),
        _QualificationAdapter(qualifications),
    )


def _acquisition_tool_result(result: HitAcquisitionResult) -> ToolResult:
    output: dict[str, Any] = {
        "association_id": str(result.association.id),
        "sample_sha256": result.association.sample_sha256,
        "qualification": result.association.qualification.value,
        "sample_id": str(result.sample_id) if result.sample_id is not None else None,
        "reused_existing_sample": result.reused_existing_sample,
        "new_to_article": (
            result.novelty.novelty.value == "new_to_article" if result.novelty else False
        ),
        "request_permission": result.request_permission,
    }
    if result.novelty is not None:
        output["novelty"] = result.novelty.novelty.value
        output["novelty_scope"] = result.novelty.scope
    if result.error_code is not None:
        output["error_code"] = result.error_code
    if result.reason is not None:
        output["reason"] = result.reason
    if result.confirmed_non_execution:
        output["confirmed_non_execution"] = True
    if result.cached:
        output["cached"] = True
    return ToolResult(
        status=result.status,
        observation_refs=result.observation_refs,
        budget_consumed=result.budget_consumed
        or {BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
        suggested_alternatives=result.suggested_alternatives,
        output=output,
        message=result.message,
        exception_class=result.exception_class,
        error_code=result.error_code,
    )


def _bounded_strings(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in values[:_MAX_STRINGS]:
        value = sanitize_evidence_text(item.get("value", ""), max_chars=_MAX_STRING_CHARS)
        if not value:
            continue
        result.append(
            {
                "value": value,
                "occurrence_count": max(1, int(item.get("occurrence_count", 1))),
                "kind": sanitize_evidence_text(item.get("kind", "string"), max_chars=64),
            }
        )
    return result


def _extractor_version_summary(summary: Mapping[str, Any]) -> str:
    versions = summary.get("extractors", {})
    stable = json.dumps(versions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode()).hexdigest()[:24]


def _uuid_refs(values: Sequence[str | UUID]) -> tuple[UUID, ...]:
    return tuple(
        dict.fromkeys(value if isinstance(value, UUID) else UUID(str(value)) for value in values)
    )


__all__ = [
    "LOCAL_ANALYSIS_CAPABILITY",
    "LOCAL_QUALIFICATION_CAPABILITY",
    "SAMPLE_ANALYZE_SCHEMA",
    "SAMPLE_SET_QUALIFICATION_SCHEMA",
    "VT_DOWNLOAD_SAMPLE_SCHEMA",
    "VT_FILE_DOWNLOAD_CAPABILITY",
    "LocalSampleAnalysisService",
    "SampleAnalysisOutcome",
    "SampleAnalyzer",
    "register_acquisition_tools",
]
