"""Application port, wire DTOs and canonical ingest for the host workspace agent.

Normative contract: ``docs/design/investigation-workshop/WORKSPACE-AGENT.md``
§4-§6 and ADR-0011 D2/D6. The agent owns the workspace tree; the backend only
receives metadata, exact bytes it asked for, and revision identities. A
workspace file revision never creates an observation, finding or evidence row:
canonical ingest is limited to the binding, revision, event and cursor tables.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from cti_app.application.investigation_persistence import (
    WorkspaceBindingRepository,
    WorkspaceFileEventRepository,
    WorkspaceFileRevisionRepository,
    WorkspaceSyncCursorRepository,
)

PROTOCOL_VERSION = "workspace-agent.v1"
MAX_PAGE_SIZE = 500
JSON_RESPONSE_LIMIT = 16 * 1024 * 1024
CONTENT_READ_LIMIT = 32 * 1024 * 1024
WORKSPACE_LOCAL_UNAVAILABLE = "workspace_local_unavailable"

DELIVERY_CODE_PATTERN = r"^[A-Z0-9][A-Z0-9._-]{0,63}$"
ARTICLE_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
WORKSPACE_ID_PATTERN = r"^ws-[0-9a-f]{32}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
REVISION_ID_PATTERN = r"^rev-[0-9a-f]{32}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{8,128}$"

OriginKind = Literal["filesystem", "ui", "agent", "system"]
PolicyClass = Literal[
    "human_editable",
    "agent_owned",
    "generated_immutable",
    "read_only",
    "agent_internal",
]
EventKind = Literal[
    "file_created",
    "file_modified",
    "file_renamed",
    "file_deleted",
    "conflict_created",
    "policy_violation",
    "sample_materialized",
    "sample_rejected",
    "scan_completed",
]
ImportStatus = Literal["received", "applied", "ignored", "rejected"]
WriteOrigin = Literal["ui", "agent"]
WireErrorCode = Literal[
    "invalid_request",
    "invalid_path",
    "unauthorized",
    "workspace_not_found",
    "workspace_identity_conflict",
    "workspace_unmanaged_directory",
    "revision_conflict",
    "concurrent_change",
    "policy_denied",
    "busy",
    "length_required",
    "size_limit",
    "hash_mismatch",
    "cursor_expired",
    "not_implemented",
    "internal_error",
]


class WorkspaceAgentError(RuntimeError):
    """A typed workspace-agent failure.

    ``code`` is the protocol code for wire errors, or the local
    ``workspace_local_unavailable`` code when the agent cannot be reached.
    """

    code: str = "workspace_agent_error"
    http_status: int = 502

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.details: dict[str, JsonValue] = dict(details or {})


class WorkspaceLocalUnavailableError(WorkspaceAgentError):
    """Missing configuration, socket or token: actions fail closed (503)."""

    code = WORKSPACE_LOCAL_UNAVAILABLE
    http_status = 503

    def __init__(self, message: str = "workspace agent is unavailable") -> None:
        super().__init__(message, retryable=False)


class WorkspaceUnconfiguredError(WorkspaceLocalUnavailableError):
    """The socket or token setting is empty."""

    def __init__(self) -> None:
        super().__init__("workspace agent is not configured")


class WorkspaceProtocolError(WorkspaceAgentError):
    """The agent returned a body outside the versioned wire contract."""

    code = "invalid_response"
    http_status = 502


class WorkspaceResponseTooLargeError(WorkspaceProtocolError):
    """A bounded response exceeded its documented byte budget."""

    def __init__(self, message: str = "workspace agent response exceeds the size limit") -> None:
        super().__init__(message)


class WorkspaceInvalidRequestError(WorkspaceAgentError):
    code = "invalid_request"
    http_status = 400


class WorkspaceInvalidPathError(WorkspaceAgentError):
    code = "invalid_path"
    http_status = 400


class WorkspaceUnauthorizedError(WorkspaceAgentError):
    code = "unauthorized"
    http_status = 401


class WorkspaceNotFoundError(WorkspaceAgentError):
    code = "workspace_not_found"
    http_status = 404


class WorkspaceIdentityConflictError(WorkspaceAgentError):
    code = "workspace_identity_conflict"
    http_status = 409


class WorkspaceUnmanagedDirectoryError(WorkspaceAgentError):
    code = "workspace_unmanaged_directory"
    http_status = 409


class WorkspaceRevisionConflictError(WorkspaceAgentError):
    code = "revision_conflict"
    http_status = 409


class WorkspaceConcurrentChangeError(WorkspaceAgentError):
    code = "concurrent_change"
    http_status = 409


class WorkspacePolicyDeniedError(WorkspaceAgentError):
    code = "policy_denied"
    http_status = 403


class WorkspaceBusyError(WorkspaceAgentError):
    code = "busy"
    http_status = 409


class WorkspaceLengthRequiredError(WorkspaceAgentError):
    code = "length_required"
    http_status = 411


class WorkspaceSizeLimitError(WorkspaceAgentError):
    code = "size_limit"
    http_status = 413


class WorkspaceHashMismatchError(WorkspaceAgentError):
    code = "hash_mismatch"
    http_status = 422


class WorkspaceCursorExpiredError(WorkspaceAgentError):
    code = "cursor_expired"
    http_status = 409


class WorkspaceNotImplementedError(WorkspaceAgentError):
    code = "not_implemented"
    http_status = 501


class WorkspaceInternalError(WorkspaceAgentError):
    code = "internal_error"
    http_status = 500


class WorkspaceIngestError(RuntimeError):
    """Canonical ingest rejected a page before any row was committed."""


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceManifestV1(_WireModel):
    """The agent-owned ``.autowork/manifest.json`` creation pin."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    manifest_schema: Literal["autowork-workspace-manifest.v1"] = Field(alias="schema")
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    delivery_code: str = Field(pattern=DELIVERY_CODE_PATTERN)
    article_slug: str = Field(pattern=ARTICLE_SLUG_PATTERN, max_length=80)
    subject_id: UUID
    investigation_id: UUID
    synthesis_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime
    created_by: str = Field(min_length=1)
    layout_version: int = Field(ge=1)
    identity_sha256: str = Field(pattern=SHA256_PATTERN)


