"""Agent-authored note capture into the bound workspace (M24-05 §5, FIX-T).

SESSION-RUNNER §5 makes ``note.capture`` the mid-run counterpart of the
supervisor's collect step: the agent asks the backend to publish one of its
text outputs through the host workspace file layer, under
``notes/agents/<task_id>/`` (origin ``agent`` + task header) or ``results/``
(``generated_by=session:<id>``).  Nothing here is evidence: a workspace
revision is a non-canonical observation of work that a separate
``contribution.import`` may later import through the one M24-07 writer.

The service reuses the existing :class:`WorkspaceAgentPort`; it never writes a
host path itself and never accepts an absolute path.  The bound workspace is
resolved from the canonical ``workspace_bindings`` row, so an agent cannot
target another investigation's tree.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from cti_app.application.workspace_agent import (
    WorkspaceAgentError,
    WorkspaceAgentPort,
    WorkspaceBindingV1,
)
from cti_app.domain.work_contribution import is_relative_workspace_path

NOTE_CAPTURE_TOOL = "note.capture"
NOTE_CAPTURE_MAX_BYTES = 1024 * 1024
NOTE_CAPTURE_MAX_PATH_BYTES = 1024
NOTE_CAPTURE_MAX_COMPONENT_BYTES = 255
NOTE_CAPTURE_TARGETS: frozenset[str] = frozenset({"note", "result"})
NOTE_CAPTURE_AGENTS_PREFIX = "notes/agents"
NOTE_CAPTURE_RESULTS_PREFIX = "results"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class NoteCaptureError(ValueError):
    """A typed refusal the broker maps to its HTTP error envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})
        super().__init__(message)


class WorkspaceBindingReader(Protocol):
    async def get_for_investigation(self, investigation_id: UUID) -> WorkspaceBindingV1 | None: ...


class NoteCaptureUnitOfWork(Protocol):
    workspace_bindings: WorkspaceBindingReader

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


class NoteCaptureUnitOfWorkFactory(Protocol):
    def __call__(self) -> NoteCaptureUnitOfWork: ...


@dataclass(frozen=True, slots=True)
class NoteCaptureResult:
    """The one workspace write an agent note produced; never evidence."""

    target: str
    path: str
    revision_id: str
    sha256: str
    size: int
    created: bool
    already_present: bool


def validate_note_relpath(value: str) -> str:
    """Return a workspace-relative note path under the task's own subtree.

    The host agent re-validates the full path (§5.4); this local copy refuses a
    traversal or an absolute path *before* any request leaves the backend.
    """
    if not isinstance(value, str) or not value:
        raise NoteCaptureError("note_path_invalid", "the note path must be non-empty")
    if unicodedata.normalize("NFC", value) != value:
        raise NoteCaptureError("note_path_invalid", "the note path must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise NoteCaptureError("note_path_invalid", "the note path contains a control character")
    encoded = value.encode("utf-8")
    if len(encoded) > NOTE_CAPTURE_MAX_PATH_BYTES:
        raise NoteCaptureError("note_path_invalid", "the note path is too long")
    if "\\" in value or not is_relative_workspace_path(value):
        raise NoteCaptureError("note_path_invalid", "the note path must stay inside the workspace")
    components = value.split("/")
    if components[0] in {".autowork", NOTE_CAPTURE_RESULTS_PREFIX}:
        raise NoteCaptureError(
            "note_path_invalid", "the note path is relative to its capture target"
        )
    if any(
        len(component.encode("utf-8")) > NOTE_CAPTURE_MAX_COMPONENT_BYTES
        for component in components
    ):
        raise NoteCaptureError("note_path_invalid", "a note path component is too long")
    return value


class AgenticNoteCaptureService:
    """Publish one agent note or result through the host workspace agent."""

    def __init__(
        self,
        *,
        uow_factory: NoteCaptureUnitOfWorkFactory,
        workspace_agent: WorkspaceAgentPort,
        max_note_bytes: int = NOTE_CAPTURE_MAX_BYTES,
    ) -> None:
        if max_note_bytes < 1:
            raise ValueError("max_note_bytes must be positive")
        self._uow_factory = uow_factory
        self._workspace_agent = workspace_agent
        self._max_note_bytes = max_note_bytes

    async def capture(
        self,
        investigation_id: UUID,
        *,
        task_id: UUID,
        session_id: UUID,
        target: str,
        relpath: str,
        text: str,
        expected_sha256: str,
        idempotency_key: str,
    ) -> NoteCaptureResult:
        if target not in NOTE_CAPTURE_TARGETS:
            raise NoteCaptureError("note_target_invalid", "the capture target is not allowlisted")
        if not isinstance(text, str) or not text:
            raise NoteCaptureError("note_content_invalid", "the note text must be non-empty")
        relpath = validate_note_relpath(relpath)
        if expected_sha256 != "new" and _SHA256.fullmatch(expected_sha256) is None:
            raise NoteCaptureError(
                "note_expected_sha_invalid", "expected_sha256 must be 'new' or a SHA-256"
            )
        content = text.encode("utf-8")
        if len(content) > self._max_note_bytes:
            raise NoteCaptureError(
                "note_too_large",
                "the note exceeds the capture byte limit",
                status_code=413,
            )
        async with self._uow_factory() as uow:
            binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
        if binding is None:
            raise NoteCaptureError(
                "workspace_not_bound",
                "the investigation has no bound workspace",
                status_code=409,
            )
        try:
            if target == "note":
                path = f"{NOTE_CAPTURE_AGENTS_PREFIX}/{task_id}/{relpath}"
                result = await self._workspace_agent.put_file(
                    binding.workspace_id,
                    path=path,
                    content=content,
                    expected_sha256=expected_sha256,
                    origin="agent",
                    task_id=str(task_id),
                    idempotency_key=idempotency_key,
                )
            else:
                path = f"{NOTE_CAPTURE_RESULTS_PREFIX}/{relpath}"
                result = await self._workspace_agent.put_generated(
                    binding.workspace_id,
                    path=path,
                    content=content,
                    expected_sha256=expected_sha256,
                    generated_by=f"session:{session_id}",
                    source_sha256=hashlib.sha256(content).hexdigest(),
                    task_id=str(task_id),
                    idempotency_key=idempotency_key,
                )
        except WorkspaceAgentError as exc:
            raise NoteCaptureError(
                exc.code, str(exc), status_code=exc.http_status, details=exc.details
            ) from exc
        revision = result.revision
        return NoteCaptureResult(
            target=target,
            path=revision.path,
            revision_id=revision.revision_id,
            sha256=revision.sha256,
            size=revision.size,
            created=result.created,
            already_present=result.already_present,
        )


__all__ = [
    "NOTE_CAPTURE_MAX_BYTES",
    "NOTE_CAPTURE_TARGETS",
    "NOTE_CAPTURE_TOOL",
    "AgenticNoteCaptureService",
    "NoteCaptureError",
    "NoteCaptureResult",
    "NoteCaptureUnitOfWork",
    "NoteCaptureUnitOfWorkFactory",
    "validate_note_relpath",
]
