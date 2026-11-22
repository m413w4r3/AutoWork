"""CAS conflicts, conflict copies and concurrent writers (design §7, ticket W4).

A conflict never touches the live file: the proposed bytes land next to it in a
deterministic ``.conflict-<sha>`` copy, the response is 409 ``revision_conflict``
and the conflict is remembered so a replay is byte-identical and file-stable.
"""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from tests.workspace_agent.test_files import (
    AgentHandle,
    ensure_workspace,
    events_for,
    expected_revision,
    put_file,
    sha256_of,
    workspace_tree,
)
from tests.workspace_agent.w3_support import scan_via_http

_BASE_URL = "http://workspace-agent"
NOTE_PATH = "notes/humaines/contrat.md"


def read_sync_state(running: AgentHandle) -> dict[str, object]:
    payload = json.loads((workspace_tree(running) / ".autowork" / "sync-state.json").read_text())
    assert isinstance(payload, dict)
    return payload


def conflict_files(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if ".conflict-" in name)


def test_a_cas_conflict_keeps_both_versions_and_never_touches_the_live_file(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    original = b"version A"
    proposed = b"version B"
    created = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        original,
        key="m24-w4-cfl-0001",
    )
    assert created.status_code == 201
    live = workspace_tree(scanning_agent) / NOTE_PATH
    before = os.lstat(live)

    response = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        proposed,
        key="m24-w4-cfl-0002",
        expected="new",
    )

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "revision_conflict"
    assert body["retryable"] is False
    details = body["details"]
    assert details["current"] == sha256_of(original)
    assert details["proposed_sha256"] == sha256_of(proposed)
    sha8 = sha256_of(proposed)[:8]
    expected_path = f"notes/humaines/contrat.conflict-{sha8}.md"
    assert details["conflict_path"] == expected_path
    assert isinstance(details["conflict_id"], str) and details["conflict_id"].startswith("cfl-")

    after = os.lstat(live)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert live.read_bytes() == original
    copy = workspace_tree(scanning_agent) / expected_path
    assert copy.read_bytes() == proposed
    assert stat.S_IMODE(os.lstat(copy).st_mode) == 0o640

    conflict_events = [
        event
        for event in events_for(scanning_client, scanning_agent, workspace_id, NOTE_PATH)
        if event["kind"] == "conflict_created"
    ]
    assert len(conflict_events) == 1
    assert conflict_events[0]["sha256"] == sha256_of(proposed)
    assert conflict_events[0]["previous_revision_id"] == expected_revision(
        workspace_id, NOTE_PATH, original
    )
    assert conflict_events[0]["origin"] == "ui"

    stored = read_sync_state(scanning_agent)["conflicts"]
    assert isinstance(stored, list)
    entries = [entry for entry in stored if entry.get("conflict_id") == details["conflict_id"]]
    assert len(entries) == 1
    assert entries[0]["path"] == NOTE_PATH
    assert entries[0]["conflict_path"] == expected_path
    assert entries[0]["current_sha256"] == sha256_of(original)
    assert entries[0]["proposed_sha256"] == sha256_of(proposed)


def test_replaying_the_conflict_key_returns_the_same_conflict_and_file(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    put_file(scanning_client, scanning_agent, workspace_id, NOTE_PATH, b"A", key="m24-w4-cfl-0100")
    first = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        b"B",
        key="m24-w4-cfl-0101",
        expected="new",
    )
    assert first.status_code == 409
    directory = workspace_tree(scanning_agent) / "notes" / "humaines"
    copies = conflict_files(directory)

    replay = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        b"B",
        key="m24-w4-cfl-0101",
        expected="new",
    )

    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert conflict_files(directory) == copies
    conflict_events = [
        event
        for event in events_for(scanning_client, scanning_agent, workspace_id, NOTE_PATH)
        if event["kind"] == "conflict_created"
    ]
    assert len(conflict_events) == 1


def test_a_new_key_reuses_an_existing_conflict_copy_with_the_same_content(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    put_file(scanning_client, scanning_agent, workspace_id, NOTE_PATH, b"A", key="m24-w4-cfl-0200")
    first = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        b"B",
        key="m24-w4-cfl-0201",
        expected="new",
    )
    assert first.status_code == 409

    second = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        b"B",
        key="m24-w4-cfl-0202",
        expected="new",
    )

    assert second.status_code == 409
    assert second.json()["details"]["conflict_path"] == first.json()["details"]["conflict_path"]
    assert second.json()["details"]["conflict_id"] == first.json()["details"]["conflict_id"]
    assert len(conflict_files(workspace_tree(scanning_agent) / "notes" / "humaines")) == 1
    conflict_events = [
        event
        for event in events_for(scanning_client, scanning_agent, workspace_id, NOTE_PATH)
        if event["kind"] == "conflict_created"
    ]
    assert len(conflict_events) == 1


