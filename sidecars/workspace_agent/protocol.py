"""Wire contract: versions, limits, error envelope, idempotency and JSON codecs.

Normative source: ``docs/design/investigation-workshop/WORKSPACE-AGENT.md`` §4.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum

PROTOCOL_VERSION = "workspace-agent.v1"
HTTP_VERSION = "HTTP/1.1"

JSON_BODY_LIMIT = 64 * 1024
EDITABLE_FILE_LIMIT = 8 * 1024 * 1024
GENERATED_FILE_LIMIT = 512 * 1024 * 1024
SAMPLE_LIMIT = 512 * 1024 * 1024
JSON_RESPONSE_LIMIT = 16 * 1024 * 1024
CONTENT_READ_LIMIT = 32 * 1024 * 1024
MAX_PAGE_SIZE = 500

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class ErrorCode(StrEnum):
    """Closed error-code set of the agent protocol."""

    INVALID_REQUEST = "invalid_request"
    INVALID_PATH = "invalid_path"
    UNAUTHORIZED = "unauthorized"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_IDENTITY_CONFLICT = "workspace_identity_conflict"
    WORKSPACE_UNMANAGED_DIRECTORY = "workspace_unmanaged_directory"
    REVISION_CONFLICT = "revision_conflict"
    CONCURRENT_CHANGE = "concurrent_change"
    POLICY_DENIED = "policy_denied"
    BUSY = "busy"
    LENGTH_REQUIRED = "length_required"
    SIZE_LIMIT = "size_limit"
    HASH_MISMATCH = "hash_mismatch"
    CURSOR_EXPIRED = "cursor_expired"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"


class ProtocolError(ValueError):
    """Base class for rejected wire payloads."""


class JsonPayloadError(ProtocolError):
    """A JSON payload is malformed or not a JSON object."""


class JsonLimitError(JsonPayloadError):
    """A JSON payload exceeds the configured byte budget."""


def error_envelope(
    code: ErrorCode,
    message: str,
    *,
    retryable: bool = False,
    details: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the normative error body ``{error,message,retryable,details}``."""
    if not isinstance(code, ErrorCode):
        raise TypeError("error code must be an ErrorCode member")
    return {
        "error": str(code),
        "message": message,
        "retryable": retryable,
        "details": {} if details is None else dict(details),
    }


def is_valid_idempotency_key(value: str | None) -> bool:
    """Check the documented ``^[A-Za-z0-9._:-]{8,128}$`` key format."""
    return value is not None and IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is not None


def encode_json(payload: Mapping[str, object]) -> bytes:
    """Encode a compact UTF-8 JSON body."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def decode_json_object(raw: bytes, *, limit: int = JSON_BODY_LIMIT) -> dict[str, object]:
    """Decode one bounded UTF-8 JSON object, rejecting every other shape."""
    if len(raw) > limit:
        raise JsonLimitError("JSON payload exceeds the byte limit")
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JsonPayloadError("malformed JSON payload") from exc
    if not isinstance(value, dict):
        raise JsonPayloadError("a JSON object is required")
    return {str(key): item for key, item in value.items()}


def format_rfc3339_utc(moment: datetime) -> str:
    """Format a timezone-aware instant as ``YYYY-MM-DDTHH:MM:SSZ``."""
    if moment.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
