"""W8 shared contract and the real UDS end-to-end path (design §9, ticket W8).

Two halves, both offline and rootless:

* the golden wire documents under ``fixtures/contract_v1`` are parsed by the
  sidecar's stdlib codecs and by the backend's strict Pydantic DTOs with the
  same accept/reject outcome (unknown fields, non-octal ``mode``, a closed
  error-code set, the 409 ``cursor_expired`` envelope);
* a real ``ThreadingHTTPServer`` on a tmpdir AF_UNIX socket is driven by the
  real :class:`HttpWorkspaceAgentClient` and the real
  :class:`WorkspaceIngestService` over in-memory canonical repositories.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx
import pytest
from pydantic import ValidationError
from sidecars.workspace_agent import events, index, protocol, samples, scan, workspace
from sidecars.workspace_agent.agent import SOCKET_FILE_NAME, AgentConfig, WorkspaceAgent
from sidecars.workspace_agent.auth import TOKEN_FILE_NAME

from cti_app.application.investigation_persistence import (
    InMemoryWorkspaceBindingRepository,
    InMemoryWorkspaceFileEventRepository,
    InMemoryWorkspaceFileRevisionRepository,
    InMemoryWorkspaceSyncCursorRepository,
)
from cti_app.application.workspace_agent import (
    EnsureWorkspaceV1,
    EventsPageV1,
    EventV1,
    FileStateV1,
    SampleMaterializationV1,
    SnapshotPageV1,
    WorkspaceAgentErrorEnvelopeV1,
    WorkspaceBindingV1,
    WorkspaceCursorExpiredError,
    WorkspaceIngestError,
    WorkspaceIngestService,
    WorkspaceRevisionConflictError,
    WorkspaceV1,
    WriteResultV1,
    error_from_envelope,
    workspace_revision_id,
)
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient
from tests.workspace_agent.conftest import REPOSITORY_ROOT  # noqa: F401  (sys.path insert)

FIXTURE_DIRECTORY = Path(__file__).resolve().parent / "fixtures" / "contract_v1"
FIXTURE_NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)

WORKSPACE_ID = "ws-78593a79e6cef517466c70b66fcf25eb"
NOTE_PATH = "notes/humaines/premier-constat.md"
NOTE_CONTENT = b"# premier constat\n\nsynthetic note\n"
SAMPLE_CONTENT = b"MZ" + bytes(range(16))
GENERATED_PATH = "plans/plan-v1.json"
DELIVERY_CODE = "IR2609"
ARTICLE_SLUG = "bluemoon-cn"


def load_fixture(name: str) -> dict[str, object]:
    payload = json.loads((FIXTURE_DIRECTORY / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict), name
    return payload


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def without(payload: dict[str, object], field: str) -> dict[str, object]:
    mutated = dict(payload)
    del mutated[field]
    return mutated


def with_extra(payload: dict[str, object], field: str = "unexpected") -> dict[str, object]:
    mutated = dict(payload)
    mutated[field] = True
    return mutated


def int_field(payload: dict[str, object], field: str) -> int:
    value = payload[field]
    assert isinstance(value, int) and not isinstance(value, bool), field
    return value


# -- sidecar-side codecs ------------------------------------------------------


def test_golden_ensure_request_is_parsed_by_both_sides() -> None:
    golden = load_fixture("ensure_workspace")
    request = workspace.EnsureWorkspaceRequest.from_mapping(golden)
    descriptor = EnsureWorkspaceV1.model_validate(golden)

    assert request.workspace_id == WORKSPACE_ID
    assert workspace.workspace_id_for(DELIVERY_CODE, ARTICLE_SLUG) == WORKSPACE_ID
    assert request.delivery_code == descriptor.delivery_code == DELIVERY_CODE
    assert request.article_slug == descriptor.article_slug == ARTICLE_SLUG
    assert request.subject_id == str(descriptor.subject_id)
    assert request.investigation_id == str(descriptor.investigation_id)
    assert request.synthesis_sha256 == descriptor.synthesis_sha256

    for mutated in (
        with_extra(golden),
        without(golden, "subject_id"),
        {**golden, "delivery_code": "ir2609"},
        {**golden, "article_slug": "Bluemoon_CN"},
        {**golden, "investigation_id": "not-a-uuid"},
        {**golden, "synthesis_sha256": "C" * 64},
    ):
        with pytest.raises(workspace.InvalidWorkspaceRequestError):
            workspace.EnsureWorkspaceRequest.from_mapping(mutated)
        with pytest.raises(ValidationError):
            EnsureWorkspaceV1.model_validate(mutated)


def test_golden_workspace_descriptor_is_parsed_by_both_sides(tmp_path: Path) -> None:
    golden = load_fixture("workspace")
    parsed = WorkspaceV1.model_validate(golden)
    assert parsed.workspace_id == parsed.manifest.workspace_id == WORKSPACE_ID
    assert parsed.host_path == golden["host_path"]

    root = tmp_path / "root"
    workspace_dir = root / DELIVERY_CODE / ARTICLE_SLUG
    metadata = workspace_dir / ".autowork"
    metadata.mkdir(parents=True)
    manifest = golden["manifest"]
    assert isinstance(manifest, dict)
    (metadata / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    descriptor = workspace.WorkspaceManager(root).get_workspace(WORKSPACE_ID)
    assert descriptor.manifest == manifest
    assert descriptor.created is False

    golden_workspace_id = golden["workspace_id"]
    golden_host_path = golden["host_path"]
    golden_created = golden["created"]
    assert isinstance(golden_workspace_id, str)
    assert isinstance(golden_host_path, str)
    assert isinstance(golden_created, bool)
    sidecar_descriptor = workspace.WorkspaceDescriptor(
        workspace_id=golden_workspace_id,
        host_path=golden_host_path,
        created=golden_created,
        manifest=manifest,
    )
    assert sidecar_descriptor.workspace_id == parsed.workspace_id
    assert (
        set(sidecar_descriptor.manifest)
        == set(manifest)
        == set(parsed.manifest.model_dump(by_alias=True))
    )

    with pytest.raises(ValidationError):
        WorkspaceV1.model_validate(with_extra(golden))
    with pytest.raises(ValidationError):
        WorkspaceV1.model_validate({**golden, "manifest": with_extra(manifest)})

    (metadata / "manifest.json").write_text(json.dumps(with_extra(manifest)), encoding="utf-8")
    with pytest.raises(workspace.WorkspaceNotFoundError):
        workspace.WorkspaceManager(root).get_workspace(WORKSPACE_ID)


def test_golden_file_state_round_trips_both_sides() -> None:
    golden = load_fixture("file_state")
    state = index.FileState.from_payload(golden)
    assert state.to_wire() == golden
    parsed = FileStateV1.model_validate(golden)
    assert parsed.mode == golden["mode"] == "0640"
    assert set(golden) == set(index.FILE_STATE_FIELDS) == set(FileStateV1.model_fields)

    for bad_mode in (0o640, "0648", "0o640", "640", " 640"):
        mutated = {**golden, "mode": bad_mode}
        with pytest.raises(index.ProjectionError):
            index.FileState.from_payload(mutated)
        with pytest.raises(ValidationError):
            FileStateV1.model_validate(mutated)

    with pytest.raises(index.ProjectionError):
        index.FileState.from_payload(with_extra(golden))
    with pytest.raises(ValidationError):
        FileStateV1.model_validate(with_extra(golden))


def _sync_state_around(event_payload: dict[str, object]) -> dict[str, object]:
    generation = event_payload["generation"]
    seq = event_payload["seq"]
    assert isinstance(generation, int) and isinstance(seq, int)
    return {
        "schema": events.SYNC_STATE_SCHEMA,
        "workspace_id": WORKSPACE_ID,
        "generation": generation,
        "last_seq": seq,
        "oldest_seq": seq,
        "events": [event_payload],
        "idempotency": {},
        "conflicts": [],
        "policy_violations": [],
    }


def test_golden_event_round_trips_both_sides() -> None:
    golden = load_fixture("event")
    state = events.SyncState.from_payload(_sync_state_around(golden), workspace_id=WORKSPACE_ID)
    [event] = state.log.retained
    assert event.to_wire() == golden
    parsed = EventV1.model_validate(golden)
    assert parsed.event_id == golden["event_id"]

    revision_id = events.revision_id_for(WORKSPACE_ID, NOTE_PATH, sha256_of(NOTE_CONTENT))
    assert revision_id == golden["revision_id"]
    assert (
        workspace_revision_id(
            workspace_id=WORKSPACE_ID, path=NOTE_PATH, sha256=sha256_of(NOTE_CONTENT)
        )
        == revision_id
    )
    assert (
        events.event_id_for(
            WORKSPACE_ID, NOTE_PATH, None, revision_id, events.EventKind.FILE_CREATED
        )
        == golden["event_id"]
    )

    broken = with_extra(golden)
    with pytest.raises(events.ProjectionError):
        events.SyncState.from_payload(_sync_state_around(broken), workspace_id=WORKSPACE_ID)
    with pytest.raises(ValidationError):
        EventV1.model_validate(broken)


def test_golden_pages_write_result_and_sample_round_trip_both_sides() -> None:
    events_golden = load_fixture("events_page")
    event_payloads = events_golden["events"]
    assert isinstance(event_payloads, list)
    log = events.EventLog(
        generation=int_field(events_golden, "generation"),
        last_seq=int_field(events_golden, "next_cursor"),
        retained=tuple(
            events.SyncState.from_payload(
                _sync_state_around(item), workspace_id=WORKSPACE_ID
            ).log.retained[0]
            for item in event_payloads
        ),
    )
    page = log.page(after=int_field(events_golden, "next_cursor") - 1, limit=100)
    assert [item.to_wire() for item in page.events] == event_payloads
    assert page.next_cursor == events_golden["next_cursor"]
    assert log.oldest_retained == events_golden["oldest_retained"]
    assert page.resync_required is False
    EventsPageV1.model_validate(events_golden)
    sidecar_page = scan.EventsPage(
        generation=int_field(events_golden, "generation"),
        events=tuple(log.retained),
        next_cursor=int_field(events_golden, "next_cursor"),
        oldest_retained=int_field(events_golden, "oldest_retained"),
        resync_required=False,
    )
    assert sidecar_page.next_cursor == page.next_cursor
    with pytest.raises(TypeError):
        scan.EventsPage(**with_extra(events_golden))
    with pytest.raises(ValidationError):
        EventsPageV1.model_validate(with_extra(events_golden))

    snapshot_golden = load_fixture("snapshot_page")
    files = snapshot_golden["files"]
    assert isinstance(files, list)
    states = tuple(index.FileState.from_payload(item) for item in files if isinstance(item, dict))
    assert [state.to_wire() for state in states] == files
    snapshot_page = scan.SnapshotPage(
        generation=int_field(snapshot_golden, "generation"),
        files=states,
        next_page_token=None,
    )
    assert snapshot_page.generation == snapshot_golden["generation"]
    SnapshotPageV1.model_validate(snapshot_golden)
    with pytest.raises(TypeError):
        scan.SnapshotPage(**with_extra(snapshot_golden))
    with pytest.raises(ValidationError):
        SnapshotPageV1.model_validate(with_extra(snapshot_golden))

    write_golden = load_fixture("write_result")
    assert set(write_golden) == {"revision", "created", "already_present", "conflict"}
    revision_payload = write_golden["revision"]
    assert isinstance(revision_payload, dict)
    record = scan.RevisionRecord(**revision_payload)
    assert record.to_wire() == revision_payload
    write_result = WriteResultV1.model_validate(write_golden)
    assert write_result.revision.revision_id == record.revision_id
    assert write_result.conflict is False
    with pytest.raises(TypeError):
        scan.RevisionRecord(**with_extra(revision_payload))
    with pytest.raises(ValidationError):
        WriteResultV1.model_validate(with_extra(write_golden))

    sample_golden = load_fixture("sample_materialization")
    materialization = samples.SampleMaterialization(**sample_golden)
    assert materialization.as_payload() == sample_golden
    assert materialization.mode == samples.SAMPLE_RESPONSE_MODE == "0440"
    sample_result = SampleMaterializationV1.model_validate(sample_golden)
    assert sample_result.mode == "0440"
    assert sample_result.sha256 == sha256_of(SAMPLE_CONTENT)
    with pytest.raises(TypeError):
        samples.SampleMaterialization(**with_extra(sample_golden))
    with pytest.raises(ValidationError):
        SampleMaterializationV1.model_validate(with_extra(sample_golden))


def test_golden_cursor_expired_envelope_is_the_409_shape_both_sides() -> None:
    golden = load_fixture("error_cursor_expired")
    message = golden["message"]
    details = golden["details"]
    assert isinstance(message, str) and isinstance(details, dict)
    assert (
        protocol.error_envelope(protocol.ErrorCode.CURSOR_EXPIRED, message, details=details)
        == golden
    )
    assert set(golden) == {"error", "message", "retryable", "details"}

    envelope = WorkspaceAgentErrorEnvelopeV1.model_validate(golden)
    error = error_from_envelope(envelope)
    assert isinstance(error, WorkspaceCursorExpiredError)
    assert error.http_status == 409
    assert error.code == "cursor_expired"
    assert error.details["resync_required"] is True
    assert error.retryable is False

    unknown_code = {**golden, "error": "not_a_code"}
    with pytest.raises(ValueError):
        protocol.ErrorCode(str(unknown_code["error"]))
    with pytest.raises(ValidationError):
        WorkspaceAgentErrorEnvelopeV1.model_validate(unknown_code)
    with pytest.raises(ValidationError):
        WorkspaceAgentErrorEnvelopeV1.model_validate(with_extra(golden))


# -- in-memory canonical ingest ----------------------------------------------


class InMemoryWorkspaceUnitOfWork:
    """The four workspace ports of one process-local store (ADR-0011 E2)."""

    def __init__(
        self,
        bindings: InMemoryWorkspaceBindingRepository,
        revisions: InMemoryWorkspaceFileRevisionRepository,
        file_events: InMemoryWorkspaceFileEventRepository,
        cursors: InMemoryWorkspaceSyncCursorRepository,
    ) -> None:
        self.workspace_bindings = bindings
        self.workspace_file_revisions = revisions
        self.workspace_file_events = file_events
        self.workspace_sync_cursors = cursors

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def commit(self) -> None:
        return None


class InMemoryWorkspaceStore:
    """A shared store whose every ingest transaction sees the same rows."""

    def __init__(self) -> None:
        self.workspace_bindings = InMemoryWorkspaceBindingRepository()
        self.workspace_file_revisions = InMemoryWorkspaceFileRevisionRepository()
        self.workspace_file_events = InMemoryWorkspaceFileEventRepository()
        self.workspace_sync_cursors = InMemoryWorkspaceSyncCursorRepository()
        self.transactions = 0

    def __call__(self) -> InMemoryWorkspaceUnitOfWork:
        self.transactions += 1
        return InMemoryWorkspaceUnitOfWork(
            self.workspace_bindings,
            self.workspace_file_revisions,
            self.workspace_file_events,
            self.workspace_sync_cursors,
        )

    async def bind(self, request: EnsureWorkspaceV1, workspace_id: str) -> None:
        await self.workspace_bindings.add_if_absent(
            WorkspaceBindingV1(
                workspace_id=workspace_id,
                investigation_id=request.investigation_id,
                subject_id=request.subject_id,
                delivery_code=request.delivery_code,
                article_slug=request.article_slug,
                synthesis_sha256=request.synthesis_sha256,
                created_at=FIXTURE_NOW,
            )
        )


# -- a real agent on a real tmpdir socket ------------------------------------


@dataclass
class LiveAgent:
    agent: WorkspaceAgent
    root: Path
    runtime_dir: Path
    stop_event: threading.Event
    thread: threading.Thread

    @property
    def socket_path(self) -> Path:
        path: Path = self.runtime_dir / SOCKET_FILE_NAME
        return path

    @property
    def token_file(self) -> Path:
        path: Path = self.runtime_dir / TOKEN_FILE_NAME
        return path

    @property
    def host_path(self) -> Path:
        return self.root / DELIVERY_CODE / ARTICLE_SLUG

    @property
    def token(self) -> str:
        token: str | None = self.agent.token
        assert token is not None, "agent is not started"
        return token

    def shutdown(self) -> None:
        if not self.thread.is_alive():
            return
        self.stop_event.set()
        self.thread.join(timeout=20)
        assert not self.thread.is_alive()


def start_agent(root: Path, runtime_dir: Path) -> LiveAgent:
    """Start one real agent; polling is parked so only explicit scans run."""
    agent = WorkspaceAgent(
        AgentConfig(
            root=root,
            socket_dir=runtime_dir,
            group=os.getgid(),
            poll_interval_ms=60_000,
            stability_window_ms=0,
            log_level="WARNING",
        )
    )
    stop_event = threading.Event()
    thread = threading.Thread(
        target=agent.serve, args=(stop_event,), name="workspace-agent", daemon=True
    )
    thread.start()
    if not agent.wait_ready(20):
        stop_event.set()
        pytest.fail("workspace agent did not become ready")
    return LiveAgent(
        agent=agent, root=root, runtime_dir=runtime_dir, stop_event=stop_event, thread=thread
    )


@contextmanager
def live_agent(tmp_path: Path, runtime_dir: Path) -> Iterator[LiveAgent]:
    """Start one agent; the workspace root may be long but the socket dir may not."""
    root = tmp_path / "workspaces"
    root.mkdir()
    running = start_agent(root, runtime_dir)
    try:
        yield running
    finally:
        running.shutdown()


def make_client(running: LiveAgent) -> HttpWorkspaceAgentClient:
    return HttpWorkspaceAgentClient(
        socket_path=str(running.socket_path),
        token_file=running.token_file,
        timeout_seconds=20.0,
    )


class AuthRecordingTransport(httpx.AsyncBaseTransport):
    """Record the bearer header of every attempted request (retry oracle)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.authorizations: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.authorizations.append(request.headers.get("authorization", ""))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class StaleTokenFirstClient(HttpWorkspaceAgentClient):
    """Read a stale token once, then the rotated token file (restart oracle)."""

    def __init__(
        self,
        *,
        socket_path: str,
        token_file: Path,
        stale_token: str,
        transport: httpx.AsyncBaseTransport,
        timeout_seconds: float = 20.0,
    ) -> None:
        super().__init__(
            socket_path=socket_path,
            token_file=token_file,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        self._stale_token = stale_token
        self._stale_read = False

    def _read_token(self) -> str:
        if not self._stale_read:
            self._stale_read = True
            return self._stale_token
        return super()._read_token()


def replace_file(path: Path, content: bytes) -> None:
    """Atomically replace a file the way a human editor would."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.human.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def ensure_request() -> EnsureWorkspaceV1:
    return EnsureWorkspaceV1.model_validate(load_fixture("ensure_workspace"))


async def test_ingest_treats_a_reobserved_revision_and_event_as_a_replay() -> None:
    """A rebuilt projection re-reports the same ids with fresh observation metadata."""
    store = InMemoryWorkspaceStore()
    request = ensure_request()
    await store.bind(request, WORKSPACE_ID)
    ingest = WorkspaceIngestService(store)

    event_payload = load_fixture("event")
    state_payload = load_fixture("file_state")
    event = EventV1.model_validate(event_payload)
    state = FileStateV1.model_validate(state_payload)
    later = datetime(2026, 11, 22, 12, 0, 30, tzinfo=UTC)
    first_page = EventsPageV1(
        generation=event.generation,
        events=[event],
        next_cursor=event.seq,
        oldest_retained=event.seq,
        resync_required=False,
    )
    assert (
        await ingest.ingest_events_page(workspace_id=WORKSPACE_ID, page=first_page)
    ).inserted_events == 1
    first_snapshot = SnapshotPageV1(
        generation=int_field(load_fixture("snapshot_page"), "generation"),
        files=[state],
        next_page_token=None,
    )
    assert (
        await ingest.ingest_snapshot_page(workspace_id=WORKSPACE_ID, page=first_snapshot)
    ).inserted_revisions == 1

    replay_generation = event.generation + 1
    reobserved_event = EventV1.model_validate(
        {
            **event_payload,
            "observed_at": later.isoformat(),
            "seq": event.seq + 2,
            "generation": replay_generation,
        }
    )
    replay_page = EventsPageV1(
        generation=replay_generation,
        events=[reobserved_event],
        next_cursor=event.seq + 2,
        oldest_retained=event.seq + 2,
        resync_required=False,
    )
    replay = await ingest.ingest_events_page(workspace_id=WORKSPACE_ID, page=replay_page)
    assert replay.inserted_events == 0
    assert replay.existing_events == 1
    assert replay.resync_required is True
    assert replay.generation == replay_generation

    reobserved_state = FileStateV1.model_validate(
        {**state_payload, "observed_at": later.isoformat(), "mtime_ns": state.mtime_ns + 1}
    )
    replay_snapshot = SnapshotPageV1(
        generation=replay_generation, files=[reobserved_state], next_page_token=None
    )
    snapshot_replay = await ingest.ingest_snapshot_page(
        workspace_id=WORKSPACE_ID, page=replay_snapshot
    )
    assert snapshot_replay.inserted_revisions == 0
    assert snapshot_replay.resync_required is False
    [stored_revision] = await store.workspace_file_revisions.list_for_workspace(WORKSPACE_ID)
    assert stored_revision.observed_at != later

    tampered_event = EventV1.model_validate(
        {**event_payload, "kind": "file_modified", "generation": replay_generation}
    )
    with pytest.raises(WorkspaceIngestError):
        await ingest.ingest_events_page(
            workspace_id=WORKSPACE_ID,
            page=EventsPageV1(
                generation=replay_generation,
                events=[tampered_event],
                next_cursor=event.seq,
                oldest_retained=event.seq,
                resync_required=False,
            ),
        )

    tampered_state = FileStateV1.model_validate({**state_payload, "size": state.size + 1})
    with pytest.raises(WorkspaceIngestError):
        await ingest.ingest_snapshot_page(
            workspace_id=WORKSPACE_ID,
            page=SnapshotPageV1(
                generation=replay_generation, files=[tampered_state], next_page_token=None
            ),
        )


# -- real client <-> real server end to end -----------------------------------


async def test_e2e_disk_note_is_ingested_with_its_golden_identity(
    tmp_path: Path, socket_runtime_dir: Path
) -> None:
    with live_agent(tmp_path, socket_runtime_dir) as running:
        client = make_client(running)
        try:
            request = ensure_request()
            descriptor = await client.ensure_workspace(request)
            assert descriptor.workspace_id == WORKSPACE_ID
            assert descriptor.created is True

            note_file = running.host_path / NOTE_PATH
            note_file.parent.mkdir(parents=True, exist_ok=True)
            note_file.write_bytes(NOTE_CONTENT)
            os.chmod(note_file, 0o640)

            scan_result = await client.scan_workspace(descriptor.workspace_id)
            assert scan_result.latest_cursor >= 1

            snapshot = await client.snapshot(descriptor.workspace_id)
            [state] = snapshot.files
            golden_state = FileStateV1.model_validate(load_fixture("file_state"))
            assert state.path == golden_state.path
            assert state.revision_id == golden_state.revision_id
            assert state.sha256 == golden_state.sha256 == sha256_of(NOTE_CONTENT)
            assert state.size == golden_state.size == len(NOTE_CONTENT)
            assert state.mode == golden_state.mode == "0640"
            assert state.origin == "filesystem"
            assert state.policy_class == "human_editable"

            events_page = await client.events(descriptor.workspace_id, after=0)
            assert events_page.resync_required is False
            [event] = events_page.events
            assert event.kind == "file_created"
            assert event.revision_id == golden_state.revision_id
            assert event.sha256 == golden_state.sha256

            content = await client.read_content(descriptor.workspace_id, path=NOTE_PATH)
            assert content.content == NOTE_CONTENT
            assert content.sha256 == golden_state.sha256

            store = InMemoryWorkspaceStore()
            await store.bind(request, descriptor.workspace_id)
            ingest = WorkspaceIngestService(store)
            outcome = await ingest.ingest_events_page(
                workspace_id=descriptor.workspace_id, page=events_page
            )
            assert outcome.inserted_events == 1
            assert outcome.existing_events == 0
            assert outcome.generation == events_page.generation
            assert outcome.cursor == events_page.next_cursor
            assert (
                await store.workspace_file_revisions.list_for_workspace(descriptor.workspace_id)
                == ()
            )

            snapshot_outcome = await ingest.ingest_snapshot_page(
                workspace_id=descriptor.workspace_id, page=snapshot
            )
            assert snapshot_outcome.inserted_revisions == 1
            [revision] = await store.workspace_file_revisions.list_for_workspace(
                descriptor.workspace_id
            )
            assert revision.revision_id == golden_state.revision_id
            assert revision.sha256 == golden_state.sha256
            assert revision.mode == "0640"
            assert revision.size == golden_state.size

            replay = await ingest.ingest_events_page(
                workspace_id=descriptor.workspace_id, page=events_page
            )
            assert replay.inserted_events == 0
            assert replay.existing_events == 1
            replay_snapshot = await ingest.ingest_snapshot_page(
                workspace_id=descriptor.workspace_id, page=snapshot
            )
            assert replay_snapshot.inserted_revisions == 0
            assert (
                len(await store.workspace_file_events.list_for_workspace(descriptor.workspace_id))
                == 1
            )
            assert (
                len(
                    await store.workspace_file_revisions.list_for_workspace(descriptor.workspace_id)
                )
                == 1
            )
        finally:
            await client.aclose()


async def test_e2e_conflict_copy_generated_export_and_read_only_sample(
    tmp_path: Path, socket_runtime_dir: Path
) -> None:
    with live_agent(tmp_path, socket_runtime_dir) as running:
        client = make_client(running)
        try:
            workspace_id = (await client.ensure_workspace(ensure_request())).workspace_id
            version_a = b"version A\n"
            version_b = b"version B edited on disk\n"
            version_c = b"version C proposed by the UI\n"

            created = await client.put_file(
                workspace_id,
                path=NOTE_PATH,
                content=version_a,
                expected_sha256="new",
                origin="ui",
                idempotency_key="e2e-put-a-0001",
            )
            assert created.created is True
            assert created.already_present is False
            assert created.conflict is False
            assert created.revision.sha256 == sha256_of(version_a)
            assert created.revision.revision_id == workspace_revision_id(
                workspace_id=workspace_id, path=NOTE_PATH, sha256=sha256_of(version_a)
            )

            note_file = running.host_path / NOTE_PATH
            replace_file(note_file, version_b)
            await client.scan_workspace(workspace_id)

            with pytest.raises(WorkspaceRevisionConflictError) as raised:
                await client.put_file(
                    workspace_id,
                    path=NOTE_PATH,
                    content=version_c,
                    expected_sha256=sha256_of(version_a),
                    origin="ui",
                    idempotency_key="e2e-put-c-0001",
                )
            conflict = raised.value
            assert conflict.details["current"] == sha256_of(version_b)
            assert conflict.details["proposed_sha256"] == sha256_of(version_c)
            conflict_path = conflict.details["conflict_path"]
            assert isinstance(conflict_path, str)
            conflict_file = running.host_path / conflict_path
            assert note_file.read_bytes() == version_b
            assert conflict_file.read_bytes() == version_c
            assert stat.S_IMODE(os.lstat(conflict_file).st_mode) == 0o640

            revisions = await client.list_revisions(workspace_id, path=NOTE_PATH)
            known = {item.revision_id for item in revisions.revisions}
            for version in (version_a, version_b, version_c):
                assert (
                    workspace_revision_id(
                        workspace_id=workspace_id, path=NOTE_PATH, sha256=sha256_of(version)
                    )
                    in known
                )

            plan = b"# plan\n\nsynthetic export\n"
            generated = await client.put_generated(
                workspace_id,
                path=GENERATED_PATH,
                content=plan,
                expected_sha256="new",
                generated_by="autowork-backend",
                source_sha256=sha256_of(version_a),
                idempotency_key="e2e-generated-0001",
            )
            assert generated.conflict is False
            assert generated.revision.sha256 == sha256_of(plan)
            plan_file = running.host_path / GENERATED_PATH
            assert stat.S_IMODE(os.lstat(plan_file).st_mode) == 0o440
            generated_content = await client.read_content(workspace_id, path=GENERATED_PATH)
            assert generated_content.content == plan
            replayed = await client.put_generated(
                workspace_id,
                path=GENERATED_PATH,
                content=plan,
                expected_sha256=sha256_of(plan),
                generated_by="autowork-backend",
                source_sha256=sha256_of(version_a),
                idempotency_key="e2e-generated-0002",
            )
            assert replayed.already_present is True

            sample = await client.materialize_base_sample(
                workspace_id,
                sha256=sha256_of(SAMPLE_CONTENT),
                content=SAMPLE_CONTENT,
                size=len(SAMPLE_CONTENT),
            )
            assert sample.mode == "0440"
            assert sample.path == f"base_samples/{sha256_of(SAMPLE_CONTENT)}"
            assert sample.sha256 == sha256_of(SAMPLE_CONTENT)
            sample_file = running.host_path / sample.path
            sample_mode = stat.S_IMODE(os.lstat(sample_file).st_mode)
            assert sample_mode == 0o440
            assert sample_mode & 0o111 == 0
            sample_content = await client.read_content(workspace_id, path=sample.path)
            assert sample_content.content == SAMPLE_CONTENT
            resend = await client.materialize_base_sample(
                workspace_id,
                sha256=sha256_of(SAMPLE_CONTENT),
                content=SAMPLE_CONTENT,
                size=len(SAMPLE_CONTENT),
            )
            assert resend.already_present is True
        finally:
            await client.aclose()


async def test_e2e_restart_reuses_revisions_replays_events_and_rotates_token(
    tmp_path: Path, socket_runtime_dir: Path
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = socket_runtime_dir
    first = start_agent(root, runtime_dir)
    client = make_client(first)
    request = ensure_request()
    try:
        workspace_id = (await client.ensure_workspace(request)).workspace_id
        note_file = first.host_path / NOTE_PATH
        note_file.parent.mkdir(parents=True, exist_ok=True)
        note_file.write_bytes(NOTE_CONTENT)
        os.chmod(note_file, 0o640)
        first_scan = await client.scan_workspace(workspace_id)
        snapshot_before = await client.snapshot(workspace_id)
        events_before = await client.events(workspace_id, after=0)
        assert snapshot_before.files and events_before.events

        store = InMemoryWorkspaceStore()
        await store.bind(request, workspace_id)
        ingest = WorkspaceIngestService(store)
        await ingest.ingest_events_page(workspace_id=workspace_id, page=events_before)
        await ingest.ingest_snapshot_page(workspace_id=workspace_id, page=snapshot_before)

        first_token = first.token
        first_instance = first.agent.instance_id
        first.shutdown()

        second = start_agent(root, runtime_dir)
        try:
            assert second.token != first_token
            assert second.agent.instance_id != first_instance

            second_scan = await client.scan_workspace(workspace_id)
            assert second_scan.generation == first_scan.generation
            assert second_scan.latest_cursor == events_before.next_cursor
            snapshot_after = await client.snapshot(workspace_id)
            assert [state.revision_id for state in snapshot_after.files] == [
                state.revision_id for state in snapshot_before.files
            ]
            events_after = await client.events(workspace_id, after=0)
            assert [event.event_id for event in events_after.events] == [
                event.event_id for event in events_before.events
            ]
            replay = await ingest.ingest_events_page(workspace_id=workspace_id, page=events_after)
            assert replay.inserted_events == 0
            assert replay.existing_events == len(events_before.events)

            probe = await client.probe()
            assert probe.instance_id == second.agent.instance_id

            recorder = AuthRecordingTransport(
                httpx.AsyncHTTPTransport(uds=str(second.socket_path), retries=0)
            )
            stale = StaleTokenFirstClient(
                socket_path=str(second.socket_path),
                token_file=second.token_file,
                stale_token=first_token,
                transport=recorder,
            )
            try:
                fetched = await stale.get_workspace(workspace_id)
            finally:
                await stale.aclose()
            assert fetched.workspace_id == workspace_id
            assert recorder.authorizations == [
                f"Bearer {first_token}",
                f"Bearer {second.token}",
            ]
        finally:
            second.shutdown()
    finally:
        await client.aclose()
        first.shutdown()


async def test_e2e_projection_loss_forces_a_resync_then_converges(
    tmp_path: Path, socket_runtime_dir: Path
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = socket_runtime_dir
    first = start_agent(root, runtime_dir)
    client = make_client(first)
    request = ensure_request()
    try:
        workspace_id = (await client.ensure_workspace(request)).workspace_id
        note_file = first.host_path / NOTE_PATH
        note_file.parent.mkdir(parents=True, exist_ok=True)
        note_file.write_bytes(NOTE_CONTENT)
        os.chmod(note_file, 0o640)
        await client.scan_workspace(workspace_id)
        snapshot_before = await client.snapshot(workspace_id)
        events_before = await client.events(workspace_id, after=0)
        [state_before] = snapshot_before.files
        generation_before = snapshot_before.generation
        cursor_before = events_before.next_cursor

        store = InMemoryWorkspaceStore()
        await store.bind(request, workspace_id)
        ingest = WorkspaceIngestService(store)
        await ingest.ingest_events_page(workspace_id=workspace_id, page=events_before)
        await ingest.ingest_snapshot_page(workspace_id=workspace_id, page=snapshot_before)
        first.shutdown()

        for name in (".autowork/file-index.json", ".autowork/sync-state.json"):
            (first.host_path / name).unlink()

        second = start_agent(root, runtime_dir)
        try:
            await client.scan_workspace(workspace_id)
            snapshot_after = await client.snapshot(workspace_id)
            assert snapshot_after.generation > generation_before
            [state_after] = snapshot_after.files
            assert state_after.revision_id == state_before.revision_id
            assert state_after.sha256 == state_before.sha256

            page = await client.events(workspace_id, after=cursor_before)
            assert page.generation == snapshot_after.generation
            outcome = await ingest.ingest_events_page(workspace_id=workspace_id, page=page)
            assert outcome.resync_required is True
            assert outcome.generation == snapshot_after.generation

            snapshot_outcome = await ingest.ingest_snapshot_page(
                workspace_id=workspace_id, page=snapshot_after
            )
            assert snapshot_outcome.resync_required is False
            assert snapshot_outcome.inserted_revisions == 0
            cursor = await store.workspace_sync_cursors.get(workspace_id)
            assert cursor is not None
            assert cursor.generation == snapshot_after.generation
            assert len(await store.workspace_file_revisions.list_for_workspace(workspace_id)) == 1

            reobserved = await client.events(workspace_id, after=0)
            assert [event.event_id for event in reobserved.events] == [
                event.event_id for event in events_before.events
            ]
            replay_outcome = await ingest.ingest_events_page(
                workspace_id=workspace_id, page=reobserved
            )
            assert replay_outcome.inserted_events == 0
            assert replay_outcome.existing_events == len(reobserved.events)

            with pytest.raises(WorkspaceCursorExpiredError) as raised:
                await client.events(workspace_id, after=cursor_before + 10_000)
            expired = raised.value
            assert expired.http_status == 409
            assert expired.code == "cursor_expired"
            assert expired.details["resync_required"] is True
            assert expired.details["generation"] == snapshot_after.generation
            assert expired.details["after"] == cursor_before + 10_000
        finally:
            second.shutdown()
    finally:
        await client.aclose()
        first.shutdown()
