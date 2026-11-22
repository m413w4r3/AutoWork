"""Wire-contract tests for ``sidecars.workspace_agent.protocol``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sidecars.workspace_agent import protocol

from tests.workspace_agent.conftest import REPOSITORY_ROOT  # noqa: F401  (sys.path insert)

EXPECTED_ERROR_CODES = {
    "invalid_request",
    "invalid_path",
    "unauthorized",
    "workspace_not_found",
    "workspace_identity_conflict",
    "workspace_unmanaged_directory",
    "revision_conflict",
    "concurrent_change",
    "policy_denied",
    "busy",
    "length_required",
    "size_limit",
    "hash_mismatch",
    "cursor_expired",
    "not_implemented",
    "internal_error",
    # M24-05 session supervisor (SESSION-RUNNER.md §6).
    "session_not_found",
    "session_state_conflict",
    "session_busy",
    "confinement_unavailable",
    "capability_unavailable",
    "resume_unavailable",
    "idempotency_conflict",
    "budget_exceeded",
    "manifest_missing",
    "secret_unavailable",
}


def test_protocol_and_framing_versions() -> None:
    assert protocol.PROTOCOL_VERSION == "workspace-agent.v1"
    assert protocol.HTTP_VERSION == "HTTP/1.1"


def test_limits_match_the_normative_design() -> None:
    assert protocol.JSON_BODY_LIMIT == 64 * 1024
    assert protocol.EDITABLE_FILE_LIMIT == 8 * 1024 * 1024
    assert protocol.GENERATED_FILE_LIMIT == 512 * 1024 * 1024
    assert protocol.SAMPLE_LIMIT == 512 * 1024 * 1024
    assert protocol.JSON_RESPONSE_LIMIT == 16 * 1024 * 1024
    assert protocol.CONTENT_READ_LIMIT == 32 * 1024 * 1024
    assert protocol.MAX_PAGE_SIZE == 500


def test_error_codes_are_the_closed_normative_set() -> None:
    assert {str(code) for code in protocol.ErrorCode} == EXPECTED_ERROR_CODES


def test_error_envelope_has_the_normative_shape() -> None:
    envelope = protocol.error_envelope(
        protocol.ErrorCode.REVISION_CONFLICT,
        "conflict",
        retryable=True,
        details={"current": "abc"},
    )
    assert envelope == {
        "error": "revision_conflict",
        "message": "conflict",
        "retryable": True,
        "details": {"current": "abc"},
    }


def test_error_envelope_defaults_to_empty_details_and_not_retryable() -> None:
    envelope = protocol.error_envelope(protocol.ErrorCode.NOT_IMPLEMENTED, "later")
    assert envelope["retryable"] is False
    assert envelope["details"] == {}


def test_error_envelope_rejects_codes_outside_the_contract() -> None:
    with pytest.raises(TypeError):
        protocol.error_envelope("not_a_code", "nope")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "value",
    ["abcd1234", "m24-w1.key-0001", "AbC-._:0123456", "a" * 128],
)
def test_idempotency_key_accepts_the_documented_alphabet(value: str) -> None:
    assert protocol.is_valid_idempotency_key(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "a" * 7,
        "a" * 129,
        "with space",
        "with/slash",
        "with+plus",
        "é" * 8,
    ],
)
def test_idempotency_key_rejects_everything_else(value: str | None) -> None:
    assert protocol.is_valid_idempotency_key(value) is False


def test_json_encoding_is_compact_utf8() -> None:
    assert protocol.encode_json({"k": "é", "n": 1}) == '{"k":"é","n":1}'.encode()


def test_json_decoding_requires_an_object() -> None:
    assert protocol.decode_json_object(b'{"a":1}') == {"a": 1}
    for raw in (b"[]", b'"text"', b"42", b"null"):
        with pytest.raises(protocol.JsonPayloadError):
            protocol.decode_json_object(raw)


def test_json_decoding_rejects_invalid_payloads() -> None:
    with pytest.raises(protocol.JsonPayloadError):
        protocol.decode_json_object(b"{not json")
    with pytest.raises(protocol.JsonPayloadError):
        protocol.decode_json_object(b"\xff\xfe")


def test_json_decoding_enforces_the_body_limit() -> None:
    with pytest.raises(protocol.JsonPayloadError):
        protocol.decode_json_object(b"{}", limit=1)
    with pytest.raises(protocol.JsonPayloadError):
        protocol.decode_json_object(b"x" * (protocol.JSON_BODY_LIMIT + 1))


def test_oversized_json_is_reported_as_a_size_error() -> None:
    with pytest.raises(protocol.JsonLimitError):
        protocol.decode_json_object(b"x" * 11, limit=10)


def test_rfc3339_formatting_is_utc_with_z() -> None:
    moment = datetime(2026, 11, 22, 10, 30, 0, 123456, tzinfo=UTC)
    assert protocol.format_rfc3339_utc(moment) == "2026-11-22T10:30:00Z"
    offset = timezone(timedelta(hours=2))
    shifted = datetime(2026, 11, 22, 12, 30, 0, tzinfo=offset)
    assert protocol.format_rfc3339_utc(shifted) == "2026-11-22T10:30:00Z"
