"""Build bounded, self-contained Bridge contexts from canonical investigation data."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, Protocol, TypeGuard
from uuid import UUID

from cti_app.application.advisor_checkpoint import advisor_checkpoint_summary
from cti_app.application.context_pagination import (
    ContextOmissionManifestEntryV1,
    ContextOmissionReason,
    KeysetPageV1,
    assert_manifest_consistency,
    canonical_page_sha256,
    decode_keyset_cursor,
    encode_keyset_cursor,
    make_keyset_page,
    source_reference,
)
from cti_app.application.investigation_contracts import (
    MAX_CONTEXT_CONFIRMED_IOC_BYTES,
    MAX_CONTEXT_CONFIRMED_IOCS,
    AdvisorGuidanceV1,
    ArticleClaimV1,
    ArticleContextV1,
    ConfirmedIOCContextV1,
    ContextInvestigationV1,
    CurrentOpinionContextV1,
    EvidenceSummaryV1,
    FindingSummaryV1,
    IndicatorInventoryV1,
    InvestigationContextV1,
    InvestigationSourceV1,
    InvestigationUserRequestV1,
    LatestToolResultV1,
    OpinionNoteSummaryV1,
    PublicationAnchorV1,
    ToolCoverageEntryV1,
    ToolSummaryV1,
)
from cti_app.application.investigation_persistence import (
    AnalystOpinionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    SampleAssociationRepository,
    ToolInvocationRepository,
)
from cti_app.application.investigation_pivot_frontier import discovered_indicator_values
from cti_app.application.investigation_pivots import available_investigation_pivots
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    project_tool_availability,
)
from cti_app.application.operator_advisor_guidance import (
    ADVISOR_CONSULTATION_TYPES,
    bound_advisor_guidance,
    build_advisor_guidance,
)
from cti_app.config import get_settings
from cti_app.domain.analyst_opinion import AnalystOpinionV1, investigation_opinion_id
from cti_app.domain.investigation import (
    BudgetDimension,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    InvestigationExecutionMode,
    InvestigationFinding,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    thaw_json,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    InvestigationWorkingState,
)
from cti_app.domain.rule_workbench import RuleTestCorpusRole, RuleValidationStatus

MAX_ARTICLE_NARRATIVE_CHARS = 8_000
MAX_CONTEXT_HYPOTHESES = 16
MAX_CONTEXT_EVIDENCE = 16
MAX_CONTEXT_SOURCE_PAGE = 256
MAX_CONTEXT_EVIDENCE_CHARS = 600
MAX_CONTEXT_EVIDENCE_INDEX = 200
MAX_CONTEXT_EVIDENCE_INDEX_BYTES = 24 * 1024
MAX_CONTEXT_TOOL_COVERAGE = 200
MAX_CONTEXT_TOOL_COVERAGE_BYTES = 48 * 1024
MAX_CONTEXT_ACQUIRED_SAMPLES = 64
MAX_CONTEXT_LATEST_TOOL_RESULTS = 16
MAX_CONTEXT_OPINION_NOTES = 24
MAX_CONTEXT_OPINION_NOTE_BYTES = 12 * 1024
PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE = "provider_authorization_refusal"
PROVIDER_AUTHORIZATION_REFUSAL_THRESHOLD = 2
PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE = "provider_download_route_refused"
PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE = "provider_download_authorization_refused"
PROVIDER_DOWNLOAD_REFUSAL_THRESHOLD = 2
MAX_CONTEXT_FINDINGS = 200
MAX_CONTEXT_FINDING_BYTES = 48 * 1024
MAX_CONTEXT_FINDING_STATEMENT_CHARS = 420
MAX_CONTEXT_PUBLICATION_ANCHORS = 128
MAX_CONTEXT_PUBLICATION_ANCHOR_BYTES = 20 * 1024
MAX_CONTEXT_TOOLS = 64
MAX_PIVOT_SOURCE_OBSERVATIONS = 512
MAX_CONTEXT_RULE_CORPUS_IMPORTS = 64
MAX_CONTEXT_RULE_CANDIDATES = 16
INVESTIGATION_MAX_CONTEXT_BYTES = 256 * 1024
USER_REQUEST_ACTION_PREFIX = "user-request:"
MAX_OPERATOR_REQUESTS = 8
MAX_OPERATOR_REQUEST_BYTES = 32_768
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer|token|secret|password|authorization|credential|cookie|signature)",
    re.I,
)
_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_ -]?key|private[_ -]?key|access[_ -]?token|client[_ -]?secret|"
    r"api[_ -]?token|authorization|credential|password|secret|signature|sig)\s*[:=]\s*[^\s,;]+"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SIGNED_URL = re.compile(r"https?://[^\s\]\[()<>\"]+", re.I)
_SIGNED_QUERY = re.compile(r"[?&](?:x-amz-[^=]+|x-goog-[^=]+|signature|sig|access_token)=", re.I)


@dataclass(frozen=True, slots=True)
class ArticlePassageSnapshot:
    """Exact paragraph text retained from the verified canonical synthesis."""

    claim_id: str
    section_index: int | None
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class ArticleSnapshot:
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    article: ArticleContextV1
    base_publication_artifact_id: UUID | None = None
    base_publication_sha256: str | None = None
    tlp: str | None = None
    do_not_submit: bool = True
    external_llm_allowed: bool = False
    narrative_hashes: tuple[str, ...] = ()
    confirmed_iocs: tuple[ConfirmedIOCContextV1, ...] = ()
    confirmed_iocs_truncated: bool = False
    article_passages: tuple[ArticlePassageSnapshot, ...] = ()
    synthesis_version: int | None = None

    def __post_init__(self) -> None:
        if self.synthesis_version is not None and (
            isinstance(self.synthesis_version, bool)
            or not isinstance(self.synthesis_version, int)
            or self.synthesis_version <= 0
        ):
            raise ValueError("Article snapshot synthesis version must be positive")
        if (self.base_publication_artifact_id is None) != (self.base_publication_sha256 is None):
            raise ValueError("Base publication identity and SHA-256 must be supplied together")
        for value in (self.synthesis_sha256, self.base_publication_sha256):
            if value is not None and (
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError("Article snapshot hashes must be lowercase SHA-256")

    @property
    def source(self) -> InvestigationSourceV1:
        return InvestigationSourceV1(
            subject_id=self.subject_id,
            production_run_id=self.production_run_id,
            synthesis_artifact_id=self.synthesis_artifact_id,
            synthesis_sha256=self.synthesis_sha256,
        )


class ArticleSnapshotPort(Protocol):
    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot: ...

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[ArticleClaimV1, ...]: ...


class EvidenceSummaryPort(Protocol):
    async def summary(self, observation: EvidenceObservation) -> Mapping[str, Any] | str | None: ...


class RuleCorpusContextInventoryPort(Protocol):
    async def manual_rule_corpus_imports(self, investigation_id: UUID) -> Sequence[Any]: ...

    async def rule_candidate_versions(
        self, investigation_id: UUID
    ) -> Sequence[DetectionCandidateVersion]: ...


class CanonicalEvidenceSummaryPort:
    """Read bounded, structured facts from canonical evidence blobs."""

    def __init__(self, artifact_store: Any, *, max_payload_bytes: int = 512 * 1024 * 1024) -> None:
        self._artifact_store = artifact_store
        self._max_payload_bytes = max_payload_bytes

    async def blob_metadata(self, blob_id: UUID) -> Mapping[str, Any] | None:
        """Read canonical blob descriptors only; this never fetches blob content."""
        catalog = getattr(self._artifact_store, "_catalog", None)
        uow_factory = getattr(catalog, "_uow_factory", None)
        if not callable(uow_factory):
            return None
        try:
            async with uow_factory() as uow:
                blob = await uow.blobs.get(blob_id)
        except Exception:
            return None
        if blob is None:
            return None
        descriptor = blob.descriptor
        return {
            "sha256": descriptor.sha256,
            "byte_size": descriptor.size,
            "mime_type": descriptor.mime_type,
        }

    async def sample_inventory(self, investigation_id: UUID) -> tuple[Mapping[str, Any], ...]:
        """List acquired quarantine descriptors without loading any sample bytes."""
        catalog = getattr(self._artifact_store, "_catalog", None)
        uow_factory = getattr(catalog, "_uow_factory", None)
        if not callable(uow_factory):
            return ()
        try:
            async with uow_factory() as uow:
                investigation = await uow.analyst_investigations.get(investigation_id)
                if investigation is None:
                    return ()
                associations = tuple(
                    await uow.sample_associations.list_for_investigation(investigation_id)
                )
                samples = tuple(await uow.samples.list_for_subject(investigation.subject_id))
                rows: list[Mapping[str, Any]] = []
                for association in associations:
                    sample = next(
                        (
                            item
                            for item in samples
                            if item.expected_hash == association.sample_sha256
                            or item.source_object_id == association.sample_sha256
                        ),
                        None,
                    )
                    if sample is None or sample.source_service not in {
                        "virustotal",
                        "operator_import",
                    }:
                        continue
                    if getattr(sample.state, "value", sample.state) not in {
                        "quarantined",
                        "review_candidate",
                    }:
                        continue
                    blob = await uow.blobs.get(sample.blob_id)
                    if (
                        blob is None
                        or blob.descriptor.sha256 != association.sample_sha256
                        or blob.descriptor.logical_bucket
                        not in {"samples-quarantine", "network-captures-quarantine"}
                    ):
                        continue
                    mime_type = blob.descriptor.mime_type
                    rows.append(
                        {
                            "association_id": str(association.id),
                            "sample_id": str(sample.id),
                            "sha256": association.sample_sha256,
                            "byte_size": blob.descriptor.size,
                            "detected_type": (
                                None
                                if mime_type.casefold()
                                in {"application/octet-stream", "binary/octet-stream"}
                                else mime_type
                            ),
                            "provenance": (
                                "operator_import"
                                if sample.source_service == "operator_import"
                                else "vt_download"
                            ),
                            "qualification": association.qualification.value,
                        }
                    )
                return tuple(rows)
        except Exception:
            return ()

    async def summary(self, observation: EvidenceObservation) -> Mapping[str, Any] | None:
        if (
            observation.source == "virustotal.sample_download"
            and observation.object_type == "sample"
        ):
            return None
        metadata = await self.blob_metadata(observation.blob_id)
        if metadata is not None:
            if metadata.get("sha256") != observation.sha256:
                return None
            mime_type = metadata.get("mime_type")
            normalized_mime = (
                mime_type.split(";", 1)[0].strip().casefold() if isinstance(mime_type, str) else ""
            )
            if normalized_mime != "application/json":
                return _binary_evidence_summary(observation, metadata)
        reader = getattr(self._artifact_store, "read_bytes", None)
        if not callable(reader):
            return _binary_evidence_summary(observation, metadata) if metadata else None
        raw: bytes | None = None
        try:
            raw = await reader(observation.blob_id, max_bytes=self._max_payload_bytes)
            if hashlib.sha256(raw).hexdigest() != observation.sha256:
                return None
            payload = json.loads(raw)
        except Exception:
            if raw is not None:
                return _binary_evidence_summary(observation, metadata, observed_size=len(raw))
            return _binary_evidence_summary(observation, metadata) if metadata else None
        if not isinstance(payload, Mapping):
            return _binary_evidence_summary(observation, metadata, observed_size=len(raw))
        schema = payload.get("schema")
        if schema == "investigation-tool-observation.v1":
            result = payload.get("result")
            if not isinstance(result, Mapping):
                return None
            if (
                observation.object_type == "tool_result"
                and observation.object_id == "vt.download_sample"
            ):
                return _download_sample_result_facts(result)
            return result
        if (
            schema == "operator-advisor-call.v1"
            and observation.source == "advisor.consult"
            and observation.object_type == "advisor_call"
        ):
            summary = _operator_advisor_call_summary(payload)
            usage = _advisor_usage_facts(payload)
            if summary is None:
                # Some advisor response versions intentionally have no normalized prose
                # projection. Keep their verified accounting visible without exposing output.
                return {"schema": schema, **usage}
            return {**summary, **usage}
        if schema == "local-command-execution.v1":
            return {
                key: payload[key]
                for key in (
                    "command_id",
                    "command",
                    "sample_sha256",
                    "status",
                    "exit_code",
                    "output",
                    "output_truncated",
                    "detail_code",
                    "raw_output_bytes",
                    "range",
                    "partial_reason",
                    "untrusted_data",
                )
                if key in payload
            }
        if schema == "ghidra-investigation-observation.v1":
            return {
                key: payload[key]
                for key in (
                    "operation",
                    "address",
                    "ghidra_version",
                    "truncated",
                    "data",
                    "untrusted_data",
                )
                if key in payload
            }
        if schema == "sample-strings.v1":
            return {
                key: payload[key]
                for key in ("sample_sha256", "bounds", "strings", "truncated")
                if key in payload
            }
        if schema == "virustotal-investigation-summary.v1":
            return payload
        if schema == "NetworkCaptureReportV1":
            bounded: dict[str, Any] = {"schema": schema}
            for field in (
                "endpoints",
                "dns_observations",
                "http_observations",
                "tls_observations",
            ):
                items = payload.get(field)
                if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                    continue
                selected: list[dict[str, str]] = []
                for item in items[:64]:
                    if not isinstance(item, Mapping):
                        continue
                    keys = ("address",) if field == "endpoints" else ("name", "value")
                    selected.append(
                        {
                            key: value[:512]
                            for key in keys
                            if isinstance((value := item.get(key)), str)
                        }
                    )
                bounded[field] = selected
            return bounded
        if payload.get("schema_version") == "family-feature-comparison-v1":
            sample_hashes = payload.get("comparison_sample_sha256s")
            if isinstance(sample_hashes, Sequence) and not isinstance(sample_hashes, (str, bytes)):
                return {
                    "schema_version": "family-feature-comparison-v1",
                    "comparison_sample_sha256s": [
                        value for value in sample_hashes[:64] if isinstance(value, str)
                    ],
                }
        if schema == "shodan-observation.v1":
            summary = payload.get("summary")
            return summary if isinstance(summary, Mapping) else None
        if schema == "investigation-yara-test.v1":
            return {
                key: payload[key]
                for key in (
                    "operation",
                    "source_sha256",
                    "corpus_sha256",
                    "corpus_sha256s_by_role",
                    "validation_status",
                    "execution_status",
                    "coverage_statement",
                    "provenance_counts_by_role",
                    "false_positive_count",
                    "false_negative_count",
                    "non_measurable_count",
                    "revision_recommendation",
                    "revision_guidance",
                )
                if key in payload
            }
        return None

    async def advisor_usage(self, observation: EvidenceObservation) -> Mapping[str, Any]:
        """Read reported usage and a clearly labelled context-size estimate."""
        unavailable = {"status": "usage_unavailable"}
        if observation.source != "advisor.consult" or observation.object_type not in {
            "advisor_call",
            "tool_call",
        }:
            return {
                "provider_usage": unavailable,
                "bridge_invocation_id": None,
                "estimated_context_tokens": {
                    "status": "not_available",
                    "reason": "advisor_observation_unavailable",
                },
            }
        metadata = await self.blob_metadata(observation.blob_id)
        if metadata is not None:
            if metadata.get("sha256") != observation.sha256:
                return {
                    "provider_usage": unavailable,
                    "bridge_invocation_id": None,
                    "estimated_context_tokens": {
                        "status": "not_available",
                        "reason": "evidence_hash_mismatch",
                    },
                }
            mime_type = metadata.get("mime_type")
            normalized_mime = (
                mime_type.split(";", 1)[0].strip().casefold() if isinstance(mime_type, str) else ""
            )
            if normalized_mime != "application/json":
                return {
                    "provider_usage": unavailable,
                    "bridge_invocation_id": None,
                    "estimated_context_tokens": {
                        "status": "not_available",
                        "reason": "evidence_not_json",
                    },
                }
        reader = getattr(self._artifact_store, "read_bytes", None)
        if not callable(reader):
            return {
                "provider_usage": unavailable,
                "bridge_invocation_id": None,
                "estimated_context_tokens": {
                    "status": "not_available",
                    "reason": "evidence_reader_unavailable",
                },
            }
        try:
            raw = await reader(observation.blob_id, max_bytes=self._max_payload_bytes)
            if hashlib.sha256(raw).hexdigest() != observation.sha256:
                return {
                    "provider_usage": unavailable,
                    "bridge_invocation_id": None,
                    "estimated_context_tokens": {
                        "status": "not_available",
                        "reason": "evidence_hash_mismatch",
                    },
                }
            payload = json.loads(raw)
        except Exception:
            return {
                "provider_usage": unavailable,
                "bridge_invocation_id": None,
                "estimated_context_tokens": {
                    "status": "not_available",
                    "reason": "evidence_unreadable",
                },
            }
        if not isinstance(payload, Mapping):
            return {
                "provider_usage": unavailable,
                "bridge_invocation_id": None,
                "estimated_context_tokens": {
                    "status": "not_available",
                    "reason": "evidence_payload_invalid",
                },
            }
        if payload.get("schema") == "investigation-tool-observation.v1":
            result = payload.get("result")
            payload = result if isinstance(result, Mapping) else {}
        elif payload.get("schema") != "operator-advisor-call.v1":
            payload = {}
        return _advisor_usage_facts(payload)


def _advisor_usage_facts(payload: Mapping[str, Any]) -> dict[str, Any]:
    unavailable = {"status": "usage_unavailable"}
    raw_usage = payload.get("provider_usage")
    provider_usage: dict[str, Any] = unavailable
    if isinstance(raw_usage, Mapping) and raw_usage.get("status") == "available":
        allowed_counts = (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        )
        counts = {
            key: value
            for key in allowed_counts
            if isinstance((value := raw_usage.get(key)), int)
            and not isinstance(value, bool)
            and value >= 0
        }
        if counts:
            provider_usage = {"status": "available", **counts}
    elif isinstance(raw_usage, Mapping) and raw_usage.get("status") == "not_submitted":
        provider_usage = {"status": "not_submitted"}

    raw_invocation_id = payload.get("bridge_invocation_id")
    try:
        bridge_invocation_id = str(UUID(str(raw_invocation_id))) if raw_invocation_id else None
    except (TypeError, ValueError):
        bridge_invocation_id = None

    context_bytes = payload.get("context_bytes")
    cost = payload.get("cost")
    if not isinstance(context_bytes, int) or isinstance(context_bytes, bool) or context_bytes < 0:
        context_bytes = cost.get("context_bytes") if isinstance(cost, Mapping) else None
    positive_context_bytes = (
        context_bytes
        if isinstance(context_bytes, int)
        and not isinstance(context_bytes, bool)
        and context_bytes > 0
        else None
    )
    estimate = payload.get("context_estimate", payload.get("estimated_context_tokens"))
    if isinstance(estimate, Mapping) and estimate.get("status") == "estimated":
        value = estimate.get("value")
        estimated_bytes = estimate.get("context_bytes", context_bytes)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            and isinstance(estimated_bytes, int)
            and not isinstance(estimated_bytes, bool)
            and estimated_bytes >= 0
        ):
            estimated_context_tokens: dict[str, Any] = {
                "status": "estimated",
                "value": value,
                "context_bytes": estimated_bytes,
                "basis": "context_bytes/4",
            }
        else:
            estimated_context_tokens = {
                "status": "not_available",
                "reason": "stored_estimate_invalid",
            }
    elif isinstance(estimate, Mapping) and estimate.get("status") == "not_available":
        reason = estimate.get("reason")
        estimated_context_tokens = {
            "status": "not_available",
            "reason": reason if isinstance(reason, str) and len(reason) <= 80 else "unavailable",
        }
    elif positive_context_bytes is not None:
        estimated_context_tokens = {
            "status": "estimated",
            "value": (positive_context_bytes + 3) // 4,
            "context_bytes": positive_context_bytes,
            "basis": "context_bytes/4",
        }
    else:
        estimated_context_tokens = {
            "status": "not_available",
            "reason": "no_verified_context_bytes",
        }
    return {
        "provider_usage": provider_usage,
        "bridge_invocation_id": bridge_invocation_id,
        "estimated_context_tokens": estimated_context_tokens,
    }


def _operator_advisor_call_summary(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Expose only validated, normalized, size-bounded advisor facts."""
    consultation_type = payload.get("consultation_type")
    output = payload.get("output")
    input_refs = _advisor_observation_refs(payload.get("input_observation_refs"))
    if (
        not isinstance(consultation_type, str)
        or consultation_type not in ADVISOR_CONSULTATION_TYPES
        or input_refs is None
        or not isinstance(output, Mapping)
        or output.get("schema") != "advisor-response.v2"
        or output.get("consultation_type") != consultation_type
        or output.get("untrusted") is not True
    ):
        return None

    summary = _advisor_bounded_text(output.get("summary"), 1_800)
    alternatives = output.get("alternative_hypotheses")
    experiment = output.get("discriminating_experiment")
    discarded = output.get("discarded_lead")
    recommendations = output.get("recommended_next_tools")
    if (
        summary is None
        or not isinstance(alternatives, (list, tuple))
        or not alternatives
        or not isinstance(experiment, Mapping)
        or not isinstance(discarded, Mapping)
        or not isinstance(recommendations, (list, tuple))
    ):
        return None

    bounded_alternatives: list[dict[str, Any]] = []
    for item in alternatives:
        if not isinstance(item, Mapping):
            return None
        statement = _advisor_bounded_text(item.get("statement"), 240)
        basis_refs = _advisor_bounded_text_refs(item.get("basis_refs"))
        if statement is None or basis_refs is None or not basis_refs:
            return None
        bounded_alternatives.append({"statement": statement, "basis_refs": basis_refs})

    experiment_question = _advisor_bounded_text(experiment.get("question"), 240)
    experiment_signal = _advisor_bounded_text(experiment.get("expected_signal"), 240)
    experiment_basis = _advisor_bounded_text_refs(experiment.get("basis_refs"))
    experiment_tool = experiment.get("tool")
    experiment_schema = experiment.get("schema_version")
    bounded_experiment_tool = (
        None if experiment_tool is None else _advisor_bounded_text(experiment_tool, 160)
    )
    bounded_experiment_schema = (
        None if experiment_schema is None else _advisor_bounded_text(experiment_schema, 64)
    )
    if (
        experiment_question is None
        or experiment_signal is None
        or experiment_basis is None
        or not experiment_basis
        or (experiment_tool is None) != (experiment_schema is None)
        or (experiment_tool is not None and bounded_experiment_tool is None)
        or (experiment_schema is not None and bounded_experiment_schema is None)
    ):
        return None
    bounded_experiment = {
        "question": experiment_question,
        "tool": bounded_experiment_tool,
        "schema_version": bounded_experiment_schema,
        "expected_signal": experiment_signal,
        "basis_refs": experiment_basis,
    }

    discarded_lead = _advisor_bounded_text(discarded.get("lead"), 240)
    discarded_reason = _advisor_bounded_text(discarded.get("reason"), 240)
    discarded_basis = _advisor_bounded_text_refs(discarded.get("basis_refs"))
    if discarded_lead is None or discarded_reason is None or not discarded_basis:
        return None
    bounded_discarded = {
        "lead": discarded_lead,
        "reason": discarded_reason,
        "basis_refs": discarded_basis,
    }

    input_ref_set = set(input_refs)
    bounded_recommendations: list[dict[str, Any]] = []
    for item in recommendations:
        if not isinstance(item, Mapping):
            return None
        tool = _advisor_bounded_text(item.get("tool"), 160)
        schema_version = _advisor_bounded_text(item.get("schema_version"), 64)
        why = _advisor_bounded_text(item.get("why"), 200)
        expected_signal = _advisor_bounded_text(item.get("expected_signal"), 220)
        basis_refs = _advisor_observation_refs(item.get("basis_refs"))
        basis_observation_refs = _advisor_observation_refs(item.get("basis_observation_refs"))
        if (
            tool is None
            or schema_version is None
            or why is None
            or expected_signal is None
            or basis_refs is None
            or not basis_refs
            or basis_observation_refs is None
            or not basis_observation_refs
            or not set(basis_refs).issubset(input_ref_set)
            or not set(basis_observation_refs).issubset(input_ref_set)
        ):
            return None
        bounded_recommendations.append(
            {
                "tool": tool,
                "schema_version": schema_version,
                "why": why,
                "expected_signal": expected_signal,
                "basis_refs": basis_refs,
                "basis_observation_refs": basis_observation_refs,
            }
        )

    risks = _advisor_bounded_text_list(output.get("risks", ()), max_items=5, max_chars=180)
    questions = _advisor_bounded_text_list(output.get("questions", ()), max_items=5, max_chars=180)
    normalizations = _advisor_bounded_text_list(
        output.get("normalizations", ()), max_items=32, max_chars=180
    )
    basis_observation_refs = _advisor_observation_refs(output.get("basis_observation_refs", ()))
    if (
        risks is None
        or questions is None
        or normalizations is None
        or basis_observation_refs is None
    ):
        return None

    bounded_output: dict[str, Any] = {
        "schema": "advisor-response.v2",
        "consultation_type": consultation_type,
        "summary": summary,
        "alternative_hypotheses": bounded_alternatives,
        "discriminating_experiment": bounded_experiment,
        "discarded_lead": bounded_discarded,
        "basis_observation_refs": basis_observation_refs,
        "recommended_next_tools": bounded_recommendations,
        "risks": risks,
        "questions": questions,
        "untrusted": True,
    }
    if normalizations:
        bounded_output["normalizations"] = normalizations
    normalization_status = output.get("normalization_status")
    if normalization_status == "partially_normalized":
        bounded_output["normalization_status"] = normalization_status

    bounded = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": consultation_type,
        "input_observation_refs": input_refs,
        "output": bounded_output,
    }
    return bounded


