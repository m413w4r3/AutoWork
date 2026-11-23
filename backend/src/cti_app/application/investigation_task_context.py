"""Bounded task context of a human Bridge consultation (M24.1 F4-D B2/B3).

The task consultation route and the optional ``context_task_id`` of
``POST /plans/request`` send the same dossier.  It is assembled from the
persisted task and its human contributions, then sanitised here -- before the
operator payload guard runs -- so a credential-like field is omitted with an
observable ``task_context_field_omitted_sensitive`` warning instead of failing
the whole consultation.  The announced ``task_context_sha256`` and the
invocation arguments are both the sanitised object.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from cti_app.application.investigation_advisor_tools import (
    TASK_CONTEXT_WARNINGS_ARGUMENT,
    sanitize_advisor_task_context,
)
from cti_app.application.investigation_consultations import (
    consultation_ledger,
)
from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_task import InvestigationTaskV1


class TaskContextError(ValueError):
    """A requested task context cannot be assembled for this investigation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TaskContextBundle:
    """The sanitised context, its observable warnings and its announced hash."""

    context: dict[str, Any] | None
    warnings: tuple[dict[str, str], ...] = ()
    sha256: str | None = None


def _task_contributions(contributions: Any, task_id: Any) -> list[dict[str, str]]:
    rows = [
        {
            "id": str(item.id),
            "category": item.category.value,
            "file_relpath": item.file_relpath,
            "file_sha256": item.file_sha256,
            "origin": item.origin.value,
            "import_status": item.import_status.value,
        }
        for item in contributions
        if item.task_id == task_id
    ]
    # The invocation payload is idempotent: keep the list order deterministic.
    return sorted(rows, key=lambda row: (row["id"],))


async def _editorial_anchor(
    factory: Any,
    investigation: Any,
    anchor: str,
) -> dict[str, str]:
    """Resolve one canonical publication anchor to its bounded excerpt.

    The article snapshot exposes the renderable anchors with a bounded excerpt;
    ``text_sha256`` identifies the exact excerpt transmitted to Bridge.
    """
    snapshot = await factory.article_snapshots.resolve(investigation.synthesis_artifact_id)
    entry = next(
        (item for item in snapshot.article.publication_anchors if item.anchor == anchor),
        None,
    )
    if entry is None:
        raise TaskContextError("task_consultation_anchor_invalid")
    excerpt = entry.excerpt
    return {
        "anchor": entry.anchor,
        "excerpt": excerpt,
        "text_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
    }


async def assemble_task_context(
    *,
    uow: Any,
    task: InvestigationTaskV1,
    factory: Any,
    investigation: Any,
    include_previous_consultation: bool = False,
    anchor: str | None = None,
    idempotency_prefix: str | None = None,
) -> dict[str, Any]:
    """Assemble the bounded non-evidence context of one human task request."""
    contributions = await uow.work_contributions.list_for_investigation(task.investigation_id)
    context: dict[str, Any] = {
        "investigation_id": str(task.investigation_id),
        "task_id": str(task.id),
        "task_key": task.task_key,
        "title": task.title,
        "question": task.question,
        "objective": task.objective,
        "status": task.status.value,
        "priority": task.priority.value,
        "claim_refs": list(task.claim_refs),
        "evidence_refs": list(task.evidence_refs),
        "sample_refs": list(task.sample_refs),
        "expected_outputs": list(task.expected_outputs),
        "result_refs": [dict(item) for item in task.result_refs],
        "human_contributions": _task_contributions(contributions, task.id),
    }
    if task.assigned_actor_kind is not None:
        context["assigned_actor"] = {
            "kind": task.assigned_actor_kind.value,
            "actor_id": task.assigned_actor_id,
        }
    if include_previous_consultation:
        entries = await consultation_ledger(uow, factory.artifact_store).list_for_investigation(
            task.investigation_id, context_task_id=task.id
        )
        prior = [
            entry
            for entry in entries
            if idempotency_prefix is None or not entry.operation_key.startswith(idempotency_prefix)
        ]
        if prior:
            last = prior[-1]
            context["previous_consultation"] = {
                "invocation_id": str(last.invocation_id),
                "type": last.consultation_type,
                "output_sha256": last.output_sha256,
                "parse_status": last.parse_status.value,
            }
    if anchor is not None:
        context["editorial_anchor"] = await _editorial_anchor(factory, investigation, anchor)
    return context


async def build_task_context(
    *,
    uow: Any,
    task: InvestigationTaskV1,
    factory: Any,
    investigation: Any,
    include_previous_consultation: bool = False,
    anchor: str | None = None,
    idempotency_prefix: str | None = None,
) -> TaskContextBundle:
    """Assemble and sanitise the task context the invocation will carry."""
    raw = await assemble_task_context(
        uow=uow,
        task=task,
        factory=factory,
        investigation=investigation,
        include_previous_consultation=include_previous_consultation,
        anchor=anchor,
        idempotency_prefix=idempotency_prefix,
    )
    warnings: list[dict[str, str]] = []
    context = sanitize_advisor_task_context(
        raw, investigation_id=task.investigation_id, warnings=warnings
    )
    return TaskContextBundle(
        context=context,
        warnings=tuple(warnings),
        sha256=(
            hashlib.sha256(canonical_json_bytes(context)).hexdigest()
            if context is not None
            else None
        ),
    )


def task_context_arguments(bundle: TaskContextBundle) -> dict[str, Any]:
    """The invocation arguments carrying the sanitised context, if any."""
    if bundle.context is None:
        return {}
    arguments: dict[str, Any] = {"task_context": bundle.context}
    if bundle.warnings:
        arguments[TASK_CONTEXT_WARNINGS_ARGUMENT] = [dict(item) for item in bundle.warnings]
    return arguments


__all__ = [
    "TaskContextBundle",
    "TaskContextError",
    "assemble_task_context",
    "build_task_context",
    "task_context_arguments",
]
