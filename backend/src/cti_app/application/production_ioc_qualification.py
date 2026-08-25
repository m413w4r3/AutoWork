"""Strict model qualification of deterministic IOC candidates.

The model classifies evidence; it never owns literal IOC facts.  This module is
pure so coverage and merge invariants are testable without a conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from cti_app.application.production_ioc_candidates import IocCandidate, IocCandidateBatch
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    SemanticType,
    TechnicalExtraction,
)

IOC_QUALIFICATION_PROMPT_VERSION: Final = "1"
IOC_QUALIFICATION_PARSER_VERSION: Final = "1"
MAX_REASON_CHARS: Final = 600


class QualificationStatus(StrEnum):
    CONFIRMED_IOC = "confirmed_ioc"
    CONTEXTUAL = "contextual"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class IocQualification:
    candidate_id: str
    status: QualificationStatus
    reason: str


@dataclass(slots=True)
class QualificationParseResult:
    qualifications: tuple[IocQualification, ...]
    errors: list[str]
    missing_candidate_ids: tuple[str, ...]
    unknown_candidate_ids: tuple[str, ...]
    warnings: list[str]
    repair_actions: list[str]
    dropped_blocks: list[str]

    @property
    def usable(self) -> bool:
        return not self.errors


_FIELD = re.compile(r"^\s*(candidate-id|status|reason)\s*:\s*(.*?)\s*$", re.I)


def parse_ioc_qualifications(text: str, batch: IocCandidateBatch) -> QualificationParseResult:
    """Parse exactly the small qualification contract and enforce N/N coverage."""
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _FIELD.match(line)
        if not match:
            continue
        key, value = match.group(1).lower(), match.group(2)
        if key == "candidate-id" and current:
            rows.append(current)
            current = {}
        current[key] = value
    if current:
        rows.append(current)

    expected = {candidate.candidate_id for candidate in batch.candidates}
    seen: set[str] = set()
    unknown: set[str] = set()
    errors: list[str] = []
    qualifications: list[IocQualification] = []
    for row in rows:
        candidate_id = row.get("candidate-id", "")
        if not candidate_id or not row.get("status") or "reason" not in row:
            errors.append("ioc_qualification_malformed")
            continue
        if candidate_id in seen:
            errors.append("ioc_candidate_duplicate")
            continue
        seen.add(candidate_id)
        if candidate_id not in expected:
            unknown.add(candidate_id)
            errors.append("ioc_candidate_unknown")
            continue
        try:
            status = QualificationStatus(row["status"].strip().casefold())
        except ValueError:
            errors.append("ioc_qualification_status_invalid")
            continue
        qualifications.append(IocQualification(candidate_id, status, _clean_reason(row["reason"])))
    missing = expected - seen
    if missing:
        errors.append("ioc_candidate_coverage_incomplete")
    return QualificationParseResult(
        qualifications=tuple(qualifications),
        errors=sorted(set(errors)),
        missing_candidate_ids=tuple(sorted(missing)),
        unknown_candidate_ids=tuple(sorted(unknown)),
        warnings=[],
        repair_actions=[],
        dropped_blocks=[],
    )


def merge_qualified_candidates(
    extraction: TechnicalExtraction,
    qualifications: tuple[IocQualification, ...],
    candidates: tuple[IocCandidate, ...],
) -> TechnicalExtraction:
    """Append deterministic candidate items, with display strictly mapped by status."""
    by_qualification = {item.candidate_id: item for item in qualifications}
    # Q2 IOC literals have no authority. Candidate items below replace them.
    candidate_values = {
        (candidate.artifact_type, candidate.normalized_value) for candidate in candidates
    }
    general_items = tuple(
        item
        for item in extraction.items
        if not (
            item.artifact_type is not None
            and (item.artifact_type, item.normalized_value) in candidate_values
        )
    )
    items: list[ExtractionItem] = list(general_items)
    for index, candidate in enumerate(candidates, 1):
        qualification = by_qualification[candidate.candidate_id]
        effective_status = qualification.status
        reason = qualification.reason
        if not candidate.source_backed and effective_status is QualificationStatus.CONFIRMED_IOC:
            effective_status = QualificationStatus.CONTEXTUAL
            reason = "discovery_only_without_literal_source_evidence: " + reason
        status, policy = _status_policy(effective_status)
        items.append(
            ExtractionItem(
                local_id=f"IOC{index}",
                category="network_artifacts",
                value=candidate.preferred_original_value,
                context=reason,
                artifact_type=candidate.artifact_type,
                attack_id=None,
                reference_ids=(),
                source_ids=candidate.source_ids,
                supported=bool(candidate.source_ids),
                semantic_type=SemanticType.INDICATOR,
                indicator_status=status,
                provenance=IndicatorProvenance.SOURCE,
                display_policy=policy,
                normalized_value=candidate.normalized_value,
            )
        )
    return TechnicalExtraction(items=tuple(items), uncertainties=extraction.uncertainties)


def effective_qualification_statuses(
    qualifications: tuple[IocQualification, ...], candidates: tuple[IocCandidate, ...]
) -> tuple[QualificationStatus, ...]:
    """Return statuses after applying the source-backed invariant."""
    by_id = {qualification.candidate_id: qualification.status for qualification in qualifications}
    statuses: list[QualificationStatus] = []
    for candidate in candidates:
        status = by_id[candidate.candidate_id]
        if not candidate.source_backed and status is QualificationStatus.CONFIRMED_IOC:
            status = QualificationStatus.CONTEXTUAL
        statuses.append(status)
    return tuple(statuses)


def _status_policy(status: QualificationStatus) -> tuple[IndicatorStatus, DisplayPolicy]:
    if status is QualificationStatus.CONFIRMED_IOC:
        return IndicatorStatus.CONFIRMED_IOC, DisplayPolicy.IOC_SECTION
    if status is QualificationStatus.CONTEXTUAL:
        return IndicatorStatus.CONTEXTUAL, DisplayPolicy.BODY_ONLY
    return IndicatorStatus.EXCLUDED, DisplayPolicy.HIDDEN


def _clean_reason(reason: str) -> str:
    return " ".join(reason.split())[:MAX_REASON_CHARS]
