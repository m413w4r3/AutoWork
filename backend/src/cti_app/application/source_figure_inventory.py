"""Application boundary for inventorying figures from archived source bytes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol, runtime_checkable
from uuid import UUID

from cti_app.domain.production_source_figures import SourceFigureCandidateV1

_PATH_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_IMAGE_MEDIA_TYPE = re.compile(r"^image/[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


def is_external_reference(locator: str) -> bool:
    """Return whether a locator names a resource outside the archived snapshot."""
    portable_locator = locator.replace("\\", "/")
    return portable_locator.startswith("//") or _PATH_SCHEME.match(portable_locator) is not None


def normalize_archive_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("Archived source asset path must be a non-empty string")

    portable_path = path.replace("\\", "/")
    if portable_path.startswith("/") or _PATH_SCHEME.match(portable_path):
        raise ValueError("Archived source asset path must be relative")

    parts = portable_path.split("/")
    if ".." in parts:
        raise ValueError("Archived source asset path must not traverse parent directories")

    normalized = PurePosixPath(portable_path).as_posix()
    if normalized in {"", "."}:
        raise ValueError("Archived source asset path must name a file")
    return normalized


@dataclass(frozen=True, slots=True)
class ArchivedSourceAsset:
    path: str
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_archive_path(self.path))
        if not isinstance(self.media_type, str) or not _IMAGE_MEDIA_TYPE.fullmatch(self.media_type):
            raise ValueError("Archived source asset media type must be an image MIME type")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("Archived source asset content must be non-empty bytes")


@dataclass(frozen=True, slots=True)
class ArchivedSourceDocumentSnapshot:
    source_document_id: UUID
    media_type: str
    body_bytes: bytes
    local_assets: tuple[ArchivedSourceAsset, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("Archived source document ID must be a UUID")
        if not isinstance(self.media_type, str):
            raise ValueError("Archived source document media type must be a string")
        if not isinstance(self.body_bytes, bytes):
            raise ValueError("Archived source document body must be bytes")
        if not isinstance(self.local_assets, tuple) or any(
            not isinstance(asset, ArchivedSourceAsset) for asset in self.local_assets
        ):
            raise ValueError("Archived source document local_assets must be a tuple of assets")

        paths = [asset.path for asset in self.local_assets]
        if len(paths) != len(set(paths)):
            raise ValueError("Archived source document asset paths must be unique")


class SourceFigureInventoryError(RuntimeError):
    """Base error for archived source figure inventory failures."""


class UnsupportedSourceFigureFormatError(SourceFigureInventoryError):
    """Raised when an archived source format cannot be inventoried."""


@runtime_checkable
class SourceFigureInventory(Protocol):
    async def inventory(
        self, source: ArchivedSourceDocumentSnapshot
    ) -> tuple[SourceFigureCandidateV1, ...]: ...