def test_an_8_hex_name_holding_other_content_forces_the_16_hex_name(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    put_file(scanning_client, scanning_agent, workspace_id, NOTE_PATH, b"A", key="m24-w4-cfl-0300")
    proposed = b"B with a collision"
    digest = sha256_of(proposed)
    occupied = (
        workspace_tree(scanning_agent) / "notes" / "humaines" / f"contrat.conflict-{digest[:8]}.md"
    )
    occupied.write_bytes(b"someone else's bytes")
    os.chmod(occupied, 0o640)

    response = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        proposed,
        key="m24-w4-cfl-0301",
        expected="new",
    )

    assert response.status_code == 409, response.text
    conflict_path = response.json()["details"]["conflict_path"]
    assert conflict_path == f"notes/humaines/contrat.conflict-{digest[:16]}.md"
    assert occupied.read_bytes() == b"someone else's bytes"
    copy = workspace_tree(scanning_agent) / conflict_path
    assert copy.read_bytes() == proposed


def test_a_body_equal_to_the_current_content_is_already_present_not_a_conflict(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    put_file(scanning_client, scanning_agent, workspace_id, NOTE_PATH, b"A", key="m24-w4-cfl-0400")
    current = workspace_tree(scanning_agent) / NOTE_PATH
    current.write_bytes(b"human version")
    os.chmod(current, 0o640)

    response = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        b"human version",
        key="m24-w4-cfl-0401",
        expected=sha256_of(b"A"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["already_present"] is True
    assert conflict_files(workspace_tree(scanning_agent) / "notes" / "humaines") == []
    assert current.read_bytes() == b"human version"


def test_a_human_edit_seen_by_the_scan_makes_a_stale_ui_put_a_conflict(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    original = b"ui version"
    human = b"human edited version"
    proposed = b"ui retry with stale expected"
    put_file(
        scanning_client, scanning_agent, workspace_id, NOTE_PATH, original, key="m24-w4-cfl-0500"
    )
    live = workspace_tree(scanning_agent) / NOTE_PATH
    live.write_bytes(human)
    os.chmod(live, 0o640)
    scan_via_http(scanning_client, scanning_agent, workspace_id)  # type: ignore[arg-type]

    response = put_file(
        scanning_client,
        scanning_agent,
        workspace_id,
        NOTE_PATH,
        proposed,
        key="m24-w4-cfl-0501",
        expected=sha256_of(original),
    )

    assert response.status_code == 409, response.text
    details = response.json()["details"]
    assert details["current"] == sha256_of(human)
    assert details["proposed_sha256"] == sha256_of(proposed)
    assert live.read_bytes() == human
    copy = workspace_tree(scanning_agent) / details["conflict_path"]
    assert copy.read_bytes() == proposed


def _concurrent_put(
    socket_path: Path,
    token: str,
    workspace_id: str,
    content: bytes,
    key: str,
    expected: str,
) -> httpx.Response:
    transport = httpx.HTTPTransport(uds=str(socket_path), retries=0)
    with httpx.Client(
        base_url=_BASE_URL, transport=transport, timeout=30.0, trust_env=False
    ) as client:
        return client.put(
            f"/v1/workspaces/{workspace_id}/files",
            params={"path": NOTE_PATH},
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": key,
                "X-Autowork-Expected-Sha256": expected,
                "X-Autowork-Origin": "ui",
            },
            content=content,
        )


def test_concurrent_puts_to_the_same_path_never_lose_a_version(
    scanning_client: httpx.Client, scanning_agent: AgentHandle
) -> None:
    workspace_id = ensure_workspace(scanning_client, scanning_agent)
    original = b"base version"
    put_file(
        scanning_client, scanning_agent, workspace_id, NOTE_PATH, original, key="m24-w4-cfl-0600"
    )
    live = workspace_tree(scanning_agent) / NOTE_PATH
    before = os.lstat(live)
    first, second = b"writer one", b"writer two"
    expected = sha256_of(original)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _concurrent_put,
                scanning_agent.socket_path,
                scanning_agent.token,
                workspace_id,
                content,
                f"m24-w4-cfl-060{index}",
                expected,
            )
            for index, content in ((1, first), (2, second))
        ]
        responses = [future.result() for future in futures]

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [200, 409], [response.text for response in responses]
    after = os.lstat(live)
    assert after.st_ino != before.st_ino
    winner = live.read_bytes()
    assert winner in {first, second}
    loser = second if winner == first else first
    copies = conflict_files(live.parent)
    assert len(copies) == 1, copies
    assert (live.parent / copies[0]).read_bytes() == loser
