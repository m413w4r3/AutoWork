"""Renderer-independent archived source figure values."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from cti_app.domain.production_editorial_enrichment import _text

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SourceFigureOriginKind(StrEnum):
    HTML_IMAGE = "html_image"


@dataclass(frozen=True, slots=True)
class SourceFigureProvenanceV1:
    origin_kind: SourceFigureOriginKind
    source_locator: str
    occurrence_index: int
    alt_text: str | None = None
    title_text: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.origin_kind, SourceFigureOriginKind):
            raise ValueError("Source figure origin kind is invalid")
        _text(self.source_locator, "Source figure locator", semantic=True)
        if type(self.occurrence_index) is not int or self.occurrence_index < 0:
            raise ValueError("Source figure occurrence index must be non-negative")
        if self.alt_text is not None:
            _text(self.alt_text, "Source figure alt text")
        if self.title_text is not None:
            _text(self.title_text, "Source figure title text")


@dataclass(frozen=True, slots=True)
class SourceFigureCandidateV1:
    key: str
    source_document_id: UUID
    media_type: str
    media_bytes: bytes
    media_sha256: str
    provenance: SourceFigureProvenanceV1

    def __post_init__(self) -> None:
        _text(self.key, "Source figure candidate key", semantic=True)
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Source figure source_document_id must be a UUID")
        if not isinstance(self.media_type, str) or not self.media_type.startswith("image/"):
            raise ValueError("Source figure media type must be an image MIME type")
        if not isinstance(self.media_bytes, bytes) or not self.media_bytes:
            raise ValueError("Source figure media bytes must be non-empty bytes")
        if not isinstance(self.media_sha256, str) or _SHA256.fullmatch(self.media_sha256) is None:
            raise ValueError("Source figure SHA-256 must be canonical lowercase hexadecimal")
        if hashlib.sha256(self.media_bytes).hexdigest() != self.media_sha256:
            raise ValueError("Source figure SHA-256 does not match its media bytes")
        if not isinstance(self.provenance, SourceFigureProvenanceV1):
            raise ValueError("Source figure provenance is invalid")
