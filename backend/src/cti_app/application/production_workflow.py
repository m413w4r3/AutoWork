"""Main production workflow orchestration service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.analyst_vt_enrichment import VirusTotalSeedEnrichmentService
from cti_app.application.collection import SupplementalSource
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.extraction import _html_encoding, parse_document
from cti_app.application.iana_tlds_snapshot import IANA_TLD_SNAPSHOT_VERSION
from cti_app.application.jobs import JobCancelledError, JobExecutionContext
from cti_app.application.model_conversations import (
    ConversationTurnFailedError,
    ModelConversationService,
    conversation_close_failure_fields,
)
from cti_app.application.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_artifact_verification import (
    ARTIFACT_VERIFIER_VERSION,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_parsers import (
    PARSER_VERSION,
    Q2_EXTRACTION_CONTRACT_VERSION,
    Q2_MARKDOWN_PARSER_VERSION,
    IndicatorStatus,
    ParsedSource,
    ParseResult,
    Q2SourceOutput,
    ReferenceIntegrationResult,
    ReferenceReport,
    parse_q2_proposals_markdown,
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_from_json,
    reference_report_to_json,
    technical_extraction_from_json,
    technical_extraction_to_json,
    validate_synthesis,
)
from cti_app.application.production_prompts import (
    ARCHIVE_FALLBACK_PROMPT_VERSION,
    ARCHIVED_SOURCE_ACCESS_VERSION,
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_PROMPT_VERSION_BY_PROFILE,
    IOC_RULES_BATCH_PROMPT_VERSION,
    IOC_RULES_PROMPT_VERSION,
    REFERENCES_FORMAT_REPAIR_VERSION,
    REFERENCES_PROMPT_VERSION,
    SYNTHESIS_FORMAT_REPAIR_VERSION,
    SYNTHESIS_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_q2_batch import (
    Q2_BATCH_PARSER_VERSION,
    Q2BatchCandidate,
    Q2BatchSource,
    make_q2_batch,
    parse_q2_batch_response,
    partition_q2_batch_candidates,
    q2_batch_model_run_id,
)
from cti_app.application.production_recovery import ProductionRecoveryPolicyV1
from cti_app.application.production_repairs import (
    build_repair_evidence_pack,
    repair_key_for_rejection,
)
from cti_app.application.production_source_evidence import (
    SOURCE_EVIDENCE_VERSION,
    SourceEvidenceDocument,
    SourceEvidenceResult,
    source_evidence_document_from_html,
    verify_ioc_rules_output_against_source,
    verify_q2_output_against_source,
)
from cti_app.application.production_stages import (
    ExtractionService,
    ProductionQAService,
    PublicationAssemblyService,
    ReferenceResearchService,
    SynthesisService,
    compute_input_hash,
)
from cti_app.config import get_settings
from cti_app.domain.collection import CollectionState, DetectedMimeType, SourceOriginKind
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationTransport,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRunStatus
from cti_app.domain.production import (
    ExtractionProfile,
    ProductionArtifactStage,
    ProductionInputSnapshot,
    ProductionRepairIssueKind,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.domain.publication import is_publication_ioc_artifact_type

if TYPE_CHECKING:
    from cti_app.application.collection import SubjectCollectionService


# Collection states that count as "the source is available for analysis".
_ARCHIVED_STATES = {"archived", "extracted", "completed"}

# Version routing decision separately from prompt/schema: changing provider policy
# must produce a distinct persisted Q2 checkpoint.
# "4": Q2 uses direct stateless ModelGateway requests against the live
# publication; IOC_RULES may group several exact URLs in one web batch. Its
# deterministic ModelRun identities include this routing policy, so changing it
# creates fresh executions without conversations or repair turns.
Q2_ROUTING_POLICY_VERSION = "4"
# The provider/model selection is part of the run-local checkpoint contract.
# Keep this separate from the routing policy so a model-policy change can
# invalidate Q2 reuse without changing the live-web request format.
Q2_MODEL_POLICY_VERSION = "openai-web-research-v1"
Q2_SUCCESSFUL_CHECKPOINT_VERSION = "q2-cross-run-v2"
REFERENCES_ROUTING_POLICY_VERSION = "openai-web-research-v1"

# Une fermeture d'onglet qui tombe pendant une éviction du service worker MV3
# réussit quelques secondes plus tard.
_CONVERSATION_CLOSE_RETRY_DELAY_SECONDS = 5.0

# Functional content of the Q4 evidence pack. Bumped whenever what Q4 can read
# changes, so a cached synthesis built on an older pack is never reused.
SYNTHESIS_EVIDENCE_PACK_VERSION = "7"

# Keep archive reads within the same decoded-document limit as collection and
# deterministic source processing. This is a local proof read, never prompt
# material.
MAX_ARCHIVED_SOURCE_BYTES = 25 * 1024 * 1024
# The gateway's persisted text contract is capped at 10 MB. An archive
# fallback is either sent as one complete request or not sent at all.
MAX_Q2_ARCHIVE_FALLBACK_PROMPT_BYTES = 10_000_000

# Bridge and network hiccups are worth retrying; anything else is a dead end
# for this attempt and must not silently burn the subject.
_TRANSIENT_CODES = {
    "bridge_server_error",
    "bridge_idle_timeout",
    "bridge_total_timeout",
    "bridge_timeout",
    "bridge_ui_timeout",
    "bridge_extension_disconnected",
    "bridge_unreachable",
    "bridge_rate_limited",
}

# The conversation itself is the problem, not the pipeline: retrying the same
# turn cannot help, but nothing is broken either.
_REVIEW_CODES = {
    "conversation_unavailable",
    "conversation_profile_mismatch",
    "conversation_busy",
    "external_llm_blocked",
}

_MODEL_SUBMISSION_RECONCILIATION_CODE = "model_submission_reconciliation_required"
_SOURCE_CONTENT_CODES = frozenset({"source_content_invalid", "q2_source_evidence_unavailable"})


def _gate_archived_q2_output(
    output: Q2SourceOutput,
    *,
    source_text: str | SourceEvidenceDocument,
    profile: ExtractionProfile,
) -> SourceEvidenceResult:
    """Apply the profile-specific source-local gate to one parsed output."""
    if profile is ExtractionProfile.FULL:
        return verify_q2_output_against_source(output, source_text)
    if profile is ExtractionProfile.IOC_RULES:
        return verify_ioc_rules_output_against_source(output, source_text)
    raise ValueError(f"Unsupported extraction profile: {profile}")


class _Q2FailureClass(StrEnum):
    GLOBAL_TRANSIENT_PRE_SUBMISSION = "global_transient_pre_submission"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SOURCE_CONTENT_FAILURE = "source_content_failure"
    CONTROL_INVARIANT_FAILURE = "control_invariant_failure"


@dataclass(frozen=True, slots=True)
class _Q2FailureClassification:
    failure_class: _Q2FailureClass
    status: str
    error_code: str
    retryable: bool
    phase: str
    submission_state: str
    contributes_to_coverage: bool


class _Q2SourceContentFailure(ValueError):
    code = "source_content_invalid"
    retryable = False
    phase = "response_validation"
    submission_state = "post_submission"


class _Q2SourceEvidenceUnavailable(_Q2SourceContentFailure):
    """A live Q2 response cannot be locally validated for its source."""

    code = "q2_source_evidence_unavailable"

    def __init__(
        self,
        reason: str,
        *,
        expected_sha256: str | None = None,
        blob_id: UUID | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(reason)
        if code:
            self.code = code[:64]
        self.details = {
            "reason": reason,
            **({"expected_decoded_sha256": expected_sha256} if expected_sha256 is not None else {}),
            **({"decoded_blob_id": str(blob_id)} if blob_id is not None else {}),
        }


class _Q2LiveSourceUnavailable(_Q2SourceContentFailure):
    """The model explicitly reported that the exact live source was unavailable."""

    code = "q2_source_unavailable"


def _is_q2_source_unavailable(errors: Sequence[str]) -> bool:
    """Recognize only the parser's explicit terminal live-source response."""
    return tuple(errors) == ("q2_source_unavailable",)


class _Q2ControlFailure(RuntimeError):
    code = "q2_provider_response_missing"
    retryable = False
    phase = "model_call"
    submission_state = "post_submission"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code[:64]
        self.details = details or {}


def _classify_q2_failure(
    exc: Exception,
    *,
    provider_response_produced: bool = False,
) -> _Q2FailureClassification:
    """Classify one Q2 source failure before it can affect coverage.

    The caller supplies only the fact that a provider response was available;
    submission safety remains the ModelGateway's responsibility.  In
    particular, a previously persisted ModelRun error is always a control or
    reconciliation outcome, never a source-content failure.
    """
    details = getattr(exc, "details", None)
    detail_state = details.get("submission_state") if isinstance(details, dict) else None
    detail_phase = details.get("phase") if isinstance(details, dict) else None
    code = str(getattr(exc, "code", "") or "")
    retryable = bool(getattr(exc, "retryable", False))
    phase = str(getattr(exc, "phase", None) or detail_phase or "model_call")[:64]
    submission_state = str(getattr(exc, "submission_state", None) or detail_state or "unknown")[:32]

    if code == _MODEL_SUBMISSION_RECONCILIATION_CODE:
        return _Q2FailureClassification(
            _Q2FailureClass.RECONCILIATION_REQUIRED,
            "needs_review",
            code,
            False,
            "reconciliation",
            submission_state,
            False,
        )

    # The HTTP client raises bridge_unreachable on a failed connection before
    # it can send bytes, so this code is a safe pre-submit transport signal
    # even though that low-level exception has no explicit state field.  The
    # gateway remains the authority that persists NOT_SUBMITTED.
    if (
        submission_state == "pre_submission"
        or (submission_state == "unknown" and code == "bridge_unreachable")
    ) and (retryable or code in _TRANSIENT_CODES):
        return _Q2FailureClassification(
            _Q2FailureClass.GLOBAL_TRANSIENT_PRE_SUBMISSION,
            "transient_error",
            code or "q2_global_transient",
            True,
            phase,
            submission_state,
            False,
        )

    # An explicit source-content code is accepted only after submission.  A
    # ModelGatewayError from a checkpoint state has no such proof and falls
    # through to the control/invariant class below.
    if (
        not retryable
        and code in _SOURCE_CONTENT_CODES
        and submission_state in {"post_submission", "submitted_or_unknown"}
    ) or (provider_response_produced and not retryable and not isinstance(exc, ModelGatewayError)):
        return _Q2FailureClassification(
            _Q2FailureClass.SOURCE_CONTENT_FAILURE,
            "source_failure",
            code if code in _SOURCE_CONTENT_CODES else "source_content_invalid",
            False,
            phase,
            submission_state,
            True,
        )

    if submission_state in {"submission_attempted", "submitted_or_unknown", "post_submission"} and (
        retryable or code in _TRANSIENT_CODES
    ):
        return _Q2FailureClassification(
            _Q2FailureClass.RECONCILIATION_REQUIRED,
            "needs_review",
            _MODEL_SUBMISSION_RECONCILIATION_CODE,
            False,
            "reconciliation",
            submission_state,
            False,
        )

    return _Q2FailureClassification(
        _Q2FailureClass.CONTROL_INVARIANT_FAILURE,
        "needs_review",
        code or "q2_control_failure",
        False,
        phase,
        submission_state,
        False,
    )


def _transient_or_terminal(stage: str, exc: Exception) -> dict[str, Any]:
    code = str(getattr(exc, "code", "") or "")
    retryable = bool(getattr(exc, "retryable", False))
    if isinstance(exc, ConversationTurnFailedError) and exc.status.value == "needs_review":
        status = "needs_review"
    elif code == _MODEL_SUBMISSION_RECONCILIATION_CODE or code in _REVIEW_CODES:
        # The conversation is gone or busy: an operator has to look, but the
        # subject is not corrupted and the batch must keep moving.
        status = "needs_review"
    elif retryable or code in _TRANSIENT_CODES:
        status = "transient_error"
    else:
        status = "terminal_error"
    result = {
        "stage": stage,
        "status": status,
        "error_code": code or f"{stage}_failed",
        "error": str(exc),
        "details": getattr(exc, "details", None),
    }
    model_run_id = getattr(exc, "model_run_id", None)
    if isinstance(model_run_id, UUID):
        result["model_run_id"] = str(model_run_id)
    return result


def _supplemental_failure_fields(
    exc: Exception,
    *,
    canonical_url: str | None = None,
    collection_state: str | None = None,
    retry_attempted: bool = False,
) -> dict[str, Any]:
    """Keep structured collection failure context without exposing raw values."""
    raw_details = getattr(exc, "details", None)
    details = dict(raw_details) if isinstance(raw_details, dict) else {}
    fields: dict[str, Any] = {}
    if canonical_url is not None:
        fields["canonical_url"] = canonical_url
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        fields["error_code"] = code
    if collection_state is not None:
        fields["collection_state"] = collection_state
    retryable = getattr(exc, "retryable", None)
    if retryable is None:
        retryable = getattr(exc, "transient", None)
    if "failed_retryable" in details:
        retryable = bool(details["failed_retryable"])
    if isinstance(retryable, bool):
        fields["retryable"] = retryable
    if retry_attempted:
        fields["retry_attempted"] = True
    for key in ("failed_retryable", "blocked", "unavailable", "failed_terminal"):
        if key in details:
            fields[key] = details[key]
    if details:
        fields["details"] = details
    fields["error_type"] = type(exc).__name__
    message = str(exc).strip()
    if message and message != fields.get("error_code"):
        fields["error_message"] = message[:1000]
    return fields


def _supplemental_failure_warning(
    exc: Exception,
    *,
    canonical_url: str | None = None,
) -> str:
    """Render a compact UI warning; full fields stay in structured diagnostics."""
    raw_details = getattr(exc, "details", None)
    details = raw_details if isinstance(raw_details, dict) else {}
    parts = ["supplemental_collection_failed"]
    if canonical_url is not None:
        parts.append(f"url={canonical_url}")
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        parts.append(f"code={code}")
    else:
        parts.append(f"type={type(exc).__name__}")
    retryable = getattr(exc, "retryable", None)
    if retryable is None:
        retryable = getattr(exc, "transient", None)
    if isinstance(retryable, bool) and not any(
        key in details for key in ("failed_retryable", "blocked", "unavailable", "failed_terminal")
    ):
        parts.append(f"retryable={int(retryable)}")
    for key in ("failed_retryable", "blocked", "unavailable", "failed_terminal"):
        if key in details:
            parts.append(f"{key}={details[key]}")
    return ":".join(parts)


def _repair_problem_descriptions(result: Any) -> list[str]:
    """Describe the parse failures precisely enough for a repair turn.

    Bare error codes collapse several distinct violations into the same line
    and hide which value must be rewritten. When the parse result carries
    violations, expose ``code: detail``.
    """
    violations = getattr(result, "violations", None) or ()
    described: list[str] = []
    for violation in violations:
        code = getattr(violation, "code", "")
        if not code:
            continue
        detail = " ".join((getattr(violation, "detail", "") or "").split())
        if len(detail) > 200:
            detail = f"{detail[:200]}…"
        described.append(f"{code}: {detail}" if detail else code)
    if described:
        # Chaque occurrence distincte doit rester visible : deux violations du
        # même code portent sur deux valeurs différentes à réécrire.
        return list(dict.fromkeys(described))[:12]
    return list(getattr(result, "errors", ()) or ())


@dataclass(frozen=True, slots=True)
class Q2SourcePlan:
    """Deterministic profile assignment for one Q1 publication."""

    source_id: str
    canonical_url: str
    profile: ExtractionProfile
    reason: str


def plan_q2_extraction_profiles(
    report: ReferenceReport,
    *,
    snapshot: ProductionInputSnapshot | None = None,
    period_start: date | str | None = None,
    period_end: date | str | None = None,
) -> tuple[Q2SourcePlan, ...]:
    """Assign FULL only to frozen core sources; all supporting sources use IOC_RULES."""

    if snapshot is None:
        raise ValueError("q2_extraction_plan_missing_snapshot")

    core_urls = {source.canonical_url for source in snapshot.core_sources}

    return tuple(
        Q2SourcePlan(
            source_id=source.local_id,
            canonical_url=source.canonical_url,
            profile=(
                ExtractionProfile.FULL
                if source.canonical_url in core_urls
                else ExtractionProfile.IOC_RULES
            ),
            reason=("core_source" if source.canonical_url in core_urls else "supporting_source"),
        )
        for source in report.sources
    )


# A descriptive alias keeps the policy easy to discover from callers/tests.
select_q2_extraction_profiles = plan_q2_extraction_profiles


_EXTRACTION_PROGRESS_COMPLETED_STATUSES = {"cached", "succeeded"}


def _batch_candidate(source: ParsedSource) -> Q2BatchCandidate | None:
    """Return the batch candidate for an IOC_RULES source, when it has a URL."""
    try:
        return Q2BatchCandidate(source=source)
    except ValueError:
        # Without an exact HTTP(S) URL there is nothing to open: the source
        # keeps its own individual request.
        return None


def _new_extraction_progress(
    report: ReferenceReport,
    plans: tuple[Q2SourcePlan, ...],
) -> dict[str, Any]:
    plans_by_url = {plan.canonical_url: plan for plan in plans}
    sources = [
        {
            "source_id": source.local_id,
            "title": source.title,
            "profile": plans_by_url[source.canonical_url].profile.value,
            "status": "pending",
            "ioc_count": 0,
            "rule_count": 0,
        }
        for source in report.sources
    ]
    full_total = sum(source["profile"] == ExtractionProfile.FULL.value for source in sources)
    ioc_rules_total = sum(
        source["profile"] == ExtractionProfile.IOC_RULES.value for source in sources
    )
    return {
        "total_sources": len(sources),
        "completed_sources": 0,
        "full_total": full_total,
        "full_completed": 0,
        "ioc_rules_total": ioc_rules_total,
        "ioc_rules_completed": 0,
        "cache_hits": 0,
        "model_calls": 0,
        "skipped_sources": 0,
        "light_batches": 0,
        "light_sources_batched": 0,
        "confirmed_iocs": 0,
        "contextual_iocs": 0,
        "rules_total": 0,
        "yara_rules": 0,
        "sigma_rules": 0,
        "suricata_rules": 0,
        "snort_rules": 0,
        "active_source_id": None,
        "active_source_title": None,
        "active_profile": None,
        "source_skips": {},
        "sources": sources,
    }


