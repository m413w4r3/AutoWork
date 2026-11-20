"""Shared occurrence resolution and identifier rules for thread commands."""

from __future__ import annotations

import json
import re
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.editorial_thread_types import EditorialThreadApplicationError
from cti_app.application.editorial_workspace import EditorialWorkspaceService
from cti_app.application.editorial_workspace.registry import WorkspaceOccurrenceResolution
from cti_app.domain.editorial_thread import (
    EditorialBindingState,
    EditorialThread,
    EditorialThreadBinding,
)
from cti_app.domain.editorial_workspace import EditorialWorkspaceError

# Editorial HTTP idempotency keys are opaque tokens: ^[A-Za-z0-9_-]{16,128}$.
_EDITORIAL_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{16,128}")
_EDITORIAL_REQUEST_SCHEMA = "editorial-thread-request-v1"


def encode_thread_request(text: str, objective: Literal["message", "proposal"]) -> bytes:
    return json.dumps(
        {
            "schema_version": _EDITORIAL_REQUEST_SCHEMA,
            "objective": objective,
            "instruction": text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_thread_request(body: bytes) -> tuple[Literal["message", "proposal"], str]:
    """Read the versioned request envelope and retain compatibility with M2 text blobs."""
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if (
        isinstance(value, dict)
        and value.get("schema_version") == _EDITORIAL_REQUEST_SCHEMA
        and value.get("objective") in {"message", "proposal"}
        and isinstance(value.get("instruction"), str)
        and set(value) == {"schema_version", "objective", "instruction"}
    ):
        objective = value["objective"]
        return objective, value["instruction"]
    try:
        return "message", body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("editorial_thread_message_corrupt") from None


def is_editorial_turn_objective(value: str | None) -> bool:
    return value in {"editorial_message", "editorial_proposal"}


def is_valid_editorial_idempotency_key(value: str) -> bool:
    return _EDITORIAL_IDEMPOTENCY_KEY.fullmatch(value) is not None


async def _resolve_current(
    workspace: EditorialWorkspaceService,
    subject_id: UUID,
    occurrence_id: UUID,
) -> WorkspaceOccurrenceResolution:
    try:
        resolution = await workspace.resolve_occurrence(subject_id, occurrence_id)
    except EditorialWorkspaceError:
        raise EditorialThreadApplicationError("thread_not_found") from None
    if (
        resolution.entry is None
        or resolution.binding_state != EditorialBindingState.VALID.value
        or resolution.artifact_id is None
        or resolution.artifact_version is None
        or resolution.canonical_sha256 is None
    ):
        raise EditorialThreadApplicationError("thread_not_found")
    return resolution


def _binding_from_resolution(
    resolution: WorkspaceOccurrenceResolution,
) -> EditorialThreadBinding:
    assert resolution.entry is not None
    assert resolution.artifact_id is not None
    assert resolution.artifact_version is not None
    assert resolution.canonical_sha256 is not None
    return EditorialThreadBinding(
        subject_id=resolution.record.subject_id,
        production_run_id=resolution.record.production_run_id,
        occurrence_id=resolution.entry.block_id,
        artifact_id=resolution.artifact_id,
        artifact_version=resolution.artifact_version,
        canonical_sha256=resolution.canonical_sha256,
    )


def _same_thread_scope(thread: EditorialThread, binding: EditorialThreadBinding) -> bool:
    return (
        thread.origin.subject_id == binding.subject_id
        and thread.origin.production_run_id == binding.production_run_id
        and thread.origin.occurrence_id == binding.occurrence_id
    )


def _new_session_thread_id(
    previous: EditorialThread, base_session_number: int, idempotency_key: str
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "editorial-thread-session:v1:"
        f"{previous.origin.subject_id}:{previous.origin.production_run_id}:"
        f"{previous.origin.occurrence_id}:{base_session_number}:{idempotency_key}",
    )
