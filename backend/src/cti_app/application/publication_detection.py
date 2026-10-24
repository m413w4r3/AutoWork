"""Build the immutable YARA rule snapshot included in V6 publications."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast
from uuid import UUID

from cti_app.application.rule_workbench import RuleValidationReason
from cti_app.domain.analyst_opinion import AnalystOpinionV1, OpinionBlockType
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
)
from cti_app.domain.publication_document import (
    PublicationDetectionReason,
    PublicationDetectionStatus,
    PublicationDetectionV1,
)
from cti_app.domain.retrohunt import RetrohuntReservation, RetrohuntReservationState
from cti_app.domain.rule_workbench import RuleTestCorpusRole, RuleValidationStatus


class _DetectionCandidateRepository(Protocol):
    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> Sequence[DetectionCandidateVersion]: ...


class _EvidenceObservationRepository(Protocol):
    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> Sequence[EvidenceObservation]: ...


class _RetrohuntReservationRepository(Protocol):
    async def list_for_subject(self, subject_id: UUID) -> Sequence[RetrohuntReservation]: ...


class _PublicationDetectionUnitOfWork(Protocol):
    @property
    def detection_candidates(self) -> _DetectionCandidateRepository: ...

    @property
    def evidence_observations(self) -> _EvidenceObservationRepository: ...

    @property
    def retrohunt_reservations(self) -> _RetrohuntReservationRepository: ...


class _EvidencePayloadReader(Protocol):
    async def read_json(self, blob_id: UUID) -> Any: ...


class PublicationDetectionDataError(ValueError):
    """Canonical rule data cannot be frozen into a publication safely."""


async def build_publication_detection(
    uow: _PublicationDetectionUnitOfWork,
    evidence_payloads: _EvidencePayloadReader,
    *,
    investigation_id: UUID,
    subject_id: UUID,
    opinion: AnalystOpinionV1,
) -> PublicationDetectionV1 | None:
    """Pin the opinion's referenced rule, or the newest non-rejected rule version.

    Candidate versions and YARA reports are read from their canonical repositories.
    The returned value is a complete immutable snapshot; rendering performs no
    repository reads and derives no validation metrics.
    """
    candidate_repository = getattr(uow, "detection_candidates", None)
    if candidate_repository is None:
        # Legacy in-memory publication contexts predate the investigation-rule
        # repositories. Keep their V6 payload exactly as it was.
        return None

    versions = tuple(
        item
        for item in await candidate_repository.list_for_investigation(investigation_id)
        if item.investigation_id == investigation_id
        and item.status is not DetectionCandidateStatus.REJECTED
    )
    if not versions:
        return None

    observation_repository = getattr(uow, "evidence_observations", None)
    raw_observations = (
        await observation_repository.list_for_investigation(investigation_id)
        if observation_repository is not None
        else ()
    )
    observations = tuple(
        item
        for item in raw_observations
        if item.investigation_id == investigation_id
        and item.kind is EvidenceKind.YARA_MATCH
        and item.object_type == "detection_candidate_version"
    )
    version_ids = {str(item.id) for item in versions}
    observations = tuple(item for item in observations if item.object_id in version_ids)
    opinion_report_refs = {
        reference
        for block in opinion.blocks
        if block.block_type is OpinionBlockType.PROPOSED_DETECTIONS
        for reference in block.evidence_refs
    }
    linked_version_ids = {item.object_id for item in observations if item.id in opinion_report_refs}
    candidates = tuple(
        item for item in versions if not linked_version_ids or str(item.id) in linked_version_ids
    )
    selected = max(candidates, key=lambda item: (item.created_at, item.version, item.id.hex))

    reports = sorted(
        (item for item in observations if item.object_id == str(selected.id)),
        key=lambda item: (item.observed_at, item.id.hex),
    )
    report_payload: Mapping[str, Any] | None = None
    report_observation: EvidenceObservation | None = None
    for observation in reports:
        payload = await evidence_payloads.read_json(observation.blob_id)
        if not isinstance(payload, Mapping):
            raise PublicationDetectionDataError("YARA report payload is not a JSON object")
        if (
            payload.get("schema") != "investigation-yara-test.v1"
            or payload.get("operation") != "test"
        ):
            continue
        if payload.get("source_sha256") != selected.source_sha256:
            raise PublicationDetectionDataError(
                "YARA report source hash does not match its canonical rule version"
            )
        report_payload = payload
        report_observation = observation

    rule_status, reason = _validation_fields(report_payload)
    counts = _provenance_counts(report_payload)
    positive_real_count, positive_synthetic_count = counts.get(
        RuleTestCorpusRole.HUMAN_CONFIRMED.value, (0, 0)
    )
    goodware_counts = counts.get("goodware", (0, 0))
    goodware_tested_count = sum(goodware_counts)
    false_positive_count = _nonnegative_count(
        report_payload.get("false_positive_count", 0) if report_payload is not None else 0,
        "false_positive_count",
    )
    engine_version = _optional_text(
        report_payload.get("engine_version") if report_payload is not None else None,
        "engine_version",
    )
    coverage_statement = _optional_text(
        report_payload.get("coverage_statement") if report_payload is not None else None,
        "coverage_statement",
    )
    limitations = _text_list(
        report_payload.get("limitations", []) if report_payload is not None else [],
        "limitations",
    )
    retrohunt_submitted = await _retrohunt_submission_state(
        uow, subject_id=subject_id, version=selected
    )
    return PublicationDetectionV1(
        candidate_id=selected.candidate_id,
        version_id=selected.id,
        version=selected.version,
        source=selected.source,
        source_sha256=selected.source_sha256,
        candidate_status=selected.status,
        rule_validation_status=rule_status,
        validation_status=_publication_status(rule_status),
        validation_reason=reason,
        positive_real_count=positive_real_count,
        positive_synthetic_count=positive_synthetic_count,
        goodware_tested_count=goodware_tested_count,
        false_positive_count=false_positive_count,
        engine_version=engine_version,
        coverage_statement=coverage_statement,
        limitations=limitations,
        created_at=selected.created_at,
        tested_at=report_observation.observed_at if report_observation is not None else None,
        test_report_observation_id=(
            report_observation.id if report_observation is not None else None
        ),
        test_report_sha256=report_observation.sha256 if report_observation is not None else None,
        retrohunt_submitted=retrohunt_submitted,
    )


def _validation_fields(
    payload: Mapping[str, Any] | None,
) -> tuple[RuleValidationStatus, PublicationDetectionReason]:
    if payload is None:
        return RuleValidationStatus.UNTESTED, PublicationDetectionReason.EXPLICIT_TEST_REQUIRED
    try:
        status = RuleValidationStatus(payload.get("validation_status", "untested"))
    except (TypeError, ValueError):
        status = RuleValidationStatus.UNTESTED
    fallback_reasons = {
        RuleValidationStatus.UNTESTED: RuleValidationReason.EXPLICIT_TEST_REQUIRED,
        RuleValidationStatus.SYNTHETIC_ONLY: RuleValidationReason.SYNTHETIC_POSITIVES,
        RuleValidationStatus.REAL_VALIDATED: RuleValidationReason.VALIDATED,
    }
    try:
        reason = RuleValidationReason(
            payload.get("validation_reason", fallback_reasons.get(status))
        )
    except (TypeError, ValueError):
        reason = RuleValidationReason.EXECUTION_INCOMPLETE
    if status is RuleValidationStatus.REAL_VALIDATED:
        if payload.get("execution_status") != "ok":
            return (
                RuleValidationStatus.REAL_PARTIAL,
                PublicationDetectionReason.EXECUTION_INCOMPLETE,
            )
        signature = payload.get("signature")
        if payload.get("signature_limitations") or (
            isinstance(signature, Mapping) and signature.get("generic_dotnet_guard") == "incomplete"
        ):
            return (
                RuleValidationStatus.REAL_PARTIAL,
                PublicationDetectionReason.SAMPLE_BYTES_MISSING,
            )
        metrics = payload.get("metrics")
        positive = (
            metrics.get("real_positive_sensitivity") if isinstance(metrics, Mapping) else None
        )
        goodware = (
            metrics.get("real_goodware_false_positive_rate")
            if isinstance(metrics, Mapping)
            else None
        )
        if not _is_calculated_rate(positive) or not _is_calculated_rate(goodware):
            return (
                RuleValidationStatus.REAL_PARTIAL,
                PublicationDetectionReason.SAMPLE_BYTES_MISSING,
            )
        assert isinstance(positive, Mapping) and isinstance(goodware, Mapping)
        if positive.get("numerator") != positive.get("denominator"):
            return RuleValidationStatus.REAL_PARTIAL, PublicationDetectionReason.FALSE_NEGATIVES
        if goodware.get("numerator", 0) != 0:
            return RuleValidationStatus.REAL_PARTIAL, PublicationDetectionReason.FALSE_POSITIVES
    return status, PublicationDetectionReason(reason.value)


def _is_calculated_rate(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("status") != "calculated":
        return False
    numerator, denominator = value.get("numerator"), value.get("denominator")
    interval = value.get("confidence_interval_95")
    lower = interval.get("lower") if isinstance(interval, Mapping) else None
    upper = interval.get("upper") if isinstance(interval, Mapping) else None
    return (
        isinstance(numerator, int)
        and not isinstance(numerator, bool)
        and isinstance(denominator, int)
        and not isinstance(denominator, bool)
        and 0 < denominator
        and 0 <= numerator <= denominator
        and value.get("method") == "wilson_score_two_sided_95"
        and isinstance(lower, int | float)
        and not isinstance(lower, bool)
        and math.isfinite(lower)
        and isinstance(upper, int | float)
        and not isinstance(upper, bool)
        and math.isfinite(upper)
        and 0 <= lower <= upper <= 1
    )


def _publication_status(status: RuleValidationStatus) -> PublicationDetectionStatus:
    if status is RuleValidationStatus.REAL_VALIDATED:
        return PublicationDetectionStatus.VALIDATED
    if status is RuleValidationStatus.UNTESTED:
        return PublicationDetectionStatus.UNTESTED
    return PublicationDetectionStatus.CANDIDATE


def _provenance_counts(
    payload: Mapping[str, Any] | None,
) -> dict[str, tuple[int, int]]:
    if payload is None:
        return {}
    raw = payload.get("provenance_counts_by_role", {})
    if not isinstance(raw, Mapping):
        raise PublicationDetectionDataError("YARA report provenance counts are invalid")
    result: dict[str, tuple[int, int]] = {}
    positive_role = RuleTestCorpusRole.HUMAN_CONFIRMED.value
    legacy_positive_role = "confirmed_positive"
    if positive_role in raw and legacy_positive_role in raw:
        raise PublicationDetectionDataError(
            "YARA report has both canonical and legacy positive role counts"
        )
    for role in (positive_role, RuleTestCorpusRole.GOODWARE.value):
        raw_key = (
            legacy_positive_role if role == positive_role and positive_role not in raw else role
        )
        counts = raw.get(raw_key, {})
        if not isinstance(counts, Mapping):
            raise PublicationDetectionDataError(f"YARA report {raw_key} counts are invalid")
        result[role] = (
            _nonnegative_count(counts.get("real", 0), f"{raw_key}.real"),
            _nonnegative_count(counts.get("synthetic", 0), f"{raw_key}.synthetic"),
        )
    return result


def _nonnegative_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicationDetectionDataError(f"YARA report {name} must be a non-negative integer")
    return cast(int, value)


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PublicationDetectionDataError(f"YARA report {name} must be text or null")
    return value


def _text_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise PublicationDetectionDataError(f"YARA report {name} must be a list of text")
    if any(not isinstance(item, str) for item in value):
        raise PublicationDetectionDataError(f"YARA report {name} must be a list of text")
    return tuple(value)


async def _retrohunt_submission_state(
    uow: _PublicationDetectionUnitOfWork,
    *,
    subject_id: UUID,
    version: DetectionCandidateVersion,
) -> bool | None:
    repository = getattr(uow, "retrohunt_reservations", None)
    if repository is None:
        return None
    reservations = tuple(
        item
        for item in await repository.list_for_subject(subject_id)
        if item.investigation_id == version.investigation_id
        and item.rule_version_id == version.id
        and item.rule_source_sha256 == version.source_sha256
    )
    if not reservations:
        return False
    if any(
        item.provider_job_id is not None
        or item.state in {RetrohuntReservationState.SUBMITTED, RetrohuntReservationState.COMPLETED}
        for item in reservations
    ):
        return True
    if any(
        item.state
        in {
            RetrohuntReservationState.SUBMITTING,
            RetrohuntReservationState.RECONCILIATION_REQUIRED,
        }
        for item in reservations
    ):
        return None
    return False