def _advisor_bounded_text(value: Any, max_chars: int) -> str | None:
    del max_chars
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _advisor_bounded_text_list(value: Any, *, max_items: int, max_chars: int) -> list[str] | None:
    del max_items
    if not isinstance(value, (list, tuple)):
        return None
    selected: list[str] = []
    for item in value:
        bounded = _advisor_bounded_text(item, max_chars)
        if bounded is None:
            return None
        selected.append(bounded)
    return selected


def _advisor_bounded_text_refs(value: Any, *, max_items: int = 64) -> list[str] | None:
    del max_items
    if not isinstance(value, (list, tuple)):
        return None
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        bounded = _advisor_bounded_text(item, 160)
        if bounded is None:
            return None
        if bounded not in refs:
            refs.append(bounded)
    return refs


def _advisor_observation_refs(value: Any, *, max_items: int = 128) -> list[str] | None:
    del max_items
    if not isinstance(value, (list, tuple)):
        return None
    refs: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        try:
            ref = str(UUID(item))
        except ValueError:
            return None
        if ref not in refs:
            refs.append(ref)
    return refs


def _binary_evidence_summary(
    observation: EvidenceObservation,
    metadata: Mapping[str, Any] | None,
    *,
    observed_size: int | None = None,
) -> Mapping[str, Any]:
    mime_type = metadata.get("mime_type") if metadata is not None else None
    byte_size = metadata.get("byte_size") if metadata is not None else observed_size
    summary: dict[str, Any] = {
        "representation": "binary_or_non_json",
        "object_type": observation.object_type[:128],
        "sha256": observation.sha256,
    }
    if isinstance(mime_type, str) and len(mime_type) <= 128:
        summary["mime_type"] = mime_type
    if isinstance(byte_size, int) and not isinstance(byte_size, bool) and byte_size >= 0:
        summary["byte_size"] = byte_size
    return summary


class InvestigationContextTooLarge(ValueError):
    code = "investigation_context_too_large"

    def __init__(
        self,
        actual_bytes: int,
        max_bytes: int = INVESTIGATION_MAX_CONTEXT_BYTES,
        *,
        dominant_sections: tuple[str, ...] = (),
    ) -> None:
        self.actual_bytes = actual_bytes
        self.max_bytes = max_bytes
        self.dominant_sections = dominant_sections
        super().__init__(f"{self.code}: {actual_bytes} bytes exceeds {max_bytes} bytes")


def ensure_investigation_context_size(context_bytes: bytes) -> None:
    if len(context_bytes) > INVESTIGATION_MAX_CONTEXT_BYTES:
        raise InvestigationContextTooLarge(len(context_bytes), INVESTIGATION_MAX_CONTEXT_BYTES)


class InvestigationReadPort(Protocol):
    async def get_evidence(self, observation_id: UUID) -> Mapping[str, Any] | None: ...

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...]
    ) -> Sequence[Mapping[str, Any]]: ...

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None: ...

    async def graph_neighbors(
        self, object_type: str, object_id: str
    ) -> Sequence[Mapping[str, Any]]: ...


class SampleFeaturePackPort(Protocol):
    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None: ...


class InvestigationGraphReadPort(Protocol):
    async def neighbors(self, object_type: str, object_id: str) -> Sequence[Mapping[str, Any]]: ...


class RepositoryInvestigationGraphReadPort:
    """Build read-only graph edges from persisted findings and sample associations."""

    def __init__(
        self,
        *,
        investigation_id: UUID,
        findings: FindingRepository,
        associations: SampleAssociationRepository,
    ) -> None:
        self._investigation_id = investigation_id
        self._findings = findings
        self._associations = associations

    async def neighbors(self, object_type: str, object_id: str) -> Sequence[Mapping[str, Any]]:
        edges: list[Mapping[str, Any]] = []
        if object_type in {"sample", "hash"}:
            for association in await self._associations.list_for_investigation(
                self._investigation_id
            ):
                if association.sample_sha256 == object_id:
                    edges.extend(
                        {
                            "source_type": "sample",
                            "source_id": association.sample_sha256,
                            "relationship": "related_sample",
                            "target_type": "sample",
                            "target_id": related,
                            "qualification": association.qualification.value,
                        }
                        for related in association.related_sample_sha256
                    )
                elif object_id in association.related_sample_sha256:
                    edges.append(
                        {
                            "source_type": "sample",
                            "source_id": association.sample_sha256,
                            "relationship": "related_sample",
                            "target_type": "sample",
                            "target_id": object_id,
                            "qualification": association.qualification.value,
                        }
                    )

        findings = await self._findings.list_for_investigation(self._investigation_id)
        for finding in findings:
            if object_type in {"finding", "investigation_finding"} and str(finding.id) == object_id:
                edges.extend(self._finding_edges(finding))
            elif object_type in {"evidence", "observation"} and any(
                str(ref) == object_id for ref in finding.observation_refs
            ):
                edges.append(self._finding_edge(finding, "evidence", object_id))
            elif object_type == "claim" and object_id in finding.claim_refs:
                edges.append(self._finding_edge(finding, "claim", object_id))
        return tuple(edges[:64])

    @staticmethod
    def _finding_edges(finding: InvestigationFinding) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            RepositoryInvestigationGraphReadPort._finding_edge(finding, node_type, node_id)
            for node_type, node_ids in (
                ("evidence", tuple(str(item) for item in finding.observation_refs)),
                ("claim", finding.claim_refs),
            )
            for node_id in node_ids
        )

    @staticmethod
    def _finding_edge(
        finding: InvestigationFinding, target_type: str, target_id: str
    ) -> Mapping[str, Any]:
        return {
            "source_type": "finding",
            "source_id": str(finding.id),
            "relationship": f"{finding.assessment.value}_references",
            "target_type": target_type,
            "target_id": target_id,
            "finding_kind": finding.kind.value,
            "summary": finding.statement,
        }


class RepositoryInvestigationReadPort:
    """Join persisted observations/claims with persisted feature and edge readers."""

    def __init__(
        self,
        *,
        investigation_id: UUID,
        observations: EvidenceObservationRepository,
        article: ArticleSnapshotPort,
        feature_packs: SampleFeaturePackPort,
        graph: InvestigationGraphReadPort,
        evidence_summary: EvidenceSummaryPort | None = None,
    ) -> None:
        self._investigation_id = investigation_id
        self._observations = observations
        self._article = article
        self._feature_packs = feature_packs
        self._graph = graph
        self._evidence_summary = evidence_summary

    async def get_evidence(self, observation_id: UUID) -> Mapping[str, Any] | None:
        observation = await self._observations.get(observation_id)
        if observation is None or observation.investigation_id != self._investigation_id:
            return None
        summary = (
            await self._evidence_summary.summary(observation) if self._evidence_summary else None
        )
        payload: dict[str, Any] = {
            "evidence_id": str(observation.id),
            "kind": observation.kind.value,
            "sha256": observation.sha256,
            "source": observation.source,
            "source_version": observation.source_version,
            "object_type": observation.object_type,
            "object_id": observation.object_id,
            "limitations": list(observation.limitations),
        }
        if summary is not None:
            payload["summary"] = summary
        return payload

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...]
    ) -> Sequence[Mapping[str, Any]]:
        if investigation_id != self._investigation_id:
            return ()
        return tuple(
            claim.model_dump(mode="json")
            for claim in await self._article.get_claims(self._investigation_id, claim_ids)
        )

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None:
        return await self._feature_packs.get_feature_pack(sample_sha256)

    async def graph_neighbors(
        self, object_type: str, object_id: str
    ) -> Sequence[Mapping[str, Any]]:
        return await self._graph.neighbors(object_type, object_id)


