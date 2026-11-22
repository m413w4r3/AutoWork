"""Dirfd path validation and resolution (design §5.4, ticket W2)."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sidecars.workspace_agent import paths
from sidecars.workspace_agent.protocol import ErrorCode

from tests.workspace_agent.conftest import REPOSITORY_ROOT

PACKAGE_DIR = REPOSITORY_ROOT / "sidecars" / "workspace_agent"


@contextmanager
def _workspace(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    workspace = tmp_path / "ws"
    for name in ("base_samples", "notes", "notes/humaines", "notes/agents", "scripts", "vt"):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    fd = paths.open_pinned_root(workspace)
    try:
        yield workspace, fd
    finally:
        os.close(fd)


def _reason(path: str) -> str:
    with pytest.raises(paths.PathValidationError) as excinfo:
        paths.validate_relative_path(path)
    reason: str = excinfo.value.reason
    return reason


@pytest.mark.parametrize(
    "path",
    [
        "notes/humaines/note.md",
        "notes/bridge/consultation.md",
        "notes/agents/task-01/output.txt",
        "scripts/run.sh",
        "regles/rule.yar",
        "discover_sample/hint.txt",
        "plans/plan.json",
        "results/result.txt",
        "pcaps/capture.pcap",
        "base_samples/" + "a" * 64,
        "vt/files/" + "b" * 64,
        "vt/relationships/graph.json",
        "notes/agents/" + "A" * 64 + "/file",
        "vt/behaviours/deeply/nested/record.json",
    ],
)
def test_accepted_paths_split_on_slashes_only(path: str) -> None:
    components = paths.validate_relative_path(path)
    assert components == tuple(path.split("/"))


def test_nfc_is_required() -> None:
    assert _reason("notes/humaines/café.md") == "nfc"
    assert paths.validate_relative_path("notes/humaines/café.md")


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("", "empty"),
        ("/etc/passwd", "absolute"),
        ("scripts\\evil", "backslash"),
        ("scripts/a\x00b", "nul"),
        ("scripts/a\nb", "control"),
        ("scripts/a\x7fb", "control"),
        ("scripts/a\u0085b", "control"),
        ("scripts//payload", "empty_component"),
        ("scripts/payload/", "empty_component"),
        ("scripts/./payload", "dot_component"),
        ("scripts/../payload", "dot_component"),
        ("scripts/..", "dot_component"),
        (".autowork", "agent_internal"),
        (".autowork/manifest.json", "agent_internal"),
        ("etc/passwd", "top_level"),
        ("scripts", "top_level"),
        ("notes", "top_level"),
        ("notes/secrets/x", "notes_child"),
        ("notes/agents", "task_id"),
        ("notes/agents/../humaines/x", "dot_component"),
        ("notes/agents/has space/x", "task_id"),
        ("notes/agents/-leading/x", "task_id"),
        ("notes/agents/" + "a" * 65 + "/x", "task_id"),
    ],
)
def test_rejected_paths_carry_a_path_free_reason(path: str, reason: str) -> None:
    with pytest.raises(paths.PathValidationError) as excinfo:
        paths.validate_relative_path(path)
    assert excinfo.value.reason == reason
    assert excinfo.value.code is ErrorCode.INVALID_PATH
    assert excinfo.value.status == 400
    if path:
        assert path not in str(excinfo.value)


def test_length_limits_are_byte_based() -> None:
    assert _reason("scripts/" + "a" * 256) == "component_too_long"
    total = "scripts/" + "/".join(["a" * 255] * 4)
    assert len(total.encode("utf-8")) > 1024
    assert _reason(total) == "too_long"


def test_non_utf8_encodable_input_is_rejected() -> None:
    assert _reason("scripts/\ud800") == "encoding"


def test_resolution_returns_the_parent_dirfd_and_final_name(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (workspace, root_fd):
        parent_fd, name, components = paths.resolve_parent(root_fd, "notes/humaines/note.md")
        try:
            assert name == "note.md"
            assert components == ("notes", "humaines", "note.md")
            opened = os.fstat(parent_fd)
            expected = os.stat(workspace / "notes" / "humaines")
            assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
        finally:
            os.close(parent_fd)


def test_symlink_component_is_rejected_and_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("untouched")
    with _workspace(tmp_path) as (workspace, root_fd):
        (workspace / "scripts").rmdir()
        (workspace / "scripts").symlink_to(outside, target_is_directory=True)
        with pytest.raises(paths.PathResolutionError):
            paths.resolve_parent(root_fd, "scripts/payload.txt")
    assert (outside / "marker.txt").read_text() == "untouched"
    assert not (outside / "payload.txt").exists()


def test_symlink_swapped_between_stat_and_open_never_escapes(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with _workspace(tmp_path) as (_, root_fd):
        swapped = False

        def swap_to_symlink(parent_fd: int, name: str) -> None:
            nonlocal swapped
            if name != "scripts" or swapped:
                return
            swapped = True
            os.rename(name, f"{name}.real", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.symlink(str(outside), name, dir_fd=parent_fd)

        with pytest.raises(paths.PathResolutionError):
            paths.resolve_parent(
                root_fd, "scripts/payload.txt", hooks=paths.WalkHooks(before_open=swap_to_symlink)
            )
    assert swapped


def test_identity_mismatch_is_retried_once_then_succeeds(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (workspace, root_fd):
        swaps = 0

        def replace_directory(fd: int, parent_fd: int, name: str) -> None:
            nonlocal swaps
            if name != "scripts" or swaps:
                return
            swaps += 1
            os.rename(name, f"{name}.old", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, 0o700, dir_fd=parent_fd)

        parent_fd, name, _ = paths.resolve_parent(
            root_fd, "scripts/payload.txt", hooks=paths.WalkHooks(after_open=replace_directory)
        )
        try:
            assert swaps == 1
            assert name == "payload.txt"
            opened = os.fstat(parent_fd)
            expected = os.stat(workspace / "scripts")
            assert (opened.st_dev, opened.st_ino) == (expected.st_dev, expected.st_ino)
        finally:
            os.close(parent_fd)


def test_second_identity_mismatch_raises_concurrent_change(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (workspace, root_fd):
        attempts = 0

        def replace_directory(fd: int, parent_fd: int, name: str) -> None:
            nonlocal attempts
            if name != "scripts":
                return
            attempts += 1
            os.rename(name, f"{name}.{attempts}", src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(name, 0o700, dir_fd=parent_fd)

        with pytest.raises(paths.ConcurrentChangeError) as excinfo:
            paths.resolve_parent(
                root_fd, "scripts/payload.txt", hooks=paths.WalkHooks(after_open=replace_directory)
            )
        assert attempts == 2
        assert excinfo.value.code is ErrorCode.CONCURRENT_CHANGE
        assert excinfo.value.status == 409
        assert str(workspace) not in str(excinfo.value)


def test_create_mode_is_exact_and_existing_directories_are_reused(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (workspace, root_fd):
        created = paths.open_directory_chain(
            root_fd, ("scripts", "nested"), create=True, mode=0o700
        )
        os.close(created)
        info = os.lstat(workspace / "scripts" / "nested")
        assert stat.S_ISDIR(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700

        os.chmod(workspace / "scripts" / "nested", 0o750)
        reused = paths.open_directory_chain(root_fd, ("scripts", "nested"), create=True, mode=0o700)
        os.close(reused)
        assert stat.S_IMODE(os.lstat(workspace / "scripts" / "nested").st_mode) == 0o750


def test_a_regular_file_component_is_rejected(tmp_path: Path) -> None:
    with _workspace(tmp_path) as (workspace, root_fd):
        (workspace / "notes" / "humaines" / "note.md").write_text("x")
        with pytest.raises(paths.PathResolutionError):
            paths.resolve_parent(root_fd, "notes/humaines/note.md/deeper")


def test_open_pinned_root_refuses_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(paths.PathResolutionError):
        paths.open_pinned_root(link)


def test_paths_never_authorize_with_path_resolution() -> None:
    for module in ("paths.py", "workspace.py"):
        source = (PACKAGE_DIR / module).read_text(encoding="utf-8")
        assert ".resolve(" not in source, module
