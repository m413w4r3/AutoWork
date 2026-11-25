"""M24 R1e: workspace write leases preserve credential detection through publication."""

from __future__ import annotations

import hashlib
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sidecars.workspace_agent import files
from sidecars.workspace_agent.sessions import supervisor as sessions_supervisor
from sidecars.workspace_agent.sessions.state import (
    SessionWriteAdmissionError,
    SessionWriteDrainTimeoutError,
)

from tests.workspace_agent.sessions_support import (
    CLOSE_KEY,
    COLLECT_KEY,
    PREPARE_KEY,
    SECOND_SESSION_ID,
    SECRET,
    SESSION_ID,
    TASK_ID,
    post_session,
    prepare_payload,
    running_session_agent,
)

SECOND_TASK_ID = "b2c3d4e5-f6a7-489b-9c0d-1e2f3a4b5c6d"
THIRD_SESSION_ID = "c1d2e3f4-a5b6-4789-8c0d-1e2f3a4b5c6d"
THIRD_TASK_ID = "d2e3f4a5-b6c7-4890-9d1e-2f3a4b5c6d7e"
PREPARE_RACE_SECRET = hashlib.sha256(b"R1e prepare addition secret").digest()
LAUNCH_RACE_SECRET = b"synthetic-provider-credential-r1e"


def _prepare(
    running,
    client: httpx.Client,
    workspace_id: str,
    *,
    session_id: str = SESSION_ID,
    task_id: str = TASK_ID,
    secret: bytes = SECRET,
    key: str = PREPARE_KEY,
) -> None:
    response = post_session(
        client,
        running,
        "/v1/sessions",
        prepare_payload(
            workspace_id,
            session_id=session_id,
            task_id=task_id,
            auth_secret=secret,
        ),
        key,
    )
    assert response.status_code == 200, response.text


def _close(
    running,
    client: httpx.Client,
    session_id: str = SESSION_ID,
    key: str = CLOSE_KEY,
) -> httpx.Response:
    return post_session(
        client,
        running,
        f"/v1/sessions/{session_id}/close",
        {"reason": "R1e test"},
        key,
    )


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


def _create_second_workspace(running, client: httpx.Client) -> str:
    payload = running.ensure_payload()
    payload["article_slug"] = "m24-r1e-secondary"
    response = client.post(
        "/v1/workspaces/ensure",
        json=payload,
        headers={**running.auth_headers, "Idempotency-Key": "m24-r1e-ensure-second"},
    )
    assert response.status_code == 201, response.text
    workspace_id = response.json()["workspace_id"]
    assert isinstance(workspace_id, str)
    return workspace_id


def _supervisor(running) -> sessions_supervisor.SessionSupervisor:
    server = running.agent._server
    assert server is not None
    return server.session_supervisor


def _seed_provider_credential_on_launch(
    monkeypatch: pytest.MonkeyPatch, supervisor: sessions_supervisor.SessionSupervisor
) -> None:
    profile = supervisor._profiles.get("fake")
    original_build_launch_plan = profile.build_launch_plan

    def build_launch_plan(context: sessions_supervisor.LaunchContext):
        credential_path = (
            context.session_directory / sessions_supervisor.PROFILE_CREDENTIAL_COPY_PATHS[0]
        )
        credential_path.parent.mkdir(parents=True, exist_ok=True)
        credential_path.write_bytes(b'{"access_token":"' + LAUNCH_RACE_SECRET + b'"}')
        return original_build_launch_plan(context)

    monkeypatch.setattr(profile, "build_launch_plan", build_launch_plan)


def test_close_waits_until_admitted_write_checks_and_publishes(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        staged = threading.Event()
        release_staging = threading.Event()
        close_waiting = threading.Event()
        close_returned = threading.Event()
        write_results: list[httpx.Response] = []
        close_results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary
        original_exclusive = supervisor._acquire_workspace_write_exclusive

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release the staged write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observe_close_wait(workspace: str, timeout: float) -> bool:
            close_waiting.set()
            return bool(original_exclusive(workspace, timeout))

        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)
        monkeypatch.setattr(supervisor, "_acquire_workspace_write_exclusive", observe_close_wait)

        def write_secret() -> None:
            with running.client() as client:
                write_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-blocked.md",
                        b"credential: " + SECRET,
                        "m24-r1e-blocked-write",
                    )
                )

        def close_session() -> None:
            try:
                with running.client() as client:
                    close_results.append(_close(running, client))
            finally:
                close_returned.set()

        writer = threading.Thread(target=write_secret, name="r1e-writer")
        closer = threading.Thread(target=close_session, name="r1e-closer")
        writer.start()
        try:
            assert staged.wait(timeout=10), "write did not acquire its lease before staging"
            closer.start()
            assert close_waiting.wait(timeout=10), "close did not request workspace exclusion"
            assert not close_returned.is_set(), "close returned while the write was suspended"
            assert handle.secret_forms, "close erased forms before the write check"

            release_staging.set()
            writer.join(timeout=10)
            closer.join(timeout=10)
            assert not writer.is_alive()
            assert not closer.is_alive()
            assert write_results and write_results[0].status_code == 403
            assert close_results and close_results[0].status_code == 200
            assert handle.secret_forms == ()
            assert not (running.host_path / "notes/humaines/r1e-blocked.md").exists()
        finally:
            release_staging.set()
            writer.join(timeout=10)
            if closer.ident is not None:
                closer.join(timeout=10)


