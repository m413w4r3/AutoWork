"""Import one workspace file revision into the canonical investigation (M24-07).

ADR-0011 D6 gives every category exactly one canonical writer:

* ``work_contribution`` — the exact blob plus this append-only row; it never
  becomes evidence and carries no canonical reference.
* ``observation`` — ``ProductionArtifactStore.put_bytes`` archives the exact
  bytes, the import verifies their SHA-256, then
  ``EvidenceRecorder.record_existing_blob`` writes the observation with
  ``source='workspace_import'`` and the closed ``(object_type, method)`` kind.
* ``finding`` — ``FindingService.propose`` validates same-investigation,
  non-advisory observation refs.  An imported finding is downgraded to
  ``inconclusive``: support is a human judgement, never an import side effect.
  A ``new_sample`` proposal only creates or reuses a ``candidate``
  ``SampleAssociation``; G3 stays exclusively human
  (``SampleQualificationService.qualify``) and sample bytes never pass here.

The backend never reads the host filesystem: it re-reads the revision through
``WorkspaceAgentPort.read_content`` and refuses bytes whose digest differs from
the recorded revision (recorded as a terminal ``rejected`` row, not imported).
Replays are idempotent by ``(investigation_id, idempotency_key)`` and by the
non-rejected ``(workspace_id, workspace_revision_id)`` row; a rejected row is
terminal and a retry is a new row with a new attempt key.

Authority split (D3/D6/D7): a verified human API call may claim ``human`` or
``host``; ``host`` never links a task lease and is never attributed to an
operator.  ``agent`` requires a task whose current ``lease_fence`` and live
lease the service validates here — but lot 07 exposes **no** unsigned route for
it: the session-bound call belongs to lot 05B2, which verifies the D7 HMAC and
passes this authority.  The route refuses ``agent`` with
``operator_session_required``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Protocol, Self, cast
from uuid import UUID

from cti_app.application.blob_storage import BlobStorageUnavailableError
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.investigation_acquisition import sample_association_id
from cti_app.application.investigation_findings import FindingService
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    InvestigationTaskRepository,
    SampleAssociationRepository,
    VersionConflictError,
    WorkContributionRepository,
    WorkspaceBindingRepository,
    WorkspaceFileRevisionRepository,
    same_work_contribution_request,
)
from cti_app.application.persistence import BlobRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.application.workspace_agent import (
    WorkspaceAgentError,
    WorkspaceAgentPort,
    WorkspaceContentV1,
    WorkspaceFileRevisionV1,
)
from cti_app.domain.investigation import (
    FindingAssessment,
    SampleAssociation,
    SampleOrigin,
    is_advisory_observation,
)
from cti_app.domain.investigation_task import InvestigationTaskV1, TaskStatus
from cti_app.domain.work_contribution import (
    WORKSPACE_IMPORT_SOURCE,
    WORKSPACE_IMPORT_SOURCE_VERSION,
    ContributionDeclarationV1,
    ContributionValidationError,
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionOrigin,
    WorkContributionV1,
    WorkContributionValidationCode,
    assert_contribution_blob_allowed,
    is_internal_workspace_path,
    observation_metadata_line,
)

WORK_CONTRIBUTION_BUCKET = "work-contributions"
IMPORTED_SUPPORTED_LIMITATION = (
    "imported finding cannot be auto-supported; human review of the cited observations is required"
)
_ACTIVE_LEASE_STATUSES = frozenset({TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class WorkContributionError(ValueError):
    """Base refusal for a contribution operation, carrying an HTTP-stable code."""

    code = "work_contribution_invalid"
    status_code = 422

    def __init__(self, message: str, *, details: Mapping[str, str] | None = None) -> None:
        self.details = dict(details or {})
        super().__init__(message)


class WorkContributionNotFoundError(WorkContributionError):
    code = "work_contribution_not_found"
    status_code = 404


class WorkContributionConflictError(WorkContributionError):
    code = "idempotency_conflict"
    status_code = 409


class WorkContributionRequestError(WorkContributionError):
    code = "work_contribution_invalid_request"
    status_code = 422


class OperatorSessionRequiredError(WorkContributionError):
    """The unsigned route never accepts an ``agent`` write (lot 05B2 owns it)."""

    code = "operator_session_required"
    status_code = 403


class WorkContributionStorageError(WorkContributionError):
    code = "work_contribution_storage_unavailable"
    status_code = 503


class ContributionArtifactStore(Protocol):
    """The one ``ProductionArtifactStore`` method a contribution needs."""

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID: ...


class WorkContributionUnitOfWork(Protocol):
    blobs: BlobRepository
    workspace_bindings: WorkspaceBindingRepository
    workspace_file_revisions: WorkspaceFileRevisionRepository
    work_contributions: WorkContributionRepository
    evidence_observations: EvidenceObservationRepository
    investigation_findings: FindingRepository
    sample_associations: SampleAssociationRepository
    investigation_tasks: InvestigationTaskRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class WorkContributionUnitOfWorkFactory(Protocol):
    def __call__(self) -> WorkContributionUnitOfWork: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ContributionAuthority:
    """Who claims the import and under which lease authority (D3/D6).

    ``origin='agent'`` is only constructed by the session-bound broker (05B2)
    after the D7 signature; the human route refuses it before this object
    exists.
    """

    origin: WorkContributionOrigin
    actor_id: str | None = None
    task_id: UUID | None = None
    lease_fence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.origin, WorkContributionOrigin):
            raise TypeError("origin must use WorkContributionOrigin")
        if self.actor_id is not None and not self.actor_id.strip():
            raise ValueError("actor_id must be a non-empty explicit identity")
        if self.origin is WorkContributionOrigin.AGENT:
            if self.task_id is None or self.lease_fence is None:
                raise ValueError("an agent contribution requires task_id and lease_fence")
        elif self.lease_fence is not None:
            raise ValueError("only an agent contribution claims lease authority")


@dataclass(frozen=True, slots=True)
class _ImportTarget:
    """One read-only resolution of the revision before any side effect."""

    revision: WorkspaceFileRevisionV1 | None = None
    task: InvestigationTaskV1 | None = None
    terminal_code: WorkContributionValidationCode | None = None
    replay: WorkContributionV1 | None = None


def _mime_type(content: bytes) -> str:
    """A content-only MIME type: identical bytes always share one descriptor.

    The blob catalog refuses a content address whose MIME metadata differs, so
    deriving the type from the file suffix would make the same bytes imported
    under two paths (``note.md`` and ``note.txt``) collide.  The exact path and
    suffix remain in the contribution row.
    """
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain; charset=utf-8"


def _finding_validation_code(exc: ValueError) -> WorkContributionValidationCode:
    message = str(exc).casefold()
    if "advisory" in message:
        return WorkContributionValidationCode.ADVISORY_OBSERVATION_NOT_EVIDENCE
    if "does not exist" in message:
        return WorkContributionValidationCode.OBSERVATION_REF_NOT_FOUND
    if "another investigation" in message:
        return WorkContributionValidationCode.OBSERVATION_REF_INVESTIGATION_MISMATCH
    if "claim" in message:
        return WorkContributionValidationCode.FINDING_CLAIM_REF_NOT_FOUND
    return WorkContributionValidationCode.FINDING_INVALID


class WorkContributionService:
    """Import workspace revisions and read the append-only contribution rows."""

    def __init__(
        self,
        *,
        uow_factory: WorkContributionUnitOfWorkFactory,
        workspace_agent: WorkspaceAgentPort,
        artifact_store: ContributionArtifactStore,
        article_snapshots: ArticleSnapshotPort,
        max_contribution_bytes: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_contribution_bytes < 1:
            raise ValueError("max_contribution_bytes must be positive")
        self._uow_factory = uow_factory
        self._workspace_agent = workspace_agent
        self._artifact_store = artifact_store
        self._article_snapshots = article_snapshots
        self._max_contribution_bytes = max_contribution_bytes
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))

    async def import_revision(
        self,
        investigation_id: UUID,
        *,
        workspace_id: str,
        workspace_revision_id: str,
        declaration: ContributionDeclarationV1,
        authority: ContributionAuthority,
        idempotency_key: str,
    ) -> WorkContributionV1:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
            raise WorkContributionRequestError("Idempotency-Key must match [A-Za-z0-9._:-]{8,128}")
        target = await self._resolve_target(
            investigation_id,
            workspace_id=workspace_id,
            workspace_revision_id=workspace_revision_id,
            declaration=declaration,
            authority=authority,
            idempotency_key=idempotency_key,
        )
        if target.replay is not None:
            return target.replay
        if target.revision is None:
            raise RuntimeError("contribution target resolved without a workspace revision")
        revision = target.revision
        if target.terminal_code is not None:
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.REJECTED,
                code=target.terminal_code,
            )
        if is_internal_workspace_path(revision.path):
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.SKIPPED,
                code=WorkContributionValidationCode.INTERNAL_WORKSPACE_PATH,
            )
        if revision.size > self._max_contribution_bytes:
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.REJECTED,
                code=WorkContributionValidationCode.CONTRIBUTION_TOO_LARGE,
            )

        content = await self._read_content(revision)
        mismatch = self._content_mismatch(revision, content)
        if mismatch is not None:
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.REJECTED,
                code=mismatch,
            )
        if len(content.content) > self._max_contribution_bytes:
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.REJECTED,
                code=WorkContributionValidationCode.CONTRIBUTION_TOO_LARGE,
            )
        if not content.content:
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.REJECTED,
                code=WorkContributionValidationCode.EMPTY_CONTENT,
            )
        try:
            assert_contribution_blob_allowed(content.content)
            metadata_line = self._metadata_line(declaration, revision)
        except ContributionValidationError as exc:
            return await self._persist_terminal(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                task=target.task,
                status=WorkContributionImportStatus.REJECTED,
                code=exc.code,
            )

        try:
            blob_id = await self._artifact_store.put_bytes(
                content.content,
                bucket=WORK_CONTRIBUTION_BUCKET,
                mime_type=_mime_type(content.content),
            )
        except BlobStorageUnavailableError as exc:
            raise WorkContributionStorageError("contribution blob storage is unavailable") from exc
        return await self._persist_imported(
            investigation_id,
            revision=revision,
            declaration=declaration,
            authority=authority,
            task=target.task,
            idempotency_key=idempotency_key,
            blob_id=blob_id,
            content_sha256=revision.sha256,
            metadata_line=metadata_line,
        )

    async def get(self, investigation_id: UUID, contribution_id: UUID) -> WorkContributionV1:
        async with self._uow_factory() as uow:
            contribution = await uow.work_contributions.get(contribution_id)
        if contribution is None or contribution.investigation_id != investigation_id:
            raise WorkContributionNotFoundError("contribution does not exist in this investigation")
        return contribution

    async def list(
        self,
        investigation_id: UUID,
        *,
        category: WorkContributionCategory | None = None,
        import_status: WorkContributionImportStatus | None = None,
        workspace_id: str | None = None,
    ) -> tuple[WorkContributionV1, ...]:
        async with self._uow_factory() as uow:
            return await uow.work_contributions.list_for_investigation(
                investigation_id,
                category=category,
                import_status=import_status,
                workspace_id=workspace_id,
            )

    async def _resolve_target(
        self,
        investigation_id: UUID,
        *,
        workspace_id: str,
        workspace_revision_id: str,
        declaration: ContributionDeclarationV1,
        authority: ContributionAuthority,
        idempotency_key: str,
    ) -> _ImportTarget:
        async with self._uow_factory() as uow:
            replay = await uow.work_contributions.get_by_idempotency(
                investigation_id, idempotency_key
            )
            if replay is not None:
                return _ImportTarget(
                    replay=self._replay(
                        replay, workspace_id, workspace_revision_id, declaration, authority
                    )
                )
            binding = await uow.workspace_bindings.get(workspace_id)
            if binding is None or binding.investigation_id != investigation_id:
                raise WorkContributionNotFoundError(
                    "workspace is not bound to this investigation",
                    details={"workspace_id": workspace_id},
                )
            revision = await uow.workspace_file_revisions.get(workspace_id, workspace_revision_id)
            if revision is None:
                raise WorkContributionNotFoundError(
                    "workspace revision does not exist",
                    details={"workspace_revision_id": workspace_revision_id},
                )
            existing_revision = await uow.work_contributions.get_non_rejected_for_revision(
                workspace_id, workspace_revision_id
            )
            if existing_revision is not None:
                return _ImportTarget(replay=existing_revision)
            task_error, task = await self._resolve_authority(uow, investigation_id, authority)
            if task_error is not None:
                return _ImportTarget(revision=revision, terminal_code=task_error)
            if (
                authority.origin is WorkContributionOrigin.AGENT
                and task is not None
                and revision.task_id is not None
                and revision.task_id != task.id
            ):
                return _ImportTarget(
                    revision=revision,
                    terminal_code=WorkContributionValidationCode.TASK_NOT_IN_INVESTIGATION,
                )
            reference_error = await self._validate_references(uow, investigation_id, declaration)
            if reference_error is not None:
                return _ImportTarget(revision=revision, terminal_code=reference_error)
            return _ImportTarget(revision=revision, task=task)

    def _replay(
        self,
        stored: WorkContributionV1,
        workspace_id: str,
        workspace_revision_id: str,
        declaration: ContributionDeclarationV1,
        authority: ContributionAuthority,
    ) -> WorkContributionV1:
        if (
            stored.workspace_id != workspace_id
            or stored.workspace_revision_id != workspace_revision_id
            or stored.category is not declaration.category
            or stored.origin is not authority.origin
        ):
            raise WorkContributionConflictError(
                "Idempotency-Key was reused for a different contribution request",
                details={"contribution_id": str(stored.id)},
            )
        return stored

    async def _resolve_authority(
        self,
        uow: WorkContributionUnitOfWork,
        investigation_id: UUID,
        authority: ContributionAuthority,
    ) -> tuple[WorkContributionValidationCode | None, InvestigationTaskV1 | None]:
        if authority.origin is WorkContributionOrigin.HOST:
            return None, None
        if authority.origin is WorkContributionOrigin.HUMAN:
            if authority.task_id is None:
                return None, None
            task = await uow.investigation_tasks.get(authority.task_id)
            if task is None or task.investigation_id != investigation_id:
                return WorkContributionValidationCode.TASK_NOT_IN_INVESTIGATION, None
            return None, task
        if authority.task_id is None or authority.lease_fence is None:
            raise RuntimeError("agent authority lost its validated task link")
        task = await uow.investigation_tasks.get(authority.task_id)
        if task is None or task.investigation_id != investigation_id:
            raise WorkContributionRequestError("agent task does not belong to this investigation")
        if task.lease_fence != authority.lease_fence:
            raise WorkContributionRequestError(
                "agent lease fence is stale",
                details={"code": WorkContributionValidationCode.LEASE_FENCE_STALE.value},
            )
        if task.status not in _ACTIVE_LEASE_STATUSES or task.lease_expires_at is None:
            raise WorkContributionRequestError(
                "agent task holds no active lease",
                details={"code": WorkContributionValidationCode.LEASE_EXPIRED.value},
            )
        if task.lease_expires_at <= self._clock():
            raise WorkContributionRequestError(
                "agent lease has expired",
                details={"code": WorkContributionValidationCode.LEASE_EXPIRED.value},
            )
        return None, task

    async def _validate_references(
        self,
        uow: WorkContributionUnitOfWork,
        investigation_id: UUID,
        declaration: ContributionDeclarationV1,
    ) -> WorkContributionValidationCode | None:
        finding = declaration.finding
        if finding is None:
            return None
        for observation_ref in finding.observation_refs:
            observation = await uow.evidence_observations.get(observation_ref)
            if observation is None:
                return WorkContributionValidationCode.OBSERVATION_REF_NOT_FOUND
            if observation.investigation_id != investigation_id:
                return WorkContributionValidationCode.OBSERVATION_REF_INVESTIGATION_MISMATCH
            if is_advisory_observation(observation):
                return WorkContributionValidationCode.ADVISORY_OBSERVATION_NOT_EVIDENCE
        if finding.claim_refs:
            try:
                claims = await self._article_snapshots.get_claims(
                    investigation_id, finding.claim_refs
                )
            except ValueError:
                return WorkContributionValidationCode.FINDING_CLAIM_REF_NOT_FOUND
            known = {claim.id for claim in claims}
            if any(ref not in known for ref in finding.claim_refs):
                return WorkContributionValidationCode.FINDING_CLAIM_REF_NOT_FOUND
        return None

    async def _read_content(self, revision: WorkspaceFileRevisionV1) -> WorkspaceContentV1:
        try:
            return await self._workspace_agent.read_content(
                revision.workspace_id, path=revision.path
            )
        except WorkspaceAgentError as exc:
            error = WorkContributionStorageError(
                f"workspace content is unavailable: {exc}",
                details={"code": exc.code},
            )
            error.status_code = exc.http_status
            raise error from exc

    @staticmethod
    def _content_mismatch(
        revision: WorkspaceFileRevisionV1, content: WorkspaceContentV1
    ) -> WorkContributionValidationCode | None:
        digest = hashlib.sha256(content.content).hexdigest()
        if (
            content.sha256 != revision.sha256
            or digest != revision.sha256
            or len(content.content) != revision.size
            or content.size != revision.size
        ):
            return WorkContributionValidationCode.WORKSPACE_REVISION_CONTENT_MISMATCH
        return None

    @staticmethod
    def _metadata_line(
        declaration: ContributionDeclarationV1,
        revision: WorkspaceFileRevisionV1,
    ) -> str | None:
        observation = declaration.observation
        if observation is None:
            return None
        return observation_metadata_line(
            declaration=observation,
            file_sha256=revision.sha256,
            workspace_revision_id=revision.revision_id,
            file_origin=revision.origin,
            observed_at=observation.observed_at or revision.observed_at,
        )

    async def _persist_terminal(
        self,
        investigation_id: UUID,
        *,
        revision: WorkspaceFileRevisionV1,
        declaration: ContributionDeclarationV1,
        authority: ContributionAuthority,
        idempotency_key: str,
        status: WorkContributionImportStatus,
        code: WorkContributionValidationCode,
        content_sha256: str | None = None,
        task: InvestigationTaskV1 | None = None,
    ) -> WorkContributionV1:
        row = self._row(
            investigation_id,
            revision=revision,
            declaration=declaration,
            authority=authority,
            idempotency_key=idempotency_key,
            status=status,
            code=code,
            content_blob_id=None,
            content_sha256=content_sha256,
            observation_id=None,
            finding_id=None,
            sample_association_id=None,
            task=task,
        )
        return await self._append(row, revision)

    async def _persist_imported(
        self,
        investigation_id: UUID,
        *,
        revision: WorkspaceFileRevisionV1,
        declaration: ContributionDeclarationV1,
        authority: ContributionAuthority,
        task: InvestigationTaskV1 | None,
        idempotency_key: str,
        blob_id: UUID,
        content_sha256: str,
        metadata_line: str | None,
    ) -> WorkContributionV1:
        async with self._uow_factory() as uow:
            blob = await uow.blobs.get(blob_id)
            if (
                blob is None
                or blob.descriptor.sha256 != revision.sha256
                or blob.descriptor.size != revision.size
            ):
                raise WorkContributionStorageError(
                    "archived contribution blob failed its SHA-256 verification"
                )
            observation_id = None
            observation = declaration.observation
            if observation is not None:
                if metadata_line is None:
                    raise RuntimeError("observation import resolved without metadata")
                recorder = ProductionEvidenceRecorder(
                    cast(ProductionArtifactStore, self._artifact_store),
                    uow.evidence_observations,
                )
                stored_observation = await recorder.record_existing_blob(
                    investigation_id=investigation_id,
                    kind=observation.evidence_kind,
                    blob_id=blob_id,
                    sha256=revision.sha256,
                    source=WORKSPACE_IMPORT_SOURCE,
                    source_version=WORKSPACE_IMPORT_SOURCE_VERSION,
                    object_type=observation.object_type.value,
                    object_id=observation.object_id,
                    limitations=(*observation.limitations, metadata_line),
                    observed_at=observation.observed_at or revision.observed_at,
                )
                observation_id = stored_observation.id
            finding_id = None
            sample_id = None
            finding_declaration = declaration.finding
            if finding_declaration is not None:
                assessment = finding_declaration.assessment
                limitations = list(finding_declaration.limitations)
                if assessment is FindingAssessment.SUPPORTED:
                    assessment = FindingAssessment.INCONCLUSIVE
                    limitations.append(IMPORTED_SUPPORTED_LIMITATION)
                try:
                    finding = await FindingService(
                        findings=uow.investigation_findings,
                        observations=uow.evidence_observations,
                        article_snapshots=self._article_snapshots,
                    ).propose(
                        investigation_id=investigation_id,
                        kind=finding_declaration.kind,
                        assessment=assessment,
                        statement=finding_declaration.statement,
                        observation_refs=finding_declaration.observation_refs,
                        claim_refs=finding_declaration.claim_refs,
                        novelty=finding_declaration.novelty,
                        scope=finding_declaration.scope,
                        limitations=tuple(limitations),
                    )
                except ValueError as exc:
                    await uow.rollback()
                    return await self._persist_terminal(
                        investigation_id,
                        revision=revision,
                        declaration=declaration,
                        authority=authority,
                        idempotency_key=idempotency_key,
                        status=WorkContributionImportStatus.REJECTED,
                        code=_finding_validation_code(exc),
                        content_sha256=content_sha256,
                        task=task,
                    )
                finding_id = finding.id
                if finding_declaration.proposed_sample_sha256 is not None:
                    sample_id = await self._ensure_candidate_association(
                        uow, investigation_id, finding_declaration.proposed_sample_sha256
                    )
            row = self._row(
                investigation_id,
                revision=revision,
                declaration=declaration,
                authority=authority,
                idempotency_key=idempotency_key,
                status=WorkContributionImportStatus.IMPORTED,
                code=None,
                content_blob_id=blob_id,
                content_sha256=content_sha256,
                observation_id=observation_id,
                finding_id=finding_id,
                sample_association_id=sample_id,
                task=task,
            )
            stored = await self._append_in_uow(uow, row, revision)
            await uow.commit()
            return stored

    @staticmethod
    async def _ensure_candidate_association(
        uow: WorkContributionUnitOfWork, investigation_id: UUID, sample_sha256: str
    ) -> UUID:
        association_id = sample_association_id(investigation_id, sample_sha256)
        existing = await uow.sample_associations.get(association_id)
        if existing is not None:
            if existing.investigation_id != investigation_id:
                raise WorkContributionConflictError(
                    "the proposed sample association belongs to another investigation",
                    details={"sample_association_id": str(association_id)},
                )
            return existing.id
        association = SampleAssociation(
            id=association_id,
            investigation_id=investigation_id,
            sample_sha256=sample_sha256,
            origin=SampleOrigin.MANUAL,
        )
        try:
            stored = await uow.sample_associations.append_version(association, expected_version=0)
        except VersionConflictError:
            concurrent = await uow.sample_associations.get(association_id)
            if concurrent is None:
                raise
            return concurrent.id
        return stored.id

    async def _append(
        self, row: WorkContributionV1, revision: WorkspaceFileRevisionV1
    ) -> WorkContributionV1:
        async with self._uow_factory() as uow:
            active = await uow.work_contributions.get_non_rejected_for_revision(
                revision.workspace_id, revision.revision_id
            )
            if active is not None:
                return active
            stored = await self._append_in_uow(uow, row, revision)
            await uow.commit()
            return stored

    @staticmethod
    async def _append_in_uow(
        uow: WorkContributionUnitOfWork,
        row: WorkContributionV1,
        revision: WorkspaceFileRevisionV1,
    ) -> WorkContributionV1:
        try:
            stored, _created = await uow.work_contributions.append_if_absent(row)
        except VersionConflictError as exc:
            raise WorkContributionConflictError(
                "the workspace revision already has a different contribution",
                details={
                    "workspace_id": revision.workspace_id,
                    "workspace_revision_id": revision.revision_id,
                },
            ) from exc
        if stored.id != row.id and not same_work_contribution_request(stored, row):
            raise WorkContributionConflictError(
                "the workspace revision already has a different contribution",
                details={
                    "workspace_id": revision.workspace_id,
                    "workspace_revision_id": revision.revision_id,
                },
            )
        return stored

    def _row(
        self,
        investigation_id: UUID,
        *,
        revision: WorkspaceFileRevisionV1,
        declaration: ContributionDeclarationV1,
        authority: ContributionAuthority,
        idempotency_key: str,
        status: WorkContributionImportStatus,
        code: WorkContributionValidationCode | None,
        content_blob_id: UUID | None,
        content_sha256: str | None,
        observation_id: UUID | None,
        finding_id: UUID | None,
        sample_association_id: UUID | None,
        task: InvestigationTaskV1 | None = None,
    ) -> WorkContributionV1:
        origin = authority.origin
        task_id: UUID | None = None
        lease_fence: int | None = None
        origin_actor_id: str | None = None
        if origin is WorkContributionOrigin.AGENT:
            task_id = task.id if task is not None else authority.task_id
            lease_fence = authority.lease_fence
            origin_actor_id = authority.actor_id or (
                task.assigned_actor_id if task is not None else None
            )
        elif origin is WorkContributionOrigin.HUMAN:
            task_id = task.id if task is not None else None
            origin_actor_id = authority.actor_id
        else:
            origin_actor_id = authority.actor_id
        return WorkContributionV1(
            investigation_id=investigation_id,
            workspace_id=revision.workspace_id,
            workspace_revision_id=revision.revision_id,
            file_relpath=revision.path,
            file_sha256=revision.sha256,
            observed_at=revision.observed_at,
            origin=origin,
            idempotency_key=idempotency_key,
            category=declaration.category,
            origin_actor_id=origin_actor_id,
            task_id=task_id,
            lease_fence=lease_fence,
            content_blob_id=content_blob_id,
            content_sha256=content_sha256,
            import_status=status,
            observation_id=observation_id,
            finding_id=finding_id,
            sample_association_id=sample_association_id,
            validation_error_code=code,
            created_at=self._clock(),
        )


__all__ = [
    "IMPORTED_SUPPORTED_LIMITATION",
    "WORK_CONTRIBUTION_BUCKET",
    "ContributionArtifactStore",
    "ContributionAuthority",
    "OperatorSessionRequiredError",
    "WorkContributionConflictError",
    "WorkContributionError",
    "WorkContributionNotFoundError",
    "WorkContributionRequestError",
    "WorkContributionService",
    "WorkContributionStorageError",
    "WorkContributionUnitOfWork",
    "WorkContributionUnitOfWorkFactory",
]
