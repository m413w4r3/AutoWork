"""Quality-gated VT Retrohunt submission with durable subject-wide slots."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4

from cti_app.application.detection_review_service import DetectionReviewService
from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.virustotal import VirusTotalRetrohuntPort
from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    EvidenceKind,
    InvestigationEnvelope,
    TLPLevel,
)
from cti_app.domain.retrohunt import (
    RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT,
    RetrohuntProviderErrorCode,
    RetrohuntProviderJobState,
    RetrohuntReservation,
    RetrohuntReservationState,
    RetrohuntSubmitRequest,
)
from cti_app.domain.rule_workbench import RuleTestCorpusRole

MAX_SUBMISSIONS_PER_SUBJECT = RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT
RETROHUNT_CAPABILITY = "vt.retrohunt"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PARAMETER_KEY = re.compile(
    r"(?:api[_-]?key|token|secret|password|cookie|authorization)", re.I
)


class RetrohuntError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RetrohuntUnavailableError(RetrohuntError):
    def __init__(self) -> None:
        super().__init__(
            "capability_unavailable", "A verified Retrohunt proxy route is unavailable."
        )


class RetrohuntQualityError(RetrohuntError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


class RetrohuntSlotLimitError(RetrohuntError):
    def __init__(self, slots_consumed: int = MAX_SUBMISSIONS_PER_SUBJECT) -> None:
        super().__init__(
            "subject_slot_limit", "This subject has already reserved both Retrohunt slots."
        )
        self.slots_consumed = min(MAX_SUBMISSIONS_PER_SUBJECT, max(0, slots_consumed))
        self.slots_remaining = max(0, MAX_SUBMISSIONS_PER_SUBJECT - self.slots_consumed)


class RetrohuntReservationNotFoundError(RetrohuntError):
    def __init__(self) -> None:
        super().__init__(
            "reservation_not_found", "The Retrohunt reservation does not exist for this subject."
        )


class RetrohuntSubmissionRejected(RuntimeError):
    """Provider-confirmed rejection; the already reserved slot remains consumed."""


class _ReviewService(Protocol):
    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]: ...


@dataclass(frozen=True, slots=True)
class RetrohuntOutcome:
    reservation: RetrohuntReservation
    slots_consumed: int
    slots_remaining: int
    provider_job_state: str
    provider_error: RetrohuntProviderErrorCode | None = None
    candidate_hits: tuple[str, ...] = ()
    observation_refs: tuple[UUID, ...] = ()


class RetrohuntService:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        review_service: DetectionReviewService | _ReviewService,
        provider: VirusTotalRetrohuntPort,
        evidence_recorder: EvidenceRecorder,
    ) -> None:
        self._uow_factory = uow_factory
        self._review_service = review_service
        self._provider = provider
        self._evidence_recorder = evidence_recorder

    async def submit(
        self,
        *,
        investigation_id: UUID,
        version_id: UUID,
        source_sha256: str,
        corpus_id: str,
        corpus_sha256: str,
        parameters: Mapping[str, Any],
        envelope: InvestigationEnvelope | None,
    ) -> RetrohuntOutcome:
        self._require_available()
        if (
            envelope is None
            or "vt" not in envelope.allowed_providers
            or envelope.disclosure.tlp in {TLPLevel.AMBER, TLPLevel.RED}
        ):
            raise RetrohuntError(
                "policy_denied", "The investigation disclosure policy forbids submissions."
            )
        source_sha256 = _require_sha256(source_sha256, "source_sha256")
        corpus_sha256 = _require_sha256(corpus_sha256, "corpus_sha256")
        if not corpus_id.strip() or len(corpus_id) > 256 or _has_control(corpus_id):
            raise RetrohuntQualityError(
                "invalid_corpus", "A bounded corpus identifier is required."
            )
        corpus_id = corpus_id.strip()
        clean_parameters = _normalize_parameters(parameters)
        version = await self._prevalidated_version(investigation_id, version_id, source_sha256)
        subject_id = await self._subject_id(investigation_id)
        operation_key = _operation_key(source_sha256, corpus_id, corpus_sha256, clean_parameters)
        reservation = RetrohuntReservation(
            subject_id=subject_id,
            investigation_id=investigation_id,
            slot_number=1,
            operation_key=operation_key,
            rule_version_id=version.id,
            rule_source_sha256=source_sha256,
            corpus_id=corpus_id,
            corpus_sha256=corpus_sha256,
            parameters=clean_parameters,
        )

        submission_token: UUID | None = None
        async with self._uow_factory() as uow:
            reserved = await uow.retrohunt_reservations.reserve(reservation)
            if reserved is None:
                consumed = await uow.retrohunt_reservations.count_for_subject(subject_id)
                raise RetrohuntSlotLimitError(consumed)
            current, _created = reserved
            # This commit is the durable reservation boundary. No provider
            # request is made until the slot row is committed.
            await uow.commit()

        async with self._uow_factory() as uow:
            repository = uow.retrohunt_reservations
            latest = await repository.get_for_subject(subject_id, current.id)
            if latest is None:
                raise RetrohuntReservationNotFoundError()
            if latest.state is RetrohuntReservationState.RESERVED:
                token = uuid4()
                claimed = latest.transition(
                    RetrohuntReservationState.SUBMITTING,
                    submission_token=token,
                )
                updated = await repository.update_if_version(
                    claimed, expected_version=latest.version
                )
                if updated is None:
                    current = await repository.get_for_subject(subject_id, latest.id) or latest
                else:
                    current = updated
                    submission_token = token
                    await uow.commit()
            elif latest.state is RetrohuntReservationState.SUBMITTING:
                # Once a submission claim is durable, a replay cannot tell
                # whether the request reached VT. Never send it again.
                reconciled = latest.transition(RetrohuntReservationState.RECONCILIATION_REQUIRED)
                updated = await repository.update_if_version(
                    reconciled, expected_version=latest.version
                )
                if updated is not None:
                    current = updated
                    await uow.commit()
                else:
                    current = await repository.get_for_subject(subject_id, latest.id) or latest

        if submission_token is None:
            return await self._outcome(subject_id, current, provider_job_state=current.state.value)

        request = RetrohuntSubmitRequest(
            subject_id=subject_id,
            investigation_id=investigation_id,
            reservation_id=current.id,
            slot_number=current.slot_number,
            rule_version_id=version.id,
            rule_source=version.source,
            rule_source_sha256=source_sha256,
            corpus_id=corpus_id,
            corpus_sha256=corpus_sha256,
            parameters=clean_parameters,
        )
        try:
            job = await self._provider.submit_retrohunt(request)
        except RetrohuntSubmissionRejected:
            current = await self._transition_submission(
                current.id,
                subject_id,
                submission_token,
                RetrohuntReservationState.FAILED,
            )
            return await self._outcome(subject_id, current, provider_job_state="failed")
        except Exception:
            current = await self._transition_submission(
                current.id,
                subject_id,
                submission_token,
                RetrohuntReservationState.RECONCILIATION_REQUIRED,
            )
            return await self._outcome(
                subject_id, current, provider_job_state="reconciliation_required"
            )

        current = await self._transition_submission(
            current.id,
            subject_id,
            submission_token,
            RetrohuntReservationState.SUBMITTED,
            provider_job_id=job.job_id,
        )
        return await self._outcome(subject_id, current, provider_job_state=job.state.value)

    async def status(self, *, investigation_id: UUID, reservation_id: UUID) -> RetrohuntOutcome:
        self._require_available()
        subject_id = await self._subject_id(investigation_id)
        current = await self._reservation(subject_id, reservation_id)
        provider_job_state = current.state.value
        if current.provider_job_id is not None and current.state not in {
            RetrohuntReservationState.COMPLETED,
            RetrohuntReservationState.FAILED,
        }:
            try:
                job = await self._provider.retrohunt_status(current.provider_job_id)
            except Exception:
                return await self._outcome(
                    subject_id,
                    current,
                    provider_job_state=provider_job_state,
                    provider_error=RetrohuntProviderErrorCode.PROVIDER_UNAVAILABLE,
                )
            if job.job_id != current.provider_job_id:
                return await self._outcome(
                    subject_id,
                    current,
                    provider_job_state=provider_job_state,
                    provider_error=RetrohuntProviderErrorCode.PROVIDER_UNAVAILABLE,
                )
            provider_job_state = job.state.value
            if job.state in {
                RetrohuntProviderJobState.COMPLETED,
                RetrohuntProviderJobState.FAILED,
            }:
                target_state = (
                    RetrohuntReservationState.COMPLETED
                    if job.state is RetrohuntProviderJobState.COMPLETED
                    else RetrohuntReservationState.FAILED
                )
                current = await self._transition_read(current, target_state)
        return await self._outcome(subject_id, current, provider_job_state=provider_job_state)

    async def results(self, *, investigation_id: UUID, reservation_id: UUID) -> RetrohuntOutcome:
        outcome = await self.status(
            investigation_id=investigation_id, reservation_id=reservation_id
        )
        current = outcome.reservation
        if (
            current.provider_job_id is None
            or current.state is not RetrohuntReservationState.COMPLETED
        ):
            return outcome
        provider_results = await self._provider.retrohunt_results(current.provider_job_id)
        if provider_results.job_id != current.provider_job_id:
            raise RetrohuntError(
                "provider_job_mismatch", "The provider returned results for another job."
            )
        references: list[UUID] = []
        for hit in provider_results.hits:
            observation = await self._evidence_recorder.record_json(
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                payload={
                    "schema": "vt-retrohunt-candidate-hit.v1",
                    "sha256": hit.sha256,
                    "qualification": "candidate",
                    "confirmed_member": False,
                    "reservation_id": str(current.id),
                    "investigation_id": str(investigation_id),
                    "rule_source_sha256": current.rule_source_sha256,
                    "corpus_sha256": current.corpus_sha256,
                },
                source="VirusTotal Retrohunt",
                source_version="vt-retrohunt-port.v1",
                object_type="vt_retrohunt_candidate_hit",
                object_id=hit.sha256,
                limitations=(
                    "Retrohunt hits are candidates and are not confirmed subject members.",
                ),
                idempotency_key=(
                    f"retrohunt:{current.id}:investigation:{investigation_id}:hit:{hit.sha256}"
                ),
            )
            references.append(observation.id)
        return RetrohuntOutcome(
            reservation=current,
            slots_consumed=outcome.slots_consumed,
            slots_remaining=outcome.slots_remaining,
            provider_job_state=outcome.provider_job_state,
            provider_error=outcome.provider_error,
            candidate_hits=tuple(hit.sha256 for hit in provider_results.hits),
            observation_refs=tuple(references),
        )

    async def _prevalidated_version(
        self, investigation_id: UUID, version_id: UUID, source_sha256: str
    ) -> Any:
        reviews = await self._review_service.list_for_investigation(investigation_id)
        review = next((item for item in reviews if item.version.id == version_id), None)
        if review is None or review.status is not DetectionCandidateStatus.APPROVED:
            raise RetrohuntQualityError(
                "rule_not_prevalidated", "This exact YARA version has no human prevalidation."
            )
        version = review.version
        if version.source_sha256 != source_sha256:
            raise RetrohuntQualityError(
                "source_sha256_mismatch",
                "The source hash does not identify the reviewed YARA version.",
            )
        decision = review.latest_decision
        if (
            decision is None
            or decision.decision_type is not AnalystDecisionType.DETECTION_APPROVE
            or decision.target_type is not AnalystDecisionTargetType.DETECTION
            or decision.target_id != version.id
            or decision.details.get("source_sha256") != source_sha256
            or not _retrohunt_authorization_matches(decision, version.id, source_sha256)
        ):
            raise RetrohuntQualityError(
                "retrohunt_human_authorization_required",
                "The exact approved version needs an explicit human Retrohunt authorization.",
            )
        report = next(
            (
                item.payload
                for item in reversed(review.test_reports)
                if item.payload.get("source_sha256") == source_sha256
                and item.payload.get("version_id") == str(version.id)
                and item.payload.get("candidate_id") == str(version.candidate_id)
                and item.payload.get("proposal_source_sha256") == source_sha256
            ),
            None,
        )
        if report is None or not _quality_report_qualifies(report, decision.details):
            raise RetrohuntQualityError(
                "rule_test_quality_insufficient",
                "A successful exact-version test with representative positives and measured "
                "controls is required.",
            )
        return version

    async def _subject_id(self, investigation_id: UUID) -> UUID:
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise RetrohuntError("investigation_not_found", "The investigation does not exist.")
            return investigation.subject_id

    async def slot_usage(self, investigation_id: UUID) -> tuple[int, int]:
        """Read subject counters for denied/failed tool responses without reserving."""
        subject_id = await self._subject_id(investigation_id)
        async with self._uow_factory() as uow:
            consumed = await uow.retrohunt_reservations.count_for_subject(subject_id)
        consumed = min(MAX_SUBMISSIONS_PER_SUBJECT, consumed)
        return consumed, max(0, MAX_SUBMISSIONS_PER_SUBJECT - consumed)

    async def _reservation(self, subject_id: UUID, reservation_id: UUID) -> RetrohuntReservation:
        async with self._uow_factory() as uow:
            reservation = await uow.retrohunt_reservations.get_for_subject(
                subject_id, reservation_id
            )
            if reservation is None:
                raise RetrohuntReservationNotFoundError()
            return reservation

    async def _transition_submission(
        self,
        reservation_id: UUID,
        subject_id: UUID,
        token: UUID,
        state: RetrohuntReservationState,
        *,
        provider_job_id: str | None = None,
    ) -> RetrohuntReservation:
        async with self._uow_factory() as uow:
            repository = uow.retrohunt_reservations
            current = await repository.get_for_subject(subject_id, reservation_id)
            if current is None:
                raise RetrohuntReservationNotFoundError()
            if current.submission_token != token:
                return current
            if current.state is state:
                return current
            if current.state not in {
                RetrohuntReservationState.SUBMITTING,
                RetrohuntReservationState.RECONCILIATION_REQUIRED,
            }:
                return current
            updated = current.transition(
                state,
                submission_token=token,
                provider_job_id=provider_job_id,
            )
            saved = await repository.update_if_version(updated, expected_version=current.version)
            if saved is not None:
                await uow.commit()
                return saved
            latest = await repository.get_for_subject(subject_id, reservation_id)
            return latest or current

    async def _transition_read(
        self, current: RetrohuntReservation, state: RetrohuntReservationState
    ) -> RetrohuntReservation:
        async with self._uow_factory() as uow:
            updated = current.transition(state)
            saved = await uow.retrohunt_reservations.update_if_version(
                updated, expected_version=current.version
            )
            if saved is None:
                latest = await uow.retrohunt_reservations.get_for_subject(
                    current.subject_id, current.id
                )
                return latest or current
            await uow.commit()
            return saved

    async def _outcome(
        self,
        subject_id: UUID,
        reservation: RetrohuntReservation,
        *,
        provider_job_state: str,
        provider_error: RetrohuntProviderErrorCode | None = None,
    ) -> RetrohuntOutcome:
        async with self._uow_factory() as uow:
            consumed = await uow.retrohunt_reservations.count_for_subject(subject_id)
        return _outcome(
            reservation,
            consumed,
            provider_job_state=provider_job_state,
            provider_error=provider_error,
        )

    def _require_available(self) -> None:
        if not self._provider.available:
            raise RetrohuntUnavailableError()


def _outcome(
    reservation: RetrohuntReservation,
    consumed: int,
    *,
    provider_job_state: str,
    provider_error: RetrohuntProviderErrorCode | None = None,
) -> RetrohuntOutcome:
    return RetrohuntOutcome(
        reservation=reservation,
        slots_consumed=consumed,
        slots_remaining=max(0, MAX_SUBMISSIONS_PER_SUBJECT - consumed),
        provider_job_state=provider_job_state,
        provider_error=provider_error,
    )


def _operation_key(
    source_sha256: str,
    corpus_id: str,
    corpus_sha256: str,
    parameters: Mapping[str, Any],
) -> str:
    payload = {
        "schema": "vt-retrohunt-operation.v1",
        "rule_source_sha256": source_sha256,
        "corpus_id": corpus_id,
        "corpus_sha256": corpus_sha256,
        "parameters": dict(parameters),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(parameters, Mapping) or len(parameters) > 32:
        raise RetrohuntQualityError(
            "invalid_parameters", "Retrohunt parameters must be a bounded JSON object."
        )
    clean = dict(parameters)
    if any(
        not isinstance(key, str) or not key.strip() or _SECRET_PARAMETER_KEY.search(key)
        for key in clean
    ):
        raise RetrohuntQualityError(
            "invalid_parameters", "Parameters cannot contain credentials or blank keys."
        )
    try:
        encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise RetrohuntQualityError(
            "invalid_parameters", "Parameters must be JSON-compatible."
        ) from None
    if len(encoded.encode("utf-8")) > 4096:
        raise RetrohuntQualityError(
            "invalid_parameters", "Parameters exceed the bounded 4 KiB limit."
        )
    return clean


def _require_sha256(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RetrohuntQualityError("invalid_sha256", f"{field} must be a lowercase SHA-256.")
    return value


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _retrohunt_authorization_matches(decision: Any, version_id: UUID, source_sha256: str) -> bool:
    authorization = getattr(decision, "retrohunt_authorization", None)
    return bool(
        authorization is not None
        and authorization.authorized is True
        and authorization.rule_version_id == version_id
        and authorization.source_sha256 == source_sha256
    )


def _quality_report_qualifies(
    payload: Mapping[str, Any], decision_details: Mapping[str, Any]
) -> bool:
    if (
        payload.get("schema") != "investigation-yara-test.v1"
        or payload.get("execution_status") != "ok"
        or not str(payload.get("engine_version", "")).strip()
        or not str(payload.get("selection_bias", "")).strip()
        or not str(payload.get("coverage_statement", "")).strip()
        or not isinstance(payload.get("roles"), list)
    ):
        return False
    required_roles = {
        RuleTestCorpusRole.HUMAN_CONFIRMED.value,
        RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED.value,
        RuleTestCorpusRole.GOODWARE.value,
        RuleTestCorpusRole.HOLDOUT_POSITIVE.value,
    }
    known_roles = {role.value for role in RuleTestCorpusRole}
    role_aliases = {
        "confirmed_positive": RuleTestCorpusRole.HUMAN_CONFIRMED.value,
        "out_of_cluster_candidate": RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED.value,
        "holdout": RuleTestCorpusRole.HOLDOUT_POSITIVE.value,
    }
    roles: dict[str, Mapping[str, Any]] = {}
    for entry in payload["roles"]:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("role"), str):
            return False
        raw_role = entry["role"]
        role = role_aliases.get(raw_role, raw_role)
        if role not in known_roles or role in roles:
            return False
        roles[role] = entry
    # Current reports serialize every canonical enum role, including empty buckets.
    # Only the four qualified buckets may contain corpus rows for this gate.
    if not required_roles.issubset(roles) or any(
        role not in required_roles and role_payload.get("corpus_sha256s")
        for role, role_payload in roles.items()
    ):
        return False
    if any(not _role_measured(role_payload) for role_payload in roles.values()):
        return False
    measured_roles = {
        role for role, role_payload in roles.items() if role_payload.get("corpus_sha256s")
    }
    if measured_roles != required_roles:
        return False
    positive_role = RuleTestCorpusRole.HUMAN_CONFIRMED.value
    positives = roles.get(positive_role)
    if not isinstance(positives, Mapping):
        return False
    if not _role_measured(positives) or not positives.get("matches"):
        return False
    for role_name in (
        RuleTestCorpusRole.GOODWARE.value,
        RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED.value,
        RuleTestCorpusRole.HOLDOUT_POSITIVE.value,
    ):
        role_payload = roles.get(role_name)
        if not isinstance(role_payload, Mapping) or not role_payload.get("corpus_sha256s"):
            continue
        if not _role_measured(role_payload):
            return False
        if role_name == RuleTestCorpusRole.GOODWARE.value:
            acknowledged = set(decision_details.get("known_false_positives", ()))
            if not set(role_payload.get("matches", ())).issubset(acknowledged):
                return False
    return True


def _role_measured(role: Mapping[str, Any]) -> bool:
    corpus = role.get("corpus_sha256s", [])
    matches = role.get("matches", [])
    non_matches = role.get("non_matches", [])
    non_measurable = role.get("non_measurable", [])
    errors = role.get("errors", [])
    return (
        isinstance(corpus, list)
        and isinstance(matches, list)
        and isinstance(non_matches, list)
        and isinstance(non_measurable, list)
        and isinstance(errors, list)
        and not non_measurable
        and not errors
        and set(matches).isdisjoint(non_matches)
        and set(matches) | set(non_matches) == set(corpus)
    )


__all__ = [
    "MAX_SUBMISSIONS_PER_SUBJECT",
    "RETROHUNT_CAPABILITY",
    "RetrohuntError",
    "RetrohuntOutcome",
    "RetrohuntQualityError",
    "RetrohuntService",
    "RetrohuntSlotLimitError",
    "RetrohuntSubmissionRejected",
    "RetrohuntUnavailableError",
]
