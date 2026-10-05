from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.domain.blobs import utc_now

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Media types accepted by the renderer-independent publication contract.
SUPPORTED_MEDIA_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/svg+xml", "image/webp", "image/gif"}
)


class MediaAssetKind(StrEnum):
    DIAGRAM_SVG = "diagram_svg"
    CHART_SVG = "chart_svg"
    SOURCE_FIGURE = "source_figure"


def media_asset_id(sha256: str, mime_type: str) -> UUID:
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise ValueError("Media asset SHA-256 must be lowercase hexadecimal")
    if not isinstance(mime_type, str) or not mime_type.strip():
        raise ValueError("Media asset MIME type must be non-empty")
    return uuid5(NAMESPACE_URL, f"autowork:media-asset:v1:{sha256}:{mime_type}")


@dataclass(frozen=True, slots=True)
class MediaAssetManifest:
    asset_id: UUID
    kind: MediaAssetKind
    blob_id: UUID
    sha256: str
    mime_type: str
    byte_size: int
    source: str
    policy_version: str
    compiler_name: str | None = None
    compiler_version: str | None = None
    provenance: str | None = None
    locator: dict[str, Any] | None = None
    decision: str | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, UUID) or not isinstance(self.blob_id, UUID):
            raise ValueError("Media asset identifiers must be UUIDs")
        if not isinstance(self.kind, MediaAssetKind):
            raise ValueError("Media asset kind is invalid")
        if _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("Media asset SHA-256 must be lowercase hexadecimal")
        if (
            not isinstance(self.mime_type, str)
            or not self.mime_type.strip()
            or type(self.byte_size) is not int
            or self.byte_size < 1
        ):
            raise ValueError("Media asset MIME type and size must be valid")
        if (
            not isinstance(self.source, str)
            or not self.source.strip()
            or not isinstance(self.policy_version, str)
            or not self.policy_version.strip()
        ):
            raise ValueError("Media asset source and policy version must be non-empty")
        if self.asset_id != media_asset_id(self.sha256, self.mime_type):
            raise ValueError("Media asset identity does not match its content address")
        if self.kind in {MediaAssetKind.DIAGRAM_SVG, MediaAssetKind.CHART_SVG}:
            if self.mime_type != "image/svg+xml":
                raise ValueError("A compiled editorial asset must be an SVG")
            if (
                not isinstance(self.compiler_name, str)
                or not self.compiler_name.strip()
                or not isinstance(self.compiler_version, str)
                or not self.compiler_version.strip()
            ):
                raise ValueError("A compiled editorial asset requires compiler metadata")
            if any(value is not None for value in (self.provenance, self.locator, self.decision)):
                raise ValueError("Compiled assets cannot carry source figure metadata")
        elif (
            not isinstance(self.provenance, str)
            or not self.provenance.strip()
            or not isinstance(self.locator, dict)
            or self.decision != "accepted"
            or self.compiler_name is not None
            or self.compiler_version is not None
        ):
            raise ValueError("A source figure asset requires provenance, locator and decision")
