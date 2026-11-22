"""Deterministic ids, the bounded event log, cursors and the read routes (§6)."""

from __future__ import annotations

import hashlib

import pytest
from sidecars.workspace_agent import events, index
from sidecars.workspace_agent.events import EventKind, EventLog, event_id_for, revision_id_for
from sidecars.workspace_agent.protocol import ErrorCode
from sidecars.workspace_agent.scan import CursorExpiredError

from tests.workspace_agent.w3_support import (
    AUTH_IDEMPOTENCY_KEY,
    RunningScanAgent,
    ScanEnvironment,
    ensure_workspace_via_http,
    json_object,
    scan_via_http,
)

OBSERVED_AT = "2026-01-01T00:00:00Z"
WORKSPACE_ID = "ws-" + "a" * 32


def append_test_event(sync: events.SyncState, path: str) -> events.FileEvent:
    event_id = event_id_for(sync.workspace_id, path, None, None, EventKind.FILE_CREATED)
    return sync.log.append(
        event_id=event_id,
        kind=EventKind.FILE_CREATED,
        path=path,
        observed_at=OBSERVED_AT,
        policy_class=index.POLICY_HUMAN_EDITABLE,
    )


def test_revision_and_event_ids_are_deterministic_pure_functions() -> None:
    path = "notes/humaines/note.md"
    content_sha = "b" * 64
    expected_revision = (
        "rev-"
        + hashlib.sha256(f"{WORKSPACE_ID}\x00{path}\x00{content_sha}".encode()).hexdigest()[:32]
    )
    assert revision_id_for(WORKSPACE_ID, path, content_sha) == expected_revision
    assert revision_id_for(WORKSPACE_ID, path, content_sha) == expected_revision
    assert revision_id_for(WORKSPACE_ID, path, "c" * 64) != expected_revision

    components = [WORKSPACE_ID, path, "null", expected_revision, "file_created"]
    expected_event = "evt-" + hashlib.sha256("|".join(components).encode()).hexdigest()[:32]
    assert (
        event_id_for(WORKSPACE_ID, path, None, expected_revision, EventKind.FILE_CREATED)
        == expected_event
    )
    assert (
        event_id_for(WORKSPACE_ID, path, None, expected_revision, EventKind.FILE_MODIFIED)
        != expected_event
    )


def append_test_event_to_log(log: EventLog, path: str) -> events.FileEvent:
    event_id = event_id_for(WORKSPACE_ID, path, None, None, EventKind.FILE_CREATED)
    return log.append(
        event_id=event_id,
        kind=EventKind.FILE_CREATED,
        path=path,
        observed_at=OBSERVED_AT,
        policy_class=index.POLICY_HUMAN_EDITABLE,
    )


def test_event_log_ring_expires_old_cursors_and_never_drops_the_tail() -> None:
    log = EventLog(7, limit=3)
    for position in range(5):
        append_test_event_to_log(log, f"p/{position}")
    assert log.generation == 7
    assert log.last_seq == 5
    assert log.oldest_retained == 3
    assert [event.seq for event in log.retained] == [3, 4, 5]

    expired = log.page(0, 10)
    assert expired.resync_required
    assert expired.events == ()

    valid = log.page(2, 10)
    assert not valid.resync_required
    assert [event.seq for event in valid.events] == [3, 4, 5]
    assert valid.next_cursor == 5

    bounded = log.page(2, 2)
    assert [event.seq for event in bounded.events] == [3, 4]
    assert bounded.next_cursor == 4

    empty = log.page(5, 10)
    assert empty.events == ()
    assert empty.next_cursor == 5
    assert not empty.resync_required
    assert log.page(6, 10).resync_required


def test_sync_state_round_trip_is_byte_identical() -> None:
    sync = events.SyncState.fresh(WORKSPACE_ID, 77)
    append_test_event(sync, "notes/humaines/a.md")
    append_test_event(sync, "notes/humaines/b.md")
    sync.policy_violations = [
        {
            "path": "plans/plan.md",
            "policy_class": index.POLICY_GENERATED_IMMUTABLE,
            "reason": "executable_bit",
            "signature": "0740:3:1700000000000000000",
            "observed_at": OBSERVED_AT,
        }
    ]
    payload = sync.to_payload()

    restored = events.SyncState.from_payload(payload, workspace_id=WORKSPACE_ID, limit=10_000)

    assert restored.to_payload() == payload
    assert restored.log.last_seq == 2
    assert restored.policy_violations == sync.policy_violations


