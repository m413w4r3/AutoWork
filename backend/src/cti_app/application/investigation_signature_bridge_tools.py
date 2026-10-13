"""Bridge measured L2 signature candidates through P09 and the rule workbench."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import ceil
from time import monotonic
from typing import Any, Protocol
from uuid import UUID, uuid5

from cti_app.application.evidence_recorder import EvidenceRecorder
from cti_app.application.invariants import InvariantRegistryService
from cti_app.application.investigation_persistence import SampleAssociationRepository
from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.sample_similarity import SampleSimilarityError, SampleSimilarityService
from cti_app.domain.invariants import (
    CodeFeatureProvenance,
    InvariantCategory,
    InvariantType,
    SampleFeatureProvenance,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    ToolInvocationStatus,
)
from cti_app.domain.rule_workbench import (
    RuleConditionStrategy,
    RuleMotif,
    RuleMotifKind,
    RuleMotifProvenanceKind,
)
from cti_app.domain.sample_similarity import (
    FeatureComparison,
    FeatureRejectionReason,
    SignatureCandidate,
    SignatureMotifKind,
    SimilarityFeatureKind,
)

_TOOL_NAME = "rule.propose_from_candidates"
_BRIDGE_NAMESPACE = UUID("ab274b5d-c1d9-5e88-8c9d-7cbca02a4120")
_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_P10_LIMITATION = (
    "P10 n'est pas invoqué par ce pont : aucun motif P10 n'est inclus dans cette exécution. "
    "Pour en utiliser, lancer séparément ProposalConversationService.propose, puis mapper "
    "explicitement ses propositions vers les feature_refs attendues par le RuleFeatureProvider "
    "avant rule.propose ; ce mapping n'est pas fait ici."
)
_SAMPLE_HASHES = {
    "type": "array",
    "minItems": 2,
    "maxItems": 32,
    "uniqueItems": True,
    "items": {"type": "string", "pattern": _SHA256_PATTERN},
}
_FEATURE_KEYS = {
    "type": "array",
    "minItems": 1,
    "maxItems": 32,
    "uniqueItems": True,
    "items": {"type": "string", "pattern": _SHA256_PATTERN},
}
RULE_PROPOSE_FROM_CANDIDATES_SCHEMA: Mapping[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sample_sha256s": _SAMPLE_HASHES,
        "feature_keys": _FEATURE_KEYS,
        "condition": {
            "type": "string",
            "enum": [item.value for item in RuleConditionStrategy],
        },
        "candidate_id": {"type": "string", "pattern": _UUID_PATTERN},
        "parent_version": {"type": "integer", "minimum": 1},
    },
    "required": ["sample_sha256s", "feature_keys", "condition"],
}


class RuleToolExecutionPort(Protocol):
    @property
    def signature_bridge_available(self) -> bool: ...

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]: ...

    async def revise_with_added_motifs(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> tuple[Mapping[str, Any], ToolInvocationStatus, tuple[UUID, ...]]: ...


def signature_bridge_tool_capabilities(
    *,
    service: SampleSimilarityService | None,
    sample_associations: SampleAssociationRepository | None,
    recorder: EvidenceRecorder | None,
    uow_factory: UnitOfWorkFactory | None,
    invariant_registry: InvariantRegistryService | None,
    rule_tools: RuleToolExecutionPort | None,
) -> frozenset[str]:
    if (
        service is None
        or sample_associations is None
        or recorder is None
        or uow_factory is None
        or invariant_registry is None
        or rule_tools is None
        or not rule_tools.signature_bridge_available
    ):
        return frozenset()
    return frozenset({_TOOL_NAME})


def register_signature_bridge_tools(
    registry: ToolRegistry,
    *,
    service: SampleSimilarityService | None,
    sample_associations: SampleAssociationRepository | None,
    recorder: EvidenceRecorder | None,
    uow_factory: UnitOfWorkFactory | None,
    invariant_registry: InvariantRegistryService | None,
    rule_tools: RuleToolExecutionPort | None,
    max_tool_seconds: int = 30,
) -> None:
    """Register the optional L2 → P09 → existing rule-tool bridge."""
    if not 1 <= max_tool_seconds <= 150:
        raise ValueError("max_tool_seconds must be between 1 and 150")
    registry.register(
        ToolDefinition(
            name=_TOOL_NAME,
            schema_version="1",
            input_schema=RULE_PROPOSE_FROM_CANDIDATES_SCHEMA,
            effect=ToolEffect.LOCAL_COMPUTE,
            cost_model={BudgetDimension.TOOL_SECONDS: max_tool_seconds * 4},
            confidentiality=ToolConfidentiality.RESTRICTED,
            limitations=(
                "Le pont ne modifie aucune qualification de sample et ne publie pas de règle.",
                "Les provenances P09 et l'artefact L2 exact restent liés aux motifs YARA.",
                _P10_LIMITATION,
                "Une révision v2 conserve les motifs du parent et appelle rule.compare après test.",
                "Les règles longues, triviales, invalides ou contaminées restent rejetées par "
                "les contrôles L2, P09 et RuleWorkbench.",
            ),
            capability_key=_TOOL_NAME,
        ),
        _SignatureBridgeAdapter(
            service=service,
            sample_associations=sample_associations,
            recorder=recorder,
            uow_factory=uow_factory,
            invariant_registry=invariant_registry,
            rule_tools=rule_tools,
            reserved_seconds=max_tool_seconds * 4,
        ),
    )


class _SignatureBridgeAdapter(ToolAdapter):
    def __init__(
        self,
        *,
        service: SampleSimilarityService | None,
        sample_associations: SampleAssociationRepository | None,
        recorder: EvidenceRecorder | None,
        uow_factory: UnitOfWorkFactory | None,
        invariant_registry: InvariantRegistryService | None,
        rule_tools: RuleToolExecutionPort | None,
        reserved_seconds: int,
    ) -> None:
        self._service = service
        self._sample_associations = sample_associations
        self._recorder = recorder
        self._uow_factory = uow_factory
        self._invariant_registry = invariant_registry
        self._rule_tools = rule_tools
        self._reserved_seconds = reserved_seconds

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started = monotonic()
        if (
            self._service is None
            or self._sample_associations is None
            or self._recorder is None
            or self._uow_factory is None
            or self._invariant_registry is None
            or self._rule_tools is None
            or not self._rule_tools.signature_bridge_available
        ):
            return self._result(
                started,
                ToolInvocationStatus.UNAVAILABLE,
                "signature_bridge_dependencies_unavailable",
            )

        try:
            sample_hashes = _string_sequence(arguments.get("sample_sha256s"))
            feature_keys = _string_sequence(arguments.get("feature_keys"))
            if ("parent_version" in arguments) != ("candidate_id" in arguments):
                raise ValueError("candidate_id_and_parent_version_must_be_paired")
            associations = await self._sample_associations.list_for_investigation(
                context.investigation_id
            )
            associated_hashes = {item.sample_sha256 for item in associations}
            if not set(sample_hashes).issubset(associated_hashes):
                raise ValueError("samples_not_associated_with_investigation")

            stored = await self._service.compare(
                investigation_id=context.investigation_id,
                sample_sha256s=sample_hashes,
            )
            comparison = stored.comparison
            observation = await self._recorder.record_existing_blob(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.BINARY_ANALYSIS,
                blob_id=stored.artifact_blob_id,
                sha256=stored.artifact_sha256,
                source="sample_similarity",
                source_version=comparison.schema_version,
                object_type="family_feature_comparison",
                object_id=comparison.sha256,
                limitations=(
                    "Cette comparaison expose des candidats mesurés et ne confirme pas une "
                    "appartenance de famille.",
                    "La version YARA reste une candidate locale soumise aux tests et à la revue "
                    "humaine existante.",
                ),
                invocation_ref=None,
                idempotency_key=(f"signature-bridge:{context.idempotency_key}:{comparison.sha256}")[
                    :512
                ],
            )
            candidates = _selected_candidates(comparison.features, feature_keys)
            sample_ids = await _sample_ids_by_hash(
                self._uow_factory,
                context.investigation_id,
                comparison.comparison_sample_sha256s,
            )
            decisions: list[dict[str, Any]] = []
            motifs: list[RuleMotif] = []
            for feature, candidate in candidates:
                reasons = _eligibility_reasons(candidate, is_revision="parent_version" in arguments)
                if reasons:
                    decisions.append(
                        {
                            "feature_key": candidate.feature_key,
                            "status": "not_proposed",
                            "reasons": reasons,
                        }
                    )
                    continue
                invariant_type = _invariant_type(feature.kind)
                provenances = _candidate_provenances(feature, candidate)
                positive_ids = tuple(
                    sample_ids[sample_hash]
                    for sample_hash in candidate.positive_sample_sha256s
                    if sample_hash in sample_ids
                )
                if (
                    invariant_type is None
                    or not provenances
                    or len(positive_ids) != len(candidate.positive_sample_sha256s)
                ):
                    decisions.append(
                        {
                            "feature_key": candidate.feature_key,
                            "status": "not_proposed",
                            "reasons": ["p09_source_provenance_unresolvable"],
                        }
                    )
                    continue
                proposal = await self._invariant_registry.propose(
                    investigation_id=context.investigation_id,
                    sample_ids=positive_ids,
                    type=invariant_type,
                    category=(
                        InvariantCategory.CODE_SEQUENCE
                        if invariant_type is InvariantType.CODE_NGRAM
                        else InvariantCategory.UNKNOWN
                    ),
                    pattern=candidate.pattern,
                    provenances=provenances,
                    positive_sample_confirmed=False,
                )
                if not proposal.accepted or proposal.invariant is None:
                    rejection = proposal.rejection
                    decisions.append(
                        {
                            "feature_key": candidate.feature_key,
                            "status": "p09_rejected",
                            "reason": rejection.reason if rejection is not None else "unknown",
                        }
                    )
                    continue
                invariant = proposal.invariant
                motif = _rule_motif(
                    feature=feature,
                    candidate=candidate,
                    invariant_id=invariant.id,
                    observation_id=observation.id,
                    positive_support=invariant.positive_support,
                    benign_prevalence=invariant.benign_prevalence,
                    goodware_occurrence_count=invariant.banality_occurrence_count,
                )
                if motif is None:
                    decisions.append(
                        {
                            "feature_key": candidate.feature_key,
                            "status": "not_proposed",
                            "reasons": ["motif_bounds_or_provenance_invalid"],
                            "p09_invariant_id": str(invariant.id),
                        }
                    )
                    continue
                motifs.append(motif)
                decisions.append(
                    {
                        "feature_key": candidate.feature_key,
                        "status": "p09_proposed",
                        "p09_invariant_id": str(invariant.id),
                        "positive_sample_sha256s": list(candidate.positive_sample_sha256s),
                        "offsets_and_functions": _provenance_summary(provenances),
                    }
                )

            if not motifs:
                return self._result(
                    started,
                    ToolInvocationStatus.EMPTY,
                    "no_p09_approved_signature_motifs",
                    output={
                        "comparison_sha256": comparison.sha256,
                        "comparison_observation_ref": str(observation.id),
                        "candidates": decisions,
                        "sample_membership_changed": False,
                        "p10_status": "not_invoked",
                        "limitations": [_P10_LIMITATION],
                    },
                    observation_refs=(observation.id,),
                )

            motif_payloads = [_motif_payload(motif) for motif in motifs]
            condition = arguments["condition"]
            candidate_id = arguments.get("candidate_id") or str(
                uuid5(
                    _BRIDGE_NAMESPACE,
                    ":".join(
                        (
                            str(context.investigation_id),
                            context.idempotency_key,
                            comparison.sha256,
                            condition,
                            ",".join(sorted(feature_keys)),
                        )
                    ),
                )
            )
            meta = {
                "classification": "candidate_signature",
                "source": "AutoWork L2 and P09",
            }
            if "parent_version" in arguments:
                (
                    rule_output,
                    rule_status,
                    rule_refs,
                ) = await self._rule_tools.revise_with_added_motifs(
                    {
                        "candidate_id": candidate_id,
                        "parent_version": arguments["parent_version"],
                        "motifs": motif_payloads,
                        "condition": condition,
                        "meta": meta,
                    },
                    context,
                )
            else:
                rule_output, rule_status, rule_refs = await self._rule_tools.execute(
                    "rule.propose",
                    {
                        "motifs": motif_payloads,
                        "condition": condition,
                        "meta": meta,
                        "candidate_id": candidate_id,
                    },
                    context,
                )

            output: dict[str, Any] = {
                "schema_version": "signature-rule-bridge-output-v1",
                "comparison_sha256": comparison.sha256,
                "comparison_observation_ref": str(observation.id),
                "candidates": decisions,
                "rule": dict(rule_output),
                "sample_membership_changed": False,
                "p10_status": "not_invoked",
                "limitations": [_P10_LIMITATION],
            }
            observation_refs = [observation.id, *rule_refs]
            result_status = rule_status
            if "parent_version" in arguments and rule_output.get("version"):
                version_label = str(rule_output["version"])
                version_b = _version_number(version_label)
                if version_b is not None:
                    try:
                        (
                            comparison_output,
                            comparison_status,
                            comparison_refs,
                        ) = await self._rule_tools.execute(
                            "rule.compare",
                            {
                                "candidate_id": arguments["candidate_id"],
                                "version_a": arguments["parent_version"],
                                "version_b": version_b,
                            },
                            context,
                        )
                        output["before_after"] = dict(comparison_output)
                        output["before_after_status"] = comparison_status.value
                        observation_refs.extend(comparison_refs)
                        if comparison_status is not ToolInvocationStatus.SUCCEEDED:
                            result_status = ToolInvocationStatus.PARTIAL
                    except Exception as exc:
                        result_status = ToolInvocationStatus.PARTIAL
                        output["before_after"] = {
                            "status": "unavailable",
                            "reason": type(exc).__name__,
                            "required_tool": "rule.compare",
                        }
                        output["before_after_status"] = ToolInvocationStatus.UNAVAILABLE.value
                else:
                    result_status = ToolInvocationStatus.PARTIAL
                    output["before_after"] = {
                        "status": "unavailable",
                        "reason": "version_b_unparseable",
                        "required_tool": "rule.compare",
                    }
                    output["before_after_status"] = ToolInvocationStatus.UNAVAILABLE.value
            return self._result(
                started,
                result_status,
                "signature_candidates_proposed_to_rule_workbench",
                output=output,
                observation_refs=tuple(dict.fromkeys(observation_refs)),
            )
        except (SampleSimilarityError, KeyError, TypeError, ValueError) as exc:
            return self._result(
                started,
                ToolInvocationStatus.FAILED,
                f"signature_bridge_failed:{type(exc).__name__}",
            )
        except Exception as exc:
            return self._result(
                started,
                ToolInvocationStatus.FAILED,
                f"signature_bridge_failed:{type(exc).__name__}",
            )

    def _result(
        self,
        started: float,
        status: ToolInvocationStatus,
        message: str,
        *,
        output: Mapping[str, Any] | None = None,
        observation_refs: tuple[UUID, ...] = (),
    ) -> ToolResult:
        return ToolResult(
            status=status,
            message=message,
            output=output or {},
            observation_refs=observation_refs,
            budget_consumed={
                BudgetDimension.TOOL_SECONDS: min(
                    self._reserved_seconds,
                    max(1, ceil(monotonic() - started)),
                )
            },
        )


def _selected_candidates(
    features: Sequence[FeatureComparison], feature_keys: Sequence[str]
) -> tuple[tuple[FeatureComparison, SignatureCandidate], ...]:
    selected = set(feature_keys)
    result = [
        (feature, candidate)
        for feature in features
        if feature.feature_key in selected
        for candidate in feature.signature_candidates
    ]
    found = {candidate.feature_key for _feature, candidate in result}
    if found != selected:
        raise ValueError("feature_key_not_in_comparison")
    return tuple(
        sorted(
            result,
            key=lambda pair: (
                pair[0].feature_key,
                pair[1].motif_kind.value,
                pair[1].pattern,
            ),
        )
    )


def _eligibility_reasons(candidate: SignatureCandidate, *, is_revision: bool) -> list[str]:
    if candidate.qualified:
        return []
    permitted = {FeatureRejectionReason.GOODWARE_PREVALENCE_UNKNOWN}
    if is_revision:
        permitted.update(
            {
                FeatureRejectionReason.INSUFFICIENT_POSITIVE_SUPPORT,
                FeatureRejectionReason.INSUFFICIENT_COMPARATIVE_SUPPORT,
            }
        )
    if candidate.rejection_reasons and set(candidate.rejection_reasons).issubset(permitted):
        return []
    return [item.value for item in candidate.rejection_reasons]


def _invariant_type(kind: SimilarityFeatureKind) -> InvariantType | None:
    return {
        SimilarityFeatureKind.STRING: InvariantType.LITERAL_STRING,
        SimilarityFeatureKind.IMPORT: InvariantType.IMPORT_NAME,
        SimilarityFeatureKind.EXPORT: InvariantType.EXPORT_NAME,
        SimilarityFeatureKind.SECTION: InvariantType.SECTION_NAME,
        SimilarityFeatureKind.CODE_NGRAM: InvariantType.CODE_NGRAM,
    }.get(kind)


def _candidate_provenances(
    feature: FeatureComparison, candidate: SignatureCandidate
) -> tuple[SampleFeatureProvenance | CodeFeatureProvenance, ...]:
    result: list[SampleFeatureProvenance | CodeFeatureProvenance] = []
    if feature.kind is SimilarityFeatureKind.CODE_NGRAM:
        for presence in candidate.source_offsets:
            for source in presence.sources:
                if (
                    not presence.selected
                    or not presence.present
                    or source.source_kind != "code_feature_set"
                    or source.offset is None
                    or source.function_offset is None
                    or source.extractor_version is None
                ):
                    continue
                try:
                    result.append(
                        CodeFeatureProvenance(
                            sample_sha256=presence.sample_sha256,
                            function_address=source.function_offset,
                            offset=source.offset,
                            disassembler_version=source.extractor_version,
                        )
                    )
                except ValueError:
                    continue
    else:
        grouped: dict[tuple[str, str], set[int]] = {}
        for presence in candidate.source_offsets:
            if not presence.selected or not presence.present:
                continue
            for source in presence.sources:
                if (
                    source.source_kind != "sample_feature_pack"
                    or source.source_id is None
                    or source.offset is None
                    or _uuid_or_none(source.source_id) is None
                ):
                    continue
                grouped.setdefault((presence.sample_sha256, source.source_id), set()).add(
                    source.offset
                )
        for (sample_sha256, feature_id), offsets in sorted(grouped.items()):
            try:
                result.append(
                    SampleFeatureProvenance(
                        sample_sha256=sample_sha256,
                        feature_id=feature_id,
                        offsets=tuple(sorted(offsets)),
                    )
                )
            except ValueError:
                continue
    unique = {str(item.as_canonical_dict()): item for item in result}
    provenances = tuple(unique[key] for key in sorted(unique))
    source_samples = {item.sample_sha256 for item in provenances}
    return provenances if set(candidate.positive_sample_sha256s).issubset(source_samples) else ()


def _rule_motif(
    *,
    feature: FeatureComparison,
    candidate: SignatureCandidate,
    invariant_id: UUID,
    observation_id: UUID,
    positive_support: int | None,
    benign_prevalence: int | None,
    goodware_occurrence_count: int | None,
) -> RuleMotif | None:
    if len(candidate.pattern.encode("utf-8")) > 512:
        return None
    if candidate.motif_kind is SignatureMotifKind.HEX:
        tokens = candidate.pattern.split()
        wildcard_indices = [index for index, token in enumerate(tokens) if "?" in token]
        if not tokens or len(tokens) > 64 or len(wildcard_indices) * 2 > len(tokens):
            return None
        longest_wildcard_run = current_run = 0
        for token in tokens:
            current_run = current_run + 1 if "?" in token else 0
            longest_wildcard_run = max(longest_wildcard_run, current_run)
        if longest_wildcard_run > 4:
            return None
        motif_kind = RuleMotifKind.HEX
    else:
        motif_kind = (
            RuleMotifKind.WIDE
            if candidate.motif_kind is SignatureMotifKind.WIDE
            else RuleMotifKind.STRING
        )
    representative = next(
        (
            item
            for item in candidate.source_offsets
            if item.selected and item.sample_sha256 in candidate.positive_sample_sha256s
        ),
        None,
    )
    if representative is None:
        return None
    offsets = tuple(representative.offsets)
    if not offsets:
        offsets = tuple(
            source.offset for source in representative.sources if source.offset is not None
        )
    if not offsets:
        return None
    rationale = (
        f"L2 feature_key={candidate.feature_key}; kind={feature.kind.value}; "
        f"positive_support={candidate.positive_support}; "
        f"P09 invariant={invariant_id}; full per-sample locations are in the immutable "
        "comparison artifact and P09 provenances."
    )
    return RuleMotif(
        kind=motif_kind,
        value=candidate.pattern,
        provenance_kind=RuleMotifProvenanceKind.P09_INVARIANT,
        provenance_id=str(invariant_id),
        observation_refs=(observation_id,),
        sample_sha256=representative.sample_sha256,
        offsets=offsets[:64],
        rationale=rationale[:1_000],
        positive_support=positive_support,
        benign_prevalence=benign_prevalence,
        goodware_occurrence_count=goodware_occurrence_count,
    )


async def _sample_ids_by_hash(
    uow_factory: UnitOfWorkFactory,
    investigation_id: UUID,
    requested_hashes: Sequence[str],
) -> dict[str, UUID]:
    requested = set(requested_hashes)
    async with uow_factory() as uow:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise ValueError("investigation_not_found")
        samples = await uow.samples.list_for_subject(investigation.subject_id)
        result: dict[str, UUID] = {}
        for sample in samples:
            blob = await uow.blobs.get(sample.blob_id)
            sample_hash = blob.descriptor.sha256 if blob is not None else None
            if sample_hash in requested:
                result[sample_hash] = sample.id
    return result


def _provenance_summary(
    provenances: Sequence[SampleFeatureProvenance | CodeFeatureProvenance],
) -> list[dict[str, Any]]:
    return [item.as_canonical_dict() for item in provenances[:64]]


def _motif_payload(motif: RuleMotif) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": motif.kind.value,
        "value": motif.value,
        "provenance_kind": motif.provenance_kind.value,
        "provenance_id": motif.provenance_id,
        "observation_refs": [str(item) for item in motif.observation_refs],
        "rationale": motif.rationale,
    }
    if motif.sample_sha256 is not None:
        result["sample_sha256"] = motif.sample_sha256
    if motif.offsets is not None:
        result["offsets"] = list(motif.offsets)
    if motif.positive_support is not None:
        result["positive_support"] = motif.positive_support
    if motif.benign_prevalence is not None:
        result["benign_prevalence"] = motif.benign_prevalence
    if motif.goodware_occurrence_count is not None:
        result["goodware_occurrence_count"] = motif.goodware_occurrence_count
    return result


def _version_number(value: str) -> int | None:
    if not value.startswith("v"):
        return None
    try:
        parsed = int(value[1:])
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _string_sequence(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("expected_string_array")
    if not value or any(not isinstance(item, str) for item in value):
        raise ValueError("expected_non_empty_string_array")
    return tuple(value)


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "RULE_PROPOSE_FROM_CANDIDATES_SCHEMA",
    "register_signature_bridge_tools",
    "signature_bridge_tool_capabilities",
]
