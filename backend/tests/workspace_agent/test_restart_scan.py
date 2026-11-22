"""Restart scan: projection reload, deterministic ids and safe rebuilds (§6)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sidecars.workspace_agent import events, index
from sidecars.workspace_agent.events import EventKind
from sidecars.workspace_agent.scan import WorkspaceScanService

from tests.workspace_agent.w3_support import ScanEnvironment


def all_events(
    service: WorkspaceScanService, environment: ScanEnvironment
) -> list[events.FileEvent]:
    return list(service.events(environment.workspace_id, limit=500).events)


def projection_path(environment: ScanEnvironment, name: str) -> Path:
    return environment.host_path / ".autowork" / name


def delete_projections(environment: ScanEnvironment) -> None:
    for name in (index.FILE_INDEX_FILE_NAME, events.SYNC_STATE_FILE_NAME):
        projection_path(environment, name).unlink()


def test_restart_reuses_generation_revision_ids_and_events(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    environment.write("notes/humaines/a.md", b"alpha")
    environment.write("notes/humaines/b.md", b"beta")
    service.scan(environment.workspace_id)
    assert environment.stable_scan(service).emitted == 2
    snapshot_before = service.snapshot(environment.workspace_id)
    events_before = all_events(service, environment)
    assert [event.seq for event in events_before] == [1, 2]

    restarted = environment.service()
    result = restarted.scan(environment.workspace_id)

    assert result.emitted == 0
    assert result.generation == snapshot_before.generation
    assert restarted.snapshot(environment.workspace_id).files == snapshot_before.files
    reloaded = all_events(restarted, environment)
    assert [event.event_id for event in reloaded] == [event.event_id for event in events_before]
    assert [event.seq for event in reloaded] == [1, 2]
    assert environment.stable_scan(restarted).emitted == 0


def test_deleting_projections_rebuilds_with_the_same_revision_ids(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    environment.write("notes/humaines/a.md", b"alpha")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    before = service.snapshot(environment.workspace_id)
    [before_event] = all_events(service, environment)

    delete_projections(environment)
    restarted = environment.service()
    assert restarted.scan(environment.workspace_id).emitted == 0
    result = environment.stable_scan(restarted)

    assert result.emitted == 1
    rebuilt = restarted.snapshot(environment.workspace_id)
    assert rebuilt.generation != before.generation
    assert rebuilt.files[0].revision_id == before.files[0].revision_id
    [replayed] = all_events(restarted, environment)
    assert replayed.event_id == before_event.event_id
    assert replayed.kind is EventKind.FILE_CREATED
    assert replayed.seq == 1


def test_corrupt_projection_mints_a_new_generation(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    environment.write("notes/humaines/a.md", b"alpha")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    before = service.snapshot(environment.workspace_id)

    projection_path(environment, events.SYNC_STATE_FILE_NAME).write_text(
        "{not json", encoding="utf-8"
    )
    restarted = environment.service()
    assert restarted.scan(environment.workspace_id).emitted == 0
    result = environment.stable_scan(restarted)

    assert result.emitted == 1
    rebuilt = restarted.snapshot(environment.workspace_id)
    assert rebuilt.generation != before.generation
    assert rebuilt.files[0].revision_id == before.files[0].revision_id
    [event] = all_events(restarted, environment)
    assert event.kind is EventKind.FILE_CREATED
    assert (
        json.loads(projection_path(environment, events.SYNC_STATE_FILE_NAME).read_text())[
            "generation"
        ]
        == rebuilt.generation
    )


def test_modification_while_stopped_is_reported_once(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("notes/humaines/a.md", b"before restart")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    [created] = all_events(service, environment)

    path.write_bytes(b"changed while stopped")
    restarted = environment.service()
    assert restarted.scan(environment.workspace_id).emitted == 0
    result = environment.stable_scan(restarted)

    assert result.emitted == 1
    created_again, modified = all_events(restarted, environment)
    assert created_again.event_id == created.event_id
    assert modified.kind is EventKind.FILE_MODIFIED
    assert modified.previous_revision_id == created.revision_id
    assert modified.revision_id == events.revision_id_for(
        environment.workspace_id,
        "notes/humaines/a.md",
        hashlib.sha256(b"changed while stopped").hexdigest(),
    )
    assert restarted.snapshot(environment.workspace_id).files[0].revision_id == modified.revision_id


def test_projections_deleted_while_running_are_rebuilt(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    environment.write("notes/humaines/a.md", b"alpha")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)

    delete_projections(environment)
    result = service.scan(environment.workspace_id)

    assert result.emitted == 0
    assert projection_path(environment, index.FILE_INDEX_FILE_NAME).exists()
    assert projection_path(environment, events.SYNC_STATE_FILE_NAME).exists()
    [state] = service.snapshot(environment.workspace_id).files
    assert state.sha256 == hashlib.sha256(b"alpha").hexdigest()
    [event] = all_events(service, environment)
    assert event.kind is EventKind.FILE_CREATED
