"""Context handed to the production model for a subject.

The reference-research prompt has slots for the actor, the technical summary and
the editorial period. They were being filled with empty strings, so the model was
asked to research a bare title with no anchor and no time window.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from cti_app.application.persistence import UnitOfWork
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.production import ProductionInputSnapshot

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
    core_sources_text: str
    supporting_sources_text: str
    external_llm_allowed: bool
    blocking_sources: tuple[str, ...]


def _describe(source: object) -> str:
    """One line per known publication, so the model does not re-find them."""
    title = getattr(source, "title", None) or ""
    publisher = getattr(source, "publisher", None) or ""
    published_at = getattr(source, "published_at", None)
    role = getattr(source, "proposed_role", getattr(source, "role", None))
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
    *,
    snapshot: ProductionInputSnapshot | None,
    relevant_source_urls: Collection[str] | None = None,
) -> SubjectProductionContext:
    """Assemble the prompt context from the run's frozen input snapshot.

    The snapshot is the only authority for the subject, its period and its
    core sources; live collections only add the reference-research sources
    explicitly retained for this stage.
    """
    if snapshot is None:
        raise ValueError("production_input_snapshot_missing")
    if snapshot.subject_id != subject_id:
        raise ValueError("production_input_snapshot_subject_mismatch")
    relevant_urls = frozenset(relevant_source_urls or ())

    collections = list(await uow.source_collections.list_for_subject(subject_id))
    core_source_urls = frozenset(source.canonical_url for source in snapshot.core_sources)
    allowed_urls = core_source_urls | relevant_urls
    core_sources_text = "\n".join(_describe(source) for source in snapshot.core_sources)
    supporting_sources_text = "\n".join(
        _describe(item)
        for item in collections
        if item.origin_kind is SourceOriginKind.REFERENCE_RESEARCH
        and item.canonical_url in relevant_urls
    )

    # The diffusion policy decides whether this subject may reach an external
    # model at all; it is never a hardcoded True.
    blocking = tuple(
        dict.fromkeys(
            (
                *(
                    item.canonical_url
                    for item in collections
                    if item.canonical_url in allowed_urls
                    and (item.do_not_submit or not item.external_llm_allowed)
                ),
                *(
                    source.canonical_url
                    for source in snapshot.core_sources
                    if not source.external_llm_allowed
                ),
            )
        )
    )

    archived = [
        item
        for item in collections
        if item.state in _ARCHIVED_STATES and item.canonical_url in allowed_urls
    ]
    technical_summary = (
        f"{len(archived)} publication(s) déjà archivée(s) pour ce sujet."
        if archived
        else "Aucune publication archivée pour l'instant."
    )

    return SubjectProductionContext(
        subject_title=snapshot.subject_title,
        subject_description=snapshot.discovery_summary,
        actor_info=snapshot.actor_or_campaign,
        technical_summary=technical_summary,
        period_start=snapshot.period_start.isoformat(),
        period_end=snapshot.period_end.isoformat(),
        research_date=snapshot.research_date,
        core_sources_text=core_sources_text,
        supporting_sources_text=supporting_sources_text,
        external_llm_allowed=not blocking,
        blocking_sources=blocking,
    )
