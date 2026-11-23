"""M24.1 R2-FIX-A oracles: F2 capture secret refusal and F5 closed wire alias.

The host must never persist agent content that carries the session secret
(raw, hex, standard/urlsafe base64, or a ≥16-byte aligned fragment); the first
detection revokes the session.  The backend's ``OperatorAdapterKind`` wire
names are aliased to the host profile names exactly once, at the entry point.

Every test is offline and rootless: a real ``WorkspaceAgent`` serves a real
HTTP server on a tmpdir AF_UNIX socket, and the ``fake`` profile is registered
under its canonical name.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from sidecars.workspace_agent.sessions import supervisor as sessions_supervisor

from tests.workspace_agent.sessions_support import (
    COLLECT_KEY,
    PREPARE_KEY,
    SECRET,
    SESSION_ID,
    START_KEY,
    TASK_ID,
    fake_script,
    get_session,
    log_lines,
    post_session,
    prepare_payload,
    running_session_agent,
    session_events,
    wait_for_state,
    workspace_files_containing,
    write_fake_script,
)


def _std_b64(value: bytes) -> bytes:
    return base64.b64encode(value)


def _url_b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value)


# A secret that really exercises the urlsafe alphabet (+/ vs -_).
URLSAFE_SECRET = bytes([0xFB, 0xFF, 0xBF]) + SECRET[3:]


def _encoded_forms(secret: bytes) -> dict[str, bytes]:
    standard = _std_b64(secret)
    urlsafe = _url_b64(secret)
    fragment = secret[8:24]
    return {
        "raw": secret,
        "hex": secret.hex().encode("ascii"),
        "hex_upper": secret.hex().upper().encode("ascii"),
        "b64": standard,
        "b64_unpadded": standard.rstrip(b"="),
        "b64url": urlsafe,
        "b64url_unpadded": urlsafe.rstrip(b"="),
        "fragment_raw": fragment,
        "fragment_hex": fragment.hex().encode("ascii"),
        "fragment_b64": _std_b64(fragment),
    }


def _prepare(client, running, workspace_id: str, *, secret: bytes = SECRET):
    return post_session(
        client,
        running,
        "/v1/sessions",
        prepare_payload(workspace_id, auth_secret=secret),
        PREPARE_KEY,
    )


def _drop_note(running, name: str, data: bytes) -> Path:
    """Drop a file where the sandbox's rw ``scratch/`` bind exposes it."""
    notes: Path = running.session_dir() / "scratch" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / name
    path.write_bytes(data)
    return path


def _collect(client, running, key: str = COLLECT_KEY):
    return post_session(
        client,
        running,
        f"/v1/sessions/{SESSION_ID}/collect",
        {"capture": True},
        key,
    )


