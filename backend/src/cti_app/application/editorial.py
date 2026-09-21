from __future__ import annotations

from uuid import UUID

from cti_app.application.discovery_identity import normalize
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.discovery import SourceRelationshipStatus, SourceRole
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)


class EditorialGroupNotFoundError(LookupError):
    pass


class LegacyEditorialProjectionService:
    """TODO AW-009: delete LegacyEditorialProjection.

    This is a reconstructible technical projection. Its only allowed
    consumers are legacy production and legacy collection.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def synchronize(self, edition_id: UUID) -> list[EditorialGroup]:
        """Rebuild legacy groups from canonical selection and discovery state."""
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditorialGroupNotFoundError(str(edition_id))

            existing = list(await uow.editorial_groups.list_for_edition(edition_id))
            snapshot = await uow.discovery_snapshots.get_active(edition_id)
            origins = list(await uow.subject_discovery_origins.list_for_edition(edition_id))
            if snapshot is None or not origins:
                await uow.commit()
                return existing

            candidates = await uow.discovery_candidates.list_for_edition(
                edition_id, include_replaced=False
            )
            candidates_by_id = {candidate.id: candidate for candidate in candidates}
            snapshot_subjects = {subject.subject_id: subject for subject in snapshot.subjects}
            groups_by_subject = {
                group.subject_id: group for group in existing if group.subject_id is not None
            }

            for origin in origins:
                projected = groups_by_subject.get(origin.subject_id)
                canonical_id = await uow.discovery_subject_identities.resolve_canonical_subject(
                    origin.discovery_subject_id
                )
                discovery_subject = snapshot_subjects.get(canonical_id)
                subject = await uow.subjects.get(origin.subject_id)
                if discovery_subject is None or subject is None:
                    continue

                references = tuple(
                    CandidateReference(
                        candidates_by_id[member.candidate_id].discovery_batch_id,
                        member.candidate_id,
                    )
                    for member in discovery_subject.member_references
                    if member.candidate_id in candidates_by_id
                )
                if not references:
                    continue

                if projected is None:
                    projected = EditorialGroup(
                        edition_id=edition_id,
                        # Subject.title is independent from the Discovery title.
                        title=subject.title,
                        candidate_references=references,
                        outcome=GroupingOutcome.NEW_SUBJECT,
                        score=_editorial_score(discovery_subject.candidate),
                        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
                        needs_source_verification=True,
                        needs_source_expansion=True,
                        grouping_confidence=GroupingConfidence.HIGH,
                        grouping_justification="Projection de l'identité sélectionnée canonique.",
                        subject_id=subject.id,
                        discovery_subject_id=canonical_id,
                    )
                    # This local transition only encodes the existing canonical
                    # Selection decision; it is not a new selection authority.
                    projected.select(subject.id)
                    await uow.editorial_groups.add(projected)
                    existing.append(projected)
                    groups_by_subject[subject.id] = projected
                    continue

                changed = False
                if projected.discovery_subject_id != canonical_id:
                    projected.discovery_subject_id = canonical_id
                    changed = True
                if projected.status is EditorialGroupStatus.PROPOSED:
                    projected.select(subject.id)
                    changed = True
                if projected.status is EditorialGroupStatus.SELECTED and (
                    projected.candidate_references != references
                ):
                    projected.synchronize_candidate_references(references)
                    changed = True
                if changed:
                    await uow.editorial_groups.save(projected)

            await uow.commit()
            return existing


def _editorial_score(candidate: object) -> EditorialScore:
    """Keep the legacy score fields populated while they remain in the schema."""
    impact = min(
        4,
        max(
            1,
            len(getattr(candidate, "countries", ()))
            + len(getattr(candidate, "sectors", ()))
            + len(getattr(candidate, "victims", ())),
        ),
    )
    novelty = (
        4
        if any(
            word in normalize(getattr(candidate, "novelty", "")) for word in ("nouveau", "inedit")
        )
        else 2
    )
    technical = getattr(candidate, "technical_potential", 0)
    hunting = min(
        4,
        len(getattr(candidate, "likely_artifacts", ()))
        + len(getattr(candidate, "iocs", ()))
        + bool(getattr(candidate, "cves", ())),
    )
    actionability = min(
        4,
        len(getattr(candidate, "relevance_reasons", ()))
        + bool(getattr(candidate, "likely_artifacts", ())),
    )
    role_weight = {
        SourceRole.PRIMARY: 2,
        SourceRole.INDEPENDENT: 2,
        SourceRole.RELAY: 1,
        SourceRole.AGGREGATOR: 0,
        SourceRole.SOCIAL: 0,
        SourceRole.UNKNOWN: 0,
    }
    source_quality = min(
        4,
        sum(role_weight[source.role] for source in getattr(candidate, "sources", ())),
    )
    return EditorialScore(
        impact=impact,
        novelty=novelty,
        technical_depth=technical,
        hunting_potential=hunting,
        actionability=actionability,
        source_quality=source_quality,
        justifications={
            "impact": "Victimes, secteurs et pays mentionnés dans les métadonnées disponibles.",
            "novelty": "Nouveauté issue du signal de découverte.",
            "technical_depth": "Potentiel technique fourni par la découverte.",
            "hunting_potential": "Artefacts et indicateurs disponibles.",
            "actionability": "Raisons de pertinence et artefacts disponibles.",
            "source_quality": "Rôles des sources déclarés par la découverte.",
        },
    )
