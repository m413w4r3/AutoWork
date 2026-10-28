"""Versioned editorial drafts and review decisions for analyst opinions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID, uuid5

from pydantic import ValidationError

from cti_app.application.analyst_opinion_inputs import ImplantMaterialEdit
from cti_app.application.investigation_contracts import (
    OperatorOpinionProtocolFigureSelectionV1,
    OperatorOpinionProtocolFiguresV1,
)
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    BridgeDraftReferenceV1,
    BridgeReviewReferenceV1,
    CitationViolation,
    CitationViolationCode,
    ImplantMaterialEditorialV1,
    ImplantMaterialType,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
    OpinionProtocolFigureV1,
    opinion_blocks_with_implant_materials,
    opinion_blocks_with_observations,
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
    ToolInvocationStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.protocol_model import ProtocolDiagramKind
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    publication_document_from_json,
    publication_document_text_anchors,
    publication_document_v5_sha256,
)

_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_DECISION_NAMESPACE = UUID("6fd4bf30-7369-4c64-9bf4-d5bd17c67ce2")
_OPERATOR_NOTE_NAMESPACE = UUID("a6088d45-c437-4d6c-9d7b-0cc75a789410")
OPERATOR_OPINION_SCHEMA_VERSION = "operator-opinion.v1"
_OPERATOR_BLOCK_TYPES = tuple(
    item
    for item in OpinionBlockType
    if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
)


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
    OPINION_NOT_DRAFT = "opinion_not_draft"
    INVALID_SELECTION = "invalid_selection"
    REVIEW_TEXT_STALE = "review_text_stale"
    INVALID_OBSERVATION_ANCHOR = "invalid_observation_anchor"
    INVALID_STATE = "invalid_opinion_state"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PROTOCOL_FIGURES_INVALID = "invalid_protocol_figures"
    PROTOCOL_FIGURES_NOT_IN_INVESTIGATION = "protocol_figures_not_in_investigation"
    PROTOCOL_FIGURES_UNAVAILABLE = "protocol_figures_unavailable"


class OpinionError(ValueError):
    """Typed refusal for invalid opinion state, evidence, or review operations."""

    def __init__(
        self,
        code: OpinionErrorCode,
        *,
        citation_violations: tuple[CitationViolation, ...] = (),
        field_path: str | None = None,
    ) -> None:
        self.code = code
        self.citation_violations = citation_violations
        self.field_path = field_path
        super().__init__(code.value)


class _OperatorOpinionInputError(ValueError):
    def __init__(self, code: str, field_path: str) -> None:
        self.code = code
        self.field_path = field_path
        super().__init__(f"{code}: {field_path}")


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
    block_types: tuple[OpinionBlockType, ...] = tuple(
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    )

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
    protocol_figures: tuple[OpinionProtocolFigureV1, ...] | None = None
    observations: tuple[AnalystObservationV1, ...] | None = None
    implant_materials: tuple[ImplantMaterialEditorialV1, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(self, "unresolved_points", tuple(self.unresolved_points))
        if any(not isinstance(item, OpinionBlock) for item in self.blocks):
            raise TypeError("Opinion edits must contain OpinionBlock values")
        if self.protocol_figures is not None:
            object.__setattr__(self, "protocol_figures", tuple(self.protocol_figures))
            if any(not isinstance(item, OpinionProtocolFigureV1) for item in self.protocol_figures):
                raise TypeError("Opinion edits must contain OpinionProtocolFigureV1 values")
        if self.observations is not None:
            object.__setattr__(self, "observations", tuple(self.observations))
            if any(not isinstance(item, AnalystObservationV1) for item in self.observations):
                raise TypeError("Opinion edits must contain AnalystObservationV1 values")
        if self.implant_materials is not None:
            object.__setattr__(self, "implant_materials", tuple(self.implant_materials))
            if any(
                not isinstance(item, ImplantMaterialEditorialV1) for item in self.implant_materials
            ):
                raise TypeError("Opinion edits must contain ImplantMaterialEditorialV1 values")


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
        *,
        protocol_figures: tuple[OpinionProtocolFigureV1, ...] | None = None,
        draft_blocks: tuple[OpinionBlock, ...] | None = None,
        implant_materials: tuple[ImplantMaterialEditorialV1, ...] | None = None,
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

            if draft_blocks is None:
                blocks, unresolved = await self._compose_blocks(
                    investigation_id=investigation_id,
                    findings=selected,
                    observations=observations,
                    selection=normalized,
                )
            else:
                blocks = tuple(draft_blocks)
                if any(not isinstance(item, OpinionBlock) for item in blocks):
                    raise OpinionError(OpinionErrorCode.INVALID_SELECTION)
                unresolved = ()
            composed_materials = (
                implant_materials
                if implant_materials is not None
                else current.implant_materials
                if current is not None
                else ()
            )
            material_section = next(
                (
                    block
                    for block in blocks
                    if block.block_type is OpinionBlockType.IMPLANT_MATERIALS
                ),
                None,
            )
            if (
                material_section is None
                and current is not None
                and (implant_materials is None or composed_materials == current.implant_materials)
            ):
                material_section = current.implant_materials_section
            blocks = opinion_blocks_with_implant_materials(
                blocks, composed_materials, section=material_section
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
                protocol_figures=(
                    protocol_figures
                    if protocol_figures is not None
                    else current.protocol_figures
                    if current is not None
                    else ()
                ),
                author_id=current.author_id if current is not None else self._author_id,
                status=AnalystOpinionStatus.DRAFT,
            )
            if current is not None and (
                current.status is AnalystOpinionStatus.DRAFT
                and current.blocks == opinion.blocks
                and current.unresolved_points == opinion.unresolved_points
                and current.protocol_figures == opinion.protocol_figures
            ):
                return current
            await self._validate_bridge_provenance(uow, opinion)
            await self._validate_citations(uow, opinion)
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
        *,
        expected_sha256: str | None = None,
    ) -> AnalystOpinionV1:
        actor_id = self._require_actor(actor)
        revision = (
            edits if isinstance(edits, OpinionRevisionEdits) else OpinionRevisionEdits(tuple(edits))
        )
        async with self._uow_factory() as uow:
            current = await self._require_current(uow, opinion_id)
            if expected_sha256 is not None:
                self._require_expected_sha(current, expected_sha256)
            edited_observations = (
                revision.observations if revision.observations is not None else current.observations
            )
            edited_materials = (
                revision.implant_materials
                if revision.implant_materials is not None
                else current.implant_materials
            )
            revised_material_section = next(
                (
                    block
                    for block in revision.blocks
                    if block.block_type is OpinionBlockType.IMPLANT_MATERIALS
                ),
                None,
            )
            if revised_material_section is None and (
                revision.implant_materials is None or edited_materials == current.implant_materials
            ):
                revised_material_section = current.implant_materials_section
            edited_blocks = opinion_blocks_with_implant_materials(
                opinion_blocks_with_observations(revision.blocks, edited_observations),
                edited_materials,
                section=revised_material_section,
            )
            if current.version != expected_version:
                replay = await uow.analyst_opinions.get(opinion_id, version=expected_version + 1)
                if (
                    replay is not None
                    and replay.status is AnalystOpinionStatus.DRAFT
                    and replay.author_id == actor_id
                    and replay.blocks == edited_blocks
                    and replay.unresolved_points == revision.unresolved_points
                    and replay.protocol_figures
                    == (
                        revision.protocol_figures
                        if revision.protocol_figures is not None
                        else current.protocol_figures
                    )
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
                blocks=edited_blocks,
                unresolved_points=revision.unresolved_points,
                protocol_figures=(
                    revision.protocol_figures
                    if revision.protocol_figures is not None
                    else current.protocol_figures
                ),
                author_id=actor_id,
                status=AnalystOpinionStatus.DRAFT,
            )
            await self._validate_bridge_provenance(uow, candidate)
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
                    and concurrent.protocol_figures == candidate.protocol_figures
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
                protocol_figures=reviewed.protocol_figures,
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
                protocol_figures=reviewed.protocol_figures,
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
        material_observation_refs = {
            item.evidence_observation_id for item in opinion.implant_materials
        }
        missing_material_evidence = material_observation_refs - observation_by_id.keys()
        invalid_material_index = next(
            (
                index
                for index, item in enumerate(opinion.implant_materials)
                if (observation := observation_by_id.get(item.evidence_observation_id)) is None
                or observation.investigation_id != opinion.investigation_id
            ),
            None,
        )
        foreign = (
            any(
                (finding := finding_by_id.get(ref)) is not None
                and finding.investigation_id != opinion.investigation_id
                for block in opinion.blocks
                for ref in block.finding_refs
            )
            or any(
                (observation := observation_by_id.get(ref)) is not None
                and observation.investigation_id != opinion.investigation_id
                for block in opinion.blocks
                for ref in block.evidence_refs
            )
            or any(
                (observation := observation_by_id.get(ref)) is not None
                and observation.investigation_id != opinion.investigation_id
                for ref in material_observation_refs
            )
        )
        if violations or foreign or missing_material_evidence:
            citation_violations = violations
            field_path = None
            if invalid_material_index is not None:
                invalid_item = opinion.implant_materials[invalid_material_index]
                citation_violations = (
                    *violations,
                    CitationViolation(
                        CitationViolationCode.OBSERVATION_NOT_FOUND,
                        OpinionBlockType.IMPLANT_MATERIALS,
                        invalid_item.evidence_observation_id,
                        "The cited implant-material observation does not exist "
                        "in this investigation.",
                    ),
                )
                field_path = f"implant_materials[{invalid_material_index}].evidence_observation_id"
            raise OpinionError(
                OpinionErrorCode.CITATIONS_INVALID,
                citation_violations=citation_violations,
                field_path=field_path,
            )
        if opinion.observations:
            base = await uow.production_artifacts.get(opinion.base_publication_artifact_id)
            if base is None or base.canonical_blob_id is None:
                raise OpinionError(OpinionErrorCode.BASE_INVALID)
            try:
                base_bytes = await self._artifact_store.read_bytes(base.canonical_blob_id)
                base_document = publication_document_from_json(json.loads(base_bytes))
            except Exception as exc:
                raise OpinionError(OpinionErrorCode.BASE_INVALID) from exc
            if not isinstance(base_document, PublicationDocumentV5):
                raise OpinionError(OpinionErrorCode.BASE_INVALID)
            anchors = publication_document_text_anchors(
                base_document.document,
                references=base_document.references,
            )
            if any(
                note.anchor not in anchors or not note.anchor.startswith(("lead:", "section:"))
                for note in opinion.observations
            ):
                raise OpinionError(OpinionErrorCode.INVALID_OBSERVATION_ANCHOR)

    async def _validate_bridge_provenance(self, uow: UnitOfWork, opinion: AnalystOpinionV1) -> None:
        requested: list[
            tuple[
                str,
                str,
                str | None,
                BridgeDraftReferenceV1 | None,
                BridgeReviewReferenceV1 | None,
            ]
        ] = []
        for block in opinion.blocks:
            if block.block_type in {
                OpinionBlockType.ANALYST_OBSERVATIONS,
                OpinionBlockType.IMPLANT_MATERIALS,
            }:
                continue
            if block.bridge_authored or block.bridge_draft_ref or block.bridge_review_ref:
                requested.append(
                    (
                        block.text,
                        block.block_type.value,
                        None,
                        block.bridge_draft_ref,
                        block.bridge_review_ref,
                    )
                )
        for note in opinion.observations:
            if note.bridge_authored or note.bridge_draft_ref or note.bridge_review_ref:
                requested.append(
                    (
                        note.text,
                        (
                            note.bridge_block_type.value
                            if note.bridge_block_type is not None
                            else OpinionBlockType.ANALYST_OBSERVATIONS.value
                        ),
                        note.anchor,
                        note.bridge_draft_ref,
                        note.bridge_review_ref,
                    )
                )
        material_section = opinion.implant_materials_section
        if material_section is not None and (
            material_section.bridge_authored
            or material_section.bridge_draft_ref
            or material_section.bridge_review_ref
        ):
            requested.append(
                (
                    material_section.text,
                    OpinionBlockType.IMPLANT_MATERIALS.value,
                    None,
                    material_section.bridge_draft_ref,
                    material_section.bridge_review_ref,
                )
            )
        if not requested:
            return

        persisted = await uow.evidence_observations.list_for_investigation(opinion.investigation_id)
        advisor_calls: dict[str, Mapping[str, Any]] = {}
        for observation in persisted:
            if observation.investigation_id != opinion.investigation_id:
                continue
            if observation.source != "advisor.consult" or observation.object_type != "advisor_call":
                continue
            try:
                raw = await self._artifact_store.read_bytes(
                    observation.blob_id, max_bytes=160 * 1024
                )
                if hashlib.sha256(raw).hexdigest() != observation.sha256:
                    continue
                payload = json.loads(raw)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema") != "operator-advisor-call.v1"
                or payload.get("consultation_type") != observation.object_id
            ):
                continue
            invocation_id = payload.get("bridge_invocation_id")
            if isinstance(invocation_id, str):
                advisor_calls[invocation_id] = payload

        for text_value, block_type, anchor, draft_ref, review_ref in requested:
            text_sha256 = hashlib.sha256(text_value.encode("utf-8")).hexdigest()
            if review_ref is not None and review_ref.reviewed_text_sha256 != text_sha256:
                raise OpinionError(OpinionErrorCode.REVIEW_TEXT_STALE)
            if draft_ref is None or draft_ref.text_sha256 != text_sha256:
                raise OpinionError(OpinionErrorCode.INVALID_SELECTION)
            draft_call = advisor_calls.get(draft_ref.invocation_id)
            if not self._bridge_call_contains_draft(
                draft_call,
                text_value=text_value,
                text_sha256=text_sha256,
                block_type=block_type,
                anchor=anchor,
            ):
                raise OpinionError(OpinionErrorCode.INVALID_SELECTION)
            if review_ref is not None:
                review_call = advisor_calls.get(review_ref.invocation_id)
                if not self._bridge_call_reviews_text(
                    review_call,
                    review_ref=review_ref,
                    text_sha256=text_sha256,
                    block_type=block_type,
                    anchor=anchor,
                ):
                    raise OpinionError(OpinionErrorCode.INVALID_SELECTION)

    @staticmethod
    def _bridge_call_contains_draft(
        call: Mapping[str, Any] | None,
        *,
        text_value: str,
        text_sha256: str,
        block_type: str,
        anchor: str | None,
    ) -> bool:
        if call is None or call.get("consultation_type") != "draft_text":
            return False
        if not AnalystOpinionService._bridge_call_has_real_provenance(call):
            return False
        output = call.get("output")
        if not isinstance(output, Mapping):
            return False
        if output.get("schema") != "advisor-response.v3":
            return False
        for block in output.get("blocks", ()):
            if not isinstance(block, Mapping):
                continue
            if (
                block.get("block_type") != block_type
                or block.get("form") != ("analyst_note" if anchor is not None else "body")
                or block.get("anchor") != anchor
            ):
                continue
            sentences = block.get("sentences")
            if not isinstance(sentences, list) or not sentences:
                continue
            sentence_texts: list[str] = []
            for item in sentences:
                if not isinstance(item, Mapping):
                    continue
                sentence_text = item.get("text")
                if isinstance(sentence_text, str):
                    sentence_texts.append(sentence_text)
            if (
                len(sentence_texts) == len(sentences)
                and (
                    block.get("usable") is True
                    or (
                        "usable" not in block
                        and output.get("usable") is True
                        and call.get("advisor_status") == "succeeded"
                    )
                )
                and " ".join(sentence_texts) == text_value
                and hashlib.sha256(" ".join(sentence_texts).encode("utf-8")).hexdigest()
                == text_sha256
            ):
                return True
        return False

    @staticmethod
    def _bridge_call_reviews_text(
        call: Mapping[str, Any] | None,
        *,
        review_ref: BridgeReviewReferenceV1,
        text_sha256: str,
        block_type: str,
        anchor: str | None,
    ) -> bool:
        if call is None or call.get("consultation_type") != "review_text":
            return False
        if not AnalystOpinionService._bridge_call_has_real_provenance(call):
            return False
        if call.get("output_sha256") != review_ref.output_sha256:
            return False
        output = call.get("output")
        if not isinstance(output, Mapping) or output.get("schema") not in {
            "advisor-response.v3",
            "advisor-response.v4",
        }:
            return False
        expected_form = "analyst_note" if anchor is not None else "body"
        review_units = call.get("review_units")
        if isinstance(review_units, list):
            reviews = output.get("reviews", ())
            if not isinstance(reviews, (list, tuple)):
                return False
            for unit in review_units:
                if not (
                    isinstance(unit, Mapping)
                    and unit.get("reviewed_text_sha256") == text_sha256
                    and unit.get("form") == expected_form
                    and unit.get("block_type") == block_type
                    and unit.get("anchor") == anchor
                ):
                    continue
                for review in reviews:
                    if not (
                        isinstance(review, Mapping)
                        and review.get("unit_id") == unit.get("unit_id")
                        and isinstance(review.get("remarks"), list)
                        and review.get("remarks")
                    ):
                        continue
                    if review.get("usable") is True or (
                        "usable" not in review
                        and output.get("usable") is True
                        and call.get("advisor_status") == "succeeded"
                    ):
                        return True
            return False
        if call.get("reviewed_text_sha256") != text_sha256:
            return False
        reviews = output.get("reviews", ())
        if not isinstance(reviews, (list, tuple)):
            return False
        if not any(
            isinstance(review, Mapping)
            and isinstance(review.get("remarks"), list)
            and review.get("remarks")
            and (
                review.get("usable") is True
                or (
                    "usable" not in review
                    and output.get("usable") is True
                    and call.get("advisor_status") == "succeeded"
                )
            )
            for review in reviews
        ):
            return False
        review_target = call.get("review_target")
        return bool(
            isinstance(review_target, Mapping)
            and review_target.get("form") == expected_form
            and review_target.get("block_type") == block_type
            and review_target.get("anchor") == anchor
        )

    @staticmethod
    def _bridge_call_has_real_provenance(call: Mapping[str, Any]) -> bool:
        invocation_id = call.get("bridge_invocation_id")
        output_sha256 = call.get("output_sha256")
        output = call.get("output")
        provenance = output.get("bridge_provenance") if isinstance(output, Mapping) else None
        if (
            call.get("advisor_status") not in {"succeeded", "partial"}
            or call.get("bridge_invocation_id_kind") != "conversation_turn"
            or not isinstance(invocation_id, str)
            or not isinstance(output_sha256, str)
            or len(output_sha256) != 64
            or any(character not in "0123456789abcdef" for character in output_sha256)
            or not isinstance(output, Mapping)
            or not isinstance(provenance, Mapping)
            or provenance.get("bridge_invocation_id") != invocation_id
            or provenance.get("bridge_invocation_id_kind") != "conversation_turn"
            or provenance.get("output_sha256") != output_sha256
            or output.get("output_sha256") != output_sha256
            or not isinstance(call.get("input_refs"), list)
            or provenance.get("input_refs") != call.get("input_refs")
        ):
            return False
        try:
            return str(UUID(invocation_id)) == invocation_id
        except (TypeError, ValueError):
            return False

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
            and left.protocol_figures == right.protocol_figures
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
            raw_blocks = cast(
                Iterable[object],
                value.get(
                    "block_types",
                    tuple(
                        item
                        for item in OpinionBlockType
                        if item
                        not in {
                            OpinionBlockType.ANALYST_OBSERVATIONS,
                            OpinionBlockType.IMPLANT_MATERIALS,
                        }
                    ),
                ),
            )
            block_types = tuple(
                item if isinstance(item, OpinionBlockType) else OpinionBlockType(str(item))
                for item in raw_blocks
            )
            return OpinionCompositionSelection(finding_ids, claim_ids, block_types)
        except (TypeError, ValueError) as exc:
            raise OpinionError(OpinionErrorCode.INVALID_SELECTION) from exc


class OperatorProtocolFigureAttachmentBuilder:
    """Validate observed protocol render provenance and compile opinion attachments."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def build(
        self,
        investigation_id: UUID,
        selection: OperatorOpinionProtocolFigureSelectionV1,
    ) -> tuple[tuple[OpinionProtocolFigureV1, ...], tuple[UUID, ...]]:
        from cti_app.application.diagram_compilation import DiagramCompilationError
        from cti_app.application.media_assets import MediaAssetStore
        from cti_app.application.network_protocol_model import render_protocol_model
        from cti_app.application.protocol_diagram_projection import (
            ProtocolDiagramProjectionError,
            compile_protocol_render_result,
            opinion_protocol_figures,
        )
        from cti_app.domain.protocol_model import ProtocolModelV1
        from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler

        if set(selection.views) != set(ProtocolDiagramKind):
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_INVALID)

        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            blob = await uow.blobs.get(selection.protocol_model_blob_id)
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(investigation_id)
            )
            model_observations = tuple(
                item
                for item in observations
                if blob is not None
                and item.object_type == "protocol_model"
                and item.object_id == blob.descriptor.sha256
            )
            render_observation = next(
                (item for item in observations if item.id == selection.render_observation_id),
                None,
            )
            if (
                investigation is None
                or blob is None
                or blob.descriptor.logical_bucket != "network-protocol-models"
                or not model_observations
                or render_observation is None
                or render_observation.source != "network.protocol.render"
                or render_observation.object_type != "protocol_diagram_specs"
            ):
                raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_NOT_IN_INVESTIGATION)
            production_run_id = investigation.production_run_id
            subject_id = investigation.subject_id
            blob_sha256 = blob.descriptor.sha256

        try:
            model = ProtocolModelV1.from_dict(
                await self._artifact_store.read_json(selection.protocol_model_blob_id)
            )
        except Exception as exc:
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_INVALID) from exc
        if (
            model.investigation_id != investigation_id
            or model.subject_id != str(subject_id)
            or model.canonical_sha256 != blob_sha256
        ):
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_NOT_IN_INVESTIGATION)

        render_result = render_protocol_model(model)
        if (
            render_observation.object_id != render_result.canonical_sha256
            or render_observation.sha256 != render_result.canonical_sha256
            or render_result.model_sha256 != model.canonical_sha256
        ):
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_INVALID)

        try:
            rendered = await compile_protocol_render_result(
                render_result,
                compiler=D2DiagramCompiler(),
                media_asset_store=MediaAssetStore(self._artifact_store, self._uow_factory),
                production_run_id=production_run_id,
            )
            figures = opinion_protocol_figures(rendered)
        except (DiagramCompilationError, ProtocolDiagramProjectionError) as exc:
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_UNAVAILABLE) from exc
        return figures, tuple(dict.fromkeys((model_observations[0].id, render_observation.id)))