def test_prepare_adds_forms_while_write_is_staged_and_checks_it(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_secret = hashlib.sha256(b"second session R1e synthetic secret").digest()
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        second_handle = None
        staged = threading.Event()
        release_staging = threading.Event()
        prepare_returned = threading.Event()
        write_results: list[httpx.Response] = []
        prepare_results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release the staged write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)

        def write_secret() -> None:
            with running.client() as client:
                write_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-late-session.md",
                        b"credential: " + second_secret,
                        "m24-r1e-late-session-write",
                    )
                )

        def prepare_second() -> None:
            try:
                with running.client() as client:
                    prepare_results.append(
                        post_session(
                            client,
                            running,
                            "/v1/sessions",
                            prepare_payload(
                                workspace_id,
                                session_id=SECOND_SESSION_ID,
                                task_id=SECOND_TASK_ID,
                                auth_secret=second_secret,
                            ),
                            "m24-r1e-prepare-second",
                        )
                    )
            finally:
                prepare_returned.set()

        writer = threading.Thread(target=write_secret, name="r1e-late-writer")
        preparer = threading.Thread(target=prepare_second, name="r1e-late-prepare")
        writer.start()
        try:
            assert staged.wait(timeout=10)
            preparer.start()
            assert prepare_returned.wait(timeout=10), "prepare waited for a staged write"
            assert prepare_results and prepare_results[0].status_code == 200
            second_handle = supervisor._handle(SECOND_SESSION_ID)
            assert second_handle.secret_forms
            assert writer.is_alive()

            release_staging.set()
            writer.join(timeout=10)
            preparer.join(timeout=10)
            assert not writer.is_alive() and not preparer.is_alive()
            assert write_results and write_results[0].status_code == 403

            with running.client() as client:
                later_write = _put_file(
                    running,
                    client,
                    workspace_id,
                    "notes/humaines/r1e-late-session-after-prepare.md",
                    b"credential: " + second_secret,
                    "m24-r1e-late-session-after-prepare",
                )
                closed = _close(running, client, SECOND_SESSION_ID, "m24-r1e-close-second")
            assert later_write.status_code == 403
            assert closed.status_code == 200
            assert second_handle.secret_forms == ()
            assert not (running.host_path / "notes/humaines/r1e-late-session.md").exists()
            assert not (
                running.host_path / "notes/humaines/r1e-late-session-after-prepare.md"
            ).exists()
        finally:
            release_staging.set()
            writer.join(timeout=10)
            if preparer.ident is not None:
                preparer.join(timeout=10)


