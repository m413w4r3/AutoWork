"""M24 R1e: non-human writes must drain before session secret forms are wiped."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from sidecars.workspace_agent import files

from tests.workspace_agent.sessions_support import (
    CLOSE_KEY,
    PREPARE_KEY,
    SECOND_SESSION_ID,
    SECRET,
    SESSION_ID,
    post_session,
    prepare_payload,
    running_session_agent,
)


def _prepare(running, client: httpx.Client, workspace_id: str, **overrides: object) -> None:
    response = post_session(
        client,
        running,
        "/v1/sessions",
        prepare_payload(workspace_id, **overrides),
        PREPARE_KEY if overrides.get("session_id") is None else "m24-r1e-prepare-next-0001",
    )
    assert response.status_code == 200, response.text


def _put_file(
    running,
    client: httpx.Client,
    workspace_id: str,
    path: str,
    body: bytes,
    key: str,
) -> httpx.Response:
    return client.put(
        f"/v1/workspaces/{workspace_id}/files",
        params={"path": path},
        content=body,
        headers={
            **running.auth_headers,
            "Idempotency-Key": key,
            "X-Autowork-Expected-Sha256": "new",
            "X-Autowork-Origin": "agent",
        },
    )


def test_close_cannot_wipe_forms_between_staging_and_secret_check(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged credential stays covered while close waits for the write."""
    with running_session_agent(session_base) as running:
        with running.client() as prepare_client:
            workspace_id = running.ensure_workspace(prepare_client)
            _prepare(running, prepare_client, workspace_id)

        server = running.agent._server
        assert server is not None
        supervisor = server.session_supervisor
        handle = supervisor._handle(SESSION_ID)
        write_admission = getattr(handle, "write_admission", None)
        checker_entered = threading.Event()
        resume_checker = threading.Event()
        close_progress = threading.Event()
        close_wiped_forms = threading.Event()
        close_started = threading.Event()
        close_done = threading.Event()
        write_result: list[httpx.Response] = []
        close_result: list[httpx.Response] = []

        original_check = supervisor.check_non_ui_file_write
        original_stop_channels = supervisor._stop_channels

        def paused_check(*args: object, **kwargs: object) -> None:
            checker_entered.set()
            if not resume_checker.wait(timeout=10):
                raise AssertionError("test did not release the secret check")
            original_check(*args, **kwargs)  # type: ignore[arg-type]

        def observed_stop_channels(handle, *, wipe_secret: bool) -> None:
            original_stop_channels(handle, wipe_secret=wipe_secret)
            if wipe_secret:
                close_wiped_forms.set()
                close_progress.set()

        original_wait_until_idle: Callable[[float], bool] | None = None
        if write_admission is not None:
            original_wait_until_idle = cast(Any, write_admission).wait_until_idle

        def observed_wait_until_idle(timeout: float) -> bool:
            close_progress.set()
            assert original_wait_until_idle is not None
            return original_wait_until_idle(timeout)

        monkeypatch.setattr(supervisor, "check_non_ui_file_write", paused_check)
        monkeypatch.setattr(supervisor, "_stop_channels", observed_stop_channels)
        if write_admission is not None:
            monkeypatch.setattr(write_admission, "wait_until_idle", observed_wait_until_idle)

        def write_secret() -> None:
            with running.client() as client:
                write_result.append(
                    client.put(
                        f"/v1/workspaces/{workspace_id}/files?path=notes%2Fhumaines%2Fr1e.md",
                        content=b"synthetic session credential: " + SECRET,
                        headers={
                            **running.auth_headers,
                            "Idempotency-Key": "m24-r1e-write-0001",
                            "X-Autowork-Expected-Sha256": "new",
                            "X-Autowork-Origin": "agent",
                        },
                    )
                )

        def close_session() -> None:
            close_started.set()
            with running.client() as client:
                close_result.append(
                    post_session(
                        client,
                        running,
                        f"/v1/sessions/{SESSION_ID}/close",
                        {"reason": "test"},
                        CLOSE_KEY,
                    )
                )
            close_done.set()

        writer = threading.Thread(target=write_secret, name="r1e-writer")
        writer.start()
        try:
            assert checker_entered.wait(timeout=10), "write never reached its secret check"
            closer = threading.Thread(target=close_session, name="r1e-closer")
            closer.start()
            assert close_started.wait(timeout=10), "close request did not start"
            assert close_progress.wait(timeout=10), "close did not reach its drain or cleanup"
            wiped_before_check = close_wiped_forms.is_set()
            resume_checker.set()
            writer.join(timeout=10)
            closer.join(timeout=10)

            assert not writer.is_alive(), "write thread did not finish"
            assert not closer.is_alive(), "close thread did not finish"
            assert not wiped_before_check, "close wiped forms before the admitted write checked"
            assert write_result and write_result[0].status_code == 403, (
                write_result[0].text if write_result else "write produced no response"
            )
            assert close_done.is_set()
            assert close_result and close_result[0].status_code == 200
            assert not (running.host_path / "notes" / "humaines" / "r1e.md").exists()
        finally:
            resume_checker.set()
            writer.join(timeout=10)


