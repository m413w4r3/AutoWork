"""Versioned render records for immutable edition releases."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from uuid import UUID

EDITION_RENDERER = "typst"
EDITION_RENDERER_VERSION = "edition-v2-typst-v1"
EDITION_TEMPLATE_VERSION = "chp-edition-v1"
EDITION_RENDER_POLICY_VERSION = "typst-edition-v2-v1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class EditionRenderFormat(StrEnum):
    PDF = "pdf"


EDITION_RENDER_FORMAT = EditionRenderFormat.PDF


class EditionRenderStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EditionRenderDisplayStatus(StrEnum):
    NONE = "none"
    NOT_STARTED = "not_started"
    QUEUED = "queued"
    RUNNING = "running"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class EditionRenderAcquisitionOutcome(StrEnum):
    ACQUIRED = "acquired"
    REUSABLE_SUCCEEDED = "reusable_succeeded"
    IN_PROGRESS = "in_progress"


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
    format: EditionRenderFormat
    input_hash: str
    source_blob_id: UUID | None
    render_data_blob_id: UUID | None
    output_blob_id: UUID | None
    output_sha256: str | None
    output_byte_size: int | None
    status: EditionRenderStatus
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("id", "edition_release_id"):
            if not isinstance(getattr(self, field_name), UUID):
                raise ValueError(f"{field_name} must be a UUID")
        for field_name in (
            "renderer",
            "renderer_version",
            "template_version",
            "compiler",
            "compiler_version",
            "font_bundle_version",
            "render_policy_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-empty text")
        if not isinstance(self.format, EditionRenderFormat):
            raise ValueError("format must be an EditionRenderFormat")
        if not isinstance(self.status, EditionRenderStatus):
            raise ValueError("status must be an EditionRenderStatus")
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

        if self.status is EditionRenderStatus.SUCCEEDED and (
            self.output_blob_id is None
            or self.output_sha256 is None
            or self.output_byte_size is None
            or self.output_byte_size <= 0
        ):
            raise ValueError("succeeded edition renders require complete output metadata")
        if self.status is EditionRenderStatus.FAILED and self.error_code is None:
            raise ValueError("failed edition renders require error_code")


@dataclass(frozen=True, slots=True)
class EditionRenderAcquisition:
    outcome: EditionRenderAcquisitionOutcome
    render: EditionRender

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, EditionRenderAcquisitionOutcome):
            raise ValueError("outcome must be an EditionRenderAcquisitionOutcome")
        if not isinstance(self.render, EditionRender):
            raise ValueError("render must be an EditionRender")
        expected_status = {
            EditionRenderAcquisitionOutcome.ACQUIRED: EditionRenderStatus.RUNNING,
            EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED: EditionRenderStatus.SUCCEEDED,
            EditionRenderAcquisitionOutcome.IN_PROGRESS: EditionRenderStatus.RUNNING,
        }[self.outcome]
        if self.render.status is not expected_status:
            raise ValueError("acquisition outcome does not match the render status")


def edition_render_acquisition_outcome(
    render: EditionRender, *, stale_running_before: datetime
) -> EditionRenderAcquisitionOutcome:
    """Choose the owner/reuse/wait result for an existing render row."""
    if render.status is EditionRenderStatus.SUCCEEDED:
        return EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED
    if render.status is EditionRenderStatus.FAILED:
        return EditionRenderAcquisitionOutcome.ACQUIRED
    if render.updated_at < stale_running_before:
        return EditionRenderAcquisitionOutcome.ACQUIRED
    return EditionRenderAcquisitionOutcome.IN_PROGRESS


def invalid_succeeded_edition_render_reacquisition_outcome(
    render: EditionRender,
    *,
    observed_output_blob_id: UUID,
    observed_output_sha256: str,
) -> EditionRenderAcquisitionOutcome:
    """Choose a repair result without granting two callers the same ownership."""
    if render.status is EditionRenderStatus.RUNNING:
        return EditionRenderAcquisitionOutcome.IN_PROGRESS
    if render.status is EditionRenderStatus.FAILED:
        return EditionRenderAcquisitionOutcome.ACQUIRED
    if (
        render.output_blob_id == observed_output_blob_id
        and render.output_sha256 == observed_output_sha256
    ):
        return EditionRenderAcquisitionOutcome.ACQUIRED
    return EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED


def edition_render_retrying(render: EditionRender, *, now: datetime) -> EditionRender:
    """Return a running value for an atomic retry/takeover persistence transition."""
    return replace(
        render,
        status=EditionRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        updated_at=now,
    )


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")


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
    format: EditionRenderFormat | str,
) -> str:
    """Hash only the stable, functional inputs that define an edition render."""
    if not isinstance(edition_release_id, UUID):
        raise ValueError("edition_release_id must be a UUID")
    _require_sha256(edition_document_sha256, "edition_document_sha256")
    _require_sha256(template_sha256, "template_sha256")
    values = {
        "renderer": renderer,
        "renderer_version": renderer_version,
        "template_version": template_version,
        "compiler": compiler,
        "compiler_version": compiler_version,
        "font_bundle_version": font_bundle_version,
        "render_policy_version": render_policy_version,
        "format": format.value if isinstance(format, EditionRenderFormat) else format,
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
