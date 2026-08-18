"""Context handed to the production model for a subject.

The reference-research prompt has slots for the actor, the technical summary and
the editorial period. They were being filled with empty strings, so the model was
asked to research a bare title with no anchor and no time window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from cti_app.application.persistence import UnitOfWork
from cti_app.domain.collection import CollectionState

_ARCHIVED_STATES = {
    CollectionState.ARCHIVED,
    CollectionState.EXTRACTED,
    CollectionState.COMPLETED,
}


@dataclass(frozen=True, slots=True)
class SubjectProductionContext:
    """Everything the prompts need about a subject."""

    subject_title: str
    subject_description: str
    actor_info: str
    technical_summary: str
    period_start: str
    period_end: str
    research_date: date
    existing_sources_text: str
    external_llm_allowed: bool
    blocking_sources: tuple[str, ...]


def _describe(source: object) -> str:
    """One line per known publication, so the model does not re-find them."""
    title = getattr(source, "title", None) or ""
    publisher = getattr(source, "publisher", None) or ""
    published_at = getattr(source, "published_at", None)
    role = getattr(source, "proposed_role", None)
    parts = [part for part in (title, publisher) if part]
    if published_at is not None:
        parts.append(str(published_at))
    if role is not None:
        parts.append(getattr(role, "value", str(role)))
    suffix = f" ({' · '.join(parts)})" if parts else ""
    return f"- {getattr(source, 'canonical_url', '')}{suffix}"


async def build_subject_production_context(
    uow: UnitOfWork,
    subject_id: UUID,
    research_date: date,
) -> SubjectProductionContext:
    """Assemble the prompt context from what the editorial phase established."""
    group = await uow.editorial_groups.get_by_subject(subject_id)
    title = group.title if group else str(subject_id)
    description = group.grouping_justification if group else ""

    period_start = ""
    period_end = ""
    if group is not None:
        edition = await uow.editions.get(group.edition_id)
        if edition is not None:
            period_start = edition.period_start.isoformat()
            period_end = edition.period_end.isoformat()

    collections = list(await uow.source_collections.list_for_subject(subject_id))
    existing_sources_text = "\n".join(_describe(item) for item in collections)

    # The diffusion policy decides whether this subject may reach an external
    # model at all; it is never a hardcoded True.
    blocking = tuple(
        item.canonical_url
        for item in collections
        if item.do_not_submit or not item.external_llm_allowed
    )

    archived = [item for item in collections if item.state in _ARCHIVED_STATES]
    technical_summary = (
        f"{len(archived)} publication(s) déjà archivée(s) pour ce sujet."
        if archived
        else "Aucune publication archivée pour l'instant."
    )

    actor_info = ""
    if group is not None:
        actor_info = getattr(group, "actor_or_campaign", "") or ""

    return SubjectProductionContext(
        subject_title=title,
        subject_description=description,
        actor_info=actor_info,
        technical_summary=technical_summary,
        period_start=period_start,
        period_end=period_end,
        research_date=research_date,
        existing_sources_text=existing_sources_text,
        external_llm_allowed=not blocking,
        blocking_sources=blocking,
    )