def test_collect_capture_holds_shared_lease_and_collect_after_stop_is_refused(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        note = running.session_dir() / "scratch/notes/captured.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_bytes(b"safe synthetic captured note")
        publication_entered = threading.Event()
        release_publication = threading.Event()
        stop_waiting = threading.Event()
        stop_returned = threading.Event()
        collect_results: list[httpx.Response] = []
        original_commit = files._commit_write
        original_exclusive = supervisor._acquire_workspace_write_exclusive

        def pause_publication(*args: object, **kwargs: object):
            publication_entered.set()
            assert release_publication.wait(timeout=10), "test did not release capture publication"
            return original_commit(*args, **kwargs)  # type: ignore[arg-type]

        def observe_stop_wait(workspace: str, timeout: float) -> bool:
            stop_waiting.set()
            return bool(original_exclusive(workspace, timeout))

        monkeypatch.setattr(files, "_commit_write", pause_publication)
        monkeypatch.setattr(supervisor, "_acquire_workspace_write_exclusive", observe_stop_wait)

        def collect() -> None:
            with running.client() as client:
                collect_results.append(
                    post_session(
                        client,
                        running,
                        f"/v1/sessions/{SESSION_ID}/collect",
                        {"capture": True},
                        COLLECT_KEY,
                    )
                )

        def stop() -> None:
            try:
                supervisor.stop()
            finally:
                stop_returned.set()

        collector = threading.Thread(target=collect, name="r1e-collector")
        stopper = threading.Thread(target=stop, name="r1e-capture-stopper")
        collector.start()
        try:
            assert publication_entered.wait(timeout=10), "collect did not reach capture publication"
            stopper.start()
            assert stop_waiting.wait(timeout=10)
            assert not stop_returned.is_set(), "stop returned while capture held the shared lease"
            assert handle.secret_forms

            release_publication.set()
            collector.join(timeout=10)
            stopper.join(timeout=10)
            assert not collector.is_alive()
            assert not stopper.is_alive()
            assert collect_results and collect_results[0].status_code == 200
            assert stop_returned.is_set()
            assert handle.secret_forms == ()

            late_note = running.session_dir() / "scratch/notes/after-stop.md"
            late_note.parent.mkdir(parents=True, exist_ok=True)
            late_note.write_bytes(b"credential: " + SECRET)
            with running.client() as client:
                after_stop = post_session(
                    client,
                    running,
                    f"/v1/sessions/{SESSION_ID}/collect",
                    {"capture": True},
                    "m24-r1e-collect-after-stop",
                )
            assert after_stop.status_code == 409
            assert after_stop.json()["error"] == "session_state_conflict"
            assert not (running.host_path / "notes/agents" / TASK_ID / "after-stop.md").exists()
        finally:
            release_publication.set()
            collector.join(timeout=10)
            if stopper.ident is not None:
                stopper.join(timeout=10)


def test_closed_or_fresh_workspace_accepts_clean_agent_writes(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        _prepare(running, client, workspace_id)
        closed = _close(running, client)
        assert closed.status_code == 200, closed.text

        after_close = _put_file(
            running,
            client,
            workspace_id,
            "notes/humaines/after-close.md",
            b"clean note after close",
            "m24-r1e-clean-after-close",
        )
        assert after_close.status_code == 201, after_close.text

        created_workspace = _create_second_workspace(running, client)
        fresh_write = _put_file(
            running,
            client,
            created_workspace,
            "notes/humaines/fresh-workspace.md",
            b"clean note in a new workspace",
            "m24-r1e-clean-fresh-workspace",
        )
        assert fresh_write.status_code == 201, fresh_write.text


def test_two_simultaneous_credential_writes_are_both_drained_by_close(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        arrival = threading.Barrier(3)
        release_staging = threading.Event()
        close_waiting = threading.Event()
        close_returned = threading.Event()
        write_results: list[tuple[str, httpx.Response]] = []
        close_results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary
        original_exclusive = supervisor._acquire_workspace_write_exclusive

        def pause_each_staging(*args: object, **kwargs: object):
            arrival.wait(timeout=10)
            assert release_staging.wait(timeout=10), "test did not release both writes"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observe_close_wait(workspace: str, timeout: float) -> bool:
            close_waiting.set()
            return bool(original_exclusive(workspace, timeout))

        monkeypatch.setattr(files, "_stream_to_temporary", pause_each_staging)
        monkeypatch.setattr(supervisor, "_acquire_workspace_write_exclusive", observe_close_wait)

        def write(path: str, key: str) -> None:
            with running.client() as client:
                write_results.append(
                    (
                        path,
                        _put_file(
                            running,
                            client,
                            workspace_id,
                            path,
                            b"credential: " + SECRET,
                            key,
                        ),
                    )
                )

        def close() -> None:
            try:
                with running.client() as client:
                    close_results.append(_close(running, client))
            finally:
                close_returned.set()

        paths = ("notes/humaines/r1e-parallel-1.md", "notes/humaines/r1e-parallel-2.md")
        writers = [
            threading.Thread(target=write, args=(path, f"m24-r1e-parallel-{index}"))
            for index, path in enumerate(paths, start=1)
        ]
        closer = threading.Thread(target=close, name="r1e-parallel-closer")
        for writer in writers:
            writer.start()
        try:
            arrival.wait(timeout=10)
            closer.start()
            assert close_waiting.wait(timeout=10)
            assert not close_returned.is_set()
            release_staging.set()
            for writer in writers:
                writer.join(timeout=10)
            closer.join(timeout=10)
            assert all(not writer.is_alive() for writer in writers)
            assert not closer.is_alive()
            assert len(write_results) == 2
            assert all(response.status_code == 403 for _, response in write_results)
            assert close_results and close_results[0].status_code == 200
            assert all(not (running.host_path / path).exists() for path in paths)
        finally:
            release_staging.set()
            for writer in writers:
                writer.join(timeout=10)
            if closer.ident is not None:
                closer.join(timeout=10)


def test_non_ui_credential_write_is_refused(session_base: Path) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        _prepare(running, client, workspace_id)
        path = "notes/humaines/r1e-credential.md"
        response = _put_file(
            running,
            client,
            workspace_id,
            path,
            b"synthetic credential: " + SECRET,
            "m24-r1e-credential-refused",
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"] == "policy_denied"
        assert response.json()["details"]["reason"] == "secret_exposure_detected"
        assert not (running.host_path / path).exists()


def test_closed_session_secret_no_longer_protected_by_design(session_base: Path) -> None:
    """Closed session secret no longer protected by design — see M24-2 §10."""
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        refused = _put_file(
            running,
            client,
            workspace_id,
            "notes/humaines/revocation.md",
            b"credential: " + SECRET,
            "m24-r1e-revoke-write",
        )
        assert refused.status_code == 403
        assert handle.record.outcome == "secret_exposure_detected"
        assert handle.secret_forms

        closed = _close(running, client)
        assert closed.status_code == 200, closed.text
        assert handle.secret_forms == ()
        after_close = _put_file(
            running,
            client,
            workspace_id,
            "notes/humaines/after-revoke-close.md",
            b"synthetic credential: " + SECRET,
            "m24-r1e-after-revoke-close",
        )
        assert after_close.status_code == 201, after_close.text


def test_prepare_does_not_wait_for_a_write_that_already_passed_its_check(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
        supervisor = _supervisor(running)
        checked = threading.Event()
        release_check = threading.Event()
        prepare_returned = threading.Event()
        write_results: list[httpx.Response] = []
        prepare_results: list[httpx.Response] = []
        original_check = supervisor.check_non_ui_file_write

        def pause_after_check(*args: object, **kwargs: object) -> None:
            original_check(*args, **kwargs)  # type: ignore[arg-type]
            checked.set()
            assert release_check.wait(timeout=10), "test did not release the checked write"

        monkeypatch.setattr(supervisor, "check_non_ui_file_write", pause_after_check)

        def write_secret() -> None:
            with running.client() as client:
                write_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-prepare-postcheck.md",
                        b"credential: " + PREPARE_RACE_SECRET,
                        "m24-r1e-prepare-postcheck-write",
                    )
                )

        def prepare() -> None:
            try:
                with running.client() as client:
                    prepare_results.append(
                        post_session(
                            client,
                            running,
                            "/v1/sessions",
                            prepare_payload(
                                workspace_id,
                                session_id=SECOND_SESSION_ID,
                                task_id=SECOND_TASK_ID,
                                auth_secret=PREPARE_RACE_SECRET,
                            ),
                            "m24-r1e-prepare-postcheck",
                        )
                    )
            finally:
                prepare_returned.set()

        writer = threading.Thread(target=write_secret, name="r1e-prepare-postcheck-writer")
        preparer = threading.Thread(target=prepare, name="r1e-prepare-postcheck-prepare")
        writer.start()
        try:
            assert checked.wait(timeout=10), "write did not reach its secret check"
            preparer.start()
            assert prepare_returned.wait(timeout=10), "prepare drained an already checked write"
            assert prepare_results and prepare_results[0].status_code == 200
            assert supervisor._handle(SECOND_SESSION_ID).secret_forms
            assert writer.is_alive()

            release_check.set()
            writer.join(timeout=10)
            preparer.join(timeout=10)
            assert not writer.is_alive() and not preparer.is_alive()
            assert write_results and write_results[0].status_code == 201
            with running.client() as client:
                refused = _put_file(
                    running,
                    client,
                    workspace_id,
                    "notes/humaines/r1e-prepare-after.md",
                    b"credential: " + PREPARE_RACE_SECRET,
                    "m24-r1e-prepare-postcheck-after",
                )
            assert refused.status_code == 403, refused.text
        finally:
            release_check.set()
            writer.join(timeout=10)
            if preparer.ident is not None:
                preparer.join(timeout=10)


def test_launch_does_not_wait_for_a_write_that_already_passed_its_check(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        checked = threading.Event()
        release_check = threading.Event()
        launch_returned = threading.Event()
        write_results: list[httpx.Response] = []
        launch_results: list[httpx.Response] = []
        original_check = supervisor.check_non_ui_file_write

        def pause_after_check(*args: object, **kwargs: object) -> None:
            original_check(*args, **kwargs)  # type: ignore[arg-type]
            checked.set()
            assert release_check.wait(timeout=10), "test did not release the checked write"

        monkeypatch.setattr(supervisor, "check_non_ui_file_write", pause_after_check)
        _seed_provider_credential_on_launch(monkeypatch, supervisor)

        def write_secret() -> None:
            with running.client() as client:
                write_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-launch-postcheck.md",
                        b"provider credential: " + LAUNCH_RACE_SECRET,
                        "m24-r1e-launch-postcheck-write",
                    )
                )

        def launch() -> None:
            try:
                with running.client() as client:
                    launch_results.append(
                        post_session(
                            client,
                            running,
                            f"/v1/sessions/{SESSION_ID}/start",
                            {},
                            "m24-r1e-launch-postcheck",
                        )
                    )
            finally:
                launch_returned.set()

        writer = threading.Thread(target=write_secret, name="r1e-launch-postcheck-writer")
        launcher = threading.Thread(target=launch, name="r1e-launch-postcheck-launch")
        writer.start()
        try:
            assert checked.wait(timeout=10), "write did not reach its secret check"
            launcher.start()
            assert launch_returned.wait(timeout=10), "launch drained an already checked write"
            assert launch_results and launch_results[0].status_code == 200
            assert LAUNCH_RACE_SECRET[:18] in handle.provider_credential_forms
            assert writer.is_alive()

            release_check.set()
            writer.join(timeout=10)
            launcher.join(timeout=10)
            assert not writer.is_alive() and not launcher.is_alive()
            assert write_results and write_results[0].status_code == 201
            with running.client() as client:
                refused = _put_file(
                    running,
                    client,
                    workspace_id,
                    "notes/humaines/r1e-launch-after.md",
                    b"provider credential: " + LAUNCH_RACE_SECRET,
                    "m24-r1e-launch-postcheck-after",
                )
            assert refused.status_code == 403, refused.text
        finally:
            release_check.set()
            writer.join(timeout=10)
            if launcher.ident is not None:
                launcher.join(timeout=10)


def test_prepare_adds_forms_without_draining_admitted_writes(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
        supervisor = _supervisor(running)
        release_write = supervisor._acquire_workspace_write_shared(workspace_id)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)
                with running.client() as client:
                    prepared = post_session(
                        client,
                        running,
                        "/v1/sessions",
                        prepare_payload(
                            workspace_id,
                            session_id=SECOND_SESSION_ID,
                            task_id=SECOND_TASK_ID,
                            auth_secret=PREPARE_RACE_SECRET,
                        ),
                        "m24-r1e-prepare-timeout",
                    )
            assert prepared.status_code == 200, prepared.text
            assert supervisor._handle(SECOND_SESSION_ID).secret_forms
        finally:
            release_write()

        with running.client() as client:
            refused = _put_file(
                running,
                client,
                workspace_id,
                "notes/humaines/r1e-prepare-no-drain.md",
                b"credential: " + PREPARE_RACE_SECRET,
                "m24-r1e-prepare-no-drain-write",
            )
        assert refused.status_code == 403, refused.text


def test_launch_adds_provider_forms_without_draining_admitted_writes(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        _seed_provider_credential_on_launch(monkeypatch, supervisor)
        release_write = supervisor._acquire_workspace_write_shared(workspace_id)
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)
                with running.client() as client:
                    started = post_session(
                        client,
                        running,
                        f"/v1/sessions/{SESSION_ID}/start",
                        {},
                        "m24-r1e-launch-timeout",
                    )
            assert started.status_code == 200, started.text
            assert LAUNCH_RACE_SECRET[:18] in handle.provider_credential_forms
        finally:
            release_write()

        with running.client() as client:
            refused = _put_file(
                running,
                client,
                workspace_id,
                "notes/humaines/r1e-launch-no-drain.md",
                b"provider credential: " + LAUNCH_RACE_SECRET,
                "m24-r1e-launch-no-drain-write",
            )
        assert refused.status_code == 403, refused.text


def test_prepare_and_launch_without_in_flight_writes_keep_current_behavior(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(workspace_id),
            "m24-r1e-idle-prepare",
        )
        assert prepared.status_code == 200, prepared.text
        launched = post_session(
            client,
            running,
            f"/v1/sessions/{SESSION_ID}/start",
            {},
            "m24-r1e-idle-launch",
        )
        assert launched.status_code == 200, launched.text


def test_stop_timeout_cleans_all_sessions_and_reports_non_drained_ids(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.wait_timeout: float | None = None

        def wait(self, timeout: float) -> None:
            self.wait_timeout = timeout

    with running_session_agent(session_base) as running:
        with running.client() as client:
            first_workspace = running.ensure_workspace(client)
            second_workspace = _create_second_workspace(running, client)
            _prepare(running, client, first_workspace)
            _prepare(
                running,
                client,
                second_workspace,
                session_id=SECOND_SESSION_ID,
                task_id=SECOND_TASK_ID,
                key="m24-r1e-stop-prepare-second",
            )
        supervisor = _supervisor(running)
        first = supervisor._handle(SESSION_ID)
        second = supervisor._handle(SECOND_SESSION_ID)
        process_objects = [FakeProcess(), FakeProcess()]
        first.managed = SimpleNamespace(pgid=101, process=process_objects[0])
        second.managed = SimpleNamespace(pgid=102, process=process_objects[1])
        signaled: list[int] = []
        monkeypatch.setattr(
            sessions_supervisor.process,
            "signal_group",
            lambda pgid, _sig: signaled.append(pgid),
        )
        credentials: list[Path] = []
        for handle in (first, second):
            for relative in sessions_supervisor.PROFILE_CREDENTIAL_COPY_PATHS:
                credential = handle.directory / relative
                credential.parent.mkdir(parents=True, exist_ok=True)
                credential.write_text(
                    '{"access_token":"synthetic-provider-credential-for-r1e"}',
                    encoding="utf-8",
                )
                credentials.append(credential)

        release_first = supervisor._acquire_workspace_write_shared(first_workspace)
        try:
            monkeypatch.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)
            with pytest.raises(SessionWriteDrainTimeoutError) as excinfo:
                supervisor.stop()
            assert excinfo.value.details == {
                "operation": "stop",
                "non_drained": [{"session_id": SESSION_ID, "workspace_id": first_workspace}],
            }
            assert set(signaled) == {101, 102}
            assert all(item.wait_timeout == 0 for item in process_objects)
            assert first.managed is None and second.managed is None
            assert first.secret is None and second.secret is None
            assert first.secret_forms
            assert second.secret_forms == ()
            assert all(handle.relay is None and handle.broker is None for handle in (first, second))
            assert all(not path.exists() for path in credentials)
            assert not first.scratch.exists() and not second.scratch.exists()
            assert all(
                not (handle.directory / "ipc" / "shim.sock").exists() for handle in (first, second)
            )
            assert all(
                not (handle.directory / "prompt.json").exists() for handle in (first, second)
            )

            with pytest.raises(SessionWriteAdmissionError):
                supervisor.prepare(
                    sessions_supervisor.PrepareSessionRequest.from_payload(
                        prepare_payload(
                            second_workspace,
                            session_id=THIRD_SESSION_ID,
                            task_id=THIRD_TASK_ID,
                        )
                    ),
                    idempotency_key="m24-r1e-prepare-after-stop",
                    body_sha256="a" * 64,
                )
            with pytest.raises(SessionWriteAdmissionError):
                supervisor.start(
                    SESSION_ID,
                    sessions_supervisor.StartSessionRequest(),
                    idempotency_key="m24-r1e-start-after-stop",
                    body_sha256="b" * 64,
                )
            with pytest.raises(SessionWriteAdmissionError):
                supervisor.resume(
                    SESSION_ID,
                    sessions_supervisor.ResumeRequest(reason="after stop"),
                    idempotency_key="m24-r1e-resume-after-stop",
                    body_sha256="c" * 64,
                )
        finally:
            release_first()
        # A retry after the outstanding lease exits can safely clear its forms.
        supervisor.stop()
        assert first.secret_forms == ()


def test_stop_kills_and_disables_egress_before_waiting_for_workspace_write(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeProcess:
        def wait(self, timeout: float) -> None:
            del timeout

    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        handle.record.state = sessions_supervisor.SessionState.RUNNING
        handle.managed = SimpleNamespace(pgid=101, process=FakeProcess())
        kill_sent = threading.Event()
        staged = threading.Event()
        release_staging = threading.Event()
        stop_waiting = threading.Event()
        write_results: list[httpx.Response] = []
        original_exclusive = supervisor._acquire_workspace_write_exclusive

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release the staged write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observe_kill(pgid: int, requested_signal: int) -> bool:
            assert pgid == 101
            assert requested_signal == signal.SIGKILL
            kill_sent.set()
            return True

        def observe_exclusive(workspace: str, timeout: float) -> bool:
            stop_waiting.set()
            return bool(original_exclusive(workspace, timeout))

        original_stream = files._stream_to_temporary
        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)
        monkeypatch.setattr(sessions_supervisor.process, "signal_group", observe_kill)
        monkeypatch.setattr(supervisor, "_acquire_workspace_write_exclusive", observe_exclusive)

        def write_secret() -> None:
            with running.client() as client:
                write_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-stop-barrier.md",
                        b"credential: " + SECRET,
                        "m24-r1e-stop-barrier-write",
                    )
                )

        stop_errors: list[BaseException] = []

        def stop() -> None:
            try:
                supervisor.stop()
            except BaseException as exc:
                stop_errors.append(exc)

        writer = threading.Thread(target=write_secret, name="r1e-stop-barrier-writer")
        stopper = threading.Thread(target=stop, name="r1e-stop-barrier-stopper")
        writer.start()
        try:
            assert staged.wait(timeout=10), "write did not acquire its lease before staging"
            stopper.start()
            assert stop_waiting.wait(timeout=10), "stop did not request workspace exclusion"
            assert kill_sent.is_set(), "stop waited for the workspace write before SIGKILL"
            assert supervisor._stopped.is_set()
            assert not handle.channels.egress_allowed()
            assert handle.secret_forms, "stop erased detection forms before the write drained"

            release_staging.set()
            writer.join(timeout=10)
            stopper.join(timeout=10)
            assert not writer.is_alive()
            assert not stopper.is_alive()
            assert not stop_errors
            assert write_results and write_results[0].status_code == 403
            assert not (running.host_path / "notes/humaines/r1e-stop-barrier.md").exists()
            assert handle.secret_forms == ()
        finally:
            release_staging.set()
            writer.join(timeout=10)
            if stopper.ident is not None:
                stopper.join(timeout=10)


