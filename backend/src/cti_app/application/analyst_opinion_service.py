"""Versioned editorial drafts and review decisions for analyst opinions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID, uuid5

from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    CitationViolation,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    verify_opinion_citations,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    NoveltyClass,
    TLPLevel,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_from_json,
    publication_document_v5_sha256,
)

_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_DECISION_NAMESPACE = UUID("6fd4bf30-7369-4c64-9bf4-d5bd17c67ce2")


_CONFIRMED_VERDICTS = frozenset({"confirmed", "confirmé"})


class OpinionErrorCode(StrEnum):
    OPINION_NOT_FOUND = "opinion_not_found"
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    STALE_VERSION = "stale_version"
    VERSION_HASH_MISMATCH = "version_hash_mismatch"
    CITATIONS_INVALID = "citations_invalid"
    TLP_FORBIDDEN = "tlp_forbidden"
    DO_NOT_SUBMIT = "do_not_submit"
    STALE_BASE = "stale_base"
    BASE_INVALID = "base_publication_invalid"
    BASE_IDENTITY_MISMATCH = "base_identity_mismatch"
    SYNTHESIS_IDENTITY_MISMATCH = "synthesis_identity_mismatch"
    OPINION_NOT_APPROVED = "opinion_not_approved"
    INVALID_SELECTION = "invalid_selection"
    INVALID_STATE = "invalid_opinion_state"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"


class OpinionError(ValueError):
    """Typed refusal for invalid opinion state, evidence, or review operations."""

    def __init__(
        self,
        code: OpinionErrorCode,
        *,
        citation_violations: tuple[CitationViolation, ...] = (),
    ) -> None:
        self.code = code
        self.citation_violations = citation_violations
        super().__init__(code.value)


class ClaimKnowledgeView(Protocol):
    """Small structural view consumed from FindingService without coupling to its module."""

    @property
    def claim_id(self) -> str: ...

    @property
    def claim_statement(self) -> str: ...

    @property
    def verdict(self) -> str: ...

    @property
    def current_findings(self) -> tuple[InvestigationFinding, ...]: ...

    @property
    def favorable_observations(self) -> tuple[EvidenceObservation, ...]: ...

    @property
    def contradictory_observations(self) -> tuple[EvidenceObservation, ...]: ...

    @property
    def qualified_observations(self) -> tuple[EvidenceObservation, ...]: ...

    @property
    def absence_of_evidence(self) -> bool: ...


class ClaimKnowledgeProvider(Protocol):
    async def knowledge_of_claim(
        self, investigation_id: UUID, claim_id: str
    ) -> ClaimKnowledgeView: ...


class FindingServiceClaimKnowledgeProvider:
    """Adapt the public FindingService method to the local claim-knowledge port."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        article_snapshots: ArticleSnapshotPort,
    ) -> None:
        self._uow_factory = uow_factory
        self._article_snapshots = article_snapshots

    async def knowledge_of_claim(self, investigation_id: UUID, claim_id: str) -> ClaimKnowledgeView:
        from cti_app.application.investigation_findings import FindingService

        async with self._uow_factory() as uow:
            service = FindingService(
                findings=uow.investigation_findings,
                observations=uow.evidence_observations,
                article_snapshots=self._article_snapshots,
            )
            return await service.knowledge_of_claim(investigation_id, claim_id)


@dataclass(frozen=True, slots=True)
class OpinionCompositionSelection:
    """Optional finding, claim, and block filters for deterministic composition."""

    finding_ids: tuple[UUID, ...] = ()
    claim_ids: tuple[str, ...] = ()
    block_types: tuple[OpinionBlockType, ...] = tuple(OpinionBlockType)

    def __post_init__(self) -> None:
        if any(not isinstance(item, UUID) for item in self.finding_ids):
            raise TypeError("finding_ids must contain UUID values")
        if any(not item.strip() for item in self.claim_ids):
            raise ValueError("claim_ids must not contain empty values")
        if any(not isinstance(item, OpinionBlockType) for item in self.block_types):
            raise TypeError("block_types must contain OpinionBlockType values")
        object.__setattr__(self, "finding_ids", tuple(dict.fromkeys(self.finding_ids)))
        object.__setattr__(self, "claim_ids", tuple(dict.fromkeys(self.claim_ids)))
        object.__setattr__(self, "block_types", tuple(dict.fromkeys(self.block_types)))


