"""Versioned render records for canonical publication artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

PUBLICATION_RENDER_POLICY_VERSION = "typst-publication-v4-v1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PublicationRenderFormat(StrEnum):
    PDF = "pdf"


class PublicationRenderStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PublicationRender:
    id: UUID
    publication_artifact_id: UUID
    renderer: str
    renderer_version: str
    template_version: str
    template_sha256: str
    compiler: str
    compiler_version: str
    format: PublicationRenderFormat
    input_hash: str
    source_blob_id: UUID | None
    render_data_blob_id: UUID | None
    output_blob_id: UUID | None
    output_sha256: str | None
    output_byte_size: int | None
    status: PublicationRenderStatus
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "publication_artifact_id"):
            if not isinstance(getattr(self, field_name), UUID):
                raise ValueError(f"{field_name} must be a UUID")
        for field_name in (
            "renderer",
            "renderer_version",
            "template_version",
            "compiler",
            "compiler_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
        if not isinstance(self.format, PublicationRenderFormat):
            raise ValueError("format must be a PublicationRenderFormat")
        if not isinstance(self.status, PublicationRenderStatus):
            raise ValueError("status must be a PublicationRenderStatus")
        for field_name in ("source_blob_id", "render_data_blob_id", "output_blob_id"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, UUID):
                raise ValueError(f"{field_name} must be a UUID when present")
        _require_sha256(self.template_sha256, "template_sha256")
        _require_sha256(self.input_hash, "input_hash")
        if self.output_sha256 is not None:
            _require_sha256(self.output_sha256, "output_sha256")
        if self.output_byte_size is not None and (
            not isinstance(self.output_byte_size, int)
            or isinstance(self.output_byte_size, bool)
            or self.output_byte_size <= 0
        ):
            raise ValueError("output_byte_size must be positive when present")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or not self.error_code.strip()
        ):
            raise ValueError("error_code must be non-empty text when present")
        if self.error_message is not None and not isinstance(self.error_message, str):
            raise ValueError("error_message must be text when present")
        for field_name, timestamp in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
        ):
            if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp.utcoffset() is None
            ):
                raise ValueError(f"{field_name} must be timezone-aware")

        if self.status is PublicationRenderStatus.SUCCEEDED and (
            self.output_blob_id is None
            or self.output_sha256 is None
            or self.output_byte_size is None
            or self.output_byte_size <= 0
        ):
            raise ValueError("succeeded publication renders require complete output metadata")
        if self.status is PublicationRenderStatus.FAILED and self.error_code is None:
            raise ValueError("failed publication renders require error_code")


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")


def compute_publication_render_input_hash(
    *,
    publication_artifact_id: UUID,
    publication_content_sha256: str,
    renderer: str,
    renderer_version: str,
    template_version: str,
    template_sha256: str,
    compiler: str,
    compiler_version: str,
    format: PublicationRenderFormat | str,
    font_bundle_version: str,
    render_policy_version: str,
) -> str:
    """Hash only the stable, functional inputs that define a publication render."""
    if not isinstance(publication_artifact_id, UUID):
        raise ValueError("publication_artifact_id must be a UUID")
    _require_sha256(publication_content_sha256, "publication_content_sha256")
    _require_sha256(template_sha256, "template_sha256")
    values = {
        "renderer": renderer,
        "renderer_version": renderer_version,
        "template_version": template_version,
        "compiler": compiler,
        "compiler_version": compiler_version,
        "format": format.value if isinstance(format, PublicationRenderFormat) else format,
        "font_bundle_version": font_bundle_version,
        "render_policy_version": render_policy_version,
    }
    for field_name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be non-empty text")

    identity = {
        "publication_artifact_id": str(publication_artifact_id),
        "publication_content_sha256": publication_content_sha256,
        **values,
        "template_sha256": template_sha256,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