def _enforce_q2_profile(
    output: Q2SourceOutput,
    profile: ExtractionProfile,
) -> tuple[Q2SourceOutput, tuple[str, ...]]:
    """Apply the planner's output contract before canonical verification."""
    if profile is ExtractionProfile.FULL:
        return output, ()
    if profile is not ExtractionProfile.IOC_RULES:
        raise ValueError(f"Unsupported extraction profile: {profile}")
    if not output.facts:
        return output, ()
    return (
        Q2SourceOutput(
            facts=[],
            artifacts=list(output.artifacts),
            rules=list(output.rules),
            uncertainties=list(output.uncertainties),
        ),
        ("q2_ioc_rules_fact_dropped",),
    )


def _canonical_extraction_progress_counts(extraction: Any) -> dict[str, int]:
    """Count only deterministic canonical extraction objects."""
    items = getattr(extraction, "items", ())
    rules = getattr(extraction, "rules", ())
    counts = {
        "confirmed_iocs": sum(
            item.artifact_type is not None
            and item.indicator_status is IndicatorStatus.CONFIRMED_IOC
            for item in items
        ),
        "contextual_iocs": sum(
            item.artifact_type is not None and item.indicator_status is IndicatorStatus.CONTEXTUAL
            for item in items
        ),
        "rules_total": len(rules),
        "yara_rules": sum(rule.rule_type.value == "yara" for rule in rules),
        "sigma_rules": sum(rule.rule_type.value == "sigma" for rule in rules),
        "suricata_rules": sum(rule.rule_type.value == "suricata" for rule in rules),
        "snort_rules": sum(rule.rule_type.value == "snort" for rule in rules),
    }
    return counts


def _source_progress_counts(output: Any, source_id: str) -> dict[str, int]:
    """Count deterministically accepted proposals from one parsed source."""
    verified = verify_q2_proposals([Q2ProposalSubmission(output=output, source_ids=(source_id,))])
    counts = _canonical_extraction_progress_counts(verified.canonical)
    return {
        **counts,
        "ioc_count": counts["confirmed_iocs"] + counts["contextual_iocs"],
        "rule_count": counts["rules_total"],
    }


def _progress_source(
    progress: dict[str, Any],
    source_id: str,
) -> dict[str, Any]:
    for source in cast(list[dict[str, Any]], progress["sources"]):
        if source["source_id"] == source_id:
            return source
    raise ValueError(f"Unknown extraction progress source {source_id}")


def _mark_extraction_source_running(
    progress: dict[str, Any],
    source: ParsedSource,
    plan: Q2SourcePlan,
) -> None:
    entry = _progress_source(progress, source.local_id)
    entry["status"] = "running"
    progress["active_source_id"] = source.local_id
    progress["active_source_title"] = source.title
    progress["active_profile"] = plan.profile.value


def _mark_extraction_source_complete(
    progress: dict[str, Any],
    source: ParsedSource,
    *,
    status: str,
    counts: dict[str, int],
    cache_hit: bool = False,
) -> None:
    entry = _progress_source(progress, source.local_id)
    was_complete = entry["status"] in _EXTRACTION_PROGRESS_COMPLETED_STATUSES
    entry["status"] = status
    entry["ioc_count"] = counts["ioc_count"]
    entry["rule_count"] = counts["rule_count"]
    if not was_complete:
        progress["confirmed_iocs"] += counts.get("confirmed_iocs", 0)
        progress["contextual_iocs"] += counts.get("contextual_iocs", 0)
        progress["rules_total"] += counts.get("rules_total", 0)
        progress["yara_rules"] += counts.get("yara_rules", 0)
        progress["sigma_rules"] += counts.get("sigma_rules", 0)
        progress["suricata_rules"] += counts.get("suricata_rules", 0)
        progress["snort_rules"] += counts.get("snort_rules", 0)
    if cache_hit:
        progress["cache_hits"] += 1
    progress["completed_sources"] = sum(
        item["status"] in _EXTRACTION_PROGRESS_COMPLETED_STATUSES
        for item in cast(list[dict[str, Any]], progress["sources"])
    )
    progress["full_completed"] = sum(
        item["profile"] == ExtractionProfile.FULL.value
        and item["status"] in _EXTRACTION_PROGRESS_COMPLETED_STATUSES
        for item in cast(list[dict[str, Any]], progress["sources"])
    )
    progress["ioc_rules_completed"] = sum(
        item["profile"] == ExtractionProfile.IOC_RULES.value
        and item["status"] in _EXTRACTION_PROGRESS_COMPLETED_STATUSES
        for item in cast(list[dict[str, Any]], progress["sources"])
    )


def _mark_extraction_source_failed(
    progress: dict[str, Any],
    source_id: str,
    status: str,
) -> None:
    _progress_source(progress, source_id)["status"] = status


def _mark_extraction_source_skipped(
    progress: dict[str, Any],
    source_id: str,
    details: dict[str, Any],
) -> None:
    """Record a non-blocking source skip without changing failure counters."""
    _progress_source(progress, source_id)["status"] = "skipped"
    _progress_source(progress, source_id)["skip"] = details
    source_skips = progress.setdefault("source_skips", {})
    if isinstance(source_skips, dict) and source_id not in source_skips:
        source_skips[source_id] = details
    progress["skipped_sources"] = sum(
        item["status"] == "skipped" for item in cast(list[dict[str, Any]], progress["sources"])
    )


@dataclass(frozen=True, slots=True)
class _ArchivedSource:
    """The archived capture backing one Q1 source, as held by this system."""

    content_sha256: str
    decoded_blob_id: UUID | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class _Q2SourceWork:
    """One planned Q2 source, with its collection provenance for diagnostics."""

    source: ParsedSource
    plan: Q2SourcePlan
    source_content_sha256: str | None


@dataclass(frozen=True, slots=True)
class _Q2ReusableSource:
    """A source-local view of a successful run-local Q2 response."""

    output: Q2SourceOutput
    raw: str
    model_run_id: UUID
    warnings: tuple[str, ...] = ()


