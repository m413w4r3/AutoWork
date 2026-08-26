"""Deterministic gate between Q2 proposals and canonical extraction."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from urllib.parse import urlsplit

from cti_app.application.iana_tlds_snapshot import IANA_TLDS
from cti_app.application.production_normalization import normalize_indicator_value, refang
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2SourceOutput,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.domain.publication import ArtifactType

# Bump whenever deterministic verification/normalization rules change (e.g. a
# validation rule, a public-suffix check, or how facts get a semantic type).
# Participates in the extraction artifact's input_hash so a canonical
# extraction artifact gets recomputed, without forcing a new Q2 model call.
ARTIFACT_VERIFIER_VERSION = "1"


class ProposalStatus(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Q2ProposalSubmission:
    """One stateless Q2 call, bound by orchestration to one Q1 source."""

    output: Q2SourceOutput
    source_ids: tuple[str, ...]
    model_run_id: str | None = None


@dataclass(frozen=True)
class ProposalDiagnostic:
    status: ProposalStatus
    proposal_index: int
    proposal_kind: str
    artifact_type: str | None
    value_hash: str
    reason_code: str | None = None


@dataclass(frozen=True)
class ArtifactVerificationResult:
    canonical: TechnicalExtraction
    diagnostics: tuple[ProposalDiagnostic, ...]
    warnings: tuple[str, ...]

    @property
    def rejected(self) -> tuple[ProposalDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.status is ProposalStatus.REJECTED)


_HEX = re.compile(r"^[0-9a-fA-F]+$")
_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

# Deterministic Q2FactProposal.category -> SemanticType.  The model only ever
# emits a structured category; this table, not the model, decides the
# semantic type.  Categories not listed fall back to SemanticType.OTHER.
_SEMANTIC_TYPE_BY_FACT_CATEGORY: Mapping[str, SemanticType] = {
    "actors": SemanticType.ACTOR,
    "campaigns": SemanticType.CAMPAIGN,
    "malware": SemanticType.MALWARE,
    "tools": SemanticType.TOOL,
    "ttps": SemanticType.TECHNIQUE,
    "protocols": SemanticType.PROTOCOL,
    "infrastructure": SemanticType.INFRASTRUCTURE,
    "files": SemanticType.FILE,
}


def verify_q2_proposals(
    submissions: Sequence[Q2ProposalSubmission],
) -> ArtifactVerificationResult:
    """Validate IOC shape and system-assigned provenance, not local evidence."""
    verified: list[ExtractionItem] = []
    diagnostics: list[ProposalDiagnostic] = []
    for submission in submissions:
        proposals: list[Q2FactProposal | Q2ArtifactProposal] = [
            *submission.output.facts,
            *submission.output.artifacts,
        ]
        for index, proposal in enumerate(proposals, start=1):
            rejection = _rejection_reason(proposal)
            if rejection is not None:
                diagnostics.append(
                    ProposalDiagnostic(
                        ProposalStatus.REJECTED,
                        index,
                        proposal_kind=_proposal_kind(proposal),
                        artifact_type=_artifact_type(proposal),
                        value_hash=_value_hash(proposal.value),
                        reason_code=rejection,
                    )
                )
                continue
            try:
                verified.append(_to_item(proposal, index, submission))
            except ValueError:
                diagnostics.append(
                    ProposalDiagnostic(
                        ProposalStatus.REJECTED,
                        index,
                        proposal_kind=_proposal_kind(proposal),
                        artifact_type=_artifact_type(proposal),
                        value_hash=_value_hash(proposal.value),
                        reason_code="normalization_error",
                    )
                )
                continue
            diagnostics.append(
                ProposalDiagnostic(
                    ProposalStatus.VERIFIED,
                    index,
                    _proposal_kind(proposal),
                    _artifact_type(proposal),
                    _value_hash(proposal.value),
                )
            )
    merged, warnings = _merge_verified(verified)
    uncertainties = tuple(
        dict.fromkeys(
            uncertainty
            for submission in submissions
            for uncertainty in submission.output.uncertainties
        )
    )
    return ArtifactVerificationResult(
        TechnicalExtraction(tuple(merged), uncertainties), tuple(diagnostics), tuple(warnings)
    )


def _rejection_reason(
    proposal: Q2FactProposal | Q2ArtifactProposal,
) -> str | None:
    if isinstance(proposal, Q2ArtifactProposal):
        if _is_placeholder(proposal.value):
            return "redacted_placeholder"
        try:
            artifact_type = ArtifactType(proposal.artifact_type)
        except ValueError:
            return "invalid_artifact_type"
        try:
            _validate_value(proposal.value, artifact_type)
        except ValueError:
            return "invalid_value"
        try:
            normalize_indicator_value(proposal.value, artifact_type)
        except ValueError:
            return "normalization_error"
    return None


def _is_placeholder(value: str) -> bool:
    folded = value.casefold()
    return bool(re.search(r"redacted|\bfuzz\b|<[^>]+>|example\.(?:com|org|net)", folded))


def _to_item(
    proposal: Q2FactProposal | Q2ArtifactProposal,
    index: int,
    submission: Q2ProposalSubmission,
) -> ExtractionItem:
    if isinstance(proposal, Q2FactProposal):
        return ExtractionItem(
            local_id=f"Q2F{index}",
            category=proposal.category,
            value=proposal.value,
            context=proposal.context,
            artifact_type=None,
            semantic_type=_SEMANTIC_TYPE_BY_FACT_CATEGORY.get(
                proposal.category, SemanticType.OTHER
            ),
            attack_id=proposal.attack_id,
            reference_ids=(),
            source_ids=submission.source_ids,
            supported=bool(submission.source_ids),
            evidence_quote=proposal.evidence_quote,
            model_run_ids=(submission.model_run_id,) if submission.model_run_id else (),
        )
    artifact_type = ArtifactType(proposal.artifact_type)
    category, semantic_type, status, display_policy = _artifact_fields(
        artifact_type, IndicatorStatus(proposal.indicator_status)
    )
    return ExtractionItem(
        local_id=f"Q2A{index}",
        category=category,
        value=proposal.value,
        context=proposal.context,
        artifact_type=artifact_type,
        semantic_type=semantic_type,
        indicator_status=status,
        display_policy=display_policy,
        normalized_value=normalize_indicator_value(proposal.value, artifact_type),
        attack_id=None,
        reference_ids=(),
        source_ids=submission.source_ids,
        supported=bool(submission.source_ids),
        evidence_quote=proposal.evidence_quote,
        model_run_ids=(submission.model_run_id,) if submission.model_run_id else (),
    )


def _validate_value(raw: str, artifact_type: ArtifactType) -> None:
    value = refang(raw)
    if artifact_type is ArtifactType.IP:
        ipaddress.ip_address(value)
    elif artifact_type is ArtifactType.DOMAIN:
        _validate_hostname(value)
    elif artifact_type is ArtifactType.URL:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ValueError("invalid URL")
        try:
            ipaddress.ip_address(parts.hostname)
        except ValueError:
            _validate_hostname(parts.hostname)
        _ = parts.port
    elif artifact_type is ArtifactType.HASH:
        if len(value) not in {32, 40, 64, 128} or not _HEX.fullmatch(value):
            raise ValueError("invalid hash")
    elif artifact_type is ArtifactType.EMAIL:
        local, marker, domain = value.rpartition("@")
        if not marker or not local or len(local) > 64 or any(c.isspace() for c in local):
            raise ValueError("invalid email")
        _validate_hostname(domain)
    elif artifact_type is ArtifactType.CVE:
        if not _CVE.fullmatch(value):
            raise ValueError("invalid CVE")
    elif artifact_type in {ArtifactType.FILENAME, ArtifactType.FILEPATH}:
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid file value")
    # Rule identifiers need literal proof only; evidence checks already did it.


def _validate_hostname(raw: str) -> None:
    hostname = raw.rstrip(".").lower()
    if len(hostname) > 253 or "/" in hostname or "\\" in hostname or hostname.count(".") < 1:
        raise ValueError("invalid hostname")
    labels = hostname.split(".")
    # DNS standard: each label must not exceed 63 characters
    if any(not _LABEL.fullmatch(label) or len(label) > 63 for label in labels):
        raise ValueError("invalid hostname")
    if labels[-1] not in IANA_TLDS:
        raise ValueError("unknown public suffix")
    # File extensions and glued prose commonly pass label syntax; require a
    # plausible registrable label, never an extension-looking final label.
    if labels[-1] in {"exe", "php", "txt", "pdf"}:
        raise ValueError("file or prose fragment")


def _proposal_kind(proposal: Q2FactProposal | Q2ArtifactProposal) -> str:
    return "artifact" if isinstance(proposal, Q2ArtifactProposal) else "fact"


def _artifact_type(proposal: Q2FactProposal | Q2ArtifactProposal) -> str | None:
    return proposal.artifact_type if isinstance(proposal, Q2ArtifactProposal) else None


def _value_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_fields(
    artifact_type: ArtifactType, status: IndicatorStatus
) -> tuple[str, SemanticType, IndicatorStatus, DisplayPolicy]:
    if artifact_type in {
        ArtifactType.YARA_RULE,
        ArtifactType.SIGMA_RULE,
        ArtifactType.SURICATA_RULE,
    }:
        return (
            "detections",
            SemanticType.OTHER,
            IndicatorStatus.NOT_APPLICABLE,
            DisplayPolicy.BODY_ONLY,
        )
    if artifact_type in {ArtifactType.FILENAME, ArtifactType.FILEPATH}:
        return "files", SemanticType.FILE, status, _display_policy(status, allow_ioc=False)
    if artifact_type is ArtifactType.CVE:
        return "cves", SemanticType.OTHER, status, _display_policy(status, allow_ioc=False)
    return "network_artifacts", SemanticType.INDICATOR, status, _display_policy(status)


def _display_policy(status: IndicatorStatus, *, allow_ioc: bool = True) -> DisplayPolicy:
    if status is IndicatorStatus.EXCLUDED:
        return DisplayPolicy.HIDDEN
    if allow_ioc and status is IndicatorStatus.CONFIRMED_IOC:
        return DisplayPolicy.IOC_SECTION
    return DisplayPolicy.BODY_ONLY


def _merge_verified(items: Sequence[ExtractionItem]) -> tuple[list[ExtractionItem], list[str]]:
    merged: dict[tuple[object, ...], ExtractionItem] = {}
    warnings: list[str] = []
    for item in items:
        key = (
            (item.artifact_type, item.normalized_value)
            if item.artifact_type
            else (item.category, item.value.casefold())
        )
        previous = merged.get(key)
        if previous is None:
            merged[key] = item
            continue
        statuses = {previous.indicator_status, item.indicator_status}
        if len(statuses) > 1:
            warnings.append("semantic_status_conflict")
        status = _merged_status(statuses)
        context = " | ".join(
            dict.fromkeys(part for part in (previous.context, item.context) if part)
        )
        merged[key] = replace(
            previous,
            context=context,
            indicator_status=status,
            display_policy=_display_policy(
                status, allow_ioc=previous.semantic_type is SemanticType.INDICATOR
            ),
            source_ids=tuple(sorted(set(previous.source_ids + item.source_ids))),
            model_run_ids=tuple(sorted(set(previous.model_run_ids + item.model_run_ids))),
            supported=previous.supported or item.supported,
        )
    ordered = [merged[key] for key in sorted(merged, key=str)]
    artifact_number = fact_number = 0
    canonical: list[ExtractionItem] = []
    for item in ordered:
        if item.artifact_type is None:
            fact_number += 1
            local_id = f"Q2F{fact_number}"
        else:
            artifact_number += 1
            local_id = f"Q2A{artifact_number}"
        canonical.append(replace(item, local_id=local_id))
    return canonical, list(dict.fromkeys(warnings))


def _merged_status(statuses: set[IndicatorStatus]) -> IndicatorStatus:
    if IndicatorStatus.CONFIRMED_IOC in statuses and IndicatorStatus.EXCLUDED in statuses:
        return IndicatorStatus.CONTEXTUAL
    if IndicatorStatus.CONFIRMED_IOC in statuses:
        return IndicatorStatus.CONFIRMED_IOC
    if IndicatorStatus.CONTEXTUAL in statuses:
        return IndicatorStatus.CONTEXTUAL
    if statuses == {IndicatorStatus.EXCLUDED}:
        return IndicatorStatus.EXCLUDED
    return IndicatorStatus.NOT_APPLICABLE