class WorkspaceV1(_WireModel):
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    host_path: str = Field(min_length=1)
    created: bool
    manifest: WorkspaceManifestV1

    @model_validator(mode="after")
    def _manifest_matches_workspace(self) -> Self:
        if self.manifest.workspace_id != self.workspace_id:
            raise ValueError("manifest workspace identity does not match the descriptor")
        return self


class EnsureWorkspaceV1(_WireModel):
    delivery_code: str = Field(pattern=DELIVERY_CODE_PATTERN)
    article_slug: str = Field(pattern=ARTICLE_SLUG_PATTERN, max_length=80)
    subject_id: UUID
    investigation_id: UUID
    synthesis_sha256: str = Field(pattern=SHA256_PATTERN)


class WorkspaceSummaryV1(_WireModel):
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    delivery_code: str = Field(pattern=DELIVERY_CODE_PATTERN)
    article_slug: str = Field(pattern=ARTICLE_SLUG_PATTERN, max_length=80)
    host_path: str = Field(min_length=1)
    generation: int = Field(ge=0)


class WorkspaceListV1(_WireModel):
    workspaces: list[WorkspaceSummaryV1]


class ScanWorkspaceV1(_WireModel):
    full: bool = False


class ScanResultV1(_WireModel):
    generation: int = Field(ge=0)
    latest_cursor: int = Field(ge=0)
    emitted: int = Field(ge=0)
    policy_violations: int = Field(ge=0)


class FileStateV1(_WireModel):
    path: str = Field(min_length=1)
    revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    mode: str = Field(min_length=1)
    mtime_ns: int = Field(ge=0)
    origin: OriginKind
    observed_at: datetime
    policy_class: PolicyClass
    task_id: str | None = None


class SnapshotPageV1(_WireModel):
    generation: int = Field(ge=0)
    files: list[FileStateV1]
    next_page_token: str | None = None


class FilesPageV1(_WireModel):
    generation: int = Field(ge=0)
    files: list[FileStateV1]
    next_page_token: str | None = None


