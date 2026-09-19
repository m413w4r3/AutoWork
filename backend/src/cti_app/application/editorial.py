from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

from cti_app.application.discovery_identity import normalize
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.subjects import SubjectService
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryCandidate,
    IocPresence,
    SourceRelationshipStatus,
    SourceRole,
)
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialGroupStatus,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.domain.entities import Sample, SourceDocument, Subject
from cti_app.logging import get_correlation_id

logger = logging.getLogger(__name__)


class EditorialGroupNotFoundError(LookupError):
    pass


class EditorialActionError(ValueError):
    pass


class EditorialDecisionValue(StrEnum):
    ARTICLE = "article"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class EditorialDecisionCommand:
    group_id: UUID
    version: int
    decision: EditorialDecisionValue


@dataclass(frozen=True, slots=True)
class EditorialAutoSelectionPolicyV1:
    """Select articles from editorial signals, independently of quotas."""

    version: int = 1
    rule: str = "ioc_signal_v1"
    actor_id: str = "system:editorial-auto-selection"

    def should_select_article(self, candidates: Sequence[CandidateTopic]) -> bool:
        return any(_candidate_has_ioc_signal(candidate) for candidate in candidates)


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
    selected_articles: int
    ignored: int
    undecided: int


class EditorialGroupingService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        materializer: WorkspaceMaterializer | None = None,
        workspace_root: Path = Path("work/subjects"),
        auto_selection_policy: EditorialAutoSelectionPolicyV1 | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._subjects = SubjectService(uow_factory)
        self._materializer = materializer
        self._workspace_root = workspace_root
        self._auto_selection_policy = auto_selection_policy or EditorialAutoSelectionPolicyV1()

    async def _materialize_subject(
        self, edition_id: UUID, group_id: UUID, subject: Subject
    ) -> None:
        if self._materializer is None:
            return
        try:
            await self._materializer.materialize(subject, (), (), {}, self._workspace_root)
        except Exception:
            logger.exception(
                "subject_workspace_materialize_failed",
                extra={
                    "operation": "subject_workspace_materialize",
                    "edition_id": str(edition_id),
                    "group_id": str(group_id),
                    "subject_id": str(subject.id),
                    "correlation_id": get_correlation_id(),
                },
            )

    async def synchronize(self, edition_id: UUID) -> list[EditorialGroup]:
        selected_subjects: list[tuple[UUID, Subject]] = []
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditorialGroupNotFoundError(str(edition_id))
            existing = list(await uow.editorial_groups.list_for_edition(edition_id))
            snapshot = await uow.discovery_snapshots.get_active(edition_id)
            if snapshot is None:
                for group in existing:
                    if (
                        group.status is EditorialGroupStatus.PROPOSED
                        and group.discovery_subject_id is not None
                    ):
                        group.supersede()
                        await uow.editorial_groups.save(group)
                await uow.commit()
                return existing
            canonical_candidates = await uow.discovery_candidates.list_for_edition(
                edition_id, include_replaced=False
            )
            candidates = _candidate_map(canonical_candidates)
            candidates_by_id = {candidate.id: candidate for candidate in canonical_candidates}
            snapshot_candidates: dict[UUID, CandidateTopic] = {}
            by_subject = {
                group.discovery_subject_id: group
                for group in existing
                if group.discovery_subject_id is not None
            }
            for subject in snapshot.subjects:
                snapshot_candidates[subject.subject_id] = subject.candidate
                references = tuple(
                    CandidateReference(
                        candidates_by_id[item.candidate_id].discovery_batch_id,
                        item.candidate_id,
                    )
                    for item in subject.member_references
                    if item.candidate_id in candidates_by_id
                )
                projected = by_subject.get(subject.subject_id)
                if not references:
                    if (
                        projected is not None
                        and projected.status is EditorialGroupStatus.PROPOSED
                    ):
                        projected.supersede()
                        await uow.editorial_groups.save(projected)
                    continue
                for reference in references:
                    candidates.setdefault(
                        reference,
                        candidates_by_id[reference.candidate_id].to_candidate_topic(),
                    )
                if projected is not None:
                    if projected.status in {
                        EditorialGroupStatus.PROPOSED,
                        EditorialGroupStatus.SELECTED,
                    }:
                        if references != projected.candidate_references:
                            projected.synchronize_candidate_references(references)
                            projected.needs_source_expansion = True
                            projected.needs_source_verification = True
                            await uow.editorial_groups.save(projected)
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
            active_subject_ids = {subject.subject_id for subject in snapshot.subjects}
            for group in existing:
                if (
                    group.status is EditorialGroupStatus.PROPOSED
                    and group.discovery_subject_id is not None
                    and group.discovery_subject_id not in active_subject_ids
                ):
                    group.supersede()
                    await uow.editorial_groups.save(group)
                    continue
                if group.status is not EditorialGroupStatus.PROPOSED:
                    continue
                group_candidates = tuple(
                    candidates[reference]
                    for reference in group.candidate_references
                    if reference in candidates
                )
                snapshot_candidate = (
                    snapshot_candidates.get(group.discovery_subject_id)
                    if group.discovery_subject_id is not None
                    else None
                )
                if snapshot_candidate is not None:
                    group_candidates = (*group_candidates, snapshot_candidate)
                if self._auto_selection_policy.should_select_article(group_candidates):
                    selected_subject = await self._select_locked(
                        uow,
                        edition,
                        group,
                        actor_id=self._auto_selection_policy.actor_id,
                        correlation_id=f"editorial-auto-selection-v1:{group.id}",
                        automatic=True,
                        rule=self._auto_selection_policy.rule,
                        policy_version=self._auto_selection_policy.version,
                    )
                    selected_subjects.append((group.id, selected_subject))
            await uow.commit()
        for group_id, selected_subject in selected_subjects:
            await self._materialize_subject(edition_id, group_id, selected_subject)
        return existing

    async def board(self, edition_id: UUID) -> EditorialBoard:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditorialGroupNotFoundError(str(edition_id))
            groups = list(await uow.editorial_groups.list_for_edition(edition_id))
            historical = list(await uow.editorial_groups.list_historical(edition_id))
            snapshot = await uow.discovery_snapshots.get_active(edition_id)
            canonical_candidates = await uow.discovery_candidates.list_for_edition(
                edition_id, include_replaced=False
            )
            candidates_by_id = {candidate.id: candidate for candidate in canonical_candidates}
            if snapshot is not None:
                subjects_by_id = {subject.subject_id: subject for subject in snapshot.subjects}
                for group in groups:
                    if (
                        group.status is EditorialGroupStatus.PROPOSED
                        and group.discovery_subject_id is not None
                        and group.discovery_subject_id not in subjects_by_id
                    ):
                        group.supersede()
                        await uow.editorial_groups.save(group)
                        continue
                    if group.discovery_subject_id is None:
                        continue
                    subject = subjects_by_id.get(group.discovery_subject_id)
                    if subject is None or group.status not in {
                        EditorialGroupStatus.PROPOSED,
                        EditorialGroupStatus.SELECTED,
                    }:
                        continue
                    references = tuple(
                        CandidateReference(
                            candidates_by_id[reference.candidate_id].discovery_batch_id,
                            reference.candidate_id,
                        )
                        for reference in subject.member_references
                        if reference.candidate_id in candidates_by_id
                    )
                    if references and references != group.candidate_references:
                        group.synchronize_candidate_references(references)
                        await uow.editorial_groups.save(group)
                await uow.commit()
            selected = [group for group in groups if group.status is EditorialGroupStatus.SELECTED]
            ignored = [group for group in groups if group.status is EditorialGroupStatus.REJECTED]
            undecided = [group for group in groups if group.status is EditorialGroupStatus.PROPOSED]
            return EditorialBoard(
                groups=groups,
                candidates=_candidate_map(canonical_candidates),
                historical_groups={group.id: group for group in [*historical, *selected]},
                selected_articles=len(selected),
                ignored=len(ignored),
                undecided=len(undecided),
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
        selected_subjects: list[tuple[UUID, Subject]] = []
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

                subject = await self._select_locked(
                    uow,
                    edition,
                    group,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    batch_confirmation=True,
                )
                selected_subjects.append((group.id, subject))
            await uow.commit()
        for group_id, subject in selected_subjects:
            await self._materialize_subject(edition_id, group_id, subject)

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
        *,
        actor_id: str,
        correlation_id: str,
    ) -> EditorialGroup:
        async with self._uow_factory() as uow:
            group = await uow.editorial_groups.get_for_update(group_id)
            edition = await uow.editions.get(edition_id)
            if group is None or group.edition_id != edition_id or edition is None:
                raise EditorialGroupNotFoundError(str(group_id))
            subject = await self._select_locked(
                uow,
                edition,
                group,
                actor_id=actor_id,
                correlation_id=correlation_id,
            )
            await uow.commit()
        await self._materialize_subject(edition_id, group.id, subject)
        return group

    async def _select_locked(
        self,
        uow: UnitOfWork,
        edition: Edition,
        group: EditorialGroup,
        *,
        actor_id: str,
        correlation_id: str,
        automatic: bool = False,
        rule: str | None = None,
        policy_version: int | None = None,
        batch_confirmation: bool = False,
    ) -> Subject:
        # All selection modes share this transaction and mutation sequence.
        subject = await self._subjects.materialize_in_uow(
            uow,
            edition_id=group.edition_id,
            title=group.title,
        )
        group.select(subject.id)
        await uow.editorial_groups.save(group)
        payload: dict[str, object] = {
            "subject_id": str(subject.id),
            "score_total": group.score.total,
            "automatic": automatic,
        }
        if rule is not None:
            payload["rule"] = rule
        if policy_version is not None:
            payload["policy_version"] = policy_version
        if batch_confirmation:
            payload["batch_confirmation"] = True
        await uow.human_decisions.append(
            HumanDecision(
                edition_id=group.edition_id,
                decision_type=HumanDecisionType.SELECT,
                group_ids=(group.id,),
                actor_id=actor_id,
                correlation_id=correlation_id,
                payload=payload,
            )
        )
        return subject

    async def decisions(self, edition_id: UUID) -> list[HumanDecision]:
        async with self._uow_factory() as uow:
            return list(await uow.human_decisions.list_for_edition(edition_id))


def _candidate_map(
    candidates: Sequence[DiscoveryCandidate],
) -> dict[CandidateReference, CandidateTopic]:
    projected = [
        (
            CandidateReference(candidate.discovery_batch_id, candidate.id),
            candidate.to_candidate_topic(),
        )
        for candidate in candidates
    ]
    return {reference: topic for reference, topic in projected if topic.selectable}


def _candidate_has_ioc_signal(candidate: CandidateTopic) -> bool:
    if candidate.iocs or candidate.provisional_iocs:
        return True
    return any(
        source.ioc_presence in {IocPresence.DECLARED, IocPresence.VISIBLE}
        or getattr(source.ioc_presence, "value", source.ioc_presence) == "present"
        or (source.ioc_declared_count is not None and source.ioc_declared_count > 0)
        or (source.ioc_visible_count is not None and source.ioc_visible_count > 0)
        for source in candidate.sources
    )


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
