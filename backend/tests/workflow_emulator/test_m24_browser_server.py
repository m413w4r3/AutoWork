"""Environment guards for the M24 investigation browser launcher.

The launcher must refuse to boot without the fake-provider attestation, a
loopback PostgreSQL administration DSN, a state path and a synthetic operator
token.  These checks run before any database or application object exists, and
the published state must never carry the token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.agent_session_verifier import build_agent_session_message
from tests.acceptance.scenario_support.harness import ScenarioMaterial
from tests.workflow_emulator.m24_browser_server import (
    ATTESTATION_ENV,
    DEFAULT_API_PORT,
    MIN_OPERATOR_TOKEN_LENGTH,
    OPERATOR_TOKEN_ENV,
    BrowserState,
    _is_loopback_host,
    _signed_action_request,
    _signed_session_headers,
    advisory_text_for,
    make_shutdown_handler,
    prompt_canonical_targets,
    prompt_consultation_type,
    two_paragraph_note,
    validate_environment,
)

pytestmark = pytest.mark.workflow_fast

LOOPBACK_DSN = "postgresql+asyncpg://postgres:postgres@127.0.0.1:55432/postgres"
SYNTHETIC_TOKEN = "m24-browser-operator-token-0123456789abcdef"
STATE_PATH = "/tmp/m24-browser-test-state.json"


def _set_required(monkeypatch: pytest.MonkeyPatch, *, dsn: str = LOOPBACK_DSN) -> None:
    monkeypatch.setenv(ATTESTATION_ENV, "1")
    monkeypatch.setenv("TEST_POSTGRES_ADMIN_DSN", dsn)
    monkeypatch.setenv("WORKFLOW_BROWSER_STATE", STATE_PATH)
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, SYNTHETIC_TOKEN)
    monkeypatch.delenv("WORKFLOW_BROWSER_API_PORT", raising=False)


@pytest.mark.parametrize(
    ("host", "expected"),
    (
        (None, True),
        ("", True),
        ("/var/run/postgresql", True),
        ("localhost", True),
        ("LOCALHOST.", True),
        ("127.0.0.1", True),
        ("127.9.9.9", True),
        ("::1", True),
        ("example.com", False),
        ("10.0.0.5", False),
        ("192.168.1.10", False),
    ),
)
def test_is_loopback_host(host: str | None, expected: bool) -> None:
    assert _is_loopback_host(host) is expected


def test_validate_environment_requires_fake_provider_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv(ATTESTATION_ENV)

    with pytest.raises(RuntimeError, match=ATTESTATION_ENV):
        validate_environment()


def test_validate_environment_rejects_non_loopback_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch, dsn="postgresql+asyncpg://postgres:postgres@db.example.com/postgres")

    with pytest.raises(RuntimeError, match="local PostgreSQL"):
        validate_environment()


def test_validate_environment_requires_state_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv("WORKFLOW_BROWSER_STATE")

    with pytest.raises(RuntimeError, match="WORKFLOW_BROWSER_STATE"):
        validate_environment()


def test_validate_environment_requires_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv(OPERATOR_TOKEN_ENV)

    with pytest.raises(RuntimeError, match=OPERATOR_TOKEN_ENV):
        validate_environment()


@pytest.mark.parametrize("token", ("too-short", "x" * (MIN_OPERATOR_TOKEN_LENGTH - 1)))
def test_validate_environment_rejects_short_operator_token(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, token)

    with pytest.raises(RuntimeError, match=OPERATOR_TOKEN_ENV):
        validate_environment()


def test_validate_environment_rejects_whitespace_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv(OPERATOR_TOKEN_ENV, "m24 browser token with spaces 0123456789")

    with pytest.raises(RuntimeError, match=OPERATOR_TOKEN_ENV):
        validate_environment()


def test_validate_environment_rejects_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("WORKFLOW_BROWSER_API_PORT", "not-a-port")

    with pytest.raises(RuntimeError, match="WORKFLOW_BROWSER_API_PORT"):
        validate_environment()


def test_validate_environment_returns_the_loopback_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)

    environment = validate_environment()

    assert environment.admin_dsn == LOOPBACK_DSN
    assert environment.state_path == Path(STATE_PATH)
    assert environment.api_port == DEFAULT_API_PORT
    assert environment.operator_token == SYNTHETIC_TOKEN


def _browser_state(**overrides: Any) -> BrowserState:
    from uuid import uuid4

    defaults: dict[str, Any] = {
        "subject_id": uuid4(),
        "edition_id": uuid4(),
        "production_run_id": uuid4(),
        "synthesis_artifact_id": uuid4(),
        "synthesis_sha256": "a" * 64,
        "publication_artifact_id": uuid4(),
        "publication_sha256": "b" * 64,
        "subject_slug": "m24-browser-subject",
        "database_name": "cti_m24_browser_test",
        "api_port": DEFAULT_API_PORT,
        "workspace_root": Path("/tmp/m24browser-test-ws"),
        "material": ScenarioMaterial.synthetic(),
    }
    defaults.update(overrides)
    return BrowserState(**defaults)


def test_browser_state_never_publishes_the_operator_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    environment = validate_environment()

    payload = _browser_state(api_port=environment.api_port).as_json()
    serialized = json.dumps(payload)

    assert environment.operator_token not in serialized
    assert "authorization" not in serialized.casefold()
    assert "bearer" not in serialized.casefold()
    assert payload["material"]["hashes"]
    assert "samples" not in payload["material"]


def test_shutdown_handler_sets_the_server_exit_flag() -> None:
    class _Server:
        should_exit = False

    server = _Server()
    import asyncio

    result = asyncio.run(make_shutdown_handler(server)())

    assert result == {"status": "shutting_down"}
    assert server.should_exit is True


def test_prompt_consultation_type_reads_the_advisor_header() -> None:
    assert prompt_consultation_type("ROLE: test\nTYPE: plan_investigation\nTASK: x") == (
        "plan_investigation"
    )
    assert prompt_consultation_type("ROLE: test\nTASK: x") == ""


def test_prompt_canonical_targets_reads_only_the_marked_block() -> None:
    prompt = (
        "CIBLES CANONIQUES (conserver exactement ces champs):\n"
        '{"id": "U1", "block_type": "key_findings", "form": "analyst_note", '
        '"anchor": "lead:0001"}\n'
        '{"id": "U2", "block_type": "network_and_infrastructure", "form": "body"}\n'
        "U1 — Constats clés:\n"
        "| Un texte cible."
    )

    targets = prompt_canonical_targets(prompt)

    assert [target["id"] for target in targets] == ["U1", "U2"]
    assert targets[0]["anchor"] == "lead:0001"


def test_two_paragraph_note_keeps_a_stored_paragraph_break() -> None:
    from cti_app.domain.opinion_note_proposal import note_draft_text_from_sentences

    sentences = [item["text"] for item in two_paragraph_note("U1")]

    text = note_draft_text_from_sentences(sentences)

    assert "\n\n" in text
    assert text.count("\n\n") == 1


def test_correction_note_differs_and_keeps_one_paragraph_break() -> None:
    from cti_app.domain.opinion_note_proposal import note_draft_text_from_sentences

    first = note_draft_text_from_sentences([item["text"] for item in two_paragraph_note("U1")])
    revised = note_draft_text_from_sentences(
        [item["text"] for item in two_paragraph_note("U1", revised=True)]
    )

    assert revised != first
    assert "corrigee" in revised.casefold()
    assert revised.count("\n\n") == 1


def test_signed_action_request_builds_the_tool_call_wire() -> None:
    raw, key, kind = _signed_action_request(
        {
            "kind": "tool_call",
            "tool_name": "task.submit_result",
            "schema_version": "1",
            "arguments": {"expected_status_version": 3},
            "idempotency_key": "m24-browser-submit",
        }
    )

    assert kind == "tool_call"
    assert key == "m24-browser-submit"
    assert json.loads(raw) == {
        "tool_name": "task.submit_result",
        "schema_version": "1",
        "arguments": {"expected_status_version": 3},
    }


def test_signed_action_request_builds_the_instruction_ack_wire() -> None:
    instruction_id = str(uuid4())
    raw, key, kind = _signed_action_request(
        {"kind": "instruction", "action": "ack", "instruction_ids": [instruction_id]}
    )

    assert kind == "instruction"
    assert key is None
    assert json.loads(raw) == {"action": "ack", "instruction_ids": [instruction_id]}


@pytest.mark.parametrize(
    "body",
    (
        {"kind": "instruction", "action": "enqueue", "text": "x"},
        {"kind": "instruction", "action": "ack", "instruction_ids": []},
        {"kind": "tool_call", "tool_name": "x"},
        {"kind": "other"},
    ),
)
def test_signed_action_request_refuses_unknown_shapes(body: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _signed_action_request(body)


def test_advisory_text_declares_the_requested_consultation_type() -> None:
    critique = advisory_text_for("critique")
    interpretation = advisory_text_for("interpretation")

    assert "TYPE: critique" in critique
    assert "TYPE: pivot_strategy" not in critique
    assert "TYPE: interpretation" in interpretation
    assert "refs=E1" not in critique
    assert "STATUS: succeeded" in critique


def test_signed_session_headers_sign_the_exact_d7_message() -> None:
    secret = b"s" * 32
    session_id = uuid4()
    path = f"/api/investigations/{uuid4()}/agent-sessions/{session_id}/instructions"
    body = b'{"action":"pull"}'

    headers = _signed_session_headers(
        secret=secret,
        session_id=session_id,
        path_and_query=path,
        body=body,
        idempotency_key=None,
    )
    message = build_agent_session_message(
        method="POST",
        path_and_query=path,
        session_id=session_id,
        body=body,
        idempotency_key=None,
        timestamp=headers["X-AutoWork-Agent-Timestamp"],
    )

    assert headers["X-AutoWork-Agent-Session"] == str(session_id)
    assert headers["X-AutoWork-Agent-Signature"] == (
        "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest()
    )
    assert "Idempotency-Key" not in headers
