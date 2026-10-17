"""Human review of immutable investigation YARA versions."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy.exc import IntegrityError

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
    RetrohuntAuthorization,
)
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
)
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_document import parse_publication_document

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DETECTION_REVIEW_NAMESPACE = UUID("3bdc0363-2ff8-4c0f-a842-98f9359f8e2c")
DETECTION_REVIEW_DETAILS_SCHEMA = "detection-review.v1"


class DetectionReviewErrorCode(StrEnum):
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    VERSION_NOT_FOUND = "detection_version_not_found"
    SOURCE_SHA256_MISMATCH = "detection_source_sha256_mismatch"
    TEST_REPORT_NOT_FOUND = "detection_test_report_not_found"
    ARTICLE_BASE_UNAVAILABLE = "detection_article_base_unavailable"
    VERSION_REJECTED_BY_TEST = "detection_version_rejected_by_test"
    INVALID_ACTOR = "detection_review_actor_required"
    INVALID_REASON = "detection_review_reason_required"
    LIMITATIONS_REQUIRED = "detection_known_limitations_required"
    FALSE_POSITIVE_NOT_ACKNOWLEDGED = "detection_false_positive_risk_not_acknowledged"
    INVALID_CORRELATION_ID = "detection_review_idempotency_key_required"
    IDEMPOTENCY_CONFLICT = "detection_review_idempotency_conflict"
    INVALID_RETROHUNT_AUTHORIZATION = "detection_retrohunt_authorization_invalid"


class DetectionReviewError(ValueError):
    def __init__(self, code: DetectionReviewErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class DetectionTestReport:
    observation: EvidenceObservation
    payload: Mapping[str, Any]

    @property
    def known_false_positives(self) -> tuple[str, ...]:
        found: set[str] = set()
        roles = self.payload.get("roles", [])
        if not isinstance(roles, list):
            return ()
        for role in roles:
            if not isinstance(role, Mapping) or role.get("role") != "goodware":
                continue
            matches = role.get("matches", [])
            if isinstance(matches, list):
                found.update(item for item in matches if isinstance(item, str))
        return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class DetectionReviewVersion:
    version: DetectionCandidateVersion
    status: DetectionCandidateStatus
    test_reports: tuple[DetectionTestReport, ...]
    latest_decision: AnalystDecision | None
    comparisons: tuple[Mapping[str, Any], ...]


class DetectionReviewService:
    """Append an identified human decision against one immutable YARA version."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def approve(
        self,
        candidate_version_id: UUID,
        expected_source_sha256: str,
        actor: str,
        reason: str,
        known_limitations: str,
        correlation_id: str,
        retrohunt_authorized: bool = False,
    ) -> AnalystDecision:
        return await self._decide(
            candidate_version_id,
            expected_source_sha256,
            actor,
            reason,
            known_limitations,
            correlation_id,
            decision_type=AnalystDecisionType.DETECTION_APPROVE,
            retrohunt_authorized=retrohunt_authorized,
        )

    async def reject(
        self,
        candidate_version_id: UUID,
        expected_source_sha256: str,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> AnalystDecision:
        return await self._decide(
            candidate_version_id,
            expected_source_sha256,
            actor,
            reason,
            "",
            correlation_id,
            decision_type=AnalystDecisionType.DETECTION_REJECT,
        )

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[DetectionReviewVersion, ...]:
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise DetectionReviewError(DetectionReviewErrorCode.INVESTIGATION_NOT_FOUND)
            observations = tuple(
                await cast(Any, uow).evidence_observations.list_for_investigation(investigation_id)
            )
            decisions = tuple(await uow.analyst_decisions.list_for_investigation(investigation_id))
            candidate_repository = cast(Any, uow).detection_candidates
            persisted_versions = tuple(
                await candidate_repository.list_for_investigation(investigation_id)
            )
            version_by_id = {str(version.id): version for version in persisted_versions}
            version_ids = {
                observation.object_id
                for observation in observations
                if observation.kind is EvidenceKind.YARA_MATCH
                and observation.object_type == "detection_candidate_version"
                and _is_uuid(observation.object_id)
            }
            version_ids.update(
                str(decision.target_id)
                for decision in decisions
                if decision.target_type is AnalystDecisionTargetType.DETECTION
                and decision.decision_type
                in {
                    AnalystDecisionType.DETECTION_APPROVE,
                    AnalystDecisionType.DETECTION_REJECT,
                }
            )
            version_ids.update(version_by_id)

            comparisons = await self._read_comparisons(observations)
            result: list[DetectionReviewVersion] = []
            for raw_id in sorted(version_ids):
                version = version_by_id.get(raw_id) or await candidate_repository.get(UUID(raw_id))
                if version is None or version.investigation_id != investigation_id:
                    continue
                reports = await self._read_test_reports(version, observations)
                related = tuple(
                    sorted(
                        (
                            decision
                            for decision in decisions
                            if decision.target_type is AnalystDecisionTargetType.DETECTION
                            and decision.target_id == version.id
                            and decision.decision_type
                            in {
                                AnalystDecisionType.DETECTION_APPROVE,
                                AnalystDecisionType.DETECTION_REJECT,
                            }
                        ),
                        key=lambda item: (item.occurred_at, str(item.id)),
                    )
                )
                latest = related[-1] if related else None
                if latest is not None:
                    status = (
                        DetectionCandidateStatus.APPROVED
                        if latest.decision_type is AnalystDecisionType.DETECTION_APPROVE
                        else DetectionCandidateStatus.REJECTED
                    )
                else:
                    status = version.status
                result.append(
                    DetectionReviewVersion(
                        version=version,
                        status=status,
                        test_reports=reports,
                        latest_decision=latest,
                        comparisons=tuple(
                            item
                            for item in comparisons
                            if item.get("candidate_id") == str(version.candidate_id)
                            or str(version.candidate_id) in str(item.get("object_id", ""))
                        ),
                    )
                )
            return tuple(
                sorted(
                    result,
                    key=lambda item: (
                        str(item.version.candidate_id),
                        item.version.version,
                        str(item.version.id),
                    ),
                )
            )

    async def _decide(
        self,
        candidate_version_id: UUID,
        expected_source_sha256: str,
        actor: str,
        reason: str,
        known_limitations: str,
        correlation_id: str,
        *,
        decision_type: AnalystDecisionType,
        retrohunt_authorized: bool | None = None,
    ) -> AnalystDecision:
        actor = actor.strip()
        reason = reason.strip()
        known_limitations = known_limitations.strip()
        correlation_id = correlation_id.strip()
        if not actor:
            raise DetectionReviewError(DetectionReviewErrorCode.INVALID_ACTOR)
        if not reason:
            raise DetectionReviewError(DetectionReviewErrorCode.INVALID_REASON)
        if not correlation_id or len(correlation_id) > 128:
            raise DetectionReviewError(DetectionReviewErrorCode.INVALID_CORRELATION_ID)
        if decision_type is AnalystDecisionType.DETECTION_APPROVE and not known_limitations:
            raise DetectionReviewError(DetectionReviewErrorCode.LIMITATIONS_REQUIRED)
        if not _SHA256.fullmatch(expected_source_sha256):
            raise DetectionReviewError(DetectionReviewErrorCode.SOURCE_SHA256_MISMATCH)
        if (
            decision_type is AnalystDecisionType.DETECTION_APPROVE
            and type(retrohunt_authorized) is not bool
        ):
            raise DetectionReviewError(DetectionReviewErrorCode.INVALID_RETROHUNT_AUTHORIZATION)

        retrohunt_authorization = (
            RetrohuntAuthorization(
                authorized=cast(bool, retrohunt_authorized),
                rule_version_id=candidate_version_id,
                source_sha256=expected_source_sha256,
            )
            if decision_type is AnalystDecisionType.DETECTION_APPROVE
            else None
        )

        async with self._uow_factory() as uow:
            version = await cast(Any, uow).detection_candidates.get(candidate_version_id)
            if version is None:
                raise DetectionReviewError(DetectionReviewErrorCode.VERSION_NOT_FOUND)
            if (
                version.source_sha256 != expected_source_sha256
                or hashlib.sha256(version.source.encode("utf-8")).hexdigest()
                != expected_source_sha256
            ):
                raise DetectionReviewError(DetectionReviewErrorCode.SOURCE_SHA256_MISMATCH)

            existing_decisions = tuple(
                await uow.analyst_decisions.list_for_investigation(version.investigation_id)
            )
            prior = next(
                (
                    item
                    for item in existing_decisions
                    if item.target_type is AnalystDecisionTargetType.DETECTION
                    and item.target_id == version.id
                    and item.correlation_id == correlation_id
                ),
                None,
            )
            if prior is not None:
                if _same_idempotent_decision(
                    prior,
                    decision_type=decision_type,
                    actor=actor,
                    reason=reason,
                    source_sha256=expected_source_sha256,
                    known_limitations=known_limitations,
                    retrohunt_authorization=retrohunt_authorization,
                ):
                    return prior
                raise DetectionReviewError(DetectionReviewErrorCode.IDEMPOTENCY_CONFLICT)

            investigation = await uow.analyst_investigations.get(version.investigation_id)
            if investigation is None:
                raise DetectionReviewError(DetectionReviewErrorCode.INVESTIGATION_NOT_FOUND)
            if (
                version.status is DetectionCandidateStatus.REJECTED
                and decision_type is AnalystDecisionType.DETECTION_APPROVE
            ):
                raise DetectionReviewError(DetectionReviewErrorCode.VERSION_REJECTED_BY_TEST)

            observations = tuple(
                await cast(Any, uow).evidence_observations.list_for_investigation(
                    version.investigation_id
                )
            )
            reports = await self._read_test_reports(version, observations)
            if not reports:
                raise DetectionReviewError(DetectionReviewErrorCode.TEST_REPORT_NOT_FOUND)
            report = reports[-1]
            false_positives = report.known_false_positives
            if (
                decision_type is AnalystDecisionType.DETECTION_APPROVE
                and false_positives
                and not _limitations_acknowledge_false_positives(known_limitations, false_positives)
            ):
                raise DetectionReviewError(DetectionReviewErrorCode.FALSE_POSITIVE_NOT_ACKNOWLEDGED)
            base, base_sha256 = await self._resolve_article_base(uow, investigation)

            decision_id = uuid5(
                _DETECTION_REVIEW_NAMESPACE,
                f"{version.investigation_id}:{version.id}:{correlation_id}",
            )
            decision = AnalystDecision(
                id=decision_id,
                investigation_id=version.investigation_id,
                decision_type=decision_type,
                target_type=AnalystDecisionTargetType.DETECTION,
                target_id=version.id,
                actor_id=actor,
                reason=reason,
                correlation_id=correlation_id,
                occurred_at=datetime.now(UTC),
                retrohunt_authorization=retrohunt_authorization,
                details={
                    "schema": DETECTION_REVIEW_DETAILS_SCHEMA,
                    "source_sha256": expected_source_sha256,
                    "known_limitations": known_limitations,
                    "known_false_positives": list(false_positives),
                    "test_report": {
                        "observation_id": str(report.observation.id),
                        "sha256": report.observation.sha256,
                        "corpus_sha256": report.payload["corpus_sha256"],
                        "engine_version": report.payload.get("engine_version"),
                    },
                    "base_publication": {
                        "artifact_id": str(base.id),
                        "sha256": base_sha256,
                    },
                },
            )
            await uow.analyst_decisions.append(decision)
            try:
                await uow.commit()
            except IntegrityError:
                # The deterministic primary key makes concurrent requests with
                # the same correlation ID converge on one audit row. Re-read
                # after rollback to turn the losing insert into an idempotent
                # response, while preserving unrelated integrity failures.
                await uow.rollback()
                raced = next(
                    (
                        item
                        for item in await uow.analyst_decisions.list_for_investigation(
                            version.investigation_id
                        )
                        if item.target_type is AnalystDecisionTargetType.DETECTION
                        and item.target_id == version.id
                        and item.correlation_id == correlation_id
                    ),
                    None,
                )
                if raced is None:
                    raise
                if not _same_idempotent_decision(
                    raced,
                    decision_type=decision_type,
                    actor=actor,
                    reason=reason,
                    source_sha256=expected_source_sha256,
                    known_limitations=known_limitations,
                    retrohunt_authorization=retrohunt_authorization,
                ):
                    raise DetectionReviewError(
                        DetectionReviewErrorCode.IDEMPOTENCY_CONFLICT
                    ) from None
                return raced
            return decision

    async def _read_test_reports(
        self,
        version: DetectionCandidateVersion,
        observations: tuple[EvidenceObservation, ...],
    ) -> tuple[DetectionTestReport, ...]:
        matching = sorted(
            (
                item
                for item in observations
                if item.investigation_id == version.investigation_id
                and item.kind is EvidenceKind.YARA_MATCH
                and item.object_type == "detection_candidate_version"
                and item.object_id == str(version.id)
            ),
            key=lambda item: (item.observed_at, str(item.id)),
        )
        reports: list[DetectionTestReport] = []
        for observation in matching:
            try:
                payload = await self._artifact_store.read_json(observation.blob_id)
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            try:
                digest = hashlib.sha256(
                    ProductionArtifactStore.canonical_json_bytes(payload)
                ).hexdigest()
            except (TypeError, ValueError):
                continue
            if digest != observation.sha256 or not _valid_test_report(payload, version):
                continue
            reports.append(DetectionTestReport(observation=observation, payload=dict(payload)))
        return tuple(reports)

    async def _read_comparisons(
        self, observations: tuple[EvidenceObservation, ...]
    ) -> tuple[Mapping[str, Any], ...]:
        comparisons: list[Mapping[str, Any]] = []
        matching = sorted(
            (
                item
                for item in observations
                if item.kind is EvidenceKind.YARA_MATCH
                and item.object_type == "detection_candidate_comparison"
            ),
            key=lambda item: (item.observed_at, str(item.id)),
        )
        for observation in matching:
            try:
                payload = await self._artifact_store.read_json(observation.blob_id)
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(payload, Mapping):
                continue
            digest = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(payload)
            ).hexdigest()
            if digest == observation.sha256:
                comparisons.append({**dict(payload), "object_id": observation.object_id})
        return tuple(comparisons)

    async def _resolve_article_base(self, uow: Any, investigation: Any) -> tuple[Any, str]:
        base = await uow.production_artifacts.get_current_for_revision(
            investigation.production_run_id,
            ProductionArtifactStage.PUBLICATION.value,
        )
        synthesis = await uow.production_artifacts.get(investigation.synthesis_artifact_id)
        if (
            base is None
            or base.stage is not ProductionArtifactStage.PUBLICATION
            or base.status is not ProductionArtifactStatus.VERIFIED
            or base.production_run_id != investigation.production_run_id
            or base.subject_id != investigation.subject_id
            or base.canonical_blob_id is None
            or synthesis is None
            or synthesis.stage is not ProductionArtifactStage.SYNTHESIS
            or synthesis.status is not ProductionArtifactStatus.VERIFIED
            or synthesis.production_run_id != investigation.production_run_id
            or synthesis.subject_id != investigation.subject_id
            or synthesis.canonical_blob_id is None
        ):
            raise DetectionReviewError(DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE)
        try:
            payload = await self._artifact_store.read_json(base.canonical_blob_id)
            document = parse_publication_document(payload)
        except (KeyError, TypeError, ValueError):
            raise DetectionReviewError(DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE) from None
        if document.schema_version not in {"4", "5", "6"}:
            raise DetectionReviewError(DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE)
        digest = hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(payload)).hexdigest()
        if document.schema_version == "6":
            provenance = payload.get("provenance")
            augmentation = base.metadata.get("publication_augmentation")
            previous_id = base.metadata.get("previous_publication_artifact_id")
            if (
                not isinstance(provenance, Mapping)
                or not isinstance(augmentation, Mapping)
                or str(augmentation.get("investigation_id")) != str(investigation.id)
                or previous_id is None
                or str(provenance.get("base_publication_artifact_id")) != str(previous_id)
            ):
                raise DetectionReviewError(DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE)
            try:
                parent_id = UUID(str(previous_id))
            except ValueError:
                raise DetectionReviewError(
                    DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE
                ) from None
            parent = await uow.production_artifacts.get(parent_id)
            if (
                parent is None
                or parent.stage is not ProductionArtifactStage.PUBLICATION
                or parent.status is not ProductionArtifactStatus.VERIFIED
                or parent.production_run_id != investigation.production_run_id
                or parent.subject_id != investigation.subject_id
                or parent.canonical_blob_id is None
            ):
                raise DetectionReviewError(DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE)
            try:
                parent_payload = await self._artifact_store.read_json(parent.canonical_blob_id)
                parent_sha256 = hashlib.sha256(
                    ProductionArtifactStore.canonical_json_bytes(parent_payload)
                ).hexdigest()
            except (KeyError, TypeError, ValueError):
                raise DetectionReviewError(
                    DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE
                ) from None
            if provenance.get("base_publication_sha256") != parent_sha256:
                raise DetectionReviewError(DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE)
        return base, digest