class ResearchContextBuilder:
    def __init__(
        self,
        *,
        article_snapshots: ArticleSnapshotPort,
        observations: EvidenceObservationRepository,
        findings: FindingRepository,
        registry: ToolRegistry | None = None,
        evidence_summaries: EvidenceSummaryPort | None = None,
        invocations: ToolInvocationRepository | None = None,
        opinions: AnalystOpinionRepository | None = None,
        rule_corpus_inventory: RuleCorpusContextInventoryPort | None = None,
    ) -> None:
        self.article_snapshots = article_snapshots
        self._observations = observations
        self._findings = findings
        self._registry = registry
        self._evidence_summaries = evidence_summaries
        self._invocations = invocations
        self._opinions = opinions
        self._rule_corpus_inventory = rule_corpus_inventory

    async def build(
        self,
        *,
        investigation_id: UUID,
        synthesis_artifact_id: UUID,
        state: InvestigationWorkingState,
        tool_context: ToolExecutionContext,
        budget_remaining: Mapping[str, int | None],
        registry: ToolRegistry | None = None,
        blocked_capabilities: tuple[str, ...] = (),
    ) -> InvestigationContextV1:
        if (
            state.investigation_id != investigation_id
            or tool_context.investigation_id != investigation_id
        ):
            raise ValueError("Research context identity does not match its investigation")
        snapshot = await self.article_snapshots.resolve(synthesis_artifact_id)
        observation_source_page = await self._page_source(
            self._observations,
            investigation_id=investigation_id,
            section="evidence_observations",
            cursor=None,
            limit=MAX_CONTEXT_SOURCE_PAGE,
            descending=True,
            position=lambda item: (item.observed_at, item.id),
        )
        observations = tuple(reversed(observation_source_page.items))
        finding_source_page = await self._page_source(
            self._findings,
            investigation_id=investigation_id,
            section="findings",
            cursor=None,
            limit=MAX_CONTEXT_SOURCE_PAGE,
            descending=True,
            position=lambda item: (item.created_at, item.id),
        )
        findings = finding_source_page.items
        all_observation_ids = {item.id for item in observations}
        all_finding_ids = {item.id for item in findings}
        # Every Bridge turn is an independent Temporary Chat. Include the newest
        # bounded evidence on every turn so the prompt remains self-contained.
        evidence_summary = await self._evidence_context(tuple(reversed(observations)))
        invocation_source_page = (
            await self._page_source(
                self._invocations,
                investigation_id=investigation_id,
                section="tool_coverage",
                cursor=None,
                limit=MAX_CONTEXT_SOURCE_PAGE,
                descending=True,
                position=lambda item: (item.created_at, item.id),
            )
            if self._invocations is not None
            else None
        )
        tool_invocations = (
            tuple(reversed(invocation_source_page.items))
            if invocation_source_page is not None
            else ()
        )
        tool_coverage, tool_coverage_truncated = await self._tool_coverage(
            tool_invocations, observations
        )
        sample_deltas, sample_deltas_truncated, sample_deltas_total = await self._sample_index(
            investigation_id, tool_invocations, observations
        )
        latest_tool_results = await self._latest_tool_results(tool_invocations, observations)
        all_blocked = await blocked_capabilities_from_history(
            investigation_id=investigation_id,
            invocations=tool_invocations,
            observations=observations,
            additional=(
                *blocked_capabilities,
                *(
                    tuple(tool_context.available_capabilities)
                    if invocation_source_page is not None and invocation_source_page.has_more
                    else ()
                ),
            ),
        )
        finding_summary, findings_truncated = self._finding_context(
            findings,
            observation_ids=all_observation_ids,
        )
        evidence_index, evidence_index_truncated = self._evidence_index(observations)
        current_opinion = (
            await self._opinions.get(investigation_opinion_id(investigation_id))
            if self._opinions is not None
            else None
        )
        opinion_notes, opinion_notes_truncated = self._opinion_note_index(
            current_opinion,
            finding_ids=all_finding_ids,
            observation_ids=all_observation_ids,
        )
        rule_corpus_deltas = await self._rule_corpus_inventory_context(
            investigation_id, observations
        )
        availability_context = replace(tool_context, blocked_capabilities=all_blocked)
        selected_registry = registry or self._registry
        tool_availability = (
            project_tool_availability(
                selected_registry,
                availability_context,
            )
            if selected_registry is not None
            else None
        )
        tools = tuple(
            ToolSummaryV1(
                name=item.name,
                schema_version=item.schema_version,
                effect=item.effect,
                arguments_schema=thaw_json(item.input_schema),
            )
            for item in (
                tool_availability.available[:MAX_CONTEXT_TOOLS]
                if tool_availability is not None
                else ()
            )
        )
        unavailable_tool_diagnostics = (
            tool_availability.unavailable_diagnostics if tool_availability is not None else ()
        )
        unavailable_tools = unavailable_tool_diagnostics[:MAX_CONTEXT_TOOLS]
        hypothesis_payload = tuple(
            self._hypothesis_payload(
                item,
                observation_ids=all_observation_ids,
                finding_ids=all_finding_ids,
            )
            for item in state.hypotheses[:MAX_CONTEXT_HYPOTHESES]
        )
        indicator_inventory = self._indicator_inventory(snapshot)
        pivot_observations = tuple(
            item
            for item in observations
            if item.kind is EvidenceKind.VT_RELATIONSHIP
            or item.source == "sample_similarity"
            or item.source.startswith("network.capture.")
        )
        pivot_observations_truncated = len(pivot_observations) > MAX_PIVOT_SOURCE_OBSERVATIONS
        pivot_observations = pivot_observations[-MAX_PIVOT_SOURCE_OBSERVATIONS:]
        pivot_payloads: dict[str, Mapping[str, Any] | None] = {}
        if self._evidence_summaries is not None:
            for observation in pivot_observations:
                summary = await self._evidence_summaries.summary(observation)
                pivot_payloads[str(observation.id)] = (
                    summary if isinstance(summary, Mapping) else None
                )
        discovered_inventory = discovered_indicator_values(pivot_observations, pivot_payloads)
        available_pivots, pivots_truncated = available_investigation_pivots(
            inventory=indicator_inventory.items,
            discovered_inventory=discovered_inventory,
            tools=tools,
            tool_coverage=tool_coverage,
            invocations=tool_invocations,
            observations=observations,
        )
        available_pivots_truncated = pivots_truncated or pivot_observations_truncated
        user_requests = self._user_requests_from_actions(state.recent_actions)
        all_previous_actions = tuple(
            {"summary": _truncate(_safe_untrusted(action), 300)}
            for action in state.recent_actions
            if not action.startswith(USER_REQUEST_ACTION_PREFIX)
        )
        previous_actions = all_previous_actions[-8:]
        truncation_counts: dict[str, int] = {}
        if observation_source_page.total > len(evidence_summary):
            truncation_counts["new_evidence"] = observation_source_page.total - len(
                evidence_summary
            )
        if observation_source_page.total > len(evidence_index):
            truncation_counts["evidence_index"] = observation_source_page.total - len(
                evidence_index
            )
        if finding_source_page.total > len(finding_summary):
            truncation_counts["findings"] = finding_source_page.total - len(finding_summary)
        opinion_note_count = len(current_opinion.observations) if current_opinion else 0
        if opinion_note_count > len(opinion_notes):
            truncation_counts["opinion_notes"] = opinion_note_count - len(opinion_notes)
        invocation_total = invocation_source_page.total if invocation_source_page is not None else 0
        if invocation_total > len(tool_coverage):
            truncation_counts["tool_coverage"] = invocation_total - len(tool_coverage)
        if len(state.hypotheses) > len(hypothesis_payload):
            truncation_counts["hypotheses"] = len(state.hypotheses) - len(hypothesis_payload)
        if sample_deltas_total > len(sample_deltas):
            truncation_counts["sample_deltas"] = sample_deltas_total - len(sample_deltas)
        full_article = self._full_article(snapshot)
        if len(snapshot.article.publication_anchors) > len(full_article.publication_anchors):
            truncation_counts["publication_anchors"] = len(
                snapshot.article.publication_anchors
            ) - len(full_article.publication_anchors)
        for section_name, truncated, source_count, retained_count in (
            (
                "article_claims",
                full_article.claims_truncated,
                len(snapshot.article.claims),
                len(full_article.claims),
            ),
            (
                "documented_samples",
                full_article.documented_samples_truncated,
                len(snapshot.article.documented_samples),
                len(full_article.documented_samples),
            ),
            (
                "known_chain",
                full_article.known_chain_truncated,
                len(snapshot.article.known_chain),
                len(full_article.known_chain),
            ),
            (
                "open_questions",
                full_article.open_questions_truncated,
                len(snapshot.article.open_questions),
                len(full_article.open_questions),
            ),
        ):
            if truncated:
                truncation_counts[section_name] = max(0, source_count - retained_count)
        article_long_texts = self._article_long_text_count(snapshot.article)
        if article_long_texts:
            truncation_counts["article_long_texts"] = article_long_texts
        if len(all_previous_actions) > len(previous_actions):
            truncation_counts["previous_actions"] = len(all_previous_actions) - len(
                previous_actions
            )
        investigation_context = ContextInvestigationV1(
            agenda=tuple(
                _truncate(_safe_untrusted(item.question), 500) for item in state.agenda.items
            ),
            hypotheses=hypothesis_payload,
            hypotheses_truncated=(len(state.hypotheses) > len(hypothesis_payload) or None),
            new_evidence=evidence_summary,
            new_evidence_truncated=len(observations) > len(evidence_summary) or None,
            evidence_index=evidence_index,
            evidence_index_truncated=evidence_index_truncated,
            findings=finding_summary,
            findings_truncated=findings_truncated,
            opinion_notes=opinion_notes,
            opinion_notes_truncated=opinion_notes_truncated,
            truncation_counts=truncation_counts,
            pagination_manifest=(
                self._source_manifest_entry(
                    section="new_evidence",
                    page=observation_source_page,
                    selected=len(evidence_summary),
                    selected_items=evidence_summary,
                    investigation_id=investigation_id,
                ),
                self._source_manifest_entry(
                    section="evidence_index",
                    page=observation_source_page,
                    selected=len(evidence_index),
                    selected_items=evidence_index,
                    investigation_id=investigation_id,
                ),
                self._source_manifest_entry(
                    section="findings",
                    page=finding_source_page,
                    selected=len(finding_summary),
                    selected_items=finding_summary,
                    investigation_id=investigation_id,
                ),
                *(
                    (
                        self._source_manifest_entry(
                            section="tool_coverage",
                            page=invocation_source_page,
                            selected=len(tool_coverage),
                            selected_items=tool_coverage,
                            investigation_id=investigation_id,
                        ),
                    )
                    if invocation_source_page is not None
                    else ()
                ),
                self._unbounded_manifest_entry(
                    section="opinion_notes",
                    investigation_id=investigation_id,
                    total=opinion_note_count,
                    selected=len(opinion_notes),
                    items=opinion_notes,
                    source_ref=f"investigation:{investigation_id}:opinion_notes:materialized",
                ),
                self._unbounded_manifest_entry(
                    section="sample_deltas",
                    investigation_id=investigation_id,
                    total=sample_deltas_total,
                    selected=len(sample_deltas),
                    items=sample_deltas,
                    source_ref=f"investigation:{investigation_id}:sample_inventory:materialized",
                ),
            ),
            sample_deltas=sample_deltas,
            rule_deltas=rule_corpus_deltas,
            previous_actions=previous_actions[-8:],
            previous_actions_truncated=(len(all_previous_actions) > len(previous_actions) or None),
            user_requests=(),
            tool_coverage=tool_coverage,
            tool_coverage_truncated=tool_coverage_truncated,
            latest_tool_results=latest_tool_results,
            sample_deltas_truncated=sample_deltas_truncated,
            blocked_capabilities=tuple(item[:128] for item in all_blocked[:64]),
            budget_remaining=_safe_budget_names(budget_remaining),
        )
        context = InvestigationContextV1.model_construct(
            view="full",
            investigation_id=investigation_id,
            source=snapshot.source,
            article=full_article,
            investigation=investigation_context,
            operator_requests=user_requests,
            indicator_inventory=indicator_inventory,
            available_pivots=available_pivots,
            available_pivots_truncated=available_pivots_truncated,
            available_tools=tools,
            unavailable_tools=unavailable_tools,
            unavailable_tools_truncated=(
                len(unavailable_tool_diagnostics) > len(unavailable_tools)
            ),
            advisor_guidance=AdvisorGuidanceV1.model_validate(
                await build_advisor_guidance(
                    invocations=tool_invocations,
                    observations=observations,
                    evidence_summaries=self._evidence_summaries,
                    available_tools=tuple(item.name for item in tools),
                    checkpoint_tool_threshold=get_settings().operator_advisor_checkpoint_tool_threshold,
                    g2_bridge_turns_remaining=(
                        tool_context.budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                        if tool_context.envelope is not None
                        else None
                    ),
                    pivot_observation_ids=tuple(item.id for item in pivot_observations),
                    enabled=state.execution_mode is InvestigationExecutionMode.OPERATOR_LED,
                )
            ),
            current_opinion=self._current_opinion_context(current_opinion),
        )
        context = self._reduce_context_to_fit(
            context,
            observations=observations,
            findings=findings,
            opinion=current_opinion,
        )
        context = self._attach_materialized_omission_manifest(
            context,
            snapshot=snapshot,
            state=state,
        )
        context = self._reconcile_pagination_manifest(context)
        validated = InvestigationContextV1.model_validate(
            context.model_dump(mode="python", by_alias=True)
        )
        return validated.model_copy(update={"current_opinion": context.current_opinion})

    @staticmethod
    def _reconcile_pagination_manifest(
        context: InvestigationContextV1,
    ) -> InvestigationContextV1:
        article = context.article
        section_values = {
            "article_claims": article.claims if article is not None else (),
            "documented_samples": article.documented_samples if article is not None else (),
            "known_chain": article.known_chain if article is not None else (),
            "open_questions": article.open_questions if article is not None else (),
            "publication_anchors": article.publication_anchors if article is not None else (),
            "article_long_texts": (),
            "new_evidence": context.investigation.new_evidence,
            "evidence_index": context.investigation.evidence_index,
            "findings": context.investigation.findings,
            "tool_coverage": context.investigation.tool_coverage,
            "hypotheses": context.investigation.hypotheses,
            "previous_actions": context.investigation.previous_actions,
            "opinion_notes": context.investigation.opinion_notes,
            "sample_deltas": context.investigation.sample_deltas,
        }
        reconciled = []
        for entry in context.investigation.pagination_manifest:
            selected_items = section_values.get(entry.section, ())
            selected = len(selected_items)
            omitted = entry.total - selected
            if entry.cursor_authentication == "unauthenticated_position":
                paginated = min(
                    entry.omission_reasons.get(ContextOmissionReason.PAGINATION, 0),
                    omitted,
                )
                prior_not_selected = entry.omission_reasons.get(
                    ContextOmissionReason.NOT_SELECTED, 0
                )
                not_selected = min(prior_not_selected, omitted - paginated)
                reasons = {
                    ContextOmissionReason.BUDGET: omitted - not_selected - paginated,
                    ContextOmissionReason.PAGINATION: paginated,
                    ContextOmissionReason.NOT_SELECTED: not_selected,
                }
                reasons = {reason: count for reason, count in reasons.items() if count}
            elif entry.source_bounded:
                not_selected = min(
                    entry.omission_reasons.get(ContextOmissionReason.NOT_SELECTED, 0),
                    omitted,
                )
                reasons = {
                    ContextOmissionReason.BUDGET: omitted - not_selected,
                    ContextOmissionReason.NOT_SELECTED: not_selected,
                }
                reasons = {reason: count for reason, count in reasons.items() if count}
            else:
                reasons = {ContextOmissionReason.SOURCE_NOT_PAGINATED: omitted} if omitted else {}
            omission_summary = {
                "section": entry.section,
                "total": entry.total,
                "selected": selected,
                "omitted": omitted,
                "source_bounded": entry.source_bounded,
                "omission_reasons": reasons,
                "unbounded_reason": entry.unbounded_reason,
            }
            previous_page_sha256 = "0" * 64
            if entry.cursor is not None:
                decoded = decode_keyset_cursor(
                    entry.cursor,
                    section=entry.section,
                    investigation_id=context.investigation_id,
                    descending=entry.descending,
                    context_sha256=entry.context_sha256,
                )
                if decoded is not None:
                    previous_page_sha256 = decoded["previous_page_sha256"]
            page_sha256 = canonical_page_sha256(
                section=entry.section,
                investigation_id=context.investigation_id,
                context_sha256=entry.context_sha256,
                cursor=entry.cursor,
                pivot=entry.pivot,
                descending=entry.descending,
                limit=entry.limit or 1,
                total=entry.total,
                has_more=entry.has_more,
                items=selected_items,
                previous_page_sha256=previous_page_sha256,
                omission_manifest=omission_summary,
            )
            source_ref = (
                source_reference(
                    section=entry.section,
                    investigation_id=context.investigation_id,
                    cursor=entry.cursor,
                    page_sha256=page_sha256,
                    limit=entry.limit or 1,
                )
                if entry.cursor_authentication == "unauthenticated_position"
                else entry.source_ref
            )
            reconciled.append(
                entry.model_copy(
                    update={
                        "selected": selected,
                        "omitted": omitted,
                        "omission_reasons": reasons,
                        "page_sha256": page_sha256,
                        "source_ref": source_ref,
                    }
                )
            )
        return context.model_copy(
            update={
                "investigation": context.investigation.model_copy(
                    update={"pagination_manifest": tuple(reconciled)}
                )
            }
        )

    def _attach_materialized_omission_manifest(
        self,
        context: InvestigationContextV1,
        *,
        snapshot: ArticleSnapshot,
        state: InvestigationWorkingState,
    ) -> InvestigationContextV1:
        if context.article is None:
            return context
        article = snapshot.article
        projected_article = context.article
        artifact_id = getattr(snapshot, "synthesis_artifact_id", None)
        artifact_sha256 = getattr(snapshot, "synthesis_sha256", None)
        if artifact_id is not None and artifact_sha256 is not None:
            article_source_ref = f"artifact:{artifact_id}:{artifact_sha256}"
        else:
            fallback_article_sha256 = canonical_page_sha256(
                section="article_snapshot",
                investigation_id=context.investigation_id,
                items=article,
                previous_page_sha256="0" * 64,
            )
            article_source_ref = (
                f"materialized-article:{context.investigation_id}:sha256={fallback_article_sha256}"
            )
        candidates = (
            (
                "article_claims",
                len(article.claims),
                projected_article.claims,
                f"{article_source_ref}:claims",
            ),
            (
                "documented_samples",
                len(article.documented_samples),
                projected_article.documented_samples,
                f"{article_source_ref}:samples",
            ),
            (
                "known_chain",
                len(article.known_chain),
                projected_article.known_chain,
                f"{article_source_ref}:chain",
            ),
            (
                "open_questions",
                len(article.open_questions),
                projected_article.open_questions,
                f"{article_source_ref}:questions",
            ),
            (
                "publication_anchors",
                len(article.publication_anchors),
                projected_article.publication_anchors,
                f"{article_source_ref}:anchors",
            ),
            (
                "article_long_texts",
                self._article_long_text_section_count(article),
                (),
                f"{article_source_ref}:long_texts",
            ),
            (
                "hypotheses",
                len(state.hypotheses),
                context.investigation.hypotheses,
                f"working-state:{state.sha256}:hypotheses",
            ),
            (
                "previous_actions",
                len(context.investigation.previous_actions)
                + context.investigation.truncation_counts.get("previous_actions", 0),
                context.investigation.previous_actions,
                f"working-state:{state.sha256}:previous-actions",
            ),
        )
        existing_sections = {entry.section for entry in context.investigation.pagination_manifest}
        additions = []
        for section, total, items, source_ref in candidates:
            selected = len(items)
            if section in existing_sections or total <= selected:
                continue
            additions.append(
                self._unbounded_manifest_entry(
                    section=section,
                    investigation_id=context.investigation_id,
                    total=total,
                    selected=selected,
                    items=items,
                    source_ref=source_ref,
                    omission_reason=ContextOmissionReason.NOT_SELECTED,
                    unbounded_reason=(
                        "The versioned artifact or working-state projection is materialized "
                        "without keyset access"
                    ),
                )
            )
        if not additions:
            return context
        return context.model_copy(
            update={
                "investigation": context.investigation.model_copy(
                    update={
                        "pagination_manifest": (
                            *context.investigation.pagination_manifest,
                            *additions,
                        )
                    }
                )
            }
        )

    @staticmethod
    async def _page_source(
        repository: Any,
        *,
        investigation_id: UUID,
        section: str,
        cursor: str | None,
        limit: int,
        descending: bool,
        position: Any,
        context_sha256: str | None = None,
        latest_only: bool = False,
    ) -> KeysetPageV1[Any]:
        page_method = getattr(repository, "page_for_investigation", None)
        if callable(page_method):
            kwargs: dict[str, Any] = {
                "cursor": cursor,
                "limit": limit,
                "descending": descending,
                "section": section,
            }
            if latest_only:
                kwargs["latest_only"] = True
            return await page_method(investigation_id, **kwargs)

        list_method = getattr(repository, "list_for_investigation", None)
        if not callable(list_method):
            raise ValueError("Repository supports neither keyset pages nor materialized lists")
        # Compatibility fallback: this materializes the full source and is not source-bounded.
        items = tuple(await list_method(investigation_id))
        if latest_only:
            superseded = {
                getattr(item, "supersedes", None)
                for item in items
                if getattr(item, "supersedes", None) is not None
            }
            items = tuple(item for item in items if item.id not in superseded)
        ordered = sorted(items, key=position, reverse=descending)
        if cursor is not None:
            decoded = decode_keyset_cursor(
                cursor,
                section=section,
                investigation_id=investigation_id,
                descending=descending,
                context_sha256=context_sha256,
            )
            if decoded is not None:
                try:
                    pivot = (datetime.fromisoformat(decoded["sort_key"]), UUID(decoded["item_id"]))
                except (TypeError, ValueError):
                    raise ValueError("Context keyset cursor is invalid") from None
                ordered = [
                    item
                    for item in ordered
                    if (position(item) < pivot if descending else position(item) > pivot)
                ]
        return make_keyset_page(
            section=section,
            investigation_id=investigation_id,
            items=tuple(ordered[: limit + 1]),
            total=len(items),
            limit=limit,
            cursor=cursor,
            position=position,
            descending=descending,
            context_sha256=context_sha256,
            source_bounded=False,
            unbounded_reason=(
                "Repository lacks page_for_investigation; the fallback materializes the full list"
            ),
        )

    @staticmethod
    def _source_manifest_entry(
        *,
        section: str,
        page: Any,
        selected: int,
        selected_items: Sequence[Any],
        investigation_id: UUID,
    ) -> ContextOmissionManifestEntryV1:
        source_selected = len(page.items)
        if selected > source_selected:
            raise ValueError("Context selection cannot exceed the bounded source page")
        if selected != len(selected_items):
            raise ValueError("Context selection count must match its projected items")
        pivot = dict(page.pivot) if page.pivot is not None else None
        if pivot is not None:
            pivot["section"] = section
        omitted_for_pagination = page.total - source_selected
        omitted_not_selected = source_selected - selected
        reasons: dict[ContextOmissionReason, int] = {}
        if omitted_for_pagination:
            reasons[ContextOmissionReason.PAGINATION] = omitted_for_pagination
        if omitted_not_selected:
            reasons[ContextOmissionReason.NOT_SELECTED] = omitted_not_selected
        previous_page_sha256 = "0" * 64
        if page.cursor is not None:
            decoded = decode_keyset_cursor(
                page.cursor,
                section=section,
                investigation_id=investigation_id,
                descending=page.descending,
                context_sha256=page.context_sha256,
            )
            if decoded is not None:
                previous_page_sha256 = decoded["previous_page_sha256"]
        omission_summary = {
            "section": section,
            "total": page.total,
            "selected": selected,
            "omitted": page.total - selected,
            "source_bounded": page.source_bounded,
            "omission_reasons": reasons,
            "unbounded_reason": page.unbounded_reason,
        }
        page_sha256 = canonical_page_sha256(
            section=section,
            investigation_id=investigation_id,
            context_sha256=page.context_sha256,
            cursor=page.cursor,
            pivot=pivot,
            descending=page.descending,
            limit=page.limit,
            total=page.total,
            has_more=page.has_more,
            items=selected_items,
            previous_page_sha256=previous_page_sha256,
            omission_manifest=omission_summary,
        )
        source_ref = source_reference(
            section=section,
            investigation_id=investigation_id,
            cursor=page.cursor,
            page_sha256=page_sha256,
            limit=page.limit,
        )
        entry = ContextOmissionManifestEntryV1(
            section=section,
            total=page.total,
            selected=selected,
            omitted=page.total - selected,
            source_bounded=page.source_bounded,
            source_ref=source_ref,
            page_sha256=page_sha256,
            omission_reasons=reasons,
            unbounded_reason=page.unbounded_reason,
            context_sha256=page.context_sha256,
            cursor=page.cursor,
            pivot=pivot,
            descending=page.descending,
            limit=page.limit,
            has_more=page.has_more,
            cursor_authentication="unauthenticated_position",
        )
        assert_manifest_consistency(
            section=section,
            investigation_id=investigation_id,
            items=selected_items,
            previous_page_sha256=previous_page_sha256,
            context_sha256=page.context_sha256,
            cursor=page.cursor,
            pivot=pivot,
            descending=page.descending,
            limit=page.limit,
            total=page.total,
            has_more=page.has_more,
            source_bounded=page.source_bounded,
            omission_reasons=reasons,
            unbounded_reason=page.unbounded_reason,
            selected=selected,
            omitted=page.total - selected,
            claimed_page_sha256=entry.page_sha256,
            claimed_source_ref=entry.source_ref,
            manifest=entry,
        )
        return entry

    @staticmethod
    def _unbounded_manifest_entry(
        *,
        section: str,
        investigation_id: UUID,
        total: int,
        selected: int,
        items: Sequence[Any],
        source_ref: str,
        omission_reason: ContextOmissionReason = ContextOmissionReason.SOURCE_NOT_PAGINATED,
        unbounded_reason: str = (
            "Current aggregate repository exposes a materialized sequence without keyset paging"
        ),
        context_sha256: str | None = None,
        cursor: str | None = None,
        pivot: Mapping[str, str] | None = None,
        descending: bool = False,
        limit: int | None = None,
        has_more: bool = False,
        cursor_authentication: Literal["unauthenticated_position"] | None = None,
    ) -> ContextOmissionManifestEntryV1:
        omitted = total - selected
        if selected > total:
            raise ValueError("Selected and omitted counts must equal source total")
        reasons = {omission_reason: omitted} if omitted else {}
        omission_summary = {
            "section": section,
            "total": total,
            "selected": selected,
            "omitted": omitted,
            "source_bounded": False,
            "omission_reasons": reasons,
            "unbounded_reason": unbounded_reason,
        }
        previous_page_sha256 = "0" * 64
        if cursor is not None:
            decoded = decode_keyset_cursor(
                cursor,
                section=section,
                investigation_id=investigation_id,
                descending=descending,
                context_sha256=context_sha256,
            )
            if decoded is not None:
                previous_page_sha256 = decoded["previous_page_sha256"]
        page_sha256 = canonical_page_sha256(
            section=section,
            investigation_id=investigation_id,
            items=items,
            previous_page_sha256=previous_page_sha256,
            context_sha256=context_sha256,
            cursor=cursor,
            pivot=pivot,
            descending=descending,
            limit=limit or 1,
            total=total,
            has_more=has_more,
            omission_manifest=omission_summary,
        )
        if cursor_authentication == "unauthenticated_position":
            source_ref = source_reference(
                section=section,
                investigation_id=investigation_id,
                cursor=cursor,
                page_sha256=page_sha256,
                limit=limit or 1,
            )
        return ContextOmissionManifestEntryV1(
            section=section,
            total=total,
            selected=selected,
            omitted=omitted,
            source_bounded=False,
            source_ref=source_ref,
            page_sha256=page_sha256,
            omission_reasons=reasons,
            unbounded_reason=unbounded_reason,
            context_sha256=context_sha256,
            cursor=cursor,
            pivot=dict(pivot) if pivot is not None else None,
            descending=descending,
            limit=limit,
            has_more=has_more,
            cursor_authentication=cursor_authentication,
        )

    async def read_context_section(
        self,
        *,
        section: str,
        investigation_id: UUID,
        state: InvestigationWorkingState,
        offset: int,
        limit: int,
        synthesis_artifact_id: UUID | None = None,
    ) -> tuple[int, tuple[dict[str, Any], ...]]:
        if state.investigation_id != investigation_id:
            raise ValueError("Research context identity does not match its investigation")
        if section in {
            "new_evidence",
            "evidence_index",
            "findings",
            "finding_versions",
            "tool_coverage",
        }:
            page = await self.read_context_section_page(
                section=section,
                investigation_id=investigation_id,
                state=state,
                cursor=None,
                offset=offset,
                limit=limit,
            )
            return page.total, page.items
        observations = tuple(
            sorted(
                await self._observations.list_for_investigation(investigation_id),
                key=lambda item: (item.observed_at, str(item.id)),
                reverse=True,
            )
        )
        page = slice(offset, offset + limit)

        article_sections = {
            "article_claims",
            "article_long_texts",
            "documented_samples",
            "known_chain",
            "open_questions",
            "publication_anchors",
        }
        if section in article_sections:
            if synthesis_artifact_id is None:
                raise ValueError("Synthesis artifact identity is required for article sections")
            snapshot = await self.article_snapshots.resolve(synthesis_artifact_id)
            article = snapshot.article
            if section == "article_long_texts":
                long_texts: list[dict[str, str]] = []
                if len(article.short_summary) > MAX_ARTICLE_NARRATIVE_CHARS:
                    long_texts.append(
                        {
                            "kind": "article_summary",
                            "id": "summary",
                            "text": _safe_untrusted(_truncate(article.short_summary, 12_000)),
                        }
                    )
                long_texts.extend(
                    {
                        "kind": "claim",
                        "id": item.id,
                        "text": _safe_untrusted(_truncate(item.statement, 4_000)),
                    }
                    for item in article.claims
                    if len(item.statement) > 900
                )
                long_texts.extend(
                    {
                        "kind": "known_chain",
                        "id": str(index),
                        "text": _safe_untrusted(
                            _truncate(json.dumps(_sanitize(item), ensure_ascii=False), 4_000)
                        ),
                    }
                    for index, item in enumerate(_prioritize_context_rows(article.known_chain))
                    if len(json.dumps(_sanitize(item), ensure_ascii=False)) > 360
                )
                long_texts.extend(
                    {
                        "kind": "open_question",
                        "id": str(index),
                        "text": _safe_untrusted(_truncate(item, 4_000)),
                    }
                    for index, item in enumerate(article.open_questions)
                    if len(item) > 560
                )
                long_texts.extend(
                    {
                        "kind": "publication_anchor",
                        "id": item.anchor,
                        "text": _safe_untrusted(_truncate(item.excerpt, 160)),
                    }
                    for item in article.publication_anchors
                    if len(item.excerpt) > 96
                )
                return len(long_texts), tuple(long_texts[page])
            if section == "article_claims":
                claims = article.claims
                claim_items = tuple(
                    ArticleClaimV1(
                        id=claim.id,
                        statement=_safe_untrusted(_truncate(claim.statement, 4_000)),
                        source_refs=tuple(
                            ref
                            for ref in claim.source_refs[:8]
                            if len(ref) <= 256
                            and not _SECRET_KEY.search(ref)
                            and not _SIGNED_QUERY.search(ref)
                        ),
                    ).model_dump(mode="json")
                    for claim in claims[page]
                )
                return len(claims), claim_items
            if section == "documented_samples":
                sample_values = tuple(
                    sample
                    for sample in article.documented_samples
                    if isinstance(sample, str)
                    and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", sample)
                )
                return len(sample_values), tuple(
                    {"sample": sample} for sample in sample_values[page]
                )
            if section == "known_chain":
                chain_values = tuple(_prioritize_context_rows(article.known_chain))
                chain_items = tuple(
                    {
                        "summary": _safe_untrusted(
                            _truncate(json.dumps(_sanitize(chain_item), ensure_ascii=False), 4_000)
                        )
                    }
                    for chain_item in chain_values[page]
                )
                return len(chain_values), chain_items
            if section == "open_questions":
                question_values = article.open_questions
                return len(question_values), tuple(
                    {"question": _safe_untrusted(_truncate(question, 4_000))}
                    for question in question_values[page]
                )
            anchor_values = tuple(
                sorted(article.publication_anchors, key=lambda anchor: anchor.anchor)
            )
            anchor_items = tuple(
                PublicationAnchorV1(
                    anchor=anchor.anchor,
                    excerpt=_safe_untrusted(_truncate(anchor.excerpt, 96)),
                ).model_dump(mode="json")
                for anchor in anchor_values[page]
            )
            return len(anchor_values), anchor_items

        if section == "new_evidence":
            new_evidence_observations = observations[page]
            new_evidence_items = await self._evidence_context(new_evidence_observations)
            return len(observations), tuple(
                evidence_item.model_dump(mode="json") for evidence_item in new_evidence_items
            )
        if section == "evidence_index":
            evidence_index_observations = observations[page]
            evidence_index_items = tuple(
                self._evidence_index((observation,))[0][0]
                for observation in evidence_index_observations
            )
            return len(observations), tuple(
                evidence_item.model_dump(mode="json") for evidence_item in evidence_index_items
            )
        if section == "finding_versions":
            all_finding_values = tuple(
                await self._findings.list_for_investigation(investigation_id)
            )
            ordered_findings = tuple(self._order_findings(all_finding_values))
            selected_findings = ordered_findings[page]
            finding_rows, _ = self._finding_context(
                selected_findings,
                observation_ids={observation.id for observation in observations},
                max_items=limit,
                max_bytes=INVESTIGATION_MAX_CONTEXT_BYTES,
            )
            return len(ordered_findings), tuple(
                finding.model_dump(mode="json") for finding in finding_rows
            )
        if section == "findings":
            all_finding_values = tuple(
                await self._findings.list_for_investigation(investigation_id)
            )
            latest_findings = self._latest_finding_versions(all_finding_values)
            selected_findings = tuple(
                sorted(
                    latest_findings,
                    key=lambda finding: (finding.created_at, str(finding.id)),
                    reverse=True,
                )[page]
            )
            finding_rows, _ = self._finding_context(
                selected_findings,
                observation_ids={observation.id for observation in observations},
                max_items=limit,
                max_bytes=INVESTIGATION_MAX_CONTEXT_BYTES,
            )
            finding_by_id = {finding.finding_id: finding for finding in finding_rows}
            ordered_finding_rows = tuple(
                finding_by_id[finding.id]
                for finding in selected_findings
                if finding.id in finding_by_id
            )
            return len(latest_findings), tuple(
                finding.model_dump(mode="json") for finding in ordered_finding_rows
            )
        if section == "opinion_notes":
            current_opinion = (
                await self._opinions.get(investigation_opinion_id(investigation_id))
                if self._opinions is not None
                else None
            )
            all_finding_values = tuple(
                await self._findings.list_for_investigation(investigation_id)
            )
            opinion_notes = current_opinion.observations if current_opinion is not None else ()
            selected_notes = opinion_notes[page]
            note_rows, _ = self._opinion_note_index(
                current_opinion,
                finding_ids={finding.id for finding in all_finding_values},
                observation_ids={observation.id for observation in observations},
                ordered_notes=selected_notes,
                max_items=limit,
                max_bytes=INVESTIGATION_MAX_CONTEXT_BYTES,
            )
            return len(opinion_notes), tuple(note.model_dump(mode="json") for note in note_rows)
        if section == "tool_coverage":
            tool_invocations = (
                tuple(await self._invocations.list_for_investigation(investigation_id))
                if self._invocations is not None
                else ()
            )
            selected_tool_invocations = tuple(
                sorted(
                    tool_invocations,
                    key=lambda invocation: (invocation.created_at, invocation.id.hex),
                    reverse=True,
                )[page]
            )
            observation_by_id = {observation.id: observation for observation in observations}
            coverage_rows: list[ToolCoverageEntryV1] = []
            for tool_invocation in selected_tool_invocations:
                linked_observations = tuple(
                    observation_by_id[ref]
                    for ref in reversed(tool_invocation.response_refs[-4:])
                    if ref in observation_by_id
                )
                linked_observation = linked_observations[0] if linked_observations else None
                summary_facts: Mapping[str, Any] | str | None = None
                if linked_observation is not None and self._evidence_summaries is not None:
                    for candidate_observation in linked_observations:
                        summary_facts = await self._evidence_summaries.summary(
                            candidate_observation
                        )
                        if summary_facts is not None:
                            linked_observation = candidate_observation
                            break
                coverage_rows.append(
                    ToolCoverageEntryV1(
                        invocation_id=tool_invocation.id,
                        tool_name=_truncate(_safe_untrusted(tool_invocation.tool_name), 128),
                        parameter_key=_coverage_parameter_key(tool_invocation),
                        status=tool_invocation.status,
                        observation_id=(
                            linked_observation.id if linked_observation is not None else None
                        ),
                        summary=_tool_coverage_summary(
                            tool_invocation, linked_observation, summary_facts
                        ),
                    )
                )
            return len(tool_invocations), tuple(
                coverage.model_dump(mode="json") for coverage in coverage_rows
            )
        if section == "sample_deltas":
            sample_invocations = (
                tuple(await self._invocations.list_for_investigation(investigation_id))
                if self._invocations is not None
                else ()
            )
            sample_rows, _truncated, sample_total = await self._sample_index(
                investigation_id,
                sample_invocations,
                observations,
                offset=offset,
                limit=limit,
            )
            return sample_total, sample_rows
        if section == "hypotheses":
            all_finding_values = tuple(
                await self._findings.list_for_investigation(investigation_id)
            )
            hypothesis_rows = tuple(
                self._hypothesis_payload(
                    item,
                    observation_ids={observation.id for observation in observations},
                    finding_ids={finding.id for finding in all_finding_values},
                )
                for item in state.hypotheses
            )
            return len(hypothesis_rows), tuple(hypothesis_rows[page])
        if section == "rule_deltas":
            rule_rows = await self._rule_corpus_inventory_context(investigation_id, observations)
            return len(rule_rows), tuple(rule_rows[page])
        raise ValueError("Unsupported operator context section")

    async def read_context_section_page(
        self,
        *,
        section: str,
        investigation_id: UUID,
        state: InvestigationWorkingState,
        cursor: str | None,
        limit: int,
        offset: int = 0,
        context_sha256: str | None = None,
    ) -> KeysetPageV1[dict[str, Any]]:
        """Read a source-bounded context section and return its recoverable page CAS."""
        if state.investigation_id != investigation_id:
            raise ValueError("Research context identity does not match its investigation")
        if not 1 <= limit <= 20:
            raise ValueError("Context page limit must be between 1 and 20")
        if offset < 0 or (cursor is not None and offset != 0):
            raise ValueError("Context page offset and cursor are incompatible")
        if cursor is not None:
            decode_keyset_cursor(
                cursor,
                section=section,
                investigation_id=investigation_id,
                descending=(section != "finding_versions"),
                context_sha256=context_sha256,
            )
        if offset:
            remaining = offset
            seek_cursor: str | None = None
            while remaining:
                skipped = await self.read_context_section_page(
                    section=section,
                    investigation_id=investigation_id,
                    state=state,
                    cursor=seek_cursor,
                    limit=min(remaining, 20),
                    context_sha256=context_sha256,
                )
                if not skipped.items:
                    return skipped
                remaining -= len(skipped.items)
                seek_cursor = skipped.next_cursor
                if seek_cursor is None:
                    empty_after_last_page = skipped.model_copy(
                        update={"items": (), "has_more": False, "next_cursor": None}
                    )
                    return self._project_source_page(
                        section=section,
                        investigation_id=investigation_id,
                        cursor=skipped.cursor,
                        limit=limit,
                        source_page=empty_after_last_page,
                        items=(),
                        position=lambda _item: (datetime.min, UUID(int=0)),
                        descending=(section != "finding_versions"),
                        context_sha256=context_sha256,
                    )
            return await self.read_context_section_page(
                section=section,
                investigation_id=investigation_id,
                state=state,
                cursor=seek_cursor,
                limit=limit,
                context_sha256=context_sha256,
            )
        if section in {"new_evidence", "evidence_index"}:
            source_page = await self._page_source(
                self._observations,
                investigation_id=investigation_id,
                cursor=cursor,
                limit=limit,
                descending=True,
                section=section,
                position=lambda item: (item.observed_at, item.id),
                context_sha256=context_sha256,
            )
            if section == "new_evidence":
                serialized_items = []
                for observation in source_page.items:
                    summaries = await self._evidence_context((observation,))
                    serialized_items.extend(item.model_dump(mode="json") for item in summaries)
                items = tuple(serialized_items)
            else:
                items = tuple(
                    self._evidence_index((observation,))[0][0].model_dump(mode="json")
                    for observation in source_page.items
                )
            return self._project_source_page(
                section=section,
                investigation_id=investigation_id,
                cursor=cursor,
                limit=limit,
                source_page=source_page,
                items=items,
                position=lambda item: (item.observed_at, item.id),
                descending=True,
                context_sha256=context_sha256,
            )
        if section in {"findings", "finding_versions"}:
            source_page = await self._page_source(
                self._findings,
                investigation_id=investigation_id,
                cursor=cursor,
                limit=limit,
                descending=(section == "findings"),
                section=section,
                latest_only=(section == "findings"),
                position=lambda item: (item.created_at, item.id),
                context_sha256=context_sha256,
            )
            observation_ids = {
                ref for finding in source_page.items for ref in finding.observation_refs
            }
            finding_rows, _ = self._finding_context(
                source_page.items,
                observation_ids=observation_ids,
                max_items=limit,
                max_bytes=INVESTIGATION_MAX_CONTEXT_BYTES,
            )
            items = tuple(item.model_dump(mode="json") for item in finding_rows)
            return self._project_source_page(
                section=section,
                investigation_id=investigation_id,
                cursor=cursor,
                limit=limit,
                source_page=source_page,
                items=items,
                position=lambda item: (item.created_at, item.id),
                descending=(section == "findings"),
                context_sha256=context_sha256,
            )
        if section == "tool_coverage":
            if self._invocations is None:
                empty = make_keyset_page(
                    section=section,
                    investigation_id=investigation_id,
                    items=(),
                    total=0,
                    limit=limit,
                    cursor=cursor,
                    position=lambda item: (item.created_at, item.id),
                    descending=True,
                    context_sha256=context_sha256,
                )
                return empty
            source_page = await self._page_source(
                self._invocations,
                investigation_id=investigation_id,
                cursor=cursor,
                limit=limit,
                descending=True,
                section=section,
                position=lambda item: (item.created_at, item.id),
                context_sha256=context_sha256,
            )
            observations_by_id: dict[UUID, EvidenceObservation] = {}
            for invocation in source_page.items:
                for observation_id in invocation.response_refs[-4:]:
                    observation = await self._observations.get(observation_id)
                    if observation is not None:
                        observations_by_id[observation_id] = observation
            coverage_items, _ = await self._tool_coverage(
                tuple(reversed(source_page.items)), tuple(observations_by_id.values())
            )
            items = tuple(item.model_dump(mode="json") for item in reversed(coverage_items))
            return self._project_source_page(
                section=section,
                investigation_id=investigation_id,
                cursor=cursor,
                limit=limit,
                source_page=source_page,
                items=items,
                position=lambda item: (item.created_at, item.id),
                descending=True,
                context_sha256=context_sha256,
            )
        raise ValueError("Section does not use source-bounded keyset pagination")

    @staticmethod
    def _project_source_page(
        *,
        section: str,
        investigation_id: UUID,
        cursor: str | None,
        limit: int,
        source_page: Any,
        items: tuple[dict[str, Any], ...],
        position: Any,
        descending: bool,
        context_sha256: str | None,
    ):
        previous_page_sha256 = (
            decode_keyset_cursor(
                cursor,
                section=section,
                investigation_id=investigation_id,
                descending=descending,
                context_sha256=context_sha256,
            )["previous_page_sha256"]
            if cursor is not None
            else "0" * 64
        )
        if len(items) > len(source_page.items):
            raise ValueError("Projected context page cannot exceed its source page")
        omitted = source_page.total - len(items)
        omission_reasons = {
            ContextOmissionReason.PAGINATION: source_page.total - len(source_page.items),
            ContextOmissionReason.NOT_SELECTED: len(source_page.items) - len(items),
        }
        omission_reasons = {reason: count for reason, count in omission_reasons.items() if count}
        pivot = dict(source_page.pivot) if source_page.pivot is not None else None
        if pivot is not None:
            pivot["section"] = section
        omission_summary = {
            "section": section,
            "total": source_page.total,
            "selected": len(items),
            "omitted": omitted,
            "source_bounded": source_page.source_bounded,
            "omission_reasons": omission_reasons,
            "unbounded_reason": source_page.unbounded_reason,
        }
        page_sha256 = canonical_page_sha256(
            section=section,
            investigation_id=investigation_id,
            items=items,
            previous_page_sha256=previous_page_sha256,
            context_sha256=context_sha256,
            cursor=cursor,
            pivot=pivot,
            descending=descending,
            limit=limit,
            total=source_page.total,
            has_more=source_page.has_more,
            omission_manifest=omission_summary,
        )
        next_cursor = None
        if source_page.has_more and source_page.items:
            sort_key, item_id = position(source_page.items[-1])
            next_cursor = encode_keyset_cursor(
                section=section,
                investigation_id=investigation_id,
                sort_key=sort_key.isoformat(),
                item_id=item_id,
                previous_page_sha256=page_sha256,
                descending=descending,
                context_sha256=context_sha256,
            )
        final_source_ref = source_reference(
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            page_sha256=page_sha256,
            limit=limit,
        )
        return KeysetPageV1[dict[str, Any]](
            items=items,
            total=source_page.total,
            has_more=source_page.has_more,
            next_cursor=next_cursor,
            page_sha256=page_sha256,
            source_ref=final_source_ref,
            cursor=cursor,
            pivot=pivot,
            descending=descending,
            limit=limit,
            context_sha256=context_sha256,
            omission_manifest=(
                ContextOmissionManifestEntryV1(
                    section=section,
                    total=source_page.total,
                    selected=len(items),
                    omitted=omitted,
                    source_bounded=source_page.source_bounded,
                    source_ref=final_source_ref,
                    page_sha256=page_sha256,
                    omission_reasons=omission_reasons,
                    unbounded_reason=source_page.unbounded_reason,
                    context_sha256=context_sha256,
                    cursor=cursor,
                    pivot=pivot,
                    descending=descending,
                    limit=limit,
                    has_more=source_page.has_more,
                    cursor_authentication="unauthenticated_position",
                ),
            ),
            source_bounded=source_page.source_bounded,
            unbounded_reason=source_page.unbounded_reason,
        )

    def _reduce_context_to_fit(
        self,
        context: InvestigationContextV1,
        *,
        observations: tuple[EvidenceObservation, ...],
        findings: tuple[InvestigationFinding, ...],
        opinion: AnalystOpinionV1 | None,
    ) -> InvestigationContextV1:
        if len(context.canonical_bytes()) <= INVESTIGATION_MAX_CONTEXT_BYTES:
            return context

        latest_findings = self._latest_finding_versions(findings)
        all_notes = opinion.observations if opinion is not None else ()
        stages = (
            (150, 12, 150, 20, 150, 12, 96, 600, 128, 1024),
            (100, 8, 100, 12, 100, 8, 64, 400, 96, 800),
            (50, 4, 64, 8, 50, 4, 32, 240, 64, 600),
            (20, 2, 32, 4, 16, 2, 16, 128, 32, 400),
            (0, 1, 16, 1, 8, 1, 8, 64, 16, 256),
        )
        current = context
        original_counts = dict(context.investigation.truncation_counts)
        for (
            evidence_index_limit,
            new_evidence_limit,
            finding_limit,
            note_limit,
            coverage_limit,
            hypothesis_limit,
            rule_limit,
            text_limit,
            pivot_limit,
            advisor_guidance_bytes,
        ) in stages:
            current = self._apply_context_budget_stage(
                current,
                observations=observations,
                findings=latest_findings,
                superseded_findings=len(findings) - len(latest_findings),
                opinion=opinion,
                notes=all_notes,
                evidence_index_limit=evidence_index_limit,
                new_evidence_limit=new_evidence_limit,
                finding_limit=finding_limit,
                note_limit=note_limit,
                coverage_limit=coverage_limit,
                hypothesis_limit=hypothesis_limit,
                rule_limit=rule_limit,
                text_limit=text_limit,
                pivot_limit=pivot_limit,
                advisor_guidance_bytes=advisor_guidance_bytes,
                prior_counts=original_counts,
            )
            if len(current.canonical_bytes()) <= INVESTIGATION_MAX_CONTEXT_BYTES:
                return current

        size = len(current.canonical_bytes())
        raise InvestigationContextTooLarge(
            size,
            INVESTIGATION_MAX_CONTEXT_BYTES,
            dominant_sections=self._dominant_context_sections(current),
        )

    @staticmethod
    def _current_opinion_context(
        opinion: AnalystOpinionV1 | None,
    ) -> CurrentOpinionContextV1 | None:
        if opinion is None:
            return None
        opinion_id = getattr(opinion, "id", None)
        version = getattr(opinion, "version", None)
        content_sha256 = getattr(opinion, "content_sha256", None)
        if (
            not isinstance(opinion_id, UUID)
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
            or not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        ):
            return None
        return CurrentOpinionContextV1(
            opinion_id=opinion_id,
            version=version,
            content_sha256=content_sha256,
        )

    def _apply_context_budget_stage(
        self,
        context: InvestigationContextV1,
        *,
        observations: tuple[EvidenceObservation, ...],
        findings: tuple[InvestigationFinding, ...],
        superseded_findings: int,
        opinion: AnalystOpinionV1 | None,
        notes: Sequence[Any],
        evidence_index_limit: int,
        new_evidence_limit: int,
        finding_limit: int,
        note_limit: int,
        coverage_limit: int,
        hypothesis_limit: int,
        rule_limit: int,
        text_limit: int,
        pivot_limit: int,
        advisor_guidance_bytes: int,
        prior_counts: Mapping[str, int],
    ) -> InvestigationContextV1:
        investigation = context.investigation
        ordered_observations = tuple(
            sorted(observations, key=lambda item: (item.observed_at, str(item.id)), reverse=True)
        )
        retained_observations = ordered_observations[:evidence_index_limit]
        evidence_rows = tuple(self._evidence_index((item,))[0][0] for item in retained_observations)
        new_evidence = investigation.new_evidence[:new_evidence_limit]

        selected_findings = tuple(
            sorted(
                findings,
                key=lambda item: (item.created_at, str(item.id)),
                reverse=True,
            )[:finding_limit]
        )
        finding_rows, _ = self._finding_context(
            selected_findings,
            observation_ids={item.id for item in observations},
        )

        note_order = tuple(notes[-note_limit:]) if note_limit else ()
        note_rows, _ = self._opinion_note_index(
            opinion,
            finding_ids={item.id for item in findings},
            observation_ids={item.id for item in observations},
            ordered_notes=note_order,
            max_items=note_limit,
            max_bytes=MAX_CONTEXT_OPINION_NOTE_BYTES,
        )
        coverage_rows = investigation.tool_coverage[-coverage_limit:] if coverage_limit else ()
        hypothesis_rows = investigation.hypotheses[:hypothesis_limit]
        rule_rows = investigation.rule_deltas[-rule_limit:] if rule_limit else ()
        action_rows = investigation.previous_actions[-8:]
        article, text_count = self._truncate_article(context.article, text_limit)
        investigation, extra_text_count = self._truncate_investigation_text(
            investigation,
            text_limit,
        )
        text_count += extra_text_count

        counts = dict(prior_counts)
        counts["evidence_index"] = max(
            counts.get("evidence_index", 0), len(observations) - len(evidence_rows)
        )
        counts["new_evidence"] = max(
            counts.get("new_evidence", 0), len(observations) - len(new_evidence)
        )
        counts["findings"] = max(counts.get("findings", 0), len(findings) - len(finding_rows))
        if superseded_findings:
            counts["findings_superseded"] = superseded_findings
        counts["opinion_notes"] = max(counts.get("opinion_notes", 0), len(notes) - len(note_rows))
        counts["tool_coverage"] = max(
            counts.get("tool_coverage", 0),
            len(investigation.tool_coverage) - len(coverage_rows),
        )
        counts["hypotheses"] = max(
            counts.get("hypotheses", 0), len(investigation.hypotheses) - len(hypothesis_rows)
        )
        counts["rule_deltas"] = max(
            counts.get("rule_deltas", 0), len(investigation.rule_deltas) - len(rule_rows)
        )
        if text_count:
            counts["long_texts"] = max(counts.get("long_texts", 0), text_count)

        updated_investigation = investigation.model_copy(
            update={
                "new_evidence": new_evidence,
                "new_evidence_truncated": len(observations) > len(new_evidence),
                "evidence_index": evidence_rows,
                "evidence_index_truncated": (
                    investigation.evidence_index_truncated or len(observations) > len(evidence_rows)
                ),
                "findings": finding_rows,
                "findings_truncated": (
                    investigation.findings_truncated
                    or superseded_findings > 0
                    or len(findings) > len(finding_rows)
                ),
                "opinion_notes": note_rows,
                "opinion_notes_truncated": (
                    investigation.opinion_notes_truncated or len(notes) > len(note_rows)
                ),
                "tool_coverage": coverage_rows,
                "tool_coverage_truncated": (
                    investigation.tool_coverage_truncated
                    or len(context.investigation.tool_coverage) > len(coverage_rows)
                ),
                "hypotheses": hypothesis_rows,
                "hypotheses_truncated": (
                    investigation.hypotheses_truncated is True
                    or len(investigation.hypotheses) > len(hypothesis_rows)
                ),
                "rule_deltas": rule_rows,
                "rule_deltas_truncated": (
                    investigation.rule_deltas_truncated is True
                    or len(investigation.rule_deltas) > len(rule_rows)
                ),
                "previous_actions": action_rows,
                "previous_actions_truncated": (
                    investigation.previous_actions_truncated is True
                    or len(investigation.previous_actions) > len(action_rows)
                ),
                "long_texts_truncated": (
                    investigation.long_texts_truncated is True or text_count > 0
                ),
                "truncation_counts": counts,
            }
        )
        advisor_guidance, _advisor_truncated = bound_advisor_guidance(
            context.advisor_guidance,
            max_bytes=advisor_guidance_bytes,
        )
        return context.model_copy(
            update={
                "article": article,
                "investigation": updated_investigation,
                "advisor_guidance": advisor_guidance,
                "available_pivots": context.available_pivots[:pivot_limit],
                "available_pivots_truncated": (
                    context.available_pivots_truncated
                    or len(context.available_pivots) > pivot_limit
                ),
            }
        )

    @staticmethod
    def _latest_finding_versions(
        findings: Sequence[InvestigationFinding],
    ) -> tuple[InvestigationFinding, ...]:
        superseded_ids = {item.supersedes for item in findings if item.supersedes is not None}
        return tuple(item for item in findings if item.id not in superseded_ids)

    @staticmethod
    def _truncate_article(
        article: ArticleContextV1 | None,
        maximum: int,
    ) -> tuple[ArticleContextV1 | None, int]:
        if article is None:
            return None, 0
        changed = 0
        short_summary = _truncate(article.short_summary, maximum)
        changed += short_summary != article.short_summary
        claim_rows = []
        for claim in article.claims:
            statement = _truncate(claim.statement, maximum)
            changed += statement != claim.statement
            claim_rows.append(claim.model_copy(update={"statement": statement}))
        known_chain_rows: list[dict[str, Any]] = []
        for chain_item in article.known_chain:
            value = dict(chain_item)
            summary = value.get("summary")
            if isinstance(summary, str):
                truncated = _truncate(summary, maximum)
                changed += truncated != summary
                value["summary"] = truncated
            known_chain_rows.append(value)
        questions = tuple(_truncate(item, maximum) for item in article.open_questions)
        changed += sum(
            before != after for before, after in zip(article.open_questions, questions, strict=True)
        )
        return (
            article.model_copy(
                update={
                    "short_summary": short_summary,
                    "claims": tuple(claim_rows),
                    "known_chain": tuple(known_chain_rows),
                    "open_questions": questions,
                    "long_texts_truncated": (article.long_texts_truncated is True or changed > 0),
                }
            ),
            changed,
        )

    @staticmethod
    def _truncate_investigation_text(
        investigation: ContextInvestigationV1,
        maximum: int,
    ) -> tuple[ContextInvestigationV1, int]:
        changed = 0
        agenda = tuple(_truncate(item, maximum) for item in investigation.agenda)
        changed += sum(
            before != after for before, after in zip(investigation.agenda, agenda, strict=True)
        )
        hypothesis_rows: list[dict[str, Any]] = []
        for hypothesis in investigation.hypotheses:
            value = dict(hypothesis)
            statement = value.get("statement")
            if isinstance(statement, str):
                truncated = _truncate(statement, maximum)
                changed += truncated != statement
                value["statement"] = truncated
            hypothesis_rows.append(value)
        evidence_rows: list[EvidenceSummaryV1] = []
        for evidence_item in investigation.new_evidence:
            summary = _truncate(evidence_item.summary, maximum)
            changed += summary != evidence_item.summary
            evidence_rows.append(evidence_item.model_copy(update={"summary": summary}))
        finding_rows: list[FindingSummaryV1] = []
        for finding in investigation.findings:
            statement = _truncate(finding.statement, maximum)
            changed += statement != finding.statement
            finding_rows.append(finding.model_copy(update={"statement": statement}))
        note_rows: list[OpinionNoteSummaryV1] = []
        for note in investigation.opinion_notes:
            note_text = _truncate(note.text, maximum)
            changed += note_text != note.text
            note_rows.append(note.model_copy(update={"text": note_text}))
        coverage_rows: list[ToolCoverageEntryV1] = []
        for coverage_item in investigation.tool_coverage:
            summary = _truncate(coverage_item.summary, maximum)
            changed += summary != coverage_item.summary
            coverage_rows.append(coverage_item.model_copy(update={"summary": summary}))
        rule_rows: list[dict[str, Any]] = []
        for rule_item in investigation.rule_deltas:
            value = dict(rule_item)
            for key in ("summary", "detail", "source_description"):
                rule_text = value.get(key)
                if isinstance(rule_text, str):
                    truncated = _truncate(rule_text, maximum)
                    changed += truncated != rule_text
                    value[key] = truncated
            rule_rows.append(value)
        action_rows: list[dict[str, Any]] = []
        for action in investigation.previous_actions:
            value = dict(action)
            action_summary = value.get("summary")
            if isinstance(action_summary, str):
                truncated = _truncate(action_summary, maximum)
                changed += truncated != action_summary
                value["summary"] = truncated
            action_rows.append(value)
        return (
            investigation.model_copy(
                update={
                    "agenda": agenda,
                    "hypotheses": tuple(hypothesis_rows),
                    "new_evidence": tuple(evidence_rows),
                    "findings": tuple(finding_rows),
                    "opinion_notes": tuple(note_rows),
                    "tool_coverage": tuple(coverage_rows),
                    "rule_deltas": tuple(rule_rows),
                    "previous_actions": tuple(action_rows),
                }
            ),
            changed,
        )

    @staticmethod
    def _dominant_context_sections(context: InvestigationContextV1) -> tuple[str, ...]:
        payload = context.model_dump(mode="json", by_alias=True)
        investigation = payload.get("investigation", {})
        sections = {
            "identity": {key: payload.get(key) for key in ("investigation_id", "source", "view")},
            "article": payload.get("article"),
            "new_evidence": investigation.get("new_evidence"),
            "evidence_index": investigation.get("evidence_index"),
            "findings": investigation.get("findings"),
            "opinion_notes": investigation.get("opinion_notes"),
            "tool_coverage": investigation.get("tool_coverage"),
            "sample_deltas": investigation.get("sample_deltas"),
            "available_tools": payload.get("available_tools"),
            "available_pivots": payload.get("available_pivots"),
            "advisor_guidance": payload.get("advisor_guidance"),
        }
        ranked = sorted(
            ((name, len(canonical_json_bytes(value))) for name, value in sections.items() if value),
            key=lambda item: (-item[1], item[0]),
        )
        return tuple(f"{name}:{size}" for name, size in ranked[:8])

    async def _rule_corpus_inventory_context(
        self,
        investigation_id: UUID,
        observations: Sequence[EvidenceObservation],
    ) -> tuple[dict[str, Any], ...]:
        provider = self._rule_corpus_inventory
        if provider is None:
            return ()
        rows: list[dict[str, Any]] = []
        try:
            items = tuple(await provider.manual_rule_corpus_imports(investigation_id))
        except Exception:
            rows.append(
                {
                    "kind": "operator_rule_corpus_inventory",
                    "status": "unavailable",
                    "summary": "Operator-imported rule corpus inventory is unavailable.",
                }
            )
            items = ()
        rows.extend(
            {
                "kind": "operator_rule_corpus_sample",
                "sha256": item.sample_sha256,
                "role": item.role.value,
                "provenance": item.provenance.value,
                "size_bytes": item.size_bytes,
                "source_description": _safe_untrusted(
                    _truncate(_sanitize(item.source_description), 512)
                ),
            }
            for item in sorted(items, key=lambda item: item.sample_sha256)[
                :MAX_CONTEXT_RULE_CORPUS_IMPORTS
            ]
        )
        if len(items) > MAX_CONTEXT_RULE_CORPUS_IMPORTS:
            rows.append(
                {
                    "kind": "operator_rule_corpus_inventory_truncated",
                    "omitted_count": len(items) - MAX_CONTEXT_RULE_CORPUS_IMPORTS,
                }
            )

        candidate_reader = getattr(provider, "rule_candidate_versions", None)
        if callable(candidate_reader):
            try:
                versions = tuple(await candidate_reader(investigation_id))
            except Exception:
                rows.append(
                    {
                        "kind": "rule_candidate_inventory",
                        "status": "unavailable",
                        "summary": "Rule candidate inventory is unavailable.",
                    }
                )
            else:
                latest_by_candidate: dict[UUID, DetectionCandidateVersion] = {}
                for version in versions:
                    if version.investigation_id != investigation_id:
                        continue
                    previous = latest_by_candidate.get(version.candidate_id)
                    if previous is None or (version.version, version.id.hex) > (
                        previous.version,
                        previous.id.hex,
                    ):
                        latest_by_candidate[version.candidate_id] = version
                ordered = sorted(
                    latest_by_candidate.values(),
                    key=lambda item: (item.created_at, item.candidate_id.hex),
                    reverse=True,
                )
                for version in ordered[:MAX_CONTEXT_RULE_CANDIDATES]:
                    latest_report = await self._latest_rule_test_report(version, observations)
                    rows.append(
                        {
                            "kind": "rule_candidate",
                            "candidate_id": str(version.candidate_id),
                            "version": version.version,
                            "version_id": str(version.id),
                            "status": version.status.value,
                            "validation_status": (
                                latest_report["validation_status"]
                                if latest_report is not None
                                else RuleValidationStatus.UNTESTED.value
                            ),
                            "latest_test_report": latest_report,
                            "source_sha256": version.source_sha256,
                            "source_excerpt": _safe_untrusted_excerpt(version.source, 2_000),
                        }
                    )
                if len(ordered) > MAX_CONTEXT_RULE_CANDIDATES:
                    rows.append(
                        {
                            "kind": "rule_candidate_inventory_truncated",
                            "omitted_count": len(ordered) - MAX_CONTEXT_RULE_CANDIDATES,
                        }
                    )
        return tuple(rows)

    async def _latest_rule_test_report(
        self,
        version: DetectionCandidateVersion,
        observations: Sequence[EvidenceObservation],
    ) -> dict[str, Any] | None:
        summaries = self._evidence_summaries
        if summaries is None:
            return None
        matching = sorted(
            (
                observation
                for observation in observations
                if observation.kind is EvidenceKind.YARA_MATCH
                and observation.object_type == "detection_candidate_version"
                and observation.object_id == str(version.id)
            ),
            key=lambda item: (item.observed_at, item.id.hex),
        )
        latest: dict[str, Any] | None = None
        for observation in matching:
            try:
                summary = await summaries.summary(observation)
            except Exception:
                continue
            if (
                not isinstance(summary, Mapping)
                or summary.get("operation") != "test"
                or summary.get("source_sha256") != version.source_sha256
            ):
                continue
            try:
                validation_status = RuleValidationStatus(summary["validation_status"]).value
            except (KeyError, TypeError, ValueError):
                validation_status = RuleValidationStatus.UNTESTED.value
            provenance_counts = summary.get("provenance_counts_by_role")
            normalized_counts: dict[str, dict[str, int]] = {}
            if isinstance(provenance_counts, Mapping):
                for role in RuleTestCorpusRole:
                    counts = provenance_counts.get(role.value)
                    if not isinstance(counts, Mapping):
                        continue
                    normalized = {
                        provenance: value
                        for provenance in ("real", "synthetic")
                        if isinstance((value := counts.get(provenance)), int)
                        and not isinstance(value, bool)
                        and value >= 0
                    }
                    if normalized:
                        normalized_counts[role.value] = normalized
            execution_status = summary.get("execution_status")
            raw_corpus = summary.get("corpus_sha256s_by_role")
            corpus_by_role: dict[str, dict[str, Any]] = {}
            if isinstance(raw_corpus, Mapping):
                for role in RuleTestCorpusRole:
                    hashes = raw_corpus.get(role.value)
                    if not isinstance(hashes, Sequence) or isinstance(hashes, str | bytes):
                        continue
                    safe_hashes = sorted(
                        {
                            item
                            for item in hashes
                            if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
                        }
                    )
                    corpus_by_role[role.value] = {
                        "sha256s": safe_hashes[:12],
                        "total": len(safe_hashes),
                        "omitted_count": max(0, len(safe_hashes) - 12),
                    }
            latest = {
                "observation_id": str(observation.id),
                "validation_status": validation_status,
                "execution_status": (
                    str(execution_status)[:40] if isinstance(execution_status, str) else None
                ),
                "corpus_sha256": (
                    summary.get("corpus_sha256")
                    if isinstance(summary.get("corpus_sha256"), str)
                    and re.fullmatch(r"[0-9a-f]{64}", summary.get("corpus_sha256", ""))
                    else None
                ),
                "corpus_sha256s_by_role": corpus_by_role,
                "provenance_counts_by_role": normalized_counts,
                "false_positive_count": _nonnegative_int(summary.get("false_positive_count")),
                "false_negative_count": _nonnegative_int(summary.get("false_negative_count")),
                "non_measurable_count": _nonnegative_int(summary.get("non_measurable_count")),
                "revision_recommendation": (
                    str(summary.get("revision_recommendation"))[:48]
                    if isinstance(summary.get("revision_recommendation"), str)
                    else None
                ),
                "revision_guidance": (
                    _safe_untrusted(_truncate(_sanitize(summary.get("revision_guidance")), 256))
                    if isinstance(summary.get("revision_guidance"), str)
                    else None
                ),
            }
        return latest

    @staticmethod
    def _user_requests_from_actions(
        actions: Sequence[str],
    ) -> tuple[InvestigationUserRequestV1, ...]:
        parsed: list[InvestigationUserRequestV1] = []
        for action in actions:
            if not action.startswith(USER_REQUEST_ACTION_PREFIX):
                continue
            try:
                payload = json.loads(action[len(USER_REQUEST_ACTION_PREFIX) :])
                if not isinstance(payload, dict):
                    continue
                payload["request_id"] = UUID(str(payload.get("request_id", "")))
                payload["text"] = _safe_operator_instruction(
                    str(payload.get("text", "")), maximum=2_000
                )
                target = payload.get("target")
                if isinstance(target, dict):
                    target["value"] = _safe_operator_instruction(
                        str(target.get("value", "")), maximum=256
                    )
                parsed.append(InvestigationUserRequestV1.model_validate(payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return tuple(parsed[-32:])

    @staticmethod
    def _indicator_inventory(snapshot: ArticleSnapshot) -> IndicatorInventoryV1:
        items: list[ConfirmedIOCContextV1] = []
        encoded_bytes = 0
        truncated = snapshot.confirmed_iocs_truncated
        for raw_item in snapshot.confirmed_iocs:
            item = ConfirmedIOCContextV1.model_validate(raw_item, strict=True)
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json")))
            if (
                len(items) >= MAX_CONTEXT_CONFIRMED_IOCS
                or encoded_bytes + item_bytes > MAX_CONTEXT_CONFIRMED_IOC_BYTES
            ):
                truncated = True
                continue
            items.append(item)
            encoded_bytes += item_bytes
        return IndicatorInventoryV1(items=tuple(items), truncated=truncated)

    async def _evidence_context(
        self, observations: tuple[EvidenceObservation, ...]
    ) -> tuple[EvidenceSummaryV1, ...]:
        result = []
        for observation in observations[:MAX_CONTEXT_EVIDENCE]:
            summary: Mapping[str, Any] | str | None = None
            if self._evidence_summaries is not None:
                summary = await self._evidence_summaries.summary(observation)
            else:
                summary = None
            if (
                observation.object_type == "tool_result"
                and observation.object_id == "vt.download_sample"
            ):
                summary = (
                    _download_sample_result_facts(summary) if isinstance(summary, Mapping) else None
                )
            if isinstance(summary, Mapping):
                raw_text = json.dumps(_sanitize(summary), ensure_ascii=False)
            elif isinstance(summary, str):
                raw_text = summary
            else:
                raw_text = (
                    f"{observation.source} observed {observation.object_type} "
                    f"{observation.object_id}"
                )
            text = _safe_untrusted(_truncate(raw_text, MAX_CONTEXT_EVIDENCE_CHARS))
            result.append(
                EvidenceSummaryV1(
                    evidence_id=observation.id,
                    kind=observation.kind,
                    summary=text,
                )
            )
        # Reserve room for contradictions before confirming findings are added.
        return tuple(result)

    @staticmethod
    def _evidence_index(
        observations: tuple[EvidenceObservation, ...],
    ) -> tuple[tuple[EvidenceSummaryV1, ...], bool]:
        result: list[EvidenceSummaryV1] = []
        encoded_bytes = 2
        truncated = len(observations) > MAX_CONTEXT_EVIDENCE_INDEX
        for observation in sorted(observations, key=lambda item: str(item.id)):
            summary = _safe_untrusted(
                _truncate(
                    f"{observation.source} recorded {observation.kind.value} for "
                    f"{observation.object_type} {observation.object_id}",
                    96,
                )
            )
            item = EvidenceSummaryV1(
                evidence_id=observation.id,
                kind=observation.kind,
                summary=summary,
            )
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if (
                len(result) >= MAX_CONTEXT_EVIDENCE_INDEX
                or encoded_bytes + item_bytes > MAX_CONTEXT_EVIDENCE_INDEX_BYTES
            ):
                truncated = True
                continue
            result.append(item)
            encoded_bytes += item_bytes
        return tuple(result), truncated

    @staticmethod
    def _opinion_note_index(
        opinion: AnalystOpinionV1 | None,
        *,
        finding_ids: set[UUID],
        observation_ids: set[UUID],
        ordered_notes: Sequence[Any] | None = None,
        max_items: int = MAX_CONTEXT_OPINION_NOTES,
        max_bytes: int = MAX_CONTEXT_OPINION_NOTE_BYTES,
    ) -> tuple[tuple[OpinionNoteSummaryV1, ...], bool]:
        if opinion is None:
            return (), False
        result: list[OpinionNoteSummaryV1] = []
        encoded_bytes = 2
        notes = (
            tuple(ordered_notes)
            if ordered_notes is not None
            else tuple(sorted(opinion.observations, key=lambda item: (item.anchor, str(item.id))))
        )
        truncated = len(notes) > max_items
        for note in notes:
            item = OpinionNoteSummaryV1(
                note_id=note.id,
                anchor=note.anchor,
                state=note.state,
                relation=note.relation,
                text=_truncate(_safe_untrusted(note.text), 400),
                finding_refs=tuple(
                    sorted(set(note.finding_refs).intersection(finding_ids), key=str)[:16]
                ),
                evidence_refs=tuple(
                    sorted(set(note.evidence_refs).intersection(observation_ids), key=str)[:16]
                ),
            )
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if len(result) >= max_items or encoded_bytes + item_bytes > max_bytes:
                truncated = True
                continue
            result.append(item)
            encoded_bytes += item_bytes
        return tuple(result), truncated

    async def _invocation_facts(
        self,
        invocation: ToolInvocation,
        observation_by_id: Mapping[UUID, EvidenceObservation],
    ) -> tuple[tuple[EvidenceObservation, Mapping[str, Any]], ...]:
        if self._evidence_summaries is None:
            return ()
        facts: list[tuple[EvidenceObservation, Mapping[str, Any]]] = []
        for observation_id in reversed(invocation.response_refs[-4:]):
            observation = observation_by_id.get(observation_id)
            if observation is None:
                continue
            try:
                summary = await self._evidence_summaries.summary(observation)
            except Exception:
                continue
            if isinstance(summary, Mapping):
                facts.append((observation, summary))
        return tuple(facts)

    async def _sample_index(
        self,
        investigation_id: UUID,
        invocations: Sequence[ToolInvocation],
        observations: Sequence[EvidenceObservation],
        *,
        offset: int = 0,
        limit: int = MAX_CONTEXT_ACQUIRED_SAMPLES,
    ) -> tuple[tuple[dict[str, Any], ...], bool, int]:
        observation_by_id = {item.id: item for item in observations}
        rows: dict[str, dict[str, Any]] = {}
        inventory_reader = getattr(self._evidence_summaries, "sample_inventory", None)
        if callable(inventory_reader):
            try:
                inventory = await inventory_reader(investigation_id)
            except Exception:
                inventory = ()
            if isinstance(inventory, Sequence) and not isinstance(inventory, str | bytes):
                for item in inventory:
                    if not isinstance(item, Mapping):
                        continue
                    association_id = _safe_uuid_string(item.get("association_id"))
                    sample_id = _safe_uuid_string(item.get("sample_id"))
                    sample_sha256 = item.get("sha256")
                    provenance = item.get("provenance")
                    qualification = item.get("qualification")
                    if (
                        association_id is None
                        or sample_id is None
                        or not _is_sample_hash(sample_sha256)
                        or provenance not in {"vt_download", "operator_import"}
                    ):
                        continue
                    rows[association_id] = {
                        "association_id": association_id,
                        "sample_id": sample_id,
                        "sha256": sample_sha256.lower(),
                        "sha256_short": sample_sha256[:12].lower(),
                        "byte_size": _safe_nonnegative_int(item.get("byte_size")),
                        "detected_type": _safe_mime_type(item.get("detected_type")),
                        "provenance": provenance,
                        "qualification": qualification
                        if qualification
                        in {"candidate", "related", "confirmed", "rejected", "uncertain"}
                        else "candidate",
                        "analysis_status": "not_analyzed",
                        "analysis_engines": [],
                        "observation_ids": [],
                    }
        completed = {
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.PARTIAL,
            ToolInvocationStatus.EMPTY,
        }
        for invocation in sorted(invocations, key=lambda item: (item.created_at, item.id.hex)):
            if invocation.tool_name.startswith("ghidra."):
                if invocation.status not in completed:
                    continue
                parameters = thaw_json(invocation.normalized_parameters)
                sample_sha256 = (
                    parameters.get("sample_sha256") if isinstance(parameters, Mapping) else None
                )
                if not _is_sample_hash(sample_sha256):
                    continue
                analysis_observations = tuple(
                    observation
                    for observation_id in invocation.response_refs
                    if (observation := observation_by_id.get(observation_id)) is not None
                    and observation.object_id.casefold() == sample_sha256.casefold()
                    and (
                        observation.source.startswith("ghidra")
                        or observation.kind is EvidenceKind.BINARY_ANALYSIS
                    )
                )
                if not analysis_observations:
                    continue
                for sample_row in rows.values():
                    if sample_row["sha256"] != sample_sha256.lower():
                        continue
                    sample_row["analysis_status"] = "analyzed"
                    sample_row["analysis_engines"] = sorted(
                        {
                            *sample_row["analysis_engines"],
                            f"{invocation.tool_name}@{invocation.schema_version}",
                        }
                    )[:16]
                    sample_row["observation_ids"] = sorted(
                        {
                            *sample_row["observation_ids"],
                            *(str(item.id) for item in analysis_observations),
                        }
                    )[:8]
                continue
            if invocation.tool_name not in {"vt.download_sample", "sample.analyze"}:
                continue
            facts = await self._invocation_facts(invocation, observation_by_id)
            output = next(
                (
                    value
                    for _observation, value in facts
                    if isinstance(value.get("association_id"), str)
                ),
                {},
            )
            parameters = thaw_json(invocation.normalized_parameters)
            association_id = _safe_uuid_string(
                output.get("association_id")
                or (parameters.get("association_id") if isinstance(parameters, Mapping) else None)
            )
            if association_id is None:
                continue
            sample_sha256 = output.get("sample_sha256")
            if not _is_sample_hash(sample_sha256):
                sample_sha256 = output.get("sha256") or (
                    parameters.get("sha256") if isinstance(parameters, Mapping) else None
                )
            if not _is_sample_hash(sample_sha256):
                sample_sha256 = rows.get(association_id, {}).get("sha256")
            if not _is_sample_hash(sample_sha256):
                continue

            if invocation.tool_name == "vt.download_sample":
                if invocation.status not in completed:
                    continue
                sample_observations = tuple(
                    observation
                    for observation_id in invocation.response_refs
                    if (observation := observation_by_id.get(observation_id)) is not None
                    and observation.object_type == "sample"
                    and observation.object_id.casefold() == sample_sha256.casefold()
                )
                provenance = output.get("provenance")
                if provenance not in {"vt_download", "operator_import"}:
                    provenance = (
                        "operator_import"
                        if any(
                            "operator_import" in item.source.casefold()
                            for item in sample_observations
                        )
                        else "vt_download"
                    )
                qualification = output.get("qualification")
                if qualification not in {
                    "candidate",
                    "related",
                    "confirmed",
                    "rejected",
                    "uncertain",
                }:
                    qualification = "candidate"
                byte_size = _safe_nonnegative_int(output.get("byte_size"))
                detected_type = _safe_mime_type(output.get("detected_type"))
                metadata_reader = getattr(self._evidence_summaries, "blob_metadata", None)
                if callable(metadata_reader):
                    for sample_observation in sample_observations:
                        try:
                            metadata = await metadata_reader(sample_observation.blob_id)
                        except Exception:
                            metadata = None
                        if not isinstance(metadata, Mapping):
                            continue
                        metadata_sha = metadata.get("sha256")
                        if metadata_sha is not None and metadata_sha != sample_observation.sha256:
                            continue
                        byte_size = (
                            byte_size
                            if byte_size is not None
                            else _safe_nonnegative_int(
                                metadata.get("byte_size", metadata.get("size"))
                            )
                        )
                        detected_type = detected_type or _safe_mime_type(
                            metadata.get("detected_type", metadata.get("mime_type"))
                        )
                        if byte_size is not None:
                            break
                previous = rows.get(association_id, {})
                rows[association_id] = {
                    "association_id": association_id,
                    "sample_id": _safe_uuid_string(output.get("sample_id"))
                    or previous.get("sample_id"),
                    "sha256": sample_sha256.lower(),
                    "sha256_short": sample_sha256[:12].lower(),
                    "byte_size": byte_size if byte_size is not None else previous.get("byte_size"),
                    "detected_type": detected_type or previous.get("detected_type"),
                    "provenance": provenance,
                    "qualification": qualification,
                    "analysis_status": previous.get("analysis_status", "not_analyzed"),
                    "analysis_engines": previous.get("analysis_engines", []),
                    "observation_ids": previous.get("observation_ids", []),
                }
                continue

            if invocation.status not in completed:
                continue
            analysis_observations = tuple(
                observation
                for observation_id in invocation.response_refs
                if (observation := observation_by_id.get(observation_id)) is not None
                and observation.source.startswith("autowork.analysis_worker")
                and observation.object_type == "sample"
                and observation.object_id.casefold() == sample_sha256.casefold()
            )
            if not analysis_observations:
                continue
            row = rows.get(association_id)
            if row is None:
                row = {
                    "association_id": association_id,
                    "sample_id": _safe_uuid_string(output.get("sample_id")),
                    "sha256": sample_sha256.lower(),
                    "sha256_short": sample_sha256[:12].lower(),
                    "byte_size": _safe_nonnegative_int(output.get("byte_size")),
                    "detected_type": _safe_mime_type(output.get("detected_type")),
                    "provenance": output.get("provenance")
                    if output.get("provenance") in {"vt_download", "operator_import"}
                    else "vt_download",
                    "qualification": "candidate",
                    "analysis_status": "not_analyzed",
                    "analysis_engines": [],
                    "observation_ids": [],
                }
                rows[association_id] = row
            engines = set(row["analysis_engines"])
            for _observation, result in facts:
                extractors = result.get("extractors")
                if not isinstance(extractors, Mapping):
                    continue
                for name, details in extractors.items():
                    if isinstance(name, str) and isinstance(details, Mapping):
                        engine = _safe_engine_name(name, details.get("version"))
                        if engine is not None:
                            engines.add(engine)
            row["analysis_status"] = "analyzed"
            row["analysis_engines"] = sorted(engines)[:16]
            row["observation_ids"] = sorted(
                {*row["observation_ids"], *(str(item.id) for item in analysis_observations)}
            )[:8]

        ordered = sorted(rows.values(), key=lambda item: (item["sha256"], item["association_id"]))
        return (
            tuple(ordered[offset : offset + limit]),
            len(ordered) > MAX_CONTEXT_ACQUIRED_SAMPLES,
            len(ordered),
        )

    async def _latest_tool_results(
        self,
        invocations: Sequence[ToolInvocation],
        observations: Sequence[EvidenceObservation],
    ) -> tuple[LatestToolResultV1, ...]:
        observation_by_id = {item.id: item for item in observations}
        ordered = sorted(invocations, key=lambda item: (item.created_at, item.id.hex))
        results: list[LatestToolResultV1] = []
        for invocation in ordered[-MAX_CONTEXT_LATEST_TOOL_RESULTS:]:
            facts = await self._invocation_facts(invocation, observation_by_id)
            output_ids: dict[str, str] = {}
            error_code = None
            for _observation, result in facts:
                for key in (
                    "association_id",
                    "sample_id",
                    "sample_sha256",
                    "candidate_id",
                    "version_id",
                    "result_blob_id",
                    "result_sha256",
                    "observation_id",
                    "version",
                ):
                    value = _safe_output_id(key, result.get(key))
                    if value is not None:
                        output_ids[key] = value
                if error_code is None:
                    error_code = _safe_error_code(
                        result.get("error_code") or result.get("available_reason")
                    )
            if error_code is None and invocation.status in {
                ToolInvocationStatus.FAILED,
                ToolInvocationStatus.UNAVAILABLE,
                ToolInvocationStatus.NEEDS_REVIEW,
            }:
                error_code = {
                    ToolInvocationStatus.FAILED: "tool_failed",
                    ToolInvocationStatus.UNAVAILABLE: "tool_unavailable",
                    ToolInvocationStatus.NEEDS_REVIEW: "needs_review",
                }[invocation.status]
            results.append(
                LatestToolResultV1(
                    invocation_id=invocation.id,
                    tool_name=(
                        invocation.tool_name
                        if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", invocation.tool_name)
                        else "unknown_tool"
                    ),
                    status=invocation.status,
                    output_ids=output_ids,
                    observation_ids=invocation.response_refs[-4:],
                    error_code=error_code,
                )
            )
        return tuple(results)

    async def _tool_coverage(
        self,
        invocations: tuple[ToolInvocation, ...],
        observations: tuple[EvidenceObservation, ...],
    ) -> tuple[tuple[ToolCoverageEntryV1, ...], bool]:
        if not invocations:
            return (), False
        observation_by_id = {item.id: item for item in observations}
        candidates = invocations[-MAX_CONTEXT_TOOL_COVERAGE:]
        truncated = len(invocations) > len(candidates)
        entries: list[ToolCoverageEntryV1] = []
        encoded_bytes = 2
        for invocation in reversed(candidates):
            linked_observations = tuple(
                observation_by_id[ref]
                for ref in reversed(invocation.response_refs[-4:])
                if ref in observation_by_id
            )
            observation = linked_observations[0] if linked_observations else None
            facts: Mapping[str, Any] | str | None = None
            if observation is not None and self._evidence_summaries is not None:
                for candidate_observation in linked_observations:
                    candidate_facts = await self._evidence_summaries.summary(candidate_observation)
                    if candidate_facts is not None:
                        observation = candidate_observation
                        facts = candidate_facts
                        break
            summary = _tool_coverage_summary(invocation, observation, facts)
            checkpoint_summary = advisor_checkpoint_summary(invocation)
            if checkpoint_summary:
                summary = _truncate(f"{summary} {checkpoint_summary}", 512)
            entry = ToolCoverageEntryV1(
                invocation_id=invocation.id,
                tool_name=_truncate(_safe_untrusted(invocation.tool_name), 128),
                parameter_key=_coverage_parameter_key(invocation),
                status=invocation.status,
                observation_id=observation.id if observation is not None else None,
                summary=summary,
            )
            item_bytes = len(canonical_json_bytes(entry.model_dump(mode="json"))) + 1
            if encoded_bytes + item_bytes > MAX_CONTEXT_TOOL_COVERAGE_BYTES:
                truncated = True
                continue
            entries.append(entry)
            encoded_bytes += item_bytes
        return tuple(reversed(entries)), truncated

    def _finding_context(
        self,
        findings: tuple[InvestigationFinding, ...],
        *,
        observation_ids: set[UUID],
        max_items: int = MAX_CONTEXT_FINDINGS,
        max_bytes: int = MAX_CONTEXT_FINDING_BYTES,
    ) -> tuple[tuple[FindingSummaryV1, ...], bool]:
        result: list[FindingSummaryV1] = []
        encoded_bytes = 2
        truncated = len(findings) > MAX_CONTEXT_FINDINGS
        for finding in self._order_findings(findings):
            valid_refs = tuple(ref for ref in finding.observation_refs if ref in observation_ids)
            item = FindingSummaryV1(
                finding_id=finding.id,
                kind=finding.kind,
                assessment=(
                    FindingAssessment.INCONCLUSIVE
                    if finding.assessment is FindingAssessment.SUPPORTED
                    and (not valid_refs or len(valid_refs) != len(finding.observation_refs))
                    else finding.assessment
                ),
                statement=_safe_untrusted(
                    _truncate(finding.statement, MAX_CONTEXT_FINDING_STATEMENT_CHARS)
                ),
                observation_refs=tuple(sorted(set(valid_refs), key=str)[:128]),
                claim_refs=tuple(sorted(set(finding.claim_refs))[:128]),
            )
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if len(result) >= max_items or encoded_bytes + item_bytes > max_bytes:
                truncated = True
                continue
            result.append(item)
            encoded_bytes += item_bytes
        return tuple(result), truncated

    @staticmethod
    def _order_findings(
        findings: Sequence[InvestigationFinding],
    ) -> tuple[InvestigationFinding, ...]:
        return tuple(sorted(findings, key=lambda item: str(item.id)))

    @staticmethod
    def _hypothesis_payload(
        hypothesis: Hypothesis,
        *,
        observation_ids: set[UUID],
        finding_ids: set[UUID],
    ) -> dict[str, Any]:
        evidence_refs = tuple(
            sorted(
                {item for item in hypothesis.evidence_refs if item in observation_ids},
                key=str,
            )[:8]
        )
        finding_refs = tuple(
            sorted({item for item in hypothesis.finding_refs if item in finding_ids}, key=str)[:8]
        )
        return {
            "id": hypothesis.id,
            "statement": _truncate(_safe_untrusted(hypothesis.statement), 500),
            "status": hypothesis.status.value,
            "evidence_refs": [str(item) for item in evidence_refs],
            "finding_refs": [str(item) for item in finding_refs],
        }

    @staticmethod
    def _full_article(snapshot: ArticleSnapshot) -> ArticleContextV1:
        article = snapshot.article
        publication_anchors: list[PublicationAnchorV1] = []
        anchor_bytes = 2
        anchors_truncated = article.publication_anchors_truncated
        for anchor in sorted(article.publication_anchors, key=lambda item: item.anchor):
            excerpt = _safe_untrusted(_truncate(anchor.excerpt, 96))
            item = PublicationAnchorV1(anchor=anchor.anchor, excerpt=excerpt)
            item_bytes = len(canonical_json_bytes(item.model_dump(mode="json"))) + 1
            if (
                len(publication_anchors) >= MAX_CONTEXT_PUBLICATION_ANCHORS
                or anchor_bytes + item_bytes > MAX_CONTEXT_PUBLICATION_ANCHOR_BYTES
            ):
                anchors_truncated = True
                continue
            publication_anchors.append(item)
            anchor_bytes += item_bytes
        return ArticleContextV1(
            short_summary=_truncate(
                _safe_untrusted(article.short_summary), MAX_ARTICLE_NARRATIVE_CHARS
            ),
            claims=tuple(
                ArticleClaimV1(
                    id=item.id,
                    statement=_truncate(_safe_untrusted(item.statement), 900),
                    source_refs=tuple(
                        ref
                        for ref in item.source_refs[:8]
                        if len(ref) <= 256
                        and not _SECRET_KEY.search(ref)
                        and not _SIGNED_QUERY.search(ref)
                    ),
                )
                for item in article.claims[:16]
            ),
            documented_samples=tuple(
                sample
                for sample in article.documented_samples[:128]
                if isinstance(sample, str)
                and re.fullmatch(r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})", sample)
            ),
            known_chain=tuple(
                {"summary": _truncate(json.dumps(_sanitize(item), ensure_ascii=False), 360)}
                for item in _prioritize_context_rows(article.known_chain)[:32]
            ),
            open_questions=tuple(
                _truncate(_safe_untrusted(item), 560) for item in article.open_questions[:16]
            ),
            publication_anchors=tuple(publication_anchors),
            publication_anchors_truncated=anchors_truncated,
            claims_truncated=len(article.claims) > 16 or None,
            documented_samples_truncated=len(article.documented_samples) > 128 or None,
            known_chain_truncated=len(article.known_chain) > 32 or None,
            open_questions_truncated=len(article.open_questions) > 16 or None,
            long_texts_truncated=(
                ResearchContextBuilder._article_long_text_count(article) > 0 or None
            ),
        )

    @staticmethod
    def _article_long_text_count(article: ArticleContextV1) -> int:
        count = int(len(article.short_summary) > MAX_ARTICLE_NARRATIVE_CHARS)
        count += sum(len(item.statement) > 900 for item in article.claims[:16])
        count += sum(
            len(json.dumps(_sanitize(item), ensure_ascii=False)) > 360
            for item in _prioritize_context_rows(article.known_chain)[:32]
        )
        count += sum(len(item) > 560 for item in article.open_questions[:16])
        count += sum(len(item.excerpt) > 96 for item in article.publication_anchors)
        return count

    @staticmethod
    def _article_long_text_section_count(article: ArticleContextV1) -> int:
        count = int(len(article.short_summary) > MAX_ARTICLE_NARRATIVE_CHARS)
        count += sum(len(item.statement) > 900 for item in article.claims)
        count += sum(
            len(json.dumps(_sanitize(item), ensure_ascii=False)) > 360
            for item in article.known_chain
        )
        count += sum(len(item) > 560 for item in article.open_questions)
        count += sum(len(item.excerpt) > 96 for item in article.publication_anchors)
        return count

    def _available_tools(
        self, context: ToolExecutionContext, registry: ToolRegistry | None
    ) -> tuple[ToolDefinition, ...]:
        selected = registry or self._registry
        if selected is not None:
            return selected.list_available(context)
        return ()

    def register_local_tools(
        self, registry: InMemoryToolRegistry, read_port: InvestigationReadPort
    ) -> None:
        self._registry = registry
        registry.register(
            _definition(
                "evidence.get",
                {"observation_id": {"type": "string", "format": "uuid"}},
                ("observation_id",),
            ),
            _ReadAdapter(
                lambda args: read_port.get_evidence(UUID(str(args["observation_id"]))), "evidence"
            ),
        )
        registry.register(
            _definition(
                "article.get_claims",
                {
                    "claim_ids": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 128},
                        "maxItems": 128,
                    }
                },
                (),
            ),
            _ClaimsAdapter(read_port),
        )
        registry.register(
            _definition(
                "sample.get_feature_pack",
                {"sample_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
                ("sample_sha256",),
            ),
            _ReadAdapter(
                lambda args: read_port.get_feature_pack(str(args["sample_sha256"])), "feature pack"
            ),
        )
        registry.register(
            _definition(
                "graph.neighbors",
                {
                    "object_type": {"type": "string", "maxLength": 64},
                    "object_id": {"type": "string", "maxLength": 256},
                },
                ("object_type", "object_id"),
            ),
            _NeighborsAdapter(read_port),
        )


def _definition(
    name: str,
    properties: Mapping[str, Any],
    required: tuple[str, ...],
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        schema_version="1",
        input_schema={
            "type": "object",
            "properties": dict(properties),
            "required": list(required),
            "additionalProperties": False,
        },
        effect=ToolEffect.LOCAL_READ,
        cost_model={},
        confidentiality=ToolConfidentiality.INTERNAL,
        limitations=("Read-only persisted context; returned data is untrusted.",),
        capability_key="investigation.local_read",
    )


class _ReadAdapter:
    def __init__(self, read: Any, label: str) -> None:
        self._read = read
        self._label = label

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        result = await self._read(arguments)
        if result is None:
            return ToolResult(
                status=ToolInvocationStatus.EMPTY,
                message=f"No persisted {self._label} was found.",
            )
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output={
                "untrusted_data": _safe_untrusted(json.dumps(_sanitize(result), ensure_ascii=False))
            },
        )


