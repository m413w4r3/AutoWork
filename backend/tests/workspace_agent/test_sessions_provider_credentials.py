"""M24.1 R2B-2 oracles: collect refuses seeded provider credentials.

The CLI profiles seed the operator's provider credential into the session
runtime.  An agent can copy those values into ``scratch/notes`` (Codex runs its
own shell; for Claude it is INFERRED but the copy is new).  ``collect`` must
refuse to persist any file that carries a token value, its usual encodings or
an aligned fragment, must refuse credential file names outright, and must never
echo the values in an event, log or state file.  The forbidden forms are derived
in host memory from the copies themselves and are never persisted.

Every test is offline and rootless: a real agent serves the HTTP API on a
tmpdir AF_UNIX socket and the ``fake`` profile is the only one registered.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from tests.workspace_agent.sessions_support import (
    COLLECT_KEY,
    INTERRUPT_KEY,
    PREPARE_KEY,
    SESSION_ID,
    START_KEY,
    TASK_ID,
    RunningSessionAgent,
    get_session,
    hang_script,
    post_session,
    prepare_payload,
    running_session_agent,
    session_events,
    wait_for_state,
    workspace_files_containing,
    write_fake_script,
)

CLAUDE_CREDENTIAL = Path("config/home/.claude/.credentials.json")
CODEX_AUTH = Path("config/codex/auth.json")

# Synthetic values only; none of these is a real provider token.
# The Claude access value carries a backslash and a quote so the JSON-escaped
# form differs from the raw bytes and is derivable in the credential file.
CLAUDE_ACCESS_TOKEN = 'synthetic\\claude"access-token-0123456789abcdef'
CLAUDE_REFRESH_TOKEN = "synthetic-claude-refresh-token-0123456789abcdef"
CODEX_ACCESS_TOKEN = "synthetic-codex-access-token-0123456789abcdef"
CODEX_REFRESH_TOKEN = "synthetic-codex-refresh-token-0123456789abcdef"
CODEX_ID_TOKEN = "synthetic-codex-id-token-0123456789abcdef"


def _claude_credential_bytes() -> bytes:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": CLAUDE_ACCESS_TOKEN,
                "refreshToken": CLAUDE_REFRESH_TOKEN,
                "expiresAt": 1893456000000,
            }
        }
    ).encode("utf-8")


def _codex_auth_bytes() -> bytes:
    return json.dumps(
        {
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": CODEX_ACCESS_TOKEN,
                "refresh_token": CODEX_REFRESH_TOKEN,
                "id_token": CODEX_ID_TOKEN,
            },
        }
    ).encode("utf-8")


def _plant_credentials(running: RunningSessionAgent) -> tuple[Path, Path]:
    session = running.session_dir()
    claude = session / CLAUDE_CREDENTIAL
    claude.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    claude.write_bytes(_claude_credential_bytes())
    codex = session / CODEX_AUTH
    codex.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    codex.write_bytes(_codex_auth_bytes())
    for path in (claude, codex):
        path.chmod(0o600)
    return claude, codex


def _prepare(
    client: httpx.Client, running: RunningSessionAgent, workspace_id: str = ""
) -> tuple[str, httpx.Response]:
    workspace_id = workspace_id or running.ensure_workspace(client)
    return workspace_id, post_session(
        client,
        running,
        "/v1/sessions",
        prepare_payload(workspace_id),
        PREPARE_KEY,
    )


def _drop_note(running: RunningSessionAgent, name: str, data: bytes) -> Path:
    notes = running.session_dir() / "scratch" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    path = notes / name
    path.write_bytes(data)
    return path


def _collect(client: httpx.Client, running: RunningSessionAgent) -> httpx.Response:
    return post_session(
        client,
        running,
        f"/v1/sessions/{SESSION_ID}/collect",
        {"capture": True},
        COLLECT_KEY,
    )


def _status(client: httpx.Client, running: RunningSessionAgent) -> dict[str, object]:
    response = get_session(client, running, f"/v1/sessions/{SESSION_ID}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return {str(key): value for key, value in body.items()}


def _persisted_runtime_text(running: RunningSessionAgent) -> str:
    """The state, events and logs only; the planted copies are not audit files."""
    session = running.session_dir()
    chunks: list[str] = []
    for name in ("state.json", "events.jsonl", "logs.jsonl"):
        path = session / name
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
    return "\n".join(chunks)


def _refused(client: httpx.Client, running: RunningSessionAgent) -> dict[str, object]:
    collected = _collect(client, running)
    assert collected.status_code == 403, collected.text
    body = collected.json()
    assert body["error"] == "policy_denied"
    assert body["details"]["reason"] == "secret_exposure_detected"
    assert body["details"]["operation"] == "collect"
    assert _status(client, running)["state"] == "failed"
    return {str(key): value for key, value in body.items()}


def _token_forms() -> dict[str, bytes]:
    fragment = CLAUDE_REFRESH_TOKEN.encode("ascii")[8:28]
    return {
        "claude_access_token": CLAUDE_ACCESS_TOKEN.encode("utf-8"),
        "claude_refresh_token": CLAUDE_REFRESH_TOKEN.encode("ascii"),
        "codex_refresh_token": CODEX_REFRESH_TOKEN.encode("ascii"),
        "codex_id_token": CODEX_ID_TOKEN.encode("ascii"),
        "base64": base64.b64encode(CLAUDE_ACCESS_TOKEN.encode("utf-8")),
        "json_escaped": json.dumps(CLAUDE_ACCESS_TOKEN)[1:-1].encode("utf-8"),
        "fragment": fragment,
        "fragment_hex": fragment.hex().encode("ascii"),
    }


@pytest.mark.parametrize("form", sorted(_token_forms()))
def test_collect_refuses_a_note_carrying_a_seeded_provider_credential(
    session_base: Path, form: str
) -> None:
    payload = _token_forms()[form]
    note = b"# synthetic note\n" + payload + b"\n"
    with running_session_agent(session_base) as running, running.client() as client:
        _, prepared = _prepare(client, running)
        assert prepared.status_code == 200, prepared.text
        _plant_credentials(running)
        _drop_note(running, "leak.md", note)

        _refused(client, running)

        assert workspace_files_containing(running, payload) == []
        assert not (running.host_path / "notes" / "agents" / TASK_ID / "leak.md").exists()
        runtime = _persisted_runtime_text(running)
        for needle in (CLAUDE_ACCESS_TOKEN, CLAUDE_REFRESH_TOKEN, CODEX_REFRESH_TOKEN):
            assert needle not in runtime
        events = session_events(client, running)
        assert any(
            event["kind"] == "state_changed"
            and event["payload"].get("reason") == "secret_exposure_detected"
            for event in events
        )


@pytest.mark.parametrize("name", ["auth.json", ".credentials.json", ".credentials"])
def test_collect_refuses_credential_file_names(session_base: Path, name: str) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        _, prepared = _prepare(client, running)
        assert prepared.status_code == 200, prepared.text
        _drop_note(running, name, b"# innocuous content\n")

        _refused(client, running)

        assert not (running.host_path / "notes" / "agents" / TASK_ID / name).exists()


def test_collect_still_captures_a_clean_note_when_credentials_are_seeded(
    session_base: Path,
) -> None:
    content = b"# clean note\n\nsynthetic facts only\n"
    with running_session_agent(session_base) as running, running.client() as client:
        _, prepared = _prepare(client, running)
        assert prepared.status_code == 200, prepared.text
        _plant_credentials(running)
        _drop_note(running, "clean.md", content)

        collected = _collect(client, running)
        assert collected.status_code == 200, collected.text
        destination = f"notes/agents/{TASK_ID}/clean.md"
        assert destination in collected.json()["captured_notes"]
        assert (running.host_path / "notes" / "agents" / TASK_ID / "clean.md").read_bytes() == (
            content
        )
        assert _status(client, running)["state"] == "ready"


def test_collect_refuses_a_token_after_the_interrupt_removed_the_copy(
    session_base: Path,
) -> None:
    """The in-memory forms survive the copy's own removal (R2B-1 + R2B-2)."""
    with running_session_agent(session_base) as running, running.client() as client:
        _, prepared = _prepare(client, running)
        assert prepared.status_code == 200, prepared.text
        write_fake_script(running, hang_script())
        claude, _codex = _plant_credentials(running)
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        wait_for_state(client, running, "running")

        interrupted = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/interrupt",
            {"reason": "r2b-2 oracle"},
            INTERRUPT_KEY,
        )
        assert interrupted.status_code == 200, interrupted.text
        assert not claude.exists()  # the copy was destroyed at interrupt
        _drop_note(running, "late-leak.md", CLAUDE_ACCESS_TOKEN.encode("ascii"))

        _refused(client, running)


def test_collect_fails_closed_when_a_credential_copy_cannot_be_read(
    session_base: Path,
) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        _, prepared = _prepare(client, running)
        assert prepared.status_code == 200, prepared.text
        claude, _codex = _plant_credentials(running)
        _drop_note(running, "clean.md", b"# clean note\n")
        claude.chmod(0o000)
        try:
            collected = _collect(client, running)
        finally:
            claude.chmod(0o600)
        assert collected.status_code == 500, collected.text
        body = collected.json()
        assert body["error"] == "internal_error"
        # Nothing was persisted and the agent content was not echoed.
        assert not (running.host_path / "notes" / "agents" / TASK_ID / "clean.md").exists()
        assert CLAUDE_ACCESS_TOKEN not in json.dumps(body)
