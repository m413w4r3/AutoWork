from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException

from cti_app.application.discovery.cumulative.errors import DiscoverySnapshotStaleError
from cti_app.application.discovery.manual_source_edits import (
    IncompleteSourceCandidateNotFoundError,
)
from cti_app.application.discovery.service import SourceCandidateNotFoundError
from cti_app.application.discovery_report_parser import ReportParsingError
from cti_app.application.editions import EditionNotFoundError
from cti_app.application.model_gateway import ModelGatewayError


def _raise_api_error(exc: Exception) -> NoReturn:
    if isinstance(exc, EditionNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "edition_not_found"}) from exc
    if isinstance(exc, SourceCandidateNotFoundError):
        raise HTTPException(status_code=404, detail={"code": "source_candidate_not_found"}) from exc
    if isinstance(exc, IncompleteSourceCandidateNotFoundError):
        raise HTTPException(
            status_code=404, detail={"code": "incomplete_source_candidate_not_found"}
        ) from exc
    if isinstance(exc, ReportParsingError):
        status_code = 404 if exc.code == "report_unavailable" else 422
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, ModelGatewayError):
        raise HTTPException(
            status_code=409,
            detail={"code": "recovery_unavailable", "message": str(exc)},
        ) from exc
    # Checked before ValueError so that a subclass added later cannot silently
    # be reported as a malformed request.
    if isinstance(exc, DiscoverySnapshotStaleError):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "discovery_snapshot_stale",
                "message": (
                    "Une contribution plus récente a modifié l'édition. "
                    "Rechargez pour voir l'état actuel."
                ),
            },
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_discovery", "message": str(exc)},
        ) from exc
    raise exc