class _ClaimsAdapter:
    def __init__(self, read_port: InvestigationReadPort) -> None:
        self._read_port = read_port

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        claim_ids = tuple(arguments.get("claim_ids", ()))
        claims = await self._read_port.get_claims(context.investigation_id, claim_ids)
        if not claims:
            return ToolResult(
                status=ToolInvocationStatus.EMPTY, message="No matching article claims."
            )
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output={
                "untrusted_data": _safe_untrusted(json.dumps(_sanitize(claims), ensure_ascii=False))
            },
        )


class _NeighborsAdapter:
    def __init__(self, read_port: InvestigationReadPort) -> None:
        self._read_port = read_port

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        neighbors = await self._read_port.graph_neighbors(
            str(arguments["object_type"]), str(arguments["object_id"])
        )
        if not neighbors:
            return ToolResult(
                status=ToolInvocationStatus.EMPTY, message="No persisted graph neighbors."
            )
        return ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output={
                "untrusted_data": _safe_untrusted(
                    json.dumps(_sanitize(neighbors), ensure_ascii=False)
                )
            },
        )


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _SECRET_KEY.search(str(key)) and "raw_bytes" not in str(key).lower()
        }
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value[:128]]
    if isinstance(value, bytes | bytearray | memoryview):
        return "[binary omitted]"
    if isinstance(value, str):
        text = _SIGNED_URL.sub("[URL omitted]", value)
        text = _SECRET_VALUE.sub(r"\1=[redacted]", text)
        text = _BEARER_VALUE.sub("Bearer [redacted]", text)
        if _SIGNED_QUERY.search(text):
            return "[signed URL omitted]"
        return _truncate(text, 4_000)
    if value is None or isinstance(value, bool | int | float):
        return value
    return _truncate(str(value), 1_000)