class OperatorOpinionToolAdapter:
    """Create and revise draft opinion content from typed, cited operator input."""

    def __init__(
        self,
        *,
        service: AnalystOpinionService,
        investigations: Any,
        article_snapshots: ArticleSnapshotPort,
        protocol_figure_builder: OperatorProtocolFigureAttachmentBuilder | None = None,
    ) -> None:
        self._service = service
        self._investigations = investigations
        self._article_snapshots = article_snapshots
        self._protocol_figure_builder = protocol_figure_builder

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        action = str(arguments["action"])
        try:
            if action == "compose":
                return await self._compose(arguments, context)
            if action == "revise":
                return await self._revise(arguments, context)
            return await self._note(arguments, context)
        except _OperatorOpinionInputError as exc:
            return _operator_opinion_input_failure(exc.code, exc.field_path)
        except OpinionError as exc:
            code = exc.code.value
            if exc.field_path is not None:
                return _operator_opinion_input_failure(code, exc.field_path)
            return ToolResult(
                ToolInvocationStatus.FAILED,
                error_code=code,
                output={"error_code": code},
                message=code,
            )
        except (TypeError, ValueError) as exc:
            code = "invalid_note_anchor" if "anchor" in str(exc).casefold() else "invalid_opinion"
            return ToolResult(
                ToolInvocationStatus.FAILED,
                error_code=code,
                output={"error_code": code},
                message=code,
            )

    async def _compose(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        current = (await self._service.get_draft(context.investigation_id)).opinion
        if current is not None and current.status is not AnalystOpinionStatus.DRAFT:
            return _operator_opinion_failure("opinion_draft_required")
        materials = _operator_implant_materials(arguments.get("implant_materials"))
        raw = arguments["selection"]
        if not isinstance(raw, Mapping):
            return _operator_opinion_failure("invalid_opinion_selection")
        selection = OpinionCompositionSelection(
            finding_ids=tuple(UUID(str(item)) for item in raw.get("finding_ids", ())),
            claim_ids=tuple(str(item) for item in raw.get("claim_ids", ())),
            block_types=tuple(OpinionBlockType(str(item)) for item in raw.get("block_types", ())),
        )
        protocol_figures, figure_observation_refs = await self._protocol_figures(
            context.investigation_id, arguments.get("protocol_figures")
        )
        draft_blocks = (
            tuple(
                _operator_opinion_block(item, allow_implant_materials=True)
                for item in arguments["blocks"]
            )
            if "blocks" in arguments
            else None
        )
        if (
            draft_blocks is not None
            and any(
                block.block_type is OpinionBlockType.IMPLANT_MATERIALS for block in draft_blocks
            )
            and not (
                materials if materials is not None else getattr(current, "implant_materials", ())
            )
        ):
            raise _OperatorOpinionInputError("implant_materials_required", "implant_materials")
        opinion = await self._service.compose_from_findings(
            context.investigation_id,
            selection,
            protocol_figures=protocol_figures,
            draft_blocks=draft_blocks,
            implant_materials=materials,
        )
        return _operator_opinion_result(
            opinion,
            message="Opinion composed as a draft.",
            additional_observation_refs=figure_observation_refs,
        )

    async def _revise(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        opinion_id = UUID(str(arguments["opinion_id"]))
        current = (await self._service.get_draft(context.investigation_id)).opinion
        if (
            current is None
            or current.id != opinion_id
            or current.status is not AnalystOpinionStatus.DRAFT
        ):
            return _operator_opinion_failure("opinion_draft_required")
        materials = _operator_implant_materials(arguments.get("implant_materials"))
        protocol_figures, figure_observation_refs = await self._protocol_figures(
            context.investigation_id, arguments.get("protocol_figures")
        )
        blocks = tuple(
            _operator_opinion_block(item, allow_implant_materials=True)
            for item in arguments["blocks"]
        )
        if any(block.block_type is OpinionBlockType.IMPLANT_MATERIALS for block in blocks) and not (
            materials if materials is not None else getattr(current, "implant_materials", ())
        ):
            raise _OperatorOpinionInputError("implant_materials_required", "implant_materials")
        edits = OpinionRevisionEdits(
            blocks=blocks,
            unresolved_points=tuple(str(item) for item in arguments["unresolved_points"]),
            protocol_figures=protocol_figures,
            implant_materials=materials,
        )
        opinion = await self._service.revise(
            opinion_id,
            int(arguments["expected_opinion_version"]),
            edits,
            "operator:claude-client",
            expected_sha256=str(arguments["expected_opinion_sha256"]),
        )
        return _operator_opinion_result(
            opinion,
            message="Opinion revised as a draft.",
            additional_observation_refs=figure_observation_refs,
        )

    async def _protocol_figures(
        self, investigation_id: UUID, raw: object
    ) -> tuple[tuple[OpinionProtocolFigureV1, ...] | None, tuple[UUID, ...]]:
        if raw is None:
            return None, ()
        try:
            selections: list[OperatorOpinionProtocolFigureSelectionV1] = []
            if not isinstance(raw, (list, tuple)):
                raise ValueError("protocol_figures must be a list")
            for item in raw:
                if not isinstance(item, Mapping) or set(item) != {
                    "protocol_model_blob_id",
                    "render_observation_id",
                    "views",
                }:
                    raise ValueError("protocol figure selection has invalid fields")
                views = item["views"]
                if not isinstance(views, (list, tuple)):
                    raise ValueError("protocol figure views must be a list")
                selections.append(
                    OperatorOpinionProtocolFigureSelectionV1(
                        protocol_model_blob_id=UUID(str(item["protocol_model_blob_id"])),
                        render_observation_id=UUID(str(item["render_observation_id"])),
                        views=tuple(ProtocolDiagramKind(str(view)) for view in views),
                    )
                )
            request = OperatorOpinionProtocolFiguresV1(protocol_figures=tuple(selections))
        except (TypeError, ValueError) as exc:
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_INVALID) from exc
        if not request.protocol_figures:
            return (), ()
        if self._protocol_figure_builder is None:
            raise OpinionError(OpinionErrorCode.PROTOCOL_FIGURES_UNAVAILABLE)
        return await self._protocol_figure_builder.build(
            investigation_id, request.protocol_figures[0]
        )

    async def _note(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        investigation = await self._investigations.get(context.investigation_id)
        if investigation is None:
            return _operator_opinion_failure("investigation_not_found")
        snapshot = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        anchor = str(arguments["anchor"])
        valid_anchors = {item.anchor for item in snapshot.article.publication_anchors}
        if anchor not in valid_anchors:
            return _operator_opinion_failure("invalid_note_anchor")

        current = (await self._service.get_draft(context.investigation_id)).opinion
        if current is None or current.status is not AnalystOpinionStatus.DRAFT:
            return _operator_opinion_failure("opinion_draft_required")
        if current.version != int(
            arguments["expected_opinion_version"]
        ) or current.content_sha256 != str(arguments["expected_opinion_sha256"]):
            return _operator_opinion_failure("stale_opinion_version")

        note = AnalystObservationV1(
            id=uuid5(_OPERATOR_NOTE_NAMESPACE, context.idempotency_key),
            anchor=anchor,
            text=str(arguments["text"]),
            state=AnalystObservationState(str(arguments["state"])),
            relation=AnalystObservationRelation(str(arguments["relation"])),
            finding_refs=tuple(UUID(str(item)) for item in arguments["finding_refs"]),
            evidence_refs=tuple(UUID(str(item)) for item in arguments["evidence_refs"]),
            bridge_authored=arguments.get("bridge_authored", False),
            bridge_block_type=(
                OpinionBlockType(str(arguments["bridge_block_type"]))
                if arguments.get("bridge_block_type") is not None
                else None
            ),
            bridge_draft_ref=_operator_bridge_draft_ref(arguments.get("bridge_draft_ref")),
            bridge_review_ref=_operator_bridge_review_ref(arguments.get("bridge_review_ref")),
        )
        notes = (*[item for item in current.observations if item.anchor != anchor], note)
        opinion = await self._service.revise(
            current.id,
            current.version,
            OpinionRevisionEdits(
                blocks=current.blocks,
                unresolved_points=current.unresolved_points,
                protocol_figures=current.protocol_figures,
                observations=notes,
            ),
            "operator:claude-client",
            expected_sha256=current.content_sha256,
        )
        return _operator_opinion_result(opinion, message="Anchored note saved in the draft.")


def register_operator_opinion_tools(
    registry: ToolRegistry,
    *,
    service: AnalystOpinionService,
    investigations: Any,
    article_snapshots: ArticleSnapshotPort,
    protocol_figure_builder: OperatorProtocolFigureAttachmentBuilder | None = None,
) -> None:
    adapter = OperatorOpinionToolAdapter(
        service=service,
        investigations=investigations,
        article_snapshots=article_snapshots,
        protocol_figure_builder=protocol_figure_builder,
    )
    uuid_schema = {
        "type": "string",
        "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    }
    string_list = {
        "type": "array",
        "maxItems": 128,
        "items": {"type": "string", "minLength": 1, "maxLength": 128},
    }
    uuid_list = {"type": "array", "maxItems": 128, "items": uuid_schema}
    bridge_draft_ref_schema = {
        "type": "object",
        "properties": {
            "invocation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "text_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        },
        "required": ["invocation_id", "text_sha256"],
        "additionalProperties": False,
    }
    bridge_review_ref_schema = {
        "type": "object",
        "properties": {
            "invocation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "output_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
            "reviewed_text_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        },
        "required": ["invocation_id", "output_sha256", "reviewed_text_sha256"],
        "additionalProperties": False,
    }
    blocks_schema = {
        "type": "array",
        "maxItems": 32,
        "items": {
            "type": "object",
            "properties": {
                "block_type": {
                    "type": "string",
                    "enum": [item.value for item in _OPERATOR_BLOCK_TYPES],
                },
                "text": {"type": "string", "maxLength": 12000},
                "finding_refs": uuid_list,
                "evidence_refs": uuid_list,
                "qualification": {
                    "type": "string",
                    "enum": [item.value for item in OpinionClaimQualification],
                },
                "certainty": {"type": "number", "minimum": 0, "maximum": 1},
                "bridge_authored": {"type": "boolean"},
                "bridge_draft_ref": bridge_draft_ref_schema,
                "bridge_review_ref": bridge_review_ref_schema,
            },
            "required": [
                "block_type",
                "text",
                "finding_refs",
                "evidence_refs",
                "qualification",
                "certainty",
            ],
            "additionalProperties": False,
        },
    }

    def editorial_blocks_schema() -> dict[str, Any]:
        schema = cast(dict[str, Any], json.loads(json.dumps(blocks_schema)))
        items = cast(dict[str, Any], schema["items"])
        properties = cast(dict[str, Any], items["properties"])
        properties["block_type"] = {
            "type": "string",
            "enum": [
                *(item.value for item in _OPERATOR_BLOCK_TYPES),
                OpinionBlockType.IMPLANT_MATERIALS.value,
            ],
        }
        return schema

    composition_blocks_schema = editorial_blocks_schema()
    revision_blocks_schema = editorial_blocks_schema()
    implant_materials_schema = {
        "type": "array",
        "maxItems": 100,
        "description": (
            "Lignes typées des matériaux d'implant. Chaque evidence_observation_id doit désigner "
            "une observation enregistrée dans cette enquête. Fournissez ces lignes pour tout "
            "bloc implant_materials; un bloc de texte seul ne crée pas de ligne typée."
        ),
        "items": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [item.value for item in ImplantMaterialType],
                },
                "value": {"type": "string", "minLength": 1, "maxLength": 4096},
                "role": {"type": "string", "minLength": 1, "maxLength": 300},
                "sample_location": {"type": "string", "minLength": 1, "maxLength": 500},
                "sample_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                "evidence_observation_id": uuid_schema,
            },
            "required": [
                "type",
                "value",
                "role",
                "sample_location",
                "sample_sha256",
                "evidence_observation_id",
            ],
            "additionalProperties": False,
        },
    }
    protocol_figures_schema = {
        "type": "array",
        "maxItems": 1,
        "description": (
            "Optionnel : joindre les trois vues D2 d'un modèle de protocole déjà rendu dans cette "
            "enquête. Indiquez son protocol_model_blob_id, le render_observation_id renvoyé par "
            "network.protocol.render, et les trois vues sequence, state_machine et message_format "
            "ensemble. Un modèle documenté reste marqué « documenté, non observé »."
        ),
        "items": {
            "type": "object",
            "properties": {
                "protocol_model_blob_id": uuid_schema,
                "render_observation_id": uuid_schema,
                "views": {
                    "type": "array",
                    "minItems": 3,
                    "maxItems": 3,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": [item.value for item in ProtocolDiagramKind],
                    },
                    "description": (
                        "Les vues sequence, state_machine et message_format "
                        "doivent être fournies ensemble."
                    ),
                },
            },
            "required": ["protocol_model_blob_id", "render_observation_id", "views"],
            "additionalProperties": False,
        },
    }
    registry.register(
        ToolDefinition(
            name="opinion.compose",
            schema_version=OPERATOR_OPINION_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"const": "compose"},
                    "protocol_figures": protocol_figures_schema,
                    "implant_materials": implant_materials_schema,
                    "blocks": composition_blocks_schema,
                    "selection": {
                        "type": "object",
                        "properties": {
                            "finding_ids": uuid_list,
                            "claim_ids": string_list,
                            "block_types": {
                                "type": "array",
                                "maxItems": 7,
                                "items": {
                                    "type": "string",
                                    "enum": [item.value for item in _OPERATOR_BLOCK_TYPES],
                                },
                            },
                        },
                        "required": ["finding_ids", "claim_ids", "block_types"],
                        "additionalProperties": False,
                    },
                },
                "required": ["action", "selection"],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=("Composition creates an unapproved draft only.",),
            capability_key="operator.write",
        ),
        adapter,
    )
    registry.register(
        ToolDefinition(
            name="opinion.revise",
            schema_version=OPERATOR_OPINION_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"const": "revise"},
                    "opinion_id": uuid_schema,
                    "expected_opinion_version": {"type": "integer", "minimum": 1},
                    "expected_opinion_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                    "blocks": revision_blocks_schema,
                    "implant_materials": implant_materials_schema,
                    "unresolved_points": {
                        "type": "array",
                        "maxItems": 128,
                        "items": {"type": "string", "maxLength": 2000},
                    },
                    "protocol_figures": protocol_figures_schema,
                },
                "required": [
                    "action",
                    "opinion_id",
                    "expected_opinion_version",
                    "expected_opinion_sha256",
                    "blocks",
                    "unresolved_points",
                ],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=("Revisions remain unapproved and must cite persisted findings/evidence.",),
            capability_key="operator.write",
        ),
        adapter,
    )
    registry.register(
        ToolDefinition(
            name="opinion.note",
            schema_version=OPERATOR_OPINION_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"const": "note"},
                    "anchor": {"type": "string", "minLength": 1, "maxLength": 128},
                    "state": {
                        "type": "string",
                        "enum": [item.value for item in AnalystObservationState],
                    },
                    "relation": {
                        "type": "string",
                        "enum": [item.value for item in AnalystObservationRelation],
                    },
                    "text": {"type": "string", "minLength": 1, "maxLength": 6000},
                    "expected_opinion_version": {"type": "integer", "minimum": 1},
                    "expected_opinion_sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                    "finding_refs": {"type": "array", "maxItems": 16, "items": uuid_schema},
                    "evidence_refs": {"type": "array", "maxItems": 16, "items": uuid_schema},
                    "bridge_authored": {"type": "boolean"},
                    "bridge_block_type": {
                        "type": "string",
                        "enum": [item.value for item in _OPERATOR_BLOCK_TYPES],
                    },
                    "bridge_draft_ref": bridge_draft_ref_schema,
                    "bridge_review_ref": bridge_review_ref_schema,
                },
                "required": [
                    "action",
                    "anchor",
                    "state",
                    "relation",
                    "text",
                    "expected_opinion_version",
                    "expected_opinion_sha256",
                    "finding_refs",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=("Notes are anchored drafts and never create evidence observations.",),
            capability_key="operator.write",
        ),
        adapter,
    )


