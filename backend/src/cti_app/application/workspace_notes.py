"""Human note annotations and note-driven actions (M24-WS2, ADR-0011 D6).

A local Markdown note is a first-class *work object* of the workshop, never
evidence.  This service owns the human annotation layer and the four actions a
note can initiate:

* ``annotate`` records one append-only human action on one per-file workspace
  revision (link a task/IOC/sample of the same investigation, mark useful,
  explicitly attribute an author, dismiss);
* ``list_note_view`` joins the canonical ingest (latest revision + history)
  with annotations, imported contributions and the agent instructions,
  handoffs and Bridge consultations that already referenced the note;
* ``ask_bridge_review`` sends a targeted ``advisor.consult`` request through
  the existing advisor path.  A Bridge outage leaves the request pending; no
  text is ever fabricated and no note becomes publishable prose;
* ``ask_agent_continue`` persists a session instruction that references the
  *latest* revision at send time (exact id and SHA-256);
* ``propose_verification_task`` opens a ``proposed`` task through the 04B task
  engine: a human intuition may become a verification task, never a finding;
* ``propose_observation_conversion`` returns the typed declaration an import
  would need, using the closed ``(object_type, method)`` mapping of D6.  It
  writes nothing canonical; the actual import stays the 07 ImportService under
  a human decision.

Authority: every action is a verified human action.  ``do_not_submit`` is a
sample-level guard and is not consulted here; G2, TLP, ``external_llm_allowed``
and provider boundaries remain enforced by the one advisor path.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from cti_app.application.investigation_persistence import (
    AgentSessionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    InvestigationHandoffRepository,
    InvestigationTaskRepository,
    SampleAssociationRepository,
    ToolInvocationRepository,
    VersionConflictError,
    WorkContributionRepository,
    WorkspaceBindingRepository,
    WorkspaceFileRevisionRepository,
    WorkspaceNoteAnnotationRepository,
    same_workspace_note_annotation_request,
)
from cti_app.application.investigation_tasks import (
    InvestigationTaskService,
    TaskCommandResult,
    TaskSessionOutcome,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
    BlobRepository,
    IndicatorRepository,
    SampleRepository,
)
from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
)
from cti_app.domain.agentic_operator import ACTIVE_AGENT_SESSION_STATES, AgentSessionV1
from cti_app.domain.collection import Indicator, IndicatorKind
from cti_app.domain.investigation import (
    canonical_json_bytes,
    is_advisory_observation,
)
from cti_app.domain.investigation_task import (
    TASK_LIMIT_DEFAULTS,
    InvestigationTaskV1,
    TaskActor,
    TaskActorKind,
    TaskLimits,
    TaskPriority,
)
from cti_app.domain.production import AnalystInvestigation
from cti_app.domain.work_contribution import (
    ContributionValidationError,
    ObservationDeclarationV1,
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionMethod,
    WorkContributionObjectType,
    WorkContributionV1,
)
from cti_app.domain.workspace_note_annotation import (
    NoteAnnotationActorKind,
    NoteAnnotationKind,
    NoteIocType,
    WorkspaceNoteAnnotationV1,
    is_note_path,
)

ADVISOR_TOOL_NAME = "advisor.consult"
NOTE_REVIEW_CONSULTATION_TYPES = frozenset({"interpretation", "critique"})
MAX_QUESTION_CHARS = 8_000
MAX_NOTE_EXCERPT_CHARS = 20_000
MAX_PASSAGE_END = 10_000_000
NOTE_VERIFICATION_POLICY_KEY = "note_reference"

_PASSAGE_LIMITATION_PREFIX = "source_note_reference:"


class NoteErrorCode(StrEnum):
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    WORKSPACE_NOT_BOUND = "workspace_not_bound"
    NOTE_NOT_FOUND = "note_not_found"
    INVALID_NOTE_PATH = "invalid_note_path"
    INVALID_ANNOTATION = "invalid_note_annotation"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    TASK_NOT_IN_INVESTIGATION = "task_not_in_investigation"
    IOC_NOT_IN_INVESTIGATION = "ioc_not_in_investigation"
    SAMPLE_NOT_IN_INVESTIGATION = "sample_not_in_investigation"
    INVALID_CITATION = "invalid_note_citation"
    ADVISORY_NOT_EVIDENCE = "advisory_observation_not_evidence"
    INVALID_PASSAGE = "invalid_note_passage"
    INVALID_OBSERVATION_CONVERSION = "invalid_observation_conversion"
    AGENT_SESSION_REQUIRED = "agent_session_required"
    PORT_UNAVAILABLE = "note_action_unavailable"
    INVALID_REQUEST = "invalid_note_request"


class NoteError(ValueError):
    """Typed refusal for a note annotation or note action."""

    def __init__(
        self,
        code: NoteErrorCode,
        *,
        status_code: int = 422,
        message: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message or code.value)


class WorkspaceNoteUnitOfWork(Protocol):
    """The narrow repository slice this service needs.

    Concrete unit of work types satisfy it structurally; the in-memory fakes
    used by tests only implement these attributes.
    """

    analyst_investigations: AnalystInvestigationRepository
    analyst_decisions: AnalystDecisionRepository
    workspace_bindings: WorkspaceBindingRepository
    workspace_file_revisions: WorkspaceFileRevisionRepository
    workspace_note_annotations: WorkspaceNoteAnnotationRepository
    investigation_tasks: InvestigationTaskRepository
    sample_associations: SampleAssociationRepository
    samples: SampleRepository
    blobs: BlobRepository
    indicators: IndicatorRepository
    work_contributions: WorkContributionRepository
    tool_invocations: ToolInvocationRepository
    investigation_handoffs: InvestigationHandoffRepository
    agent_sessions: AgentSessionRepository
    evidence_observations: EvidenceObservationRepository
    investigation_findings: FindingRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class WorkspaceNoteUnitOfWorkFactory(Protocol):
    def __call__(self) -> WorkspaceNoteUnitOfWork: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class NotePassageRangeV1:
    """A passage quoted *by reference* (start/end offsets on the exact revision)."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or isinstance(self.end, bool):
            raise NoteError(NoteErrorCode.INVALID_PASSAGE, message="passage offsets must be ints")
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise NoteError(NoteErrorCode.INVALID_PASSAGE, message="passage offsets must be ints")
        if self.start < 0 or self.end <= self.start or self.end > MAX_PASSAGE_END:
            raise NoteError(
                NoteErrorCode.INVALID_PASSAGE,
                message="a passage range requires 0 <= start < end <= 10000000",
            )

    def to_payload(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteRevisionViewV1:
    """One canonical per-file revision of a note (metadata only)."""

    revision_id: str
    path: str
    sha256: str
    size: int
    observed_at: datetime
    origin: str
    supersedes_revision_id: str | None = None

    @classmethod
    def from_revision(cls, revision: WorkspaceFileRevisionV1) -> NoteRevisionViewV1:
        return cls(
            revision_id=revision.revision_id,
            path=revision.path,
            sha256=revision.sha256,
            size=revision.size,
            observed_at=revision.observed_at,
            origin=revision.origin,
            supersedes_revision_id=revision.supersedes_revision_id,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "observed_at": self.observed_at.isoformat(),
            "origin": self.origin,
            "supersedes_revision_id": self.supersedes_revision_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteInstructionUsageV1:
    instruction_id: UUID
    session_id: UUID
    state: str
    text_sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "instruction_id": str(self.instruction_id),
            "session_id": str(self.session_id),
            "state": self.state,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteHandoffUsageV1:
    handoff_id: UUID
    task_id: UUID
    version: int

    def to_payload(self) -> dict[str, object]:
        return {
            "handoff_id": str(self.handoff_id),
            "task_id": str(self.task_id),
            "version": self.version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteConsultationUsageV1:
    invocation_id: UUID
    operation_key: str
    consultation_type: str
    status: str

    def to_payload(self) -> dict[str, object]:
        return {
            "invocation_id": str(self.invocation_id),
            "operation_key": self.operation_key,
            "consultation_type": self.consultation_type,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceNoteViewV1:
    """The cockpit projection of one note path."""

    investigation_id: UUID
    workspace_id: str
    path: str
    latest: NoteRevisionViewV1
    versions: tuple[NoteRevisionViewV1, ...]
    annotations: tuple[WorkspaceNoteAnnotationV1, ...]
    linked_task_ids: tuple[UUID, ...]
    linked_iocs: tuple[tuple[NoteIocType, str], ...]
    linked_sample_ids: tuple[UUID, ...]
    useful: bool
    dismissed: bool
    attributed_authors: tuple[str, ...]
    observation_imported: bool
    is_evidence: bool
    unverified_hypothesis: bool
    contributions: tuple[WorkContributionV1, ...]
    instructions: tuple[NoteInstructionUsageV1, ...]
    handoffs: tuple[NoteHandoffUsageV1, ...]
    consultations: tuple[NoteConsultationUsageV1, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "investigation_id": str(self.investigation_id),
            "workspace_id": self.workspace_id,
            "path": self.path,
            "latest": self.latest.to_payload(),
            "versions": [item.to_payload() for item in self.versions],
            "annotations": [item.to_payload() for item in self.annotations],
            "linked_task_ids": [str(item) for item in self.linked_task_ids],
            "linked_iocs": [
                {"ioc_type": ioc_type.value, "value": value} for ioc_type, value in self.linked_iocs
            ],
            "linked_sample_ids": [str(item) for item in self.linked_sample_ids],
            "useful": self.useful,
            "dismissed": self.dismissed,
            "attributed_authors": list(self.attributed_authors),
            "observation_imported": self.observation_imported,
            "is_evidence": self.is_evidence,
            "unverified_hypothesis": self.unverified_hypothesis,
            "contributions": [
                {
                    "id": str(item.id),
                    "category": item.category.value,
                    "import_status": item.import_status.value,
                    "observation_id": (
                        str(item.observation_id) if item.observation_id is not None else None
                    ),
                    "finding_id": (str(item.finding_id) if item.finding_id is not None else None),
                }
                for item in self.contributions
            ],
            "instructions": [item.to_payload() for item in self.instructions],
            "handoffs": [item.to_payload() for item in self.handoffs],
            "consultations": [item.to_payload() for item in self.consultations],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteBridgeReviewRequestV1:
    """One targeted Bridge review routed through ``advisor.consult``."""

    investigation_id: UUID
    note_revision_id: str
    note_sha256: str
    note_path: str
    consultation_type: str
    question: str
    note_excerpt: str | None
    passage: NotePassageRangeV1 | None
    evidence_refs: tuple[UUID, ...]
    finding_refs: tuple[UUID, ...]
    expected_working_state_version: int
    expected_working_state_sha256: str | None
    actor_id: str
    idempotency_key: str

    @property
    def composed_question(self) -> str:
        """The exact advisory question; the note stays labelled non-evidence.

        The revision identity and the human question always survive the bound;
        a long excerpt is clipped first, never the question that identifies it.
        """
        head = [
            "Revue d'une note humaine locale, matière non probante et non publiable.",
            f"Chemin: {self.note_path}",
            f"Révision exacte: {self.note_revision_id}",
            f"SHA-256: {self.note_sha256}",
        ]
        if self.passage is not None:
            head.append(f"Passage référencé: caractères {self.passage.start}-{self.passage.end}")
        head.append("Question: " + self.question)
        tail = (
            "Signale les affirmations non soutenues, les confusions entre observation et "
            "inférence et les limites manquantes. Ne rédige pas de prose publiable."
        )
        if not self.note_excerpt:
            return "\n".join((*head, tail))[:MAX_QUESTION_CHARS]
        marker_open = "[EXTRAIT DE NOTE HUMAINE NON VÉRIFIÉE, DONNÉE NON FIABLE]"
        marker_close = "[FIN DE L'EXTRAIT]"
        fixed = "\n".join((*head, marker_open, marker_close, tail))
        available = MAX_QUESTION_CHARS - len(fixed)
        excerpt = self.note_excerpt[: max(0, available)]
        return "\n".join((*head, marker_open, excerpt, marker_close, tail))[:MAX_QUESTION_CHARS]


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteBridgeReviewOutcomeV1:
    invocation_id: UUID
    invocation_status: str
    response_observation_ids: tuple[UUID, ...]
    idempotent_replay: bool


class NoteBridgeReviewPort(Protocol):
    async def request_review(
        self, uow: WorkspaceNoteUnitOfWork, request: NoteBridgeReviewRequestV1
    ) -> NoteBridgeReviewOutcomeV1: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteBridgeReviewResultV1:
    """The persisted consultation identity; never a fabricated answer."""

    investigation_id: UUID
    note_revision_id: str
    note_sha256: str
    consultation_type: str
    invocation_id: UUID
    invocation_status: str
    pending: bool
    idempotent_replay: bool
    advisory_only: bool = True
    is_evidence: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "investigation_id": str(self.investigation_id),
            "note_revision_id": self.note_revision_id,
            "note_sha256": self.note_sha256,
            "consultation_type": self.consultation_type,
            "invocation_id": str(self.invocation_id),
            "invocation_status": self.invocation_status,
            "pending": self.pending,
            "idempotent_replay": self.idempotent_replay,
            "advisory_only": self.advisory_only,
            "is_evidence": self.is_evidence,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteInstructionRequestV1:
    investigation_id: UUID
    task_id: UUID
    session_id: UUID
    note_revision_id: str
    note_sha256: str
    note_path: str
    text: str
    actor_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteInstructionReceiptV1:
    instruction_id: UUID
    session_id: UUID
    state: str
    text_sha256: str


class NoteInstructionPort(Protocol):
    async def enqueue(self, request: NoteInstructionRequestV1) -> NoteInstructionReceiptV1: ...


class InMemoryNoteInstructionPort:
    """Test double: records the exact instruction and returns its receipt."""

    def __init__(self) -> None:
        self.requests: list[NoteInstructionRequestV1] = []

    async def enqueue(self, request: NoteInstructionRequestV1) -> NoteInstructionReceiptV1:
        self.requests.append(request)
        return NoteInstructionReceiptV1(
            instruction_id=uuid4(),
            session_id=request.session_id,
            state="queued",
            text_sha256=hashlib.sha256(request.text.encode("utf-8")).hexdigest(),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteAgentContinuationV1:
    investigation_id: UUID
    note_revision_id: str
    note_sha256: str
    note_path: str
    task_id: UUID
    session_id: UUID
    instruction_id: UUID
    instruction_state: str
    instruction_sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "investigation_id": str(self.investigation_id),
            "note_revision_id": self.note_revision_id,
            "note_sha256": self.note_sha256,
            "note_path": self.note_path,
            "task_id": str(self.task_id),
            "session_id": str(self.session_id),
            "instruction_id": str(self.instruction_id),
            "instruction_state": self.instruction_state,
            "instruction_sha256": self.instruction_sha256,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class NoteObservationConversionProposalV1:
    """What an import as observation would need; nothing canonical is written."""

    investigation_id: UUID
    workspace_id: str
    note_revision_id: str
    note_sha256: str
    note_path: str
    passage: NotePassageRangeV1 | None
    declaration: ObservationDeclarationV1
    import_category: WorkContributionCategory = WorkContributionCategory.OBSERVATION
    import_service: str = "WorkContributionService.import_revision"
    requires_human_decision: bool = True
    canonical_writes: tuple[str, ...] = ()
    is_evidence: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "investigation_id": str(self.investigation_id),
            "workspace_id": self.workspace_id,
            "note_revision_id": self.note_revision_id,
            "note_sha256": self.note_sha256,
            "note_path": self.note_path,
            "passage": self.passage.to_payload() if self.passage is not None else None,
            "object_type": self.declaration.object_type.value,
            "method": self.declaration.method.value,
            "object_id": self.declaration.object_id,
            "tool": self.declaration.tool,
            "tool_version": self.declaration.tool_version,
            "parameters": dict(self.declaration.parameters),
            "limitations": list(self.declaration.limitations),
            "evidence_kind": self.declaration.evidence_kind.value,
            "import_category": self.import_category.value,
            "import_service": self.import_service,
            "requires_human_decision": self.requires_human_decision,
            "canonical_writes": list(self.canonical_writes),
            "is_evidence": self.is_evidence,
        }


class _NoLeaseTaskSessionPort:
    """``create_task`` never touches a session; this port is fail-closed."""

    async def is_active_session(
        self, *, session_id: UUID, investigation_id: UUID, task_id: UUID
    ) -> bool:
        del session_id, investigation_id, task_id
        return False

    async def close_session(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        outcome: TaskSessionOutcome,
        reason: str,
    ) -> None:
        del session_id, investigation_id, task_id, outcome, reason
        return None


_IOC_INDICATOR_KINDS: Mapping[NoteIocType, IndicatorKind] = {
    NoteIocType.MD5: IndicatorKind.HASH,
    NoteIocType.SHA1: IndicatorKind.HASH,
    NoteIocType.SHA256: IndicatorKind.HASH,
    NoteIocType.IPV4: IndicatorKind.IP,
    NoteIocType.DOMAIN: IndicatorKind.DOMAIN,
    NoteIocType.URL: IndicatorKind.URL,
}
_REVISION_ID_RE = re.compile(r"^rev-[0-9a-f]{32}$")


def _normalized(value: str) -> str:
    return value.strip().casefold()


class WorkspaceNoteService:
    """Append-only human note annotations, note views and note-driven actions."""

    def __init__(
        self,
        *,
        uow_factory: WorkspaceNoteUnitOfWorkFactory,
        bridge: NoteBridgeReviewPort | None = None,
        instructions: NoteInstructionPort | None = None,
        task_limits: TaskLimits = TASK_LIMIT_DEFAULTS,
    ) -> None:
        self._uow_factory = uow_factory
        self._bridge = bridge
        self._instructions = instructions
        self._task_limits = task_limits

    # -------------------------------------------------------------- annotate

    async def annotate(
        self,
        investigation_id: UUID,
        revision_id: str,
        *,
        kind: NoteAnnotationKind,
        actor_id: str,
        idempotency_key: str,
        task_id: UUID | None = None,
        ioc_value: str | None = None,
        ioc_type: NoteIocType | None = None,
        sample_id: UUID | None = None,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> WorkspaceNoteAnnotationV1:
        """Append one human annotation; a replay returns the stored row."""
        if not isinstance(kind, NoteAnnotationKind):
            raise NoteError(NoteErrorCode.INVALID_ANNOTATION, message="unknown annotation kind")
        _require_text(actor_id, "actor_id")
        _require_text(idempotency_key, "idempotency_key")
        timestamp = now or datetime.now(UTC)
        async with self._uow_factory() as uow:
            investigation = await self._require_investigation(uow, investigation_id)
            binding = await self._require_binding(uow, investigation_id)
            revision = await self._require_revision(uow, binding, revision_id)
            try:
                candidate = WorkspaceNoteAnnotationV1(
                    investigation_id=investigation_id,
                    workspace_id=binding.workspace_id,
                    revision_id=revision.revision_id,
                    file_relpath=revision.path,
                    kind=kind,
                    actor_kind=NoteAnnotationActorKind.HUMAN,
                    actor_id=actor_id.strip(),
                    target_task_id=task_id,
                    ioc_value=ioc_value,
                    ioc_type=ioc_type,
                    target_sample_id=sample_id,
                    detail=detail,
                    idempotency_key=idempotency_key,
                    created_at=timestamp,
                )
            except (TypeError, ValueError) as exc:
                raise NoteError(NoteErrorCode.INVALID_ANNOTATION, message=str(exc)) from exc
            existing = await uow.workspace_note_annotations.get_by_idempotency(
                investigation_id, idempotency_key
            )
            if existing is not None:
                if not same_workspace_note_annotation_request(existing, candidate):
                    raise NoteError(
                        NoteErrorCode.IDEMPOTENCY_CONFLICT,
                        status_code=409,
                        message="The idempotency key records a different annotation",
                    )
                return existing
            await self._validate_target(uow, investigation, candidate)
            try:
                stored, _created = await uow.workspace_note_annotations.append_if_absent(candidate)
            except VersionConflictError as exc:
                raise NoteError(
                    NoteErrorCode.IDEMPOTENCY_CONFLICT, status_code=409, message=str(exc)
                ) from exc
            await uow.commit()
            return stored

    # ------------------------------------------------------------------ view

    async def list_note_view(self, investigation_id: UUID, note_path: str) -> WorkspaceNoteViewV1:
        """Join canonical ingest, annotations, contributions and note usage."""
        if not is_note_path(note_path):
            raise NoteError(
                NoteErrorCode.INVALID_NOTE_PATH,
                message="a note path is relative and starts with 'notes/'",
            )
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await self._require_binding(uow, investigation_id)
            history = tuple(
                sorted(
                    (
                        item
                        for item in await uow.workspace_file_revisions.list_for_workspace(
                            binding.workspace_id
                        )
                        if item.path == note_path
                    ),
                    key=lambda item: (item.observed_at, item.revision_id),
                )
            )
            if not history:
                raise NoteError(NoteErrorCode.NOTE_NOT_FOUND, status_code=404)
            latest = history[-1]
            annotations = await uow.workspace_note_annotations.list_for_path(
                investigation_id, note_path
            )
            contributions = tuple(
                item
                for item in await uow.work_contributions.list_for_investigation(investigation_id)
                if item.file_relpath == note_path and item.workspace_id == binding.workspace_id
            )
            instruction_usage, handoff_usage, consultation_usage = await self._usage(
                uow,
                investigation_id,
                note_path=note_path,
                history=history,
            )
            latest_annotations = tuple(
                item for item in annotations if item.revision_id == latest.revision_id
            )
            useful = False
            for item in latest_annotations:
                if item.kind is NoteAnnotationKind.MARK_USEFUL:
                    useful = True
                elif item.kind is NoteAnnotationKind.UNMARK_USEFUL:
                    useful = False
            attributed_authors = tuple(
                dict.fromkeys(
                    item.detail
                    for item in annotations
                    if item.kind is NoteAnnotationKind.ATTRIBUTE_AUTHOR and item.detail is not None
                )
            )
            observation_imported = any(
                item.category is WorkContributionCategory.OBSERVATION
                and item.import_status is WorkContributionImportStatus.IMPORTED
                and item.observation_id is not None
                for item in contributions
            )
            return WorkspaceNoteViewV1(
                investigation_id=investigation_id,
                workspace_id=binding.workspace_id,
                path=note_path,
                latest=NoteRevisionViewV1.from_revision(latest),
                versions=tuple(NoteRevisionViewV1.from_revision(item) for item in history),
                annotations=annotations,
                linked_task_ids=tuple(
                    dict.fromkeys(
                        item.target_task_id
                        for item in annotations
                        if item.kind is NoteAnnotationKind.LINK_TASK
                        and item.target_task_id is not None
                    )
                ),
                linked_iocs=tuple(
                    dict.fromkeys(
                        (item.ioc_type, item.ioc_value)
                        for item in annotations
                        if item.kind is NoteAnnotationKind.LINK_IOC
                        and item.ioc_type is not None
                        and item.ioc_value is not None
                    )
                ),
                linked_sample_ids=tuple(
                    dict.fromkeys(
                        item.target_sample_id
                        for item in annotations
                        if item.kind is NoteAnnotationKind.LINK_SAMPLE
                        and item.target_sample_id is not None
                    )
                ),
                useful=useful,
                dismissed=any(item.kind is NoteAnnotationKind.DISMISS for item in annotations),
                attributed_authors=attributed_authors,
                observation_imported=observation_imported,
                is_evidence=observation_imported,
                unverified_hypothesis=not observation_imported,
                contributions=contributions,
                instructions=instruction_usage,
                handoffs=handoff_usage,
                consultations=consultation_usage,
            )

    # ---------------------------------------------------------- ask bridge

    async def ask_bridge_review(
        self,
        investigation_id: UUID,
        revision_id: str,
        *,
        question: str,
        actor_id: str,
        idempotency_key: str,
        consultation_type: str = "critique",
        note_excerpt: str | None = None,
        passage: NotePassageRangeV1 | None = None,
        evidence_refs: Sequence[UUID] = (),
        finding_refs: Sequence[UUID] = (),
        expected_working_state_version: int,
        expected_working_state_sha256: str | None = None,
    ) -> NoteBridgeReviewResultV1:
        """Route one note review through the existing advisor consultation path."""
        if self._bridge is None:
            raise NoteError(NoteErrorCode.PORT_UNAVAILABLE, status_code=503)
        if consultation_type not in NOTE_REVIEW_CONSULTATION_TYPES:
            raise NoteError(
                NoteErrorCode.INVALID_REQUEST,
                message="a note review is 'interpretation' or 'critique'",
            )
        _require_text(question, "question")
        if len(question) > MAX_QUESTION_CHARS:
            raise NoteError(NoteErrorCode.INVALID_REQUEST, message="question is too long")
        if note_excerpt is not None:
            if not isinstance(note_excerpt, str) or not note_excerpt.strip():
                raise NoteError(NoteErrorCode.INVALID_REQUEST, message="excerpt is empty")
            if len(note_excerpt) > MAX_NOTE_EXCERPT_CHARS:
                raise NoteError(NoteErrorCode.INVALID_REQUEST, message="excerpt is too long")
        if expected_working_state_version < 1:
            raise NoteError(NoteErrorCode.INVALID_REQUEST, message="working state version < 1")
        _require_text(actor_id, "actor_id")
        _require_text(idempotency_key, "idempotency_key")
        cited_evidence = tuple(dict.fromkeys(evidence_refs))
        cited_findings = tuple(dict.fromkeys(finding_refs))
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await self._require_binding(uow, investigation_id)
            revision = await self._require_revision(uow, binding, revision_id)
            await self._validate_citations(uow, investigation_id, cited_evidence, cited_findings)
            outcome = await self._bridge.request_review(
                uow,
                NoteBridgeReviewRequestV1(
                    investigation_id=investigation_id,
                    note_revision_id=revision.revision_id,
                    note_sha256=revision.sha256,
                    note_path=revision.path,
                    consultation_type=consultation_type,
                    question=question.strip(),
                    note_excerpt=note_excerpt.strip() if note_excerpt is not None else None,
                    passage=passage,
                    evidence_refs=cited_evidence,
                    finding_refs=cited_findings,
                    expected_working_state_version=expected_working_state_version,
                    expected_working_state_sha256=expected_working_state_sha256,
                    actor_id=actor_id.strip(),
                    idempotency_key=idempotency_key,
                ),
            )
            await uow.commit()
        return NoteBridgeReviewResultV1(
            investigation_id=investigation_id,
            note_revision_id=revision.revision_id,
            note_sha256=revision.sha256,
            consultation_type=consultation_type,
            invocation_id=outcome.invocation_id,
            invocation_status=outcome.invocation_status,
            pending=not outcome.response_observation_ids,
            idempotent_replay=outcome.idempotent_replay,
        )

    # ----------------------------------------------------------- ask agent

    async def ask_agent_continue(
        self,
        investigation_id: UUID,
        revision_id: str,
        *,
        task_id: UUID,
        question: str,
        actor_id: str,
        idempotency_key: str,
        session_id: UUID | None = None,
    ) -> NoteAgentContinuationV1:
        """Persist a session instruction referencing the latest revision at send."""
        if self._instructions is None:
            raise NoteError(NoteErrorCode.PORT_UNAVAILABLE, status_code=503)
        _require_text(question, "question")
        if len(question) > MAX_QUESTION_CHARS:
            raise NoteError(NoteErrorCode.INVALID_REQUEST, message="question is too long")
        _require_text(actor_id, "actor_id")
        _require_text(idempotency_key, "idempotency_key")
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await self._require_binding(uow, investigation_id)
            revision = await self._require_revision(uow, binding, revision_id)
            task = await self._require_task(uow, investigation_id, task_id)
            history = tuple(
                sorted(
                    (
                        item
                        for item in await uow.workspace_file_revisions.list_for_workspace(
                            binding.workspace_id
                        )
                        if item.path == revision.path
                    ),
                    key=lambda item: (item.observed_at, item.revision_id),
                )
            )
            latest = history[-1] if history else revision
            session = await self._resolve_session(uow, investigation_id, task, session_id)
            text = _agent_instruction_text(latest.path, latest.revision_id, latest.sha256, question)
            request = NoteInstructionRequestV1(
                investigation_id=investigation_id,
                task_id=task.id,
                session_id=session.id,
                note_revision_id=latest.revision_id,
                note_sha256=latest.sha256,
                note_path=latest.path,
                text=text,
                actor_id=actor_id.strip(),
                idempotency_key=idempotency_key,
            )
        receipt = await self._instructions.enqueue(request)
        return NoteAgentContinuationV1(
            investigation_id=investigation_id,
            note_revision_id=latest.revision_id,
            note_sha256=latest.sha256,
            note_path=latest.path,
            task_id=task.id,
            session_id=receipt.session_id,
            instruction_id=receipt.instruction_id,
            instruction_state=receipt.state,
            instruction_sha256=receipt.text_sha256,
        )

    # --------------------------------------------------- verification task

    async def propose_verification_task(
        self,
        investigation_id: UUID,
        revision_id: str,
        *,
        passage: NotePassageRangeV1,
        question: str,
        actor_id: str,
        idempotency_key: str,
        evidence_refs: Sequence[UUID] = (),
        claim_refs: Sequence[str] = (),
        task_id: UUID | None = None,
        now: datetime | None = None,
    ) -> TaskCommandResult:
        """Create one ``proposed`` verification task; never a finding."""
        _require_text(question, "question")
        if len(question) > MAX_QUESTION_CHARS:
            raise NoteError(NoteErrorCode.INVALID_REQUEST, message="question is too long")
        _require_text(actor_id, "actor_id")
        _require_text(idempotency_key, "idempotency_key")
        timestamp = now or datetime.now(UTC)
        cited_evidence = tuple(dict.fromkeys(evidence_refs))
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await self._require_binding(uow, investigation_id)
            revision = await self._require_revision(uow, binding, revision_id)
            await self._validate_citations(uow, investigation_id, cited_evidence, ())
            task_key = _verification_task_key(investigation_id, revision.revision_id, passage)
            service = InvestigationTaskService(
                tasks=uow.investigation_tasks,
                decisions=uow.analyst_decisions,
                sessions=_NoLeaseTaskSessionPort(),
                limits=self._task_limits,
            )
            result = await service.create_task(
                investigation_id=investigation_id,
                actor=TaskActor(kind=TaskActorKind.HUMAN, actor_id=actor_id.strip()),
                idempotency_key=idempotency_key,
                task_key=task_key,
                task_id=task_id,
                title=f"Vérifier la note {revision.path} (passage {passage.start}-{passage.end})"[
                    :240
                ],
                question=_verification_question(
                    revision.path, revision.revision_id, revision.sha256, passage, question
                ),
                objective=(
                    "Confirmer ou infirmer par des observations canoniques de la même enquête."
                ),
                priority=TaskPriority.NORMAL,
                claim_refs=tuple(claim_refs),
                evidence_refs=tuple(str(item) for item in cited_evidence),
                allowed_workspace_paths=(revision.path,),
                execution_policy={
                    NOTE_VERIFICATION_POLICY_KEY: {
                        "workspace_id": binding.workspace_id,
                        "revision_id": revision.revision_id,
                        "file_relpath": revision.path,
                        "note_sha256": revision.sha256,
                        "passage": passage.to_payload(),
                    },
                    "note_is_evidence": False,
                },
                now=timestamp,
            )
            await uow.commit()
            return result

    # ------------------------------------------- observation proposal

    async def propose_observation_conversion(
        self,
        investigation_id: UUID,
        revision_id: str,
        *,
        object_type: WorkContributionObjectType,
        method: WorkContributionMethod,
        tool: str,
        tool_version: str = "unspecified",
        object_id: str | None = None,
        parameters: Mapping[str, Any] | None = None,
        limitations: Sequence[str] = (),
        passage: NotePassageRangeV1 | None = None,
        observed_at: datetime | None = None,
    ) -> NoteObservationConversionProposalV1:
        """Type what an observation import would need; write nothing canonical."""
        if not isinstance(object_type, WorkContributionObjectType):
            raise NoteError(NoteErrorCode.INVALID_OBSERVATION_CONVERSION)
        if not isinstance(method, WorkContributionMethod):
            raise NoteError(NoteErrorCode.INVALID_OBSERVATION_CONVERSION)
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            binding = await self._require_binding(uow, investigation_id)
            revision = await self._require_revision(uow, binding, revision_id)
        resolved_object_id = object_id
        if resolved_object_id is None and object_type in {
            WorkContributionObjectType.SAMPLE,
            WorkContributionObjectType.FILE,
        }:
            resolved_object_id = revision.sha256
        if resolved_object_id is None:
            raise NoteError(
                NoteErrorCode.INVALID_OBSERVATION_CONVERSION,
                message="this object type requires an explicit object identity",
            )
        reference_limitation = _passage_limitation(
            revision.path, revision.revision_id, revision.sha256, passage
        )
        try:
            declaration = ObservationDeclarationV1(
                object_type=object_type,
                method=method,
                object_id=resolved_object_id,
                tool=tool,
                tool_version=tool_version,
                parameters=parameters or {},
                limitations=(*tuple(limitations), reference_limitation),
                observed_at=observed_at,
            )
        except ContributionValidationError as exc:
            raise NoteError(NoteErrorCode.INVALID_OBSERVATION_CONVERSION, message=str(exc)) from exc
        return NoteObservationConversionProposalV1(
            investigation_id=investigation_id,
            workspace_id=binding.workspace_id,
            note_revision_id=revision.revision_id,
            note_sha256=revision.sha256,
            note_path=revision.path,
            passage=passage,
            declaration=declaration,
        )

    # -------------------------------------------------------------- helpers

    async def _usage(
        self,
        uow: WorkspaceNoteUnitOfWork,
        investigation_id: UUID,
        *,
        note_path: str,
        history: Sequence[WorkspaceFileRevisionV1],
    ) -> tuple[
        tuple[NoteInstructionUsageV1, ...],
        tuple[NoteHandoffUsageV1, ...],
        tuple[NoteConsultationUsageV1, ...],
    ]:
        references = {
            reference
            for revision in history
            for reference in (revision.revision_id, revision.sha256)
        }
        instructions: list[NoteInstructionUsageV1] = []
        for session in await uow.agent_sessions.list_for_investigation(investigation_id):
            for instruction in await uow.agent_sessions.list_instructions(session.id):
                if any(reference in instruction.text for reference in references):
                    instructions.append(
                        NoteInstructionUsageV1(
                            instruction_id=instruction.id,
                            session_id=instruction.session_id,
                            state=instruction.state.value,
                            text_sha256=instruction.text_sha256,
                        )
                    )
        handoffs: list[NoteHandoffUsageV1] = []
        for record in await uow.investigation_handoffs.list_for_investigation(investigation_id):
            if any(
                item.file_relpath == note_path
                and (item.workspace_revision_id in references or item.file_sha256 in references)
                for item in record.handoff.files
            ):
                handoffs.append(
                    NoteHandoffUsageV1(
                        handoff_id=UUID(str(record.handoff.handoff_id)),
                        task_id=UUID(str(record.handoff.task_id)),
                        version=record.handoff.version,
                    )
                )
        consultations: list[NoteConsultationUsageV1] = []
        for invocation in await uow.tool_invocations.list_for_investigation(investigation_id):
            if invocation.tool_name != ADVISOR_TOOL_NAME:
                continue
            question = invocation.normalized_parameters.get("question")
            if not isinstance(question, str):
                question = canonical_json_bytes(dict(invocation.normalized_parameters)).decode(
                    "utf-8", "ignore"
                )
            if any(reference in question for reference in references):
                consultation_type = invocation.normalized_parameters.get("consultation_type")
                consultations.append(
                    NoteConsultationUsageV1(
                        invocation_id=invocation.id,
                        operation_key=invocation.operation_key,
                        consultation_type=(
                            consultation_type if isinstance(consultation_type, str) else "unknown"
                        ),
                        status=invocation.status.value,
                    )
                )
        return tuple(instructions), tuple(handoffs), tuple(consultations)

    async def _resolve_session(
        self,
        uow: WorkspaceNoteUnitOfWork,
        investigation_id: UUID,
        task: InvestigationTaskV1,
        session_id: UUID | None,
    ) -> AgentSessionV1:
        if session_id is not None:
            session = await uow.agent_sessions.get(session_id)
            if (
                session is None
                or session.investigation_id != investigation_id
                or session.task_id != task.id
            ):
                raise NoteError(NoteErrorCode.AGENT_SESSION_REQUIRED, status_code=409)
            return session
        candidates = tuple(
            item
            for item in await uow.agent_sessions.list_for_task(task.id)
            if item.state in ACTIVE_AGENT_SESSION_STATES
        )
        if len(candidates) != 1:
            raise NoteError(
                NoteErrorCode.AGENT_SESSION_REQUIRED,
                status_code=409,
                message="no single active agent session is bound to this task",
            )
        return candidates[0]

    async def _validate_target(
        self,
        uow: WorkspaceNoteUnitOfWork,
        investigation: AnalystInvestigation,
        annotation: WorkspaceNoteAnnotationV1,
    ) -> None:
        if annotation.kind is NoteAnnotationKind.LINK_TASK:
            assert annotation.target_task_id is not None
            task = await uow.investigation_tasks.get(annotation.target_task_id)
            if task is None or task.investigation_id != investigation.id:
                raise NoteError(NoteErrorCode.TASK_NOT_IN_INVESTIGATION, status_code=404)
            return
        if annotation.kind is NoteAnnotationKind.LINK_IOC:
            assert annotation.ioc_value is not None and annotation.ioc_type is not None
            indicators = await uow.indicators.list_for_subject(investigation.subject_id)
            if not _ioc_matches(indicators, annotation.ioc_type, annotation.ioc_value):
                raise NoteError(
                    NoteErrorCode.IOC_NOT_IN_INVESTIGATION,
                    message="the IOC is not documented for this investigation's subject",
                )
            return
        if annotation.kind is NoteAnnotationKind.LINK_SAMPLE:
            assert annotation.target_sample_id is not None
            await self._require_investigation_sample(
                uow, investigation, annotation.target_sample_id
            )

    async def _require_investigation_sample(
        self,
        uow: WorkspaceNoteUnitOfWork,
        investigation: AnalystInvestigation,
        sample_id: UUID,
    ) -> None:
        sample = await uow.samples.get(sample_id)
        if sample is None or sample.subject_id != investigation.subject_id:
            raise NoteError(NoteErrorCode.SAMPLE_NOT_IN_INVESTIGATION, status_code=404)
        blob = await uow.blobs.get(sample.blob_id)
        if blob is None:
            raise NoteError(NoteErrorCode.SAMPLE_NOT_IN_INVESTIGATION, status_code=404)
        associations = await uow.sample_associations.list_for_investigation(investigation.id)
        if not any(item.sample_sha256 == blob.descriptor.sha256 for item in associations):
            raise NoteError(
                NoteErrorCode.SAMPLE_NOT_IN_INVESTIGATION,
                message="the sample is not associated with this investigation",
            )

    async def _validate_citations(
        self,
        uow: WorkspaceNoteUnitOfWork,
        investigation_id: UUID,
        evidence_refs: Sequence[UUID],
        finding_refs: Sequence[UUID],
    ) -> None:
        observations = {
            item.id: item
            for item in await uow.evidence_observations.list_for_investigation(investigation_id)
            if item.investigation_id == investigation_id
        }
        for observation_id in evidence_refs:
            observation = observations.get(observation_id)
            if observation is None:
                raise NoteError(
                    NoteErrorCode.INVALID_CITATION,
                    message="a cited observation is absent from the investigation",
                )
            if is_advisory_observation(observation):
                raise NoteError(
                    NoteErrorCode.ADVISORY_NOT_EVIDENCE,
                    message="Bridge advisory material cannot be cited as evidence",
                )
        if not finding_refs:
            return
        findings = await uow.investigation_findings.list_for_investigation(investigation_id)
        superseded = {
            item.supersedes for item in findings if getattr(item, "supersedes", None) is not None
        }
        current = {
            item.id
            for item in findings
            if item.investigation_id == investigation_id and item.id not in superseded
        }
        if any(item not in current for item in finding_refs):
            raise NoteError(
                NoteErrorCode.INVALID_CITATION,
                message="a cited finding is absent from the investigation",
            )

    async def _require_investigation(
        self, uow: WorkspaceNoteUnitOfWork, investigation_id: UUID
    ) -> AnalystInvestigation:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise NoteError(NoteErrorCode.INVESTIGATION_NOT_FOUND, status_code=404)
        return investigation

    async def _require_binding(
        self, uow: WorkspaceNoteUnitOfWork, investigation_id: UUID
    ) -> WorkspaceBindingV1:
        binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
        if binding is None:
            raise NoteError(
                NoteErrorCode.WORKSPACE_NOT_BOUND,
                status_code=409,
                message="the investigation has no bound workspace",
            )
        return binding

    async def _require_revision(
        self,
        uow: WorkspaceNoteUnitOfWork,
        binding: WorkspaceBindingV1,
        revision_id: str,
    ) -> WorkspaceFileRevisionV1:
        if not isinstance(revision_id, str) or _REVISION_ID_RE.fullmatch(revision_id) is None:
            raise NoteError(NoteErrorCode.NOTE_NOT_FOUND, status_code=404)
        revision = await uow.workspace_file_revisions.get(binding.workspace_id, revision_id)
        if revision is None:
            raise NoteError(NoteErrorCode.NOTE_NOT_FOUND, status_code=404)
        if not is_note_path(revision.path):
            raise NoteError(
                NoteErrorCode.INVALID_NOTE_PATH,
                message="the revision is not a note under 'notes/'",
            )
        return revision

    async def _require_task(
        self,
        uow: WorkspaceNoteUnitOfWork,
        investigation_id: UUID,
        task_id: UUID,
    ) -> InvestigationTaskV1:
        task = await uow.investigation_tasks.get(task_id)
        if task is None or task.investigation_id != investigation_id:
            raise NoteError(NoteErrorCode.TASK_NOT_IN_INVESTIGATION, status_code=404)
        return task


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NoteError(NoteErrorCode.INVALID_REQUEST, message=f"{name} must be non-empty")
    return value.strip()


def _ioc_matches(indicators: Sequence[Indicator], ioc_type: NoteIocType, ioc_value: str) -> bool:
    expected_kind = _IOC_INDICATOR_KINDS[ioc_type]
    candidate = _normalized(ioc_value)
    return any(
        indicator.kind is expected_kind
        and (
            _normalized(indicator.normalized_value) == candidate
            or _normalized(indicator.original_value) == candidate
        )
        for indicator in indicators
    )


def _agent_instruction_text(path: str, revision_id: str, sha256: str, question: str) -> str:
    return "\n".join(
        (
            "Note humaine locale mise à jour.",
            f"Chemin: {path}",
            f"Révision exacte à utiliser: {revision_id}",
            f"SHA-256: {sha256}",
            "Cette note est une contribution non vérifiée, jamais une preuve.",
            "Question: " + question.strip(),
        )
    )


def _verification_task_key(
    investigation_id: UUID, revision_id: str, passage: NotePassageRangeV1
) -> str:
    identity = f"{investigation_id}\x00{revision_id}\x00{passage.start}:{passage.end}".encode()
    return "note-verify-" + hashlib.sha256(identity).hexdigest()[:24]


def _verification_question(
    path: str,
    revision_id: str,
    sha256: str,
    passage: NotePassageRangeV1,
    question: str,
) -> str:
    return "\n".join(
        (
            f"Vérifier l'intuition humaine de la note {path} (révision {revision_id}).",
            f"Passage cité par référence: caractères {passage.start}-{passage.end}, "
            f"SHA-256 {sha256}. Aucun texte de la note n'est copié comme fait.",
            "Question de vérification: " + question.strip(),
            "La réponse exige des observations canoniques de la même enquête; "
            "la note elle-même n'est pas une preuve.",
        )
    )[:MAX_QUESTION_CHARS]


def _passage_limitation(
    path: str,
    revision_id: str,
    sha256: str,
    passage: NotePassageRangeV1 | None,
) -> str:
    range_text = f"{passage.start}-{passage.end}" if passage is not None else "whole-note"
    raw = f"{_PASSAGE_LIMITATION_PREFIX}{path}:{revision_id}:{sha256}:{range_text}"
    return raw[:500]


__all__ = [
    "ADVISOR_TOOL_NAME",
    "MAX_QUESTION_CHARS",
    "NOTE_REVIEW_CONSULTATION_TYPES",
    "NOTE_VERIFICATION_POLICY_KEY",
    "InMemoryNoteInstructionPort",
    "NoteAgentContinuationV1",
    "NoteBridgeReviewOutcomeV1",
    "NoteBridgeReviewPort",
    "NoteBridgeReviewRequestV1",
    "NoteBridgeReviewResultV1",
    "NoteConsultationUsageV1",
    "NoteError",
    "NoteErrorCode",
    "NoteHandoffUsageV1",
    "NoteInstructionPort",
    "NoteInstructionReceiptV1",
    "NoteInstructionRequestV1",
    "NoteInstructionUsageV1",
    "NoteObservationConversionProposalV1",
    "NotePassageRangeV1",
    "NoteRevisionViewV1",
    "WorkspaceNoteService",
    "WorkspaceNoteUnitOfWork",
    "WorkspaceNoteUnitOfWorkFactory",
    "WorkspaceNoteViewV1",
]
