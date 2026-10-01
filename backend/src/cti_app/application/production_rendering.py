"""Deterministic projections of extracted production indicators."""

from __future__ import annotations

from cti_app.application.production_normalization import canonical_indicator_key
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    TechnicalExtraction,
)
from cti_app.domain.publication import ArtifactType, is_publication_ioc_artifact_type


def collect_indicators(extraction: TechnicalExtraction) -> list[ExtractionItem]:
    """Explicitly qualified IOC, deduplicated by type and canonical value."""
    seen: set[tuple[str, str]] = set()
    out: list[ExtractionItem] = []
    for item in extraction.items:
        if (
            not item.supported
            or item.indicator_status is not IndicatorStatus.CONFIRMED_IOC
            or item.display_policy not in {DisplayPolicy.IOC_SECTION, DisplayPolicy.BOTH}
            or item.artifact_type is None
        ):
            continue
        artifact_type = (
            item.artifact_type
            if isinstance(item.artifact_type, ArtifactType)
            else ArtifactType(item.artifact_type)
        )
        if artifact_type in {
            ArtifactType.YARA_RULE,
            ArtifactType.SIGMA_RULE,
            ArtifactType.SURICATA_RULE,
        }:
            continue
        if not is_publication_ioc_artifact_type(artifact_type):
            continue
        try:
            key = (artifact_type.value, canonical_indicator_key(item.value, artifact_type))
        except ValueError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
