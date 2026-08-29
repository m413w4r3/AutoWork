"""Immutable contracts for freezing and rendering an edition publication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.publication import (
    BriefDocumentV1,
    PublicationDocumentV2,
    publication_document_from_json,
)

PUBLICATION_MANIFEST_SCHEMA_VERSION = "1"
EDITION_DOCUMENT_SCHEMA_VERSION = "1"  # historical EditionDocumentV1
EDITION_DOCUMENT_V2_SCHEMA_VERSION = "2"


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _datetime_from_json(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationManifestEntryV1:
    position: int
    subject_id: UUID
    production_run_id: UUID
    pipeline_generation: int
    document_artifact_id: UUID
    document_artifact_version: int
    document_input_hash: str

    def __post_init__(self) -> None:
        if self.position < 1:
            raise ValueError("manifest entry position must be >= 1")
        if self.pipeline_generation < 0:
            raise ValueError("pipeline_generation must be >= 0")
        if self.document_artifact_version < 1:
            raise ValueError("document_artifact_version must be >= 1")
        if len(self.document_input_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.document_input_hash
        ):
            raise ValueError("document_input_hash must be lowercase SHA-256")

    def to_json(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "subject_id": str(self.subject_id),
            "production_run_id": str(self.production_run_id),
            "pipeline_generation": self.pipeline_generation,
            "document_artifact_id": str(self.document_artifact_id),
            "document_artifact_version": self.document_artifact_version,
            "document_input_hash": self.document_input_hash,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> PublicationManifestEntryV1:
        return cls(
            position=int(payload["position"]),
            subject_id=UUID(str(payload["subject_id"])),
            production_run_id=UUID(str(payload["production_run_id"])),
            pipeline_generation=int(payload["pipeline_generation"]),
            document_artifact_id=UUID(str(payload["document_artifact_id"])),
            document_artifact_version=int(payload["document_artifact_version"]),
            document_input_hash=str(payload["document_input_hash"]),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationManifestExclusionV1:
    subject_id: UUID
    review_decision_id: UUID

    def to_json(self) -> dict[str, Any]:
        return {
            "subject_id": str(self.subject_id),
            "review_decision_id": str(self.review_decision_id),
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> PublicationManifestExclusionV1:
        return cls(
            subject_id=UUID(str(payload["subject_id"])),
            review_decision_id=UUID(str(payload["review_decision_id"])),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationManifestV1:
    edition_id: UUID
    edition_version: int
    batch_id: UUID
    created_by: str
    entries: tuple[PublicationManifestEntryV1, ...]
    exclusions: tuple[PublicationManifestExclusionV1, ...]
    content_sha256: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = PUBLICATION_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported publication manifest schema: {self.schema_version}")
        if self.edition_version < 1:
            raise ValueError("edition_version must be >= 1")
        if not self.created_by.strip():
            raise ValueError("created_by must not be empty")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

        entries = tuple(sorted(self.entries, key=lambda item: item.position))
        if any(item.position < 1 for item in entries):
            raise ValueError("manifest entry positions must be positive")
        if len({item.position for item in entries}) != len(entries):
            raise ValueError("manifest entry positions must be unique")
        if len({item.subject_id for item in entries}) != len(entries):
            raise ValueError("manifest entries must contain unique subjects")
        exclusions = tuple(sorted(self.exclusions, key=lambda item: str(item.subject_id)))
        if set(item.subject_id for item in entries) & {item.subject_id for item in exclusions}:
            raise ValueError("a subject cannot be included and excluded")
        if len({item.subject_id for item in exclusions}) != len(exclusions):
            raise ValueError("manifest exclusions must contain unique subjects")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "exclusions", exclusions)
        if len(self.content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.content_sha256
        ):
            raise ValueError("content_sha256 must be lowercase SHA-256")
        if self.content_sha256 != self.computed_content_sha256:
            raise ValueError("publication manifest content_sha256 does not match its content")

    @classmethod
    def create(
        cls,
        *,
        edition_id: UUID,
        edition_version: int,
        batch_id: UUID,
        created_by: str,
        entries: tuple[PublicationManifestEntryV1, ...],
        exclusions: tuple[PublicationManifestExclusionV1, ...],
        id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> PublicationManifestV1:
        manifest_id = id or uuid4()
        manifest_created_at = (created_at or datetime.now(UTC)).astimezone(UTC)
        draft = {
            "schema_version": PUBLICATION_MANIFEST_SCHEMA_VERSION,
            "id": str(manifest_id),
            "edition_id": str(edition_id),
            "edition_version": edition_version,
            "batch_id": str(batch_id),
            "created_at": manifest_created_at.isoformat(),
            "created_by": created_by,
            "entries": [item.to_json() for item in sorted(entries, key=lambda item: item.position)],
            "exclusions": [
                item.to_json() for item in sorted(exclusions, key=lambda item: str(item.subject_id))
            ],
        }
        return cls(
            id=manifest_id,
            edition_id=edition_id,
            edition_version=edition_version,
            batch_id=batch_id,
            created_at=manifest_created_at,
            created_by=created_by,
            entries=entries,
            exclusions=exclusions,
            content_sha256=_sha256(draft),
        )

    @property
    def computed_content_sha256(self) -> str:
        return _sha256(self.to_json(include_content_sha256=False))

    def to_json(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": str(self.id),
            "edition_id": str(self.edition_id),
            "edition_version": self.edition_version,
            "batch_id": str(self.batch_id),
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "entries": [entry.to_json() for entry in self.entries],
            "exclusions": [exclusion.to_json() for exclusion in self.exclusions],
        }
        if include_content_sha256:
            payload["content_sha256"] = self.content_sha256
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> PublicationManifestV1:
        content_sha256 = str(payload["content_sha256"])
        return cls(
            schema_version=str(payload["schema_version"]),
            id=UUID(str(payload["id"])),
            edition_id=UUID(str(payload["edition_id"])),
            edition_version=int(payload["edition_version"]),
            batch_id=UUID(str(payload["batch_id"])),
            created_at=_datetime_from_json(payload["created_at"], "created_at"),
            created_by=str(payload["created_by"]),
            entries=tuple(
                PublicationManifestEntryV1.from_json(item) for item in payload.get("entries", [])
            ),
            exclusions=tuple(
                PublicationManifestExclusionV1.from_json(item)
                for item in payload.get("exclusions", [])
            ),
            content_sha256=content_sha256,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionPublicationV1:
    position: int
    subject_id: UUID
    document: BriefDocumentV1

    def to_json(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "subject_id": str(self.subject_id),
            "document": self.document.to_json(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionDocumentV1:
    """Renderer-independent edition document made from frozen publications."""

    edition: dict[str, Any]
    publications: tuple[EditionPublicationV1, ...]
    schema_version: str = EDITION_DOCUMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EDITION_DOCUMENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported edition document schema: {self.schema_version}")
        publications = tuple(sorted(self.publications, key=lambda item: item.position))
        if any(item.position < 1 for item in publications):
            raise ValueError("edition publication positions must be positive")
        if len({item.position for item in publications}) != len(publications):
            raise ValueError("edition publication positions must be unique")
        object.__setattr__(self, "publications", publications)

    @property
    def edition_metadata(self) -> dict[str, Any]:
        return self.edition

    @property
    def ordered_publications(self) -> tuple[EditionPublicationV1, ...]:
        return self.publications

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "edition": self.edition,
            "publications": [publication.to_json() for publication in self.publications],
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> EditionDocumentV1:
        return cls(
            schema_version=str(payload["schema_version"]),
            edition=dict(payload["edition"]),
            publications=tuple(
                EditionPublicationV1(
                    position=int(item["position"]),
                    subject_id=UUID(str(item["subject_id"])),
                    document=_legacy_publication_from_json(item["document"]),
                )
                for item in payload.get("publications", [])
            ),
        )


def _legacy_publication_from_json(payload: Mapping[str, Any]) -> BriefDocumentV1:
    document = publication_document_from_json(payload)
    if not isinstance(document, BriefDocumentV1):
        raise ValueError("EditionDocumentV1 publications must contain BriefDocumentV1 documents")
    return document


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionPublicationV2:
    position: int
    subject_id: UUID
    document: PublicationDocumentV2

    def to_json(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "subject_id": str(self.subject_id),
            "document": self.document.to_json(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionDocumentV2:
    """Renderer-independent edition document for new publication releases."""

    edition: dict[str, Any]
    publications: tuple[EditionPublicationV2, ...]
    schema_version: str = EDITION_DOCUMENT_V2_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EDITION_DOCUMENT_V2_SCHEMA_VERSION:
            raise ValueError(f"unsupported edition document schema: {self.schema_version}")
        publications = tuple(sorted(self.publications, key=lambda item: item.position))
        if any(item.position < 1 for item in publications):
            raise ValueError("edition publication positions must be positive")
        if len({item.position for item in publications}) != len(publications):
            raise ValueError("edition publication positions must be unique")
        object.__setattr__(self, "publications", publications)

    @property
    def edition_metadata(self) -> dict[str, Any]:
        return self.edition

    @property
    def ordered_publications(self) -> tuple[EditionPublicationV2, ...]:
        return self.publications

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "edition": self.edition,
            "publications": [publication.to_json() for publication in self.publications],
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> EditionDocumentV2:
        publications: list[EditionPublicationV2] = []
        for item in payload.get("publications", []):
            document = publication_document_from_json(item["document"])
            if not isinstance(document, PublicationDocumentV2):
                raise ValueError(
                    "EditionDocumentV2 publications must contain "
                    "PublicationDocumentV2 documents"
                )
            publications.append(
                EditionPublicationV2(
                    position=int(item["position"]),
                    subject_id=UUID(str(item["subject_id"])),
                    document=document,
                )
            )
        return cls(
            schema_version=str(payload["schema_version"]),
            edition=dict(payload["edition"]),
            publications=tuple(publications),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EditionRelease:
    edition_id: UUID
    manifest_id: UUID
    edition_document_blob_id: UUID
    markdown_blob_id: UUID
    docx_blob_id: UUID
    edition_document_sha256: str
    markdown_sha256: str
    docx_sha256: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        for name in ("edition_document_sha256", "markdown_sha256", "docx_sha256"):
            value = getattr(self, name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be lowercase SHA-256")


__all__ = [
    "EDITION_DOCUMENT_SCHEMA_VERSION",
    "EDITION_DOCUMENT_V2_SCHEMA_VERSION",
    "PUBLICATION_MANIFEST_SCHEMA_VERSION",
    "EditionDocumentV1",
    "EditionDocumentV2",
    "EditionPublicationV1",
    "EditionPublicationV2",
    "EditionRelease",
    "PublicationManifestEntryV1",
    "PublicationManifestExclusionV1",
    "PublicationManifestV1",
]

# Short aliases keep callers from having to repeat the schema suffix when the
# surrounding type already establishes that it is a V1 manifest.
PublicationManifestEntry = PublicationManifestEntryV1
PublicationManifestExclusion = PublicationManifestExclusionV1

__all__ += ["PublicationManifestEntry", "PublicationManifestExclusion"]