def test_close_drains_two_simultaneous_clean_writes(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        server = running.agent._server
        assert server is not None
        supervisor = server.session_supervisor
        handle = supervisor._handle(SESSION_ID)
        write_admission = getattr(handle, "write_admission", None)
        arrival = threading.Barrier(3)
        release_stream = threading.Event()
        close_progress = threading.Event()
        close_wiped_forms = threading.Event()
        close_result: list[httpx.Response] = []
        write_results: list[tuple[str, httpx.Response]] = []
        original_stream = files._stream_to_temporary
        original_stop_channels = supervisor._stop_channels
        original_wait_until_idle: Callable[[float], bool] | None = None
        if write_admission is not None:
            original_wait_until_idle = cast(Any, write_admission).wait_until_idle

        def paused_stream(*args: object, **kwargs: object):
            arrival.wait(timeout=10)
            if not release_stream.wait(timeout=10):
                raise AssertionError("test did not release both writes")
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observed_stop_channels(target, *, wipe_secret: bool) -> None:
            original_stop_channels(target, wipe_secret=wipe_secret)
            if wipe_secret:
                close_wiped_forms.set()
                close_progress.set()

        def observed_wait_until_idle(timeout: float) -> bool:
            close_progress.set()
            assert original_wait_until_idle is not None
            return original_wait_until_idle(timeout)

        monkeypatch.setattr(files, "_stream_to_temporary", paused_stream)
        monkeypatch.setattr(supervisor, "_stop_channels", observed_stop_channels)
        if write_admission is not None:
            monkeypatch.setattr(write_admission, "wait_until_idle", observed_wait_until_idle)

        def write(path: str, key: str) -> None:
            with running.client() as client:
                write_results.append(
                    (path, _put_file(running, client, workspace_id, path, b"clean note", key))
                )

        def close() -> None:
            with running.client() as client:
                close_result.append(
                    post_session(
                        client,
                        running,
                        f"/v1/sessions/{SESSION_ID}/close",
                        {"reason": "test"},
                        CLOSE_KEY,
                    )
                )

        writers = [
            threading.Thread(
                target=write, args=(f"notes/humaines/r1e-{i}.md", f"m24-r1e-clean-{i}")
            )
            for i in range(2)
        ]
        closer = threading.Thread(target=close, name="r1e-clean-closer")
        for writer in writers:
            writer.start()
        try:
            arrival.wait(timeout=10)
            closer.start()
            assert close_progress.wait(timeout=10), "close did not reach its drain or cleanup"
            wiped_before_publish = close_wiped_forms.is_set()
            release_stream.set()
            for writer in writers:
                writer.join(timeout=10)
            closer.join(timeout=10)

            assert not wiped_before_publish
            assert all(not writer.is_alive() for writer in writers)
            assert not closer.is_alive()
            assert len(write_results) == 2
            assert all(response.status_code == 201 for _, response in write_results)
            assert all(
                (running.host_path / path).read_bytes() == b"clean note"
                for path, _ in write_results
            )
            assert close_result and close_result[0].status_code == 200
            assert handle.secret_forms == ()
        finally:
            release_stream.set()
            for writer in writers:
                writer.join(timeout=10)
            if closer.ident is not None:
                closer.join(timeout=10)


def test_stop_drains_secret_check_and_refuses_later_writes(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        server = running.agent._server
        assert server is not None
        supervisor = server.session_supervisor
        body_entered = threading.Event()
        release_stream = threading.Event()
        stop_progress = threading.Event()
        forms_wiped = threading.Event()
        write_result: list[httpx.Response] = []
        stop_done = threading.Event()
        original_stream = files._stream_to_temporary
        original_stop_channels = supervisor._stop_channels
        handle = supervisor._handle(SESSION_ID)
        write_admission = getattr(handle, "write_admission", None)
        original_wait_until_idle: Callable[[float], bool] | None = None
        if write_admission is not None:
            original_wait_until_idle = cast(Any, write_admission).wait_until_idle

        def paused_stream(*args: object, **kwargs: object):
            body_entered.set()
            if not release_stream.wait(timeout=10):
                raise AssertionError("test did not release the credential write")
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observed_stop_channels(target, *, wipe_secret: bool) -> None:
            original_stop_channels(target, wipe_secret=wipe_secret)
            if wipe_secret:
                forms_wiped.set()
                stop_progress.set()

        def observed_wait_until_idle(timeout: float) -> bool:
            stop_progress.set()
            assert original_wait_until_idle is not None
            return original_wait_until_idle(timeout)

        monkeypatch.setattr(files, "_stream_to_temporary", paused_stream)
        monkeypatch.setattr(supervisor, "_stop_channels", observed_stop_channels)
        if write_admission is not None:
            monkeypatch.setattr(write_admission, "wait_until_idle", observed_wait_until_idle)

        def write_secret() -> None:
            with running.client() as client:
                write_result.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-stop.md",
                        b"credential: " + SECRET,
                        "m24-r1e-stop-write-0001",
                    )
                )

        writer = threading.Thread(target=write_secret, name="r1e-stop-writer")

        def stop_supervisor() -> None:
            supervisor.stop()
            stop_done.set()

        stopper = threading.Thread(target=stop_supervisor, name="r1e-stopper")
        writer.start()
        try:
            assert body_entered.wait(timeout=10)
            stopper.start()
            assert stop_progress.wait(timeout=10), "stop did not reach its drain or cleanup"
            wiped_before_check = forms_wiped.is_set()
            release_stream.set()
            writer.join(timeout=10)
            stopper.join(timeout=10)

            assert not wiped_before_check
            assert not writer.is_alive()
            assert not stopper.is_alive()
            assert stop_done.is_set()
            assert write_result and write_result[0].status_code == 403
            assert not (running.host_path / "notes" / "humaines" / "r1e-stop.md").exists()
            assert forms_wiped.is_set()

            with running.client() as client:
                after_stop = _put_file(
                    running,
                    client,
                    workspace_id,
                    "notes/humaines/r1e-after-stop.md",
                    b"clean note",
                    "m24-r1e-after-stop-0001",
                )
            assert after_stop.status_code == 409
            assert not (running.host_path / "notes" / "humaines" / "r1e-after-stop.md").exists()
        finally:
            release_stream.set()
            writer.join(timeout=10)
            if stopper.ident is not None:
                stopper.join(timeout=10)


