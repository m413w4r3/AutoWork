"""Shared pure rules for deciding whether an extracted IOC belongs to a subject."""

from __future__ import annotations

import re

from cti_app.domain.legitimate_services import LegitimateServiceRegistry
from cti_app.domain.production_extraction import ExtractionIndicatorStatus
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceClassificationItemV1,
    RelevanceReasonCode,
)
from cti_app.domain.publication import PUBLICATION_IOC_ARTIFACT_TYPES, ArtifactType

_MAIN_IOC_REASONS = frozenset(
    {
        RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
        RelevanceReasonCode.MALICIOUS_SUBJECT_CORROBORATION,
    }
)
_HASH_VALUE = re.compile(r"[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}")


def is_subject_ioc_eligible(
    *,
    indicator_status: ExtractionIndicatorStatus,
    artifact_type: ArtifactType,
    value: str,
    relevance: RelevanceClassificationItemV1 | None,
    legitimate_services: LegitimateServiceRegistry | None = None,
) -> bool:
    """Return whether one confirmed indicator is an eligible subject IOC.

    ``relevance=None`` is accepted only for the publication builder's legacy
    projection path. Routing always supplies a complete, validated projection.
    Value normalization and legitimate-service exclusions are shared so the
    route count matches the builder's canonical IOC inventory.
    """
    if (
        indicator_status is not ExtractionIndicatorStatus.CONFIRMED_IOC
        or artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
    ):
        return False
    if relevance is not None and not (
        relevance.classification
        in {
            RelevanceClassification.DIRECT,
            RelevanceClassification.CORROBORATION,
        }
        and relevance.reason_code in _MAIN_IOC_REASONS
    ):
        return False

    # Without a registry this helper is used by the builder before it builds
    # its detailed diagnostics. Preserve that ordering: normalization failures
    # and unsupported hashes remain diagnosed by the builder's existing path.
    if legitimate_services is None:
        return True

    from cti_app.domain.indicator_normalization import normalize_indicator_value

    try:
        normalized = normalize_indicator_value(value, artifact_type)
    except (TypeError, ValueError):
        return False
    if not is_supported_subject_ioc_hash(artifact_type, normalized):
        return False
    return legitimate_services.match_indicator(artifact_type, normalized) is None


def is_supported_subject_ioc_hash(artifact_type: ArtifactType, normalized_value: str) -> bool:
    """Keep the publication hash formats in the shared IOC eligibility policy."""
    return (
        artifact_type is not ArtifactType.HASH
        or _HASH_VALUE.fullmatch(normalized_value) is not None
    )