def test_stop_uses_one_global_deadline_across_workspaces(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            first_workspace = running.ensure_workspace(client)
            second_workspace = _create_second_workspace(running, client)
            _prepare(running, client, first_workspace)
            _prepare(
                running,
                client,
                second_workspace,
                session_id=SECOND_SESSION_ID,
                task_id=SECOND_TASK_ID,
                key="m24-r1e-deadline-prepare-second",
            )
        supervisor = _supervisor(running)
        now = [0.0]
        acquisitions: list[tuple[str, float]] = []

        def fake_monotonic() -> float:
            return now[0]

        def fail_acquisition(workspace_id: str, timeout: float) -> bool:
            acquisitions.append((workspace_id, timeout))
            now[0] += 3.0
            return False

        with monkeypatch.context() as scoped:
            scoped.setattr(sessions_supervisor.time, "monotonic", fake_monotonic)
            scoped.setattr(supervisor, "_acquire_workspace_write_exclusive", fail_acquisition)
            with pytest.raises(SessionWriteDrainTimeoutError):
                supervisor.stop()

        assert {workspace_id for workspace_id, _ in acquisitions} == {
            first_workspace,
            second_workspace,
        }
        timeouts = [timeout for _, timeout in acquisitions]
        assert timeouts[0] == 5.0
        assert timeouts[1] == 2.0


def test_lost_session_secret_no_longer_protected_by_design(
    session_base: Path,
) -> None:
    """Lost session secret no longer protected by design — see M24-2 §10."""
    with running_session_agent(session_base) as first:
        with first.client() as client:
            workspace_id = first.ensure_workspace(client)
            _prepare(first, client, workspace_id)

    with running_session_agent(session_base) as second:
        new_secret = hashlib.sha256(b"new supervisor R1e synthetic secret").digest()
        with second.client() as client:
            lost_write = _put_file(
                second,
                client,
                workspace_id,
                "notes/humaines/r1e-lost-session.md",
                b"credential from lost session: " + SECRET,
                "m24-r1e-lost-session-write",
            )
            assert lost_write.status_code == 201, lost_write.text

            _prepare(
                second,
                client,
                workspace_id,
                session_id=SECOND_SESSION_ID,
                task_id=SECOND_TASK_ID,
                secret=new_secret,
                key="m24-r1e-restart-prepare-new",
            )
            refused = _put_file(
                second,
                client,
                workspace_id,
                "notes/humaines/r1e-new-session.md",
                b"credential from new session: " + new_secret,
                "m24-r1e-new-session-write",
            )
            assert refused.status_code == 403
            assert not (second.host_path / "notes/humaines/r1e-new-session.md").exists()


def test_failed_publication_releases_shared_lease_and_close_can_retry(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        publication_entered = threading.Event()
        release_publication = threading.Event()
        close_waiting = threading.Event()
        close_returned = threading.Event()
        failed_results: list[httpx.Response] = []
        close_results: list[httpx.Response] = []
        original_exclusive = supervisor._acquire_workspace_write_exclusive

        def fail_publication(*_args: object, **_kwargs: object):
            publication_entered.set()
            assert release_publication.wait(timeout=10), "test did not release failed publication"
            raise files.FileStorageError("synthetic publication failure")

        def observe_close_wait(workspace: str, timeout: float) -> bool:
            close_waiting.set()
            return bool(original_exclusive(workspace, timeout))

        monkeypatch.setattr(files, "_commit_write", fail_publication)
        monkeypatch.setattr(supervisor, "_acquire_workspace_write_exclusive", observe_close_wait)

        def failed_write() -> None:
            with running.client() as client:
                failed_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-failed-publication.md",
                        b"clean synthetic body",
                        "m24-r1e-failed-publication",
                    )
                )

        def close() -> None:
            try:
                with running.client() as client:
                    close_results.append(_close(running, client))
            finally:
                close_returned.set()

        writer = threading.Thread(target=failed_write, name="r1e-failing-writer")
        closer = threading.Thread(target=close, name="r1e-failing-closer")
        writer.start()
        try:
            assert publication_entered.wait(timeout=10)
            closer.start()
            assert close_waiting.wait(timeout=10)
            assert not close_returned.is_set()
            release_publication.set()
            writer.join(timeout=10)
            closer.join(timeout=10)
            assert not writer.is_alive() and not closer.is_alive()
            assert failed_results and failed_results[0].status_code == 500
            assert close_results and close_results[0].status_code == 200
            assert not (running.host_path / "notes/humaines/r1e-failed-publication.md").exists()
            assert workspace_id not in supervisor._workspace_write_gates
        finally:
            release_publication.set()
            writer.join(timeout=10)
            if closer.ident is not None:
                closer.join(timeout=10)


