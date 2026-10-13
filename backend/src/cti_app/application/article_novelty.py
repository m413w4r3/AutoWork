"""Article-relative novelty classification for investigation candidates."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.application.evidence_recorder import sanitize_evidence_text
from cti_app.application.investigation_persistence import EvidenceObservationRepository
from cti_app.application.persistence import AnalystInvestigationRepository
from cti_app.application.research_context_builder import ArticleSnapshot, ArticleSnapshotPort
from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    NoveltyClass,
)

_HASH = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])")
_SCOPE = (
    "Comparaison limitée au snapshot de cet article/corpus ; une absence ici ne démontre "
    "pas une absence mondiale."
)


class NoveltyCandidateKind(StrEnum):
    SAMPLE = "sample"
    IOC = "ioc"
    BEHAVIOUR = "behaviour"
    CLAIM = "claim"


class NoveltyReasonCode(StrEnum):
    NEW_ARTICLE_ITEM = "new_article_item"
    ALREADY_DOCUMENTED = "already_documented"
    NEW_DETAIL = "new_detail"
    CLAIM_CORRECTION = "claim_correction"
    CLAIM_SUPPORTED = "claim_supported"
    CLAIM_UNRESOLVED = "claim_unresolved"
    EVIDENCE_REQUIRED = "evidence_required"
    INVESTIGATION_MISMATCH = "investigation_mismatch"


@dataclass(frozen=True, slots=True)
class NoveltyCandidate:
    investigation_id: UUID
    kind: NoveltyCandidateKind
    value: str
    object_type: str
    object_id: str
    details: Mapping[str, Any] = field(default_factory=dict)
    claim_id: str | None = None
    claim_assessment: FindingAssessment | None = None
    observation_refs: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, NoveltyCandidateKind):
            raise TypeError("Novelty candidate kind must use NoveltyCandidateKind")
        if not self.value.strip() or not self.object_type.strip() or not self.object_id.strip():
            raise ValueError("Novelty candidate value and object identity must not be empty")
        if self.claim_assessment is not None and not isinstance(
            self.claim_assessment, FindingAssessment
        ):
            raise TypeError("Claim assessment must use FindingAssessment")
        object.__setattr__(self, "observation_refs", tuple(self.observation_refs))


@dataclass(frozen=True, slots=True)
class NoveltyReason:
    code: NoveltyReasonCode
    summary: str
    matched_article_identifiers: tuple[str, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()
    limitations: tuple[str, ...] = (_SCOPE,)


@dataclass(frozen=True, slots=True)
class NoveltyClassification:
    novelty: NoveltyClass
    reason: NoveltyReason
    compared_ids: tuple[str, ...]
    scope: str = _SCOPE
    limitations: tuple[str, ...] = (_SCOPE,)


class ArticleNoveltyService:
    """Compare a candidate only with the snapshot linked to its investigation."""

    def __init__(
        self,
        *,
        observations: EvidenceObservationRepository,
        investigations: AnalystInvestigationRepository,
        article_snapshots: ArticleSnapshotPort,
    ) -> None:
        self._observations = observations
        self._investigations = investigations
        self._article_snapshots = article_snapshots

    async def classify(
        self,
        *,
        investigation_id: UUID,
        candidate: NoveltyCandidate,
    ) -> NoveltyClassification:
        if candidate.investigation_id != investigation_id:
            raise ValueError("Novelty candidate belongs to another investigation")
        investigation = await self._investigations.get(investigation_id)
        if investigation is None:
            raise ValueError("Investigation does not exist")
        snapshot = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        evidence = await self._observations_for_candidate(investigation_id, candidate)
        evidence_refs = tuple(item.id for item in evidence)
        compared_ids = [
            f"article:{snapshot.synthesis_artifact_id}",
            f"{candidate.kind.value}:{sanitize_evidence_text(candidate.object_id, max_chars=256)}",
        ]
        compared_ids.extend(f"evidence:{ref}" for ref in evidence_refs)

        if candidate.kind is NoveltyCandidateKind.CLAIM:
            return self._classify_claim(snapshot, candidate, evidence_refs, tuple(compared_ids))

        article_strings = _article_strings(snapshot)
        match = _find_match(candidate, snapshot, article_strings)
        if match is not None:
            has_new_detail = _has_new_detail(candidate.details, article_strings)
            if evidence_refs and has_new_detail:
                return _result(
                    NoveltyClass.ALREADY_DOCUMENTED_BUT_NEW_DETAIL,
                    NoveltyReasonCode.NEW_DETAIL,
                    "L'objet figure deja dans l'article ; les observations apportent "
                    "un detail absent du snapshot.",
                    compared_ids,
                    evidence_refs,
                    (match,),
                )
            return _result(
                NoveltyClass.NOT_CONFIRMED,
                NoveltyReasonCode.ALREADY_DOCUMENTED,
                "L'objet est deja documente dans l'article ; aucune nouveaute "
                "distincte n'est etablie.",
                compared_ids,
                evidence_refs,
                (match,),
            )

        if not evidence_refs:
            return _result(
                NoveltyClass.NOT_CONFIRMED,
                NoveltyReasonCode.EVIDENCE_REQUIRED,
                "L'absence dans le snapshot est constatee, mais le candidat n'a pas d'observation "
                "verifiee.",
                compared_ids,
                evidence_refs,
            )
        return _result(
            NoveltyClass.NEW_TO_ARTICLE,
            NoveltyReasonCode.NEW_ARTICLE_ITEM,
            "Le candidat est etaye et n'apparait pas dans le snapshot de cet article.",
            compared_ids,
            evidence_refs,
        )

    async def _observations_for_candidate(
        self, investigation_id: UUID, candidate: NoveltyCandidate
    ) -> tuple[EvidenceObservation, ...]:
        result: list[EvidenceObservation] = []
        for ref in candidate.observation_refs:
            observation = await self._observations.get(ref)
            if observation is None:
                raise ValueError(f"Novelty evidence observation {ref} does not exist")
            if observation.investigation_id != investigation_id:
                raise ValueError(
                    f"Novelty evidence observation {ref} belongs to another investigation"
                )
            if ref not in {item.id for item in result}:
                result.append(observation)
        return tuple(result)

    def _classify_claim(
        self,
        snapshot: ArticleSnapshot,
        candidate: NoveltyCandidate,
        evidence_refs: tuple[UUID, ...],
        compared_ids: tuple[str, ...],
    ) -> NoveltyClassification:
        if candidate.claim_id is None:
            raise ValueError("Claim candidates require a claim_id")
        claim = next(
            (item for item in snapshot.article.claims if item.id == candidate.claim_id),
            None,
        )
        if claim is None:
            raise ValueError("Claim candidate is not in the selected article snapshot")
        claim_compared_ids = (*compared_ids, f"claim:{claim.id}")
        if not evidence_refs:
            return _result(
                NoveltyClass.NOT_CONFIRMED,
                NoveltyReasonCode.EVIDENCE_REQUIRED,
                "Une affirmation de l'article ne peut pas etre corrigee sans observation de cette "
                "enquete.",
                claim_compared_ids,
                evidence_refs,
                (claim.id,),
            )
        if candidate.claim_assessment is FindingAssessment.REFUTED:
            return _result(
                NoveltyClass.CLAIM_CORRECTION,
                NoveltyReasonCode.CLAIM_CORRECTION,
                "Une observation de cette enquete contredit l'affirmation citee ; "
                "la correction reste revisable.",
                claim_compared_ids,
                evidence_refs,
                (claim.id,),
            )
        if candidate.claim_assessment is FindingAssessment.QUALIFIED:
            return _result(
                NoveltyClass.ALREADY_DOCUMENTED_BUT_NEW_DETAIL,
                NoveltyReasonCode.NEW_DETAIL,
                "Les observations nuancent une affirmation deja presente dans l'article.",
                claim_compared_ids,
                evidence_refs,
                (claim.id,),
            )
        code = (
            NoveltyReasonCode.CLAIM_SUPPORTED
            if candidate.claim_assessment is FindingAssessment.SUPPORTED
            else NoveltyReasonCode.CLAIM_UNRESOLVED
        )
        summary = (
            "Les observations etayent l'affirmation existante ; aucune correction n'est proposee."
            if code is NoveltyReasonCode.CLAIM_SUPPORTED
            else "Les observations ne permettent pas de confirmer une correction de l'affirmation."
        )
        return _result(
            NoveltyClass.NOT_CONFIRMED,
            code,
            summary,
            claim_compared_ids,
            evidence_refs,
            (claim.id,),
        )


def _result(
    novelty: NoveltyClass,
    code: NoveltyReasonCode,
    summary: str,
    compared_ids: tuple[str, ...] | list[str],
    evidence_refs: tuple[UUID, ...],
    matched: tuple[str, ...] = (),
) -> NoveltyClassification:
    return NoveltyClassification(
        novelty=novelty,
        reason=NoveltyReason(
            code=code,
            summary=summary,
            matched_article_identifiers=matched,
            evidence_refs=evidence_refs,
        ),
        compared_ids=tuple(dict.fromkeys(compared_ids)),
    )


def _find_match(
    candidate: NoveltyCandidate,
    snapshot: ArticleSnapshot,
    article_strings: tuple[str, ...],
) -> str | None:
    if candidate.kind is NoveltyCandidateKind.SAMPLE:
        candidate_hash = _hash_identity(candidate.value)
        if candidate_hash is not None:
            for documented in snapshot.article.documented_samples:
                if _hash_identity(documented) == candidate_hash:
                    return f"sample:{candidate_hash[0]}:{candidate_hash[1]}"
            for documented in snapshot.narrative_hashes:
                if _hash_identity(documented) == candidate_hash:
                    return f"narrative_hash:{candidate_hash[0]}:{candidate_hash[1]}"
            # A hash in another family is not a match, even if its characters overlap.
            return None
        normalized = candidate.value.strip().casefold()
        for documented in snapshot.article.documented_samples:
            if documented.strip().casefold() == normalized:
                return f"sample:{sanitize_evidence_text(documented, max_chars=128)}"
        return None

    if candidate.kind is NoveltyCandidateKind.IOC:
        candidate_hash = _hash_identity(candidate.value)
        if candidate_hash is not None:
            for value in article_strings:
                for match in _HASH.finditer(value):
                    identity = _hash_identity(match.group(0))
                    if identity == candidate_hash:
                        return f"hash:{identity[0]}:{identity[1]}"
            return None
        normalized = _normalize_ioc(candidate.object_type, candidate.value)
        for value in article_strings:
            if _normalize_ioc(candidate.object_type, value) == normalized:
                return f"ioc:{candidate.object_type}:{sanitize_evidence_text(value, max_chars=256)}"
        return None

    normalized = candidate.value.strip().casefold()
    for value in article_strings:
        if value.strip().casefold() == normalized:
            return f"behaviour:{sanitize_evidence_text(value, max_chars=256)}"
    return None


def _article_strings(snapshot: ArticleSnapshot) -> tuple[str, ...]:
    values: list[str] = [
        snapshot.article.short_summary,
        *snapshot.article.documented_samples,
        *snapshot.narrative_hashes,
        *snapshot.article.open_questions,
    ]
    for claim in snapshot.article.claims:
        values.extend((claim.id, claim.statement, *claim.source_refs))
    for entry in snapshot.article.known_chain:
        values.extend(_walk_strings(entry))
    return tuple(values)


def _walk_strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Mapping):
        strings: list[str] = []
        for key, item in value.items():
            strings.extend(_walk_strings(str(key)))
            strings.extend(_walk_strings(item))
        return tuple(strings)
    if isinstance(value, tuple | list):
        return tuple(string for item in value for string in _walk_strings(item))
    return ()


def _hash_identity(value: str) -> tuple[str, str] | None:
    candidate = value.strip().lower()
    if len(candidate) == 32 and re.fullmatch(r"[0-9a-f]{32}", candidate):
        return "md5", candidate
    if len(candidate) == 40 and re.fullmatch(r"[0-9a-f]{40}", candidate):
        return "sha1", candidate
    if len(candidate) == 64 and re.fullmatch(r"[0-9a-f]{64}", candidate):
        return "sha256", candidate
    return None


def _normalize_ioc(object_type: str, value: str) -> str:
    normalized = value.strip().strip(".,;()[]{}<>\"'")
    object_type = object_type.casefold()
    if object_type in {"ip", "ip_address", "ipv4", "ipv6"}:
        try:
            return str(ipaddress.ip_address(normalized.strip("[]")))
        except ValueError:
            return normalized.casefold()
    if object_type in {"domain", "hostname", "fqdn"}:
        return normalized.rstrip(".").casefold()
    return normalized.casefold()


def _has_new_detail(details: Mapping[str, Any], article_strings: tuple[str, ...]) -> bool:
    candidates = _walk_strings(details)
    article_values = {value.strip().casefold() for value in article_strings}
    return any(
        value.strip() and value.strip().casefold() not in article_values for value in candidates
    )


__all__ = [
    "ArticleNoveltyService",
    "NoveltyCandidate",
    "NoveltyCandidateKind",
    "NoveltyClassification",
    "NoveltyReason",
    "NoveltyReasonCode",
]
