"""Investigation workspace integration service for the M24 cockpit (lot WS1).

The host workspace agent owns the tree; PostgreSQL owns the binding, the
per-file revision metadata and the ingest cursor (ADR-0011 D2/D6,
``docs/design/investigation-workshop/WORKSPACE-AGENT.md``). This module is the
business orchestration between them:

* ``ensure_workspace`` derives the stable business identity (``IRYYYYMM``
  delivery code, subject slug or deterministic ordinal), pins the exact
  verified synthesis SHA-256, calls the agent and persists the canonical
  binding. Identity collisions are typed; the agent being absent never fails
  an investigation start.
* ``sync_workspace`` runs the agent scan and feeds the existing
  :class:`~cti_app.application.workspace_agent.WorkspaceIngestService` with
  event and snapshot pages, resyncing on a generation change or an expired
  cursor. It is single-flight per workspace and returns a bounded report.
* ``export_pending`` publishes canonical artefacts (plan versions rendered as
  Markdown, the 03A ``vt/`` export set through its credential guard, and a
  canonical results summary) through ``PUT generated``. A file whose exported
  SHA already matches is skipped; a modified generated file is preserved.
* File views, content reads and conflict listings read the canonical ingest
  tables. Content is re-read through the agent and verified against the
  recorded SHA-256, capped at 1 MiB for the UI.
* UI note writes and human conflict resolutions go through the agent CAS.
  A conflict never deletes the losing copy; one ``workspace_conflict_resolve``
  analyst decision records the human choice.

Nothing here creates evidence, an observation or a finding: a workspace file
is an observation of the host disk, never canonical CTI evidence.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from cti_app.application.investigation_bootstrap import (
    MAX_EXPORT_FILES,
    BootstrapError,
    BootstrapExportRefusedError,
    InvestigationBootstrapService,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    InvestigationPlanRepository,
    InvestigationTaskRepository,
    WorkspaceBindingRepository,
    WorkspaceFileEventRepository,
    WorkspaceFileRevisionRepository,
    WorkspaceSyncCursorRepository,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
    EditionRepository,
    ProductionRunRepository,
    SubjectRepository,
)
from cti_app.application.research_context_builder import ArticleSnapshot, ArticleSnapshotPort
from cti_app.application.workspace_agent import (
    ARTICLE_SLUG_PATTERN,
    DELIVERY_CODE_PATTERN,
    IDEMPOTENCY_KEY_PATTERN,
    MAX_PAGE_SIZE,
    REVISION_ID_PATTERN,
    SHA256_PATTERN,
    EnsureWorkspaceV1,
    EventsPageV1,
    FileStateV1,
    WorkspaceAgentError,
    WorkspaceAgentPort,
    WorkspaceBindingV1,
    WorkspaceContentV1,
    WorkspaceCursorExpiredError,
    WorkspaceFileEventV1,
    WorkspaceFileRevisionV1,
    WorkspaceIngestPageV1,
    WorkspaceIngestService,
    WorkspaceManifestV1,
    WorkspaceSyncCursorV1,
    WorkspaceV1,
    WriteResultV1,
)
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import SLUG_PATTERN, Subject
from cti_app.domain.investigation import InvestigationFinding, canonical_json_bytes
from cti_app.domain.investigation_plan import (
    InvestigationPlanHead,
    InvestigationPlanVersion,
)
from cti_app.domain.investigation_task import InvestigationTaskV1
from cti_app.domain.production import AnalystInvestigation, ProductionRun

WORKSPACE_RESULTS_SCHEMA = "autowork-workspace-results.v1"
WORKSPACE_UNAVAILABLE_MESSAGE = "Workspace local indisponible"
UI_CONTENT_LIMIT = 1024 * 1024
EDITABLE_CONTENT_LIMIT = 8 * 1024 * 1024
DIFF_CHAR_LIMIT = 20_000
DIFF_LINE_LIMIT = 400
HISTORY_LIMIT = 50
MAX_SYNC_PAGES = 64
MAX_SYNC_EVENTS = MAX_SYNC_PAGES * MAX_PAGE_SIZE
MAX_CONFLICT_DIFFS = 20
MAX_NOTE_REPORT_PATHS = 200
MAX_EXPORT_ENTRIES = MAX_EXPORT_FILES + 1024
DEFAULT_FILE_PAGE_LIMIT = 200
MAX_FILE_PAGE_LIMIT = MAX_PAGE_SIZE
HUMAN_NOTES_PREFIX = "notes/humaines/"
HUMAN_EDITABLE_PREFIXES = ("notes/humaines/", "scripts/", "regles/", "discover_sample/")
GENERATED_FILE_MODE = "0440"
CONFLICT_INFIX = ".conflict-"
CONFLICT_ID_PREFIX = "cfl-"
CONFLICT_ID_PATTERN = r"^cfl-[0-9a-f]{32}$"
CONFLICT_HEX_LENGTHS: tuple[int, ...] = (8, 16, 64)
RESULTS_SUMMARY_PATH = "results/investigation-summary.json"
_CONFLICT_DECISION_NAMESPACE = UUID("5f4a8b71-9c2d-4e63-8a17-0d3c6b9e21f4")

ConflictChoice = Literal["keep_current", "keep_proposed", "merged_text"]
ExportSource = Literal["plan", "vt", "results"]
ExportAction = Literal["exported", "already_exported", "preserved", "conflict"]
AvailabilityCode = Literal[
    "available", "unconfigured", "unavailable", "identity_conflict", "not_bound"
]


class WorkspaceServiceError(RuntimeError):
    """A typed workspace refusal carrying an HTTP-stable code."""

    code = "workspace_error"
    status_code = 422

    def __init__(self, message: str, *, details: Mapping[str, JsonValue] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, JsonValue] = dict(details or {})


class InvestigationNotFoundError(WorkspaceServiceError):
    code = "investigation_not_found"
    status_code = 404


class WorkspaceNotBoundError(WorkspaceServiceError):
    code = "workspace_not_bound"
    status_code = 409


class WorkspaceIdentityUnavailableError(WorkspaceServiceError):
    """The investigation chain lacks the edition/subject needed to derive identity."""

    code = "workspace_identity_unavailable"
    status_code = 409


class WorkspaceSynthesisPinError(WorkspaceServiceError):
    """The binding or the verified synthesis does not match the pinned hash."""

    code = "workspace_synthesis_pin_mismatch"
    status_code = 409


class WorkspaceSynthesisNotVerifiedError(WorkspaceServiceError):
    code = "synthesis_not_verified"
    status_code = 409


class WorkspaceBindingConflictError(WorkspaceServiceError):
    """The canonical binding or the host manifest already holds another identity."""

    code = "workspace_identity_conflict"
    status_code = 409


class WorkspaceSyncBusyError(WorkspaceServiceError):
    code = "workspace_sync_in_progress"
    status_code = 409


class WorkspaceSyncError(WorkspaceServiceError):
    code = "workspace_sync_failed"
    status_code = 502


class WorkspaceFileNotFoundError(WorkspaceServiceError):
    code = "workspace_file_not_found"
    status_code = 404


class WorkspaceContentMismatchError(WorkspaceServiceError):
    """The agent bytes no longer match the canonical revision SHA-256."""

    code = "workspace_content_mismatch"
    status_code = 409


class WorkspacePathNotEditableError(WorkspaceServiceError):
    code = "workspace_path_not_editable"
    status_code = 403


class WorkspaceFileTooLargeError(WorkspaceServiceError):
    code = "workspace_file_too_large"
    status_code = 413


class WorkspaceRevisionConflictError(WorkspaceServiceError):
    """A stale CAS write; the agent kept both versions and returned a conflict copy."""

    code = "workspace_revision_conflict"
    status_code = 409


class WorkspaceConflictNotFoundError(WorkspaceServiceError):
    code = "workspace_conflict_not_found"
    status_code = 404


class WorkspaceConflictStaleError(WorkspaceServiceError):
    code = "workspace_conflict_stale"
    status_code = 409


class WorkspaceConflictResolvedError(WorkspaceServiceError):
    code = "workspace_conflict_already_resolved"
    status_code = 409


class WorkspaceIdempotencyConflictError(WorkspaceServiceError):
    code = "idempotency_conflict"
    status_code = 409


class WorkspaceExportRefusedError(WorkspaceServiceError):
    """The canonical VT export set was refused by its credential guard."""

    code = "workspace_export_refused"
    status_code = 409


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceEnsureResultV1(_StrictModel):
    binding: WorkspaceBindingV1
    binding_created: bool
    workspace_created: bool


class WorkspaceFileVersionV1(_StrictModel):
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    observed_at: datetime
    origin: Literal["filesystem", "ui", "agent", "system"]
    task_id: UUID | None = None
    supersedes_revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)


class WorkspaceFileViewV1(_StrictModel):
    path: str = Field(min_length=1)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    mode: str = Field(pattern=r"^[0-7]{4}$")
    policy_class: (
        Literal[
            "human_editable", "agent_owned", "generated_immutable", "read_only", "agent_internal"
        ]
        | None
    ) = None
    origin: Literal["filesystem", "ui", "agent", "system"]
    import_status: Literal["received", "applied", "ignored", "rejected"]
    task_id: UUID | None = None
    observed_at: datetime
    conflict: bool
    history: tuple[WorkspaceFileVersionV1, ...] = Field(default=(), max_length=HISTORY_LIMIT)


class WorkspaceFileListV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    workspace_available: bool
    files: tuple[WorkspaceFileViewV1, ...] = Field(default=(), max_length=MAX_FILE_PAGE_LIMIT)
    next_page_token: str | None = None


class WorkspaceContentViewV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    path: str
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    sha256: str = Field(pattern=SHA256_PATTERN)
    size: int = Field(ge=0)
    metadata_only: bool
    text: str | None = None
    content_base64: str | None = None


class WorkspaceNoteWriteResultV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    path: str
    revision: WorkspaceFileVersionV1
    created: bool
    already_present: bool


class WorkspaceConflictVersionV1(_StrictModel):
    path: str
    sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    size: int | None = Field(default=None, ge=0)
    text: str | None = None
    binary: bool = False


class WorkspaceConflictViewV1(_StrictModel):
    conflict_id: str = Field(pattern=CONFLICT_ID_PATTERN)
    path: str
    conflict_path: str | None = None
    current_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    proposed_sha256: str = Field(pattern=SHA256_PATTERN)
    origin: Literal["filesystem", "ui", "agent", "system"]
    task_id: UUID | None = None
    observed_at: datetime
    current: WorkspaceConflictVersionV1 | None = None
    proposed: WorkspaceConflictVersionV1 | None = None
    diff: str | None = None


class WorkspaceConflictListV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    workspace_available: bool
    conflicts: tuple[WorkspaceConflictViewV1, ...] = Field(
        default=(), max_length=MAX_FILE_PAGE_LIMIT
    )


class WorkspaceConflictResolutionV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    conflict_id: str = Field(pattern=CONFLICT_ID_PATTERN)
    path: str
    choice: ConflictChoice
    decision_id: UUID
    result_revision: WorkspaceFileVersionV1 | None = None
    current: WorkspaceConflictVersionV1 | None = None
    proposed: WorkspaceConflictVersionV1 | None = None
    diff: str | None = None
    idempotent_replay: bool


class WorkspaceSyncReportV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    generation: int = Field(ge=0)
    cursor: int = Field(ge=0)
    resync: bool
    truncated: bool
    files_observed: int = Field(ge=0)
    events_received: int = Field(ge=0)
    events_inserted: int = Field(ge=0)
    events_existing: int = Field(ge=0)
    revisions_inserted: int = Field(ge=0)
    new_human_notes: tuple[str, ...] = Field(default=(), max_length=MAX_NOTE_REPORT_PATHS)
    changed_human_notes: tuple[str, ...] = Field(default=(), max_length=MAX_NOTE_REPORT_PATHS)
    human_notes_truncated: bool = False
    conflicts_observed: int = Field(ge=0)
    unresolved_conflicts: int = Field(ge=0)
    policy_violations: int = Field(ge=0)
    lastly_observed_at: datetime | None = None


class WorkspaceExportEntryV1(_StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    source: ExportSource
    action: ExportAction
    revision_id: str | None = Field(default=None, pattern=REVISION_ID_PATTERN)


class WorkspaceExportReportV1(_StrictModel):
    investigation_id: UUID
    workspace_id: str
    exported: int = Field(ge=0)
    already_exported: int = Field(ge=0)
    preserved: int = Field(ge=0)
    conflicts: int = Field(ge=0)
    vt_snapshot: Literal["exported", "none"]
    entries: tuple[WorkspaceExportEntryV1, ...] = Field(default=(), max_length=MAX_EXPORT_ENTRIES)


class WorkspaceCountsV1(_StrictModel):
    files: int = Field(ge=0)
    human_notes: int = Field(ge=0)
    unresolved_conflicts: int = Field(ge=0)
    policy_violations: int = Field(ge=0)
    plans: int = Field(ge=0)
    tasks: int = Field(ge=0)
    findings: int = Field(ge=0)


class WorkspaceStatusV1(_StrictModel):
    investigation_id: UUID
    workspace_state: Literal["bound", "unbound"]
    workspace_available: bool
    availability_code: AvailabilityCode
    availability_reason: str | None = None
    binding: WorkspaceBindingV1 | None = None
    manifest: WorkspaceManifestV1 | None = None
    generation: int | None = Field(default=None, ge=0)
    cursor: int | None = Field(default=None, ge=0)
    counts: WorkspaceCountsV1
    lastly_observed_at: datetime | None = None


class WorkspaceUnitOfWork(Protocol):
    """The canonical slices the workspace service reads or writes."""

    analyst_investigations: AnalystInvestigationRepository
    production_runs: ProductionRunRepository
    subjects: SubjectRepository
    editions: EditionRepository
    workspace_bindings: WorkspaceBindingRepository
    workspace_file_revisions: WorkspaceFileRevisionRepository
    workspace_file_events: WorkspaceFileEventRepository
    workspace_sync_cursors: WorkspaceSyncCursorRepository
    analyst_decisions: AnalystDecisionRepository
    investigation_plans: InvestigationPlanRepository
    investigation_tasks: InvestigationTaskRepository
    investigation_findings: FindingRepository
    evidence_observations: EvidenceObservationRepository

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


def delivery_code_for_edition(edition: Edition) -> str:
    """Derive the one delivery code of an edition: ``IR`` + ``YYYYMM``.

    ADR-0011 D2 fixes ``IR202609`` for a September 2026 edition; the
    ``IRYYMM`` spelling of WORKSPACE-AGENT §5.1 is the superseded example.
    """
    start = edition.period_start
    code = f"IR{start.year:04d}{start.month:02d}"
    if _matches(code, DELIVERY_CODE_PATTERN) is None:
        raise WorkspaceIdentityUnavailableError("the edition period produces no valid code")
    return code


def article_slug_for_subject(subject: Subject, edition_subjects: Sequence[Subject]) -> str:
    """Return the stable article slug of one subject.

    A subject slug is immutable and unique per edition; the workspace contract
    caps it at 80 characters, so a longer (or non-conforming) slug falls back
    to ``articleNN``: the subject ordinal among its edition ordered by
    ``(created_at, id)``. The slug never replaces ``subject_id``.
    """
    slug = subject.slug
    if SLUG_PATTERN.fullmatch(slug) is not None and len(slug) <= 80:
        return slug
    ordered = sorted(edition_subjects, key=lambda item: (item.created_at, str(item.id)))
    position = next((index for index, item in enumerate(ordered, 1) if item.id == subject.id), None)
    if position is None:
        position = len(ordered) + 1
    candidate = f"article{position:02d}"
    if _matches(candidate, ARTICLE_SLUG_PATTERN) is None:
        raise WorkspaceIdentityUnavailableError("the subject ordinal produces no valid slug")
    return candidate


def render_plan_markdown(version: InvestigationPlanVersion) -> bytes:
    """Render one canonical plan version as deterministic Markdown."""
    lines = [
        f"# Plan d'investigation — version {version.version}",
        "",
        f"- investigation_id : {version.investigation_id}",
        f"- plan_version_id : {version.id}",
        f"- content_sha256 : {version.content_sha256}",
        f"- schema_version : {version.schema_version}",
        f"- parse_status : {version.parse_status.value}",
        f"- created_at : {version.created_at.isoformat()}",
    ]
    for index, lead in enumerate(version.content.leads, start=1):
        lines.extend(["", f"## Piste {index} — {lead.title}", ""])
        if lead.lead_id is not None:
            lines.append(f"- Identifiant : {_markdown_value(lead.lead_id)}")
        for label, value in (
            ("Objectif", lead.objective),
            ("Justification CTI", lead.cti_justification),
            ("Intérêt", lead.interest),
            ("Critères de conclusion", lead.conclusion_criteria),
            ("Limites", lead.limits),
        ):
            if value:
                lines.append(f"- {label} : {_markdown_value(value)}")
        for label, values in (
            ("Claims", lead.claim_refs),
            ("Preuves de départ", lead.start_evidence_refs),
            ("Échantillons", lead.sample_refs),
            ("Outils", lead.tools),
            ("Préconditions", lead.preconditions),
            ("Dépendances", lead.depends_on),
        ):
            if values:
                lines.append(f"- {label} : {', '.join(values)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def plan_export_path(version: int) -> str:
    """The versioned plan path; a previous version is never rewritten."""
    if version == 1:
        return "plans/initial-v0001.md"
    return f"plans/revision-v{version:04d}.md"


def conflict_id_for(workspace_id: str, path: str, proposed_sha256: str) -> str:
    """The agent's deterministic conflict identity for one proposed revision."""
    material = f"{workspace_id}\x00{path}\x00{proposed_sha256}".encode()
    return CONFLICT_ID_PREFIX + hashlib.sha256(material).hexdigest()[:32]