def _operator_opinion_block(
    value: object, *, allow_implant_materials: bool = False
) -> OpinionBlock:
    if not isinstance(value, Mapping):
        raise ValueError("Opinion block is invalid")
    block_type = OpinionBlockType(str(value["block_type"]))
    if block_type is OpinionBlockType.ANALYST_OBSERVATIONS or (
        block_type is OpinionBlockType.IMPLANT_MATERIALS and not allow_implant_materials
    ):
        raise ValueError("Editorial projection blocks are not direct operator edits")
    return OpinionBlock(
        block_type=block_type,
        text=str(value["text"]),
        finding_refs=tuple(UUID(str(item)) for item in value["finding_refs"]),
        evidence_refs=tuple(UUID(str(item)) for item in value["evidence_refs"]),
        qualification=OpinionClaimQualification(str(value["qualification"])),
        certainty=float(value["certainty"]),
        bridge_authored=value.get("bridge_authored", False),
        bridge_draft_ref=_operator_bridge_draft_ref(value.get("bridge_draft_ref")),
        bridge_review_ref=_operator_bridge_review_ref(value.get("bridge_review_ref")),
    )


def _operator_implant_materials(
    value: object,
) -> tuple[ImplantMaterialEditorialV1, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise _OperatorOpinionInputError("invalid_implant_materials", "implant_materials")
    if len(value) > 100:
        raise _OperatorOpinionInputError("invalid_implant_materials", "implant_materials")
    result: list[ImplantMaterialEditorialV1] = []
    for index, item in enumerate(value):
        try:
            validated = ImplantMaterialEdit.model_validate(item)
        except ValidationError as exc:
            errors = exc.errors(include_input=False)
            suffix = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
            field_path = f"implant_materials[{index}]"
            if suffix:
                field_path = f"{field_path}.{suffix}"
            raise _OperatorOpinionInputError("invalid_implant_materials", field_path) from exc
        result.append(validated.to_domain())
    return tuple(result)


def _operator_bridge_draft_ref(value: object) -> BridgeDraftReferenceV1 | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"invocation_id", "text_sha256"}:
        raise ValueError("Bridge draft reference is invalid")
    if not isinstance(value["invocation_id"], str) or not isinstance(value["text_sha256"], str):
        raise ValueError("Bridge draft reference is invalid")
    return BridgeDraftReferenceV1(value["invocation_id"], value["text_sha256"])


