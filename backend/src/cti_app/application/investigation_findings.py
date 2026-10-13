"""Code-validated, append-only investigation finding operations."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid5

from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    VersionConflictError,
)
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    NoveltyClass,
    canonical_json_bytes,
)

_FINDING_NAMESPACE = UUID("d89458ef-e8cf-4e8e-8291-bc76f0fd3150")
_NO_VALUE = object()
SUPPORTED_OBJECT_LIMITATION = "l'observation citée ne porte pas sur l'objet visé"
_OBJECT_IDENTIFIED_KINDS = frozenset(
    {
        FindingKind.NEW_SAMPLE,
        FindingKind.NETWORK_BEHAVIOUR,
        FindingKind.INFECTION_CHAIN,
        FindingKind.CAPABILITY,
        FindingKind.DETECTION,
    }
)


def constrain_supported_assessment(
    *,
    kind: FindingKind,
    assessment: FindingAssessment,
    subject_ref: str | None,
    claim_refs: tuple[str, ...],
    observations: tuple[EvidenceObservation, ...],
    limitations: tuple[str, ...],
) -> tuple[FindingAssessment, tuple[str, ...]]:
    """Keep a Bridge verdict supported only when cited evidence names its subject."""
    limits = list(limitations)
    if assessment is not FindingAssessment.SUPPORTED:
        return assessment, tuple(dict.fromkeys(limits))

    if kind in _OBJECT_IDENTIFIED_KINDS:
        if (
            subject_ref is None
            or not subject_ref.strip()
            or not any(item.object_id == subject_ref for item in observations)
        ):
            if SUPPORTED_OBJECT_LIMITATION not in limits:
                limits.append(SUPPORTED_OBJECT_LIMITATION)
            assessment = (
                FindingAssessment.QUALIFIED
                if subject_ref is not None and subject_ref.strip() and observations
                else FindingAssessment.INCONCLUSIVE
            )
    elif kind is FindingKind.CLAIM_VERIFICATION:
        if not claim_refs or not any(
            item.kind is not EvidenceKind.CLAIM_SOURCE for item in observations
        ):
            assessment = FindingAssessment.INCONCLUSIVE
            limits.append(
                "La validation de claim exige une claim de l'article et une observation de preuve."
            )
    return assessment, tuple(dict.fromkeys(limits))


class ClaimVerdict(StrEnum):
    CONFIRMED = "confirmé"
    QUALIFIED = "nuancé"
    CONTRADICTED = "contredit"
    NOT_ESTABLISHED = "non établi"


class FindingDowngradeReason(StrEnum):
    SUPPORTED_WITHOUT_OBSERVATION = "supported_without_observation"


@dataclass(frozen=True, slots=True)
class ClaimKnowledge:
    """Current claim assessment plus the evidence and finding history behind it."""

    investigation_id: UUID
    claim_id: str
    claim_statement: str
    verdict: ClaimVerdict
    finding_versions: tuple[InvestigationFinding, ...]
    current_findings: tuple[InvestigationFinding, ...]
    favorable_observations: tuple[EvidenceObservation, ...]
    contradictory_observations: tuple[EvidenceObservation, ...]
    qualified_observations: tuple[EvidenceObservation, ...]
    ignored_observations: tuple[EvidenceObservation, ...]
    missing_observation_refs: tuple[UUID, ...]
    absence_of_evidence: bool


class FindingService:
    """Validate citations against canonical observations and the article snapshot."""

    def __init__(
        self,
        *,
        findings: FindingRepository,
        observations: EvidenceObservationRepository,
        article_snapshots: ArticleSnapshotPort,
    ) -> None:
        self._findings = findings
        self._observations = observations
        self._article_snapshots = article_snapshots

    async def propose(
        self,
        *,
        investigation_id: UUID,
        kind: FindingKind,
        assessment: FindingAssessment,
        statement: str,
        observation_refs: tuple[UUID, ...] = (),
        claim_refs: tuple[str, ...] = (),
        novelty: NoveltyClass | None = None,
        scope: str = "",
        limitations: tuple[str, ...] = (),
        created_at: datetime | None = None,
    ) -> InvestigationFinding:
        refs = await self._validated_observation_refs(investigation_id, observation_refs)
        claims = await self._validated_claim_refs(investigation_id, claim_refs)
        validated_assessment, validated_limits = self._assessment_and_limits(
            assessment, refs, limitations
        )
        identity = self._finding_identity(
            investigation_id=investigation_id,
            kind=kind,
            assessment=validated_assessment,
            statement=statement,
            observation_refs=refs,
            claim_refs=claims,
            novelty=novelty,
            scope=scope,
            limitations=validated_limits,
        )
        finding_id = uuid5(
            _FINDING_NAMESPACE,
            f"propose:{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()}",
        )
        existing = await self._findings.get(finding_id)
        if existing is not None:
            return existing
        finding = InvestigationFinding(
            id=finding_id,
            investigation_id=investigation_id,
            kind=kind,
            assessment=validated_assessment,
            statement=statement,
            observation_refs=refs,
            claim_refs=claims,
            novelty=novelty,
            scope=scope,
            limitations=validated_limits,
            created_at=created_at or datetime.now(UTC),
        )
        return await self._findings.append_version(finding, expected_previous_id=None)

    async def revise(
        self,
        finding_id: UUID,
        *,
        assessment: FindingAssessment,
        statement: str,
        observation_refs: tuple[UUID, ...] | None = None,
        claim_refs: tuple[str, ...] | None = None,
        novelty: object = _NO_VALUE,
        scope: str | None = None,
        limitations: tuple[str, ...] | None = None,
        created_at: datetime | None = None,
    ) -> InvestigationFinding:
        previous = await self._findings.get(finding_id)
        if previous is None:
            raise ValueError("Finding version does not exist")

        refs = await self._validated_observation_refs(
            previous.investigation_id,
            previous.observation_refs if observation_refs is None else observation_refs,
        )
        claims = await self._validated_claim_refs(
            previous.investigation_id,
            previous.claim_refs if claim_refs is None else claim_refs,
        )
        next_novelty = previous.novelty if novelty is _NO_VALUE else novelty
        if next_novelty is not None and not isinstance(next_novelty, NoveltyClass):
            raise TypeError("Finding novelty must use NoveltyClass")
        next_scope = previous.scope if scope is None else scope
        next_limits = previous.limitations if limitations is None else limitations
        validated_assessment, validated_limits = self._assessment_and_limits(
            assessment, refs, next_limits
        )
        identity = self._finding_identity(
            investigation_id=previous.investigation_id,
            kind=previous.kind,
            assessment=validated_assessment,
            statement=statement,
            observation_refs=refs,
            claim_refs=claims,
            novelty=next_novelty,
            scope=next_scope,
            limitations=validated_limits,
        )
        revised_id = uuid5(
            _FINDING_NAMESPACE,
            f"revise:{previous.id}:{hashlib.sha256(canonical_json_bytes(identity)).hexdigest()}",
        )
        existing = await self._findings.get(revised_id)
        if existing is not None:
            if not self._same_revision(existing, previous, identity):
                raise ValueError("Finding version id is already used with different content")
            return existing

        versions = await self._findings.list_for_investigation(previous.investigation_id)
        if any(item.supersedes == previous.id for item in versions):
            raise VersionConflictError("Finding already has a newer version")
        revised = InvestigationFinding(
            id=revised_id,
            investigation_id=previous.investigation_id,
            kind=previous.kind,
            assessment=validated_assessment,
            statement=statement,
            observation_refs=refs,
            claim_refs=claims,
            novelty=next_novelty,
            scope=next_scope,
            limitations=validated_limits,
            version=previous.version + 1,
            supersedes=previous.id,
            created_at=created_at or datetime.now(UTC),
        )
        try:
            return await self._findings.append_version(revised, expected_previous_id=previous.id)
        except ValueError:
            concurrent = await self._findings.get(revised.id)
            if concurrent is not None and self._same_revision(concurrent, previous, identity):
                return concurrent
            raise

    async def knowledge_of_claim(self, investigation_id: UUID, claim_id: str) -> ClaimKnowledge:
        claims = await self._article_snapshots.get_claims(investigation_id, (claim_id,))
        claim = next((item for item in claims if item.id == claim_id), None)
        if claim is None:
            raise ValueError("Claim does not belong to the investigation article snapshot")

        investigation_findings = await self._findings.list_for_investigation(investigation_id)
        all_findings = tuple(
            sorted(
                (item for item in investigation_findings if claim_id in item.claim_refs),
                key=lambda item: (item.version, item.created_at, str(item.id)),
            )
        )
        superseded_ids = {
            item.supersedes for item in investigation_findings if item.supersedes is not None
        }
        current = tuple(item for item in all_findings if item.id not in superseded_ids)
        all_observations = await self._observations.list_for_investigation(investigation_id)
        observation_by_id = {item.id: item for item in all_observations}
        missing_refs: set[UUID] = set()
        favorable_ids: set[UUID] = set()
        contradictory_ids: set[UUID] = set()
        qualified_ids: set[UUID] = set()
        linked_ids: set[UUID] = set()
        has_supported = False
        has_refuted = False
        has_qualified = False

        for finding in current:
            valid_ids = {
                ref
                for ref in finding.observation_refs
                if ref in observation_by_id
                and observation_by_id[ref].investigation_id == investigation_id
            }
            missing_refs.update(set(finding.observation_refs).difference(valid_ids))
            linked_ids.update(valid_ids)
            if finding.assessment is FindingAssessment.SUPPORTED and valid_ids:
                has_supported = True
                favorable_ids.update(valid_ids)
            elif finding.assessment is FindingAssessment.REFUTED and valid_ids:
                has_refuted = True
                contradictory_ids.update(valid_ids)
            elif finding.assessment is FindingAssessment.QUALIFIED and valid_ids:
                has_qualified = True
                qualified_ids.update(valid_ids)

        if has_qualified or (has_supported and has_refuted):
            verdict = ClaimVerdict.QUALIFIED
        elif has_refuted:
            verdict = ClaimVerdict.CONTRADICTED
        elif has_supported:
            verdict = ClaimVerdict.CONFIRMED
        else:
            verdict = ClaimVerdict.NOT_ESTABLISHED

        return ClaimKnowledge(
            investigation_id=investigation_id,
            claim_id=claim_id,
            claim_statement=claim.statement,
            verdict=verdict,
            finding_versions=all_findings,
            current_findings=current,
            favorable_observations=tuple(
                observation_by_id[ref] for ref in sorted(favorable_ids, key=str)
            ),
            contradictory_observations=tuple(
                observation_by_id[ref] for ref in sorted(contradictory_ids, key=str)
            ),
            qualified_observations=tuple(
                observation_by_id[ref] for ref in sorted(qualified_ids, key=str)
            ),
            ignored_observations=tuple(
                item for item in all_observations if item.id not in linked_ids
            ),
            missing_observation_refs=tuple(sorted(missing_refs, key=str)),
            absence_of_evidence=not (favorable_ids or contradictory_ids or qualified_ids),
        )

    async def _validated_observation_refs(
        self, investigation_id: UUID, refs: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        result: list[UUID] = []
        for ref in refs:
            observation = await self._observations.get(ref)
            if observation is None:
                raise ValueError(f"Observation {ref} does not exist")
            if observation.investigation_id != investigation_id:
                raise ValueError(f"Observation {ref} belongs to another investigation")
            if ref not in result:
                result.append(ref)
        return tuple(result)

    async def _validated_claim_refs(
        self, investigation_id: UUID, refs: tuple[str, ...]
    ) -> tuple[str, ...]:
        unique_refs = tuple(dict.fromkeys(refs))
        if not unique_refs:
            return ()
        claims = await self._article_snapshots.get_claims(investigation_id, unique_refs)
        valid_ids = {item.id for item in claims}
        unknown = tuple(ref for ref in unique_refs if ref not in valid_ids)
        if unknown:
            raise ValueError(f"Claim references are not in the article snapshot: {unknown}")
        return unique_refs

    @staticmethod
    def _assessment_and_limits(
        assessment: FindingAssessment,
        observation_refs: tuple[UUID, ...],
        limitations: tuple[str, ...],
    ) -> tuple[FindingAssessment, tuple[str, ...]]:
        if not isinstance(assessment, FindingAssessment):
            raise TypeError("Finding assessment must use FindingAssessment")
        result = tuple(dict.fromkeys(limitations))
        if assessment is FindingAssessment.SUPPORTED and not observation_refs:
            assessment = FindingAssessment.INCONCLUSIVE
            result = (*result, FindingDowngradeReason.SUPPORTED_WITHOUT_OBSERVATION.value)
        return assessment, result

    @staticmethod
    def _finding_identity(
        *,
        investigation_id: UUID,
        kind: FindingKind,
        assessment: FindingAssessment,
        statement: str,
        observation_refs: tuple[UUID, ...],
        claim_refs: tuple[str, ...],
        novelty: NoveltyClass | None,
        scope: str,
        limitations: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "investigation_id": str(investigation_id),
            "kind": kind.value,
            "assessment": assessment.value,
            "statement": statement,
            "observation_refs": [str(item) for item in observation_refs],
            "claim_refs": list(claim_refs),
            "novelty": novelty.value if novelty is not None else None,
            "scope": scope,
            "limitations": list(limitations),
        }

    @staticmethod
    def _same_revision(
        finding: InvestigationFinding,
        previous: InvestigationFinding,
        identity: dict[str, Any],
    ) -> bool:
        return (
            finding.investigation_id == previous.investigation_id
            and finding.kind.value == identity["kind"]
            and finding.assessment.value == identity["assessment"]
            and finding.statement == identity["statement"]
            and tuple(str(item) for item in finding.observation_refs)
            == tuple(identity["observation_refs"])
            and finding.claim_refs == tuple(identity["claim_refs"])
            and (finding.novelty.value if finding.novelty is not None else None)
            == identity["novelty"]
            and finding.scope == identity["scope"]
            and finding.limitations == tuple(identity["limitations"])
            and finding.version == previous.version + 1
            and finding.supersedes == previous.id
        )


__all__ = [
    "ClaimKnowledge",
    "ClaimVerdict",
    "FindingDowngradeReason",
    "FindingService",
]
