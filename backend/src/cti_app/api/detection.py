"""Human review API for investigation YARA rules."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.detection_review_service import (
    DetectionReviewError,
    DetectionReviewErrorCode,
    DetectionReviewService,
    DetectionReviewVersion,
    DetectionTestReport,
)
from cti_app.application.identity import IdentityProvider
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial import AnalystDecision
from cti_app.domain.rule_workbench import RuleValidationStatus

router = APIRouter(prefix="/api", tags=["detection-review"])


class DetectionReviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=4000)


class DetectionApprovalPayload(DetectionReviewPayload):
    known_limitations: str = Field(min_length=1, max_length=8000)


def _review_service(request: Request) -> DetectionReviewService:
    configured = getattr(request.app.state, "detection_review_service", None)
    if configured is not None:
        return cast(DetectionReviewService, configured)
    return DetectionReviewService(
        uow_factory=request.app.state.uow_factory,
        artifact_store=cast(ProductionArtifactStore, request.app.state.production_artifact_store),
    )


async def _actor_id(request: Request) -> str:
    provider: IdentityProvider = request.app.state.identity_provider
    return (await provider.current()).actor_id


def _require_idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "idempotency_key_required"},
        )
    if len(value) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_idempotency_key"},
        )
    return value.strip()


def _raise_detection_error(exc: DetectionReviewError) -> None:
    status_code = {
        DetectionReviewErrorCode.INVESTIGATION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        DetectionReviewErrorCode.VERSION_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        DetectionReviewErrorCode.SOURCE_SHA256_MISMATCH: status.HTTP_409_CONFLICT,
        DetectionReviewErrorCode.TEST_REPORT_NOT_FOUND: status.HTTP_409_CONFLICT,
        DetectionReviewErrorCode.ARTICLE_BASE_UNAVAILABLE: status.HTTP_409_CONFLICT,
        DetectionReviewErrorCode.VERSION_REJECTED_BY_TEST: status.HTTP_409_CONFLICT,
        DetectionReviewErrorCode.IDEMPOTENCY_CONFLICT: status.HTTP_409_CONFLICT,
        DetectionReviewErrorCode.FALSE_POSITIVE_NOT_ACKNOWLEDGED: (
            status.HTTP_422_UNPROCESSABLE_CONTENT
        ),
        DetectionReviewErrorCode.INVALID_ACTOR: status.HTTP_422_UNPROCESSABLE_CONTENT,
        DetectionReviewErrorCode.INVALID_REASON: status.HTTP_422_UNPROCESSABLE_CONTENT,
        DetectionReviewErrorCode.LIMITATIONS_REQUIRED: status.HTTP_422_UNPROCESSABLE_CONTENT,
        DetectionReviewErrorCode.INVALID_CORRELATION_ID: status.HTTP_422_UNPROCESSABLE_CONTENT,
    }[exc.code]
    raise HTTPException(status_code=status_code, detail={"code": exc.code.value}) from exc


def _report_view(report: DetectionTestReport) -> dict[str, Any]:
    goodware_cases = [
        item
        for item in report.payload.get("roles", [])
        if isinstance(item, dict) and item.get("role") == "goodware"
    ]
    goodware_case_count = sum(len(item.get("corpus_sha256s", [])) for item in goodware_cases)
    return {
        "observation_id": str(report.observation.id),
        "sha256": report.observation.sha256,
        "source_sha256": report.payload.get("source_sha256"),
        "corpus_sha256": report.payload.get("corpus_sha256"),
        "corpus_sha256s_by_role": report.payload.get("corpus_sha256s_by_role", {}),
        "engine_version": report.payload.get("engine_version"),
        "execution_status": report.payload.get("execution_status"),
        "validation_status": report.payload.get("validation_status", "untested"),
        "selection_bias": report.payload.get("selection_bias"),
        "coverage_statement": report.payload.get("coverage_statement"),
        "provenance_counts_by_role": report.payload.get("provenance_counts_by_role", {}),
        "false_positive_count": report.payload.get(
            "false_positive_count", len(report.known_false_positives)
        ),
        "false_positive_sha256s": report.payload.get(
            "false_positive_sha256s", list(report.known_false_positives)
        ),
        "false_negative_count": report.payload.get("false_negative_count", 0),
        "false_negative_sha256s": report.payload.get("false_negative_sha256s", []),
        "roles": report.payload.get("roles", []),
        "limitations": report.payload.get("limitations", []),
        "known_false_positives": list(report.known_false_positives),
        "known_false_positive_status": (
            "present"
            if report.known_false_positives
            else "none_observed"
            if goodware_case_count
            else "not_tested"
        ),
        "goodware_case_count": goodware_case_count,
    }


def _decision_view(decision: AnalystDecision | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    details = decision.details
    return {
        "id": str(decision.id),
        "type": decision.decision_type.value,
        "actor": decision.actor_id,
        "reason": decision.reason,
        "occurred_at": decision.occurred_at.isoformat(),
        "correlation_id": decision.correlation_id,
        "known_limitations": details.get("known_limitations", ""),
        "source_sha256": details.get("source_sha256"),
        "test_report": details.get("test_report"),
        "base_publication": details.get("base_publication"),
    }


def _version_view(item: DetectionReviewVersion) -> dict[str, Any]:
    version = item.version
    matching_reports = tuple(
        report
        for report in item.test_reports
        if report.payload.get("source_sha256") == version.source_sha256
    )
    try:
        validation_status = RuleValidationStatus(
            matching_reports[-1].payload.get("validation_status", "untested")
            if matching_reports
            else "untested"
        ).value
    except (TypeError, ValueError):
        validation_status = RuleValidationStatus.UNTESTED.value
    return {
        "candidate_id": str(version.candidate_id),
        "version_id": str(version.id),
        "version": version.version,
        "parent_version_id": str(version.parent_version_id)
        if version.parent_version_id is not None
        else None,
        "investigation_id": str(version.investigation_id),
        "source": version.source,
        "source_sha256": version.source_sha256,
        "status": item.status.value,
        "validation_status": validation_status,
        "created_at": version.created_at.isoformat(),
        "test_reports": [_report_view(report) for report in item.test_reports],
        "latest_decision": _decision_view(item.latest_decision),
        "comparisons": [dict(comparison) for comparison in item.comparisons],
    }


def _decision_result(decision: AnalystDecision, *, status_value: str) -> dict[str, Any]:
    return {"status": status_value, "decision": _decision_view(decision)}


@router.get("/investigations/{investigation_id}/detections")
async def list_investigation_detections(investigation_id: UUID, request: Request) -> dict[str, Any]:
    try:
        versions = await _review_service(request).list_for_investigation(investigation_id)
    except DetectionReviewError as exc:
        _raise_detection_error(exc)
    return {
        "investigation_id": str(investigation_id),
        "versions": [_version_view(item) for item in versions],
    }


@router.post("/detections/{version_id}/approve")
async def approve_detection(
    version_id: UUID,
    request: Request,
    payload: DetectionApprovalPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    try:
        decision = await _review_service(request).approve(
            version_id,
            payload.expected_source_sha256,
            await _actor_id(request),
            payload.reason,
            payload.known_limitations,
            correlation_id=key,
        )
    except DetectionReviewError as exc:
        _raise_detection_error(exc)
    return _decision_result(decision, status_value="approved")


@router.post("/detections/{version_id}/reject")
async def reject_detection(
    version_id: UUID,
    request: Request,
    payload: DetectionReviewPayload,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    key = _require_idempotency_key(idempotency_key)
    try:
        decision = await _review_service(request).reject(
            version_id,
            payload.expected_source_sha256,
            await _actor_id(request),
            payload.reason,
            correlation_id=key,
        )
    except DetectionReviewError as exc:
        _raise_detection_error(exc)
    return _decision_result(decision, status_value="rejected")


__all__ = ["router"]
