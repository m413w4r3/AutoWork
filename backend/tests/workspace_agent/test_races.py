"""W8 race hardening against a real tmpdir workspace (design §5.4-§5.6, ticket W8).

Every test uses real directories and real dirfd syscalls; the only seam is the
documented ``paths.WalkHooks`` seam (production passes ``None``), which makes
the stat/open window deterministic instead of hoping a thread scheduler lands
on it. The loop tests then repeat the real race enough times to catch a
regression that a single interleaving would miss.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import threading
from pathlib import Path

import pytest
from sidecars.workspace_agent import events, files, paths, policies, scan, workspace

from tests.workspace_agent.w3_support import ScanEnvironment, write_workspace_file

NOTE_A = "notes/humaines/a.md"
NOTE_B = "notes/humaines/b.md"
RACE_NOTE = "notes/humaines/note.md"


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def revision_for(environment: ScanEnvironment, path: str, content: bytes) -> str:
    revision: str = events.revision_id_for(environment.workspace_id, path, sha256_of(content))
    return revision


def write_via_files_route(
    environment: ScanEnvironment,
    service: scan.WorkspaceScanService,
    path: str,
    content: bytes,
    *,
    key: str,
    expected: str = "new",
    hooks: paths.WalkHooks | None = None,
) -> files.WriteOutcome:
    return files.write_file(
        root=environment.root,
        descriptor=environment.descriptor,
        scans=service,
        route=policies.WriteRoute.FILES,
        path=path,
        expected_sha256=expected,
        origin="ui",
        task_id=None,
        generated_by=None,
        source_sha256=None,
        source=io.BytesIO(content),
        declared_length=len(content),
        idempotency_key=key,
        hooks=hooks,
    )


def replace_file(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.human.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


# -- symlink swap between stat and open ---------------------------------------


def test_symlink_swap_between_stat_and_open_never_writes_outside(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service(stability_window_ms=0)
    outside = environment.root.parent / "outside"
    outside.mkdir()
    host = environment.host_path
    scripts = host / "scripts"
    scripts.mkdir(exist_ok=True)
    swapped = threading.Event()

    def swap_after_open(fd: int, parent_fd: int, name: str) -> None:
        del fd, parent_fd
        if name != "scripts" or swapped.is_set():
            return
        swapped.set()
        os.rename(scripts, host / "scripts-pinned")
        os.symlink(outside, scripts)

    hooks = paths.WalkHooks(after_open=swap_after_open)
    with pytest.raises(paths.WorkspacePathError):
        write_via_files_route(
            environment,
            service,
            "scripts/race.txt",
            b"payload",
            key="race-swap-0001",
            hooks=hooks,
        )

    assert swapped.is_set()
    assert list(outside.iterdir()) == []
    assert not (host / "scripts-pinned" / "race.txt").exists()
    assert not (host / "scripts-pinned" / "race.txt.tmp").exists()


def test_symlink_swap_loop_never_escapes_the_workspace(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service(stability_window_ms=0)
    outside = environment.root.parent / "outside"
    outside.mkdir()
    host = environment.host_path
    scripts = host / "scripts"
    scripts.mkdir(exist_ok=True)
    parked = host / "scripts-parked"
    stop = threading.Event()

    def racer() -> None:
        while not stop.is_set():
            try:
                os.rename(scripts, parked)
                os.symlink(outside, scripts)
                os.unlink(scripts)
                os.rename(parked, scripts)
            except OSError:
                # Another party moved the entry mid-swap; put it back and retry.
                try:
                    if scripts.is_symlink():
                        os.unlink(scripts)
                except OSError:
                    pass
                try:
                    if parked.exists() and not scripts.exists():
                        os.rename(parked, scripts)
                except OSError:
                    pass

    thread = threading.Thread(target=racer, name="symlink-swap", daemon=True)
    thread.start()
    successes = 0
    try:
        for iteration in range(100):
            try:
                outcome = write_via_files_route(
                    environment,
                    service,
                    f"scripts/race-{iteration:03d}.txt",
                    f"payload {iteration}\n".encode(),
                    key=f"race-loop-{iteration:04d}",
                )
            except (workspace.WorkspaceError, paths.WorkspacePathError):
                pass
            else:
                assert outcome.status in {200, 201}
                successes += 1
            assert list(outside.iterdir()) == [], f"iteration {iteration} escaped"
    finally:
        stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive()

    assert list(outside.iterdir()) == []
    assert successes > 0, "the loop never exercised a successful write"


# -- rename races -------------------------------------------------------------


def test_rename_is_one_event_and_one_revision(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service(stability_window_ms=0)
    workspace_id = environment.workspace_id
    alpha = environment.write(NOTE_A, b"alpha")
    assert service.scan(workspace_id).emitted == 1
    [created] = service.events(workspace_id, limit=100).events

    beta = environment.host_path / NOTE_B
    os.rename(alpha, beta)
    assert service.scan(workspace_id).emitted == 1

    page = service.events(workspace_id, limit=100)
    assert [event.kind for event in page.events] == [
        events.EventKind.FILE_CREATED,
        events.EventKind.FILE_RENAMED,
    ]
    renamed = page.events[-1]
    expected_revision = revision_for(environment, NOTE_B, b"alpha")
    assert renamed.path == NOTE_B
    assert renamed.previous_path == NOTE_A
    assert renamed.previous_revision_id == created.revision_id
    assert renamed.revision_id == expected_revision

    [state] = service.snapshot(workspace_id).files
    assert state.path == NOTE_B
    assert state.revision_id == expected_revision
    assert service.scan(workspace_id).emitted == 0
    assert len(service.snapshot(workspace_id).files) == 1


def test_rename_race_neither_loses_nor_duplicates_a_revision(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service(stability_window_ms=0)
    workspace_id = environment.workspace_id
    content = b"racing content\n"
    alpha = environment.write(NOTE_A, content)
    beta = environment.host_path / NOTE_B
    assert service.scan(workspace_id).emitted == 1
    stop = threading.Event()

    def racer() -> None:
        while not stop.is_set():
            try:
                os.rename(alpha, beta)
                os.rename(beta, alpha)
            except OSError:
                continue

    thread = threading.Thread(target=racer, name="rename-race", daemon=True)
    thread.start()
    retained = 0
    try:
        for _ in range(50):
            service.scan(workspace_id)
            page = service.events(workspace_id, limit=500)
            batch = [event.event_id for event in page.events[retained:]]
            # One scan never reports the same transition twice. A later scan may
            # legitimately re-observe it (the event id is deterministic and the
            # canonical ingest dedupes by id), so uniqueness is per scan batch.
            assert len(batch) == len(set(batch))
            retained = len(page.events)
    finally:
        stop.set()
        thread.join(timeout=15)
        assert not thread.is_alive()

    service.scan(workspace_id)
    service.scan(workspace_id)
    live = [path for path in (alpha, beta) if path.exists()]
    assert len(live) == 1
    live_relative = NOTE_A if live[0] == alpha else NOTE_B
    [state] = service.snapshot(workspace_id).files
    assert state.path == live_relative
    assert state.sha256 == sha256_of(content)
    assert state.revision_id == revision_for(environment, live_relative, content)

    page = service.events(workspace_id, limit=500)
    sequences = [event.seq for event in page.events]
    assert sequences == sorted(set(sequences))
    known_revisions = {
        event.revision_id
        for event in page.events
        if event.path in {NOTE_A, NOTE_B} and event.revision_id is not None
    }
    assert known_revisions <= {
        revision_for(environment, NOTE_A, content),
        revision_for(environment, NOTE_B, content),
    }
    assert service.scan(workspace_id).emitted == 0


# -- concurrent UI write versus filesystem edit -------------------------------


def test_concurrent_ui_put_and_filesystem_edit_keep_both_versions(
    scan_environment: ScanEnvironment,
) -> None:
    environment = scan_environment
    service = environment.service(stability_window_ms=0)
    workspace_id = environment.workspace_id
    host = environment.host_path
    version_a = b"initial ui version\n"
    version_b = b"human edit during the put\n"
    version_c = b"ui proposal during the edit\n"

    seed = write_via_files_route(environment, service, RACE_NOTE, version_a, key="race-seed-0001")
    assert seed.status == 201
    assert service.scan(workspace_id).emitted == 0

    edited = threading.Event()

    def human_edit_before_open(parent_fd: int, name: str) -> None:
        del parent_fd
        if name != "humaines" or edited.is_set():
            return
        edited.set()
        replace_file(host / RACE_NOTE, version_b)

    outcome = write_via_files_route(
        environment,
        service,
        RACE_NOTE,
        version_c,
        key="race-conflict-0001",
        expected=sha256_of(version_a),
        hooks=paths.WalkHooks(before_open=human_edit_before_open),
    )

    assert edited.is_set()
    assert outcome.status == 409
    assert outcome.body["error"] == "revision_conflict"
    details = outcome.body["details"]
    assert isinstance(details, dict)
    assert details["current"] == sha256_of(version_b)
    assert details["proposed_sha256"] == sha256_of(version_c)
    conflict_path = details["conflict_path"]
    assert isinstance(conflict_path, str)
    conflict_file = host / conflict_path
    assert (host / RACE_NOTE).read_bytes() == version_b
    assert conflict_file.read_bytes() == version_c
    assert stat.S_IMODE(os.lstat(conflict_file).st_mode) == 0o640

    service.scan(workspace_id)
    known = {
        record.revision_id for record in service.known_revisions(workspace_id, RACE_NOTE, limit=100)
    }
    for version in (version_a, version_b, version_c):
        assert revision_for(environment, RACE_NOTE, version) in known
    assert (host / RACE_NOTE).read_bytes() == version_b
    assert conflict_file.read_bytes() == version_c


# -- mid-write stability ------------------------------------------------------


def test_mid_write_is_not_imported_until_stable(scan_environment: ScanEnvironment) -> None:
    environment = scan_environment
    service = environment.service(stability_window_ms=400)
    workspace_id = environment.workspace_id
    target = environment.host_path / RACE_NOTE
    target.parent.mkdir(parents=True, exist_ok=True)
    final_content = b"partial complete\n"

    with target.open("wb") as handle:
        handle.write(b"partial ")
        handle.flush()
        os.fsync(handle.fileno())
        first = service.scan(workspace_id)
        assert first.emitted == 0
        assert service.snapshot(workspace_id).files == ()

        handle.write(b"complete\n")
        handle.flush()
        os.fsync(handle.fileno())
        second = service.scan(workspace_id)
        assert second.emitted == 0
        assert service.snapshot(workspace_id).files == ()

    environment.clock.advance(0.5)
    third = service.scan(workspace_id)
    assert third.emitted == 1
    [state] = service.snapshot(workspace_id).files
    assert state.path == RACE_NOTE
    assert state.sha256 == sha256_of(final_content)
    [event] = service.events(workspace_id, limit=100).events
    assert event.kind is events.EventKind.FILE_CREATED
    assert event.sha256 == sha256_of(final_content)

    write_workspace_file(
        environment.host_path, "notes/humaines/.note.md.deadbeef.tmp", b"never indexed"
    )
    environment.clock.advance(1.0)
    assert service.scan(workspace_id).emitted == 0
    assert {item.path for item in service.snapshot(workspace_id).files} == {RACE_NOTE}
