"""Read-only policy for historical publication documents."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from cti_app.application.edition_classification import explicit_publication_kind
from cti_app.domain.production_routing import ProductionRoutingDecisionV1
from cti_app.domain.publication_document import parse_publication_document
from cti_app.domain.publication_kind import PublicationKind


class EditorialLegacyReadOnlyReason(StrEnum):
    FORMAT_NOT_EDITABLE = "format_not_editable"
    PUBLICATION_KIND_AMBIGUOUS = "publication_kind_ambiguous"
    PUBLICATION_KIND_UNKNOWN = "publication_kind_unknown"
    HISTORICAL_VERSION = "historical_version"


_LEGACY_READ_ONLY_FORMATS = frozenset({"4", "5", "6", "7", "8"})


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
    schema_version = (
        publication_content.get("schema_version") if publication_content is not None else None
    )
    if schema_version in _LEGACY_READ_ONLY_FORMATS:
        return EditorialLegacyReadOnlyReason.FORMAT_NOT_EDITABLE
    if historical:
        return EditorialLegacyReadOnlyReason.HISTORICAL_VERSION
    return None


__all__ = [
    "EditorialLegacyReadOnlyReason",
    "publication_kind_is_ambiguous",
    "publication_read_only_reason",
]
