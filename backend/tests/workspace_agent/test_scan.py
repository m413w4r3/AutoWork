"""Polling reconcile scan: stability, hash bounds, renames, policy violations.

Normative source: ``docs/design/investigation-workshop/WORKSPACE-AGENT.md``
§5.3, §5.5, §5.6 and §6. Time is injected; the filesystem is a real tmpdir.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest
from sidecars.workspace_agent import events, index
from sidecars.workspace_agent.events import EventKind, event_id_for, revision_id_for
from sidecars.workspace_agent.scan import (
    METADATA_ONLY_THRESHOLD_BYTES,
    WorkspaceScanService,
)

from tests.workspace_agent.w3_support import ScanEnvironment


def all_events(
    service: WorkspaceScanService, environment: ScanEnvironment
) -> list[events.FileEvent]:
    return list(service.events(environment.workspace_id, limit=500).events)


def snapshot_paths(service: WorkspaceScanService, environment: ScanEnvironment) -> list[str]:
    return [state.path for state in service.snapshot(environment.workspace_id).files]


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_projection(environment: ScanEnvironment, name: str, payload: dict[str, object]) -> None:
    directory = environment.host_path / ".autowork"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def seed_system_generated_entry(
    environment: ScanEnvironment, relative_path: str, content: bytes
) -> str:
    """Simulate a W4 ``PUT generated`` write: index entry with origin ``system``."""
    path = environment.host_path / relative_path
    info = path.stat()
    sha256 = sha256_of(content)
    state = index.FileState(
        path=relative_path,
        revision_id=revision_id_for(environment.workspace_id, relative_path, sha256),
        sha256=sha256,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        origin="system",
        observed_at="2026-01-01T00:00:00Z",
        policy_class=index.POLICY_GENERATED_IMMUTABLE,
        task_id=None,
    )
    generation = 4242
    file_index = index.FileIndex(
        workspace_id=environment.workspace_id, generation=generation, files={relative_path: state}
    )
    write_projection(environment, index.FILE_INDEX_FILE_NAME, file_index.to_payload())
    write_projection(
        environment,
        events.SYNC_STATE_FILE_NAME,
        events.SyncState.fresh(environment.workspace_id, generation).to_payload(),
    )
    return sha256


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("notes/humaines/note.md", index.POLICY_HUMAN_EDITABLE),
        ("notes/bridge/plan.md", index.POLICY_GENERATED_IMMUTABLE),
        ("notes/agents/task-1/result.md", index.POLICY_AGENT_OWNED),
        ("notes/other/note.md", index.POLICY_HUMAN_EDITABLE),
        ("scripts/run.sh", index.POLICY_HUMAN_EDITABLE),
        ("regles/rule.yar", index.POLICY_HUMAN_EDITABLE),
        ("discover_sample/sample.bin", index.POLICY_HUMAN_EDITABLE),
        ("plans/initial-v0001.md", index.POLICY_GENERATED_IMMUTABLE),
        ("vt/files/report.json", index.POLICY_GENERATED_IMMUTABLE),
        ("results/output.txt", index.POLICY_GENERATED_IMMUTABLE),
        ("pcaps/capture.pcap", index.POLICY_GENERATED_IMMUTABLE),
        ("base_samples/" + "a" * 64, index.POLICY_READ_ONLY),
        (".autowork/manifest.json", index.POLICY_AGENT_INTERNAL),
    ],
)
def test_policy_class_follows_the_section_5_3_prefixes(path: str, expected: str) -> None:
    assert index.policy_class_for(path) == expected


def test_mid_write_file_is_not_imported_until_stable(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("notes/humaines/note.md", b"part one")

    assert service.scan(environment.workspace_id).emitted == 0
    environment.clock.advance(0.2)
    path.write_bytes(b"part one and two")
    assert service.scan(environment.workspace_id).emitted == 0
    environment.clock.advance(0.3)
    assert service.scan(environment.workspace_id).emitted == 0
    environment.clock.advance(0.2)
    result = service.scan(environment.workspace_id)

    assert result.emitted == 1
    assert result.latest_cursor == 1
    [state] = service.snapshot(environment.workspace_id).files
    assert state.path == "notes/humaines/note.md"
    assert state.sha256 == sha256_of(b"part one and two")
    assert state.revision_id == revision_id_for(
        environment.workspace_id, state.path, sha256_of(b"part one and two")
    )


def test_created_event_and_ids_are_deterministic(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    content = b"# note\n"
    environment.write("notes/humaines/note.md", content)
    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)
    assert result.emitted == 1

    [event] = all_events(service, environment)
    revision_id = revision_id_for(
        environment.workspace_id, "notes/humaines/note.md", sha256_of(content)
    )
    assert event.kind is EventKind.FILE_CREATED
    assert event.seq == 1
    assert event.origin == "filesystem"
    assert event.task_id is None
    assert event.revision_id == revision_id
    assert event.event_id == event_id_for(
        environment.workspace_id, event.path, None, revision_id, EventKind.FILE_CREATED
    )
    assert result.latest_cursor == 1

    assert environment.stable_scan(service).emitted == 0
    assert len(all_events(service, environment)) == 1


def test_large_file_is_indexed_metadata_only(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.host_path / "pcaps" / "capture.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.truncate(METADATA_ONLY_THRESHOLD_BYTES + 1)
    os.chmod(path, 0o640)

    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)
    assert result.emitted == 1

    [event] = all_events(service, environment)
    assert event.kind is EventKind.FILE_CREATED
    assert event.path == "pcaps/capture.bin"
    assert event.sha256 is None
    assert event.revision_id is None
    assert event.size == METADATA_ONLY_THRESHOLD_BYTES + 1
    [state] = service.snapshot(environment.workspace_id).files
    assert state.sha256 is None
    assert state.revision_id is None


def test_symlink_is_reported_but_never_followed(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    outside = environment.root.parent / "outside-secret.txt"
    outside.write_bytes(b"must never be indexed")
    environment.write("notes/humaines/real.md", b"real")
    link = environment.host_path / "notes" / "humaines" / "link.md"
    os.symlink(outside, link)

    first = service.scan(environment.workspace_id)
    assert first.emitted == 1
    assert first.policy_violations == 1
    second = environment.stable_scan(service)
    assert second.emitted == 1

    kinds = {event.path: event.kind for event in all_events(service, environment)}
    assert kinds == {
        "notes/humaines/link.md": EventKind.POLICY_VIOLATION,
        "notes/humaines/real.md": EventKind.FILE_CREATED,
    }
    assert "notes/humaines/link.md" not in snapshot_paths(service, environment)
    link_event = next(
        event
        for event in all_events(service, environment)
        if event.path == "notes/humaines/link.md"
    )
    assert link_event.policy_class == index.POLICY_HUMAN_EDITABLE
    assert link_event.sha256 is None
    assert environment.stable_scan(service).emitted == 0


def test_executable_bit_on_generated_file_is_a_policy_violation(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("plans/initial-v0001.md", b"plan", mode=0o640)
    service.scan(environment.workspace_id)
    environment.stable_scan(service)

    os.chmod(path, 0o740)
    result = service.scan(environment.workspace_id)
    assert result.emitted == 1
    assert result.policy_violations == 1
    [violation] = [
        event
        for event in all_events(service, environment)
        if event.kind is EventKind.POLICY_VIOLATION
    ]
    assert violation.path == "plans/initial-v0001.md"
    assert violation.policy_class == index.POLICY_GENERATED_IMMUTABLE
    assert violation.revision_id == revision_id_for(
        environment.workspace_id, "plans/initial-v0001.md", sha256_of(b"plan")
    )

    assert environment.stable_scan(service).emitted == 0


def test_filesystem_edit_of_a_generated_file_is_reported_and_preserved(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("plans/plan.md", b"generated v1")
    seed_system_generated_entry(environment, "plans/plan.md", b"generated v1")

    assert service.scan(environment.workspace_id).emitted == 0
    path.write_bytes(b"tampered by hand")
    assert service.scan(environment.workspace_id).emitted == 0
    result = environment.stable_scan(service)

    assert result.emitted == 1
    assert result.policy_violations == 1
    [event] = all_events(service, environment)
    assert event.kind is EventKind.POLICY_VIOLATION
    assert event.path == "plans/plan.md"
    assert event.policy_class == index.POLICY_GENERATED_IMMUTABLE
    assert event.origin == "filesystem"
    assert path.read_bytes() == b"tampered by hand"
    [state] = service.snapshot(environment.workspace_id).files
    assert state.sha256 == sha256_of(b"tampered by hand")
    assert state.origin == "filesystem"


def test_rename_emits_one_rename_event(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    source = environment.write("scripts/run.sh", b"#!/bin/sh\n")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    [created] = all_events(service, environment)

    source.rename(environment.host_path / "scripts" / "deploy.sh")
    environment.clock.advance(0.1)
    assert service.scan(environment.workspace_id).emitted == 0
    result = environment.stable_scan(service, advance_seconds=1.0)

    assert result.emitted == 1
    created_event, event = all_events(service, environment)
    assert created_event.kind is EventKind.FILE_CREATED
    assert event.kind is EventKind.FILE_RENAMED
    assert event.path == "scripts/deploy.sh"
    assert event.previous_path == "scripts/run.sh"
    assert event.previous_revision_id == created.revision_id
    assert event.revision_id == revision_id_for(
        environment.workspace_id, "scripts/deploy.sh", sha256_of(b"#!/bin/sh\n")
    )
    assert snapshot_paths(service, environment) == ["scripts/deploy.sh"]
    assert environment.stable_scan(service).emitted == 0


def test_delete_emits_one_delete_event_after_the_stability_window(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("notes/humaines/temp.md", b"temporary")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    [created] = all_events(service, environment)

    path.unlink()
    assert service.scan(environment.workspace_id).emitted == 0
    environment.clock.advance(0.2)
    assert service.scan(environment.workspace_id).emitted == 0
    result = environment.stable_scan(service)

    assert result.emitted == 1
    created_event, event = all_events(service, environment)
    assert created_event.kind is EventKind.FILE_CREATED
    assert event.kind is EventKind.FILE_DELETED
    assert event.path == "notes/humaines/temp.md"
    assert event.revision_id is None
    assert event.previous_revision_id == created.revision_id
    assert snapshot_paths(service, environment) == []
    assert environment.stable_scan(service).emitted == 0


def test_mtime_only_change_does_not_mint_a_revision(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("notes/humaines/stable.md", b"same bytes")
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    [created] = all_events(service, environment)

    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns + 1_000_000_000, info.st_mtime_ns + 1_000_000_000))
    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)

    assert result.emitted == 0
    [state] = service.snapshot(environment.workspace_id).files
    assert state.revision_id == created.revision_id
    assert state.mtime_ns == path.stat().st_mtime_ns
    assert [event.event_id for event in all_events(service, environment)] == [created.event_id]


def test_same_size_and_mtime_with_a_new_inode_triggers_rehashing(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("scripts/run.sh", b"AAAA", mode=0o640)
    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    [created] = all_events(service, environment)

    original = path.stat()
    replacement = environment.host_path / "scripts" / ".replacement"
    replacement.write_bytes(b"BBBB")
    os.chmod(replacement, 0o640)
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_ino != original.st_ino
    assert path.stat().st_size == original.st_size
    assert path.stat().st_mtime_ns == original.st_mtime_ns

    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)

    assert result.emitted == 1
    created_event, modified = all_events(service, environment)
    assert created_event.kind is EventKind.FILE_CREATED
    assert modified.kind is EventKind.FILE_MODIFIED
    assert modified.previous_revision_id == created.revision_id
    assert modified.sha256 == sha256_of(b"BBBB")


def test_temporaries_and_agent_metadata_are_never_indexed(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    environment.write("notes/humaines/.note.md.0123456789abcdef.tmp", b"mid-write")
    environment.write(".autowork/incoming/sample.part", b"incoming")

    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)

    assert result.emitted == 0
    assert snapshot_paths(service, environment) == []
    assert all_events(service, environment) == []


def test_base_samples_content_is_skipped_but_executable_bit_is_policed(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service()
    sample_path = environment.write("base_samples/" + "d" * 64, b"sample", mode=0o440)

    service.scan(environment.workspace_id)
    result = environment.stable_scan(service)
    assert result.emitted == 0
    assert snapshot_paths(service, environment) == []

    os.chmod(sample_path, 0o550)
    result = service.scan(environment.workspace_id)
    assert result.emitted == 1
    assert result.policy_violations == 1
    [violation] = all_events(service, environment)
    assert violation.kind is EventKind.POLICY_VIOLATION
    assert violation.path == "base_samples/" + "d" * 64
    assert violation.policy_class == index.POLICY_READ_ONLY
    assert violation.revision_id is None
    assert environment.stable_scan(service).emitted == 0


def test_scan_never_modifies_user_content(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service()
    path = environment.write("notes/humaines/human.md", b"do not touch", mode=0o640)
    before = path.stat()

    service.scan(environment.workspace_id)
    environment.stable_scan(service)
    environment.stable_scan(service, full=True)

    after = path.stat()
    assert path.read_bytes() == b"do not touch"
    assert (after.st_size, after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == (
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    )