def _operator_bridge_review_ref(value: object) -> BridgeReviewReferenceV1 | None:
    if value is None:
        return None
    expected = {"invocation_id", "output_sha256", "reviewed_text_sha256"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("Bridge review reference is invalid")
    if any(not isinstance(value[key], str) for key in expected):
        raise ValueError("Bridge review reference is invalid")
    return BridgeReviewReferenceV1(
        value["invocation_id"], value["output_sha256"], value["reviewed_text_sha256"]
    )


def _operator_opinion_failure(code: str) -> ToolResult:
    return ToolResult(
        ToolInvocationStatus.FAILED,
        output={"error_code": code},
        error_code=code,
        message=code,
    )


def _operator_opinion_input_failure(code: str, field_path: str) -> ToolResult:
    return ToolResult(
        ToolInvocationStatus.FAILED,
        output={"error_code": code, "field": field_path},
        error_code=code,
        message=f"{code}: {field_path}",
    )


def _operator_opinion_result(
    opinion: AnalystOpinionV1,
    *,
    message: str,
    additional_observation_refs: tuple[UUID, ...] = (),
) -> ToolResult:
    evidence_refs = tuple(
        dict.fromkeys(ref for block in opinion.blocks for ref in block.evidence_refs)
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                *evidence_refs,
                *(ref for note in opinion.observations for ref in note.evidence_refs),
                *additional_observation_refs,
            )
        )
    )
    return ToolResult(
        ToolInvocationStatus.SUCCEEDED,
        observation_refs=evidence_refs,
        output={
            "opinion_id": str(opinion.id),
            "opinion_version": opinion.version,
            "status": opinion.status.value,
            "content_sha256": opinion.content_sha256,
        },
        message=message,
    )


__all__ = [
    "AnalystOpinionDraftView",
    "AnalystOpinionService",
    "ClaimKnowledgeProvider",
    "FindingServiceClaimKnowledgeProvider",
    "OperatorOpinionToolAdapter",
    "OpinionCompositionSelection",
    "OpinionError",
    "OpinionErrorCode",
    "OpinionRevisionEdits",
    "register_operator_opinion_tools",
]
