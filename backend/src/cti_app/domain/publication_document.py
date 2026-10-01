"""Single entry point for the canonical publication document contract.

Readers, writers and validators use this module so the canonical document
format has one version boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cti_app.domain.publication import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
    publication_document_v4_from_json,
    publication_document_v4_to_json,
)

type CanonicalPublicationDocument = PublicationDocumentV4


def validate_publication_document(
    value: Mapping[str, Any] | CanonicalPublicationDocument,
) -> CanonicalPublicationDocument:
    """Validate one parsed model or canonical JSON payload."""
    if isinstance(value, PublicationDocumentV4):
        if value.schema_version != PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported publication document schema_version={value.schema_version!r}"
            )
        return value
    if not isinstance(value, Mapping):
        raise ValueError("publication document must be a JSON object")
    schema_version = value.get("schema_version")
    if schema_version != PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION:
        raise ValueError(f"unsupported publication document schema_version={schema_version!r}")
    return publication_document_v4_from_json(value)


def parse_publication_document(payload: Mapping[str, Any]) -> CanonicalPublicationDocument:
    """Parse and validate the canonical publication JSON payload."""
    return validate_publication_document(payload)


def serialize_publication_document(
    document: CanonicalPublicationDocument,
) -> dict[str, Any]:
    """Validate and serialize the canonical publication model."""
    return publication_document_v4_to_json(validate_publication_document(document))


__all__ = [
    "CanonicalPublicationDocument",
    "parse_publication_document",
    "publication_document_v4_from_json",
    "publication_document_v4_to_json",
    "serialize_publication_document",
    "validate_publication_document",
]
