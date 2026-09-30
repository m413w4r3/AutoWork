"""Single entry point for the canonical publication document contract.

Readers, writers and validators use this module so the canonical document
format has one version boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cti_app.domain.publication import PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION, PublicationDocumentV3

type CanonicalPublicationDocument = PublicationDocumentV3


def validate_publication_document(
    value: Mapping[str, Any] | CanonicalPublicationDocument,
) -> CanonicalPublicationDocument:
    """Validate one parsed model or canonical JSON payload."""
    if isinstance(value, PublicationDocumentV3):
        if value.schema_version != PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported publication document schema_version={value.schema_version!r}"
            )
        return value
    if not isinstance(value, Mapping):
        raise ValueError("publication document must be a JSON object")
    schema_version = value.get("schema_version")
    if schema_version != PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION:
        raise ValueError(f"unsupported publication document schema_version={schema_version!r}")
    return PublicationDocumentV3._from_json(value)


def parse_publication_document(payload: Mapping[str, Any]) -> CanonicalPublicationDocument:
    """Parse and validate the canonical publication JSON payload."""
    return validate_publication_document(payload)


def serialize_publication_document(
    document: CanonicalPublicationDocument,
) -> dict[str, Any]:
    """Validate and serialize the canonical publication model."""
    return validate_publication_document(document)._to_json()


__all__ = [
    "CanonicalPublicationDocument",
    "parse_publication_document",
    "serialize_publication_document",
    "validate_publication_document",
]
