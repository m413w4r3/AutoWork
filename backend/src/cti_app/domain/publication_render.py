"""Versioned render records for canonical publication artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from cti_app.domain.typst_render import (
    TypstRenderFormat,
    TypstRenderStatus,
    require_sha256,
    validate_typst_render_record,
)

PUBLICATION_RENDER_POLICY_VERSION = "typst-publication-v4-v1"


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
        validate_typst_render_record(
            self, owner_id_field="publication_artifact_id", label="publication"
        )


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
    format: TypstRenderFormat | str,
    font_bundle_version: str,
    render_policy_version: str,
) -> str:
    """Hash only the stable, functional inputs that define a publication render."""
    if not isinstance(publication_artifact_id, UUID):
        raise ValueError("publication_artifact_id must be a UUID")
    require_sha256(publication_content_sha256, "publication_content_sha256")
    require_sha256(template_sha256, "template_sha256")
    values = {
        "renderer": renderer,
        "renderer_version": renderer_version,
        "template_version": template_version,
        "compiler": compiler,
        "compiler_version": compiler_version,
        "format": format.value if isinstance(format, TypstRenderFormat) else format,
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