def _valid_test_report(payload: Mapping[str, Any], version: DetectionCandidateVersion) -> bool:
    if (
        payload.get("schema") != "investigation-yara-test.v1"
        or payload.get("candidate_id") != str(version.candidate_id)
        or payload.get("version_id") != str(version.id)
        or payload.get("source_sha256") != version.source_sha256
        or payload.get("proposal_source_sha256") != version.source_sha256
        or not isinstance(payload.get("corpus_sha256"), str)
        or not _SHA256.fullmatch(str(payload.get("corpus_sha256")))
        or not isinstance(payload.get("corpus_sha256s_by_role"), Mapping)
        or not isinstance(payload.get("roles"), list)
    ):
        return False
    for hashes in payload["corpus_sha256s_by_role"].values():
        if not isinstance(hashes, list) or any(
            not isinstance(value, str) or not _SHA256.fullmatch(value) for value in hashes
        ):
            return False
    return True


def _limitations_acknowledge_false_positives(
    known_limitations: str, false_positives: tuple[str, ...]
) -> bool:
    normalized = known_limitations.casefold()
    if any(value.casefold() in normalized for value in false_positives):
        return True
    return (
        "faux positif" in normalized or "goodware" in normalized or "false positive" in normalized
    )


def _same_idempotent_decision(
    decision: AnalystDecision,
    *,
    decision_type: AnalystDecisionType,
    actor: str,
    reason: str,
    source_sha256: str,
    known_limitations: str,
    retrohunt_authorization: RetrohuntAuthorization | None,
) -> bool:
    return (
        decision.decision_type is decision_type
        and decision.actor_id == actor
        and decision.reason == reason
        and decision.details.get("schema") == DETECTION_REVIEW_DETAILS_SCHEMA
        and decision.details.get("source_sha256") == source_sha256
        and decision.details.get("known_limitations") == known_limitations
        and decision.retrohunt_authorization == retrohunt_authorization
    )


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except (ValueError, TypeError):
        return False
    return True


__all__ = [
    "DETECTION_REVIEW_DETAILS_SCHEMA",
    "DetectionReviewError",
    "DetectionReviewErrorCode",
    "DetectionReviewService",
    "DetectionReviewVersion",
    "DetectionTestReport",
]
