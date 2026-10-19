"""Tool adapters for quarantined sample acquisition, analysis and qualification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.confined_analysis import (
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisRequest,
    ConfinedAnalysisTool,
)
from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
from cti_app.application.goodware import GoodwareMeasurementService
from cti_app.application.investigation_acquisition import (
    HitAcquisitionError,
    HitAcquisitionResult,
    HitAcquisitionService,
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
from cti_app.application.static_analysis import StaticAnalysisService
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    QualificationActorKind,
    SampleOrigin,
    SampleQualification,
    ToolInvocationStatus,
)

_ACQUISITION_COST = {
    BudgetDimension.VT_UNITS: 1,
    BudgetDimension.DOWNLOADS: 1,
}
VT_FILE_DOWNLOAD_CAPABILITY = "vt.file_download"
LOCAL_QUALIFICATION_CAPABILITY = "investigation.local_read"
SAMPLE_ANALYSIS_CAPABILITY = "investigation.sample_analysis"
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
            "operator_import"
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
                    source_version="confined-analysis-result.v1",
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
