from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from cti_app.application.discovery_identity import normalize
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.domain.entities import Sample, SourceDocument, Subject


class EditorialGroupNotFoundError(LookupError):
    pass


class EditorialActionError(ValueError):
    pass


class EditorialDecisionValue(StrEnum):
    BRIEF = "brief"
    MAJOR = "major"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class EditorialDecisionCommand:
    group_id: UUID
    version: int
    decision: EditorialDecisionValue


class WorkspaceMaterializer(Protocol):
    async def materialize(
        self,
        subject: Subject,
        source_documents: Sequence[SourceDocument],
        samples: Sequence[Sample],
        blobs: Mapping[UUID, BlobRecord],
        workspace_root: Path,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class EditorialBoard:
    groups: list[EditorialGroup]
    candidates: dict[CandidateReference, CandidateTopic]
    historical_groups: dict[UUID, EditorialGroup]
    selected_briefs: int
    selected_major: int
    ignored: int
    undecided: int
    target_briefs: int
    target_major: int


class EditorialGroupingService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        materializer: WorkspaceMaterializer | None = None,
        workspace_root: Path = Path("work/subjects"),
    ) -> None:
        self._uow_factory = uow_factory
        self._materializer = materializer
        self._workspace_root = workspace_root

    async def synchronize(self, edition_id: UUID) -> list[EditorialGroup]:
        async with self._uow_factory() as uow:
            existing = list(await uow.editorial_groups.list_for_edition(edition_id))
            snapshot = await uow.discovery_snapshots.get_active(edition_id)
            if snapshot is None:
                return existing
            by_subject = {
                group.discovery_subject_id: group
                for group in existing
                if group.discovery_subject_id is not None
            }
            for subject in snapshot.subjects:
                references = tuple(
                    CandidateReference(item.batch_id, item.candidate_id)
                    for item in subject.member_references
                )
                group = by_subject.get(subject.subject_id)
                if group is not None:
                    additions = tuple(
                        reference
                        for reference in references
                        if reference not in group.candidate_references
                    )
                    if additions and group.status in {
                        EditorialGroupStatus.PROPOSED,
                        EditorialGroupStatus.SELECTED,
                    }:
                        group.add_candidates(additions)
                        group.needs_source_expansion = True
                        group.needs_source_verification = True
                        await uow.editorial_groups.save(group)
                    continue
                candidate = subject.candidate
                group = EditorialGroup(
                    edition_id=edition_id,
                    title=candidate.title,
                    candidate_references=references,
                    outcome=GroupingOutcome.NEW_SUBJECT,
                    score=_editorial_score(candidate),
                    source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
                    needs_source_verification=True,
                    needs_source_expansion=True,
                    grouping_confidence=GroupingConfidence.HIGH,
                    grouping_justification="Identité issue du snapshot cumulatif actif.",
                    discovery_subject_id=subject.subject_id,
                )
                await uow.editorial_groups.add(group)
                existing.append(group)
            await uow.commit()
            return existing

    async def board(self, edition_id: UUID) -> EditorialBoard:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditorialGroupNotFoundError(str(edition_id))
            groups = list(await uow.editorial_groups.list_for_edition(edition_id))
            historical = list(await uow.editorial_groups.list_historical(edition_id))
            batches = [
                batch
                for batch in await uow.discovery_batches.list_for_edition(edition_id)
                if batch.is_active_revision
            ]
            selected = [group for group in groups if group.status is EditorialGroupStatus.SELECTED]
            ignored = [group for group in groups if group.status is EditorialGroupStatus.REJECTED]
            undecided = [group for group in groups if group.status is EditorialGroupStatus.PROPOSED]
            return EditorialBoard(
                groups=groups,
                candidates=_candidate_map(batches),
                historical_groups={group.id: group for group in [*historical, *selected]},
                selected_briefs=sum(
                    group.editorial_type is EditorialType.BRIEF for group in selected
                ),
                selected_major=sum(
                    group.editorial_type is EditorialType.MAJOR for group in selected
                ),
                ignored=len(ignored),
                undecided=len(undecided),
                target_briefs=edition.target_briefs,
                target_major=edition.target_major_articles,
            )

    async def decide_many(
        self,
        edition_id: UUID,
        commands: Sequence[EditorialDecisionCommand],
        *,
        actor_id: str,
        correlation_id: str,
    ) -> None:
        if not commands:
            raise EditorialActionError("At least one editorial decision is required")
        if len({command.group_id for command in commands}) != len(commands):
            raise EditorialActionError("A group can only be decided once per confirmation")

        ordered = sorted(commands, key=lambda command: command.group_id.hex)
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditorialGroupNotFoundError(str(edition_id))

            locked: list[tuple[EditorialDecisionCommand, EditorialGroup]] = []
            for command in ordered:
                group = await uow.editorial_groups.get_for_update(command.group_id)
                if group is None or group.edition_id != edition_id:
                    raise EditorialGroupNotFoundError(str(command.group_id))
                if group.version != command.version:
                    raise EditorialActionError(
                        f"Editorial group {group.id} has changed; reload before confirming"
                    )
                if group.status is not EditorialGroupStatus.PROPOSED:
                    raise EditorialActionError(
                        f"Editorial group {group.id} is no longer awaiting a decision"
                    )
                locked.append((command, group))

            # All groups and versions are validated before the first mutation.
            for command, group in locked:
                if command.decision is EditorialDecisionValue.IGNORE:
                    group.reject()
                    await uow.editorial_groups.save(group)
                    await uow.human_decisions.append(
                        HumanDecision(
                            edition_id=edition_id,
                            decision_type=HumanDecisionType.REJECT,
                            group_ids=(group.id,),
                            actor_id=actor_id,
                            correlation_id=correlation_id,
                            payload={
                                "reason": "Ignoré lors de la sélection éditoriale",
                                "batch_confirmation": True,
                            },
                        )
                    )
                    continue

                editorial_type = EditorialType(command.decision.value)
                subject = Subject(
                    external_id=f"edition:{edition_id}:group:{group.id}",
                    slug=_subject_slug(group.title, group.id),
                    tlp=edition.tlp,
                )
                await uow.subjects.add(subject)
                if self._materializer is not None:
                    await self._materializer.materialize(subject, (), (), {}, self._workspace_root)
                group.select(editorial_type, subject.id)
                await uow.editorial_groups.save(group)
                await uow.human_decisions.append(
                    HumanDecision(
                        edition_id=edition_id,
                        decision_type=HumanDecisionType.SELECT,
                        group_ids=(group.id,),
                        actor_id=actor_id,
                        correlation_id=correlation_id,
                        payload={
                            "editorial_type": editorial_type.value,
                            "subject_id": str(subject.id),
                            "score_total": group.score.total,
                            "automatic": False,
                            "batch_confirmation": True,
                        },
                    )
                )
            await uow.commit()

    async def merge(
        self,
        edition_id: UUID,
        group_ids: tuple[UUID, ...],
        *,
        actor_id: str,
        correlation_id: str,
    ) -> EditorialGroup:
        if len(set(group_ids)) < 2:
            raise EditorialActionError("At least two distinct groups are required")
        async with self._uow_factory() as uow:
            groups = [await uow.editorial_groups.get_for_update(item) for item in group_ids]
            if any(group is None or group.edition_id != edition_id for group in groups):
                raise EditorialGroupNotFoundError("One of the groups does not exist")
            concrete = [group for group in groups if group is not None]
            target = concrete[0]
            for source in concrete[1:]:
                target.add_candidates(source.candidate_references)
                source.supersede()
                await uow.editorial_groups.save(source)
            target.grouping_justification = "Fusion décidée par l'analyste."
            target.grouping_confidence = GroupingConfidence.HIGH
            await uow.editorial_groups.save(target)
            await uow.human_decisions.append(
                HumanDecision(
                    edition_id=edition_id,
                    decision_type=HumanDecisionType.MERGE,
                    group_ids=group_ids,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    payload={"target_group_id": str(target.id)},
                )
            )
            await uow.commit()
            return target

    async def split(
        self,
        edition_id: UUID,
        group_id: UUID,
        candidate_ids: tuple[UUID, ...],
        *,
        actor_id: str,
        correlation_id: str,
    ) -> EditorialGroup:
        async with self._uow_factory() as uow:
            group = await uow.editorial_groups.get_for_update(group_id)
            if group is None or group.edition_id != edition_id:
                raise EditorialGroupNotFoundError(str(group_id))
            requested_ids = set(candidate_ids)
            group_candidate_ids = {
                reference.candidate_id for reference in group.candidate_references
            }
            if requested_ids - group_candidate_ids:
                raise EditorialActionError(
                    "Every requested split candidate must belong to the group"
                )
            selected = {
                reference
                for reference in group.candidate_references
                if reference.candidate_id in requested_ids
            }
            if not selected:
                raise EditorialActionError("Split candidates do not belong to the group")
            group.remove_candidates(selected)
            batches = list(await uow.discovery_batches.list_for_edition(edition_id))
            candidate_map = _candidate_map(batches)
            first = candidate_map[next(iter(selected))]
            new_group = EditorialGroup(
                edition_id=edition_id,
                title=first.title,
                candidate_references=tuple(selected),
                outcome=GroupingOutcome.NEW_SUBJECT,
                score=_editorial_score(first),
                source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
                needs_source_verification=True,
                needs_source_expansion=True,
                grouping_confidence=GroupingConfidence.HIGH,
                grouping_justification="Séparation décidée par l'analyste.",
            )
            await uow.editorial_groups.save(group)
            await uow.editorial_groups.add(new_group)
            await uow.human_decisions.append(
                HumanDecision(
                    edition_id=edition_id,
                    decision_type=HumanDecisionType.SPLIT,
                    group_ids=(group.id, new_group.id),
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    payload={"candidate_ids": [str(item) for item in candidate_ids]},
                )
            )
            await uow.commit()
            return new_group

    async def reject(
        self,
        edition_id: UUID,
        group_id: UUID,
        *,
        reason: str,
        actor_id: str,
        correlation_id: str,
    ) -> EditorialGroup:
        async with self._uow_factory() as uow:
            group = await uow.editorial_groups.get_for_update(group_id)
            if group is None or group.edition_id != edition_id:
                raise EditorialGroupNotFoundError(str(group_id))
            group.reject()
            await uow.editorial_groups.save(group)
            await uow.human_decisions.append(
                HumanDecision(
                    edition_id=edition_id,
                    decision_type=HumanDecisionType.REJECT,
                    group_ids=(group.id,),
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    payload={"reason": reason.strip()},
                )
            )
            await uow.commit()
            return group

    async def select(
        self,
        edition_id: UUID,
        group_id: UUID,
        editorial_type: EditorialType,
        *,
        actor_id: str,
        correlation_id: str,
    ) -> EditorialGroup:
        async with self._uow_factory() as uow:
            group = await uow.editorial_groups.get_for_update(group_id)
            edition = await uow.editions.get(edition_id)
            if group is None or group.edition_id != edition_id or edition is None:
                raise EditorialGroupNotFoundError(str(group_id))
            subject = Subject(
                external_id=f"edition:{edition_id}:group:{group.id}",
                slug=_subject_slug(group.title, group.id),
                tlp=edition.tlp,
            )
            await uow.subjects.add(subject)
            if self._materializer is not None:
                await self._materializer.materialize(subject, (), (), {}, self._workspace_root)
            group.select(editorial_type, subject.id)
            await uow.editorial_groups.save(group)
            await uow.human_decisions.append(
                HumanDecision(
                    edition_id=edition_id,
                    decision_type=HumanDecisionType.SELECT,
                    group_ids=(group.id,),
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    payload={
                        "editorial_type": editorial_type.value,
                        "subject_id": str(subject.id),
                        "score_total": group.score.total,
                        "automatic": False,
                    },
                )
            )
            await uow.commit()
            return group

    async def decisions(self, edition_id: UUID) -> list[HumanDecision]:
        async with self._uow_factory() as uow:
            return list(await uow.human_decisions.list_for_edition(edition_id))