def test_close_timeout_keeps_forms_and_retry_succeeds(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        staged = threading.Event()
        release_staging = threading.Event()
        write_results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release timed-out write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)

        def write_secret() -> None:
            with running.client() as client:
                write_results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-timeout.md",
                        b"credential: " + SECRET,
                        "m24-r1e-timeout-write",
                    )
                )

        writer = threading.Thread(target=write_secret, name="r1e-timeout-writer")
        writer.start()
        try:
            assert staged.wait(timeout=10)
            monkeypatch.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)
            with running.client() as client:
                timed_out = _close(running, client)
            assert timed_out.status_code == 409
            assert timed_out.json()["error"] == "session_busy"
            assert timed_out.json()["details"] == {
                "operation": "close",
                "workspace_id": workspace_id,
            }
            assert handle.secret_forms
            assert handle.record.state.value == "ready"

            release_staging.set()
            writer.join(timeout=10)
            assert not writer.is_alive()
            assert write_results and write_results[0].status_code == 403
            with running.client() as client:
                retried = _close(running, client)
            assert retried.status_code == 200, retried.text
            assert handle.secret_forms == ()
        finally:
            release_staging.set()
            writer.join(timeout=10)


def test_stop_timeout_captures_refreshed_provider_forms_before_purge(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOWORK_FAKE_CLI_SCENARIO", "hang")
    refreshed = b"rotated-token-Q7L2x9-A1m8-D4k6"
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        _seed_provider_credential_on_launch(monkeypatch, supervisor)
        with running.client() as client:
            started = post_session(
                client,
                running,
                f"/v1/sessions/{SESSION_ID}/start",
                {},
                "m24-r1e-stop-refreshed-start",
            )
        assert started.status_code == 200, started.text
        handle = supervisor._handle(SESSION_ID)
        credential = handle.directory / sessions_supervisor.PROFILE_CREDENTIAL_COPY_PATHS[0]
        assert handle.managed is not None and credential.is_file()
        credential.write_bytes(b'{"access_token":"' + refreshed + b'"}')
        assert all(refreshed[:18] not in form for form in handle.provider_credential_forms)

        staged = threading.Event()
        release_staging = threading.Event()
        results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release the staged write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)

        def write_refreshed_token() -> None:
            with running.client() as client:
                results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-stop-refreshed.md",
                        b"credential: " + refreshed,
                        "m24-r1e-stop-refreshed-write",
                    )
                )

        writer = threading.Thread(target=write_refreshed_token, name="r1e-stop-refreshed-writer")
        writer.start()
        try:
            assert staged.wait(timeout=10), "write did not stage before stop"
            monkeypatch.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)
            with pytest.raises(SessionWriteDrainTimeoutError):
                supervisor.stop()
            assert not credential.exists()
            assert any(refreshed[:18] in form for form in handle.provider_credential_forms)

            release_staging.set()
            writer.join(timeout=10)
            assert not writer.is_alive()
            assert results and results[0].status_code == 403, results[0].text if results else ""
            assert not (running.host_path / "notes/humaines/r1e-stop-refreshed.md").exists()
        finally:
            release_staging.set()
            writer.join(timeout=10)