@dataclass(frozen=True, slots=True)
class OpinionRevisionEdits:
    blocks: tuple[OpinionBlock, ...]
    unresolved_points: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "unresolved_points", tuple(self.unresolved_points))
        if any(not isinstance(item, OpinionBlock) for item in self.blocks):
            raise TypeError("Opinion edits must contain OpinionBlock values")


@dataclass(frozen=True, slots=True)
class AnalystOpinionDraftView:
    opinion: AnalystOpinionV1 | None
    history: tuple[AnalystOpinionV1, ...]
    citation_violations: tuple[CitationViolation, ...]


class AnalystOpinionService:
    """Compose, revise, and review immutable opinion versions."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        claim_knowledge_provider: ClaimKnowledgeProvider | None = None,
        author_id: str = "analyst:opinion-composer",
    ) -> None:
        if not author_id.strip():
            raise ValueError("Opinion composer author identity must not be empty")
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._claim_knowledge_provider = claim_knowledge_provider
        self._author_id = author_id

    async def get_draft(self, investigation_id: UUID) -> AnalystOpinionDraftView:
        opinion_id = uuid5(_OPINION_NAMESPACE, str(investigation_id))
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise OpinionError(OpinionErrorCode.INVESTIGATION_NOT_FOUND)
            current = await uow.analyst_opinions.get(opinion_id)
            if current is None:
                return AnalystOpinionDraftView(None, (), ())
            history_items = []
            for version in range(1, current.version + 1):
                item = await uow.analyst_opinions.get(opinion_id, version=version)
                if item is not None:
                    history_items.append(item)
            history = tuple(history_items)
            findings = tuple(
                await uow.investigation_findings.list_for_investigation(investigation_id)
            )
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(investigation_id)
            )
            violations = verify_opinion_citations(current, findings, observations)
            return AnalystOpinionDraftView(current, history, violations)

    async def compose_from_findings(
        self,
        investigation_id: UUID,
        selection: OpinionCompositionSelection | Mapping[str, object] | None = None,
    ) -> AnalystOpinionV1:
        normalized = self._normalize_selection(selection)
        opinion_id = uuid5(_OPINION_NAMESPACE, str(investigation_id))
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise OpinionError(OpinionErrorCode.INVESTIGATION_NOT_FOUND)
            findings = tuple(
                await uow.investigation_findings.list_for_investigation(investigation_id)
            )
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(investigation_id)
            )
            current_findings = self._current_findings(findings)
            if normalized.finding_ids:
                current_by_id = {item.id: item for item in current_findings}
                missing = set(normalized.finding_ids).difference(current_by_id)
                if missing:
                    raise OpinionError(OpinionErrorCode.INVALID_SELECTION)
                selected = tuple(current_by_id[item] for item in normalized.finding_ids)
            else:
                selected = current_findings
            current = await uow.analyst_opinions.get(opinion_id)
            base_artifact, base_sha256, synthesis_sha256 = await self._resolve_base(
                uow, investigation
            )
            if current is not None and (
                current.base_publication_artifact_id != base_artifact.id
                or current.base_publication_sha256 != base_sha256
                or current.synthesis_sha256 != synthesis_sha256
            ):
                raise OpinionError(OpinionErrorCode.STALE_BASE)

            blocks, unresolved = await self._compose_blocks(
                investigation_id=investigation_id,
                findings=selected,
                observations=observations,
                selection=normalized,
            )
            next_version = (current.version if current else 0) + 1
            opinion = AnalystOpinionV1(
                id=opinion_id,
                version=next_version,
                investigation_id=investigation_id,
                synthesis_artifact_id=investigation.synthesis_artifact_id,
                synthesis_sha256=synthesis_sha256,
                base_publication_artifact_id=base_artifact.id,
                base_publication_sha256=base_sha256,
                blocks=blocks,
                unresolved_points=unresolved,
                author_id=current.author_id if current is not None else self._author_id,
                status=AnalystOpinionStatus.DRAFT,
            )
            if current is not None and (
                current.status is AnalystOpinionStatus.DRAFT
                and current.blocks == opinion.blocks
                and current.unresolved_points == opinion.unresolved_points
            ):
                return current
            violations = verify_opinion_citations(opinion, findings, observations)
            if violations:
                raise OpinionError(
                    OpinionErrorCode.CITATIONS_INVALID,
                    citation_violations=violations,
                )
            appended = await uow.analyst_opinions.append_version(
                opinion, expected_version=current.version if current else 0
            )
            await uow.commit()
            return appended

    async def revise(
        self,
        opinion_id: UUID,
        expected_version: int,
        edits: OpinionRevisionEdits | Iterable[OpinionBlock],
        actor: str,
    ) -> AnalystOpinionV1:
        actor_id = self._require_actor(actor)
        revision = (
            edits if isinstance(edits, OpinionRevisionEdits) else OpinionRevisionEdits(tuple(edits))
        )
        async with self._uow_factory() as uow:
            current = await self._require_current(uow, opinion_id)
            if current.version != expected_version:
                replay = await uow.analyst_opinions.get(opinion_id, version=expected_version + 1)
                if (
                    replay is not None
                    and replay.status is AnalystOpinionStatus.DRAFT
                    and replay.author_id == actor_id
                    and replay.blocks == revision.blocks
                    and replay.unresolved_points == revision.unresolved_points
                ):
                    return replay
                raise OpinionError(OpinionErrorCode.STALE_VERSION)
            if current.status is AnalystOpinionStatus.APPROVED:
                raise OpinionError(OpinionErrorCode.INVALID_STATE)
            candidate = AnalystOpinionV1(
                id=current.id,
                version=current.version + 1,
                investigation_id=current.investigation_id,
                synthesis_artifact_id=current.synthesis_artifact_id,
                synthesis_sha256=current.synthesis_sha256,
                base_publication_artifact_id=current.base_publication_artifact_id,
                base_publication_sha256=current.base_publication_sha256,
                blocks=revision.blocks,
                unresolved_points=revision.unresolved_points,
                author_id=actor_id,
                status=AnalystOpinionStatus.DRAFT,
            )
            await self._validate_citations(uow, candidate)
            try:
                result = await uow.analyst_opinions.append_version(
                    candidate, expected_version=expected_version
                )
            except Exception as exc:
                concurrent = await uow.analyst_opinions.get(opinion_id)
                if (
                    concurrent is not None
                    and concurrent.status is AnalystOpinionStatus.DRAFT
                    and concurrent.version == candidate.version
                    and concurrent.author_id == actor_id
                    and concurrent.blocks == candidate.blocks
                    and concurrent.unresolved_points == candidate.unresolved_points
                ):
                    return concurrent
                if type(exc).__name__ in {"VersionConflictError", "IntegrityError"}:
                    raise OpinionError(OpinionErrorCode.STALE_VERSION) from exc
                raise
            await uow.commit()
            return result

    async def approve(
        self,
        opinion_id: UUID,
        version: int,
        expected_sha256: str,
        actor: str,
        reason: str,
        *,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> AnalystOpinionV1:
        actor_id = self._require_actor(actor)
        self._require_reason(reason)
        async with self._uow_factory() as uow:
            reviewed = await self._require_version(uow, opinion_id, version)
            self._require_expected_sha(reviewed, expected_sha256)
            current = await self._require_current(uow, opinion_id)
            if current.status is AnalystOpinionStatus.APPROVED:
                if (
                    current.version == version + 1
                    and current.reviewer_id == actor_id
                    and self._same_opinion_text(current, reviewed)
                ):
                    return current
                raise OpinionError(OpinionErrorCode.STALE_VERSION)
            if current.version != version or current.status is not AnalystOpinionStatus.DRAFT:
                raise OpinionError(OpinionErrorCode.STALE_VERSION)
            await self._validate_review_preconditions(uow, reviewed)
            approved = AnalystOpinionV1(
                id=reviewed.id,
                version=reviewed.version + 1,
                investigation_id=reviewed.investigation_id,
                synthesis_artifact_id=reviewed.synthesis_artifact_id,
                synthesis_sha256=reviewed.synthesis_sha256,
                base_publication_artifact_id=reviewed.base_publication_artifact_id,
                base_publication_sha256=reviewed.base_publication_sha256,
                blocks=reviewed.blocks,
                unresolved_points=reviewed.unresolved_points,
                author_id=reviewed.author_id,
                reviewer_id=actor_id,
                status=AnalystOpinionStatus.APPROVED,
            )
            await self._validate_citations(uow, approved)
            try:
                saved = await uow.analyst_opinions.append_version(
                    approved, expected_version=version
                )
            except Exception as exc:
                concurrent = await uow.analyst_opinions.get(opinion_id)
                if (
                    concurrent is not None
                    and concurrent.version == approved.version
                    and concurrent.status is AnalystOpinionStatus.APPROVED
                    and concurrent.reviewer_id == actor_id
                    and self._same_opinion_text(concurrent, reviewed)
                ):
                    return concurrent
                if type(exc).__name__ in {"VersionConflictError", "IntegrityError"}:
                    raise OpinionError(OpinionErrorCode.STALE_VERSION) from exc
                raise
            await self._append_decision(
                uow,
                opinion=saved,
                decision_type=AnalystDecisionType.NOTE_APPROVE,
                actor=actor_id,
                reason=reason,
                reviewed_version=version,
                reviewed_sha256=expected_sha256,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return saved

    async def request_changes(
        self,
        opinion_id: UUID,
        version: int,
        expected_sha256: str,
        actor: str,
        reason: str,
        *,
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> AnalystOpinionV1:
        actor_id = self._require_actor(actor)
        self._require_reason(reason)
        async with self._uow_factory() as uow:
            reviewed = await self._require_version(uow, opinion_id, version)
            self._require_expected_sha(reviewed, expected_sha256)
            current = await self._require_current(uow, opinion_id)
            if current.status is AnalystOpinionStatus.REJECTED and current.version == version + 1:
                if current.reviewer_id != actor_id or not self._same_opinion_text(
                    current, reviewed
                ):
                    raise OpinionError(OpinionErrorCode.STALE_VERSION)
                return current
            if current.version != version or current.status is not AnalystOpinionStatus.DRAFT:
                raise OpinionError(OpinionErrorCode.STALE_VERSION)
            await self._validate_citations(uow, reviewed)
            rejected = AnalystOpinionV1(
                id=reviewed.id,
                version=reviewed.version + 1,
                investigation_id=reviewed.investigation_id,
                synthesis_artifact_id=reviewed.synthesis_artifact_id,
                synthesis_sha256=reviewed.synthesis_sha256,
                base_publication_artifact_id=reviewed.base_publication_artifact_id,
                base_publication_sha256=reviewed.base_publication_sha256,
                blocks=reviewed.blocks,
                unresolved_points=reviewed.unresolved_points,
                author_id=reviewed.author_id,
                reviewer_id=actor_id,
                status=AnalystOpinionStatus.REJECTED,
            )
            saved = await uow.analyst_opinions.append_version(rejected, expected_version=version)
            await self._append_decision(
                uow,
                opinion=saved,
                decision_type=AnalystDecisionType.NOTE_CHANGES_REQUESTED,
                actor=actor_id,
                reason=reason,
                reviewed_version=version,
                reviewed_sha256=expected_sha256,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
            await uow.commit()
            return saved

    async def _compose_blocks(
        self,
        *,
        investigation_id: UUID,
        findings: tuple[InvestigationFinding, ...],
        observations: tuple[EvidenceObservation, ...],
        selection: OpinionCompositionSelection,
    ) -> tuple[tuple[OpinionBlock, ...], tuple[str, ...]]:
        allowed_observations = {
            item.id: item for item in observations if item.investigation_id == investigation_id
        }
        supported = tuple(
            item
            for item in findings
            if item.assessment is FindingAssessment.SUPPORTED
            and item.observation_refs
            and all(ref in allowed_observations for ref in item.observation_refs)
        )
        uncertain = tuple(item for item in findings if item not in supported)
        blocks: list[OpinionBlock] = []
        unresolved: list[str] = []

        for block_type in selection.block_types:
            if block_type is OpinionBlockType.CLAIM_VERIFICATION:
                claim_block, missing = await self._claim_verification_block(
                    investigation_id=investigation_id,
                    findings=findings,
                    observations=allowed_observations,
                    claim_ids=selection.claim_ids,
                )
                if claim_block is not None:
                    blocks.append(claim_block)
                unresolved.extend(missing)
                continue
            if block_type is OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS:
                if uncertain:
                    blocks.append(
                        self._finding_block(
                            block_type,
                            uncertain,
                            allowed_observations,
                            qualification=OpinionClaimQualification.HYPOTHESIS,
                        )
                    )
                continue
            candidates = tuple(
                finding
                for finding in supported
                if block_type is OpinionBlockType.KEY_FINDINGS
                or self._block_for_finding(finding) is block_type
            )
            if candidates:
                blocks.append(
                    self._finding_block(
                        block_type,
                        candidates,
                        allowed_observations,
                        qualification=OpinionClaimQualification.FACT,
                    )
                )
        for finding in findings:
            if finding not in supported:
                if finding.limitations:
                    unresolved.extend(finding.limitations)
                elif finding.assessment is not FindingAssessment.SUPPORTED:
                    unresolved.append(
                        f"À vérifier : {finding.statement} ({finding.assessment.value})."
                    )
                elif any(ref not in allowed_observations for ref in finding.observation_refs):
                    unresolved.append(
                        f"Références d'observation manquantes pour : {finding.statement}."
                    )
        if self._claim_knowledge_provider is None and (
            selection.claim_ids or any(finding.claim_refs for finding in findings)
        ):
            unresolved.append(
                "La vérification des affirmations est indisponible pour cette composition."
            )
        return tuple(blocks), tuple(dict.fromkeys(item for item in unresolved if item.strip()))

    async def _claim_verification_block(
        self,
        *,
        investigation_id: UUID,
        findings: tuple[InvestigationFinding, ...],
        observations: Mapping[UUID, EvidenceObservation],
        claim_ids: tuple[str, ...],
    ) -> tuple[OpinionBlock | None, tuple[str, ...]]:
        provider = self._claim_knowledge_provider
        if provider is None:
            return None, ()
        claims = claim_ids or tuple(
            dict.fromkeys(ref for item in findings for ref in item.claim_refs)
        )
        if not claims:
            return None, ()
        lines: list[str] = []
        finding_refs: list[UUID] = []
        evidence_refs: list[UUID] = []
        all_confirmed = True
        missing: list[str] = []
        for claim_id in claims:
            try:
                knowledge = await provider.knowledge_of_claim(investigation_id, claim_id)
            except (ValueError, LookupError):
                missing.append(f"Affirmation {claim_id} absente du snapshot de l'article.")
                continue
            verdict = getattr(knowledge.verdict, "value", str(knowledge.verdict))
            if verdict in _CONFIRMED_VERDICTS:
                eligible = tuple(
                    item
                    for item in knowledge.current_findings
                    if item.assessment is FindingAssessment.SUPPORTED
                    and item.observation_refs
                    and all(ref in observations for ref in item.observation_refs)
                )
                if not eligible:
                    all_confirmed = False
                    lines.append(
                        f"{claim_id} — {knowledge.claim_statement}: éléments insuffisants "
                        "pour confirmer."
                    )
                    continue
                finding_refs.extend(item.id for item in eligible)
                evidence_refs.extend(
                    ref for item in eligible for ref in item.observation_refs if ref in observations
                )
                lines.append(
                    f"{claim_id} — {knowledge.claim_statement}: confirmé par les findings cités."
                )
            else:
                all_confirmed = False
                lines.append(
                    f"{claim_id} — {knowledge.claim_statement}: "
                    f"{self._claim_verdict_text(verdict)}."
                )
                for finding in knowledge.current_findings:
                    finding_refs.append(finding.id)
                    evidence_refs.extend(
                        ref for ref in finding.observation_refs if ref in observations
                    )
                for item in (
                    *knowledge.favorable_observations,
                    *knowledge.contradictory_observations,
                    *knowledge.qualified_observations,
                ):
                    if item.id in observations:
                        evidence_refs.append(item.id)
        if not lines:
            return None, tuple(missing)
        return (
            OpinionBlock(
                block_type=OpinionBlockType.CLAIM_VERIFICATION,
                text="\n".join(lines),
                finding_refs=tuple(dict.fromkeys(finding_refs)),
                evidence_refs=tuple(dict.fromkeys(evidence_refs)),
                qualification=(
                    OpinionClaimQualification.FACT
                    if all_confirmed and finding_refs
                    else OpinionClaimQualification.INFERENCE
                ),
                certainty=0.8 if all_confirmed else 0.55,
            ),
            tuple(missing),
        )

    @staticmethod
    def _claim_verdict_text(verdict: str) -> str:
        # `ClaimVerdict` (investigation_findings) uses the editorial French values;
        # the English identifiers are accepted for provider fakes.
        return {
            "contradicted": "les éléments cités contredisent l'affirmation",
            "contredit": "les éléments cités contredisent l'affirmation",
            "qualified": "l'affirmation doit être nuancée",
            "nuancé": "l'affirmation doit être nuancée",
            "not_established": "l'affirmation n'est pas établie",
            "non établi": "l'affirmation n'est pas établie",
        }.get(verdict, "le verdict reste indéterminé")

    @staticmethod
    def _finding_block(
        block_type: OpinionBlockType,
        findings: tuple[InvestigationFinding, ...],
        observations: Mapping[UUID, EvidenceObservation],
        *,
        qualification: OpinionClaimQualification,
    ) -> OpinionBlock:
        lines: list[str] = []
        for finding in findings:
            line = f"• {finding.statement}"
            if block_type is OpinionBlockType.ADDITIONAL_SAMPLES:
                novelty = finding.novelty or NoveltyClass.NOT_CONFIRMED
                line += f"\n  Nouveauté relative à l'article : {novelty.value}."
            if finding.limitations:
                line += f"\n  Réserves : {'; '.join(finding.limitations)}."
            lines.append(line)
        refs = tuple(dict.fromkeys(ref for item in findings for ref in item.observation_refs))
        return OpinionBlock(
            block_type=block_type,
            text="\n".join(lines),
            finding_refs=tuple(item.id for item in findings),
            evidence_refs=tuple(ref for ref in refs if ref in observations),
            qualification=qualification,
            certainty=0.8 if qualification is OpinionClaimQualification.FACT else 0.45,
        )

    @staticmethod
    def _block_for_finding(finding: InvestigationFinding) -> OpinionBlockType:
        return {
            FindingKind.NEW_SAMPLE: OpinionBlockType.ADDITIONAL_SAMPLES,
            FindingKind.INFECTION_CHAIN: OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
            FindingKind.CAPABILITY: OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES,
            FindingKind.NETWORK_BEHAVIOUR: OpinionBlockType.NETWORK_AND_INFRASTRUCTURE,
            FindingKind.CLAIM_VERIFICATION: OpinionBlockType.CLAIM_VERIFICATION,
            FindingKind.DETECTION: OpinionBlockType.PROPOSED_DETECTIONS,
        }[finding.kind]

    async def _validate_review_preconditions(
        self, uow: UnitOfWork, opinion: AnalystOpinionV1
    ) -> None:
        await self._validate_citations(uow, opinion)
        investigation = await uow.analyst_investigations.get(opinion.investigation_id)
        if investigation is None:
            raise OpinionError(OpinionErrorCode.INVESTIGATION_NOT_FOUND)
        await self._validate_policy(uow, opinion.investigation_id)
        base_artifact, base_sha256, synthesis_sha256 = await self._resolve_base(uow, investigation)
        if (
            base_artifact.id != opinion.base_publication_artifact_id
            or base_sha256 != opinion.base_publication_sha256
        ):
            raise OpinionError(OpinionErrorCode.STALE_BASE)
        if (
            investigation.synthesis_artifact_id != opinion.synthesis_artifact_id
            or synthesis_sha256 != opinion.synthesis_sha256
        ):
            raise OpinionError(OpinionErrorCode.SYNTHESIS_IDENTITY_MISMATCH)

    async def _validate_policy(self, uow: UnitOfWork, investigation_id: UUID) -> None:
        envelope = await uow.investigation_envelopes.get(investigation_id)
        if envelope is None:
            raise OpinionError(OpinionErrorCode.TLP_FORBIDDEN)
        if envelope.disclosure.do_not_submit:
            raise OpinionError(OpinionErrorCode.DO_NOT_SUBMIT)
        if envelope.disclosure.tlp is TLPLevel.RED:
            raise OpinionError(OpinionErrorCode.TLP_FORBIDDEN)

    async def _validate_citations(self, uow: UnitOfWork, opinion: AnalystOpinionV1) -> None:
        findings = tuple(
            await uow.investigation_findings.list_for_investigation(opinion.investigation_id)
        )
        observations = tuple(
            await uow.evidence_observations.list_for_investigation(opinion.investigation_id)
        )
        violations = verify_opinion_citations(opinion, findings, observations)
        finding_by_id = {item.id: item for item in findings}
        observation_by_id = {item.id: item for item in observations}
        foreign = any(
            (finding := finding_by_id.get(ref)) is not None
            and finding.investigation_id != opinion.investigation_id
            for block in opinion.blocks
            for ref in block.finding_refs
        ) or any(
            (observation := observation_by_id.get(ref)) is not None
            and observation.investigation_id != opinion.investigation_id
            for block in opinion.blocks
            for ref in block.evidence_refs
        )
        if violations or foreign:
            raise OpinionError(
                OpinionErrorCode.CITATIONS_INVALID,
                citation_violations=violations,
            )

    async def _resolve_base(
        self, uow: UnitOfWork, investigation: AnalystInvestigation
    ) -> tuple[ProductionArtifact, str, str]:
        base = await uow.production_artifacts.get_current_for_revision(
            investigation.production_run_id,
            ProductionArtifactStage.PUBLICATION.value,
        )
        synthesis = await uow.production_artifacts.get(investigation.synthesis_artifact_id)
        if (
            base is None
            or base.stage is not ProductionArtifactStage.PUBLICATION
            or base.status is not ProductionArtifactStatus.VERIFIED
            or base.production_run_id != investigation.production_run_id
            or base.subject_id != investigation.subject_id
            or base.canonical_blob_id is None
            or synthesis is None
            or synthesis.stage is not ProductionArtifactStage.SYNTHESIS
            or synthesis.status is not ProductionArtifactStatus.VERIFIED
            or synthesis.production_run_id != investigation.production_run_id
            or synthesis.canonical_blob_id is None
        ):
            raise OpinionError(OpinionErrorCode.BASE_INVALID)
        base_bytes = await self._artifact_store.read_bytes(base.canonical_blob_id)
        synthesis_bytes = await self._artifact_store.read_bytes(synthesis.canonical_blob_id)
        base_sha256 = hashlib.sha256(base_bytes).hexdigest()
        synthesis_sha256 = hashlib.sha256(synthesis_bytes).hexdigest()
        try:
            document = publication_document_from_json(json.loads(base_bytes))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OpinionError(OpinionErrorCode.BASE_INVALID) from exc
        if (
            not isinstance(document, PublicationDocumentV5)
            or publication_document_v5_sha256(document) != base_sha256
        ):
            raise OpinionError(OpinionErrorCode.BASE_INVALID)
        return base, base_sha256, synthesis_sha256

    async def _append_decision(
        self,
        uow: UnitOfWork,
        *,
        opinion: AnalystOpinionV1,
        decision_type: AnalystDecisionType,
        actor: str,
        reason: str,
        reviewed_version: int,
        reviewed_sha256: str,
        correlation_id: str,
        idempotency_key: str,
    ) -> None:
        operation = idempotency_key.strip() or (
            f"{decision_type.value}:{opinion.id}:{reviewed_version}:{reviewed_sha256}:{actor}"
        )
        decision_id = uuid5(
            _DECISION_NAMESPACE,
            f"{opinion.investigation_id}:{opinion.id}:{decision_type.value}:{operation}",
        )
        existing = await uow.analyst_decisions.list_for_investigation(opinion.investigation_id)
        prior = next((item for item in existing if item.id == decision_id), None)
        expected_correlation = correlation_id.strip() or operation
        if prior is not None:
            if (
                prior.target_id != opinion.id
                or prior.decision_type is not decision_type
                or prior.actor_id != actor
                or prior.reason != reason
            ):
                raise OpinionError(OpinionErrorCode.IDEMPOTENCY_CONFLICT)
            return
        await uow.analyst_decisions.append(
            AnalystDecision(
                id=decision_id,
                investigation_id=opinion.investigation_id,
                decision_type=decision_type,
                target_type=AnalystDecisionTargetType.NOTE,
                target_id=opinion.id,
                actor_id=actor,
                reason=reason,
                correlation_id=expected_correlation[:128],
            )
        )

    async def _require_current(self, uow: UnitOfWork, opinion_id: UUID) -> AnalystOpinionV1:
        opinion = await uow.analyst_opinions.get(opinion_id)
        if opinion is None:
            raise OpinionError(OpinionErrorCode.OPINION_NOT_FOUND)
        return opinion

    async def _require_version(
        self, uow: UnitOfWork, opinion_id: UUID, version: int
    ) -> AnalystOpinionV1:
        opinion = await uow.analyst_opinions.get(opinion_id, version=version)
        if opinion is None:
            raise OpinionError(OpinionErrorCode.OPINION_NOT_FOUND)
        return opinion

    @staticmethod
    def _require_expected_sha(opinion: AnalystOpinionV1, expected_sha256: str) -> None:
        if opinion.content_sha256 != expected_sha256:
            raise OpinionError(OpinionErrorCode.VERSION_HASH_MISMATCH)

    @staticmethod
    def _require_actor(actor: str) -> str:
        if not actor.strip():
            raise OpinionError(OpinionErrorCode.INVALID_STATE)
        return actor.strip()

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not reason.strip():
            raise OpinionError(OpinionErrorCode.INVALID_STATE)

    @staticmethod
    def _same_opinion_text(left: AnalystOpinionV1, right: AnalystOpinionV1) -> bool:
        return (
            left.investigation_id == right.investigation_id
            and left.synthesis_artifact_id == right.synthesis_artifact_id
            and left.synthesis_sha256 == right.synthesis_sha256
            and left.base_publication_artifact_id == right.base_publication_artifact_id
            and left.base_publication_sha256 == right.base_publication_sha256
            and left.blocks == right.blocks
            and left.unresolved_points == right.unresolved_points
            and left.author_id == right.author_id
        )

    @staticmethod
    def _current_findings(
        findings: tuple[InvestigationFinding, ...],
    ) -> tuple[InvestigationFinding, ...]:
        superseded = {item.supersedes for item in findings if item.supersedes is not None}
        return tuple(item for item in findings if item.id not in superseded)

    @staticmethod
    def _normalize_selection(
        value: OpinionCompositionSelection | Mapping[str, object] | None,
    ) -> OpinionCompositionSelection:
        if value is None:
            return OpinionCompositionSelection()
        if isinstance(value, OpinionCompositionSelection):
            return value
        try:
            finding_ids = tuple(
                UUID(str(item)) for item in cast(Iterable[object], value.get("finding_ids", ()))
            )
            claim_ids = tuple(
                str(item) for item in cast(Iterable[object], value.get("claim_ids", ()))
            )
            raw_blocks = cast(Iterable[object], value.get("block_types", tuple(OpinionBlockType)))
            block_types = tuple(
                item if isinstance(item, OpinionBlockType) else OpinionBlockType(str(item))
                for item in raw_blocks
            )
            return OpinionCompositionSelection(finding_ids, claim_ids, block_types)
        except (TypeError, ValueError) as exc:
            raise OpinionError(OpinionErrorCode.INVALID_SELECTION) from exc


__all__ = [
    "AnalystOpinionDraftView",
    "AnalystOpinionService",
    "ClaimKnowledgeProvider",
    "FindingServiceClaimKnowledgeProvider",
    "OpinionCompositionSelection",
    "OpinionError",
    "OpinionErrorCode",
    "OpinionRevisionEdits",
]