class EventV1(_WireModel):
    event_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    generation: int = Field(ge=0)
    kind: EventKind
    path: str | None = None
    previous_path: str | None = None
    revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)
    previous_revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    size: int | None = Field(default=None, ge=0)
    origin: OriginKind
    task_id: str | None = None
    observed_at: datetime
    policy_class: PolicyClass | None = None


class EventsPageV1(_WireModel):
    generation: int = Field(ge=0)
    events: list[EventV1]
    next_cursor: int = Field(ge=0)
    oldest_retained: int = Field(ge=0)
    resync_required: bool


class RevisionV1(_WireModel):
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    observed_at: datetime
    origin: OriginKind
    task_id: str | None = None
    supersedes_revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)


class RevisionListV1(_WireModel):
    revisions: list[RevisionV1]


class WriteResultV1(_WireModel):
    revision: RevisionV1
    created: bool
    already_present: bool
    conflict: bool


class SampleMaterializationV1(_WireModel):
    sha256: str = Field(pattern=SHA256_PATTERN)
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    mode: Literal["0440"]
    already_present: bool


class WorkspaceContentV1(BaseModel):
    """Raw bytes read back with the agent's verification headers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: bytes
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)


class WorkspaceAgentHealthV1(_WireModel):
    status: Literal["ready"]
    protocol: Literal["workspace-agent.v1"]
    instance_id: str = Field(min_length=1)
    pid: int = Field(gt=0)
    started_at: datetime


class WorkspaceAgentProbeV1(BaseModel):
    """Status-route answer; never a readiness dependency."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["available", "unavailable", "unconfigured"]
    instance_id: str | None = None


class WorkspaceAgentErrorEnvelopeV1(_WireModel):
    error: WireErrorCode
    message: str
    retryable: bool
    details: dict[str, JsonValue]


_WIRE_ERROR_CLASSES: dict[str, type[WorkspaceAgentError]] = {
    "invalid_request": WorkspaceInvalidRequestError,
    "invalid_path": WorkspaceInvalidPathError,
    "unauthorized": WorkspaceUnauthorizedError,
    "workspace_not_found": WorkspaceNotFoundError,
    "workspace_identity_conflict": WorkspaceIdentityConflictError,
    "workspace_unmanaged_directory": WorkspaceUnmanagedDirectoryError,
    "revision_conflict": WorkspaceRevisionConflictError,
    "concurrent_change": WorkspaceConcurrentChangeError,
    "policy_denied": WorkspacePolicyDeniedError,
    "busy": WorkspaceBusyError,
    "length_required": WorkspaceLengthRequiredError,
    "size_limit": WorkspaceSizeLimitError,
    "hash_mismatch": WorkspaceHashMismatchError,
    "cursor_expired": WorkspaceCursorExpiredError,
    "not_implemented": WorkspaceNotImplementedError,
    "internal_error": WorkspaceInternalError,
}


def error_from_envelope(
    envelope: WorkspaceAgentErrorEnvelopeV1,
) -> WorkspaceAgentError:
    """Instantiate the typed error matching one closed wire code."""
    error_class = _WIRE_ERROR_CLASSES[str(envelope.error)]
    return error_class(
        envelope.message,
        retryable=envelope.retryable,
        details=envelope.details,
    )


def workspace_revision_id(*, workspace_id: str, path: str, sha256: str) -> str:
    """Derive the deterministic per-file revision id (§6, ADR-0011 E10)."""
    digest = hashlib.sha256(
        workspace_id.encode("utf-8")
        + b"\x00"
        + path.encode("utf-8")
        + b"\x00"
        + sha256.encode("ascii")
    ).hexdigest()
    return "rev-" + digest[:32]