class BlobContentReader(Protocol):
    """Narrow read port over the canonical blob catalog."""

    async def read_blob(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


async def _archived_sources_by_url(
    uow: UnitOfWork, subject_id: UUID, report: ReferenceReport
) -> dict[str, _ArchivedSource]:
    """Resolve the local capture identity needed by the post-response gate."""
    collections_repository = getattr(uow, "source_collections", None)
    documents_repository = getattr(uow, "source_documents", None)
    if collections_repository is None or documents_repository is None:
        return {}

    collections = await collections_repository.list_for_subject(subject_id)
    documents = await documents_repository.list_for_subject(subject_id)
    by_id = {document.id: document for document in documents}
    by_url = {
        document.final_url: document
        for document in documents
        if getattr(document, "final_url", None)
    }
    archived: dict[str, _ArchivedSource] = {}
    for source in report.sources:
        collection = next(
            (item for item in collections if item.canonical_url == source.canonical_url),
            None,
        )
        document = (
            by_id.get(collection.source_document_id)
            if collection is not None and collection.source_document_id is not None
            else by_url.get(source.canonical_url)
        )
        content_hash = getattr(document, "decoded_sha256", None)
        if not isinstance(content_hash, str):
            continue
        normalized_hash = content_hash.casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
            continue
        decoded_blob_id = getattr(collection, "decoded_blob_id", None) or getattr(
            document, "decoded_blob_id", None
        )
        archived[source.canonical_url] = _ArchivedSource(
            content_sha256=normalized_hash,
            decoded_blob_id=decoded_blob_id if isinstance(decoded_blob_id, UUID) else None,
            mime_type=getattr(document, "detected_mime_type", None),
        )
    return archived


class ProductionWorkflowOrchestrator:
    """Orchestrates the single article publication workflow."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        model_service: ModelConversationService | None = None,
        model_gateway: ModelGateway | None = None,
        collection_service: SubjectCollectionService | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        diagnostics: DiagnosticsLog | None = None,
        seed_enrichment: VirusTotalSeedEnrichmentService | None = None,
        pacing: ProductionPacingPolicy | None = None,
        blob_reader: BlobContentReader | None = None,
        q2_reuse_max_age_days: float = 14.0,
    ) -> None:
        self._uow_factory = uow_factory
        # The collection service owns the canonical blob catalog used for
        # archived captures. The artifact store is the equivalent fallback for
        # callers that do not construct a collection service. Q2 only reads
        # from either after a live model response.
        self._blob_reader: BlobContentReader | None = blob_reader or cast(
            "BlobContentReader | None", collection_service or artifact_store
        )
        self._model_service = model_service
        self._model_gateway = model_gateway or getattr(model_service, "_gateway", None)
        self._collection_service = collection_service
        self._artifact_store = artifact_store
        self._diagnostics = diagnostics or DiagnosticsLog(None)
        self._correlation_id = "-"
        production_uow_factory = cast(Any, uow_factory)
        self._references = ReferenceResearchService(production_uow_factory, artifact_store)
        self._extraction = ExtractionService(production_uow_factory, artifact_store)
        self._synthesis = SynthesisService(production_uow_factory, artifact_store)
        self._assembly = PublicationAssemblyService(production_uow_factory, artifact_store)
        self._artifact_reuse = ProductionArtifactReuseService(
            production_uow_factory, artifact_store, self._diagnostics
        )
        self._qa = ProductionQAService(production_uow_factory)
        self._seed_enrichment = seed_enrichment
        self._pacing = pacing or ProductionPacingPolicy.zero()
        self._q2_reuse_max_age_days = q2_reuse_max_age_days
        self._settings = get_settings()

    async def _check_cancellation(self, run_id: UUID, context: JobExecutionContext | None) -> None:
        """Fence both the job cancellation flag and the persistent run state."""
        if context is not None:
            await context.check_cancelled()
        uow_factory = getattr(self, "_uow_factory", None)
        if uow_factory is None:
            return
        async with uow_factory() as uow:
            runs = getattr(uow, "subject_production_runs", None)
            if runs is None:
                return
            run = await runs.get(run_id)
        if run is not None and run.status is SubjectProductionStatus.CANCELLED:
            raise JobCancelledError

    async def _persist_extraction_progress(
        self,
        run_id: UUID,
        progress: dict[str, Any],
    ) -> None:
        """Write one compact progress snapshot in its own short transaction."""
        async with self._uow_factory() as uow:
            runs = getattr(uow, "subject_production_runs", None)
            if runs is None:
                return
            get_for_update = getattr(runs, "get_for_update", None)
            get_run = get_for_update or getattr(runs, "get", None)
            if get_run is None:
                return
            persisted = await get_run(run_id)
            if persisted is None:
                return
            persisted.set_extraction_progress(progress)
            save = getattr(runs, "save", None)
            if save is not None:
                await save(persisted)
            commit = getattr(uow, "commit", None)
            if commit is not None:
                await commit()

    async def _close_completed_stage_conversation_best_effort(
        self,
        run: SubjectProductionRun,
        stage: SubjectProductionStage,
    ) -> None:
        """Archive the model conversation after a durable stage result.

        Archiving also asks the bridge to close the exact bound browser tab.
        Cleanup is deliberately best-effort: the stage artifact is already
        durable, so a browser cleanup failure must not turn a successful stage
        into a production failure.
        """
        model_service = getattr(self, "_model_service", None)
        if model_service is None:
            return

        conversation_id: UUID | None = None
        if stage is SubjectProductionStage.REFERENCES:
            conversation_id = run.references_conversation_id
        elif stage is SubjectProductionStage.SYNTHESIS:
            conversation_id = run.synthesis_conversation_id

        if conversation_id is None:
            return

        try:
            await model_service.archive(
                conversation_id,
                context_subject_id=run.subject_id,
            )
        except Exception as first_failure:
            first_failure_fields = conversation_close_failure_fields(first_failure)
            failure_code = first_failure_fields.get("error_code")
            # ModelConversationService wraps the bridge error to preserve the
            # durable archive/close boundary; recover from its typed cause.
            if failure_code == "conversation_session_close_failed":
                failure_code = first_failure_fields.get("cause_code")
            retry_allowed = ProductionRecoveryPolicyV1.is_auto_recoverable(failure_code)
            exc: Exception | None = first_failure
            if retry_allowed:
                # Le service worker MV3 de l'extension est évincé environ
                # toutes les trois minutes : une fermeture qui tombe dans
                # cette fenêtre réussit à la tentative suivante.
                await asyncio.sleep(_CONVERSATION_CLOSE_RETRY_DELAY_SECONDS)
                try:
                    await model_service.archive(
                        conversation_id,
                        context_subject_id=run.subject_id,
                    )
                    exc = None
                except Exception as second_failure:
                    exc = second_failure
            if exc is not None:
                failure = conversation_close_failure_fields(exc)
                self._diagnostics.record(
                    event="production.conversation_close_failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage=stage.value,
                    correlation_id=self._correlation_id,
                    conversation_id=str(conversation_id),
                    error_type=type(exc).__name__,
                    error_code=failure["error_code"],
                    retryable=failure.get("retryable"),
                    phase=failure.get("phase"),
                    cause_code=failure.get("cause_code"),
                    reason=failure.get("reason"),
                    details=failure["details"],
                    attempts=2 if retry_allowed else 1,
                    error=str(exc)[:512],
                    error_message=str(exc)[:512],
                )

    async def execute_stage(
        self,
        run_id: UUID,
        expected_stage: SubjectProductionStage,
        context: JobExecutionContext | None = None,
        correlation_id: str = "-",
    ) -> dict[str, Any]:
        """Idempotent: if stage is already complete, returns cached result."""
        self._correlation_id = correlation_id

        # Read the run without locking it. A stage spans a full model
        # round-trip; holding `FOR UPDATE` on the run for that long deadlocks
        # the stage against itself as soon as it opens its own unit of work,
        # and blocks the batch besides. State transitions take their own short
        # lock, in the job handler.
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get(run_id)
            snapshot = (
                await uow.production_input_snapshots.get_by_run(run.id) if run is not None else None
            )
        if not run:
            raise ValueError(f"Production run {run_id} not found")
        if snapshot is None:
            raise RuntimeError("production_input_snapshot_missing")

        await self._check_cancellation(run.id, context)

        if run.current_stage != expected_stage:
            raise ValueError(
                f"Run on stage {run.current_stage.value}, expected {expected_stage.value}"
            )

        try:
            if expected_stage == SubjectProductionStage.SOURCES:
                result = await self._execute_sources_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.REFERENCES:
                result = await self._execute_references_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.EXTRACTION:
                result = await self._execute_extraction_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.SYNTHESIS:
                result = await self._execute_synthesis_stage(run, context, snapshot)
            elif expected_stage == SubjectProductionStage.ASSEMBLY:
                result = await self._execute_assembly_stage(run, context, snapshot)
            else:
                raise ValueError(f"Unknown stage: {expected_stage.value}")
        except ProductionReuseStorageUnavailableError as exc:
            result = self._handle_stage_exception(run, expected_stage.value, exc)

        if result.get("status") in {"success", "cached", "reused"}:
            await self._close_completed_stage_conversation_best_effort(run, expected_stage)

        self._diagnostics.record_stage_outcome(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=expected_stage.value,
            correlation_id=correlation_id,
            result=result,
        )
        return result

    def _handle_stage_exception(
        self, run: SubjectProductionRun, stage: str, exc: Exception
    ) -> dict[str, Any]:
        """Preserves the original exception's traceback in diagnostics before
        converting it to a safe error result for the caller."""
        self._diagnostics.record_failure(
            event="stage.exception",
            run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            correlation_id=self._correlation_id,
            error=exc,
        )
        return _transient_or_terminal(stage, exc)

    async def _reuse_artifact(
        self,
        run: SubjectProductionRun,
        stage: str,
        input_hash: str,
    ) -> dict[str, Any] | None:
        artifact_stage = ProductionArtifactStage(stage)
        result = await self._artifact_reuse.find_or_reuse(
            run=run,
            stage=artifact_stage,
            input_hash=input_hash,
            allow_cross_run=cross_run_reuse_allowed(run, artifact_stage),
        )
        if result is None:
            return None
        return {
            "stage": stage,
            "status": "reused" if result.reused else "cached",
            "artifact_id": str(result.artifact.id),
            "reused": result.reused,
            "reused_from_artifact_id": (
                str(result.artifact.reused_from_artifact_id)
                if result.artifact.reused_from_artifact_id is not None
                else None
            ),
        }

    async def _ask_with_format_repair(
        self,
        *,
        run: SubjectProductionRun,
        conversation_id: UUID,
        stage: str,
        prompt: str,
        prompt_version: str,
        repair_version: str,
        mode: ConversationMode,
        parse: Callable[[str], Any],
        external_llm_allowed: bool,
        web_search: bool = False,
        request_identity: str | None = None,
        lifecycle_policy: ConversationPolicy = ConversationPolicy.KEEP,
        context: JobExecutionContext | None = None,
    ) -> tuple[Any | None, str, UUID | None, UUID | None]:
        """Ask the model, and give it exactly one chance to fix its formatting.

        Used by Q1 (references) and Q4 (synthesis): both draft FRESH with web
        search, then repair CONTINUE without web search — the repair turn
        never researches again, it restates the same answer in the expected
        structure. Returns the parse result, the raw text used, and the turn
        id it came from.
        """
        assert self._model_service is not None
        identity = f"-{request_identity}" if request_identity else ""
        idempotency_key = f"{stage}-{run.id}-v{prompt_version}{identity}"
        await self._check_cancellation(run.id, context)
        turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=prompt,
            mode=mode,
            external_llm_allowed=external_llm_allowed,
            web_search=web_search,
            idempotency_key=idempotency_key,
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
            lifecycle_policy=lifecycle_policy,
        )
        model_run_id = getattr(turn, "model_run_id", None)
        raw = await self._turn_output_text(conversation_id, turn.id) or ""
        await self._check_cancellation(run.id, context)
        self._diagnostics.record_model_answer(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            correlation_id=self._correlation_id,
            prompt=prompt,
            answer=raw,
            idempotency_key=idempotency_key,
        )
        if not raw:
            return None, "", turn.id, model_run_id

        result = parse(raw)
        self._log_parse(run, stage, result)
        if result.usable:
            return result, raw, turn.id, model_run_id

        repair_prompt = ProductionPromptTemplates.get_format_repair_prompt(
            stage=stage, problems=_repair_problem_descriptions(result)
        )
        repair_idempotency_key = (
            f"{stage}-format-repair-{run.id}-v{prompt_version}{identity}-rv{repair_version}"
            if request_identity
            else f"{stage}-format-repair-{run.id}-v{repair_version}"
        )
        await self._check_cancellation(run.id, context)
        repair_turn = await self._model_service.add_turn(
            conversation_id=conversation_id,
            message=repair_prompt,
            mode=ConversationMode.CONTINUE,
            external_llm_allowed=external_llm_allowed,
            web_search=False,
            idempotency_key=repair_idempotency_key,
            correlation_id=self._correlation_id,
            context_subject_id=run.subject_id,
        )
        repair_model_run_id = getattr(repair_turn, "model_run_id", None)
        repaired_raw = await self._turn_output_text(conversation_id, repair_turn.id) or ""
        await self._check_cancellation(run.id, context)
        self._diagnostics.record_model_answer(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=f"{stage}-repair",
            correlation_id=self._correlation_id,
            prompt=repair_prompt,
            answer=repaired_raw,
            idempotency_key=repair_idempotency_key,
        )
        if not repaired_raw:
            return result, raw, turn.id, model_run_id

        repaired = parse(repaired_raw)
        self._log_parse(run, f"{stage}-repair", repaired)
        repaired.repair_actions.append(f"{stage}_format_repair")
        repaired.warnings.extend(result.errors)
        return repaired, repaired_raw, repair_turn.id, repair_model_run_id

    async def _integrate_reference_sources(
        self,
        run: SubjectProductionRun,
        report: ReferenceReport,
        context: JobExecutionContext | None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> ReferenceIntegrationResult:
        """Attach, collect and archive the publications Q1 proposed.

        An event survives if at least one of its sources ended up archived; a
        URL already attached to the subject is reused, never re-downloaded.
        """
        warnings: list[str] = []
        supplemental_failures: list[dict[str, Any]] = []
        new_sources = 0

        if self._collection_service is not None:
            supplemental = [
                SupplementalSource(
                    url=source.canonical_url,
                    title=source.title or None,
                    publisher=source.publisher,
                    published_at=source.published_at,
                    role=source.role,
                )
                for source in report.sources
            ]
            try:
                await self._check_cancellation(run.id, context)
                added = await self._collection_service.add_supplemental_sources(
                    run.subject_id, supplemental
                )
                new_sources = len(added)
                if context is not None:
                    report_urls = {source.canonical_url for source in report.sources}
                    for collection in await self._collection_service.list_sources(run.subject_id):
                        if (
                            collection.canonical_url not in report_urls
                            or collection.state.value in _ARCHIVED_STATES
                        ):
                            continue
                        collection_state = getattr(collection.state, "value", collection.state)
                        retry_attempted = False
                        try:
                            await self._check_cancellation(run.id, context)
                            if (
                                collection_state == CollectionState.FAILED_RETRYABLE.value
                                and callable(
                                    getattr(self._collection_service, "prepare_retry", None)
                                )
                            ):
                                # One targeted retry is allowed for a retryable
                                # supplemental collection. Blocked and terminal
                                # states never enter this branch.
                                await self._collection_service.prepare_retry(collection.id)
                                retry_attempted = True
                                await self._check_cancellation(run.id, context)
                            await self._collection_service.collect_subject(
                                run.subject_id,
                                context.job_id,
                                context,
                                collection_id=collection.id,
                                snapshot=snapshot,
                            )
                        except JobCancelledError:
                            raise
                        except Exception as exc:
                            warnings.append(
                                _supplemental_failure_warning(
                                    exc,
                                    canonical_url=collection.canonical_url,
                                )
                            )
                            failure_fields = _supplemental_failure_fields(
                                exc,
                                canonical_url=collection.canonical_url,
                                collection_state=str(collection_state),
                                retry_attempted=retry_attempted,
                            )
                            supplemental_failures.append(failure_fields)
                            diagnostics = getattr(self, "_diagnostics", None)
                            if diagnostics is not None:
                                diagnostics.record(
                                    event="q1.supplemental_collection_failed",
                                    run_id=run.id,
                                    subject_id=run.subject_id,
                                    stage="references",
                                    correlation_id=getattr(self, "_correlation_id", None),
                                    **failure_fields,
                                )
            except JobCancelledError:
                raise
            except Exception as exc:
                warnings.append(_supplemental_failure_warning(exc))
                failure_fields = _supplemental_failure_fields(exc)
                supplemental_failures.append(failure_fields)
                diagnostics = getattr(self, "_diagnostics", None)
                if diagnostics is not None:
                    diagnostics.record(
                        event="q1.supplemental_collection_failed",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="references",
                        correlation_id=getattr(self, "_correlation_id", None),
                        **failure_fields,
                    )

        archived_urls: set[str] = set()
        async with self._uow_factory() as uow:
            for item in await uow.source_collections.list_for_subject(run.subject_id):
                if item.state.value in _ARCHIVED_STATES:
                    archived_urls.add(item.canonical_url)

        reconciled = reconcile_reference_report_with_archives(report, archived_urls)
        warnings.extend(
            f"event_without_archived_source_dropped:{event_id}"
            for event_id in reconciled.dropped_event_ids
        )
        return ReferenceIntegrationResult(
            report=reconciled.report,
            dropped_source_ids=reconciled.dropped_source_ids,
            dropped_event_ids=reconciled.dropped_event_ids,
            restored_source_ids=reconciled.restored_source_ids,
            restored_event_ids=reconciled.restored_event_ids,
            warnings=tuple(warnings),
            new_sources=new_sources,
            archived_sources=len(reconciled.report.sources),
            supplemental_collection_failures=tuple(supplemental_failures),
        )

    async def _load_qa_inputs(
        self,
        references: Any,
        extraction: Any,
        synthesis: Any,
        publication: Any,
    ) -> dict[str, Any]:
        """Read back what QA needs to judge the publication."""
        if self._artifact_store is None:
            return {}
        store = self._artifact_store
        loaded: dict[str, Any] = {}
        try:
            if references.canonical_blob_id is not None:
                loaded["report"] = reference_report_from_json(
                    await store.read_json(references.canonical_blob_id)
                )
            if extraction.canonical_blob_id is not None:
                loaded["extraction"] = technical_extraction_from_json(
                    await store.read_json(extraction.canonical_blob_id)
                )
            if synthesis.rendered_blob_id is not None:
                loaded["synthesis_text"] = await store.read_text(synthesis.rendered_blob_id)
            if publication.rendered_blob_id is not None:
                loaded["publication_markdown"] = await store.read_text(publication.rendered_blob_id)
        except Exception:
            return loaded
        return loaded

    async def _load_reference_report(self, artifact: Any) -> ReferenceReport | None:
        """Read back the canonical Q1 report stored with the artifact."""
        if self._artifact_store is None or artifact.canonical_blob_id is None:
            return None
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            return reference_report_from_json(payload)
        except (OSError, UnicodeError, ValueError):
            return None

    def _log_parse(self, run: SubjectProductionRun, stage: str, result: ParseResult[Any]) -> None:
        self._diagnostics.record_parse(
            run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            correlation_id=self._correlation_id,
            usable=result.usable,
            warnings=result.warnings,
            errors=result.errors,
            repair_actions=result.repair_actions,
            dropped_blocks=result.dropped_blocks,
        )

    async def _subject_context(
        self,
        uow: UnitOfWork,
        subject_id: UUID,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> tuple[str, str]:
        """Editorial title and context for a subject.

        `Subject` itself only carries identifiers; the human-readable title
        lives on the editorial group that selected it.
        """
        if snapshot is not None:
            return snapshot.subject_title, snapshot.subject_description
        group = await uow.editorial_groups.get_by_subject(subject_id)
        if group is None:
            return str(subject_id), ""
        return group.title, group.grouping_justification

    async def _turn_output_text(self, conversation_id: UUID, turn_id: UUID) -> str | None:
        """Read a turn's output text.

        The turn entity only carries a blob reference; the conversation service
        is what resolves it back to text.
        """
        assert self._model_service is not None
        for content in await self._model_service.turns(conversation_id):
            if content.turn.id == turn_id:
                return content.output_text
        return None

    async def _open_conversation(
        self, run: SubjectProductionRun, subject_title: str, purpose: ConversationPurpose
    ) -> ModelConversation:
        assert self._model_service is not None
        return await self._model_service.create(
            provider=ModelProvider.OPENAI,
            transport=ConversationTransport.CHATGPT_BRIDGE,
            purpose=purpose,
            title=(
                f"Production research — {subject_title}"
                if purpose is ConversationPurpose.SUBJECT_RESEARCH
                else f"Production synthesis — {subject_title}"
            ),
            edition_id=run.edition_id,
            subject_id=run.subject_id,
            expected_profile=None,
            requested_model=None,
        )

    async def _execute_sources_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """No LLM. Pulls publications retained at discovery, dedupes by canonical
        URL, downloads and archives them into the subject workspace."""
        if self._collection_service is None:
            return {
                "stage": "sources",
                "status": "error",
                "error": "SubjectCollectionService not configured",
            }
        if context is None:
            return {
                "stage": "sources",
                "status": "error",
                "error": "Job context required for source collection",
            }

        await self._check_cancellation(run.id, context)
        try:
            await self._collection_service.collect_subject(
                run.subject_id,
                context.job_id,
                context,
                snapshot=snapshot,
            )
            sources = await self._collection_service.list_sources(run.subject_id)
            if snapshot is not None:
                snapshot_urls = {source.canonical_url for source in snapshot.core_sources}
                sources = [source for source in sources if source.canonical_url in snapshot_urls]
        except JobCancelledError:
            raise
        except Exception as e:
            details = getattr(e, "details", None)
            # Le job dispatcher n'accorde ses trois tentatives qu'à un
            # `transient_error`. Un échec de collecte dont au moins une source
            # est elle-même retryable doit rester retryable, sinon le sujet
            # meurt sur un aléa réseau.
            retryable = bool(
                isinstance(details, dict) and int(details.get("failed_retryable", 0) or 0) > 0
            )
            return {
                "stage": "sources",
                "status": "transient_error" if retryable else "error",
                "error_code": str(getattr(e, "code", "") or "sources_error"),
                "error": str(e),
                "details": details,
            }

        archived = sum(1 for source in sources if source.state in _ARCHIVED_STATES)
        await self._check_cancellation(run.id, context)
        if archived == 0:
            return {
                "stage": "sources",
                "status": "error",
                "error": "No source could be archived for this subject",
            }

        return {
            "stage": "sources",
            "status": "success",
            "sources_count": len(sources),
            "archived": archived,
        }

    async def _execute_references_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            research_date = run.research_date or datetime.now(UTC).date()
            ctx = await build_subject_production_context(
                uow,
                run.subject_id,
                research_date,
                snapshot=snapshot,
                relevant_source_urls=None,
            )
            subject_title = ctx.subject_title

            input_hash = _references_input_hash(
                subject_id=run.subject_id,
                snapshot=snapshot,
                subject_title=subject_title,
                subject_description=ctx.subject_description,
                research_date=research_date,
            )
            reused = await self._reuse_artifact(run, "references", input_hash)
            if reused is not None:
                return reused

            if not self._model_service:
                return {
                    "stage": "references",
                    "status": "error",
                    "error": "ModelConversationService not configured",
                }

            # The diffusion policy, not a hardcoded flag, decides whether this
            # subject may be sent to an external model.
            if not ctx.external_llm_allowed:
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }

            # Q1 has its own research conversation; Q2 remains stateless.
            if not run.references_conversation_id:
                conversation = await self._open_conversation(
                    run, subject_title, ConversationPurpose.SUBJECT_RESEARCH
                )
                run.references_conversation_id = conversation.id
                persisted = await uow.subject_production_runs.get_for_update(run.id)
                if persisted is not None:
                    persisted.references_conversation_id = conversation.id
                    await uow.subject_production_runs.save(persisted)
                    await uow.commit()

            prompt = ProductionPromptTemplates.get_references_prompt(
                subject_title=subject_title,
                subject_description=ctx.subject_description,
                actor_info=ctx.actor_info,
                technical_summary=ctx.technical_summary,
                research_date=research_date.isoformat(),
                period_start=ctx.period_start,
                period_end=ctx.period_end,
                core_sources_text=ctx.core_sources_text,
                supporting_sources_text=ctx.supporting_sources_text,
            )

            try:
                parsed, raw, turn_id, _ = await self._ask_with_format_repair(
                    run=run,
                    conversation_id=run.references_conversation_id,
                    stage="references",
                    prompt=prompt,
                    prompt_version=REFERENCES_PROMPT_VERSION,
                    repair_version=REFERENCES_FORMAT_REPAIR_VERSION,
                    mode=ConversationMode.FRESH,
                    parse=lambda text: parse_reference_report(text, research_date),
                    external_llm_allowed=ctx.external_llm_allowed,
                    web_search=True,
                    request_identity=f"g{run.pipeline_generation}",
                    context=context,
                )
            except JobCancelledError:
                raise
            except Exception as e:
                await self._check_cancellation(run.id, context)
                return self._handle_stage_exception(run, "references", e)

            if parsed is None:
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "no_model_response",
                    "error": "No response from model",
                }
            if not parsed.usable:
                # A format the parser cannot read is a review case, never a crash.
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "error_code": "references_format_unusable",
                    "error": "; ".join(parsed.errors),
                    "warnings": parsed.warnings,
                }

            report = parsed.value
            assert report is not None
            proposed_source_index = [
                {
                    "source_id": source.local_id,
                    "source_title": source.title,
                    "source_url": source.canonical_url,
                    "publisher": source.publisher,
                }
                for source in report.sources
            ]

            # Order matters: the new publications must be attached, downloaded
            # and archived before Q1 is recorded, so extraction only ever sees
            # events backed by a source we actually hold.
            await self._check_cancellation(run.id, context)
            integration = await self._integrate_reference_sources(run, report, context, snapshot)
            parsed.warnings.extend(integration.warnings)
            report = integration.report

            await self._check_cancellation(run.id, context)
            artifact = await self._references.store_references_result(
                run_id=run.id,
                subject_id=run.subject_id,
                input_hash=input_hash,
                raw_result=raw,
                canonical_json=reference_report_to_json(report),
                conversation_turn_id=turn_id,
                warnings=parsed.warnings,
                repair_source_index={
                    "proposed": proposed_source_index,
                    "canonical": [
                        {
                            "source_id": source.local_id,
                            "source_url": source.canonical_url,
                        }
                        for source in report.sources
                    ],
                },
            )

            # Keep raw Q1 and the empty canonical projection available even
            # when collection failures left no usable event.  This is what
            # lets the Repair Desk archive one of those proposed sources and
            # deterministically rebuild REFERENCES later, without asking the
            # model the same Q1 question again.
            if not integration.kept_events:
                return {
                    "stage": "references",
                    "status": "needs_review",
                    "artifact_id": str(artifact.id),
                    "error_code": "no_event_with_archived_source",
                    "error": "No chronological event is backed by an archived source",
                    "warnings": parsed.warnings,
                }

            return {
                "stage": "references",
                "status": "success",
                "artifact_id": str(artifact.id),
                "sources_count": len(report.sources),
                "events_count": len(report.events),
                "new_sources": integration.new_sources,
                "archived_sources": integration.archived_sources,
                "warnings": parsed.warnings,
                "repair_actions": parsed.repair_actions,
            }

    async def _execute_extraction_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        return await self._execute_direct_url_extraction(run, context, snapshot)

    async def _load_archived_source_text(
        self, archived: _ArchivedSource | None
    ) -> SourceEvidenceDocument:
        """Read and integrity-check one decoded archive for local validation.

        The returned local evidence views are never put in a ModelRequest. They
        are read only after a live Q2 response exists, and the digest is checked
        before parsing them.
        """
        if archived is None:
            raise _Q2SourceEvidenceUnavailable("Archived source is missing")
        if archived.decoded_blob_id is None:
            raise _Q2SourceEvidenceUnavailable(
                "Archived decoded blob is missing",
                expected_sha256=archived.content_sha256,
            )

        reader = getattr(self, "_blob_reader", None)
        read_blob = getattr(reader, "read_blob", None)
        if not callable(read_blob):
            # ProductionArtifactStore exposes the same canonical catalog via
            # read_bytes; accepting it keeps the workflow easy to exercise in
            # isolation while the collection service remains the normal port.
            read_blob = getattr(reader, "read_bytes", None)
        if not callable(read_blob):
            raise _Q2SourceEvidenceUnavailable(
                "Archived blob reader is unavailable",
                expected_sha256=archived.content_sha256,
                blob_id=archived.decoded_blob_id,
            )

        try:
            content = await read_blob(
                archived.decoded_blob_id,
                max_bytes=MAX_ARCHIVED_SOURCE_BYTES,
            )
        except Exception as exc:
            raise _Q2SourceEvidenceUnavailable(
                "Archived decoded blob is unreadable",
                expected_sha256=archived.content_sha256,
                blob_id=archived.decoded_blob_id,
            ) from exc
        if not isinstance(content, bytes):
            raise _Q2SourceEvidenceUnavailable(
                "Archived decoded blob did not return bytes",
                expected_sha256=archived.content_sha256,
                blob_id=archived.decoded_blob_id,
            )
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != archived.content_sha256:
            raise _Q2SourceEvidenceUnavailable(
                "Archived decoded blob integrity check failed",
                expected_sha256=archived.content_sha256,
                blob_id=archived.decoded_blob_id,
            )

        try:
            mime_type = DetectedMimeType(archived.mime_type or DetectedMimeType.HTML.value)
            parsed = parse_document(content, mime_type)
            if mime_type is DetectedMimeType.HTML:
                return source_evidence_document_from_html(
                    parsed.text,
                    content.decode(_html_encoding(content), errors="replace"),
                )
            return SourceEvidenceDocument(parsed_text=parsed.text)
        except Exception as exc:
            raise _Q2SourceEvidenceUnavailable(
                "Archived source text is unreadable",
                expected_sha256=archived.content_sha256,
                blob_id=archived.decoded_blob_id,
            ) from exc

    async def _execute_direct_url_extraction(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Q2: at most one source-level, web-enabled request per Q1 source."""
        await self._check_cancellation(run.id, context)
        if snapshot is None:
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": "q2_extraction_plan_missing_snapshot",
                "error": "Q2 extraction requires the frozen production input snapshot",
            }
        async with self._uow_factory() as uow:
            references = await uow.production_artifacts.get_current(run.id, "references")
            if references is None:
                return {
                    "stage": "extraction",
                    "status": "error",
                    "error": "References artifact not found",
                }
            report = await self._load_reference_report(references)
            if report is None:
                return {
                    "stage": "extraction",
                    "status": "terminal_error",
                    "error_code": "references_payload_missing",
                    "error": "Reference report content is not readable",
                }
            research_date = run.research_date or datetime.now(UTC).date()
            policy = await build_subject_production_context(
                uow,
                run.subject_id,
                research_date,
                snapshot=snapshot,
                relevant_source_urls={source.canonical_url for source in report.sources},
            )
            source_plans = plan_q2_extraction_profiles(
                report,
                snapshot=snapshot,
                period_start=getattr(policy, "period_start", None),
                period_end=getattr(policy, "period_end", None),
            )
            archived_sources = await _archived_sources_by_url(uow, run.subject_id, report)
            input_hash = _extraction_input_hash(
                subject_id=run.subject_id,
                references_hash=references.input_hash,
                source_urls=[source.canonical_url for source in report.sources],
                references_payload_hash=compute_input_hash(reference_report_to_json(report)),
            )
            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)
            progress = _new_extraction_progress(report, source_plans)

        # The plan is visible before cache lookup or the first provider call.
        await self._persist_extraction_progress(run.id, progress)

        reused = await self._reuse_artifact(run, "extraction", input_hash)
        if reused is not None:
            return reused
        if self._model_gateway is None:
            return {
                "stage": "extraction",
                "status": "error",
                "error": "ModelGateway not configured",
            }
        model_gateway = cast(ModelGateway, self._model_gateway)
        if not policy.external_llm_allowed:
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": "external_llm_blocked",
                "error": "Diffusion policy forbids sending this subject to an external model",
            }

        submissions: list[Q2ProposalSubmission] = []
        url_raw_parts: list[str] = []
        warnings: list[str] = []
        completed: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        failed_attempts: list[str] = []
        failures: dict[str, dict[str, Any]] = {}
        source_skips: dict[str, dict[str, Any]] = {}
        source_evidence_rejections: list[dict[str, Any]] = []
        repair_evidence_entries: list[dict[str, Any]] = []
        source_evidence_rejection_counts: dict[tuple[str, str | None, str, str], int] = {}
        plans_by_url = {plan.canonical_url: plan for plan in source_plans}
        full_calls = 0
        light_calls = 0
        light_batches = 0
        light_sources_batched = 0
        cache_hits = 0
        model_calls_avoided = 0
        # Each entry carries the batch and its ModelRun identity, all decided
        # before any prompt exists.
        light_batches_by_first_source: dict[str, tuple[tuple[Q2BatchSource, ...], UUID]] = {}
        individual_source_ids: set[str] = set()
        batch_candidates: list[Q2BatchCandidate] = []
        pending: dict[str, _Q2SourceWork] = {}

        requested_model = "unknown"
        router = getattr(model_gateway, "_router", None)
        if router is not None:
            try:
                requested_model = str(
                    router.by_provider(ModelProvider.OPENAI, ModelRole.RESEARCH).requested_model
                )
            except (AttributeError, KeyError):
                pass

        def metrics() -> dict[str, int]:
            return {
                "model_calls": progress["model_calls"],
                "full_calls": full_calls,
                "light_calls": light_calls,
                "light_batches": light_batches,
                "light_sources_batched": light_sources_batched,
                "cache_hits": cache_hits,
                "model_calls_avoided": model_calls_avoided,
            }

        reuse_max_age_days = float(getattr(self, "_q2_reuse_max_age_days", 14.0))
        q2_reuse_not_before = (
            datetime.now(UTC) - timedelta(days=reuse_max_age_days)
            if reuse_max_age_days > 0
            else None
        )
        archive_fallback_min_chars = int(
            getattr(
                getattr(self, "_settings", None),
                "production_archive_fallback_min_chars",
                1200,
            )
        )

        async def find_q2_checkpoint(checkpoint_key: str) -> Any | None:
            async with self._uow_factory() as uow:
                model_runs = getattr(uow, "model_runs", None)
                finder = getattr(model_runs, "find_successful_q2_checkpoint", None)
                if finder is None:
                    return None
                checkpoint = await finder(checkpoint_key, not_before=q2_reuse_not_before)
                if checkpoint is None or checkpoint.status is not ModelRunStatus.SUCCEEDED:
                    return None
                return checkpoint

        async def read_q2_checkpoint(checkpoint: Any) -> str | None:
            reference = getattr(checkpoint, "raw_output_reference", None) or (
                checkpoint.output_references[0]
                if getattr(checkpoint, "output_references", ())
                else None
            )
            reader = getattr(model_gateway, "read_output", None)
            if reference is None or not callable(reader):
                return None
            try:
                content = await reader(reference)
            except Exception as exc:
                self._diagnostics.record(
                    event="q2.checkpoint.read_failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    model_run_id=str(checkpoint.id),
                    error_code="q2_checkpoint_read_failed",
                    error=str(exc),
                )
                return None
            if not isinstance(content, bytes):
                return None
            return content.decode("utf-8", errors="replace")

        def checkpoint_key(
            work: _Q2SourceWork,
            *,
            batched: bool,
            access_mode: str = "live_url",
        ) -> str:
            if access_mode == "archive_fallback":
                if work.source_content_sha256 is None:
                    raise ValueError("Archive fallback checkpoints require a source hash")
                return _q2_archive_fallback_checkpoint_key(
                    canonical_url=work.source.canonical_url,
                    source_content_sha256=work.source_content_sha256,
                    profile=work.plan.profile,
                    provider=ModelProvider.OPENAI,
                    requested_model=requested_model,
                )
            if access_mode != "live_url":
                raise ValueError(f"Unsupported Q2 access mode: {access_mode}")
            if batched:
                prompt_version = IOC_RULES_BATCH_PROMPT_VERSION
                batch_parser_version: str | None = Q2_BATCH_PARSER_VERSION
            else:
                prompt_version = EXTRACTION_PROMPT_VERSION_BY_PROFILE[work.plan.profile]
                batch_parser_version = None
            return _q2_checkpoint_key(
                canonical_url=work.source.canonical_url,
                profile=work.plan.profile,
                prompt_version=prompt_version,
                batch_parser_version=batch_parser_version,
                provider=ModelProvider.OPENAI,
                requested_model=requested_model,
            )

        def clear_active_source() -> None:
            progress["active_source_id"] = None
            progress["active_source_title"] = None
            progress["active_profile"] = None

        async def gate_source_output(
            work: _Q2SourceWork,
            output: Q2SourceOutput,
            *,
            model_run_id: UUID,
            batch_id: str | None = None,
            source_text: SourceEvidenceDocument | None = None,
        ) -> Q2SourceOutput:
            """Validate one source output against only that source's archive."""
            archived = archived_sources.get(work.source.canonical_url)
            archived_text = (
                source_text
                if source_text is not None
                else await self._load_archived_source_text(archived)
            )
            evidence = _gate_archived_q2_output(
                output,
                source_text=archived_text,
                profile=work.plan.profile,
            )
            warnings.extend(evidence.warnings)
            for rejection in evidence.rejections:
                issue_kind = (
                    "rejected_rule" if rejection.proposal_kind == "rule" else "rejected_indicator"
                )
                value_sha256 = hashlib.sha256(rejection.value.encode("utf-8")).hexdigest()
                repair_key = repair_key_for_rejection(
                    edition_id=run.edition_id,
                    subject_id=run.subject_id,
                    kind=issue_kind,
                    source_url=work.source.canonical_url,
                    artifact_type=rejection.artifact_type,
                    value=rejection.value,
                )
                repair_evidence_entries.append(
                    {
                        "repair_key": repair_key,
                        "source_id": work.source.local_id,
                        "source_title": work.source.title,
                        "source_url": work.source.canonical_url,
                        "batch_id": batch_id,
                        "model_run_id": str(model_run_id),
                        "proposal_index": rejection.proposal_index,
                        "proposal_kind": rejection.proposal_kind,
                        "artifact_type": rejection.artifact_type,
                        "reason_code": rejection.reason_code,
                        "value": rejection.value,
                        "value_sha256": value_sha256,
                    }
                )
                # La valeur rejetée vient de la sortie du modèle, déjà archivée
                # en clair dans le blob de résultat brut : la conserver ici
                # n'expose rien de neuf et c'est la seule façon pour
                # l'analyste de savoir quel IOC a été écarté avant de
                # publier. L'empreinte reste, elle sert au recoupement.
                source_evidence_rejections.append(
                    {
                        "source_id": work.source.local_id,
                        "source_url": work.source.canonical_url,
                        "batch_id": batch_id,
                        "model_run_id": str(model_run_id),
                        "proposal_index": rejection.proposal_index,
                        "proposal_kind": rejection.proposal_kind,
                        "artifact_type": rejection.artifact_type,
                        "reason_code": rejection.reason_code,
                        "value": rejection.value[:512],
                        "value_hash": value_sha256,
                    }
                )
                group_key = (
                    work.source.local_id,
                    batch_id,
                    rejection.artifact_type or rejection.proposal_kind,
                    rejection.reason_code,
                )
                source_evidence_rejection_counts[group_key] = (
                    source_evidence_rejection_counts.get(group_key, 0) + 1
                )
                self._diagnostics.record(
                    event="q2.source.evidence_rejected",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=work.source.local_id,
                    source_url=work.source.canonical_url,
                    source_content_sha256=work.source_content_sha256,
                    model_run_id=str(model_run_id),
                    batch_id=batch_id,
                    profile=work.plan.profile.value,
                    proposal_index=rejection.proposal_index,
                    proposal_kind=rejection.proposal_kind,
                    artifact_type=rejection.artifact_type,
                    reason_code=rejection.reason_code,
                    value=rejection.value[:512],
                )
            return evidence.filtered_output

        async def load_reusable_source(
            work: _Q2SourceWork,
            *,
            batched: bool,
            access_mode: str = "live_url",
        ) -> _Q2ReusableSource | None:
            key = checkpoint_key(work, batched=batched, access_mode=access_mode)
            checkpoint = await find_q2_checkpoint(key)
            if checkpoint is None:
                return None
            raw = await read_q2_checkpoint(checkpoint)
            if raw is None or not raw.strip():
                return None

            parameters = getattr(checkpoint, "parameters", {})
            kind = parameters.get("q2_execution_kind") if isinstance(parameters, dict) else None
            persisted_access_mode = (
                parameters.get("q2_access_mode") if isinstance(parameters, dict) else None
            )
            if access_mode == "archive_fallback" and persisted_access_mode != "archive_fallback":
                return None
            if access_mode == "live_url" and persisted_access_mode == "archive_fallback":
                return None
            if kind == "batch":
                batch_sources = parameters.get("q2_batch_sources", [])
                target = next(
                    (
                        item
                        for item in batch_sources
                        if isinstance(item, dict)
                        and item.get("canonical_url") == work.source.canonical_url
                    ),
                    None,
                )
                if not isinstance(target, dict) or not isinstance(target.get("batch_id"), str):
                    return None
                parsed_batch = parse_q2_batch_response(
                    raw,
                    {target["batch_id"]: work.source},
                )
                if (
                    not parsed_batch.usable
                    or not parsed_batch.sources
                    or not parsed_batch.sources[0].usable
                ):
                    return None
                source_result = parsed_batch.sources[0]
                assert source_result.output is not None
                return _Q2ReusableSource(
                    output=source_result.output,
                    raw=source_result.raw_block,
                    model_run_id=checkpoint.id,
                    warnings=(*parsed_batch.warnings, *source_result.warnings),
                )
            if kind == "individual":
                parsed_individual = parse_q2_proposals_markdown(raw)
                if not parsed_individual.usable or parsed_individual.value is None:
                    return None
                return _Q2ReusableSource(
                    output=parsed_individual.value,
                    raw=raw,
                    model_run_id=checkpoint.id,
                    warnings=tuple(parsed_individual.warnings),
                )
            return None

        async def persist_q2_checkpoint_keys(model_run_id: UUID, keys: Sequence[str]) -> None:
            """Record only source results that passed the Q2 source parser."""
            async with self._uow_factory() as uow:
                model_runs = getattr(uow, "model_runs", None)
                get_for_update = getattr(model_runs, "get_for_update", None)
                save = getattr(model_runs, "save", None)
                if get_for_update is None or save is None:
                    return
                model_run = await get_for_update(model_run_id)
                if model_run is None or model_run.status is not ModelRunStatus.SUCCEEDED:
                    return
                parameters = dict(getattr(model_run, "parameters", {}) or {})
                parameters["q2_checkpoint_keys"] = list(dict.fromkeys(keys))
                model_run.parameters = parameters
                await save(model_run)
                commit = getattr(uow, "commit", None)
                if commit is not None:
                    await commit()

        async def remove_q2_checkpoint_keys(model_run_id: UUID, keys: Sequence[str]) -> None:
            """Remove source keys that failed local archive validation."""
            if not keys:
                return
            async with self._uow_factory() as uow:
                model_runs = getattr(uow, "model_runs", None)
                get_for_update = getattr(model_runs, "get_for_update", None)
                save = getattr(model_runs, "save", None)
                if get_for_update is None or save is None:
                    return
                model_run = await get_for_update(model_run_id)
                if model_run is None or model_run.status is not ModelRunStatus.SUCCEEDED:
                    return
                parameters = dict(getattr(model_run, "parameters", {}) or {})
                current_keys = parameters.get("q2_checkpoint_keys", [])
                if not isinstance(current_keys, list):
                    return
                parameters["q2_checkpoint_keys"] = [
                    key for key in current_keys if key not in set(keys)
                ]
                model_run.parameters = parameters
                await save(model_run)
                commit = getattr(uow, "commit", None)
                if commit is not None:
                    await commit()

        async def record_reused_source(
            work: _Q2SourceWork,
            reusable: _Q2ReusableSource,
            *,
            access_mode: str = "live_url",
            source_text: SourceEvidenceDocument | None = None,
            live_failure_code: str | None = None,
        ) -> None:
            nonlocal cache_hits, model_calls_avoided
            try:
                profiled_output, profile_warnings = _enforce_q2_profile(
                    reusable.output, work.plan.profile
                )
                filtered_output = await gate_source_output(
                    work,
                    profiled_output,
                    model_run_id=reusable.model_run_id,
                    source_text=source_text,
                )
            except _Q2SourceEvidenceUnavailable as exc:
                await remove_q2_checkpoint_keys(
                    reusable.model_run_id,
                    (
                        checkpoint_key(work, batched=False, access_mode=access_mode),
                        checkpoint_key(work, batched=True, access_mode="live_url")
                        if access_mode == "live_url"
                        else checkpoint_key(work, batched=False, access_mode="live_url"),
                    ),
                )
                await record_source_failure(
                    work.source,
                    error_code=exc.code,
                    model_run_id=reusable.model_run_id,
                    details=exc.details,
                    profile=work.plan.profile,
                )
                return
            warnings.extend((*reusable.warnings, *profile_warnings))
            submissions.append(
                Q2ProposalSubmission(
                    output=filtered_output,
                    source_ids=(work.source.local_id,),
                    model_run_id=str(reusable.model_run_id),
                )
            )
            completed.append(work.source.local_id)
            _mark_extraction_source_complete(
                progress,
                work.source,
                status="cached",
                counts=_source_progress_counts(filtered_output, work.source.local_id),
                cache_hit=True,
            )
            cache_hits += 1
            model_calls_avoided += 1
            await self._persist_extraction_progress(run.id, progress)
            url_raw_parts.append(reusable.raw)
            self._diagnostics.record(
                event="q2.source.reused",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                source_id=work.source.local_id,
                source_url=work.source.canonical_url,
                model_run_id=str(reusable.model_run_id),
                profile=work.plan.profile.value,
                checkpoint_version=Q2_SUCCESSFUL_CHECKPOINT_VERSION,
                access_mode=access_mode,
            )
            if access_mode == "archive_fallback":
                self._diagnostics.record(
                    event="q2.source.archive_fallback_completed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=work.source.local_id,
                    source_url=work.source.canonical_url,
                    source_content_sha256=work.source_content_sha256,
                    profile=work.plan.profile.value,
                    live_failure_code=live_failure_code,
                    fallback_model_run_id=str(reusable.model_run_id),
                    duration_ms=0,
                    reused=True,
                )

        async def pace_before_model_call() -> None:
            if progress["model_calls"]:
                await self._check_cancellation(run.id, context)
                await asyncio.sleep(self._pacing.model_delay_seconds())

        def remove_persisted_model_call(
            execution: Any,
            *,
            profile: ExtractionProfile,
            batched_source_count: int = 0,
        ) -> None:
            """Do not count a ModelRun registry hit as a provider call."""
            nonlocal full_calls, light_calls, light_batches, light_sources_batched
            if execution.metadata.get("checkpoint") != "hit":
                return
            progress["model_calls"] = max(0, progress["model_calls"] - 1)
            if profile is ExtractionProfile.FULL:
                full_calls -= 1
            else:
                light_calls -= 1
                if batched_source_count:
                    light_batches -= 1
                    light_sources_batched -= batched_source_count
                    progress["light_batches"] = light_batches
                    progress["light_sources_batched"] = light_sources_batched

        async def record_source_failure(
            source: ParsedSource,
            *,
            error_code: str,
            model_run_id: UUID,
            details: dict[str, Any] | None = None,
            profile: ExtractionProfile,
            batch_id: str | None = None,
        ) -> None:
            _mark_extraction_source_failed(progress, source.local_id, "failed")
            await self._persist_extraction_progress(run.id, progress)
            failed_attempts.append(source.local_id)
            failed.append(source.local_id)
            failure_details = details or {}
            failures[source.local_id] = {
                "model_run_id": str(model_run_id),
                "batch_id": batch_id,
                "source_url": source.canonical_url,
                "error_code": error_code,
                "error": error_code,
                "details": failure_details,
                "retryable": False,
                "phase": "response_validation",
                "submission_state": "post_submission",
                "access_mode": "live_url",
                "failure_class": _Q2FailureClass.SOURCE_CONTENT_FAILURE.value,
                "contributes_to_coverage": True,
                "duration_ms": 0,
            }
            self._diagnostics.record(
                event="q2.source.failed",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                source_id=source.local_id,
                source_url=source.canonical_url,
                model_run_id=str(model_run_id),
                batch_id=batch_id,
                profile=profile.value,
                error_code=error_code,
                error=error_code,
                retryable=False,
                phase="response_validation",
                submission_state="post_submission",
                failure_class=_Q2FailureClass.SOURCE_CONTENT_FAILURE.value,
                access_mode="live_url",
                duration_ms=0,
            )

        async def record_source_skip(
            work: _Q2SourceWork,
            *,
            live_error_code: str,
            archive_error_code: str,
            archive_reason: str | None,
            profile: ExtractionProfile,
            live_model_run_id: UUID | None = None,
            batch_id: str | None = None,
        ) -> None:
            details: dict[str, Any] = {
                "source_url": work.source.canonical_url,
                "reason_code": "live_unavailable_archive_unusable",
                "live_error_code": live_error_code,
                "archive_error_code": archive_error_code,
                "blocking": False,
            }
            if archive_reason:
                details["archive_reason"] = archive_reason
            if batch_id is not None:
                details["batch_id"] = batch_id
            source_skips[work.source.local_id] = details
            if work.source.local_id not in skipped:
                skipped.append(work.source.local_id)
            _mark_extraction_source_skipped(progress, work.source.local_id, details)
            clear_active_source()
            await self._persist_extraction_progress(run.id, progress)
            self._diagnostics.record(
                event="q2.source.skipped",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                source_id=work.source.local_id,
                source_url=work.source.canonical_url,
                source_content_sha256=work.source_content_sha256,
                profile=profile.value,
                live_error_code=live_error_code,
                archive_error_code=archive_error_code,
                archive_reason=archive_reason,
                live_model_run_id=(str(live_model_run_id) if live_model_run_id else None),
                batch_id=batch_id,
                blocking=False,
            )

        async def record_batch_source_failure(
            item: Q2BatchSource,
            *,
            error_code: str,
            model_run_id: UUID,
            details: dict[str, Any] | None = None,
        ) -> None:
            await record_source_failure(
                item.source,
                error_code=error_code,
                model_run_id=model_run_id,
                details=details,
                profile=ExtractionProfile.IOC_RULES,
                batch_id=item.batch_id,
            )

        async def execute_archive_fallback(
            work: _Q2SourceWork,
            *,
            live_failure_code: str,
            live_model_run_id: UUID | None = None,
            batch_id: str | None = None,
        ) -> dict[str, Any] | None:
            """Run the archive-only path after an explicit live UNAVAILABLE."""
            nonlocal full_calls, light_calls
            source = work.source
            plan = work.plan
            archived = archived_sources.get(source.canonical_url)
            try:
                archived_text = await self._load_archived_source_text(archived)
                # The evidence document is also the object passed to the gate.
                # Prefer the safe complete rendered view when available, and
                # never slice either representation.
                source_text = (
                    archived_text.decoded_source_view or archived_text.parsed_text
                ).strip()
                if not source_text:
                    raise _Q2SourceEvidenceUnavailable(
                        "Archived source text is empty",
                        expected_sha256=archived.content_sha256 if archived else None,
                        blob_id=archived.decoded_blob_id if archived else None,
                    )
                if len(source_text) < archive_fallback_min_chars:
                    # Coquille anti-bot ou page de blocage : un appel modèle
                    # dessus coûte 45 s et ne produit rien.
                    raise _Q2SourceEvidenceUnavailable(
                        "Archived source text is not substantive",
                        expected_sha256=archived.content_sha256 if archived else None,
                        blob_id=archived.decoded_blob_id if archived else None,
                        code="archive_source_not_substantive",
                    )
            except _Q2SourceEvidenceUnavailable as exc:
                details = exc.details if isinstance(exc.details, dict) else {}
                await record_source_skip(
                    work,
                    live_error_code=live_failure_code,
                    archive_error_code=exc.code,
                    archive_reason=(
                        str(details["reason"]) if isinstance(details.get("reason"), str) else None
                    ),
                    profile=plan.profile,
                    live_model_run_id=live_model_run_id,
                    batch_id=batch_id,
                )
                return None

            archive_model_run_id = _q2_archive_fallback_model_run_id(
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                source_id=source.local_id,
                canonical_url=source.canonical_url,
                source_content_sha256=work.source_content_sha256 or "",
                profile=plan.profile,
                provider=ModelProvider.OPENAI,
                requested_model=requested_model,
            )
            reusable = await load_reusable_source(
                work,
                batched=False,
                access_mode="archive_fallback",
            )
            if reusable is not None:
                await record_reused_source(
                    work,
                    reusable,
                    access_mode="archive_fallback",
                    source_text=archived_text,
                    live_failure_code=live_failure_code,
                )
                return None

            prompt = ProductionPromptTemplates.get_archived_extraction_prompt(
                subject_title,
                source.local_id,
                source.title,
                source.canonical_url,
                source_text,
                profile=plan.profile,
            )
            if len(prompt.encode("utf-8")) > MAX_Q2_ARCHIVE_FALLBACK_PROMPT_BYTES:
                await record_source_skip(
                    work,
                    live_error_code=live_failure_code,
                    archive_error_code="q2_archive_prompt_too_large",
                    archive_reason="archive_fallback_prompt_exceeds_gateway_limit",
                    profile=plan.profile,
                    live_model_run_id=live_model_run_id,
                    batch_id=batch_id,
                )
                return None
            if plan.profile is ExtractionProfile.FULL:
                full_calls += 1
            else:
                light_calls += 1
            await pace_before_model_call()
            _mark_extraction_source_running(progress, source, plan)
            progress["model_calls"] += 1
            await self._persist_extraction_progress(run.id, progress)
            self._diagnostics.record(
                event="q2.source.archive_fallback_started",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                source_id=source.local_id,
                source_url=source.canonical_url,
                source_content_sha256=work.source_content_sha256,
                profile=plan.profile.value,
                live_failure_code=live_failure_code,
                fallback_model_run_id=str(archive_model_run_id),
                web_search=False,
            )
            started_at = time.monotonic()
            raw = ""
            execution: Any | None = None
            try:
                execution = await model_gateway.execute(
                    ModelRequest(
                        text=prompt,
                        prompt_template_id="production-q2-url-archive-fallback",
                        prompt_template_version=ARCHIVE_FALLBACK_PROMPT_VERSION,
                        evidence_pack_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                        external_llm_allowed=True,
                        routing_hint=ModelRoutingHint.WEB_RESEARCH,
                        provider=ModelProvider.OPENAI,
                        web_search=False,
                        run_id=archive_model_run_id,
                        allow_failed_resubmit=True,
                        metadata={
                            "source_id": source.local_id,
                            "source_url": source.canonical_url,
                            "profile": plan.profile.value,
                            "access_mode": "archive_fallback",
                            "source_content_sha256": work.source_content_sha256,
                            "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
                            "parser_version": Q2_MARKDOWN_PARSER_VERSION,
                            "verifier_version": ARTIFACT_VERIFIER_VERSION,
                            "source_evidence_version": SOURCE_EVIDENCE_VERSION,
                            "archive_fallback_prompt_version": ARCHIVE_FALLBACK_PROMPT_VERSION,
                            "archived_source_access_version": ARCHIVED_SOURCE_ACCESS_VERSION,
                        },
                        parameters={
                            "q2_execution_kind": "individual",
                            "q2_access_mode": "archive_fallback",
                            "source_content_sha256": work.source_content_sha256,
                            "archive_fallback_prompt_version": ARCHIVE_FALLBACK_PROMPT_VERSION,
                            "archived_source_access_version": ARCHIVED_SOURCE_ACCESS_VERSION,
                        },
                    ),
                    ModelRole.RESEARCH,
                )
                remove_persisted_model_call(execution, profile=plan.profile)
                await self._check_cancellation(run.id, context)
                if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
                    review_details = dict(execution.run.error_details or {})
                    review_details.update(execution.metadata)
                    raise _Q2ControlFailure(
                        execution.run.error_message or "Model run needs review",
                        code=execution.run.error_code or "q2_control_failure",
                        details=review_details,
                    )
                if execution.run.status is not ModelRunStatus.SUCCEEDED:
                    run_status = execution.run.status.value
                    raise _Q2ControlFailure(
                        f"Model run reached unexpected status {run_status}",
                        code=execution.run.error_code or "q2_model_run_not_succeeded",
                        details={
                            **(execution.run.error_details or {}),
                            **execution.metadata,
                            "model_run_status": run_status,
                        },
                    )
                raw = execution.output_text or ""
                if not raw.strip():
                    raise _Q2ControlFailure("Provider returned no Q2 response")
                parsed = parse_q2_proposals_markdown(raw)
                self._log_parse(run, "extraction", parsed)
                if not parsed.usable or parsed.value is None:
                    # An archive was actually supplied: this is a normal
                    # fallback output failure, not another source skip.
                    raise _Q2SourceContentFailure(
                        "; ".join(parsed.errors) or "archive_fallback_output_invalid"
                    )
                filtered_output, profile_warnings = _enforce_q2_profile(parsed.value, plan.profile)
                filtered_output = await gate_source_output(
                    work,
                    filtered_output,
                    model_run_id=execution.run.id,
                    source_text=archived_text,
                )
                warnings.extend(profile_warnings)
                submissions.append(
                    Q2ProposalSubmission(
                        output=filtered_output,
                        source_ids=(source.local_id,),
                        model_run_id=str(execution.run.id),
                    )
                )
                completed.append(source.local_id)
                _mark_extraction_source_complete(
                    progress,
                    source,
                    status="succeeded",
                    counts=_source_progress_counts(filtered_output, source.local_id),
                )
                await persist_q2_checkpoint_keys(
                    execution.run.id,
                    [checkpoint_key(work, batched=False, access_mode="archive_fallback")],
                )
                clear_active_source()
                await self._persist_extraction_progress(run.id, progress)
                url_raw_parts.append(raw)
                warnings.extend(parsed.warnings)
                duration_ms = int((time.monotonic() - started_at) * 1000)
                self._diagnostics.record(
                    event="q2.source.completed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    source_url=source.canonical_url,
                    source_content_sha256=work.source_content_sha256,
                    model_run_id=str(execution.run.id),
                    profile=plan.profile.value,
                    access_mode="archive_fallback",
                    answer_chars=len(raw),
                    facts_count=len(filtered_output.facts),
                    artifacts_count=len(filtered_output.artifacts),
                    rules_count=len(filtered_output.rules),
                    duration_ms=duration_ms,
                )
                self._diagnostics.record(
                    event="q2.source.archive_fallback_completed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    source_url=source.canonical_url,
                    source_content_sha256=work.source_content_sha256,
                    profile=plan.profile.value,
                    live_failure_code=live_failure_code,
                    fallback_model_run_id=str(execution.run.id),
                    duration_ms=duration_ms,
                )
                return None
            except JobCancelledError:
                raise
            except Exception as exc:
                await self._check_cancellation(run.id, context)
                if (
                    execution is not None
                    and getattr(execution, "run", None) is not None
                    and execution.run.status is ModelRunStatus.SUCCEEDED
                ):
                    await persist_q2_checkpoint_keys(execution.run.id, [])
                classification = _classify_q2_failure(
                    exc,
                    provider_response_produced=bool(raw),
                )
                _mark_extraction_source_failed(
                    progress,
                    source.local_id,
                    "needs_review" if classification.status == "needs_review" else "failed",
                )
                clear_active_source()
                await self._persist_extraction_progress(run.id, progress)
                error = str(exc)[:1000]
                duration_ms = int((time.monotonic() - started_at) * 1000)
                failed_attempts.append(source.local_id)
                if classification.contributes_to_coverage:
                    failed.append(source.local_id)
                exception_details = getattr(exc, "details", None)
                failures[source.local_id] = {
                    "model_run_id": str(archive_model_run_id),
                    "source_url": source.canonical_url,
                    "error_code": classification.error_code,
                    "error": error,
                    "details": (
                        dict(exception_details) if isinstance(exception_details, dict) else {}
                    ),
                    "retryable": classification.retryable,
                    "phase": classification.phase,
                    "submission_state": classification.submission_state,
                    "failure_class": classification.failure_class.value,
                    "contributes_to_coverage": classification.contributes_to_coverage,
                    "access_mode": "archive_fallback",
                    "duration_ms": duration_ms,
                }
                self._diagnostics.record(
                    event="q2.source.failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    source_url=source.canonical_url,
                    source_content_sha256=work.source_content_sha256,
                    model_run_id=str(archive_model_run_id),
                    profile=plan.profile.value,
                    access_mode="archive_fallback",
                    error_code=classification.error_code,
                    error=error,
                    retryable=classification.retryable,
                    phase=classification.phase,
                    submission_state=classification.submission_state,
                    failure_class=classification.failure_class.value,
                    duration_ms=duration_ms,
                )
                if classification.failure_class is _Q2FailureClass.GLOBAL_TRANSIENT_PRE_SUBMISSION:
                    return {
                        "stage": "extraction",
                        "status": "transient_error",
                        "error_code": classification.error_code,
                        "error": error,
                        "details": {
                            "completed_source_ids": completed,
                            "skipped_source_ids": skipped,
                            "failed_source_ids": failed_attempts,
                            "source_skips": source_skips,
                            "source_failures": failures,
                            "failure_class": classification.failure_class.value,
                        },
                        "completed_source_ids": completed,
                        "skipped_source_ids": skipped,
                        "failed_source_ids": failed_attempts,
                        "source_skips": source_skips,
                        "source_failures": failures,
                        **metrics(),
                    }
                if classification.failure_class in {
                    _Q2FailureClass.RECONCILIATION_REQUIRED,
                    _Q2FailureClass.CONTROL_INVARIANT_FAILURE,
                }:
                    return {
                        "stage": "extraction",
                        "status": classification.status,
                        "error_code": classification.error_code,
                        "error": error,
                        "details": {
                            "completed_source_ids": completed,
                            "skipped_source_ids": skipped,
                            "failed_source_ids": failed_attempts,
                            "source_skips": source_skips,
                            "source_failures": failures,
                            "failure_class": classification.failure_class.value,
                        },
                        "completed_source_ids": completed,
                        "skipped_source_ids": skipped,
                        "failed_source_ids": failed_attempts,
                        "source_skips": source_skips,
                        "source_failures": failures,
                        **metrics(),
                    }
                return None

        async def execute_individual(work: _Q2SourceWork) -> dict[str, Any] | None:
            nonlocal full_calls, light_calls
            source = work.source
            plan = work.plan
            source_content_sha256 = work.source_content_sha256
            model_run_id = _q2_source_model_run_id(
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                source_id=source.local_id,
                canonical_url=source.canonical_url,
                profile=plan.profile,
            )
            prompt_version = EXTRACTION_PROMPT_VERSION_BY_PROFILE[plan.profile]
            prompt = ProductionPromptTemplates.get_extraction_prompt(
                subject_title,
                source.local_id,
                source.title,
                source.canonical_url,
                profile=plan.profile,
            )
            if plan.profile is ExtractionProfile.FULL:
                full_calls += 1
            else:
                light_calls += 1
            await pace_before_model_call()
            # The progress snapshot immediately before submission is the first
            # state that may claim this source is running.
            _mark_extraction_source_running(progress, source, plan)
            progress["model_calls"] += 1
            await self._persist_extraction_progress(run.id, progress)
            self._diagnostics.record(
                event="q2.source.started",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                pipeline_generation=run.pipeline_generation,
                source_id=source.local_id,
                source_url=source.canonical_url,
                source_content_sha256=source_content_sha256,
                model_run_id=str(model_run_id),
                profile=plan.profile.value,
                access_mode="live_url",
                web_search=True,
            )
            started_at = time.monotonic()
            raw = ""
            execution: Any | None = None
            try:
                execution = await model_gateway.execute(
                    ModelRequest(
                        text=prompt,
                        prompt_template_id="production-q2-url",
                        prompt_template_version=prompt_version,
                        evidence_pack_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                        external_llm_allowed=True,
                        routing_hint=ModelRoutingHint.WEB_RESEARCH,
                        provider=ModelProvider.OPENAI,
                        web_search=True,
                        run_id=model_run_id,
                        allow_failed_resubmit=True,
                        metadata={
                            # The collection hash is provenance, not identity:
                            # a re-archived source must not break the reuse of
                            # this run's ModelRun.
                            "source_id": source.local_id,
                            "source_url": source.canonical_url,
                            "profile": plan.profile.value,
                            "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
                            "parser_version": Q2_MARKDOWN_PARSER_VERSION,
                            "verifier_version": ARTIFACT_VERIFIER_VERSION,
                        },
                        parameters={
                            "q2_execution_kind": "individual",
                        },
                    ),
                    ModelRole.RESEARCH,
                )
                remove_persisted_model_call(execution, profile=plan.profile)
                await self._check_cancellation(run.id, context)
                if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
                    review_details = dict(execution.run.error_details or {})
                    review_details.update(execution.metadata)
                    raise _Q2ControlFailure(
                        execution.run.error_message or "Model run needs review",
                        code=execution.run.error_code or "q2_control_failure",
                        details=review_details,
                    )
                if execution.run.status is not ModelRunStatus.SUCCEEDED:
                    run_status = execution.run.status.value
                    raise _Q2ControlFailure(
                        f"Model run reached unexpected status {run_status}",
                        code=execution.run.error_code or "q2_model_run_not_succeeded",
                        details={
                            **(execution.run.error_details or {}),
                            **execution.metadata,
                            "model_run_status": run_status,
                        },
                    )
                raw = execution.output_text or ""
                if not raw.strip():
                    raise _Q2ControlFailure("Provider returned no Q2 response")
                parsed = parse_q2_proposals_markdown(raw)
                self._log_parse(run, "extraction", parsed)
                if not parsed.usable or parsed.value is None:
                    if _is_q2_source_unavailable(parsed.errors):
                        return await execute_archive_fallback(
                            work,
                            live_failure_code="q2_source_unavailable",
                            live_model_run_id=execution.run.id,
                        )
                    raise _Q2SourceContentFailure(
                        "; ".join(parsed.errors) or "source_content_invalid"
                    )
                filtered_output, profile_warnings = _enforce_q2_profile(parsed.value, plan.profile)
                filtered_output = await gate_source_output(
                    work,
                    filtered_output,
                    model_run_id=execution.run.id,
                )
                warnings.extend(profile_warnings)
                submissions.append(
                    Q2ProposalSubmission(
                        output=filtered_output,
                        source_ids=(source.local_id,),
                        model_run_id=str(execution.run.id),
                    )
                )
                completed.append(source.local_id)
                _mark_extraction_source_complete(
                    progress,
                    source,
                    status="succeeded",
                    counts=_source_progress_counts(filtered_output, source.local_id),
                )
                await persist_q2_checkpoint_keys(
                    execution.run.id,
                    [checkpoint_key(work, batched=False)],
                )
                clear_active_source()
                await self._persist_extraction_progress(run.id, progress)
                url_raw_parts.append(raw)
                warnings.extend(parsed.warnings)
                self._diagnostics.record(
                    event="q2.source.completed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    source_content_sha256=source_content_sha256,
                    model_run_id=str(model_run_id),
                    profile=plan.profile.value,
                    access_mode="live_url",
                    answer_chars=len(raw),
                    facts_count=len(filtered_output.facts),
                    artifacts_count=len(filtered_output.artifacts),
                    rules_count=len(filtered_output.rules),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
                return None
            except JobCancelledError:
                raise
            except Exception as exc:
                await self._check_cancellation(run.id, context)
                if (
                    execution is not None
                    and getattr(execution, "run", None) is not None
                    and execution.run.status is ModelRunStatus.SUCCEEDED
                ):
                    await persist_q2_checkpoint_keys(execution.run.id, [])
                classification = _classify_q2_failure(
                    exc,
                    provider_response_produced=bool(raw),
                )
                _mark_extraction_source_failed(
                    progress,
                    source.local_id,
                    "needs_review" if classification.status == "needs_review" else "failed",
                )
                clear_active_source()
                await self._persist_extraction_progress(run.id, progress)
                error = str(exc)[:1000]
                duration_ms = int((time.monotonic() - started_at) * 1000)
                failed_attempts.append(source.local_id)
                if classification.contributes_to_coverage:
                    failed.append(source.local_id)
                exception_details = getattr(exc, "details", None)
                failures[source.local_id] = {
                    "model_run_id": str(model_run_id),
                    "source_url": source.canonical_url,
                    "error_code": classification.error_code,
                    "error": error,
                    "details": (
                        dict(exception_details) if isinstance(exception_details, dict) else {}
                    ),
                    "retryable": classification.retryable,
                    "phase": classification.phase,
                    "submission_state": classification.submission_state,
                    "failure_class": classification.failure_class.value,
                    "access_mode": "live_url",
                    "contributes_to_coverage": classification.contributes_to_coverage,
                    "duration_ms": duration_ms,
                }
                self._diagnostics.record(
                    event="q2.source.failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    source_id=source.local_id,
                    source_url=source.canonical_url,
                    source_content_sha256=source_content_sha256,
                    model_run_id=str(model_run_id),
                    profile=plan.profile.value,
                    error_code=classification.error_code,
                    error=error,
                    retryable=classification.retryable,
                    phase=classification.phase,
                    submission_state=classification.submission_state,
                    failure_class=classification.failure_class.value,
                    access_mode="live_url",
                    duration_ms=duration_ms,
                )
                if classification.failure_class is _Q2FailureClass.GLOBAL_TRANSIENT_PRE_SUBMISSION:
                    return {
                        "stage": "extraction",
                        "status": "transient_error",
                        "error_code": classification.error_code,
                        "error": error,
                        "details": {
                            "completed_source_ids": completed,
                            "failed_source_ids": failed_attempts,
                            "source_failures": failures,
                            "failure_class": classification.failure_class.value,
                        },
                        "completed_source_ids": completed,
                        "failed_source_ids": failed_attempts,
                        "source_failures": failures,
                        **metrics(),
                    }
                if classification.failure_class in {
                    _Q2FailureClass.RECONCILIATION_REQUIRED,
                    _Q2FailureClass.CONTROL_INVARIANT_FAILURE,
                }:
                    return {
                        "stage": "extraction",
                        "status": classification.status,
                        "error_code": classification.error_code,
                        "error": error,
                        "details": {
                            "completed_source_ids": completed,
                            "failed_source_ids": failed_attempts,
                            "source_failures": failures,
                            "failure_class": classification.failure_class.value,
                        },
                        "completed_source_ids": completed,
                        "failed_source_ids": failed_attempts,
                        "source_failures": failures,
                        **metrics(),
                    }
                return None

        async def execute_batch(
            batch_sources: tuple[Q2BatchSource, ...],
            *,
            model_run_id: UUID,
        ) -> dict[str, Any] | None:
            nonlocal light_calls, light_batches, light_sources_batched
            batch = make_q2_batch(tuple(item.candidate for item in batch_sources))
            # ``batch_sources`` already carries B# labels; rebuild only guards
            # that a caller cannot accidentally submit a differently labelled
            # batch to the parser.
            if tuple(item.batch_id for item in batch.sources) != tuple(
                item.batch_id for item in batch_sources
            ):
                raise _Q2ControlFailure("Batch source mapping is not deterministic")
            prompt = ProductionPromptTemplates.get_ioc_rules_batch_prompt(
                subject_title,
                [(item.batch_id, item.canonical_url) for item in batch_sources],
            )
            light_calls += 1
            light_batches += 1
            light_sources_batched += len(batch_sources)
            await pace_before_model_call()
            # Only this batch is in flight. Future batches remain pending until
            # their own provider submission is about to start.
            for item in batch_sources:
                _mark_extraction_source_running(
                    progress,
                    item.source,
                    pending[item.source.local_id].plan,
                )
            progress["model_calls"] += 1
            progress["light_batches"] = light_batches
            progress["light_sources_batched"] = light_sources_batched
            await self._persist_extraction_progress(run.id, progress)
            self._diagnostics.record(
                event="q2.batch.started",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                batch_model_run_id=str(model_run_id),
                batch_source_ids=[item.source.local_id for item in batch_sources],
                batch_source_urls=[item.canonical_url for item in batch_sources],
                source_count=len(batch_sources),
                access_mode="live_url",
            )
            started_at = time.monotonic()
            raw = ""
            execution: Any | None = None
            try:
                execution = await model_gateway.execute(
                    ModelRequest(
                        text=prompt,
                        prompt_template_id="production-q2-ioc-batch",
                        prompt_template_version=IOC_RULES_BATCH_PROMPT_VERSION,
                        evidence_pack_hash=hashlib.sha256(prompt.encode()).hexdigest(),
                        external_llm_allowed=True,
                        routing_hint=ModelRoutingHint.WEB_RESEARCH,
                        provider=ModelProvider.OPENAI,
                        web_search=True,
                        run_id=model_run_id,
                        allow_failed_resubmit=True,
                        metadata={
                            # Only what the batch identity already carries
                            # belongs here: the gateway hashes this metadata, so
                            # anything else would break the reuse of this run's
                            # ModelRun on a retry.
                            "source_id": f"batch:{model_run_id!s}",
                            "batch_id": str(model_run_id),
                            "batch_source_count": len(batch_sources),
                            "batch_source_urls": [item.canonical_url for item in batch_sources],
                            "ioc_rules_batch_prompt_version": IOC_RULES_BATCH_PROMPT_VERSION,
                            "q2_markdown_parser_version": Q2_MARKDOWN_PARSER_VERSION,
                            "q2_batch_parser_version": Q2_BATCH_PARSER_VERSION,
                        },
                        parameters={
                            "q2_execution_kind": "batch",
                            "q2_batch_sources": [
                                {
                                    "batch_id": item.batch_id,
                                    "canonical_url": item.canonical_url,
                                }
                                for item in batch_sources
                            ],
                        },
                    ),
                    ModelRole.RESEARCH,
                )
                remove_persisted_model_call(
                    execution,
                    profile=ExtractionProfile.IOC_RULES,
                    batched_source_count=len(batch_sources),
                )
                await self._check_cancellation(run.id, context)
                if execution.run.status is ModelRunStatus.NEEDS_REVIEW:
                    review_details = dict(execution.run.error_details or {})
                    review_details.update(execution.metadata)
                    raise _Q2ControlFailure(
                        execution.run.error_message or "Model run needs review",
                        code=execution.run.error_code or "q2_control_failure",
                        details=review_details,
                    )
                if execution.run.status is not ModelRunStatus.SUCCEEDED:
                    run_status = execution.run.status.value
                    raise _Q2ControlFailure(
                        f"Model run reached unexpected status {run_status}",
                        code=execution.run.error_code or "q2_model_run_not_succeeded",
                        details={
                            **(execution.run.error_details or {}),
                            **execution.metadata,
                            "model_run_status": run_status,
                        },
                    )
                raw = execution.output_text or ""
                if not raw.strip():
                    raise _Q2ControlFailure("Provider returned no Q2 response")
                url_raw_parts.append(raw)
                parsed = parse_q2_batch_response(raw, batch.source_mapping)
                self._diagnostics.record(
                    event="q2.batch.parsed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    batch_model_run_id=str(execution.run.id),
                    warnings=list(parsed.warnings),
                    errors=list(parsed.errors),
                )
                warnings.extend(parsed.warnings)
                if not parsed.usable:
                    raise _Q2ControlFailure(
                        "Batch response did not contain a readable expected source",
                        code="batch_response_failure",
                        details={"errors": list(parsed.errors)},
                    )
                by_id = {item.batch_id: item for item in batch_sources}
                archive_fallback_items: list[Q2BatchSource] = []
                for source_result in parsed.sources:
                    item = by_id[source_result.batch_id]
                    warnings.extend(source_result.warnings)
                    if not source_result.usable or source_result.output is None:
                        if source_result.error_code == "batch_source_unavailable":
                            archive_fallback_items.append(item)
                            continue
                        await record_batch_source_failure(
                            item,
                            error_code=source_result.error_code or "batch_source_invalid",
                            model_run_id=execution.run.id,
                            details={"errors": list(source_result.errors)},
                        )
                        continue
                    # Provenance stays local: the model only ever saw B# and
                    # this block is checked against only its own archive.
                    filtered_output, profile_warnings = _enforce_q2_profile(
                        source_result.output, ExtractionProfile.IOC_RULES
                    )
                    try:
                        filtered_output = await gate_source_output(
                            pending[item.source.local_id],
                            filtered_output,
                            model_run_id=execution.run.id,
                            batch_id=item.batch_id,
                        )
                    except _Q2SourceEvidenceUnavailable as exc:
                        await record_batch_source_failure(
                            item,
                            error_code=exc.code,
                            model_run_id=execution.run.id,
                            details=exc.details,
                        )
                        continue
                    warnings.extend(profile_warnings)
                    submissions.append(
                        Q2ProposalSubmission(
                            output=filtered_output,
                            source_ids=(item.source.local_id,),
                            model_run_id=str(execution.run.id),
                        )
                    )
                    completed.append(item.source.local_id)
                    _mark_extraction_source_complete(
                        progress,
                        item.source,
                        status="succeeded",
                        counts=_source_progress_counts(filtered_output, item.source.local_id),
                    )
                    await self._persist_extraction_progress(run.id, progress)
                    self._diagnostics.record(
                        event="q2.source.completed",
                        run_id=run.id,
                        subject_id=run.subject_id,
                        stage="extraction",
                        correlation_id=self._correlation_id,
                        source_id=item.source.local_id,
                        source_url=item.canonical_url,
                        model_run_id=str(execution.run.id),
                        batch_model_run_id=str(model_run_id),
                        batch_id=item.batch_id,
                        profile=ExtractionProfile.IOC_RULES.value,
                        answer_chars=len(source_result.raw_block),
                        facts_count=0,
                        artifacts_count=len(filtered_output.artifacts),
                        rules_count=len(filtered_output.rules),
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
                await persist_q2_checkpoint_keys(
                    execution.run.id,
                    [
                        checkpoint_key(pending[item.source.local_id], batched=True)
                        for item in batch_sources
                        if item.source.local_id in completed
                    ],
                )
                clear_active_source()
                await self._persist_extraction_progress(run.id, progress)
                for item in archive_fallback_items:
                    fallback_result = await execute_archive_fallback(
                        pending[item.source.local_id],
                        live_failure_code="batch_source_unavailable",
                        live_model_run_id=execution.run.id,
                        batch_id=item.batch_id,
                    )
                    if fallback_result is not None:
                        return fallback_result
                return None
            except JobCancelledError:
                raise
            except Exception as exc:
                await self._check_cancellation(run.id, context)
                if (
                    execution is not None
                    and getattr(execution, "run", None) is not None
                    and execution.run.status is ModelRunStatus.SUCCEEDED
                ):
                    await persist_q2_checkpoint_keys(execution.run.id, [])
                classification = _classify_q2_failure(exc, provider_response_produced=False)
                error = str(exc)[:1000]
                duration_ms = int((time.monotonic() - started_at) * 1000)
                exception_details = getattr(exc, "details", None)
                batch_failure = {
                    "batch_model_run_id": str(model_run_id),
                    "source_ids": [item.source.local_id for item in batch_sources],
                    "error_code": classification.error_code,
                    "error": error,
                    "details": (
                        dict(exception_details) if isinstance(exception_details, dict) else {}
                    ),
                    "retryable": classification.retryable,
                    "phase": classification.phase,
                    "submission_state": classification.submission_state,
                    "failure_class": classification.failure_class.value,
                    "duration_ms": duration_ms,
                }
                for item in batch_sources:
                    if _progress_source(progress, item.source.local_id)["status"] not in {
                        "cached",
                        "succeeded",
                    }:
                        _mark_extraction_source_failed(
                            progress,
                            item.source.local_id,
                            "needs_review",
                        )
                clear_active_source()
                await self._persist_extraction_progress(run.id, progress)
                self._diagnostics.record(
                    event="q2.batch.failed",
                    run_id=run.id,
                    subject_id=run.subject_id,
                    stage="extraction",
                    correlation_id=self._correlation_id,
                    batch_model_run_id=batch_failure["batch_model_run_id"],
                    source_ids=batch_failure["source_ids"],
                    error_code=batch_failure["error_code"],
                    error=error,
                    details=batch_failure["details"],
                    retryable=classification.retryable,
                    phase=classification.phase,
                    submission_state=classification.submission_state,
                    failure_class=classification.failure_class.value,
                    access_mode="live_url",
                    duration_ms=duration_ms,
                )
                return {
                    "stage": "extraction",
                    "status": (
                        "transient_error"
                        if classification.failure_class
                        is _Q2FailureClass.GLOBAL_TRANSIENT_PRE_SUBMISSION
                        else classification.status
                    ),
                    "error_code": classification.error_code,
                    "error": error,
                    "details": {
                        "completed_source_ids": completed,
                        "failed_source_ids": failed_attempts,
                        "source_failures": failures,
                        "batch_failure": batch_failure,
                        "failure_class": classification.failure_class.value,
                    },
                    "completed_source_ids": completed,
                    "failed_source_ids": failed_attempts,
                    "source_failures": failures,
                    **metrics(),
                }

        # Q1 ids are provenance keys.  Refuse the whole extraction before a
        # batch can make two indistinguishable submissions.
        if len({source.local_id for source in report.sources}) != len(report.sources):
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": "duplicate_reference_source_id",
                "error": "Q1 source ids must be unique before Q2 extraction",
                "completed_source_ids": [],
                "failed_source_ids": [],
                **metrics(),
            }

        # First pass: plan every source. Planning must not claim work is in
        # flight: all sources were persisted as pending above, and only the
        # request immediately before a provider call changes that state.
        for source in report.sources:
            plan = plans_by_url[source.canonical_url]
            await self._check_cancellation(run.id, context)
            archived = archived_sources.get(source.canonical_url)
            source_content_sha256 = archived.content_sha256 if archived is not None else None
            self._diagnostics.record(
                event="q2.source.plan",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                pipeline_generation=run.pipeline_generation,
                source_id=source.local_id,
                source_url=source.canonical_url,
                source_content_sha256=source_content_sha256,
                profile=plan.profile.value,
                reason=plan.reason,
            )
            work = _Q2SourceWork(
                source=source,
                plan=plan,
                source_content_sha256=source_content_sha256,
            )
            pending[source.local_id] = work

        # Deux sources différentes peuvent avoir archivé exactement le même
        # contenu (page anti-bot, shell JavaScript, redirection). Une seule
        # extraction est nécessaire ; les autres réutilisent son résultat.
        content_twins: dict[str, str] = {}
        duplicate_source_ids: dict[str, str] = {}
        for source_id, work in pending.items():
            digest = work.source_content_sha256
            if not digest:
                continue
            primary = content_twins.get(digest)
            if primary is None:
                content_twins[digest] = source_id
                continue
            duplicate_source_ids[source_id] = primary
            self._diagnostics.record(
                event="q2.source.content_duplicate",
                run_id=run.id,
                subject_id=run.subject_id,
                stage="extraction",
                correlation_id=self._correlation_id,
                source_id=source_id,
                source_url=work.source.canonical_url,
                primary_source_id=primary,
                source_content_sha256=digest,
            )

        # Select reusable results and live candidates only after every source
        # has been planned, so content twins can be excluded consistently.
        for source in report.sources:
            if source.local_id in duplicate_source_ids:
                continue
            plan = plans_by_url[source.canonical_url]
            work = pending[source.local_id]
            candidate = (
                _batch_candidate(source) if plan.profile is ExtractionProfile.IOC_RULES else None
            )
            if candidate is not None:
                batch_candidates.append(candidate)
            else:
                reusable = await load_reusable_source(work, batched=False)
                if reusable is not None:
                    await record_reused_source(work, reusable)
                else:
                    individual_source_ids.add(source.local_id)

        for candidate_group in partition_q2_batch_candidates(batch_candidates):
            remaining: list[Q2BatchCandidate] = []
            for candidate in candidate_group:
                work = pending[candidate.source.local_id]
                reusable = await load_reusable_source(work, batched=True)
                if reusable is None:
                    # An earlier individual IOC_RULES response is also a valid
                    # source checkpoint; it is parsed without batch framing.
                    reusable = await load_reusable_source(work, batched=False)
                if reusable is not None:
                    await record_reused_source(work, reusable)
                else:
                    remaining.append(candidate)

            if len(remaining) == 1:
                # A one-source retry is always the individual IOC_RULES path.
                individual_source_ids.add(remaining[0].source.local_id)
                continue
            if len(remaining) < 2:
                continue
            local_batch = make_q2_batch(remaining)
            batch_run_id = _q2_batch_model_run_id(
                production_run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                canonical_urls=local_batch.canonical_urls,
            )
            light_batches_by_first_source[local_batch.sources[0].source.local_id] = (
                local_batch.sources,
                batch_run_id,
            )

        handled_source_ids = set(completed)
        for source in report.sources:
            if source.local_id in handled_source_ids:
                continue
            prepared_batch = light_batches_by_first_source.get(source.local_id)
            if prepared_batch is not None:
                batch_sources, batch_run_id = prepared_batch
                early_result = await execute_batch(
                    batch_sources,
                    model_run_id=batch_run_id,
                )
                if early_result is not None:
                    return early_result
                handled_source_ids.update(item.source.local_id for item in batch_sources)
                continue
            if source.local_id in individual_source_ids:
                early_result = await execute_individual(pending[source.local_id])
                if early_result is not None:
                    return early_result
                handled_source_ids.add(source.local_id)

        # Chaque doublon de contenu hérite du résultat de sa source primaire :
        # les artefacts restent attribués à la source qui les publie.
        for duplicate_id, primary_id in duplicate_source_ids.items():
            primary_submission = next(
                (item for item in submissions if primary_id in item.source_ids),
                None,
            )
            if primary_submission is None:
                _mark_extraction_source_failed(progress, duplicate_id, "skipped")
                continue
            submissions.append(
                Q2ProposalSubmission(
                    output=primary_submission.output,
                    source_ids=(duplicate_id,),
                    model_run_id=primary_submission.model_run_id,
                )
            )
            completed.append(duplicate_id)
            _mark_extraction_source_complete(
                progress,
                pending[duplicate_id].source,
                status="cached",
                counts=_source_progress_counts(primary_submission.output, duplicate_id),
                cache_hit=True,
            )
            cache_hits += 1
            model_calls_avoided += 1
        await self._persist_extraction_progress(run.id, progress)

        if failed:
            return {
                "stage": "extraction",
                "status": "needs_review",
                "error_code": "q2_source_coverage_failed",
                "error": "One or more Q1 sources could not be analysed",
                "details": {
                    "completed_source_ids": completed,
                    "skipped_source_ids": skipped,
                    "failed_source_ids": failed,
                    "source_skips": source_skips,
                    "source_failures": failures,
                },
                "completed_source_ids": completed,
                "skipped_source_ids": skipped,
                "failed_source_ids": failed,
                "source_skips": source_skips,
                "source_failures": failures,
                "model_calls": progress["model_calls"],
                "full_calls": full_calls,
                "light_calls": light_calls,
                "light_batches": light_batches,
                "light_sources_batched": light_sources_batched,
                "cache_hits": cache_hits,
                "model_calls_avoided": model_calls_avoided,
            }
        self._diagnostics.record(
            event="q2.extraction.metrics",
            run_id=run.id,
            subject_id=run.subject_id,
            stage="extraction",
            correlation_id=self._correlation_id,
            model_calls=progress["model_calls"],
            full_calls=full_calls,
            light_calls=light_calls,
            light_batches=light_batches,
            light_sources_batched=light_sources_batched,
            cache_hits=cache_hits,
            model_calls_avoided=model_calls_avoided,
        )
        verification = verify_q2_proposals(submissions)
        extraction = verification.canonical
        # Source-gate rejections have already been captured while each Q2
        # response was framed. Capture the second rejection boundary too:
        # malformed/placeholder artifacts must be arbitrable with their exact
        # model value, not only with a diagnostic hash.
        source_by_id = {source.local_id: source for source in report.sources}
        diagnostic_offset = 0
        for submission in submissions:
            proposals = [
                *submission.output.facts,
                *submission.output.artifacts,
                *submission.output.rules,
            ]
            submission_diagnostics = verification.diagnostics[
                diagnostic_offset : diagnostic_offset + len(proposals)
            ]
            diagnostic_offset += len(proposals)
            submission_source_id = submission.source_ids[0] if submission.source_ids else None
            submission_source = (
                source_by_id.get(submission_source_id) if submission_source_id is not None else None
            )
            if submission_source is None:
                continue
            for diagnostic, proposal in zip(submission_diagnostics, proposals, strict=True):
                if diagnostic.status.value != "rejected" or diagnostic.proposal_kind not in {
                    "artifact",
                    "rule",
                }:
                    continue
                value = getattr(proposal, "body", None) or getattr(proposal, "value", None)
                if not isinstance(value, str):
                    continue
                issue_kind = (
                    ProductionRepairIssueKind.REJECTED_RULE.value
                    if diagnostic.proposal_kind == "rule"
                    else ProductionRepairIssueKind.REJECTED_INDICATOR.value
                )
                value_sha256 = hashlib.sha256(value.encode("utf-8")).hexdigest()
                repair_key = repair_key_for_rejection(
                    edition_id=run.edition_id,
                    subject_id=run.subject_id,
                    kind=issue_kind,
                    source_url=submission_source.canonical_url,
                    artifact_type=diagnostic.artifact_type,
                    value=value,
                )
                repair_evidence_entries.append(
                    {
                        "repair_key": repair_key,
                        "source_id": submission_source.local_id,
                        "source_title": submission_source.title,
                        "source_url": submission_source.canonical_url,
                        "batch_id": None,
                        "model_run_id": submission.model_run_id,
                        "proposal_index": diagnostic.proposal_index,
                        "proposal_kind": diagnostic.proposal_kind,
                        "artifact_type": diagnostic.artifact_type,
                        "reason_code": diagnostic.reason_code or "rejected",
                        "value": value,
                        "value_sha256": value_sha256,
                    }
                )
                source_evidence_rejections.append(
                    {
                        "source_id": submission_source.local_id,
                        "source_url": submission_source.canonical_url,
                        "batch_id": None,
                        "model_run_id": submission.model_run_id,
                        "proposal_index": diagnostic.proposal_index,
                        "proposal_kind": diagnostic.proposal_kind,
                        "artifact_type": diagnostic.artifact_type,
                        "reason_code": diagnostic.reason_code or "rejected",
                        "value": value[:512],
                        "value_hash": value_sha256,
                    }
                )
        progress.update(_canonical_extraction_progress_counts(extraction))
        progress["active_source_id"] = None
        progress["active_source_title"] = None
        progress["active_profile"] = None
        status_totals = {
            status.value: sum(item.indicator_status is status for item in extraction.items)
            for status in IndicatorStatus
        }
        source_evidence_rejection_groups = [
            {
                "source_id": source_id,
                "batch_id": batch_id,
                "artifact_type": artifact_type,
                "rejection_count": count,
                "reason_code": reason_code,
            }
            for (source_id, batch_id, artifact_type, reason_code), count in (
                source_evidence_rejection_counts.items()
            )
        ]
        # Une règle de détection perdue est un incident éditorial, pas un
        # avertissement de plus : elle ne se retrouve pas, contrairement à un
        # IOC qu'une autre source republiera.
        rejected_rules = [
            rejection
            for rejection in source_evidence_rejections
            if rejection["proposal_kind"] == "rule"
        ]
        rejected_ioc_count = sum(
            is_publication_ioc_artifact_type(rejection.get("artifact_type"))
            for rejection in source_evidence_rejections
            if rejection.get("proposal_kind") == "artifact"
        )
        source_evidence_warnings = [
            (
                f"q2_batch_source_evidence_rejected:{batch_id}:{source_id}:"
                f"{artifact_type}:count={count}:reason={reason_code}"
                if batch_id
                else (
                    f"q2_source_evidence_rejected:{source_id}:{artifact_type}:"
                    f"count={count}:reason={reason_code}"
                )
            )
            for (source_id, batch_id, artifact_type, reason_code), count in (
                source_evidence_rejection_counts.items()
            )
        ]
        if rejected_rules:
            source_evidence_warnings.insert(
                0,
                f"q2_detection_rules_lost:count={len(rejected_rules)}",
            )
        await self._check_cancellation(run.id, context)
        repair_evidence_blob_id = None
        put_repair_evidence = getattr(self._artifact_store, "put_repair_evidence", None)
        if callable(put_repair_evidence):
            repair_evidence_blob_id = await put_repair_evidence(
                build_repair_evidence_pack(repair_evidence_entries)
            )
        repair_evidence_index = [
            {key: value for key, value in entry.items() if key != "value"}
            | {
                "preview": str(entry.get("value", ""))[:512],
            }
            for entry in repair_evidence_entries
        ]
        artifact = await self._extraction.store_extraction_result(
            run_id=run.id,
            subject_id=run.subject_id,
            input_hash=input_hash,
            raw_result="\n\n".join(url_raw_parts),
            canonical_json=technical_extraction_to_json(extraction),
            warnings=[
                *warnings,
                *verification.warnings,
                *source_evidence_warnings,
                *(f"q2_rejected:{item.reason_code}" for item in verification.rejected),
            ],
            verification_diagnostics={
                "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
                "source_evidence_version": SOURCE_EVIDENCE_VERSION,
                "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
                "extraction_profiles": {
                    plan.canonical_url: plan.profile.value for plan in source_plans
                },
                "model_calls": progress["model_calls"],
                "full_calls": full_calls,
                "light_calls": light_calls,
                "light_batches": light_batches,
                "light_sources_batched": light_sources_batched,
                "cache_hits": cache_hits,
                "model_calls_avoided": model_calls_avoided,
                "completed_source_ids": completed,
                "skipped_source_ids": skipped,
                "failed_source_ids": failed,
                "source_skips": source_skips,
                "q2_proposal_diagnostics": [
                    {
                        "status": item.status.value,
                        "proposal_index": item.proposal_index,
                        "proposal_kind": item.proposal_kind,
                        "artifact_type": item.artifact_type,
                        "value_hash": item.value_hash,
                        "reason_code": item.reason_code,
                    }
                    for item in verification.diagnostics
                ],
                "q2_source_evidence_rejections": source_evidence_rejections,
                "q2_source_evidence_rejection_groups": source_evidence_rejection_groups,
                "q2_rejected_rules": rejected_rules,
                "q2_rejected_rule_count": len(rejected_rules),
                "q2_rejected_ioc_count": rejected_ioc_count,
                "q2_rejected_artifact_count": len(source_evidence_rejections) - len(rejected_rules),
                "q2_rejected_other_artifact_count": max(
                    0,
                    len(source_evidence_rejections) - len(rejected_rules) - rejected_ioc_count,
                ),
                "semantic_status_conflicts": [
                    {
                        "artifact_type": item.artifact_type,
                        "value_hash": item.value_hash,
                        "statuses": list(item.statuses),
                        "source_ids": list(item.source_ids),
                    }
                    for item in verification.semantic_status_conflicts
                ],
            },
            repair_evidence_blob_id=repair_evidence_blob_id,
            repair_evidence_entry_count=len(repair_evidence_entries),
            repair_evidence_index=repair_evidence_index,
        )
        await self._persist_extraction_progress(run.id, progress)
        return {
            "stage": "extraction",
            "status": "success",
            "artifact_id": str(artifact.id),
            "items_count": len(extraction.items),
            "rules_count": len(extraction.rules),
            "supported_items": len(extraction.supported_items()),
            "status_totals": status_totals,
            "completed_source_ids": completed,
            "skipped_source_ids": skipped,
            "failed_source_ids": failed,
            "source_skips": source_skips,
            "model_calls": progress["model_calls"],
            "full_calls": full_calls,
            "light_calls": light_calls,
            "light_batches": light_batches,
            "light_sources_batched": light_sources_batched,
            "cache_hits": cache_hits,
            "model_calls_avoided": model_calls_avoided,
        }

    @staticmethod
    def _build_synthesis_evidence_pack(
        report: ReferenceReport,
        extraction: Any,
        source_tiers_by_url: dict[str, str],
    ) -> dict[str, Any]:
        """Deterministic Q4 input, stripped of operational/internal evidence.

        Q4 must write from the verified Q1/Q2 results, not from raw collection
        material. In particular, never expose source URLs or model IDs, or
        items explicitly kept out of publication.
        """
        # Le pack Q4 ne transporte jamais les libellés internes
        # (`indicator_status`, `display_policy`) : le modèle les recopiait dans
        # la prose, ce que la validation rejette ensuite.
        #
        # L'extraction émet souvent deux entrées pour une même valeur : l'une
        # typée sans contexte, l'autre non typée avec le contexte utile. Elles
        # sont fusionnées ici sur (catégorie, valeur), en gardant le plus
        # informatif de chaque attribut.
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for item in extraction.items:
            if (
                not item.supported
                or item.indicator_status is IndicatorStatus.EXCLUDED
                or item.display_policy.value == "hidden"
            ):
                continue
            dedup_key = (item.category or "", item.value.strip().casefold())
            artifact_type = item.artifact_type.value if item.artifact_type else None
            confirmed = item.indicator_status is IndicatorStatus.CONFIRMED_IOC
            existing = merged.get(dedup_key)
            if existing is None:
                merged[dedup_key] = {
                    "category": item.category,
                    "value": item.value,
                    "context": item.context,
                    "source_ids": sorted(item.source_ids),
                    "is_confirmed_indicator": confirmed,
                    "artifact_type": artifact_type,
                }
                order.append(dedup_key)
                continue
            if len(item.context or "") > len(existing["context"] or ""):
                existing["context"] = item.context
            if existing["artifact_type"] is None and artifact_type is not None:
                existing["artifact_type"] = artifact_type
            if confirmed:
                existing["is_confirmed_indicator"] = True
            existing["source_ids"] = sorted(set(existing["source_ids"]) | set(item.source_ids))

        items: list[dict[str, Any]] = [merged[key] for key in order]

        return {
            "version": SYNTHESIS_EVIDENCE_PACK_VERSION,
            "reference_report": {
                "sources": [
                    {
                        "id": source.local_id,
                        "tier": source_tiers_by_url.get(source.canonical_url, "unknown"),
                        "title": source.title,
                        "publisher": source.publisher,
                        "published_at": (
                            source.published_at.isoformat() if source.published_at else None
                        ),
                    }
                    for source in sorted(report.sources, key=lambda source: source.local_id)
                ],
                "events": [
                    {
                        "date": event.event_date.isoformat() if event.event_date else None,
                        "source_ids": sorted(event.source_ids),
                        "text": re.sub(
                            r"\b(?:https?|hxxps?)://\S+",
                            "[URL omitted]",
                            event.text,
                            flags=re.IGNORECASE,
                        ),
                    }
                    for event in sorted(
                        report.events,
                        key=lambda event: (
                            event.event_date.isoformat() if event.event_date else "",
                            event.local_id,
                        ),
                    )
                ],
                "uncertainties": sorted(report.uncertainties),
            },
            "technical_extraction": {
                "items": sorted(
                    items,
                    key=lambda item: (
                        item["category"],
                        item.get("value", ""),
                        item["context"],
                    ),
                ),
                "uncertainties": sorted(extraction.uncertainties),
            },
        }

    async def _execute_synthesis_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            references = await uow.production_artifacts.get_current(run.id, "references")
            if not extraction:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "Extraction artifact not found",
                }
            if references is None:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "References artifact not found",
                }

            report = await self._load_reference_report(references)
            if (
                report is None
                or self._artifact_store is None
                or extraction.canonical_blob_id is None
            ):
                return {
                    "stage": "synthesis",
                    "status": "terminal_error",
                    "error_code": "synthesis_inputs_missing",
                    "error": "Reference or extraction payload is not readable",
                }
            synthesis_research_date = run.research_date or datetime.now(UTC).date()
            synthesis_ctx = await build_subject_production_context(
                uow,
                run.subject_id,
                synthesis_research_date,
                snapshot=snapshot,
                relevant_source_urls={source.canonical_url for source in report.sources},
            )
            synthesis_policy_allows = synthesis_ctx.external_llm_allowed
            extraction_payload = technical_extraction_from_json(
                await self._artifact_store.read_json(extraction.canonical_blob_id)
            )
            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)
            collections = await uow.source_collections.list_for_subject(run.subject_id)
            source_tiers_by_url: dict[str, str] = {}
            if snapshot is not None:
                core_urls = {source.canonical_url for source in snapshot.core_sources}
                relevant_urls = {source.canonical_url for source in report.sources}
                source_tiers_by_url.update({url: "core" for url in core_urls})
                source_tiers_by_url.update({url: "supporting" for url in relevant_urls - core_urls})
            else:
                for collection in collections:
                    if collection.origin_kind in {
                        SourceOriginKind.DISCOVERY,
                        SourceOriginKind.MANUAL,
                    }:
                        source_tiers_by_url[collection.canonical_url] = "core"
                    elif collection.origin_kind is SourceOriginKind.REFERENCE_RESEARCH:
                        source_tiers_by_url[collection.canonical_url] = "supporting"
            synthesis_pack = self._build_synthesis_evidence_pack(
                report, extraction_payload, source_tiers_by_url
            )
            input_hash = _synthesis_input_hash(
                subject_id=run.subject_id,
                references_hash=references.input_hash,
                reference_report_hash=compute_input_hash(reference_report_to_json(report)),
                extraction_hash=extraction.input_hash,
                technical_extraction_hash=compute_input_hash(
                    technical_extraction_to_json(extraction_payload)
                ),
                synthesis_evidence_pack_hash=compute_input_hash(synthesis_pack),
            )
            reused = await self._reuse_artifact(run, "synthesis", input_hash)
            if reused is not None:
                return reused
            if not self._model_service:
                return {
                    "stage": "synthesis",
                    "status": "error",
                    "error": "ModelConversationService not configured",
                }
            if not synthesis_policy_allows:
                return {
                    "stage": "synthesis",
                    "status": "needs_review",
                    "error_code": "external_llm_blocked",
                    "error": "Diffusion policy forbids sending this subject to an external model",
                }

            if run.synthesis_conversation_id is None:
                conversation = await self._open_conversation(
                    run, subject_title, ConversationPurpose.DRAFTING
                )
                run.synthesis_conversation_id = conversation.id
                persisted = await uow.subject_production_runs.get_for_update(run.id)
                if persisted is not None:
                    persisted.synthesis_conversation_id = conversation.id
                    await uow.subject_production_runs.save(persisted)
                    await uow.commit()
            prompt = ProductionPromptTemplates.get_synthesis_prompt(
                subject_title=subject_title,
                synthesis_evidence_pack=json.dumps(
                    synthesis_pack,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )

            try:
                parsed, output_text, turn_id, _ = await self._ask_with_format_repair(
                    run=run,
                    conversation_id=run.synthesis_conversation_id,
                    stage="synthesis",
                    prompt=prompt,
                    prompt_version=SYNTHESIS_PROMPT_VERSION,
                    repair_version=SYNTHESIS_FORMAT_REPAIR_VERSION,
                    mode=ConversationMode.FRESH,
                    parse=lambda text: validate_synthesis(text, report, extraction_payload),
                    external_llm_allowed=synthesis_policy_allows,
                    web_search=True,
                    request_identity=f"g{run.pipeline_generation}",
                    context=context,
                )
                await self._check_cancellation(run.id, context)
                if parsed is None:
                    return {
                        "stage": "synthesis",
                        "status": "needs_review",
                        "error_code": "no_model_response",
                        "error": "No response from model",
                    }
                if not parsed.usable:
                    return {
                        "stage": "synthesis",
                        "status": "needs_review",
                        "error_code": "synthesis_validation_failed",
                        "error": "; ".join(parsed.errors),
                        "details": {
                            "violations": [
                                {
                                    "code": violation.code,
                                    "detail": violation.detail,
                                    "span": violation.span,
                                }
                                for violation in parsed.violations
                            ],
                            "repair_actions": parsed.repair_actions,
                        },
                        "violations": [
                            {
                                "code": violation.code,
                                "detail": violation.detail,
                                "span": violation.span,
                            }
                            for violation in parsed.violations
                        ],
                        "repair_actions": parsed.repair_actions,
                    }

                source_tiers_by_id = {
                    source["id"]: source["tier"]
                    for source in synthesis_pack["reference_report"]["sources"]
                }
                citation_counts = {"core": 0, "supporting": 0, "unknown": 0}
                for source_id in re.findall(r"\[S(\d+)\]", output_text):
                    citation_counts[source_tiers_by_id.get(f"S{source_id}", "unknown")] += 1
                artifact = await self._synthesis.store_synthesis_result(
                    run_id=run.id,
                    subject_id=run.subject_id,
                    input_hash=input_hash,
                    raw_result=output_text,
                    markdown_content=output_text,
                    conversation_turn_id=turn_id,
                    diagnostics={
                        "core_citation_count": citation_counts["core"],
                        "supporting_citation_count": citation_counts["supporting"],
                        "unknown_citation_count": citation_counts["unknown"],
                    },
                )

                result = {
                    "stage": "synthesis",
                    "status": "success",
                    "artifact_id": str(artifact.id),
                    "word_count": len(output_text.split()),
                    "repair_actions": parsed.repair_actions,
                }
                return result
            except JobCancelledError:
                raise
            except Exception as e:
                await self._check_cancellation(run.id, context)
                return self._handle_stage_exception(run, "synthesis", e)

    async def _execute_assembly_stage(
        self,
        run: SubjectProductionRun,
        context: JobExecutionContext | None = None,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> dict[str, Any]:
        """Deterministic: pure rendering from artifacts, no LLM call."""
        await self._check_cancellation(run.id, context)
        async with self._uow_factory() as uow:
            references = await uow.production_artifacts.get_current(run.id, "references")
            extraction = await uow.production_artifacts.get_current(run.id, "extraction")
            synthesis = await uow.production_artifacts.get_current(run.id, "synthesis")

            if references is None or extraction is None or synthesis is None:
                return {
                    "stage": "assembly",
                    "status": "error",
                    "error": "Missing upstream artifacts",
                }

            subject_title, _ = await self._subject_context(uow, run.subject_id, snapshot)
            archived_urls = {
                item.canonical_url
                for item in await uow.source_collections.list_for_subject(run.subject_id)
                if item.state.value in _ARCHIVED_STATES
            }
            publication = await self._assembly.assemble_publication(
                run_id=run.id,
                subject_id=run.subject_id,
                subject_title=subject_title,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
            )

            # QA reads the real payloads, not the counters.
            qa_inputs = await self._load_qa_inputs(references, extraction, synthesis, publication)
            qa_result = await self._qa.run_qa(
                run_id=run.id,
                references_artifact=references,
                extraction_artifact=extraction,
                synthesis_artifact=synthesis,
                publication_artifact=publication,
                archived_urls=archived_urls,
                research_date=run.research_date,
                **qa_inputs,
            )

            await self._check_cancellation(run.id, context)
            ending = await uow.subject_production_runs.get_for_update(run.id)
            if ending is None or ending.status is SubjectProductionStatus.CANCELLED:
                return {
                    "stage": "assembly",
                    "status": "cancelled",
                }

            if qa_result["passed"]:
                ending.mark_ready(now=datetime.now(UTC))
                await uow.subject_production_runs.save(ending)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "success",
                    "run_status": SubjectProductionStatus.READY.value,
                    "qa": qa_result,
                }
            else:
                ending.mark_needs_review(
                    code="qa_failed",
                    message="; ".join(qa_result["errors"]),
                    details=qa_result,
                    now=datetime.now(UTC),
                )
                await uow.subject_production_runs.save(ending)
                await uow.commit()

                return {
                    "stage": "assembly",
                    "status": "needs_review",
                    "run_status": SubjectProductionStatus.NEEDS_REVIEW.value,
                    "qa": qa_result,
                }


def _q2_checkpoint_key(
    *,
    canonical_url: str,
    profile: ExtractionProfile,
    prompt_version: str,
    batch_parser_version: str | None,
    provider: ModelProvider,
    requested_model: str,
) -> str:
    """Return the identity of a reusable successful Q2 source response.

    The identity is deliberately cross-run: the same canonical URL extracted
    with the same profile, contract and prompt versions yields the same
    result. Freshness is bounded at lookup time, not in the key. A batch
    response carries one key for every source it was asked to process; the
    parser decides which of those source results actually succeeded.
    """
    identity = {
        "checkpoint_version": Q2_SUCCESSFUL_CHECKPOINT_VERSION,
        "canonical_url": canonical_url,
        "profile": profile.value,
        "contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "verifier_version": ARTIFACT_VERIFIER_VERSION,
        "prompt_version": prompt_version,
        "q2_markdown_parser_version": Q2_MARKDOWN_PARSER_VERSION,
        "q2_batch_parser_version": batch_parser_version,
        "q2_routing_policy_version": Q2_ROUTING_POLICY_VERSION,
        "q2_model_policy_version": Q2_MODEL_POLICY_VERSION,
        "provider": provider.value,
        "requested_model": requested_model,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _q2_source_model_run_id(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    source_id: str,
    canonical_url: str,
    prompt_version: str | None = None,
    parser_version: str = Q2_MARKDOWN_PARSER_VERSION,
    profile: ExtractionProfile = ExtractionProfile.FULL,
    provider: ModelProvider = ModelProvider.OPENAI,
) -> UUID:
    """Stable ModelRun identity for one Q1 source in a Q2 generation."""
    identity = json.dumps(
        {
            "production_run_id": str(production_run_id),
            "pipeline_generation": pipeline_generation,
            "source_id": source_id,
            "canonical_url": canonical_url,
            "profile": profile.value,
            "prompt_version": prompt_version or EXTRACTION_PROMPT_VERSION_BY_PROFILE[profile],
            "parser_version": parser_version,
            "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
            "provider": provider.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, f"production-q2-source:{identity}")


def _q2_archive_fallback_identity(
    *,
    canonical_url: str,
    source_content_sha256: str,
    profile: ExtractionProfile,
    provider: ModelProvider,
    requested_model: str,
) -> dict[str, Any]:
    """Return the complete functional identity of an archive fallback."""
    return {
        "access_mode": "archive_fallback",
        "canonical_url": canonical_url,
        "source_content_sha256": source_content_sha256,
        "profile": profile.value,
        "contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "prompt_version": EXTRACTION_PROMPT_VERSION_BY_PROFILE[profile],
        "archive_fallback_prompt_version": ARCHIVE_FALLBACK_PROMPT_VERSION,
        "archived_source_access_version": ARCHIVED_SOURCE_ACCESS_VERSION,
        "q2_markdown_parser_version": Q2_MARKDOWN_PARSER_VERSION,
        "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
        "source_evidence_version": SOURCE_EVIDENCE_VERSION,
        "q2_routing_policy_version": Q2_ROUTING_POLICY_VERSION,
        "q2_model_policy_version": Q2_MODEL_POLICY_VERSION,
        "provider": provider.value,
        "requested_model": requested_model,
    }


def _q2_archive_fallback_run_identity(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    source_id: str,
    canonical_url: str,
    source_content_sha256: str,
    profile: ExtractionProfile,
    provider: ModelProvider,
    requested_model: str,
) -> dict[str, Any]:
    """Return the run-local identity used for archive ModelRun IDs."""
    return {
        "production_run_id": str(production_run_id),
        "pipeline_generation": pipeline_generation,
        "source_id": source_id,
        **_q2_archive_fallback_identity(
            canonical_url=canonical_url,
            source_content_sha256=source_content_sha256,
            profile=profile,
            provider=provider,
            requested_model=requested_model,
        ),
    }


def _q2_archive_fallback_model_run_id(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    source_id: str,
    canonical_url: str,
    source_content_sha256: str,
    profile: ExtractionProfile,
    provider: ModelProvider = ModelProvider.OPENAI,
    requested_model: str = "unknown",
) -> UUID:
    """Return an ID that can never collide with the live URL ModelRun."""
    identity = json.dumps(
        _q2_archive_fallback_run_identity(
            production_run_id=production_run_id,
            pipeline_generation=pipeline_generation,
            source_id=source_id,
            canonical_url=canonical_url,
            source_content_sha256=source_content_sha256,
            profile=profile,
            provider=provider,
            requested_model=requested_model,
        ),
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, f"production-q2-archive-fallback:{identity}")


def _q2_archive_fallback_checkpoint_key(
    *,
    canonical_url: str,
    source_content_sha256: str,
    profile: ExtractionProfile,
    provider: ModelProvider = ModelProvider.OPENAI,
    requested_model: str = "unknown",
) -> str:
    """Return the reusable checkpoint key for one exact archived capture."""
    identity = _q2_archive_fallback_identity(
        canonical_url=canonical_url,
        source_content_sha256=source_content_sha256,
        profile=profile,
        provider=provider,
        requested_model=requested_model,
    )
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _q2_batch_model_run_id(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    canonical_urls: Sequence[str],
    provider: ModelProvider = ModelProvider.OPENAI,
) -> UUID:
    """Stable identity for one IOC_RULES web batch inside one production run."""
    return q2_batch_model_run_id(
        production_run_id=production_run_id,
        pipeline_generation=pipeline_generation,
        canonical_urls=canonical_urls,
        routing_policy_version=Q2_ROUTING_POLICY_VERSION,
        provider=provider,
        ioc_rules_batch_prompt_version=IOC_RULES_BATCH_PROMPT_VERSION,
        q2_markdown_parser_version=Q2_MARKDOWN_PARSER_VERSION,
        q2_batch_parser_version=Q2_BATCH_PARSER_VERSION,
    )


def _references_input_hash(
    *,
    subject_id: UUID,
    snapshot: ProductionInputSnapshot | None,
    subject_title: str,
    subject_description: str,
    research_date: Any,
) -> str:
    """Functional Q1 identity; execution/run identities are deliberately absent."""
    snapshot_hash = (
        snapshot.input_hash
        if snapshot is not None
        else compute_input_hash(
            {
                "subject_id": str(subject_id),
                "title": subject_title,
                "description": subject_description,
                "research_date": str(research_date),
            }
        )
    )
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "production_input_snapshot_hash": snapshot_hash,
            "prompt_version": REFERENCES_PROMPT_VERSION,
            "format_repair_version": REFERENCES_FORMAT_REPAIR_VERSION,
            "parser_version": PARSER_VERSION,
            "routing_policy_version": REFERENCES_ROUTING_POLICY_VERSION,
            "stage": "references",
        }
    )


def _extraction_input_hash(
    *,
    subject_id: UUID,
    references_hash: str,
    source_urls: list[str],
    references_payload_hash: str | None = None,
    pipeline_generation: int | None = None,
) -> str:
    """Q2 canonical identity, distinct from per-source model-run identities."""
    # ``pipeline_generation`` remains accepted for callers using the old
    # helper signature, but is intentionally not part of this hash.
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "references_hash": references_hash,
            "references_payload_hash": references_payload_hash or references_hash,
            "source_urls": sorted(source_urls),
            "full_prompt_version": EXTRACTION_PROMPT_VERSION,
            "ioc_rules_prompt_version": IOC_RULES_PROMPT_VERSION,
            "ioc_rules_batch_prompt_version": IOC_RULES_BATCH_PROMPT_VERSION,
            "q2_markdown_parser_version": Q2_MARKDOWN_PARSER_VERSION,
            "q2_batch_parser_version": Q2_BATCH_PARSER_VERSION,
            "artifact_verifier_version": ARTIFACT_VERIFIER_VERSION,
            "source_evidence_version": SOURCE_EVIDENCE_VERSION,
            "iana_tld_snapshot_version": IANA_TLD_SNAPSHOT_VERSION,
            "routing_policy_version": Q2_ROUTING_POLICY_VERSION,
        }
    )


def _synthesis_input_hash(
    *,
    subject_id: UUID,
    references_hash: str,
    reference_report_hash: str,
    extraction_hash: str,
    technical_extraction_hash: str,
    synthesis_evidence_pack_hash: str,
    prompt_version: str = SYNTHESIS_PROMPT_VERSION,
    format_repair_version: str = SYNTHESIS_FORMAT_REPAIR_VERSION,
    web_policy_version: str = "q4-web-non-authoritative-v1",
    routing_policy_version: str = "openai-drafting-v1",
) -> str:
    """Return the functional Q4 identity, excluding run and execution state."""
    return compute_input_hash(
        {
            "subject_id": str(subject_id),
            "references_hash": references_hash,
            "reference_report_hash": reference_report_hash,
            "extraction_hash": extraction_hash,
            "technical_extraction_hash": technical_extraction_hash,
            "synthesis_evidence_pack_version": SYNTHESIS_EVIDENCE_PACK_VERSION,
            "synthesis_evidence_pack_hash": synthesis_evidence_pack_hash,
            "prompt_version": prompt_version,
            "format_repair_version": format_repair_version,
            "web_policy_version": web_policy_version,
            "model_routing_policy": routing_policy_version,
            "stage": "synthesis",
        }
    )