def test_sync_state_rejects_corrupt_projections() -> None:
    sync = events.SyncState.fresh(WORKSPACE_ID, 5)
    append_test_event(sync, "p/a")
    append_test_event(sync, "p/b")
    good = sync.to_payload()
    good_events = [event.to_wire() for event in sync.log.retained]

    missing_events: dict[str, object] = dict(good)
    del missing_events["events"]
    wrong_schema: dict[str, object] = dict(good)
    wrong_schema["schema"] = "autowork-workspace-sync.v0"
    foreign_workspace: dict[str, object] = dict(good)
    foreign_workspace["workspace_id"] = "ws-" + "b" * 32
    bad_last_seq: dict[str, object] = dict(good)
    bad_last_seq["last_seq"] = 1
    bad_oldest_seq: dict[str, object] = dict(good)
    bad_oldest_seq["oldest_seq"] = 2
    reordered: dict[str, object] = dict(good)
    reordered["events"] = list(reversed(good_events))
    bad_events = [dict(entry) for entry in good_events]
    bad_events[0]["kind"] = "file_exploded"
    bad_kind: dict[str, object] = dict(good)
    bad_kind["events"] = bad_events

    for payload in (
        missing_events,
        wrong_schema,
        foreign_workspace,
        bad_last_seq,
        bad_oldest_seq,
        reordered,
        bad_kind,
    ):
        with pytest.raises(events.ProjectionError):
            events.SyncState.from_payload(payload, workspace_id=WORKSPACE_ID)


def test_cursor_older_than_the_retained_ring_requires_a_snapshot(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service(retained_event_limit=2)
    for name in ("a", "b", "c"):
        environment.write(f"notes/humaines/{name}.md", name.encode())
    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)
    assert result.emitted == 3

    with pytest.raises(CursorExpiredError) as expired:
        service.events(environment.workspace_id, after=0)
    assert expired.value.code is ErrorCode.CURSOR_EXPIRED
    assert expired.value.status == 409
    assert expired.value.details["resync_required"] is True

    page = service.events(environment.workspace_id, after=1)
    assert [event.seq for event in page.events] == [2, 3]
    assert page.oldest_retained == 2
    assert page.next_cursor == 3
    assert service.events(environment.workspace_id, after=3).events == ()


def test_generation_mismatch_requires_a_snapshot(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    environment.write("notes/humaines/a.md", b"alpha")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)

    with pytest.raises(CursorExpiredError):
        service.events(environment.workspace_id, after=0, generation=1)

    current = service.events(environment.workspace_id, after=0)
    page = service.events(environment.workspace_id, after=0, generation=current.generation)
    assert [event.seq for event in page.events] == [1]


def test_scan_and_read_routes_serve_the_stable_index(
    scanning_client, scanning_agent: RunningScanAgent
) -> None:
    client = scanning_client
    workspace_id = ensure_workspace_via_http(client, scanning_agent)
    scanning_agent.write("notes/humaines/b.md", b"beta")
    scanning_agent.write("notes/humaines/a.md", b"alpha")

    scan_via_http(client, scanning_agent, workspace_id)
    headers = scanning_agent.auth_headers

    snapshot = json_object(client.get(f"/v1/workspaces/{workspace_id}/snapshot", headers=headers))
    assert isinstance(snapshot["generation"], int)
    assert [state["path"] for state in snapshot["files"]] == [
        "notes/humaines/a.md",
        "notes/humaines/b.md",
    ]
    first = snapshot["files"][0]
    assert first["origin"] == "filesystem"
    assert first["mode"] == "0640"
    assert first["policy_class"] == index.POLICY_HUMAN_EDITABLE
    assert first["sha256"] == hashlib.sha256(b"alpha").hexdigest()
    assert first["task_id"] is None

    events_page = json_object(client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers))
    assert [event["kind"] for event in events_page["events"]] == [
        "file_created",
        "file_created",
    ]
    assert events_page["oldest_retained"] == 1
    assert events_page["resync_required"] is False
    cursor = events_page["next_cursor"]
    assert isinstance(cursor, int)
    drained = json_object(
        client.get(f"/v1/workspaces/{workspace_id}/events?after={cursor}", headers=headers)
    )
    assert drained["events"] == []
    assert drained["next_cursor"] == cursor

    listed = json_object(
        client.get(f"/v1/workspaces/{workspace_id}/files?prefix=notes/humaines/", headers=headers)
    )
    assert [state["path"] for state in listed["files"]] == [
        "notes/humaines/a.md",
        "notes/humaines/b.md",
    ]
    empty_list = json_object(
        client.get(f"/v1/workspaces/{workspace_id}/files?prefix=scripts/", headers=headers)
    )
    assert empty_list["files"] == []

    full = scan_via_http(client, scanning_agent, workspace_id, full=True)
    assert full["emitted"] == 0
    unchanged = json_object(client.get(f"/v1/workspaces/{workspace_id}/events", headers=headers))
    assert len(unchanged["events"]) == 2