class WorkspaceAgentPort(Protocol):
    """The typed operations the backend needs from the host agent."""

    async def probe(self) -> WorkspaceAgentProbeV1: ...

    async def ensure_workspace(
        self, request: EnsureWorkspaceV1, *, idempotency_key: str | None = None
    ) -> WorkspaceV1: ...

    async def get_workspace(self, workspace_id: str) -> WorkspaceV1: ...

    async def list_workspaces(self) -> WorkspaceListV1: ...

    async def scan_workspace(
        self,
        workspace_id: str,
        *,
        full: bool = False,
        idempotency_key: str | None = None,
    ) -> ScanResultV1: ...

    async def snapshot(
        self,
        workspace_id: str,
        *,
        page_token: str | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> SnapshotPageV1: ...

    async def events(
        self,
        workspace_id: str,
        *,
        after: int | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> EventsPageV1: ...

    async def list_files(
        self,
        workspace_id: str,
        *,
        prefix: str | None = None,
        page_token: str | None = None,
        limit: int = MAX_PAGE_SIZE,
    ) -> FilesPageV1: ...

    async def list_revisions(self, workspace_id: str, *, path: str) -> RevisionListV1: ...

    async def read_content(self, workspace_id: str, *, path: str) -> WorkspaceContentV1: ...

    async def put_file(
        self,
        workspace_id: str,
        *,
        path: str,
        content: bytes,
        expected_sha256: str,
        origin: WriteOrigin,
        task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResultV1: ...

    async def put_generated(
        self,
        workspace_id: str,
        *,
        path: str,
        content: bytes,
        expected_sha256: str,
        generated_by: str,
        source_sha256: str,
        task_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteResultV1: ...

    async def materialize_base_sample(
        self,
        workspace_id: str,
        *,
        sha256: str,
        content: bytes | AsyncIterator[bytes],
        size: int,
        idempotency_key: str | None = None,
    ) -> SampleMaterializationV1: ...

    async def aclose(self) -> None: ...


class WorkspaceBindingV1(_WireModel):
    """Canonical binding of one host workspace to one investigation."""

    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    investigation_id: UUID
    subject_id: UUID
    delivery_code: str = Field(pattern=DELIVERY_CODE_PATTERN)
    article_slug: str = Field(pattern=ARTICLE_SLUG_PATTERN, max_length=80)
    synthesis_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at: datetime


class WorkspaceFileRevisionV1(_WireModel):
    """Metadata of one per-file revision; the canonical store holds no bytes."""

    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    mode: str = Field(min_length=1)
    mtime_ns: int = Field(ge=0)
    observed_at: datetime
    origin: OriginKind
    task_id: UUID | None = None
    supersedes_revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)


class WorkspaceFileEventV1(_WireModel):
    """One ingested agent event; ``import_status`` stays ``received`` until applied."""

    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    event_id: str = Field(min_length=1)
    seq: int = Field(ge=0)
    generation: int = Field(ge=0)
    kind: EventKind
    path: str | None = None
    previous_path: str | None = None
    revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)
    previous_revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    size: int | None = Field(default=None, ge=0)
    origin: OriginKind
    task_id: UUID | None = None
    observed_at: datetime
    policy_class: PolicyClass | None = None
    import_status: ImportStatus = "received"
    imported_at: datetime | None = None


class WorkspaceSyncCursorV1(_WireModel):
    workspace_id: str = Field(pattern=WORKSPACE_ID_PATTERN)
    generation: int = Field(ge=0)
    cursor: int = Field(ge=0)
    last_scan_at: datetime


class WorkspaceIngestPageV1(BaseModel):
    """Outcome of one committed page (or one full replay of it)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    generation: int
    cursor: int
    inserted_events: int = Field(ge=0)
    existing_events: int = Field(ge=0)
    inserted_revisions: int = Field(ge=0)
    resync_required: bool


class WorkspaceUnitOfWork(Protocol):
    """The narrow slice of the UnitOfWork the ingest service owns."""

    workspace_bindings: WorkspaceBindingRepository
    workspace_file_revisions: WorkspaceFileRevisionRepository
    workspace_file_events: WorkspaceFileEventRepository
    workspace_sync_cursors: WorkspaceSyncCursorRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class WorkspaceUnitOfWorkFactory(Protocol):
    def __call__(self) -> WorkspaceUnitOfWork: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_uuid(value: str | None, *, field: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError as exc:
        raise WorkspaceIngestError(f"{field} is not a canonical UUID") from exc


def _canonical_event(workspace_id: str, event: EventV1) -> WorkspaceFileEventV1:
    if (
        event.revision_id is not None
        and event.path is not None
        and event.sha256 is not None
        and workspace_revision_id(workspace_id=workspace_id, path=event.path, sha256=event.sha256)
        != event.revision_id
    ):
        raise WorkspaceIngestError("event revision identity does not match its path and content")
    return WorkspaceFileEventV1(
        workspace_id=workspace_id,
        event_id=event.event_id,
        seq=event.seq,
        generation=event.generation,
        kind=event.kind,
        path=event.path,
        previous_path=event.previous_path,
        revision_id=event.revision_id,
        previous_revision_id=event.previous_revision_id,
        sha256=event.sha256,
        size=event.size,
        origin=event.origin,
        task_id=_canonical_uuid(event.task_id, field="task_id"),
        observed_at=event.observed_at,
        policy_class=event.policy_class,
    )


def _canonical_revision(workspace_id: str, state: FileStateV1) -> WorkspaceFileRevisionV1 | None:
    """Build a revision row when the snapshot exposes a complete identity.

    Files larger than the hashing bound are indexed metadata-only in the agent
    (``sha256`` and ``revision_id`` are null) and mint no canonical revision.
    """
    if state.revision_id is None or state.sha256 is None:
        return None
    expected = workspace_revision_id(
        workspace_id=workspace_id, path=state.path, sha256=state.sha256
    )
    if expected != state.revision_id:
        raise WorkspaceIngestError("snapshot revision identity does not match its path and content")
    return WorkspaceFileRevisionV1(
        workspace_id=workspace_id,
        revision_id=state.revision_id,
        path=state.path,
        sha256=state.sha256,
        size=state.size,
        mode=state.mode,
        mtime_ns=state.mtime_ns,
        observed_at=state.observed_at,
        origin=state.origin,
        task_id=_canonical_uuid(state.task_id, field="task_id"),
        supersedes_revision_id=None,
    )


class WorkspaceIngestService:
    """Idempotent canonical ingest: one transaction per agent page."""

    def __init__(self, uow_factory: WorkspaceUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def ingest_events_page(
        self,
        *,
        workspace_id: str,
        page: EventsPageV1,
        observed_at: datetime | None = None,
    ) -> WorkspaceIngestPageV1:
        """Insert new events and advance the cursor monotonically per generation.

        A replay returns the stored event rows and never moves the cursor
        backwards. A generation change is a resync: the cursor restarts at the
        new generation's cursor instead of being compared with the old one.
        """
        now = observed_at or _utc_now()
        async with self._uow_factory() as uow:
            binding = await uow.workspace_bindings.get(workspace_id)
            if binding is None:
                raise WorkspaceIngestError("workspace is not bound to an investigation")
            current = await uow.workspace_sync_cursors.get(workspace_id)
            resync = page.resync_required or (
                current is not None and current.generation != page.generation
            )
            inserted = existing = 0
            for event in page.events:
                if event.generation != page.generation:
                    raise WorkspaceIngestError("event generation differs from its page")
                _, created = await uow.workspace_file_events.append_if_absent(
                    _canonical_event(workspace_id, event)
                )
                if created:
                    inserted += 1
                else:
                    existing += 1
            if current is None or resync:
                stored_cursor = await uow.workspace_sync_cursors.save(
                    WorkspaceSyncCursorV1(
                        workspace_id=workspace_id,
                        generation=page.generation,
                        cursor=page.next_cursor,
                        last_scan_at=now,
                    )
                )
            elif page.generation == current.generation and page.next_cursor > current.cursor:
                stored_cursor = await uow.workspace_sync_cursors.save(
                    WorkspaceSyncCursorV1(
                        workspace_id=workspace_id,
                        generation=page.generation,
                        cursor=page.next_cursor,
                        last_scan_at=now,
                    )
                )
            else:
                stored_cursor = current
            await uow.commit()
        return WorkspaceIngestPageV1(
            workspace_id=workspace_id,
            generation=stored_cursor.generation,
            cursor=stored_cursor.cursor,
            inserted_events=inserted,
            existing_events=existing,
            inserted_revisions=0,
            resync_required=resync,
        )

    async def ingest_snapshot_page(
        self,
        *,
        workspace_id: str,
        page: SnapshotPageV1,
    ) -> WorkspaceIngestPageV1:
        """Insert every complete per-file revision of one snapshot page once.

        Revisions carry no bytes and are never promoted to evidence. Events
        pages cannot mint them because EventV1 has no ``mode``/``mtime_ns``
        (WORKSPACE-AGENT §4) while the canonical table requires both (ADR-0011
        D2); the snapshot is the complete metadata feed.
        """
        async with self._uow_factory() as uow:
            binding = await uow.workspace_bindings.get(workspace_id)
            if binding is None:
                raise WorkspaceIngestError("workspace is not bound to an investigation")
            inserted = 0
            for state in page.files:
                revision = _canonical_revision(workspace_id, state)
                if revision is None:
                    continue
                _, created = await uow.workspace_file_revisions.append_if_absent(revision)
                if created:
                    inserted += 1
            current = await uow.workspace_sync_cursors.get(workspace_id)
            resync = current is not None and current.generation != page.generation
            if current is None or resync:
                cursor = WorkspaceSyncCursorV1(
                    workspace_id=workspace_id,
                    generation=page.generation,
                    cursor=0,
                    last_scan_at=_utc_now(),
                )
            else:
                cursor = current
            stored = await uow.workspace_sync_cursors.save(cursor)
            await uow.commit()
        return WorkspaceIngestPageV1(
            workspace_id=workspace_id,
            generation=stored.generation,
            cursor=stored.cursor,
            inserted_events=0,
            existing_events=0,
            inserted_revisions=inserted,
            resync_required=resync,
        )


__all__ = [
    "ARTICLE_SLUG_PATTERN",
    "CONTENT_READ_LIMIT",
    "DELIVERY_CODE_PATTERN",
    "IDEMPOTENCY_KEY_PATTERN",
    "JSON_RESPONSE_LIMIT",
    "MAX_PAGE_SIZE",
    "PROTOCOL_VERSION",
    "REVISION_ID_PATTERN",
    "SHA256_PATTERN",
    "WORKSPACE_ID_PATTERN",
    "WORKSPACE_LOCAL_UNAVAILABLE",
    "EnsureWorkspaceV1",
    "EventV1",
    "EventsPageV1",
    "FileStateV1",
    "FilesPageV1",
    "RevisionListV1",
    "RevisionV1",
    "SampleMaterializationV1",
    "ScanResultV1",
    "ScanWorkspaceV1",
    "SnapshotPageV1",
    "WireErrorCode",
    "WorkspaceAgentError",
    "WorkspaceAgentErrorEnvelopeV1",
    "WorkspaceAgentHealthV1",
    "WorkspaceAgentPort",
    "WorkspaceAgentProbeV1",
    "WorkspaceBindingV1",
    "WorkspaceBusyError",
    "WorkspaceConcurrentChangeError",
    "WorkspaceContentV1",
    "WorkspaceCursorExpiredError",
    "WorkspaceFileEventV1",
    "WorkspaceFileRevisionV1",
    "WorkspaceHashMismatchError",
    "WorkspaceIdentityConflictError",
    "WorkspaceIngestError",
    "WorkspaceIngestPageV1",
    "WorkspaceIngestService",
    "WorkspaceInternalError",
    "WorkspaceInvalidPathError",
    "WorkspaceInvalidRequestError",
    "WorkspaceLengthRequiredError",
    "WorkspaceListV1",
    "WorkspaceLocalUnavailableError",
    "WorkspaceManifestV1",
    "WorkspaceNotFoundError",
    "WorkspaceNotImplementedError",
    "WorkspacePolicyDeniedError",
    "WorkspaceProtocolError",
    "WorkspaceResponseTooLargeError",
    "WorkspaceRevisionConflictError",
    "WorkspaceSizeLimitError",
    "WorkspaceSummaryV1",
    "WorkspaceSyncCursorV1",
    "WorkspaceUnauthorizedError",
    "WorkspaceUnconfiguredError",
    "WorkspaceUnitOfWork",
    "WorkspaceUnitOfWorkFactory",
    "WorkspaceUnmanagedDirectoryError",
    "WorkspaceV1",
    "WriteResultV1",
    "error_from_envelope",
    "workspace_revision_id",
]
