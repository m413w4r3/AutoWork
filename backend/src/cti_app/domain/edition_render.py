"""Versioned render records for immutable edition releases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from cti_app.domain.typst_render import (
    TypstRenderFormat,
    TypstRenderStatus,
    require_sha256,
    validate_typst_render_record,
)

EDITION_RENDERER = "typst"
EDITION_RENDERER_VERSION = "edition-v2-typst-v1"
EDITION_RENDER_POLICY_VERSION = "typst-edition-v2-v1"


class EditionRenderDisplayStatus(StrEnum):
    NONE = "none"
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


@dataclass(frozen=True, slots=True)
class EditionRender:
    id: UUID
    edition_release_id: UUID
    renderer: str
    renderer_version: str
    template_version: str
    template_sha256: str
    compiler: str
    compiler_version: str
    font_bundle_version: str
    render_policy_version: str
    format: TypstRenderFormat
    input_hash: str
    source_blob_id: UUID | None
    render_data_blob_id: UUID | None
    output_blob_id: UUID | None
    output_sha256: str | None
    output_byte_size: int | None
    status: TypstRenderStatus
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        validate_typst_render_record(self, owner_id_field="edition_release_id", label="edition")


def compute_edition_render_input_hash(
    *,
    edition_release_id: UUID,
    edition_document_sha256: str,
    renderer: str,
    renderer_version: str,
    template_version: str,
    template_sha256: str,
    compiler: str,
    compiler_version: str,
    font_bundle_version: str,
    render_policy_version: str,
    format: TypstRenderFormat | str,
) -> str:
    """Hash only the stable, functional inputs that define an edition render."""
    if not isinstance(edition_release_id, UUID):
        raise ValueError("edition_release_id must be a UUID")
    require_sha256(edition_document_sha256, "edition_document_sha256")
    require_sha256(template_sha256, "template_sha256")
    values = {
        "renderer": renderer,
        "renderer_version": renderer_version,
        "template_version": template_version,
        "compiler": compiler,
        "compiler_version": compiler_version,
        "font_bundle_version": font_bundle_version,
        "render_policy_version": render_policy_version,
        "format": format.value if isinstance(format, TypstRenderFormat) else format,
    }
    for field_name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be non-empty text")

    identity = {
        "edition_release_id": str(edition_release_id),
        "edition_document_sha256": edition_document_sha256,
        **values,
        "template_sha256": template_sha256,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
