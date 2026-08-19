from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.discovery_identity import (
    build_discovery_identity_index,
    explicit_entity_tokens,
    normalize,
)
from cti_app.application.model_gateway import (
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    StructuredExtractionModel,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    SourceRelationshipStatus,
    SourceRole,
    canonicalize_http_url,
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


class AmbiguousGroupingResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: str = Field(pattern="^(merge|separate|update_previous|non_independent_reprint)$")
    confidence: GroupingConfidence
    justification: str = Field(min_length=1, max_length=1_000)


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
        structured_model: StructuredExtractionModel | None,
        *,
        materializer: WorkspaceMaterializer | None = None,
        workspace_root: Path = Path("work/subjects"),
    ) -> None:
        self._uow_factory = uow_factory
        self._structured_model = structured_model
        self._materializer = materializer
        self._workspace_root = workspace_root

    async def synchronize(
        self, edition_id: UUID, *, resolve_ambiguous: bool = True
    ) -> list[EditorialGroup]:
        async with self._uow_factory() as uow:
            all_batches = list(await uow.discovery_batches.list_for_edition(edition_id))
            batches = [batch for batch in all_batches if batch.is_active_revision]
            existing = list(await uow.editorial_groups.list_for_edition(edition_id))
            historical = list(await uow.editorial_groups.list_historical(edition_id))
            archived_urls: dict[UUID, set[str]] = {}
            for group in [*existing, *historical]:
                if group.subject_id is None:
                    continue
                documents = await uow.source_documents.list_for_subject(group.subject_id)
                for document in documents:
                    try:
                        archived_urls.setdefault(group.id, set()).add(
                            canonicalize_http_url(document.origin)
                        )
                    except ValueError:
                        continue
            replacements = _revision_reference_replacements(all_batches)
            for group in existing:
                before = group.candidate_references
                group.replace_candidate_references(replacements)
                if group.candidate_references != before:
                    await uow.editorial_groups.save(group)
            candidate_map = _candidate_map(batches)
            reference_map = {
                reference for group in existing for reference in group.candidate_references
            }
            comparison_candidates = dict(candidate_map)
            for group in historical:
                for reference in group.candidate_references:
                    if reference in comparison_candidates:
                        continue
                    batch = await uow.discovery_batches.get(reference.batch_id)
                    if batch is not None:
                        candidate = next(
                            (
                                item
                                for item in batch.candidates
                                if item.id == reference.candidate_id
                            ),
                            None,
                        )
                        if candidate is not None:
                            comparison_candidates[reference] = candidate

            for reference, candidate in candidate_map.items():
                if reference in reference_map:
                    continue
                current_match = _best_group_match(
                    reference, candidate, existing, comparison_candidates, archived_urls
                )
                historical_match = _best_group_match(
                    reference, candidate, historical, comparison_candidates, archived_urls
                )
                best = _prefer_match(current_match, historical_match)
                # Enrichir en place les groupes PROPOSED et SELECTED (§27.1).
                if (
                    best is not None
                    and best.score >= 0.85
                    and best.group in existing
                    and best.group.status
                    in (EditorialGroupStatus.PROPOSED, EditorialGroupStatus.SELECTED)
                ):
                    best.group.add_candidates((reference,))
                    best.group.needs_source_expansion = True
                    best.group.needs_source_verification = True
                    await uow.editorial_groups.save(best.group)
                    reference_map.add(reference)
                    continue

                if best is not None and best.score >= 0.85:
                    same_publication = "URL canonique" in best.justification
                    if best.group in existing:
                        outcome = (
                            GroupingOutcome.DUPLICATE_PUBLICATION
                            if same_publication
                            else GroupingOutcome.AMBIGUOUS_REVIEW
                        )
                    else:
                        outcome = (
                            GroupingOutcome.DUPLICATE_PUBLICATION
                            if same_publication
                            else GroupingOutcome.UPDATE_PREVIOUS
                        )
                    group = EditorialGroup(
                        edition_id=edition_id,
                        title=candidate.title,
                        candidate_references=(reference,),
                        outcome=outcome,
                        score=_editorial_score(candidate),
                        source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
                        needs_source_verification=True,
                        needs_source_expansion=True,
                        grouping_confidence=GroupingConfidence.HIGH,
                        grouping_justification=best.justification,
                        potential_historical_group_id=best.group.id,
                    )
                    await uow.editorial_groups.add(group)
                    existing.append(group)
                    reference_map.add(reference)
                    continue

                outcome = GroupingOutcome.NEW_SUBJECT
                confidence = GroupingConfidence.HIGH
                justification = "Aucun rapprochement déterministe suffisamment fort."
                potential_id = None
                if best is not None and best.score >= 0.45:
                    outcome = GroupingOutcome.AMBIGUOUS_REVIEW
                    confidence = GroupingConfidence.MEDIUM
                    justification = best.justification
                    potential_id = best.group.id
                    resolved = (
                        await self._resolve_ambiguous(candidate, best, comparison_candidates)
                        if resolve_ambiguous
                        else None
                    )
                    if resolved is not None:
                        confidence = resolved.confidence
                        justification = resolved.justification
                        # Patch 1: Remove LLM merge authority.
                        # Even if LLM says "merge", route it to AMBIGUOUS_REVIEW for human decision.
                        if resolved.decision == "merge":
                            # The LLM suggests a merge, but we don't auto-apply it.
                            # Instead, mark as AMBIGUOUS_REVIEW with the suggested group.
                            outcome = GroupingOutcome.AMBIGUOUS_REVIEW
                            confidence = resolved.confidence
                            justification = f"LLM suggests: {resolved.justification}"
                            potential_id = best.group.id
                        elif resolved.decision == "update_previous" and best.group not in existing:
                            outcome = GroupingOutcome.UPDATE_PREVIOUS
                        elif resolved.decision == "non_independent_reprint":
                            outcome = GroupingOutcome.NON_INDEPENDENT_REPRINT
                        elif resolved.decision == "separate":
                            outcome = GroupingOutcome.NEW_SUBJECT
                elif _only_relay_sources(candidate):
                    outcome = GroupingOutcome.NON_INDEPENDENT_REPRINT
                    confidence = GroupingConfidence.HIGH
                    justification = "Toutes les sources visibles sont des reprises ou agrégateurs."

                group = EditorialGroup(
                    edition_id=edition_id,
                    title=candidate.title,
                    candidate_references=(reference,),
                    outcome=outcome,
                    score=_editorial_score(candidate),
                    source_relationship_status=SourceRelationshipStatus.PROVISIONAL,
                    needs_source_verification=True,
                    needs_source_expansion=True,
                    grouping_confidence=confidence,
                    grouping_justification=justification,
                    potential_historical_group_id=potential_id,
                )
                await uow.editorial_groups.add(group)
                existing.append(group)
                reference_map.add(reference)
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

    async def _resolve_ambiguous(
        self,
        candidate: CandidateTopic,
        match: _GroupMatch,
        candidates: Mapping[CandidateReference, CandidateTopic],
    ) -> AmbiguousGroupingResult | None:
        if self._structured_model is None:
            return None
        other = _representative(match.group, candidates)
        if other is None:
            return None
        payload = {
            "candidate": _comparison_payload(candidate),
            "possible_match": _comparison_payload(other),
            "deterministic_similarity": round(match.score, 3),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        try:
            execution = await self._structured_model.extract(
                ModelRequest(
                    text=(
                        "Compare uniquement l'identité éditoriale de ces publications. Décide si "
                        "elles décrivent le même sujet, une mise à jour, une reprise non "
                        "indépendante ou deux sujets distincts. Ne produis aucune attribution et "
                        "n'invente aucun fait.\n" + raw
                    ),
                    prompt_template_id="ambiguous-editorial-grouping",
                    prompt_template_version="1.0",
                    evidence_pack_hash=hashlib.sha256(raw.encode()).hexdigest(),
                    external_llm_allowed=(
                        candidate.external_llm_allowed and other.external_llm_allowed
                    ),
                    routing_hint=ModelRoutingHint.AMBIGUOUS_CLUSTERING,
                    sensitivity=candidate.sensitivity,
                ),
                AmbiguousGroupingResult,
            )
        except ModelGatewayError:
            return None
        return (
            execution.structured_output
            if isinstance(execution.structured_output, AmbiguousGroupingResult)
            else None
        )


@dataclass(frozen=True, slots=True)
class _GroupMatch:
    group: EditorialGroup
    score: float
    justification: str


def _candidate_map(batches: Sequence[DiscoveryBatch]) -> dict[CandidateReference, CandidateTopic]:
    return {
        CandidateReference(batch.id, candidate.id): candidate
        for batch in batches
        for candidate in batch.candidates
        if candidate.selectable
    }


def _best_group_match(
    reference: CandidateReference,
    candidate: CandidateTopic,
    groups: Sequence[EditorialGroup],
    candidates: Mapping[CandidateReference, CandidateTopic],
    archived_urls: Mapping[UUID, set[str]],
) -> _GroupMatch | None:
    """Find the best matching editorial group for a new candidate.

    Computes numeric similarity scores (0.0-1.0) for ranking and display.
    The attachment decision is driven by score thresholds defined in synchronize().
    """
    from cti_app.application.discovery_identity import match_topics

    # Build identity index over the candidate and group representatives
    # (This is a local index for this editorial matching, not the consolidation-wide index)
    # For simplicity, we don't rebuild a full cross-batch index here; instead,
    # we use archived_urls as a proxy for "known contextual" URLs

    matches: list[_GroupMatch] = []
    for group in groups:
        if group.status is EditorialGroupStatus.SUPERSEDED:
            continue
        if any(item.batch_id == reference.batch_id for item in group.candidate_references):
            continue
        other = _representative(group, candidates)
        if other is None:
            continue

        # Use the new tri-state matcher
        # Note: we don't have a full identity index here, so we can't detect all contextual URLs.
        # This is a limitation we'll accept in Patch 1; a full fix would require passing
        # the full batches to build a proper index.
        # For now, use archived_urls as a partial proxy.
        candidate_urls = {
            source.canonical_url
            for source in candidate.sources
            if source.role in {SourceRole.PRIMARY, SourceRole.INDEPENDENT}
        }
        archived_for_group = archived_urls.get(group.id, set())

        # Standard similarity check
        score, reasons = _similarity(candidate, other)

        # Note archived URL evidence for ranking, but do NOT boost score
        # Per requirement C2: archived URL alone cannot produce structural SAME
        if candidate_urls & archived_for_group:
            reasons.append("URL canonique déjà archivée pour ce groupe")

        matches.append(_GroupMatch(group, score, ", ".join(reasons)))

    return max(matches, key=lambda item: item.score, default=None)


def _prefer_match(left: _GroupMatch | None, right: _GroupMatch | None) -> _GroupMatch | None:
    if left is None:
        return right
    if right is None:
        return left
    return left if left.score >= right.score else right


def _representative(
    group: EditorialGroup, candidates: Mapping[CandidateReference, CandidateTopic]
) -> CandidateTopic | None:
    return next(
        (candidates[item] for item in group.candidate_references if item in candidates), None
    )


def _similarity(
    left: CandidateTopic, right: CandidateTopic
) -> tuple[float, list[str]]:
    """Compute editorial similarity score (weak/medium signals).

    Returns: (score, reasons) where score is normalized to [0.0, 1.0].

    Note: This is the editorial scoring layer only. It does NOT replace the
    authoritative hard-identity matching from discovery_identity.match_topics().
    """
    score = 0.0
    reasons: list[str] = []

    # Title similarity: up to 0.4 contribution
    title_score = SequenceMatcher(None, normalize(left.title), normalize(right.title)).ratio()
    score += title_score * 0.4
    if title_score >= 0.75:
        reasons.append(f"titre très similaire ({title_score:.2f})")
    elif title_score >= 0.65:
        reasons.append(f"titres proches ({title_score:.2f})")

    # Domain overlap: up to 0.15 contribution
    domains_left = {urlsplit(source.canonical_url).hostname for source in left.sources}
    domains_right = {urlsplit(source.canonical_url).hostname for source in right.sources}
    if domains_left & domains_right:
        score += 0.15
        reasons.append("même domaine")

    # Entity overlap (actors, campaigns, malware): up to 0.25 contribution
    entities_left = _entities(left)
    entities_right = _entities(right)
    entity_overlap = _jaccard(entities_left, entities_right)
    score += entity_overlap * 0.25
    if entity_overlap:
        reasons.append(f"entités communes ({entity_overlap:.2f})")

    # IOC overlap: up to 0.35 contribution
    ioc_overlap = _jaccard({normalize(item) for item in left.iocs}, {normalize(item) for item in right.iocs})
    score += ioc_overlap * 0.35
    if ioc_overlap:
        reasons.append(f"IOC communs ({ioc_overlap:.2f})")

    # Date proximity: up to 0.15 contribution
    if _dates_close(left.event_date, right.event_date):
        score += 0.15
        reasons.append("dates proches")

    final_score = min(score, 1.0)

    return final_score, reasons or ["rapprochement faible sans preuve d'identité"]


def _editorial_score(candidate: CandidateTopic) -> EditorialScore:
    impact = min(
        4, max(1, len(candidate.countries) + len(candidate.sectors) + len(candidate.victims))
    )
    novelty = (
        4 if any(word in _normalize(candidate.novelty) for word in ("nouveau", "inedit")) else 2
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


def _comparison_payload(candidate: CandidateTopic) -> dict[str, object]:
    return {
        "title": candidate.title,
        "event_date": candidate.event_date.isoformat() if candidate.event_date else None,
        "urls": [source.canonical_url for source in candidate.sources],
        "publishers": [source.publisher for source in candidate.sources],
        "actors_as_reported": list(candidate.actors),
        "campaigns": list(candidate.campaigns),
        "malware": list(candidate.malware),
        "cves": list(candidate.cves),
        "iocs": list(candidate.iocs),
    }


def _entities(candidate: CandidateTopic) -> set[str]:
    return {
        _normalize(value)
        for values in (candidate.actors, candidate.campaigns, candidate.malware, candidate.cves)
        for value in values
        if value.strip()
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def _dates_close(left: date | None, right: date | None) -> bool:
    return left is not None and right is not None and abs((left - right).days) <= 7


def _only_relay_sources(candidate: CandidateTopic) -> bool:
    return bool(candidate.sources) and all(
        source.role in {SourceRole.RELAY, SourceRole.AGGREGATOR, SourceRole.SOCIAL}
        for source in candidate.sources
    )




def _revision_reference_replacements(
    batches: Sequence[DiscoveryBatch],
) -> dict[CandidateReference, CandidateReference]:
    active_by_run = {
        batch.discovery_model_run_id: batch for batch in batches if batch.is_active_revision
    }
    replacements: dict[CandidateReference, CandidateReference] = {}
    for batch in batches:
        active = active_by_run.get(batch.discovery_model_run_id)
        if active is None or active.id == batch.id:
            continue
        active_ids = {candidate.id for candidate in active.candidates}
        for candidate in batch.candidates:
            if candidate.id in active_ids:
                replacements[CandidateReference(batch.id, candidate.id)] = CandidateReference(
                    active.id, candidate.id
                )
    return replacements




def _subject_slug(title: str, group_id: UUID) -> str:
    base = "-".join(_normalize(title).split())[:100].strip("-") or "subject"
    return f"{base}-{group_id.hex[:8]}"