def test_interrupt_signals_while_workspace_write_is_suspended(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOWORK_FAKE_CLI_SCENARIO", "hang")
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
            started = post_session(
                client,
                running,
                f"/v1/sessions/{SESSION_ID}/start",
                {},
                "m24-r1e-interrupt-start",
            )
        assert started.status_code == 200, started.text
        supervisor = _supervisor(running)
        handle = supervisor._handle(SESSION_ID)
        assert handle.managed is not None
        staged = threading.Event()
        release_staging = threading.Event()
        signal_sent = threading.Event()
        results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary
        original_signal_group = sessions_supervisor.process.signal_group

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release the staged write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observe_signal(pgid: int, requested_signal: int) -> bool:
            sent = original_signal_group(pgid, requested_signal)
            if requested_signal == signal.SIGTERM:
                signal_sent.set()
            return bool(sent)

        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)
        monkeypatch.setattr(sessions_supervisor.process, "signal_group", observe_signal)

        def write_secret() -> None:
            with running.client() as client:
                results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-interrupt.md",
                        b"credential: " + SECRET,
                        "m24-r1e-interrupt-write",
                    )
                )

        writer = threading.Thread(target=write_secret, name="r1e-interrupt-writer")
        writer.start()
        try:
            assert staged.wait(timeout=10), "write did not stage before interrupt"
            monkeypatch.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)
            with running.client() as client:
                interrupted = post_session(
                    client,
                    running,
                    f"/v1/sessions/{SESSION_ID}/interrupt",
                    {"reason": "test"},
                    "m24-r1e-interrupt-while-write",
                )
            assert signal_sent.is_set(), "interrupt did not send SIGTERM before the write resumed"
            assert interrupted.status_code == 200, interrupted.text
            assert interrupted.json() == {
                "state": "interrupt_requested",
                "signal_sent": True,
            }

            release_staging.set()
            writer.join(timeout=10)
            assert not writer.is_alive()
            assert results and results[0].status_code == 403, results[0].text if results else ""
            assert not (running.host_path / "notes/humaines/r1e-interrupt.md").exists()
        finally:
            release_staging.set()
            writer.join(timeout=10)