def test_snapshot_pages_use_opaque_tokens(
    scanning_client, scanning_agent: RunningScanAgent
) -> None:
    client = scanning_client
    workspace_id = ensure_workspace_via_http(client, scanning_agent)
    for name in ("a", "b", "c"):
        scanning_agent.write(f"notes/humaines/{name}.md", name.encode())
    scan_via_http(client, scanning_agent, workspace_id)
    base = f"/v1/workspaces/{workspace_id}/snapshot?limit=2"
    headers = scanning_agent.auth_headers

    first_page = json_object(client.get(base, headers=headers))
    assert len(first_page["files"]) == 2
    token = first_page["next_page_token"]
    assert isinstance(token, str)
    second_page = json_object(client.get(f"{base}&page_token={token}", headers=headers))
    assert len(second_page["files"]) == 1
    assert second_page["next_page_token"] is None
    first_files = first_page["files"]
    second_files = second_page["files"]
    assert isinstance(first_files, list) and isinstance(second_files, list)
    paths = [state["path"] for state in [*first_files, *second_files]]
    assert paths == ["notes/humaines/a.md", "notes/humaines/b.md", "notes/humaines/c.md"]


def test_event_routes_reject_a_foreign_generation(
    scanning_client, scanning_agent: RunningScanAgent
) -> None:
    client = scanning_client
    workspace_id = ensure_workspace_via_http(client, scanning_agent)
    response = client.get(
        f"/v1/workspaces/{workspace_id}/events?after=0&generation=1",
        headers=scanning_agent.auth_headers,
    )
    assert response.status_code == 409
    payload = json_object(response)
    assert payload["error"] == "cursor_expired"
    assert payload["retryable"] is False
    details = payload["details"]
    assert isinstance(details, dict)
    assert details["resync_required"] is True


def test_scan_route_validates_its_request(
    scanning_client, scanning_agent: RunningScanAgent
) -> None:
    client = scanning_client
    workspace_id = ensure_workspace_via_http(client, scanning_agent)
    url = f"/v1/workspaces/{workspace_id}/scan"
    headers = scanning_agent.auth_headers

    wrong_type = client.post(
        url,
        json={"full": "yes"},
        headers={**headers, "Idempotency-Key": "w3-scan-bad-1"},
    )
    assert wrong_type.status_code == 400
    assert json_object(wrong_type)["error"] == "invalid_request"

    unknown_field = client.post(
        url,
        json={"unknown": True},
        headers={**headers, "Idempotency-Key": "w3-scan-bad-2"},
    )
    assert unknown_field.status_code == 400

    missing_key = client.post(url, json={"full": False}, headers=headers)
    assert missing_key.status_code == 400
    assert "Idempotency-Key" in missing_key.text


def test_query_parameters_are_validated(scanning_client, scanning_agent: RunningScanAgent) -> None:
    client = scanning_client
    workspace_id = ensure_workspace_via_http(client, scanning_agent)
    headers = scanning_agent.auth_headers

    bad_after = client.get(f"/v1/workspaces/{workspace_id}/events?after=abc", headers=headers)
    assert bad_after.status_code == 400
    bad_limit = client.get(f"/v1/workspaces/{workspace_id}/snapshot?limit=0", headers=headers)
    assert bad_limit.status_code == 400
    unknown = client.get(f"/v1/workspaces/{workspace_id}/files?bogus=1", headers=headers)
    assert unknown.status_code == 400
    bad_token = client.get(f"/v1/workspaces/{workspace_id}/snapshot?page_token=zz", headers=headers)
    assert bad_token.status_code == 400


def test_unknown_workspace_is_not_found(scanning_client, scanning_agent: RunningScanAgent) -> None:
    client = scanning_client
    missing = "ws-" + "f" * 32
    headers = scanning_agent.auth_headers
    for suffix in ("snapshot", "events", "files"):
        response = client.get(f"/v1/workspaces/{missing}/{suffix}", headers=headers)
        assert response.status_code == 404
        assert json_object(response)["error"] == "workspace_not_found"
    scan = client.post(
        f"/v1/workspaces/{missing}/scan",
        json={"full": False},
        headers={**headers, "Idempotency-Key": AUTH_IDEMPOTENCY_KEY},
    )
    assert scan.status_code == 404