def test_restart_refuses_a_lost_session_then_admits_a_new_one(session_base: Path) -> None:
    with running_session_agent(session_base) as first:
        with first.client() as client:
            workspace_id = first.ensure_workspace(client)
            _prepare(first, client, workspace_id)

    with running_session_agent(session_base) as second:
        with second.client() as client:
            lost_write = _put_file(
                second,
                client,
                workspace_id,
                "notes/humaines/r1e-lost.md",
                b"credential: " + SECRET,
                "m24-r1e-lost-write-0001",
            )
            assert lost_write.status_code == 409
            assert not (second.host_path / "notes" / "humaines" / "r1e-lost.md").exists()

            _prepare(
                second,
                client,
                workspace_id,
                session_id=SECOND_SESSION_ID,
                task_id="b2c3d4e5-f6a7-489b-9c0d-1e2f3a4b5c6d",
            )
            clean_write = _put_file(
                second,
                client,
                workspace_id,
                "notes/humaines/r1e-restarted.md",
                b"clean after restart",
                "m24-r1e-restarted-write-0001",
            )
            assert clean_write.status_code == 201
            assert (
                second.host_path / "notes" / "humaines" / "r1e-restarted.md"
            ).read_bytes() == b"clean after restart"


def test_failed_publication_drains_before_close_wipes_forms(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        server = running.agent._server
        assert server is not None
        supervisor = server.session_supervisor
        handle = supervisor._handle(SESSION_ID)
        write_admission = getattr(handle, "write_admission", None)
        original_stop_channels = supervisor._stop_channels
        publication_entered = threading.Event()
        release_publication = threading.Event()
        close_progress = threading.Event()
        forms_wiped = threading.Event()
        failed_result: list[httpx.Response] = []
        close_result: list[httpx.Response] = []

        def fail_publication(*_args: object, **_kwargs: object):
            publication_entered.set()
            if not release_publication.wait(timeout=10):
                raise AssertionError("test did not release the failed publication")
            raise files.FileStorageError("synthetic publication failure")

        def observed_stop_channels(target, *, wipe_secret: bool) -> None:
            original_stop_channels(target, wipe_secret=wipe_secret)
            if wipe_secret:
                forms_wiped.set()
                close_progress.set()

        original_wait_until_idle: Callable[[float], bool] | None = None
        if write_admission is not None:
            original_wait_until_idle = cast(Any, write_admission).wait_until_idle

        def observed_wait_until_idle(timeout: float) -> bool:
            close_progress.set()
            assert original_wait_until_idle is not None
            return original_wait_until_idle(timeout)

        monkeypatch.setattr(files, "_commit_write", fail_publication)
        monkeypatch.setattr(supervisor, "_stop_channels", observed_stop_channels)
        if write_admission is not None:
            monkeypatch.setattr(write_admission, "wait_until_idle", observed_wait_until_idle)

        def write() -> None:
            with running.client() as client:
                failed_result.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-failed.md",
                        b"clean body",
                        "m24-r1e-failed-write-0001",
                    )
                )

        def close() -> None:
            with running.client() as client:
                close_result.append(
                    post_session(
                        client,
                        running,
                        f"/v1/sessions/{SESSION_ID}/close",
                        {"reason": "test"},
                        CLOSE_KEY,
                    )
                )

        writer = threading.Thread(target=write, name="r1e-failed-writer")
        closer = threading.Thread(target=close, name="r1e-failed-closer")
        writer.start()
        try:
            assert publication_entered.wait(timeout=10), "write did not reach publication"
            closer.start()
            assert close_progress.wait(timeout=10), "close did not reach its drain or cleanup"
            wiped_before_failure = forms_wiped.is_set()
            release_publication.set()
            writer.join(timeout=10)
            closer.join(timeout=10)

            assert not writer.is_alive()
            assert not closer.is_alive()
            assert not wiped_before_failure
            assert failed_result and failed_result[0].status_code == 500
            assert close_result and close_result[0].status_code == 200
            assert handle.record.state.value == "closed"
            assert handle.secret_forms == ()
            assert not (running.host_path / "notes" / "humaines" / "r1e-failed.md").exists()
            assert not any(
                entry.name.endswith(".tmp")
                for entry in (running.host_path / "notes" / "humaines").iterdir()
            )
        finally:
            release_publication.set()
            writer.join(timeout=10)
            if closer.ident is not None:
                closer.join(timeout=10)