def _status(client, running) -> dict[str, object]:
    response = get_session(client, running, f"/v1/sessions/{SESSION_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return {str(key): value for key, value in body.items()}


def test_alias_is_closed_and_symmetric() -> None:
    from sidecars.workspace_agent.sessions.supervisor import (
        HOST_ADAPTER_WIRE_NAMES,
        WIRE_ADAPTER_ALIASES,
        canonical_adapter_kind,
        wire_adapter_kind,
    )

    assert canonical_adapter_kind("codex_cli") == "codex"
    assert canonical_adapter_kind("claude_code") == "claude"
    assert canonical_adapter_kind("fake") == "fake"
    assert canonical_adapter_kind("codex") == "codex"
    assert canonical_adapter_kind("codex_cli_v2") == "codex_cli_v2"
    assert canonical_adapter_kind(None) is None
    assert wire_adapter_kind("codex") == "codex_cli"
    assert wire_adapter_kind("claude") == "claude_code"
    assert wire_adapter_kind("fake") == "fake"
    assert set(WIRE_ADAPTER_ALIASES) == {"codex_cli", "claude_code"}
    assert set(HOST_ADAPTER_WIRE_NAMES) == {"codex", "claude"}


def test_registry_resolves_a_profile_registered_under_the_wire_name() -> None:
    """M24's acceptance harness registers its fake profile under ``codex_cli``."""
    from sidecars.workspace_agent.sessions.supervisor import FakeProfile, ProfileRegistry

    # ``sidecars.*`` is an untyped boundary for mypy (pyproject override), so the
    # FakeProfile base is ``Any``; the subclass only binds the adapter kind.
    class _WireNamed(FakeProfile):  # type: ignore[misc]
        adapter_kind = "codex_cli"

    registry = ProfileRegistry((_WireNamed(),))
    assert registry.get("codex").adapter_kind == "codex_cli"
    assert registry.get("codex_cli").adapter_kind == "codex_cli"
    with pytest.raises(sessions_supervisor.CapabilityUnavailableError):
        registry.get("codex_cli_v2")


@pytest.mark.parametrize(
    "form",
    [
        "raw",
        "hex",
        "hex_upper",
        "b64",
        "b64_unpadded",
        "b64url",
        "b64url_unpadded",
        "fragment_raw",
        "fragment_hex",
        "fragment_b64",
    ],
)
def test_collect_refuses_a_note_containing_the_secret(session_base: Path, form: str) -> None:
    secret = URLSAFE_SECRET
    forms = _encoded_forms(secret)
    payload = forms[form]
    note = b"# synthetic note\n" + payload + b"\n"
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = _prepare(client, running, workspace_id, secret=secret)
        assert prepared.status_code == 200, prepared.text
        # The fake profile arms the relay and the one-shot secret at prepare.
        ipc = running.session_dir() / "ipc"
        assert (ipc / "broker.sock").is_socket()
        assert (ipc / "shim.sock").is_socket()

        _drop_note(running, "leak.bin", note)
        collected = _collect(client, running)
        assert collected.status_code == 403, collected.text
        body = collected.json()
        assert body["error"] == "policy_denied"
        assert body["details"]["reason"] == "secret_exposure_detected"
        assert body["details"]["operation"] == "collect"

        status = _status(client, running)
        assert status["state"] == "failed"
        # Revocation destroys both listeners and the armed secret socket.
        assert not (ipc / "broker.sock").exists()
        assert not (ipc / "shim.sock").exists()

        assert workspace_files_containing(running, payload) == []
        assert not (running.host_path / "notes" / "agents" / TASK_ID / "leak.bin").exists()

        events = session_events(client, running)
        revoked = [
            event
            for event in events
            if event["kind"] == "state_changed"
            and event["payload"].get("reason") == "secret_exposure_detected"
        ]
        assert revoked, events
        assert revoked[-1]["payload"]["to"] == "failed"
        assert revoked[-1]["payload"]["operation"] == "collect"
        # No event carries the offending content in any tested form.
        serialized = json.dumps(events)
        for encoded in forms.values():
            assert encoded.decode("latin-1") not in serialized


def test_collect_refuses_the_secret_in_the_result_file(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        result = running.session_dir() / "scratch" / "result.md"
        result.write_bytes(b"result body\n" + _std_b64(SECRET) + b"\n")
        collected = _collect(client, running)
        assert collected.status_code == 403, collected.text
        assert collected.json()["details"]["reason"] == "secret_exposure_detected"
        assert _status(client, running)["state"] == "failed"
        assert not (running.host_path / "results" / "sessions" / SESSION_ID / "result.md").exists()


def test_collect_copies_a_legitimate_near_miss_note(session_base: Path) -> None:
    # Flipping bytes 0 and 16 breaks every 16-byte aligned window, so the file
    # is close to the secret but shares no detectable fragment with it.
    altered = bytearray(SECRET)
    altered[0] ^= 0xFF
    altered[16] ^= 0xFF
    content = b"# synthetic near miss\n" + bytes(altered).hex().encode("ascii") + b"\n"
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        _drop_note(running, "near-miss.md", content)
        collected = _collect(client, running)
        assert collected.status_code == 200, collected.text
        body = collected.json()
        destination = f"notes/agents/{TASK_ID}/near-miss.md"
        assert destination in body["captured_notes"]
        captured = running.host_path / "notes" / "agents" / TASK_ID / "near-miss.md"
        assert captured.read_bytes() == content
        assert _status(client, running)["state"] == "ready"
        assert workspace_files_containing(running, SECRET) == []


def test_a_failed_capture_does_not_copy_the_other_notes(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        _drop_note(running, "a-clean.md", b"# clean note\n")
        _drop_note(running, "b-leak.md", b"leak\n" + SECRET.hex().encode("ascii") + b"\n")
        collected = _collect(client, running)
        assert collected.status_code == 403, collected.text
        assert not (running.host_path / "notes" / "agents" / TASK_ID / "a-clean.md").exists()
        assert workspace_files_containing(running, SECRET.hex().encode("ascii")) == []


def test_legitimate_capture_is_unchanged(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id).status_code == 200
        _drop_note(running, "plain.md", b"# plain note\n")
        collected = _collect(client, running)
        assert collected.status_code == 200, collected.text
        assert f"notes/agents/{TASK_ID}/plain.md" in collected.json()["captured_notes"]


def test_cli_logs_never_persist_the_raw_secret(session_base: Path) -> None:
    """F2 defense in depth: a printable secret in a CLI log line is redacted."""
    printable = b"ABCDEFGHIJKLMNOPQRSTUVWX01234567"
    assert len(printable) == 32
    script = fake_script(
        [
            {"op": "emit", "event": {"type": printable.decode("ascii")}},
            {"op": "exit", "code": 0},
        ]
    )
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        assert _prepare(client, running, workspace_id, secret=printable).status_code == 200
        write_fake_script(running, script)
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        # A clean run end is the D2 instruction receive point (ID-4).
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")
        lines, _cursor = log_lines(client, running)
        text = "\n".join(str(line["text"]) for line in lines)
        assert printable.decode("ascii") not in text
        assert "<secret-redacted>" in text


def test_secret_forms_never_match_random_content() -> None:
    """A sanity bound: the derived forms are specific, not short byte strings."""
    forms = sessions_supervisor._secret_content_forms(SECRET)
    assert forms
    assert all(len(form) >= 16 for form in forms)
    random_bytes = hashlib.sha256(b"unrelated content").digest()
    assert not any(form in random_bytes for form in forms)
