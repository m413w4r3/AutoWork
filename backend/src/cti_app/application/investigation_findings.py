"""Code-validated, append-only investigation finding operations."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid5

from cti_app.application.article_novelty import (
    ArticleNoveltyService,
    NoveltyCandidate,
    NoveltyCandidateKind,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    VersionConflictError,
)
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    NoveltyClass,
    ToolInvocationStatus,
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
OPERATOR_FINDING_SCHEMA_VERSION = "operator-finding.v1"
_UUID_SCHEMA = {
    "type": "string",
    "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
}
_SUBJECT_SCOPE_PREFIX = "Subject reference: "


class OperatorFindingToolAdapter:
    """Write operator findings through the same versioned repository path."""

    def __init__(
        self,
        *,
        findings: FindingRepository,
        observations: EvidenceObservationRepository,
        investigations: Any,
        article_snapshots: ArticleSnapshotPort,
    ) -> None:
        self._findings = findings
        self._observations = observations
        self._investigations = investigations
        self._article_snapshots = article_snapshots

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        try:
            return await self._execute(arguments, context)
        except VersionConflictError:
            return ToolResult(
                ToolInvocationStatus.FAILED,
                error_code="stale_finding_version",
                message="The finding already has a newer version.",
            )
        except (TypeError, ValueError) as exc:
            message = str(exc).casefold()
            code = (
                "finding_observation_not_found"
                if "observation" in message and "does not exist" in message
                else "finding_observation_investigation_mismatch"
                if "another investigation" in message
                else "finding_claim_not_found"
                if "claim references" in message or "claim does not belong" in message
                else "finding_invalid"
            )
            return ToolResult(
                ToolInvocationStatus.FAILED,
                output={"error_code": code},
                error_code=code,
                message=code,
            )

    async def _execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        action = arguments["action"]
        kind = FindingKind(arguments["kind"]) if action == "propose" else None
        requested_assessment = FindingAssessment(arguments["assessment"])
        previous = None
        if action == "revise":
            finding_id = UUID(arguments["finding_id"])
            previous = await self._findings.get(finding_id)
            if previous is None or previous.investigation_id != context.investigation_id:
                return ToolResult(
                    ToolInvocationStatus.FAILED,
                    error_code="finding_not_found",
                    message="Finding does not belong to this investigation.",
                )
            if previous.version != arguments["expected_finding_version"]:
                return ToolResult(
                    ToolInvocationStatus.FAILED,
                    error_code="stale_finding_version",
                    message="The expected finding version is stale.",
                )
            kind = previous.kind

        assert kind is not None
        assessment = requested_assessment
        refs = tuple(UUID(item) for item in arguments["observation_refs"])
        claim_refs = tuple(arguments["claim_refs"])
        supplied_subject = arguments.get("subject_ref")
        if action == "revise" and supplied_subject is None:
            supplied_subject = _subject_from_scope(previous.scope if previous else "")
        subject_ref = _normalize_subject_ref(supplied_subject)
        evidence = await self._load_observations(context.investigation_id, refs)
        limitations = tuple(arguments["limitations"])
        assessment, limitations = constrain_supported_assessment(
            kind=kind,
            assessment=assessment,
            subject_ref=subject_ref,
            claim_refs=claim_refs,
            observations=evidence,
            limitations=limitations,
        )
        novelty = NoveltyClass(arguments["novelty"]) if arguments.get("novelty") else None
        if kind is FindingKind.NEW_SAMPLE and not _is_sample_hash(subject_ref):
            novelty = NoveltyClass.NOT_CONFIRMED
            assessment = FindingAssessment.INCONCLUSIVE
            limitations = (*limitations, "Un finding NEW_SAMPLE exige un hash d'objet valide.")
        elif kind is FindingKind.NEW_SAMPLE or novelty is NoveltyClass.NEW_TO_ARTICLE:
            classification = await self._classify_novelty(
                context.investigation_id,
                kind=kind,
                assessment=assessment,
                statement=arguments["statement"],
                subject_ref=subject_ref,
                claim_refs=claim_refs,
                observation_refs=refs,
            )
            novelty = classification.novelty
            limitations = tuple(dict.fromkeys((*limitations, *classification.limitations)))

        downgrade_code, prewrite_message = _finding_downgrade_guidance(
            requested_assessment=requested_assessment,
            resulting_assessment=assessment,
            kind=kind,
            subject_ref=subject_ref,
            claim_refs=claim_refs,
            observations=evidence,
            observation_refs=refs,
        )
        scope = _scope_with_subject_ref(subject_ref, arguments["scope"])
        service = FindingService(
            findings=self._findings,
            observations=self._observations,
            article_snapshots=self._article_snapshots,
        )
        if action == "propose":
            finding = await service.propose(
                investigation_id=context.investigation_id,
                kind=kind,
                assessment=assessment,
                statement=arguments["statement"],
                observation_refs=refs,
                claim_refs=claim_refs,
                novelty=novelty,
                scope=scope,
                limitations=limitations,
            )
        else:
            assert previous is not None
            finding = await service.revise(
                previous.id,
                assessment=assessment,
                statement=arguments["statement"],
                observation_refs=refs,
                claim_refs=claim_refs,
                novelty=novelty,
                scope=scope,
                limitations=limitations,
            )
        if finding.assessment is not requested_assessment and downgrade_code is None:
            downgrade_code, prewrite_message = _finding_downgrade_guidance(
                requested_assessment=requested_assessment,
                resulting_assessment=finding.assessment,
                kind=kind,
                subject_ref=subject_ref,
                claim_refs=claim_refs,
                observations=evidence,
                observation_refs=refs,
            )
            if downgrade_code is None:
                downgrade_code = "downgraded_by_provenance_guard"
                prewrite_message = (
                    "The requested assessment was constrained by the finding provenance "
                    "rules before the version was saved. Review its cited observations."
                )
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            observation_refs=tuple(item.id for item in evidence),
            output={
                "finding_id": str(finding.id),
                "finding_version": finding.version,
                "assessment": finding.assessment.value,
                "assessment_requested": requested_assessment.value,
                "assessment_recorded": finding.assessment.value,
                "assessment_downgrade_code": downgrade_code,
                "assessment_prewrite_message": prewrite_message,
                "kind": finding.kind.value,
            },
            message=prewrite_message or "Operator finding saved as a versioned draft.",
        )

    async def result_for_replay(
        self, arguments: Mapping[str, Any], investigation_id: UUID
    ) -> Mapping[str, Any] | None:
        """Rebuild the bounded finding result from its canonical saved version."""
        try:
            action = arguments["action"]
            if action == "revise":
                previous_id = UUID(str(arguments["finding_id"]))
                previous = await self._findings.get(previous_id)
                if previous is None or previous.investigation_id != investigation_id:
                    return None
                kind = previous.kind
                subject_ref = arguments.get("subject_ref")
                if subject_ref is None:
                    subject_ref = _subject_from_scope(previous.scope)
                expected_version = int(arguments["expected_finding_version"])
            elif action == "propose":
                previous_id = None
                kind = FindingKind(arguments["kind"])
                subject_ref = arguments.get("subject_ref")
                expected_version = 0
            else:
                return None

            normalized_subject = _normalize_subject_ref(subject_ref)
            expected_scope = _scope_with_subject_ref(
                normalized_subject, str(arguments.get("scope", ""))
            )
            expected_refs = tuple(
                dict.fromkeys(UUID(str(item)) for item in arguments["observation_refs"])
            )
            expected_claims = tuple(dict.fromkeys(str(item) for item in arguments["claim_refs"]))
            statement = str(arguments["statement"])
        except (KeyError, TypeError, ValueError):
            return None

        candidates = tuple(
            item
            for item in await self._findings.list_for_investigation(investigation_id)
            if item.kind is kind
            and item.statement == statement
            and item.scope == expected_scope
            and item.observation_refs == expected_refs
            and item.claim_refs == expected_claims
            and (
                (previous_id is None and item.version == 1 and item.supersedes is None)
                or (
                    previous_id is not None
                    and item.version == expected_version + 1
                    and item.supersedes == previous_id
                )
            )
        )
        if len(candidates) != 1:
            return None
        finding = candidates[0]
        requested_assessment = FindingAssessment(arguments["assessment"])
        evidence = await self._load_observations(investigation_id, expected_refs)
        downgrade_code, _message = _finding_downgrade_guidance(
            requested_assessment=requested_assessment,
            resulting_assessment=finding.assessment,
            kind=kind,
            subject_ref=normalized_subject,
            claim_refs=expected_claims,
            observations=evidence,
            observation_refs=expected_refs,
        )
        return {
            "finding_id": str(finding.id),
            "finding_version": finding.version,
            "assessment": finding.assessment.value,
            "assessment_requested": requested_assessment.value,
            "assessment_recorded": finding.assessment.value,
            "assessment_downgrade_code": downgrade_code,
            "kind": finding.kind.value,
        }

    async def _load_observations(
        self, investigation_id: UUID, refs: tuple[UUID, ...]
    ) -> tuple[EvidenceObservation, ...]:
        items: list[EvidenceObservation] = []
        for ref in refs:
            item = await self._observations.get(ref)
            if item is None:
                raise ValueError(f"Observation {ref} does not exist")
            if item.investigation_id != investigation_id:
                raise ValueError(f"Observation {ref} belongs to another investigation")
            items.append(item)
        return tuple(items)

    async def _classify_novelty(
        self,
        investigation_id: UUID,
        *,
        kind: FindingKind,
        assessment: FindingAssessment,
        statement: str,
        subject_ref: str | None,
        claim_refs: tuple[str, ...],
        observation_refs: tuple[UUID, ...],
    ) -> Any:
        if kind is FindingKind.NEW_SAMPLE and subject_ref:
            candidate_kind = NoveltyCandidateKind.SAMPLE
            value, object_type, object_id = subject_ref, "sample", subject_ref
        elif kind is FindingKind.CLAIM_VERIFICATION and claim_refs:
            candidate_kind = NoveltyCandidateKind.CLAIM
            value, object_type, object_id = statement, "claim", claim_refs[0]
        elif subject_ref:
            candidate_kind = NoveltyCandidateKind.IOC
            value, object_type, object_id = subject_ref, kind.value, subject_ref
        else:
            candidate_kind = NoveltyCandidateKind.BEHAVIOUR
            value, object_type, object_id = statement, kind.value, statement[:256]
        return await ArticleNoveltyService(
            observations=self._observations,
            investigations=self._investigations,
            article_snapshots=self._article_snapshots,
        ).classify(
            investigation_id=investigation_id,
            candidate=NoveltyCandidate(
                investigation_id=investigation_id,
                kind=candidate_kind,
                value=value,
                object_type=object_type,
                object_id=object_id,
                details={"statement": statement},
                claim_id=claim_refs[0]
                if kind is FindingKind.CLAIM_VERIFICATION and claim_refs
                else None,
                claim_assessment=assessment if kind is FindingKind.CLAIM_VERIFICATION else None,
                observation_refs=observation_refs,
            ),
        )


def register_operator_finding_tools(
    registry: ToolRegistry,
    *,
    findings: FindingRepository,
    observations: EvidenceObservationRepository,
    investigations: Any,
    article_snapshots: ArticleSnapshotPort,
) -> None:
    adapter = OperatorFindingToolAdapter(
        findings=findings,
        observations=observations,
        investigations=investigations,
        article_snapshots=article_snapshots,
    )
    base_properties = {
        "action": {"type": "string", "enum": ["propose", "revise"]},
        "kind": {"type": "string", "enum": [item.value for item in FindingKind]},
        "finding_id": _UUID_SCHEMA,
        "expected_finding_version": {"type": "integer", "minimum": 1},
        "assessment": {"type": "string", "enum": [item.value for item in FindingAssessment]},
        "statement": {"type": "string", "minLength": 1, "maxLength": 8000},
        "subject_ref": {"type": ["string", "null"], "maxLength": 256},
        "observation_refs": {"type": "array", "maxItems": 128, "items": _UUID_SCHEMA},
        "claim_refs": {
            "type": "array",
            "maxItems": 128,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "novelty": {
            "type": ["string", "null"],
            "enum": [*(item.value for item in NoveltyClass), None],
        },
        "scope": {"type": "string", "maxLength": 2000},
        "limitations": {
            "type": "array",
            "maxItems": 128,
            "items": {"type": "string", "maxLength": 2000},
        },
    }
    common_required = [
        "action",
        "assessment",
        "statement",
        "subject_ref",
        "observation_refs",
        "claim_refs",
        "novelty",
        "scope",
        "limitations",
    ]
    registry.register(
        ToolDefinition(
            name="finding.propose",
            schema_version=OPERATOR_FINDING_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {**base_properties, "action": {"const": "propose"}},
                "required": ["action", "kind", *common_required],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=("Findings remain drafts and cite persisted observations.",),
            capability_key="operator.write",
        ),
        adapter,
    )
    registry.register(
        ToolDefinition(
            name="finding.revise",
            schema_version=OPERATOR_FINDING_SCHEMA_VERSION,
            input_schema={
                "type": "object",
                "properties": {**base_properties, "action": {"const": "revise"}},
                "required": [
                    "action",
                    "finding_id",
                    "expected_finding_version",
                    *common_required[1:],
                ],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={},
            confidentiality=ToolConfidentiality.CONFIDENTIAL,
            limitations=("Finding revisions are append-only and remain drafts.",),
            capability_key="operator.write",
        ),
        adapter,
    )


def _scope_with_subject_ref(subject_ref: str | None, scope: str) -> str:
    clean = scope.strip()
    if not subject_ref:
        return clean
    line = f"{_SUBJECT_SCOPE_PREFIX}{subject_ref}"
    if clean.startswith(_SUBJECT_SCOPE_PREFIX):
        clean = clean.split("\n", 1)[1] if "\n" in clean else ""
    return f"{line}\n{clean}" if clean else line


def _subject_from_scope(scope: str) -> str | None:
    first, _, _rest = scope.partition("\n")
    return first[len(_SUBJECT_SCOPE_PREFIX) :] if first.startswith(_SUBJECT_SCOPE_PREFIX) else None


def _normalize_subject_ref(subject_ref: str | None) -> str | None:
    if subject_ref is None:
        return None
    normalized = subject_ref.strip()
    return normalized.lower() if _is_sample_hash(normalized) else normalized


def _is_sample_hash(value: str | None) -> bool:
    return (
        value is not None
        and len(value) in {32, 40, 64}
        and all(char in "0123456789abcdefABCDEF" for char in value)
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


def _finding_downgrade_guidance(
    *,
    requested_assessment: FindingAssessment,
    resulting_assessment: FindingAssessment,
    kind: FindingKind,
    subject_ref: str | None,
    claim_refs: tuple[str, ...],
    observations: tuple[EvidenceObservation, ...],
    observation_refs: tuple[UUID, ...],
) -> tuple[str | None, str | None]:
    """Explain a predictable provenance downgrade without echoing evidence values."""
    if (
        requested_assessment is not FindingAssessment.SUPPORTED
        or resulting_assessment is FindingAssessment.SUPPORTED
    ):
        return None, None

    if kind in _OBJECT_IDENTIFIED_KINDS:
        if subject_ref is None or not subject_ref.strip():
            return (
                "downgraded_no_matching_subject",
                "Before saving a supported finding, provide subject_ref for the object and "
                "cite an observation whose object_id matches it. Without subject_ref, the "
                "assessment is recorded as inconclusive.",
            )
        if not observation_refs:
            return (
                "downgraded_without_observation",
                "Before saving a supported finding, cite an observation from this investigation "
                "whose object_id matches subject_ref; without a cited observation, the "
                "assessment is recorded as inconclusive.",
            )
        if not any(item.object_id == subject_ref for item in observations):
            return (
                "downgraded_no_matching_observation",
                "Before saving a supported finding, cite an observation from this investigation "
                "whose object_id matches subject_ref; the recorded assessment is qualified "
                "when cited observations do not match.",
            )
    if kind is FindingKind.CLAIM_VERIFICATION and (
        not claim_refs
        or not any(item.kind is not EvidenceKind.CLAIM_SOURCE for item in observations)
    ):
        return (
            "downgraded_claim_without_supporting_observation",
            "Before saving a supported claim verification, provide a claim reference and cite "
            "a non-source observation from this investigation.",
        )
    if kind is FindingKind.NEW_SAMPLE and not _is_sample_hash(subject_ref):
        return (
            "downgraded_invalid_sample_subject",
            "Before saving a supported new-sample finding, provide a valid sample hash in "
            "subject_ref and cite matching evidence.",
        )
    if not observation_refs:
        return (
            "downgraded_without_observation",
            "Before saving a supported finding, cite an observation from this investigation; "
            "supported requires persisted evidence.",
        )
    return (
        "downgraded_by_provenance_guard",
        "The requested assessment was constrained by the finding provenance rules before the "
        "version was saved. Review its cited observations.",
    )


__all__ = [
    "ClaimKnowledge",
    "ClaimVerdict",
    "FindingDowngradeReason",
    "FindingService",
]
