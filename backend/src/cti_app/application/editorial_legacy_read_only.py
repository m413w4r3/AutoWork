"""Read-only policy for publication identity and pinned editorial inputs."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from cti_app.application.edition_classification import explicit_publication_kind
from cti_app.domain.production_editorial_enrichment import EDITORIAL_ENRICHMENT_SCHEMA_VERSION
from cti_app.domain.production_routing import ProductionRoutingDecisionV1
from cti_app.domain.production_synthesis import PRODUCTION_SYNTHESIS_SCHEMA_VERSION
from cti_app.domain.publication_document import parse_publication_document
from cti_app.domain.publication_kind import PublicationKind


class EditorialLegacyReadOnlyReason(StrEnum):
    FORMAT_NOT_EDITABLE = "format_not_editable"
    PUBLICATION_KIND_AMBIGUOUS = "publication_kind_ambiguous"
    PUBLICATION_KIND_UNKNOWN = "publication_kind_unknown"
    HISTORICAL_VERSION = "historical_version"


def publication_kind_is_ambiguous(
    routing_decision: ProductionRoutingDecisionV1 | None,
    publication_content: Mapping[str, Any] | None,
) -> bool:
    if routing_decision is None or publication_content is None:
        return False
    try:
        document = parse_publication_document(publication_content)
    except (TypeError, ValueError):
        return False
    envelope_kind = explicit_publication_kind(document)
    envelope_hash = getattr(document, "routing_decision_hash", None)
    return (
        envelope_kind is not None and routing_decision.publication_kind is not envelope_kind
    ) or (isinstance(envelope_hash, str) and envelope_hash != routing_decision.decision_hash)


def publication_read_only_reason(
    publication_content: Mapping[str, Any] | None,
    publication_kind: PublicationKind | None,
    *,
    ambiguous_kind: bool = False,
    historical: bool = False,
) -> EditorialLegacyReadOnlyReason | None:
    if ambiguous_kind:
        return EditorialLegacyReadOnlyReason.PUBLICATION_KIND_AMBIGUOUS
    if publication_kind is None:
        return EditorialLegacyReadOnlyReason.PUBLICATION_KIND_UNKNOWN
    if historical:
        return EditorialLegacyReadOnlyReason.HISTORICAL_VERSION
    return None


def pinned_editorial_inputs_read_only_reason(
    synthesis_content: Mapping[str, Any],
    enrichment_content: Mapping[str, Any] | None,
) -> EditorialLegacyReadOnlyReason | None:
    """Fail closed when a pinned source artifact uses an unsupported editor format."""
    synthesis_version = synthesis_content.get("schema_version")
    synthesis_supported = synthesis_version == str(PRODUCTION_SYNTHESIS_SCHEMA_VERSION) or (
        type(synthesis_version) is int and synthesis_version == PRODUCTION_SYNTHESIS_SCHEMA_VERSION
    )
    if not synthesis_supported:
        return EditorialLegacyReadOnlyReason.FORMAT_NOT_EDITABLE

    if enrichment_content is not None:
        enrichment_version = enrichment_content.get("schema_version")
        if (
            type(enrichment_version) is not int
            or enrichment_version < 1
            or enrichment_version > EDITORIAL_ENRICHMENT_SCHEMA_VERSION
        ):
            return EditorialLegacyReadOnlyReason.FORMAT_NOT_EDITABLE
    return None


def current_editorial_read_only_reason(
    *,
    routing_decision: ProductionRoutingDecisionV1 | None,
    publication_content: Mapping[str, Any] | None,
    publication_kind: PublicationKind | None,
    synthesis_content: Mapping[str, Any],
    enrichment_content: Mapping[str, Any] | None,
) -> EditorialLegacyReadOnlyReason | None:
    """Single policy computation shared by the snapshot projection and every write guard."""
    reason = publication_read_only_reason(
        publication_content,
        publication_kind,
        ambiguous_kind=publication_kind_is_ambiguous(routing_decision, publication_content),
    )
    if reason is None:
        reason = pinned_editorial_inputs_read_only_reason(synthesis_content, enrichment_content)
    return reason


__all__ = [
    "EditorialLegacyReadOnlyReason",
    "current_editorial_read_only_reason",
    "pinned_editorial_inputs_read_only_reason",
    "publication_kind_is_ambiguous",
    "publication_read_only_reason",
]
