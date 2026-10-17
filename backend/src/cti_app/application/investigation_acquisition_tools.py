"""Tool adapters for quarantined sample acquisition, analysis and qualification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
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
LOCAL_ANALYSIS_CAPABILITY = "investigation.local_read"
LOCAL_QUALIFICATION_CAPABILITY = "investigation.local_read"
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
    """Compose existing static, SMDA, capa and goodware services for a quarantine sample."""

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
    ) -> None:
        self._hits = hits
        self._uow_factory = uow_factory
        self._recorder = recorder
        self._static_analysis = static_analysis
        self._code_features = code_features
        self._capabilities = capabilities
        self._goodware = goodware

    async def analyze(
        self,
        *,
        association_id: UUID,
        invocation_ref: UUID | None,
    ) -> SampleAnalysisOutcome:
        if not any(
            (self._static_analysis, self._code_features, self._capabilities, self._goodware)
        ):
            return SampleAnalysisOutcome(
                ToolInvocationStatus.UNAVAILABLE,
                (),
                {"association_id": str(association_id)},
                "No local sample analysis service is configured.",
            )
        sample = await self._hits.resolve_quarantined_sample(association_id)
        summary: dict[str, Any] = {
            "schema": "sample-analysis.v1",
            "sample_sha256": sample.sha256,
            "extractors": {},
            "bounds": {
                "max_strings": _MAX_STRINGS,
                "max_string_chars": _MAX_STRING_CHARS,
                "max_payload_strings": _MAX_STRINGS,
            },
            "limitations": [
                "Analysis is local and static; it does not show runtime execution.",
                "Extractor output is untrusted and bounded before it is archived.",
                "Analysis does not qualify this sample as a cluster member.",
            ],
        }
        strings: list[dict[str, Any]] = []
        statuses: list[ToolInvocationStatus] = []
        static_features: Any | None = None
        code_feature_set: Any | None = None
        capability_set: Any | None = None

        if self._static_analysis is not None:
            try:
                static_features = await self._static_analysis.analyze(sample.sample_id)
                static_json = static_features.as_json()
                raw_strings = static_json.get("strings", ())
                strings = _bounded_strings(raw_strings)
                summary["extractors"]["static"] = {
                    "version": static_features.extractor_version,
                    "parameters_sha256": static_features.parameters_sha256,
                    "max_sample_bytes": getattr(self._static_analysis, "_max_sample_bytes", None),
                    "string_min_length": getattr(self._static_analysis, "_string_min_length", None),
                    "max_strings": getattr(self._static_analysis, "_max_strings", None),
                }
                summary["static_analysis"] = {
                    "format": static_features.format.value,
                    "size": static_features.size,
                    "sha256": static_features.sha256,
                    "sections": len(static_features.sections),
                    "imports": len(static_features.imports),
                    "exports": len(static_features.exports),
                    "strings_observed": len(raw_strings),
                    "strings_recorded": len(strings),
                    "partial_errors": list(static_features.partial_errors[:16]),
                }
                statuses.append(ToolInvocationStatus.SUCCEEDED)
            except Exception as exc:
                summary["static_analysis"] = {"status": "failed", "error_type": type(exc).__name__}
                statuses.append(ToolInvocationStatus.FAILED)

        if self._code_features is not None:
            parameter_payload = {
                "analysis": "investigation-smda.v1",
                "code_ngram_sizes": [4, 6, 8],
                "code_ngram_max_per_sample": 100_000,
            }
            parameter_hash = hashlib.sha256(
                json.dumps(parameter_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            try:
                code_feature_set = await self._code_features.extract(
                    sample_id=sample.sample_id,
                    parameters_sha256=parameter_hash,
                    code_ngram_sizes=(4, 6, 8),
                    code_ngram_max_per_sample=100_000,
                )
                summary["extractors"]["smda"] = {
                    "version": code_feature_set.tool_version,
                    "escaper_compatibility_version": code_feature_set.escaper_compatibility_version,
                    "intel_pic_hash_escape_version": code_feature_set.intel_pic_hash_escape_version,
                    "parameters_sha256": parameter_hash,
                    "max_ngrams": 100_000,
                }
                summary["code_features"] = {
                    "status": code_feature_set.status.value,
                    "architecture": code_feature_set.architecture,
                    "ngram_count": len(code_feature_set.ngrams),
                    "packing": code_feature_set.as_json()["packing"],
                    "errors": list(code_feature_set.errors[:16]),
                }
                statuses.append(
                    ToolInvocationStatus.SUCCEEDED
                    if code_feature_set.status.value == "SUCCEEDED"
                    else ToolInvocationStatus.PARTIAL
                )
            except Exception as exc:
                summary["code_features"] = {"status": "failed", "error_type": type(exc).__name__}
                statuses.append(ToolInvocationStatus.FAILED)

        if self._capabilities is not None:
            try:
                capability_set = await self._capabilities.analyze(sample.sample_id)
                summary["extractors"]["capa"] = {
                    "version": capability_set.tool_version,
                    "ruleset_sha256": capability_set.ruleset_sha256,
                    "parameters_sha256": capability_set.parameters_sha256,
                }
                summary["capa"] = {
                    "status": capability_set.status.value,
                    "capabilities": [
                        item.rule_id for item in capability_set.capabilities[:_MAX_ANALYSIS_OUTPUT]
                    ],
                    "capabilities_observed": len(capability_set.capabilities),
                    "errors": list(capability_set.errors[:16]),
                }
                statuses.append(
                    ToolInvocationStatus.SUCCEEDED
                    if capability_set.status.value.upper() == "SUCCEEDED"
                    else ToolInvocationStatus.PARTIAL
                )
            except Exception as exc:
                summary["capa"] = {"status": "failed", "error_type": type(exc).__name__}
                statuses.append(ToolInvocationStatus.FAILED)

        goodware_summary = await self._goodware_summary(
            sample.investigation_id,
            static_features=static_features,
            code_feature_set=code_feature_set,
            capability_set=capability_set,
        )
        summary["goodware"] = goodware_summary
        baseline_id = goodware_summary.get("baseline_id")
        if isinstance(baseline_id, str):
            summary["extractors"]["goodware"] = {"baseline_id": baseline_id}
        if self._code_features is None or not goodware_summary.get("available"):
            statuses.append(ToolInvocationStatus.PARTIAL)
        string_refs: tuple[UUID, ...] = ()
        if strings:
            string_observation = await self._recorder.record_json(
                investigation_id=sample.investigation_id,
                kind=EvidenceKind.BINARY_STRING,
                payload={
                    "schema": "sample-strings.v1",
                    "sample_sha256": sample.sha256,
                    "extractor_version": (
                        static_features.extractor_version
                        if static_features is not None
                        else "unknown"
                    ),
                    "bounds": {"max_items": _MAX_STRINGS, "max_chars_per_item": _MAX_STRING_CHARS},
                    "strings": strings,
                    "truncated": len(getattr(static_features, "strings", ())) > len(strings),
                },
                source="autowork.static_analysis",
                source_version=(
                    static_features.extractor_version if static_features is not None else "unknown"
                ),
                object_type="sample",
                object_id=sample.sha256,
                limitations=(
                    "Extracted strings are untrusted data and may be incomplete.",
                    "A string match alone does not establish execution or attribution.",
                ),
                invocation_ref=invocation_ref,
                idempotency_key=f"sample-strings:{sample.investigation_id}:{sample.sha256}",
            )
            string_refs = (string_observation.id,)

        analysis_observation = await self._recorder.record_json(
            investigation_id=sample.investigation_id,
            kind=EvidenceKind.BINARY_ANALYSIS,
            payload=summary,
            source="autowork.local_sample_analysis",
            source_version=_extractor_version_summary(summary),
            object_type="sample",
            object_id=sample.sha256,
            limitations=tuple(summary["limitations"]),
            invocation_ref=invocation_ref,
            idempotency_key=(
                f"sample-analysis:{sample.investigation_id}:{sample.sha256}:"
                f"{_extractor_version_summary(summary)}"
            ),
        )
        refs = (*string_refs, analysis_observation.id)
        final_status = (
            ToolInvocationStatus.FAILED
            if statuses and all(item is ToolInvocationStatus.FAILED for item in statuses)
            else ToolInvocationStatus.PARTIAL
            if ToolInvocationStatus.FAILED in statuses or ToolInvocationStatus.PARTIAL in statuses
            else ToolInvocationStatus.SUCCEEDED
        )
        return SampleAnalysisOutcome(
            status=final_status,
            observation_refs=refs,
            output={
                "association_id": str(association_id),
                "sample_id": str(sample.sample_id),
                "sample_sha256": sample.sha256,
                "qualification": SampleQualification.CANDIDATE.value,
                "extractors": summary["extractors"],
                "goodware": goodware_summary,
                "strings_recorded": len(strings),
                "limitations": summary["limitations"],
            },
            message=(
                "Analysis observations were recorded; membership remains candidate."
                if final_status is not ToolInvocationStatus.FAILED
                else "Every configured local analysis service failed."
            ),
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
        if static_features is not None:
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


class _DownloadSampleAdapter:
    def __init__(self, hits: HitAcquisitionService) -> None:
        self._hits = hits

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
        return _acquisition_tool_result(result)


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
        return ToolResult(
            status=outcome.status,
            observation_refs=outcome.observation_refs,
            output=outcome.output,
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
                "Downloads use VirusTotal's existing hash-verified quarantine path.",
                "Acquisition leaves membership in candidate state; it is not G3.",
                "A request outside G2 returns needs_review and asks for permission.",
            ),
            capability_key=VT_FILE_DOWNLOAD_CAPABILITY,
        ),
        _DownloadSampleAdapter(hits),
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
                "The analysis is local, bounded, static and does not establish cluster membership.",
            ),
            capability_key=LOCAL_ANALYSIS_CAPABILITY,
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
