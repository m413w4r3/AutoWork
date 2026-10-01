"""State machine and invariants shared by every persisted Typst render record."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class TypstRenderFormat(StrEnum):
    PDF = "pdf"


class TypstRenderStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TypstRenderAcquisitionOutcome(StrEnum):
    ACQUIRED = "acquired"
    REUSABLE_SUCCEEDED = "reusable_succeeded"
    IN_PROGRESS = "in_progress"


class TypstRenderRecord(Protocol):
    """The lifecycle fields every render record (publication or edition) exposes."""

    @property
    def id(self) -> UUID: ...

    @property
    def input_hash(self) -> str: ...

    @property
    def status(self) -> TypstRenderStatus: ...

    @property
    def updated_at(self) -> datetime: ...

    @property
    def output_blob_id(self) -> UUID | None: ...

    @property
    def output_sha256(self) -> str | None: ...

    @property
    def output_byte_size(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class TypstRenderAcquisition[RecordT: TypstRenderRecord]:
    outcome: TypstRenderAcquisitionOutcome
    render: RecordT

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TypstRenderAcquisitionOutcome):
            raise ValueError("outcome must be a TypstRenderAcquisitionOutcome")
        expected_status = {
            TypstRenderAcquisitionOutcome.ACQUIRED: TypstRenderStatus.RUNNING,
            TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED: TypstRenderStatus.SUCCEEDED,
            TypstRenderAcquisitionOutcome.IN_PROGRESS: TypstRenderStatus.RUNNING,
        }[self.outcome]
        if self.render.status is not expected_status:
            raise ValueError("acquisition outcome does not match the render status")


def typst_render_acquisition_outcome(
    render: TypstRenderRecord, *, stale_running_before: datetime
) -> TypstRenderAcquisitionOutcome:
    """Choose the owner/reuse/wait result for an existing render row."""
    if render.status is TypstRenderStatus.SUCCEEDED:
        return TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED
    if render.status is TypstRenderStatus.FAILED:
        return TypstRenderAcquisitionOutcome.ACQUIRED
    if render.updated_at < stale_running_before:
        return TypstRenderAcquisitionOutcome.ACQUIRED
    return TypstRenderAcquisitionOutcome.IN_PROGRESS


def invalid_succeeded_reacquisition_outcome(
    render: TypstRenderRecord,
    *,
    observed_output_blob_id: UUID,
    observed_output_sha256: str,
) -> TypstRenderAcquisitionOutcome:
    """Choose a repair result without granting two callers the same ownership."""
    if render.status is TypstRenderStatus.RUNNING:
        return TypstRenderAcquisitionOutcome.IN_PROGRESS
    if render.status is TypstRenderStatus.FAILED:
        return TypstRenderAcquisitionOutcome.ACQUIRED
    if (
        render.output_blob_id == observed_output_blob_id
        and render.output_sha256 == observed_output_sha256
    ):
        return TypstRenderAcquisitionOutcome.ACQUIRED
    return TypstRenderAcquisitionOutcome.REUSABLE_SUCCEEDED


def typst_render_retrying[RecordT: TypstRenderRecord](render: RecordT, *, now: datetime) -> RecordT:
    """Return a running value for an atomic retry/takeover persistence transition."""
    return replace(  # type: ignore[type-var]
        render,
        status=TypstRenderStatus.RUNNING,
        error_code=None,
        error_message=None,
        updated_at=now,
    )


def require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")


def validate_typst_render_record(record: Any, *, owner_id_field: str, label: str) -> None:
    """Enforce the field-level and status invariants common to all render records."""
    for field_name in ("id", owner_id_field):
        if not isinstance(getattr(record, field_name), UUID):
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
        value = getattr(record, field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be non-empty text")
    if not isinstance(record.format, TypstRenderFormat):
        raise ValueError("format must be a TypstRenderFormat")
    if not isinstance(record.status, TypstRenderStatus):
        raise ValueError("status must be a TypstRenderStatus")
    for field_name in ("source_blob_id", "render_data_blob_id", "output_blob_id"):
        value = getattr(record, field_name)
        if value is not None and not isinstance(value, UUID):
            raise ValueError(f"{field_name} must be a UUID when present")
    require_sha256(record.template_sha256, "template_sha256")
    require_sha256(record.input_hash, "input_hash")
    if record.output_sha256 is not None:
        require_sha256(record.output_sha256, "output_sha256")
    if record.output_byte_size is not None and (
        not isinstance(record.output_byte_size, int)
        or isinstance(record.output_byte_size, bool)
        or record.output_byte_size <= 0
    ):
        raise ValueError("output_byte_size must be positive when present")
    if record.error_code is not None and (
        not isinstance(record.error_code, str) or not record.error_code.strip()
    ):
        raise ValueError("error_code must be non-empty text when present")
    if record.error_message is not None and not isinstance(record.error_message, str):
        raise ValueError("error_message must be text when present")
    for field_name in ("created_at", "updated_at"):
        timestamp = getattr(record, field_name)
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
        ):
            raise ValueError(f"{field_name} must be timezone-aware")

    if record.status is TypstRenderStatus.SUCCEEDED and (
        record.output_blob_id is None
        or record.output_sha256 is None
        or record.output_byte_size is None
        or record.output_byte_size <= 0
    ):
        raise ValueError(f"succeeded {label} renders require complete output metadata")
    if record.status is TypstRenderStatus.FAILED and record.error_code is None:
        raise ValueError(f"failed {label} renders require error_code")


__all__ = [
    "TypstRenderAcquisition",
    "TypstRenderAcquisitionOutcome",
    "TypstRenderFormat",
    "TypstRenderRecord",
    "TypstRenderStatus",
    "invalid_succeeded_reacquisition_outcome",
    "require_sha256",
    "typst_render_acquisition_outcome",
    "typst_render_retrying",
    "validate_typst_render_record",
]
