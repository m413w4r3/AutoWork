"""Shared verification and diagnostics for archived production model output."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from cti_app.application.model_gateway import ModelGateway
from cti_app.domain.model_runs import ModelRun


async def verified_raw_output_text(
    model_gateway: ModelGateway,
    run: ModelRun,
    *,
    error_prefix: str,
    error_field: str = "error_code",
    expected_text: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Read the exact provider response bytes and verify them before parsing."""
    reference = run.raw_output_reference or (
        run.output_references[0] if run.output_references else None
    )

    def failure(reason: str) -> dict[str, Any]:
        return {error_field: f"{error_prefix}_{reason}"}

    if not reference or not run.raw_output_sha256:
        return None, failure("raw_output_archive_missing")
    try:
        raw_bytes = await model_gateway.read_output(reference)
    except Exception:
        return None, failure("raw_output_archive_unreadable")
    if hashlib.sha256(raw_bytes).hexdigest() != run.raw_output_sha256:
        return None, failure("raw_output_hash_mismatch")
    if expected_text is not None and raw_bytes != expected_text.encode("utf-8"):
        return None, failure("raw_output_response_mismatch")
    try:
        return raw_bytes.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, failure("raw_output_encoding_invalid")


async def record_wire_parse_diagnostics(
    model_gateway: ModelGateway,
    run: ModelRun,
    *,
    parser_stage: str,
    parse_identity: str,
    validation_errors: Sequence[Mapping[str, Any]],
    transformations: Sequence[str],
    normalized_output: bytes | None = None,
    normalized_mime_type: str = "application/json; charset=utf-8",
) -> str:
    """Persist parser-only identity and normalized output beside immutable raw bytes."""
    if not run.raw_output_sha256:
        raise ValueError("Wire parsing requires a verified raw output digest")
    if run.parser_stage == parser_stage and run.normalization_version == parse_identity:
        return parse_identity

    normalized_reference: str | None = None
    normalized_sha256: str | None = None
    if normalized_output is not None:
        normalized_sha256 = hashlib.sha256(normalized_output).hexdigest()
        normalized_reference = await model_gateway.archive_output(
            normalized_output,
            mime_type=normalized_mime_type,
        )

    await model_gateway.record_output_diagnostics(
        run.id,
        normalized_reference=normalized_reference,
        normalized_sha256=normalized_sha256,
        parser_stage=parser_stage,
        normalization_version=parse_identity,
        transformations=tuple(transformations),
        validation_errors=tuple(dict(item) for item in validation_errors),
    )
    return parse_identity