def _markdown_value(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\n", "\n  ")


def _matches(value: str, pattern: str) -> re.Match[str] | None:
    return re.fullmatch(pattern, value)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _workspace_id_for(delivery_code: str, article_slug: str) -> str:
    digest = hashlib.sha256(f"{delivery_code}\x00{article_slug}".encode()).hexdigest()
    return "ws-" + digest[:32]


def _conflict_target_id(workspace_id: str, path: str, proposed_sha256: str) -> UUID:
    return uuid5(
        _CONFLICT_DECISION_NAMESPACE,
        f"workspace_conflict:{workspace_id}:{path}:{proposed_sha256}",
    )


def _conflict_decision_id(investigation_id: UUID, idempotency_key: str) -> UUID:
    return uuid5(
        _CONFLICT_DECISION_NAMESPACE,
        f"workspace_conflict_resolve:{investigation_id}:{idempotency_key.strip()}",
    )


def _agent_idempotency_key(*parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:48]
    return f"ws-{digest}"


def _require_idempotency_key(value: str) -> str:
    if _matches(value, IDEMPOTENCY_KEY_PATTERN) is None:
        raise WorkspaceServiceError(
            "Idempotency-Key must match [A-Za-z0-9._:-]{8,128}",
            details={"field": "Idempotency-Key"},
        )
    return value


def _is_human_editable(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in HUMAN_EDITABLE_PREFIXES)


def _valid_relative_path(path: str) -> bool:
    """A conservative pre-check; the agent remains the path authority."""
    if not path or len(path.encode("utf-8")) > 1024:
        return False
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return False
    if any(ord(character) < 0x20 for character in path):
        return False
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        return False
    return components[0] != ".autowork"


def _bounded_diff(current: bytes | None, proposed: bytes | None) -> str | None:
    """A bounded unified diff; binary or oversized pairs carry no diff."""
    if current is None or proposed is None:
        return None
    try:
        current_text = current.decode("utf-8")
        proposed_text = proposed.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = list(
        difflib.unified_diff(
            current_text.splitlines(),
            proposed_text.splitlines(),
            fromfile="current",
            tofile="proposed",
            lineterm="",
        )
    )
    if len(lines) > DIFF_LINE_LIMIT:
        lines = lines[:DIFF_LINE_LIMIT]
        lines.append("… (diff tronqué)")
    diff = "\n".join(lines)
    if len(diff) > DIFF_CHAR_LIMIT:
        diff = diff[:DIFF_CHAR_LIMIT] + "\n… (diff tronqué)"
    return diff


def _decode_text(content: bytes) -> tuple[str | None, str | None]:
    try:
        return content.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, base64.b64encode(content).decode("ascii")


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _version(result: WriteResultV1) -> WorkspaceFileVersionV1:
    revision = result.revision
    return WorkspaceFileVersionV1(
        revision_id=revision.revision_id,
        sha256=revision.sha256,
        size=revision.size,
        observed_at=revision.observed_at,
        origin=cast(Any, revision.origin),
        task_id=UUID(revision.task_id) if revision.task_id is not None else None,
        supersedes_revision_id=revision.supersedes_revision_id,
    )


@dataclass
class _SyncCollector:
    resync: bool = False
    truncated: bool = False
    files_observed: int = 0
    events_received: int = 0
    events_inserted: int = 0
    events_existing: int = 0
    revisions_inserted: int = 0
    policy_violations: int = 0
    conflicts_observed: int = 0
    new_human_notes: set[str] = field(default_factory=set)
    changed_human_notes: set[str] = field(default_factory=set)
    observed_at: list[datetime] = field(default_factory=list)

    def add_snapshot_page(self, files: Sequence[FileStateV1]) -> None:
        self.files_observed += len(files)
        for state in files:
            self.observed_at.append(state.observed_at)

    def add_events_page(self, page: EventsPageV1) -> None:
        self.events_received += len(page.events)
        for event in page.events:
            self.observed_at.append(event.observed_at)
            if event.kind == "policy_violation":
                self.policy_violations += 1
            elif event.kind == "conflict_created":
                self.conflicts_observed += 1
            if (
                event.path is not None
                and event.path.startswith(HUMAN_NOTES_PREFIX)
                and CONFLICT_INFIX not in event.path.rsplit("/", 1)[-1]
            ):
                if event.kind == "file_created":
                    self.new_human_notes.add(event.path)
                elif event.kind == "file_modified":
                    self.changed_human_notes.add(event.path)

    def add_ingest(self, page: WorkspaceIngestPageV1) -> None:
        self.events_inserted += page.inserted_events
        self.events_existing += page.existing_events
        self.revisions_inserted += page.inserted_revisions
        self.resync = self.resync or page.resync_required


@dataclass(frozen=True)
class _CanonicalConflict:
    conflict_id: str
    target_id: UUID
    path: str
    conflict_path: str | None
    current_sha256: str | None
    proposed_sha256: str
    origin: str
    task_id: UUID | None
    observed_at: datetime


@dataclass(frozen=True)
class _DesiredExport:
    path: str
    content: bytes
    source_sha256: str
    generated_by: str
    source: ExportSource


_SYNC_GUARD = threading.Lock()
_SYNC_INFLIGHT: set[str] = set()


@contextmanager
def _sync_slot(workspace_id: str) -> Iterator[None]:
    """Single-flight per workspace without binding an asyncio lock to one loop."""
    with _SYNC_GUARD:
        if workspace_id in _SYNC_INFLIGHT:
            raise WorkspaceSyncBusyError("a workspace sync is already running")
        _SYNC_INFLIGHT.add(workspace_id)
    try:
        yield
    finally:
        with _SYNC_GUARD:
            _SYNC_INFLIGHT.discard(workspace_id)


class InvestigationWorkspaceService:
    """Business orchestration of one investigation's host workspace."""

    def __init__(
        self,
        *,
        uow_factory: WorkspaceUnitOfWorkFactory,
        workspace_agent: WorkspaceAgentPort,
        article_snapshots: ArticleSnapshotPort,
        bootstrap: InvestigationBootstrapService,
    ) -> None:
        self._uow_factory = uow_factory
        self._workspace_agent = workspace_agent
        self._article_snapshots = article_snapshots
        self._bootstrap = bootstrap

    # --------------------------------------------------------------- identity

    async def ensure_workspace(
        self,
        investigation_id: UUID,
        *,
        verified_synthesis_sha256: str | None = None,
    ) -> WorkspaceEnsureResultV1:
        """Create or refresh the workspace once; returns the canonical binding."""
        async with self._uow_factory() as uow:
            investigation, _run, subject, edition, snapshot = await self._identity(
                uow, investigation_id, verified_synthesis_sha256
            )
            existing = await uow.workspace_bindings.get_for_investigation(investigation_id)
            if existing is not None:
                if existing.synthesis_sha256 != snapshot.synthesis_sha256:
                    raise WorkspaceSynthesisPinError(
                        "the workspace binding does not pin the verified synthesis hash",
                        details={
                            "binding_sha256": existing.synthesis_sha256,
                            "verified_sha256": snapshot.synthesis_sha256,
                        },
                    )
                delivery_code = existing.delivery_code
                article_slug = existing.article_slug
            else:
                delivery_code = delivery_code_for_edition(edition)
                subjects = await uow.subjects.list_for_edition(edition.id)
                article_slug = article_slug_for_subject(subject, subjects)
        workspace_id = _workspace_id_for(delivery_code, article_slug)
        request = EnsureWorkspaceV1(
            delivery_code=delivery_code,
            article_slug=article_slug,
            subject_id=investigation.subject_id,
            investigation_id=investigation_id,
            synthesis_sha256=snapshot.synthesis_sha256,
        )
        workspace = await self._workspace_agent.ensure_workspace(
            request,
            idempotency_key=_agent_idempotency_key(
                "ensure", str(investigation_id), snapshot.synthesis_sha256
            ),
        )
        self._require_manifest_matches(workspace, request, workspace_id)
        binding = WorkspaceBindingV1(
            workspace_id=workspace_id,
            investigation_id=investigation_id,
            subject_id=investigation.subject_id,
            delivery_code=delivery_code,
            article_slug=article_slug,
            synthesis_sha256=snapshot.synthesis_sha256,
            created_at=workspace.manifest.created_at,
        )
        async with self._uow_factory() as uow:
            try:
                stored, binding_created = await uow.workspace_bindings.add_if_absent(binding)
            except ValueError as exc:
                raise WorkspaceBindingConflictError(
                    "the canonical workspace binding already holds another identity",
                    details={"workspace_id": workspace_id},
                ) from exc
            await uow.commit()
        return WorkspaceEnsureResultV1(
            binding=stored,
            binding_created=binding_created,
            workspace_created=workspace.created,
        )

    async def _identity(
        self,
        uow: WorkspaceUnitOfWork,
        investigation_id: UUID,
        verified_synthesis_sha256: str | None,
    ) -> tuple[AnalystInvestigation, ProductionRun, Subject, Edition, ArticleSnapshot]:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise InvestigationNotFoundError("the investigation does not exist")
        run = await uow.production_runs.get(investigation.production_run_id)
        if run is None:
            raise WorkspaceIdentityUnavailableError("the production run is unavailable")
        subject = await uow.subjects.get(investigation.subject_id)
        if subject is None or subject.edition_id != run.edition_id:
            raise WorkspaceIdentityUnavailableError("the subject is unavailable")
        edition = await uow.editions.get(run.edition_id)
        if edition is None:
            raise WorkspaceIdentityUnavailableError("the edition is unavailable")
        try:
            snapshot = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        except (ValueError, KeyError) as exc:
            raise WorkspaceSynthesisNotVerifiedError(
                "the investigation synthesis is not verified"
            ) from exc
        if snapshot.subject_id != investigation.subject_id or snapshot.production_run_id != run.id:
            raise WorkspaceSynthesisPinError("the verified synthesis belongs to another chain")
        if (
            verified_synthesis_sha256 is not None
            and snapshot.synthesis_sha256 != verified_synthesis_sha256
        ):
            raise WorkspaceSynthesisPinError(
                "the verified synthesis hash changed since the start request"
            )
        return investigation, run, subject, edition, snapshot

    @staticmethod
    def _require_manifest_matches(
        workspace: WorkspaceV1, request: EnsureWorkspaceV1, workspace_id: str
    ) -> None:
        manifest = workspace.manifest
        if (
            workspace.workspace_id != workspace_id
            or manifest.workspace_id != workspace_id
            or manifest.subject_id != request.subject_id
            or manifest.investigation_id != request.investigation_id
            or manifest.delivery_code != request.delivery_code
            or manifest.article_slug != request.article_slug
            or manifest.synthesis_sha256 != request.synthesis_sha256
        ):
            raise WorkspaceBindingConflictError(
                "the host workspace manifest does not match the investigation identity",
                details={"workspace_id": workspace_id},
            )

    async def _binding(self, investigation_id: UUID) -> WorkspaceBindingV1:
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
        if binding is None:
            raise WorkspaceNotBoundError("the investigation has no host workspace")
        return binding

    @staticmethod
    async def _require_investigation(
        uow: WorkspaceUnitOfWork, investigation_id: UUID
    ) -> AnalystInvestigation:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise InvestigationNotFoundError("the investigation does not exist")
        return investigation

    # ------------------------------------------------------------------- sync

    async def sync_workspace(self, investigation_id: UUID) -> WorkspaceSyncReportV1:
        """Scan the host tree once and ingest every new page into PostgreSQL."""
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        with _sync_slot(workspace_id):
            scan = await self._workspace_agent.scan_workspace(workspace_id, full=False)
            ingest = WorkspaceIngestService(cast(Any, self._uow_factory))
            collector = _SyncCollector()
            async with self._uow_factory() as uow:
                cursor = await uow.workspace_sync_cursors.get(workspace_id)
            resync = cursor is None or cursor.generation != scan.generation
            collector.resync = resync
            # The snapshot is the only complete metadata feed (it carries mode
            # and mtime_ns); it is bounded and makes every revision canonical.
            await self._ingest_snapshot(workspace_id, ingest, collector)
            await self._ingest_events(
                workspace_id,
                ingest,
                collector,
                cursor=None if resync else cursor,
            )
            # The scan result and the event page describe the same emissions once.
            collector.policy_violations = max(collector.policy_violations, scan.policy_violations)
            async with self._uow_factory() as uow:
                stored_cursor = await uow.workspace_sync_cursors.get(workspace_id)
                conflicts = await self._canonical_conflicts(uow, workspace_id)
                decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            resolved = self._resolved_conflict_targets(decisions)
            unresolved = sum(1 for conflict in conflicts if conflict.target_id not in resolved)
            lastly = max(collector.observed_at, default=None)
            if lastly is None and stored_cursor is not None:
                lastly = stored_cursor.last_scan_at
            return WorkspaceSyncReportV1(
                investigation_id=investigation_id,
                workspace_id=workspace_id,
                generation=(
                    stored_cursor.generation if stored_cursor is not None else scan.generation
                ),
                cursor=stored_cursor.cursor if stored_cursor is not None else 0,
                resync=collector.resync,
                truncated=collector.truncated,
                files_observed=collector.files_observed,
                events_received=collector.events_received,
                events_inserted=collector.events_inserted,
                events_existing=collector.events_existing,
                revisions_inserted=collector.revisions_inserted,
                new_human_notes=tuple(sorted(collector.new_human_notes)[:MAX_NOTE_REPORT_PATHS]),
                changed_human_notes=tuple(
                    sorted(collector.changed_human_notes)[:MAX_NOTE_REPORT_PATHS]
                ),
                human_notes_truncated=(
                    len(collector.new_human_notes) > MAX_NOTE_REPORT_PATHS
                    or len(collector.changed_human_notes) > MAX_NOTE_REPORT_PATHS
                ),
                conflicts_observed=collector.conflicts_observed,
                unresolved_conflicts=unresolved,
                policy_violations=collector.policy_violations,
                lastly_observed_at=lastly,
            )

    async def _ingest_snapshot(
        self,
        workspace_id: str,
        ingest: WorkspaceIngestService,
        collector: _SyncCollector,
    ) -> None:
        token: str | None = None
        for _page in range(MAX_SYNC_PAGES):
            page = await self._workspace_agent.snapshot(
                workspace_id, page_token=token, limit=MAX_PAGE_SIZE
            )
            collector.add_snapshot_page(page.files)
            collector.add_ingest(
                await ingest.ingest_snapshot_page(workspace_id=workspace_id, page=page)
            )
            token = page.next_page_token
            if token is None:
                return
        collector.truncated = True

    async def _ingest_events(
        self,
        workspace_id: str,
        ingest: WorkspaceIngestService,
        collector: _SyncCollector,
        *,
        cursor: WorkspaceSyncCursorV1 | None,
    ) -> None:
        after = cursor.cursor if cursor is not None else None
        resynced = cursor is None or collector.resync
        received = 0
        for _page in range(MAX_SYNC_PAGES):
            if received >= MAX_SYNC_EVENTS:
                collector.truncated = True
                return
            try:
                page = await self._workspace_agent.events(
                    workspace_id, after=after, limit=MAX_PAGE_SIZE
                )
            except WorkspaceCursorExpiredError:
                if resynced:
                    raise WorkspaceSyncError("the agent cursor expired after a resync") from None
                resynced = True
                collector.resync = True
                after = None
                continue
            if page.resync_required and not resynced:
                resynced = True
                collector.resync = True
                after = None
                continue
            collector.add_events_page(page)
            collector.add_ingest(
                await ingest.ingest_events_page(workspace_id=workspace_id, page=page)
            )
            received += len(page.events)
            previous = after or 0
            if page.next_cursor <= previous and not page.events:
                return
            after = page.next_cursor
            if not page.events:
                return
        collector.truncated = True

    # ---------------------------------------------------------------- exports

    async def export_pending(self, investigation_id: UUID) -> WorkspaceExportReportV1:
        """Export every canonical artefact whose SHA is not already on the host."""
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        current = await self._agent_files(workspace_id)
        desired: list[_DesiredExport] = []
        vt_state: Literal["exported", "none"] = "none"
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            plan_versions = await uow.investigation_plans.list_for_investigation(investigation_id)
            head = await uow.investigation_plans.get_head(investigation_id)
            tasks = await uow.investigation_tasks.list_for_investigation(investigation_id)
            findings = await uow.investigation_findings.list_for_investigation(investigation_id)
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        for version in plan_versions:
            desired.append(
                _DesiredExport(
                    path=plan_export_path(version.version),
                    content=render_plan_markdown(version),
                    source_sha256=version.content_sha256,
                    generated_by="autowork:plans",
                    source="plan",
                )
            )
        summary = self._results_summary(
            investigation_id=investigation_id,
            head=head,
            versions=plan_versions,
            tasks=tasks,
            findings=findings,
            observation_count=len(observations),
        )
        desired.append(
            _DesiredExport(
                path=RESULTS_SUMMARY_PATH,
                content=summary,
                source_sha256=_sha256(summary),
                generated_by="autowork:results",
                source="results",
            )
        )
        try:
            snapshot = await self._bootstrap.latest_snapshot(investigation_id)
        except BootstrapError as exc:
            raise WorkspaceServiceError(
                f"the bootstrap snapshot is unavailable: {exc.code}",
                details={"code": exc.code},
            ) from exc
        if snapshot is not None:
            try:
                export = self._bootstrap.snapshot_export(snapshot)
            except BootstrapExportRefusedError as exc:
                raise WorkspaceExportRefusedError(
                    "the canonical VT export set was refused by its credential guard"
                ) from exc
            vt_state = "exported"
            desired.extend(
                _DesiredExport(
                    path=file.path,
                    content=file.content.encode("utf-8"),
                    source_sha256=file.sha256,
                    generated_by="autowork:vt-bootstrap",
                    source="vt",
                )
                for file in export.files
            )
        if len(desired) > MAX_EXPORT_ENTRIES:
            raise WorkspaceServiceError("the export set exceeds its bounded report size")
        entries: list[WorkspaceExportEntryV1] = []
        exported = already = preserved = conflicts = 0
        for item in desired:
            existing = current.get(item.path)
            sha256 = _sha256(item.content)
            if existing is not None and existing.sha256 == sha256:
                already += 1
                entries.append(
                    WorkspaceExportEntryV1(
                        path=item.path,
                        sha256=sha256,
                        source=item.source,
                        action="already_exported",
                        revision_id=existing.revision_id,
                    )
                )
                continue
            if existing is not None and existing.origin != "system":
                preserved += 1
                entries.append(
                    WorkspaceExportEntryV1(
                        path=item.path,
                        sha256=sha256,
                        source=item.source,
                        action="preserved",
                        revision_id=existing.revision_id,
                    )
                )
                continue
            expected = existing.sha256 if existing is not None and existing.sha256 else "new"
            try:
                result = await self._workspace_agent.put_generated(
                    workspace_id,
                    path=item.path,
                    content=item.content,
                    expected_sha256=expected,
                    generated_by=item.generated_by,
                    source_sha256=item.source_sha256,
                    idempotency_key=_agent_idempotency_key(
                        "export", workspace_id, item.path, sha256
                    ),
                )
            except WorkspaceAgentError as exc:
                if exc.code != "revision_conflict":
                    raise
                conflicts += 1
                entries.append(
                    WorkspaceExportEntryV1(
                        path=item.path,
                        sha256=sha256,
                        source=item.source,
                        action="conflict",
                    )
                )
                continue
            exported += 1
            current[item.path] = FileStateV1(
                path=item.path,
                revision_id=result.revision.revision_id,
                sha256=result.revision.sha256,
                size=result.revision.size,
                mode=GENERATED_FILE_MODE,
                mtime_ns=0,
                origin="system",
                observed_at=result.revision.observed_at,
                policy_class="generated_immutable",
                task_id=None,
            )
            entries.append(
                WorkspaceExportEntryV1(
                    path=item.path,
                    sha256=sha256,
                    source=item.source,
                    action="exported",
                    revision_id=result.revision.revision_id,
                )
            )
        return WorkspaceExportReportV1(
            investigation_id=investigation_id,
            workspace_id=workspace_id,
            exported=exported,
            already_exported=already,
            preserved=preserved,
            conflicts=conflicts,
            vt_snapshot=vt_state,
            entries=tuple(entries),
        )

    async def _agent_files(self, workspace_id: str) -> dict[str, FileStateV1]:
        files: dict[str, FileStateV1] = {}
        token: str | None = None
        for _page in range(MAX_SYNC_PAGES):
            page = await self._workspace_agent.list_files(
                workspace_id, page_token=token, limit=MAX_PAGE_SIZE
            )
            for state in page.files:
                files[state.path] = state
            token = page.next_page_token
            if token is None:
                return files
        raise WorkspaceSyncError("the workspace file list exceeds the bounded export scan")

    @staticmethod
    def _results_summary(
        *,
        investigation_id: UUID,
        head: InvestigationPlanHead | None,
        versions: Sequence[InvestigationPlanVersion],
        tasks: Sequence[InvestigationTaskV1],
        findings: Sequence[InvestigationFinding],
        observation_count: int,
    ) -> bytes:
        current = next(
            (
                version
                for version in versions
                if head is not None and version.version == head.current_version
            ),
            None,
        )
        by_assessment: dict[str, int] = {}
        for finding in findings:
            key = finding.assessment.value
            by_assessment[key] = by_assessment.get(key, 0) + 1
        payload = {
            "schema": WORKSPACE_RESULTS_SCHEMA,
            "investigation_id": str(investigation_id),
            "plan": (
                {
                    "current_version": current.version,
                    "plan_version_id": str(current.id),
                    "content_sha256": current.content_sha256,
                }
                if current is not None
                else None
            ),
            "tasks": [
                {
                    "task_id": str(task.id),
                    "task_key": task.task_key,
                    "title": task.title,
                    "status": task.status.value,
                    "status_version": task.status_version,
                    "result_verification_status": task.result_verification_status.value,
                    "assigned_actor_kind": (
                        task.assigned_actor_kind.value
                        if task.assigned_actor_kind is not None
                        else None
                    ),
                    "assigned_actor_id": task.assigned_actor_id,
                    "result_refs": [_json_safe(dict(ref)) for ref in task.result_refs],
                    "updated_at": task.updated_at.isoformat(),
                }
                for task in sorted(tasks, key=lambda item: (item.created_at, str(item.id)))
            ],
            "findings": {"total": len(findings), "by_assessment": by_assessment},
            "observations": {"total": observation_count},
        }
        return canonical_json_bytes(payload)

    # ------------------------------------------------------------- file views

    async def status(self, investigation_id: UUID) -> WorkspaceStatusV1:
        """Status + binding + manifest + canonical counts; never a host path."""
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
            cursor = (
                await uow.workspace_sync_cursors.get(binding.workspace_id)
                if binding is not None
                else None
            )
            plans = await uow.investigation_plans.list_for_investigation(investigation_id)
            tasks = await uow.investigation_tasks.list_for_investigation(investigation_id)
            findings = await uow.investigation_findings.list_for_investigation(investigation_id)
            conflicts = (
                await self._canonical_conflicts(uow, binding.workspace_id)
                if binding is not None
                else ()
            )
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            revisions = (
                await uow.workspace_file_revisions.list_for_workspace(binding.workspace_id)
                if binding is not None
                else ()
            )
            events = (
                await uow.workspace_file_events.list_for_workspace(binding.workspace_id)
                if binding is not None
                else ()
            )
        resolved = self._resolved_conflict_targets(decisions)
        paths = {revision.path for revision in revisions}
        policy_violations = sum(1 for event in events if event.kind == "policy_violation")
        lastly = max((revision.observed_at for revision in revisions), default=None)
        event_observed = max((event.observed_at for event in events), default=None)
        if event_observed is not None and (lastly is None or event_observed > lastly):
            lastly = event_observed
        counts = WorkspaceCountsV1(
            files=len(paths),
            human_notes=sum(1 for path in paths if path.startswith(HUMAN_NOTES_PREFIX)),
            unresolved_conflicts=sum(
                1 for conflict in conflicts if conflict.target_id not in resolved
            ),
            policy_violations=policy_violations,
            plans=len(plans),
            tasks=len(tasks),
            findings=len(findings),
        )
        probe = await self._workspace_agent.probe()
        available = probe.status == "available"
        availability_code: AvailabilityCode
        manifest: WorkspaceManifestV1 | None = None
        if binding is None:
            availability_code = "not_bound"
        elif probe.status == "unconfigured":
            availability_code = "unconfigured"
        elif not available:
            availability_code = "unavailable"
        else:
            try:
                workspace = await self._workspace_agent.get_workspace(binding.workspace_id)
            except WorkspaceAgentError:
                available = False
                availability_code = "unavailable"
            else:
                if (
                    workspace.manifest.workspace_id != binding.workspace_id
                    or workspace.manifest.investigation_id != binding.investigation_id
                ):
                    available = False
                    availability_code = "identity_conflict"
                else:
                    manifest = workspace.manifest
                    availability_code = "available"
        return WorkspaceStatusV1(
            investigation_id=investigation_id,
            workspace_state="bound" if binding is not None else "unbound",
            workspace_available=available,
            availability_code=availability_code,
            availability_reason=(
                None if available or binding is None else WORKSPACE_UNAVAILABLE_MESSAGE
            ),
            binding=binding,
            manifest=manifest,
            generation=cursor.generation if cursor is not None else None,
            cursor=cursor.cursor if cursor is not None else None,
            counts=counts,
            lastly_observed_at=lastly,
        )

    async def list_files(
        self,
        investigation_id: UUID,
        *,
        prefix: str | None = None,
        page_token: str | None = None,
        limit: int = DEFAULT_FILE_PAGE_LIMIT,
    ) -> WorkspaceFileListV1:
        """Latest canonical revision per path, with bounded version history."""
        if not 1 <= limit <= MAX_FILE_PAGE_LIMIT:
            raise WorkspaceServiceError("limit must be between 1 and 500")
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        normalized_prefix = (prefix or "").rstrip("/")
        if prefix is not None and not _valid_relative_path(normalized_prefix):
            raise WorkspaceServiceError("prefix is not a valid workspace path")
        async with self._uow_factory() as uow:
            revisions = await uow.workspace_file_revisions.list_for_workspace(workspace_id)
            events = await uow.workspace_file_events.list_for_workspace(workspace_id)
            conflicts = await self._canonical_conflicts(uow, workspace_id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
        resolved = self._resolved_conflict_targets(decisions)
        unresolved_paths = {
            conflict.path for conflict in conflicts if conflict.target_id not in resolved
        }
        latest = self._latest_by_path(revisions)
        event_by_revision = self._latest_events(events)
        paths = sorted(
            path
            for path in latest
            if not normalized_prefix
            or path == normalized_prefix
            or path.startswith(f"{normalized_prefix}/")
        )
        if page_token is not None:
            paths = [path for path in paths if path > page_token]
        selected = paths[: limit + 1]
        has_more = len(selected) > limit
        selected = selected[:limit]
        files: list[WorkspaceFileViewV1] = []
        for path in selected:
            current, history = latest[path]
            event = event_by_revision.get((path, current.revision_id))
            files.append(
                WorkspaceFileViewV1(
                    path=path,
                    revision_id=current.revision_id,
                    sha256=current.sha256,
                    size=current.size,
                    mode=current.mode,
                    policy_class=cast(Any, event.policy_class) if event is not None else None,
                    origin=cast(Any, current.origin),
                    import_status=(
                        cast(Any, event.import_status) if event is not None else "received"
                    ),
                    task_id=current.task_id,
                    observed_at=current.observed_at,
                    conflict=path in unresolved_paths,
                    history=tuple(
                        WorkspaceFileVersionV1(
                            revision_id=revision.revision_id,
                            sha256=revision.sha256,
                            size=revision.size,
                            observed_at=revision.observed_at,
                            origin=cast(Any, revision.origin),
                            task_id=revision.task_id,
                            supersedes_revision_id=revision.supersedes_revision_id,
                        )
                        for revision in history[:HISTORY_LIMIT]
                    ),
                )
            )
        probe = await self._workspace_agent.probe()
        return WorkspaceFileListV1(
            investigation_id=investigation_id,
            workspace_id=workspace_id,
            workspace_available=probe.status == "available",
            files=tuple(files),
            next_page_token=selected[-1] if has_more and selected else None,
        )

    async def read_file(
        self,
        investigation_id: UUID,
        *,
        path: str,
        expected_sha256: str | None = None,
    ) -> WorkspaceContentViewV1:
        """Read one canonical revision through the agent, capped at 1 MiB."""
        if not _valid_relative_path(path):
            raise WorkspaceServiceError("path is not a valid workspace path")
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        async with self._uow_factory() as uow:
            revisions = await uow.workspace_file_revisions.list_for_workspace(workspace_id)
        latest = self._latest_by_path(revisions)
        if path not in latest:
            raise WorkspaceFileNotFoundError("the workspace path has no canonical revision")
        revision, _history = latest[path]
        if expected_sha256 is not None and expected_sha256 != revision.sha256:
            raise WorkspaceConflictStaleError(
                "the expected revision is not the current canonical revision",
                details={
                    "path": path,
                    "expected_sha256": expected_sha256,
                    "current_sha256": revision.sha256,
                },
            )
        if revision.size > UI_CONTENT_LIMIT:
            return WorkspaceContentViewV1(
                investigation_id=investigation_id,
                workspace_id=workspace_id,
                path=path,
                revision_id=revision.revision_id,
                sha256=revision.sha256,
                size=revision.size,
                metadata_only=True,
            )
        content = await self._read_verified_content(workspace_id, revision)
        if len(content.content) > UI_CONTENT_LIMIT:
            return WorkspaceContentViewV1(
                investigation_id=investigation_id,
                workspace_id=workspace_id,
                path=path,
                revision_id=revision.revision_id,
                sha256=revision.sha256,
                size=revision.size,
                metadata_only=True,
            )
        text, encoded = _decode_text(content.content)
        return WorkspaceContentViewV1(
            investigation_id=investigation_id,
            workspace_id=workspace_id,
            path=path,
            revision_id=revision.revision_id,
            sha256=revision.sha256,
            size=revision.size,
            metadata_only=False,
            text=text,
            content_base64=encoded,
        )

    # -------------------------------------------------------------- UI writes

    async def write_note(
        self,
        investigation_id: UUID,
        *,
        path: str,
        expected_sha256: str,
        text: str,
        actor_id: str,
        idempotency_key: str,
    ) -> WorkspaceNoteWriteResultV1:
        """Write one human-editable note through the agent CAS (origin ``ui``).

        ``actor_id`` is the verified human of the request; the agent event keeps
        the ``ui`` origin and the canonical attribution stays in the API audit
        trail, because a workspace revision is never evidence.
        """
        del actor_id
        _require_idempotency_key(idempotency_key)
        if not _valid_relative_path(path):
            raise WorkspaceServiceError("path is not a valid workspace path")
        if not _is_human_editable(path):
            raise WorkspacePathNotEditableError(
                "only human-editable workspace paths accept UI writes",
                details={"path": path},
            )
        if expected_sha256 != "new" and _matches(expected_sha256, SHA256_PATTERN) is None:
            raise WorkspaceServiceError("expected_sha256 must be 64 lowercase hex or 'new'")
        encoded = text.encode("utf-8")
        if len(encoded) > EDITABLE_CONTENT_LIMIT:
            raise WorkspaceFileTooLargeError("the note exceeds the 8 MiB editable limit")
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        try:
            result = await self._workspace_agent.put_file(
                workspace_id,
                path=path,
                content=encoded,
                expected_sha256=expected_sha256,
                origin="ui",
                idempotency_key=_agent_idempotency_key(
                    "note", workspace_id, path, _sha256(encoded), idempotency_key
                ),
            )
        except WorkspaceAgentError as exc:
            if exc.code != "revision_conflict":
                raise
            proposed_sha256 = str(exc.details.get("proposed_sha256") or _sha256(encoded))
            raise WorkspaceRevisionConflictError(
                "the file changed since the expected revision; both versions were kept",
                details=await self._conflict_details(
                    workspace_id,
                    path=path,
                    proposed_sha256=proposed_sha256,
                    current_sha256=cast(str | None, exc.details.get("current")),
                    conflict_path=cast(str | None, exc.details.get("conflict_path")),
                    conflict_id=cast(str | None, exc.details.get("conflict_id")),
                    proposed_content=(encoded if proposed_sha256 == _sha256(encoded) else None),
                ),
            ) from exc
        return WorkspaceNoteWriteResultV1(
            investigation_id=investigation_id,
            workspace_id=workspace_id,
            path=path,
            revision=_version(result),
            created=result.created,
            already_present=result.already_present,
        )

    async def _conflict_details(
        self,
        workspace_id: str,
        *,
        path: str,
        proposed_sha256: str,
        current_sha256: str | None,
        conflict_path: str | None,
        conflict_id: str | None,
        proposed_content: bytes | None,
    ) -> dict[str, JsonValue]:
        current = await self._read_optional(workspace_id, path, current_sha256)
        proposed = (
            await self._read_optional(workspace_id, conflict_path, proposed_sha256)
            if conflict_path is not None
            else None
        )
        if proposed is None and proposed_content is not None:
            proposed = self._content_version(
                path=conflict_path or path,
                sha256=proposed_sha256,
                content=proposed_content,
            )
        diff = _bounded_diff(
            current.text.encode("utf-8")
            if current is not None and current.text is not None
            else None,
            proposed.text.encode("utf-8")
            if proposed is not None and proposed.text is not None
            else None,
        )
        return cast(
            dict[str, JsonValue],
            _json_safe(
                {
                    "workspace_id": workspace_id,
                    "path": path,
                    "conflict_id": conflict_id
                    or conflict_id_for(workspace_id, path, proposed_sha256),
                    "conflict_path": conflict_path,
                    "current_sha256": current_sha256,
                    "proposed_sha256": proposed_sha256,
                    "current": current.model_dump(mode="json") if current is not None else None,
                    "proposed": (
                        proposed.model_dump(mode="json") if proposed is not None else None
                    ),
                    "diff": diff,
                }
            ),
        )

    async def _read_optional(
        self, workspace_id: str, path: str, expected_sha256: str | None
    ) -> WorkspaceConflictVersionV1 | None:
        try:
            content = await self._workspace_agent.read_content(workspace_id, path=path)
        except WorkspaceAgentError:
            return None
        if expected_sha256 is not None and content.sha256 != expected_sha256:
            return None
        return self._content_version(path=path, sha256=content.sha256, content=content.content)

    @staticmethod
    def _content_version(*, path: str, sha256: str, content: bytes) -> WorkspaceConflictVersionV1:
        text, _encoded = _decode_text(content)
        return WorkspaceConflictVersionV1(
            path=path,
            sha256=sha256,
            size=len(content),
            text=text,
            binary=text is None,
        )

    async def _read_verified_content(
        self, workspace_id: str, revision: WorkspaceFileRevisionV1
    ) -> WorkspaceContentV1:
        content = await self._workspace_agent.read_content(workspace_id, path=revision.path)
        if (
            content.sha256 != revision.sha256
            or _sha256(content.content) != revision.sha256
            or len(content.content) != revision.size
            or content.size != revision.size
        ):
            raise WorkspaceContentMismatchError(
                "the agent content does not match the canonical revision SHA-256",
                details={"path": revision.path, "revision_id": revision.revision_id},
            )
        return content

    # -------------------------------------------------------------- conflicts

    async def list_conflicts(self, investigation_id: UUID) -> WorkspaceConflictListV1:
        """Unresolved conflicts from the canonical ingest and decision tables."""
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        async with self._uow_factory() as uow:
            conflicts = await self._canonical_conflicts(uow, workspace_id)
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
        resolved = self._resolved_conflict_targets(decisions)
        unresolved = [conflict for conflict in conflicts if conflict.target_id not in resolved]
        probe = await self._workspace_agent.probe()
        available = probe.status == "available"
        views: list[WorkspaceConflictViewV1] = []
        for index, conflict in enumerate(unresolved[:MAX_FILE_PAGE_LIMIT]):
            current: WorkspaceConflictVersionV1 | None = None
            proposed: WorkspaceConflictVersionV1 | None = None
            diff: str | None = None
            if available and index < MAX_CONFLICT_DIFFS:
                current = await self._read_optional(
                    workspace_id, conflict.path, conflict.current_sha256
                )
                if conflict.conflict_path is not None:
                    proposed = await self._read_optional(
                        workspace_id, conflict.conflict_path, conflict.proposed_sha256
                    )
                diff = _bounded_diff(
                    current.text.encode("utf-8")
                    if current is not None and current.text is not None
                    else None,
                    proposed.text.encode("utf-8")
                    if proposed is not None and proposed.text is not None
                    else None,
                )
            views.append(
                WorkspaceConflictViewV1(
                    conflict_id=conflict.conflict_id,
                    path=conflict.path,
                    conflict_path=conflict.conflict_path,
                    current_sha256=conflict.current_sha256,
                    proposed_sha256=conflict.proposed_sha256,
                    origin=cast(Any, conflict.origin),
                    task_id=conflict.task_id,
                    observed_at=conflict.observed_at,
                    current=current,
                    proposed=proposed,
                    diff=diff,
                )
            )
        return WorkspaceConflictListV1(
            investigation_id=investigation_id,
            workspace_id=workspace_id,
            workspace_available=available,
            conflicts=tuple(views),
        )

    async def resolve_conflict(
        self,
        investigation_id: UUID,
        *,
        conflict_id: str,
        choice: ConflictChoice,
        expected_current_sha256: str,
        expected_proposed_sha256: str,
        merged_text: str | None,
        actor_id: str,
        idempotency_key: str,
        reason: str = "Human resolution of a host workspace conflict",
    ) -> WorkspaceConflictResolutionV1:
        """Apply a human conflict choice through CAS and record one decision."""
        _require_idempotency_key(idempotency_key)
        if _matches(conflict_id, CONFLICT_ID_PATTERN) is None:
            raise WorkspaceConflictNotFoundError("the conflict does not exist")
        if _matches(expected_proposed_sha256, SHA256_PATTERN) is None:
            raise WorkspaceServiceError("expected_proposed_sha256 must be a lowercase SHA-256")
        if (
            expected_current_sha256 != "new"
            and _matches(expected_current_sha256, SHA256_PATTERN) is None
        ):
            raise WorkspaceServiceError("expected_current_sha256 must be a SHA-256 or 'new'")
        if choice == "merged_text" and merged_text is None:
            raise WorkspaceServiceError("merged_text is required for the merged_text choice")
        if choice != "merged_text" and merged_text is not None:
            raise WorkspaceServiceError("merged_text is only accepted for the merged_text choice")
        binding = await self._binding(investigation_id)
        workspace_id = binding.workspace_id
        decision_id = _conflict_decision_id(investigation_id, idempotency_key)
        async with self._uow_factory() as uow:
            decisions = await uow.analyst_decisions.list_for_investigation(investigation_id)
            prior = next((decision for decision in decisions if decision.id == decision_id), None)
            conflicts = await self._canonical_conflicts(uow, workspace_id)
        if prior is not None:
            if (
                prior.decision_type is not AnalystDecisionType.WORKSPACE_CONFLICT_RESOLVE
                or prior.target_type is not AnalystDecisionTargetType.WORKSPACE_CONFLICT
                or prior.details.get("conflict_id") != conflict_id
            ):
                raise WorkspaceIdempotencyConflictError(
                    "Idempotency-Key was reused for a different conflict resolution"
                )
            return WorkspaceConflictResolutionV1(
                investigation_id=investigation_id,
                workspace_id=workspace_id,
                conflict_id=conflict_id,
                path=str(prior.details.get("path", "")),
                choice=cast(ConflictChoice, prior.details.get("choice", choice)),
                decision_id=decision_id,
                idempotent_replay=True,
            )
        resolved = self._resolved_conflict_targets(decisions)
        conflict = next((item for item in conflicts if item.conflict_id == conflict_id), None)
        if conflict is None:
            raise WorkspaceConflictNotFoundError("the conflict does not exist")
        if conflict.target_id in resolved:
            raise WorkspaceConflictResolvedError("the conflict is already resolved")
        if conflict.proposed_sha256 != expected_proposed_sha256:
            raise WorkspaceConflictStaleError(
                "the expected proposed SHA-256 is not the stored conflict",
                details={
                    "conflict_id": conflict_id,
                    "expected_proposed_sha256": expected_proposed_sha256,
                    "proposed_sha256": conflict.proposed_sha256,
                },
            )
        if conflict.current_sha256 != (
            None if expected_current_sha256 == "new" else expected_current_sha256
        ):
            raise WorkspaceConflictStaleError(
                "the conflict's live revision changed since the resolution request",
                details={
                    "conflict_id": conflict_id,
                    "expected_current_sha256": expected_current_sha256,
                    "current_sha256": conflict.current_sha256,
                },
            )
        if merged_text is not None and len(merged_text.encode("utf-8")) > EDITABLE_CONTENT_LIMIT:
            raise WorkspaceFileTooLargeError("the merged text exceeds the 8 MiB editable limit")
        current = await self._read_optional(workspace_id, conflict.path, conflict.current_sha256)
        proposed = (
            await self._read_optional(
                workspace_id, conflict.conflict_path, conflict.proposed_sha256
            )
            if conflict.conflict_path is not None
            else None
        )
        diff = _bounded_diff(
            current.text.encode("utf-8")
            if current is not None and current.text is not None
            else None,
            proposed.text.encode("utf-8")
            if proposed is not None and proposed.text is not None
            else None,
        )
        if choice == "keep_current":
            if current is None or current.text is None:
                raise WorkspaceConflictStaleError(
                    "the current version cannot be read through the agent",
                    details={"conflict_id": conflict_id},
                )
            content = current.text.encode("utf-8")
        elif choice == "keep_proposed":
            if proposed is None or proposed.text is None:
                raise WorkspaceConflictStaleError(
                    "the conflict copy cannot be read through the agent",
                    details={"conflict_id": conflict_id},
                )
            content = proposed.text.encode("utf-8")
        else:
            assert merged_text is not None
            content = merged_text.encode("utf-8")
        try:
            result = await self._workspace_agent.put_file(
                workspace_id,
                path=conflict.path,
                content=content,
                expected_sha256=expected_current_sha256,
                origin="ui",
                idempotency_key=_agent_idempotency_key(
                    "resolve", workspace_id, conflict_id, idempotency_key
                ),
            )
        except WorkspaceAgentError as exc:
            if exc.code != "revision_conflict":
                raise
            raise WorkspaceConflictStaleError(
                "the workspace changed while resolving; both versions were kept",
                details=cast(
                    dict[str, JsonValue],
                    _json_safe(
                        {
                            "conflict_id": conflict_id,
                            "current": exc.details.get("current"),
                            "proposed_sha256": exc.details.get("proposed_sha256"),
                            "conflict_path": exc.details.get("conflict_path"),
                            "new_conflict_id": exc.details.get("conflict_id"),
                        }
                    ),
                ),
            ) from exc
        decision = AnalystDecision(
            id=decision_id,
            investigation_id=investigation_id,
            decision_type=AnalystDecisionType.WORKSPACE_CONFLICT_RESOLVE,
            target_type=AnalystDecisionTargetType.WORKSPACE_CONFLICT,
            target_id=conflict.target_id,
            actor_id=actor_id,
            reason=reason.strip(),
            correlation_id=(f"workspace_conflict_resolve:{investigation_id}:{idempotency_key}")[
                :128
            ],
            details={
                "workspace_id": workspace_id,
                "conflict_id": conflict_id,
                "path": conflict.path,
                "conflict_path": conflict.conflict_path,
                "choice": choice,
                "current_sha256": conflict.current_sha256,
                "proposed_sha256": conflict.proposed_sha256,
                "expected_current_sha256": expected_current_sha256,
                "expected_proposed_sha256": expected_proposed_sha256,
                "result_sha256": result.revision.sha256,
                "result_revision_id": result.revision.revision_id,
            },
        )
        async with self._uow_factory() as uow:
            await uow.analyst_decisions.append(decision)
            await uow.commit()
        return WorkspaceConflictResolutionV1(
            investigation_id=investigation_id,
            workspace_id=workspace_id,
            conflict_id=conflict_id,
            path=conflict.path,
            choice=choice,
            decision_id=decision_id,
            result_revision=_version(result),
            current=current,
            proposed=proposed,
            diff=diff,
            idempotent_replay=False,
        )

    # ---------------------------------------------------------------- internal

    @staticmethod
    def _resolved_conflict_targets(decisions: Sequence[AnalystDecision]) -> set[UUID]:
        return {
            decision.target_id
            for decision in decisions
            if decision.decision_type is AnalystDecisionType.WORKSPACE_CONFLICT_RESOLVE
            and decision.target_type is AnalystDecisionTargetType.WORKSPACE_CONFLICT
        }

    @staticmethod
    def _latest_by_path(
        revisions: Sequence[WorkspaceFileRevisionV1],
    ) -> dict[str, tuple[WorkspaceFileRevisionV1, tuple[WorkspaceFileRevisionV1, ...]]]:
        grouped: dict[str, list[WorkspaceFileRevisionV1]] = {}
        for revision in revisions:
            grouped.setdefault(revision.path, []).append(revision)
        latest: dict[str, tuple[WorkspaceFileRevisionV1, tuple[WorkspaceFileRevisionV1, ...]]] = {}
        for path, items in grouped.items():
            ordered = sorted(
                items, key=lambda item: (item.observed_at, item.revision_id), reverse=True
            )
            latest[path] = (ordered[0], tuple(ordered))
        return latest

    @staticmethod
    def _latest_events(
        events: Sequence[WorkspaceFileEventV1],
    ) -> dict[tuple[str, str], WorkspaceFileEventV1]:
        selected: dict[tuple[str, str], WorkspaceFileEventV1] = {}
        for event in events:
            if event.path is None or event.revision_id is None:
                continue
            key = (event.path, event.revision_id)
            current = selected.get(key)
            if current is None or (event.generation, event.seq) > (
                current.generation,
                current.seq,
            ):
                selected[key] = event
        return selected

    @staticmethod
    def _conflict_copy_paths(
        revisions: Sequence[WorkspaceFileRevisionV1],
        latest_paths: set[str],
    ) -> dict[tuple[str, str], str]:
        """Map ``(live path, proposed sha)`` to the agent's conflict copy path.

        The copy name is ``<stem>.conflict-<hex><suffix>``; the hex length is
        8, 16 or 64 (agent §7), so the live path is recovered by trying each
        length and keeping only candidates that exist as canonical paths.
        """
        copies: dict[tuple[str, str], str] = {}
        for revision in revisions:
            directory, _, name = revision.path.rpartition("/")
            if CONFLICT_INFIX not in name:
                continue
            stem, _, remainder = name.partition(CONFLICT_INFIX)
            for length in CONFLICT_HEX_LENGTHS:
                if len(remainder) < length:
                    continue
                suffix = remainder[length:]
                if suffix and not suffix.startswith("."):
                    continue
                candidate = f"{directory}/{stem}{suffix}" if directory else f"{stem}{suffix}"
                if candidate in latest_paths:
                    copies.setdefault((candidate, revision.sha256), revision.path)
                    break
        return copies

    async def _canonical_conflicts(
        self, uow: WorkspaceUnitOfWork, workspace_id: str
    ) -> tuple[_CanonicalConflict, ...]:
        events = await uow.workspace_file_events.list_for_workspace(workspace_id)
        revisions = await uow.workspace_file_revisions.list_for_workspace(workspace_id)
        latest = self._latest_by_path(revisions)
        copies = self._conflict_copy_paths(revisions, set(latest))
        conflicts: list[_CanonicalConflict] = []
        seen: set[str] = set()
        for event in events:
            if event.kind != "conflict_created" or event.path is None or event.sha256 is None:
                continue
            conflict_id = conflict_id_for(workspace_id, event.path, event.sha256)
            if conflict_id in seen:
                continue
            seen.add(conflict_id)
            current = latest.get(event.path)
            conflicts.append(
                _CanonicalConflict(
                    conflict_id=conflict_id,
                    target_id=_conflict_target_id(workspace_id, event.path, event.sha256),
                    path=event.path,
                    conflict_path=copies.get((event.path, event.sha256)),
                    current_sha256=current[0].sha256 if current is not None else None,
                    proposed_sha256=event.sha256,
                    origin=event.origin,
                    task_id=event.task_id,
                    observed_at=event.observed_at,
                )
            )
        conflicts.sort(key=lambda item: (item.observed_at, item.conflict_id))
        return tuple(conflicts)


__all__ = [
    "DIFF_CHAR_LIMIT",
    "EDITABLE_CONTENT_LIMIT",
    "MAX_SYNC_PAGES",
    "RESULTS_SUMMARY_PATH",
    "UI_CONTENT_LIMIT",
    "WORKSPACE_UNAVAILABLE_MESSAGE",
    "ConflictChoice",
    "InvestigationNotFoundError",
    "InvestigationWorkspaceService",
    "WorkspaceBindingConflictError",
    "WorkspaceConflictListV1",
    "WorkspaceConflictNotFoundError",
    "WorkspaceConflictResolutionV1",
    "WorkspaceConflictResolvedError",
    "WorkspaceConflictStaleError",
    "WorkspaceConflictViewV1",
    "WorkspaceContentMismatchError",
    "WorkspaceContentViewV1",
    "WorkspaceCountsV1",
    "WorkspaceEnsureResultV1",
    "WorkspaceExportRefusedError",
    "WorkspaceExportReportV1",
    "WorkspaceFileListV1",
    "WorkspaceFileNotFoundError",
    "WorkspaceFileTooLargeError",
    "WorkspaceFileViewV1",
    "WorkspaceIdempotencyConflictError",
    "WorkspaceIdentityUnavailableError",
    "WorkspaceNotBoundError",
    "WorkspaceNoteWriteResultV1",
    "WorkspacePathNotEditableError",
    "WorkspaceRevisionConflictError",
    "WorkspaceServiceError",
    "WorkspaceStatusV1",
    "WorkspaceSyncBusyError",
    "WorkspaceSyncError",
    "WorkspaceSyncReportV1",
    "WorkspaceSynthesisNotVerifiedError",
    "WorkspaceSynthesisPinError",
    "WorkspaceUnitOfWork",
    "WorkspaceUnitOfWorkFactory",
    "article_slug_for_subject",
    "conflict_id_for",
    "delivery_code_for_edition",
    "plan_export_path",
    "render_plan_markdown",
]