def _candidate_map(batches: Sequence[DiscoveryBatch]) -> dict[CandidateReference, CandidateTopic]:
    return {
        CandidateReference(batch.id, candidate.id): candidate
        for batch in batches
        for candidate in batch.candidates
        if candidate.selectable
    }


def _editorial_score(candidate: CandidateTopic) -> EditorialScore:
    impact = min(
        4, max(1, len(candidate.countries) + len(candidate.sectors) + len(candidate.victims))
    )
    novelty = (
        4 if any(word in normalize(candidate.novelty) for word in ("nouveau", "inedit")) else 2
    )
    technical = candidate.technical_potential
    hunting = min(4, len(candidate.likely_artifacts) + len(candidate.iocs) + bool(candidate.cves))
    actionability = min(4, len(candidate.relevance_reasons) + bool(candidate.likely_artifacts))
    role_weight = {
        SourceRole.PRIMARY: 2,
        SourceRole.INDEPENDENT: 2,
        SourceRole.RELAY: 1,
        SourceRole.AGGREGATOR: 0,
        SourceRole.SOCIAL: 0,
        SourceRole.UNKNOWN: 0,
    }
    source_quality = min(4, sum(role_weight[source.role] for source in candidate.sources))
    return EditorialScore(
        impact=impact,
        novelty=novelty,
        technical_depth=technical,
        hunting_potential=hunting,
        actionability=actionability,
        source_quality=source_quality,
        justifications={
            "impact": "Victimes, secteurs et pays mentionnés dans les métadonnées disponibles.",
            "novelty": candidate.novelty,
            "technical_depth": f"Potentiel technique déclaré : {technical}/4.",
            "hunting_potential": "IOC et artefacts techniques signalés, non encore collectés.",
            "actionability": "Raisons de pertinence et artefacts exploitables proposés.",
            "source_quality": "Rôles de sources provisoires issus des citations visibles.",
        },
    )


def _subject_slug(title: str, group_id: UUID) -> str:
    base = "-".join(normalize(title).split())[:100].strip("-") or "subject"
    return f"{base}-{group_id.hex[:8]}"