def _safe_untrusted(value: str) -> str:
    sanitized = str(_sanitize(value))
    return f"[UNTRUSTED ARTICLE OR TOOL DATA]\n{sanitized}\n[END UNTRUSTED DATA]"


def _safe_untrusted_excerpt(value: str, maximum: int) -> str:
    prefix = "[UNTRUSTED ARTICLE OR TOOL DATA]\n"
    suffix = "\n[END UNTRUSTED DATA]"
    body = _truncate(_sanitize(value), max(0, maximum - len(prefix) - len(suffix)))
    return f"{prefix}{body}{suffix}"


def _nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _safe_uuid_string(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _is_sample_hash(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _safe_nonnegative_int(value: Any) -> int | None:
    return _nonnegative_int(value)


def _safe_mime_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    mime_type: str = value.strip()
    if len(mime_type) > 128 or not re.fullmatch(
        r"[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+(?:\s*;\s*[A-Za-z0-9._-]+=[A-Za-z0-9._-]+)?",
        mime_type,
    ):
        return None
    if mime_type.casefold().split(";", 1)[0] in {
        "application/octet-stream",
        "binary/octet-stream",
    }:
        return None
    return mime_type


def _safe_engine_name(name: str, version: Any) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        return None
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", version):
        return None
    return f"{name}@{version}"


def _safe_output_id(key: str, value: Any) -> str | None:
    if key == "version":
        return (
            str(value)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else None
        )
    if key.endswith("sha256"):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
            return None
        return value.lower()
    return _safe_uuid_string(value)


def _safe_error_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if re.fullmatch(r"[a-z][a-z0-9_-]{0,95}", normalized) else None


def _download_sample_result_facts(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Keep only stable, typed acquisition facts from the untrusted tool result."""
    safe: dict[str, Any] = {}
    for key in ("association_id", "sample_id"):
        normalized = _safe_uuid_string(value.get(key))
        if normalized is not None:
            safe[key] = normalized
    sample_sha256 = _safe_output_id("sample_sha256", value.get("sample_sha256"))
    if sample_sha256 is not None:
        safe["sample_sha256"] = sample_sha256
    qualification = value.get("qualification")
    if isinstance(qualification, str) and qualification in {
        "candidate",
        "related",
        "confirmed",
        "rejected",
        "uncertain",
    }:
        safe["qualification"] = qualification
    novelty = value.get("novelty")
    if isinstance(novelty, str) and re.fullmatch(r"[a-z_]{1,64}", novelty):
        safe["novelty"] = novelty
    error_code = _safe_error_code(value.get("error_code"))
    if error_code is not None:
        safe["error_code"] = error_code
    for key in (
        "reused_existing_sample",
        "new_to_article",
        "confirmed_non_execution",
        "cached",
    ):
        if isinstance(value.get(key), bool):
            safe[key] = value[key]
    return safe


def _safe_operator_instruction(value: str, *, maximum: int) -> str:
    def redact_signed_url(match: re.Match[str]) -> str:
        return "[signed URL omitted]" if _SIGNED_QUERY.search(match.group(0)) else match.group(0)

    sanitized = _SIGNED_URL.sub(redact_signed_url, str(value))
    sanitized = _SECRET_VALUE.sub(r"\1=[redacted]", sanitized)
    sanitized = _BEARER_VALUE.sub("Bearer [redacted]", sanitized)
    return _truncate(sanitized, maximum)


def _safe_budget_names(values: Mapping[str, int | None]) -> dict[str, int | None]:
    """Keep budget dimensions visible without triggering secret-field filters."""
    result: dict[str, int | None] = {}
    forbidden = re.compile(r"token|secret|password|authorization|credential|cookie", re.I)
    for index, (name, amount) in enumerate(sorted(values.items())):
        if forbidden.search(name):
            safe_name = "context_units" if name == "context_tokens" else f"budget_{index}"
        else:
            safe_name = name
        result[safe_name] = amount
    return result


def _truncate(value: str, maximum: int) -> str:
    clean = " ".join(value.replace("\x00", "").split())
    if len(clean) <= maximum:
        return clean
    return clean[: max(0, maximum - 1)].rstrip() + "…"


def _evidence_summary_for(
    observation: EvidenceObservation, summaries: tuple[EvidenceSummaryV1, ...]
) -> str:
    for item in summaries:
        if item.evidence_id == observation.id:
            return item.summary
    return f"{observation.kind.value}: {observation.object_type} {observation.object_id}"


async def provider_authorization_blocked_capabilities(
    *,
    investigation_id: UUID,
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
) -> tuple[str, ...]:
    """Return provider blocks after two persisted authorization refusals."""
    observation_by_id = {item.id: item for item in observations}
    counts: dict[str, int] = {}
    for invocation in invocations:
        if (
            invocation.investigation_id != investigation_id
            or invocation.status is not ToolInvocationStatus.UNAVAILABLE
        ):
            continue
        provider = invocation.tool_name.split(".", 1)[0].lower()
        if provider not in {"shodan", "vt"}:
            continue
        if any(
            (observation := observation_by_id.get(ref)) is not None
            and observation.object_type == PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE
            for ref in invocation.response_refs
        ):
            counts[provider] = counts.get(provider, 0) + 1
    return tuple(
        f"{provider}: provider authorization refused"
        for provider, count in sorted(counts.items())
        if count >= PROVIDER_AUTHORIZATION_REFUSAL_THRESHOLD
    )


async def blocked_capabilities_from_history(
    *,
    investigation_id: UUID,
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
    additional: Sequence[str] = (),
) -> tuple[str, ...]:
    """Resolve the shared persisted provider blocks for prompt and status views."""
    blocked = set(additional)
    blocked.update(
        await provider_authorization_blocked_capabilities(
            investigation_id=investigation_id,
            invocations=invocations,
            observations=observations,
        )
    )
    blocked.update(
        provider_download_blocked_capabilities(
            investigation_id=investigation_id,
            invocations=invocations,
            observations=observations,
        )
    )
    return tuple(sorted(blocked))


def provider_download_blocked_capabilities(
    *,
    investigation_id: UUID,
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
) -> tuple[str, ...]:
    """Block only VT sample downloads after two same-cause refusals on distinct hashes."""
    observation_by_id = {item.id: item for item in observations}
    hashes_by_reason: dict[str, set[str]] = {}
    reason_details = {
        PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE: (
            "vt.file_download: provider download route unavailable"
        ),
        PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE: (
            "vt.file_download: provider authorization refused"
        ),
    }
    for invocation in invocations:
        if (
            invocation.investigation_id != investigation_id
            or invocation.tool_name != "vt.download_sample"
            or invocation.status is not ToolInvocationStatus.UNAVAILABLE
        ):
            continue
        parameters = thaw_json(invocation.normalized_parameters)
        requested_hash = parameters.get("sha256") if isinstance(parameters, Mapping) else None
        identity = requested_hash if isinstance(requested_hash, str) else str(invocation.id)
        for ref in invocation.response_refs:
            observation = observation_by_id.get(ref)
            if observation is not None and observation.object_type in reason_details:
                hashes_by_reason.setdefault(observation.object_type, set()).add(identity)
    return tuple(
        reason_details[object_type]
        for object_type in sorted(reason_details)
        if len(hashes_by_reason.get(object_type, ())) >= PROVIDER_DOWNLOAD_REFUSAL_THRESHOLD
    )


def _coverage_parameter_key(invocation: ToolInvocation) -> str:
    parameters = thaw_json(invocation.normalized_parameters)
    if not isinstance(parameters, Mapping):
        return "parameters: unavailable"
    preferred_names = (
        "file_hash",
        "hash",
        "sha256",
        "sha1",
        "md5",
        "ip",
        "ip_address",
        "domain",
        "url",
        "host",
        "query",
    )
    for name in preferred_names:
        value = parameters.get(name)
        if isinstance(value, str) and value.strip():
            safe_value = _truncate(_safe_untrusted(value), 240)
            return f"{name}: {safe_value}"
    encoded = json.dumps(_sanitize(parameters), ensure_ascii=False, sort_keys=True)
    return f"parameters: {_truncate(_safe_untrusted(encoded), 240)}"


def _tool_coverage_summary(
    invocation: ToolInvocation,
    observation: EvidenceObservation | None,
    facts: Mapping[str, Any] | str | None,
) -> str:
    if (
        observation is not None
        and observation.object_type == PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE
    ):
        return "Provider authorization was refused."
    if (
        observation is not None
        and observation.object_type == PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE
    ):
        return "VirusTotal download route unavailable; do not retry downloads."
    if (
        observation is not None
        and observation.object_type == PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE
    ):
        return "VirusTotal download authorization refused; do not retry downloads."
    if observation is not None and observation.kind is EvidenceKind.NEGATIVE_RESULT:
        return "The provider returned an explicit no-result observation."

    values: Mapping[str, Any] = facts if isinstance(facts, Mapping) else {}
    if invocation.tool_name.startswith("vt."):
        nested = values.get("summary")
        if isinstance(nested, Mapping):
            values = nested
        stats = values.get("last_analysis_stats")
        parts: list[str] = []
        if isinstance(stats, Mapping):
            counts = {
                key: value
                for key, value in stats.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
            }
            detections = counts.get("malicious", 0) + counts.get("suspicious", 0)
            total = counts.get(
                "total", sum(value for key, value in counts.items() if key != "total")
            )
            if total:
                parts.append(f"{detections}/{total} detections")
        name = _first_safe_fact(values, ("meaningful_name", "name"), 96)
        file_type = _first_safe_fact(values, ("type_description", "type"), 72)
        if name:
            parts.append(f"file {name}")
        if file_type:
            parts.append(f"type {file_type}")
        if parts:
            return _truncate(_safe_untrusted("VT: " + "; ".join(parts)), 400)
    if invocation.tool_name.startswith("shodan."):
        nested = values.get("summary")
        if isinstance(nested, Mapping):
            values = nested
        ports = values.get("ports")
        parts = []
        if isinstance(ports, (tuple, list)):
            safe_ports = [
                str(value)
                for value in ports[:16]
                if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 65_535
            ]
            if safe_ports:
                parts.append("ports " + ",".join(safe_ports))
        organization = _first_safe_fact(values, ("organization", "org"), 96)
        if organization:
            parts.append("organization " + organization)
        if parts:
            return _truncate(_safe_untrusted("Shodan: " + "; ".join(parts)), 400)
    if observation is not None:
        return _truncate(
            _safe_untrusted(
                f"{observation.source} recorded {observation.kind.value} "
                f"for {observation.object_type} {observation.object_id}."
            ),
            400,
        )
    return f"{invocation.tool_name} returned {invocation.status.value}."


def _first_safe_fact(values: Mapping[str, Any], names: tuple[str, ...], maximum: int) -> str | None:
    for name in names:
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            return _truncate(_safe_untrusted(value), maximum)
    return None


def _prioritize_context_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    indexed = tuple(enumerate(rows))
    return tuple(
        row
        for _, row in sorted(
            indexed,
            key=lambda pair: (
                0 if pair[1].get("kind") == "documented_ioc" else 1,
                pair[0],
            ),
        )
    )


__all__ = [
    "MAX_OPERATOR_REQUESTS",
    "MAX_OPERATOR_REQUEST_BYTES",
    "ArticleSnapshot",
    "ArticleSnapshotPort",
    "CanonicalEvidenceSummaryPort",
    "EvidenceSummaryPort",
    "InvestigationGraphReadPort",
    "InvestigationReadPort",
    "RepositoryInvestigationGraphReadPort",
    "RepositoryInvestigationReadPort",
    "ResearchContextBuilder",
    "SampleFeaturePackPort",
    "provider_authorization_blocked_capabilities",
    "provider_download_blocked_capabilities",
]