def test_monitor_finalizes_after_bounded_drain_with_forms_retained(
    session_base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOWORK_FAKE_CLI_SCENARIO", "hang")
    refreshed = b"rotated-token-Monitor-Q7L2x9"
    with running_session_agent(session_base) as running:
        with running.client() as client:
            workspace_id = running.ensure_workspace(client)
            _prepare(running, client, workspace_id)
        supervisor = _supervisor(running)
        _seed_provider_credential_on_launch(monkeypatch, supervisor)
        with running.client() as client:
            started = post_session(
                client,
                running,
                f"/v1/sessions/{SESSION_ID}/start",
                {},
                "m24-r1e-monitor-start",
            )
        assert started.status_code == 200, started.text
        handle = supervisor._handle(SESSION_ID)
        credential = handle.directory / sessions_supervisor.PROFILE_CREDENTIAL_COPY_PATHS[0]
        assert handle.managed is not None and credential.is_file()
        credential.write_bytes(b'{"access_token":"' + refreshed + b'"}')

        staged = threading.Event()
        release_staging = threading.Event()
        finalized = threading.Event()
        results: list[httpx.Response] = []
        original_stream = files._stream_to_temporary
        original_finalize = supervisor._finalize_exit

        def pause_staging(*args: object, **kwargs: object):
            staged.set()
            assert release_staging.wait(timeout=10), "test did not release the staged write"
            return original_stream(*args, **kwargs)  # type: ignore[arg-type]

        def observe_finalize(*args: object, **kwargs: object) -> None:
            original_finalize(*args, **kwargs)
            finalized.set()

        monkeypatch.setattr(files, "_stream_to_temporary", pause_staging)
        monkeypatch.setattr(supervisor, "_finalize_exit", observe_finalize)
        monkeypatch.setattr(sessions_supervisor, "WRITE_DRAIN_TIMEOUT_SECONDS", 0.0)

        def write_refreshed_token() -> None:
            with running.client() as client:
                results.append(
                    _put_file(
                        running,
                        client,
                        workspace_id,
                        "notes/humaines/r1e-monitor.md",
                        b"credential: " + refreshed,
                        "m24-r1e-monitor-write",
                    )
                )

        writer = threading.Thread(target=write_refreshed_token, name="r1e-monitor-writer")
        writer.start()
        try:
            assert staged.wait(timeout=10), "write did not stage before process exit"
            managed = handle.managed
            assert managed is not None
            assert sessions_supervisor.process.signal_group(managed.pgid, signal.SIGKILL)
            assert finalized.wait(timeout=5), "monitor waited for the suspended workspace write"
            assert not credential.exists()
            assert any(refreshed[:18] in form for form in handle.provider_credential_forms)

            release_staging.set()
            writer.join(timeout=10)
            assert not writer.is_alive()
            assert results and results[0].status_code == 403, results[0].text if results else ""
            assert not (running.host_path / "notes/humaines/r1e-monitor.md").exists()
        finally:
            release_staging.set()
            writer.join(timeout=10)
            for thread in list(handle.threads):
                if thread is not threading.current_thread():
                    thread.join(timeout=10)
