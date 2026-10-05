"""Deterministic subject-scoping policy and artifact lifecycle for L3b."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_reuse import (
    ProductionArtifactReuseService,
    cross_run_reuse_allowed,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_relevance_model import (
    ModelRelevanceClassifier,
    ModelRelevanceProposalExecution,
    RelevanceProposalStatus,
)
from cti_app.application.production_synthesis import (
    build_synthesis_access_policy,
    canonical_extraction_hash,
)
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ProductionExtractionV1,
    production_extraction_from_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceTier,
)
from cti_app.domain.production_relevance import (
    DEFAULT_RELEVANCE_CLASSIFIER_VERSION,
    RELEVANCE_PROJECTION_POLICY_VERSION,
    RELEVANCE_PROJECTION_SCHEMA_VERSION,
    RelevanceClassification,
    RelevanceClassificationItemV1,
    RelevanceDecisionProvenance,
    RelevanceProjectionV1,
    RelevanceProposalRejectionReason,
    RelevanceProposalRejectionV1,
    RelevanceReasonCode,
    RelevanceSourcePairRelation,
    RelevanceSourcePairRelationV1,
    relevance_projection_from_json,
    relevance_projection_to_json,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
    extraction_evidence_elements,
    extraction_evidence_refs_v1,
)

RELEVANCE_CLASSIFIER_VERSION = DEFAULT_RELEVANCE_CLASSIFIER_VERSION
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[A-Za-z]{2,}\b")
_GENERIC_FILENAMES = frozenset(
    {
        "main.py",
        "index.py",
        "app.py",
        "server.py",
        "index.js",
        "app.js",
        "server.js",
        "index.html",
        "config.json",
        "readme.md",
        "requirements.txt",
        "package.json",
    }
)
_COUNTER_ANALYSIS_TECHNIQUE_PATTERNS = (
    ("OP_RETURN", re.compile(r"(?<![a-z0-9])op[ _-]*return(?![a-z0-9])", re.I)),
    ("Bitcoin", re.compile(r"(?<![a-z0-9])bitcoin(?![a-z0-9])", re.I)),
    ("Ethereum", re.compile(r"(?<![a-z0-9])ethereum(?![a-z0-9])", re.I)),
    ("EVM", re.compile(r"(?<![a-z0-9])evm(?![a-z0-9])", re.I)),
    ("JSON-RPC", re.compile(r"(?<![a-z0-9])json[ _-]*rpc(?![a-z0-9])", re.I)),
)
_BROAD_SCOPE_WORDS = frozenset(
    {
        "actor",
        "campaign",
        "case",
        "dead",
        "drop",
        "iran",
        "iranian",
        "malware",
        "operator",
        "operation",
        "report",
        "subject",
        "threat",
        "using",
        "bitcoin",
        "op",
        "return",
    }
)
_OTHER_ACTOR_MARKERS = (
    "dprk",
    "north korea",
    "north korean",
    "lazarus",
    "necurs",
    "glupteba",
    "clearfake",
    "unc5342",
    "muddywater",
    "arenac2",
)
_COUNTER_MARKERS = (
    "not attributed to",
    "not linked to",
    "no evidence linking",
    "no evidence that",
    "cannot be attributed",
    "cannot link",
    "does not establish",
    "not demonstrated",
    "unattributed",
    "n'est pas attribue",
    "aucune attribution",
    "ne permet pas d'attribuer",
    "aucun lien etabli",
    "lien non demontre",
)
_MALICIOUS_ROLE_MARKERS = (
    "malicious",
    "malware",
    "payload",
    "dropper",
    "backdoor",
    "command and control",
    "c2 server",
    "c2 infrastructure",
    "phishing",
    "used by the actor",
    "used by this actor",
    "communicates with",
    "communicated with",
    "deployed by",
    "downloaded by",
    "hash of the sample",
    "sample hash",
    "used for command",
)


class RelevanceClassifier(Protocol):
    """Classifier port reserved for deterministic or future model proposals."""

    @property
    def version(self) -> str: ...

    def classify(
        self,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        evidence: tuple[tuple[ExtractionEvidenceRefV1, Mapping[str, Any]], ...],
    ) -> tuple[RelevanceClassificationItemV1, ...]: ...


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(_WORD.findall(unaccented))


def _subject_anchor_terms(snapshot: ProductionInputSnapshot) -> frozenset[str]:
    raw = snapshot.actor_or_campaign or snapshot.subject_title
    terms = set(_normalize(raw).split())
    return frozenset(term for term in terms if len(term) >= 4 and term not in _BROAD_SCOPE_WORDS)


def _matches_subject(text: str, anchors: frozenset[str]) -> bool:
    return bool(anchors & set(_normalize(text).split()))


def _contains_marker(text: str, markers: tuple[str, ...]) -> bool:
    normalized = f" {_normalize(text)} "
    return any(f" {_normalize(marker)} " in normalized for marker in markers)


def _item_text(kind: EvidenceKind, payload: Mapping[str, Any]) -> str:
    fields = {
        EvidenceKind.FACT: ("value", "context", "evidence_quote"),
        EvidenceKind.EVENT: ("text", "context", "evidence_quote"),
        EvidenceKind.INDICATOR: ("value", "context", "evidence_quote"),
        EvidenceKind.RULE: ("name", "context", "evidence_quote", "body"),
        EvidenceKind.UNCERTAINTY: ("text",),
    }[kind]
    return " ".join(str(payload.get(key) or "") for key in fields)


def _source_for_ref(extraction: ProductionExtractionV1) -> dict[UUID, Any]:
    return {source.source_document_id: source for source in extraction.sources}


def _is_primary_core(source: Any) -> bool:
    return (
        source.tier is ProductionReferenceTier.CORE
        and source.editorial_role is ProductionEditorialRole.PRIMARY
    )


def _classify_item(
    *,
    snapshot: ProductionInputSnapshot,
    source: Any,
    ref: ExtractionEvidenceRefV1,
    payload: Mapping[str, Any],
    subject_support: tuple[ExtractionEvidenceRefV1, ...],
) -> RelevanceClassificationItemV1:
    kind = ref.kind
    text = _item_text(kind, payload)
    scope_match = _matches_subject(text, _subject_anchor_terms(snapshot))
    primary_core = _is_primary_core(source)
    counter = _contains_marker(text, _COUNTER_MARKERS)
    other_actor = _contains_marker(text, _OTHER_ACTOR_MARKERS)
    support_refs = tuple(sorted({ref, *subject_support}, key=evidence_ref_sort_key))

    def decision(
        classification: RelevanceClassification,
        reason: RelevanceReasonCode,
    ) -> RelevanceClassificationItemV1:
        return RelevanceClassificationItemV1(
            evidence_ref=ref,
            classification=classification,
            reason_code=reason,
            supporting_evidence_refs=support_refs,
            provenance=RelevanceDecisionProvenance.DETERMINISTIC_POLICY,
        )

    if kind is EvidenceKind.INDICATOR:
        value = str(payload.get("value") or "").strip().casefold()
        # The extractor's "confirmed IOC" status already records that the source
        # presents the value as malicious; markers cover contextual wording.
        malicious_role = str(
            payload.get("indicator_status") or ""
        ) == ExtractionIndicatorStatus.CONFIRMED_IOC or _contains_marker(
            text, _MALICIOUS_ROLE_MARKERS
        )
        if value in _GENERIC_FILENAMES:
            return decision(RelevanceClassification.CONTEXT, RelevanceReasonCode.GENERIC_FILENAME)
        if _EMAIL.search(value):
            return decision(RelevanceClassification.CONTEXT, RelevanceReasonCode.FOOTER_CONTACT)
        if other_actor and not scope_match:
            return decision(
                RelevanceClassification.OUT_OF_SCOPE,
                RelevanceReasonCode.EXPLICIT_OTHER_ACTOR,
            )
        if not malicious_role:
            return decision(
                RelevanceClassification.CONTEXT,
                RelevanceReasonCode.MALICIOUS_ROLE_NOT_DEMONSTRATED,
            )
        if primary_core:
            # The primary CORE publication is about this subject: a value it
            # presents as malicious belongs to it unless another actor is named.
            return decision(
                RelevanceClassification.DIRECT,
                RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
            )
        if not scope_match:
            return decision(
                RelevanceClassification.INDETERMINATE,
                RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
            )
        if source.editorial_role is ProductionEditorialRole.CORROBORATION:
            return decision(
                RelevanceClassification.CORROBORATION,
                RelevanceReasonCode.MALICIOUS_SUBJECT_CORROBORATION,
            )
        return decision(
            RelevanceClassification.CONTEXT,
            RelevanceReasonCode.CONTEXT_SOURCE_WITHOUT_RELATION,
        )

    if counter and (
        scope_match or source.editorial_role is ProductionEditorialRole.COUNTER_ANALYSIS
    ):
        return decision(
            RelevanceClassification.COUNTER_INDICATION,
            RelevanceReasonCode.EXPLICIT_COUNTER_ANALYSIS,
        )
    if other_actor and not scope_match:
        return decision(
            RelevanceClassification.OUT_OF_SCOPE,
            RelevanceReasonCode.EXPLICIT_OTHER_ACTOR,
        )
    if source.editorial_role is ProductionEditorialRole.COUNTER_ANALYSIS and scope_match:
        return decision(
            RelevanceClassification.COUNTER_INDICATION,
            RelevanceReasonCode.EXPLICIT_COUNTER_ANALYSIS,
        )
    if primary_core:
        reason = (
            RelevanceReasonCode.SUBJECT_MATCHED_PRIMARY
            if scope_match
            else RelevanceReasonCode.PRIMARY_CORE_DEFAULT
        )
        return decision(RelevanceClassification.DIRECT, reason)
    if source.editorial_role is ProductionEditorialRole.CORROBORATION:
        if scope_match:
            return decision(
                RelevanceClassification.CORROBORATION,
                RelevanceReasonCode.SUBJECT_MATCHED_CORROBORATION,
            )
        return decision(
            RelevanceClassification.INDETERMINATE,
            RelevanceReasonCode.RELATION_NOT_ESTABLISHED,
        )
    if scope_match or source.editorial_role is ProductionEditorialRole.CONTEXT:
        return decision(
            RelevanceClassification.CONTEXT,
            RelevanceReasonCode.CONTEXT_SOURCE_WITHOUT_RELATION,
        )
    return decision(
        RelevanceClassification.INDETERMINATE,
        RelevanceReasonCode.RELATION_NOT_ESTABLISHED,
    )


class DeterministicRelevanceClassifier:
    """Conservative lexical baseline; no provider or external state is used."""

    @property
    def version(self) -> str:
        return RELEVANCE_CLASSIFIER_VERSION

    def classify(
        self,
        snapshot: ProductionInputSnapshot,
        extraction: ProductionExtractionV1,
        evidence: tuple[tuple[ExtractionEvidenceRefV1, Mapping[str, Any]], ...],
    ) -> tuple[RelevanceClassificationItemV1, ...]:
        sources = _source_for_ref(extraction)
        anchors = _subject_anchor_terms(snapshot)
        matched_by_source: dict[UUID, list[ExtractionEvidenceRefV1]] = {}
        for ref, payload in evidence:
            if _matches_subject(_item_text(ref.kind, payload), anchors):
                matched_by_source.setdefault(ref.source_document_id, []).append(ref)
        classifications = []
        for ref, payload in evidence:
            supporting = tuple(
                other for other in matched_by_source.get(ref.source_document_id, ()) if other != ref
            )
            classifications.append(
                _classify_item(
                    snapshot=snapshot,
                    source=sources[ref.source_document_id],
                    ref=ref,
                    payload=payload,
                    subject_support=supporting,
                )
            )
        return tuple(classifications)


def _counter_analysis_overlap_fallback(
    extraction: ProductionExtractionV1,
    evidence: tuple[tuple[ExtractionEvidenceRefV1, Mapping[str, Any]], ...],
    classifications: tuple[RelevanceClassificationItemV1, ...],
) -> tuple[
    Mapping[ExtractionEvidenceRefV1, tuple[ExtractionEvidenceRefV1, ...]],
    tuple[RelevanceSourcePairRelationV1, ...],
]:
    """Link named protocol overlaps in reserve uncertainties to primary claims.

    Only a curated set of protocol/technique names and exact primary IOC values
    can create this fallback. Generic word overlap is intentionally insufficient.
    It changes an uncertain counter-reading into reserve evidence, never a direct
    claim.
    """
    sources = {source.source_document_id: source for source in extraction.sources}
    payloads = dict(evidence)
    decisions = {item.evidence_ref: item for item in classifications}
    direct_claims: list[ExtractionEvidenceRefV1] = []
    for ref, item in decisions.items():
        source = sources[ref.source_document_id]
        if (
            item.classification is RelevanceClassification.DIRECT
            and source.profile.value == "full"
            and source.editorial_role is ProductionEditorialRole.PRIMARY
            and ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT, EvidenceKind.INDICATOR}
        ):
            direct_claims.append(ref)

    def named_tokens(ref: ExtractionEvidenceRefV1, payload: Mapping[str, Any]) -> set[str]:
        text = _item_text(ref.kind, payload)
        tokens = {
            label for label, pattern in _COUNTER_ANALYSIS_TECHNIQUE_PATTERNS if pattern.search(text)
        }
        if ref.kind is EvidenceKind.INDICATOR:
            value = str(payload.get("value") or "").strip()
            if len(value) >= 5 and value.casefold() in text.casefold():
                tokens.add(value.casefold())
        return tokens

    direct_tokens = {ref: named_tokens(ref, payloads[ref]) for ref in direct_claims}
    supporting_for_counter: dict[ExtractionEvidenceRefV1, tuple[ExtractionEvidenceRefV1, ...]] = {}
    relation_refs: dict[tuple[UUID, UUID], set[ExtractionEvidenceRefV1]] = {}
    relation_tokens: dict[tuple[UUID, UUID], set[str]] = {}
    for ref, payload in evidence:
        source = sources[ref.source_document_id]
        if (
            ref.kind is not EvidenceKind.UNCERTAINTY
            or source.editorial_role is not ProductionEditorialRole.COUNTER_ANALYSIS
        ):
            continue
        counter_text = _item_text(ref.kind, payload)
        counter_tokens = {
            label
            for label, pattern in _COUNTER_ANALYSIS_TECHNIQUE_PATTERNS
            if pattern.search(counter_text)
        }
        if not counter_tokens:
            continue
        matches = [
            (direct_ref, counter_tokens & direct_tokens[direct_ref])
            for direct_ref in direct_claims
            if counter_tokens & direct_tokens[direct_ref]
        ]
        if not matches:
            continue
        matches.sort(key=lambda entry: (-len(entry[1]), evidence_ref_sort_key(entry[0])))
        direct_ref, overlap = matches[0]
        supporting_for_counter[ref] = (direct_ref,)
        ordered_source_ids = sorted(
            {ref.source_document_id, direct_ref.source_document_id},
            key=str,
        )
        pair = (ordered_source_ids[0], ordered_source_ids[1])
        relation_refs.setdefault(pair, set()).update((ref, direct_ref))
        relation_tokens.setdefault(pair, set()).update(overlap)

    relations = tuple(
        RelevanceSourcePairRelationV1(
            relation=RelevanceSourcePairRelation.COUNTER_INDICATION,
            reason=(
                "Counter-analysis uncertainty shares the named technique/protocol "
                f"{', '.join(sorted(relation_tokens[pair], key=str.casefold))} with a "
                "primary direct claim. Treat it as a qualification; it does not prove "
                "the same transactions or attribution."
            ),
            supporting_evidence_refs=tuple(sorted(relation_refs[pair], key=evidence_ref_sort_key)),
            provenance=RelevanceDecisionProvenance.DETERMINISTIC_POLICY,
        )
        for pair in sorted(relation_refs, key=lambda value: tuple(map(str, value)))
    )
    return supporting_for_counter, relations


def subject_relevance_evidence_counts(
    projection: RelevanceProjectionV1,
) -> dict[str, int]:
    """Count narrative facts/events for the production substance gate."""
    narrative = tuple(
        item
        for item in projection.classifications
        if item.evidence_ref.kind in {EvidenceKind.FACT, EvidenceKind.EVENT}
    )
    return {
        # Corroboration is independently supported subject evidence for this gate.
        "direct_count": sum(
            item.classification
            in {RelevanceClassification.DIRECT, RelevanceClassification.CORROBORATION}
            for item in narrative
        ),
        "context_count": sum(
            item.classification is RelevanceClassification.CONTEXT for item in narrative
        ),
        "out_of_scope_count": sum(
            item.classification is RelevanceClassification.OUT_OF_SCOPE for item in narrative
        ),
    }


def production_evidence_gate_details(
    counts: Mapping[str, int],
    *,
    minimum: int,
    projection_artifact_id: UUID,
    override_active: bool,
) -> dict[str, Any] | None:
    """Return a review payload only when current evidence is below the gate."""
    if minimum < 0:
        raise ValueError("Production evidence minimum cannot be negative")
    direct_count = counts["direct_count"]
    if minimum == 0 or direct_count >= minimum or override_active:
        return None
    return {
        "direct_count": direct_count,
        "minimum": minimum,
        "context_count": counts["context_count"],
        "out_of_scope_count": counts["out_of_scope_count"],
        "projection_artifact_id": str(projection_artifact_id),
    }


def relevance_projection_input_hash(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    *,
    classifier_version: str = RELEVANCE_CLASSIFIER_VERSION,
) -> str:
    payload = {
        "production_input_hash": snapshot.input_hash,
        "subject_id": str(snapshot.subject_id),
        "extraction_hash": canonical_extraction_hash(extraction),
        "projection_schema_version": RELEVANCE_PROJECTION_SCHEMA_VERSION,
        "policy_version": RELEVANCE_PROJECTION_POLICY_VERSION,
        "classifier_version": classifier_version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_relevance_projection(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
    *,
    classifier: RelevanceClassifier | None = None,
    source_pair_relations: tuple[RelevanceSourcePairRelationV1, ...] = (),
    model_proposal_rejections: tuple[RelevanceProposalRejectionV1, ...] = (),
) -> RelevanceProjectionV1:
    if snapshot.subject_id != extraction.subject_id:
        raise ValueError("Projection snapshot and extraction subjects differ")
    selected_classifier = classifier or DeterministicRelevanceClassifier()
    evidence_by_ref: dict[ExtractionEvidenceRefV1, Mapping[str, Any]] = {}
    for ref, payload in extraction_evidence_elements(extraction):
        evidence_by_ref.setdefault(ref, payload)
    evidence = tuple(
        (ref, evidence_by_ref[ref]) for ref in sorted(evidence_by_ref, key=evidence_ref_sort_key)
    )
    classifications = selected_classifier.classify(snapshot, extraction, evidence)
    counter_supporting, fallback_relations = _counter_analysis_overlap_fallback(
        extraction, evidence, classifications
    )
    classifications = tuple(
        RelevanceClassificationItemV1(
            evidence_ref=item.evidence_ref,
            classification=RelevanceClassification.COUNTER_INDICATION,
            reason_code=RelevanceReasonCode.EXPLICIT_COUNTER_ANALYSIS,
            supporting_evidence_refs=tuple(
                sorted(
                    {item.evidence_ref, *counter_supporting[item.evidence_ref]},
                    key=evidence_ref_sort_key,
                )
            ),
            provenance=RelevanceDecisionProvenance.DETERMINISTIC_POLICY,
        )
        if item.evidence_ref in counter_supporting
        else item
        for item in classifications
    )
    supplied_pairs = {item.source_pair for item in source_pair_relations}
    source_pair_relations = (
        *source_pair_relations,
        *(item for item in fallback_relations if item.source_pair not in supplied_pairs),
    )
    projection = RelevanceProjectionV1(
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        input_hash=relevance_projection_input_hash(
            snapshot, extraction, classifier_version=selected_classifier.version
        ),
        extraction_evidence_refs=extraction_evidence_refs_v1(extraction),
        classifications=classifications,
        source_pair_relations=source_pair_relations,
        model_proposal_rejections=model_proposal_rejections,
        classifier_version=selected_classifier.version,
    )
    validate_relevance_projection_lineage(
        projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
    )
    return projection


class RelevanceProjectionExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REUSED = "reused"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class RelevanceProjectionExecution:
    status: RelevanceProjectionExecutionStatus
    artifact: ProductionArtifact | None
    projection: RelevanceProjectionV1
    model_calls: int = 0
    model_run_id: UUID | None = None
    invocation_hash: str | None = None
    parse_identity: str | None = None
    error_code: str | None = None
    error: str | None = None
    details: Mapping[str, Any] | None = None


async def persist_relevance_projection_in_uow(
    uow: Any,
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    projection: RelevanceProjectionV1,
    artifact_store: ProductionArtifactStore,
    *,
    mark_downstream_stale: bool = True,
    artifact_metadata: Mapping[str, Any] | None = None,
) -> RelevanceProjectionExecution:
    """Persist or reuse a projection in a caller-owned transaction."""
    if run.subject_id != snapshot.subject_id or projection.subject_id != snapshot.subject_id:
        raise ValueError("Projection persistence subject identities differ")
    stage = ProductionArtifactStage.RELEVANCE_PROJECTION
    payload = relevance_projection_to_json(projection)
    canonical_bytes = ProductionArtifactStore.canonical_json_bytes(payload)
    current = await uow.production_artifacts.get_current(run.id, stage.value)
    if (
        current is not None
        and current.status is ProductionArtifactStatus.VERIFIED
        and current.input_hash == projection.input_hash
        and current.canonical_blob_id is not None
        and await artifact_store.read_bytes(current.canonical_blob_id) == canonical_bytes
    ):
        stored_projection = relevance_projection_from_json(
            await artifact_store.read_json(current.canonical_blob_id)
        )
        return RelevanceProjectionExecution(
            RelevanceProjectionExecutionStatus.REUSED,
            current,
            stored_projection,
        )
    history = await uow.production_artifacts.list_for_run(run.id)
    version = (
        max(
            (artifact.version for artifact in history if artifact.stage is stage),
            default=0,
        )
        + 1
    )
    _, canonical_id, _ = await artifact_store.store_stage_payloads(canonical=payload)
    if canonical_id is None:
        raise ValueError("Canonical relevance projection blob was not stored")
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=snapshot.subject_id,
        stage=stage,
        version=version,
        input_hash=projection.input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_id,
        metadata={
            "schema_version": projection.schema_version,
            "policy_version": projection.policy_version,
            "classifier_version": projection.classifier_version,
            "projection_hash": projection.projection_hash,
            "extraction_hash": projection.extraction_hash,
            "classification_count": len(projection.classifications),
            "indeterminate_count": sum(
                item.classification is RelevanceClassification.INDETERMINATE
                for item in projection.classifications
            ),
            "subject_evidence_counts": subject_relevance_evidence_counts(projection),
            "model_proposal_rejection_count": len(projection.model_proposal_rejections),
            "model_proposal_rejection_codes": sorted(
                {item.reason_code.value for item in projection.model_proposal_rejections}
            ),
            "source_pair_relation_count": len(projection.source_pair_relations),
            "counter_analysis_fallback_relation_count": sum(
                item.provenance is RelevanceDecisionProvenance.DETERMINISTIC_POLICY
                and item.relation is RelevanceSourcePairRelation.COUNTER_INDICATION
                for item in projection.source_pair_relations
            ),
            **dict(artifact_metadata or {}),
        },
    )
    await uow.production_artifacts.append(artifact)
    if mark_downstream_stale:
        await uow.production_artifacts.mark_downstream_stale(run.id, stage.value)
    return RelevanceProjectionExecution(
        RelevanceProjectionExecutionStatus.SUCCEEDED,
        artifact,
        projection,
    )


class ProductionRelevanceProjectionService:
    """Build and persist one subject projection as a canonical run artifact."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        classifier: RelevanceClassifier | None = None,
        *,
        model_gateway: Any | None = None,
        model_enabled: bool = True,
        model_classifier: ModelRelevanceClassifier | None = None,
        artifact_reuse: ProductionArtifactReuseService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._artifact_reuse = artifact_reuse
        self._classifier = classifier or DeterministicRelevanceClassifier()
        self._model_classifier = None
        if model_enabled:
            self._model_classifier = model_classifier or (
                ModelRelevanceClassifier(model_gateway) if model_gateway is not None else None
            )

    async def execute(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        extraction_artifact: ProductionArtifact,
    ) -> RelevanceProjectionExecution:
        if (
            extraction_artifact.stage is not ProductionArtifactStage.EXTRACTION
            or extraction_artifact.production_run_id != run.id
            or extraction_artifact.subject_id != snapshot.subject_id
            or extraction_artifact.canonical_blob_id is None
        ):
            raise ValueError("Projection requires the current canonical EXTRACTION artifact")
        if run.subject_id != snapshot.subject_id:
            raise ValueError("Projection run and snapshot subjects differ")
        extraction = production_extraction_from_json(
            await self._artifact_store.read_json(extraction_artifact.canonical_blob_id)
        )
        if extraction.subject_id != snapshot.subject_id:
            raise ValueError("Projection extraction subject differs from its snapshot")
        baseline_projection = build_relevance_projection(
            snapshot, extraction, classifier=self._classifier
        )
        requested_projection = (
            build_relevance_projection(snapshot, extraction, classifier=self._model_classifier)
            if self._model_classifier is not None
            else baseline_projection
        )
        reused = await self._reuse_exact(run, requested_projection)
        if reused is not None:
            return reused
        projection = requested_projection
        proposal: ModelRelevanceProposalExecution | None = None
        artifact_metadata: dict[str, Any] = {}
        if self._model_classifier is not None:
            try:
                async with self._uow_factory() as policy_uow:
                    access_policy = await build_synthesis_access_policy(
                        snapshot,
                        extraction,
                        policy_uow.source_documents,
                        policy_uow.source_collections,
                    )
            except (AttributeError, TypeError, ValueError) as exc:
                return await self._persist_classifier_fallback(
                    run,
                    snapshot,
                    baseline_projection,
                    error_code="relevance_classifier_access_policy_unavailable",
                    reason=str(exc),
                )
            if access_policy.do_not_submit or not access_policy.external_llm_allowed:
                return await self._persist_classifier_fallback(
                    run,
                    snapshot,
                    baseline_projection,
                    error_code="relevance_classifier_policy_blocked",
                    reason="The source access policy forbids a model submission.",
                    details={
                        "do_not_submit": access_policy.do_not_submit,
                        "external_llm_allowed": access_policy.external_llm_allowed,
                    },
                )
            proposal = await self._model_classifier.propose(
                run, snapshot, extraction, access_policy
            )
            if proposal.status is RelevanceProposalStatus.NEEDS_REVIEW:
                if (
                    proposal.reconciliation_required
                    or proposal.error_code == PRODUCTION_RECONCILIATION_ERROR_CODE
                ):
                    return RelevanceProjectionExecution(
                        status=RelevanceProjectionExecutionStatus.NEEDS_REVIEW,
                        artifact=None,
                        projection=requested_projection,
                        model_calls=proposal.model_calls,
                        model_run_id=proposal.model_run_id,
                        invocation_hash=proposal.invocation_hash,
                        parse_identity=proposal.parse_identity,
                        error_code=proposal.error_code,
                        error=proposal.error,
                        details=proposal.details,
                    )
                return await self._persist_classifier_fallback(
                    run,
                    snapshot,
                    baseline_projection,
                    error_code=proposal.error_code or "relevance_classifier_model_call_failed",
                    reason=proposal.error or "The relevance classifier proposal is unavailable.",
                    details=proposal.details,
                    model_proposal_rejections=proposal.rejections,
                    model_proposal_warnings=proposal.warnings,
                    model_calls=proposal.model_calls,
                    model_run_id=proposal.model_run_id,
                    invocation_hash=proposal.invocation_hash,
                    parse_identity=proposal.parse_identity,
                )
            projection = self._merge_model_proposals(projection, extraction, proposal)
            artifact_metadata = {
                "model_classifier_enabled": True,
                "model_calls": proposal.model_calls,
                "model_run_id": str(proposal.model_run_id) if proposal.model_run_id else None,
                "model_invocation_hash": proposal.invocation_hash,
                "model_parse_identity": proposal.parse_identity,
                "model_proposal_rejection_count": len(projection.model_proposal_rejections),
                "model_proposal_rejection_codes": sorted(
                    {item.reason_code.value for item in projection.model_proposal_rejections}
                ),
                "model_proposal_warnings": list(proposal.warnings),
                "source_pair_relation_count": len(projection.source_pair_relations),
                "counter_analysis_fallback_relation_count": sum(
                    item.provenance is RelevanceDecisionProvenance.DETERMINISTIC_POLICY
                    and item.relation is RelevanceSourcePairRelation.COUNTER_INDICATION
                    for item in projection.source_pair_relations
                ),
            }
        async with self._uow_factory() as uow:
            execution = await persist_relevance_projection_in_uow(
                uow,
                run,
                snapshot,
                projection,
                self._artifact_store,
                artifact_metadata=artifact_metadata,
            )
            if execution.status is RelevanceProjectionExecutionStatus.SUCCEEDED:
                await uow.commit()
            if proposal is None:
                return execution
            return replace(
                execution,
                model_calls=proposal.model_calls,
                model_run_id=proposal.model_run_id,
                invocation_hash=proposal.invocation_hash,
                parse_identity=proposal.parse_identity,
                details={
                    **dict(proposal.details),
                    "model_proposal_rejection_count": len(projection.model_proposal_rejections),
                    "model_proposal_rejection_codes": sorted(
                        {item.reason_code.value for item in projection.model_proposal_rejections}
                    ),
                    "model_proposal_warnings": list(proposal.warnings),
                    "source_pair_relation_count": len(projection.source_pair_relations),
                },
            )

    async def _persist_classifier_fallback(
        self,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        baseline: RelevanceProjectionV1,
        *,
        error_code: str,
        reason: str,
        details: Mapping[str, Any] | None = None,
        model_proposal_rejections: tuple[RelevanceProposalRejectionV1, ...] = (),
        model_proposal_warnings: tuple[str, ...] = (),
        model_calls: int = 0,
        model_run_id: UUID | None = None,
        invocation_hash: str | None = None,
        parse_identity: str | None = None,
    ) -> RelevanceProjectionExecution:
        fallback = {"error_code": error_code, "reason": reason}
        baseline = replace(
            baseline,
            model_proposal_rejections=model_proposal_rejections,
        )
        async with self._uow_factory() as uow:
            execution = await persist_relevance_projection_in_uow(
                uow,
                run,
                snapshot,
                baseline,
                self._artifact_store,
                artifact_metadata={
                    "model_classifier_fallback": fallback,
                    "model_proposal_rejection_count": len(model_proposal_rejections),
                    "model_proposal_rejection_codes": sorted(
                        {item.reason_code.value for item in model_proposal_rejections}
                    ),
                    "model_proposal_warnings": list(model_proposal_warnings),
                    "source_pair_relation_count": len(baseline.source_pair_relations),
                },
            )
            if execution.artifact is not None:
                execution.artifact.metadata = {
                    **execution.artifact.metadata,
                    "model_classifier_fallback": fallback,
                }
                if execution.status is RelevanceProjectionExecutionStatus.REUSED:
                    execution = replace(
                        execution, status=RelevanceProjectionExecutionStatus.SUCCEEDED
                    )
            if execution.status is RelevanceProjectionExecutionStatus.SUCCEEDED:
                await uow.commit()
        return replace(
            execution,
            model_calls=model_calls,
            model_run_id=model_run_id,
            invocation_hash=invocation_hash,
            parse_identity=parse_identity,
            details={
                **dict(details or {}),
                "model_classifier_fallback": fallback,
                "model_proposal_rejection_count": len(model_proposal_rejections),
                "model_proposal_rejection_codes": sorted(
                    {item.reason_code.value for item in model_proposal_rejections}
                ),
                "model_proposal_warnings": list(model_proposal_warnings),
            },
        )

    async def _reuse_exact(
        self, run: ProductionRun, projection: RelevanceProjectionV1
    ) -> RelevanceProjectionExecution | None:
        """Reuse an identical verified projection before any model submission.

        The projection input hash covers the frozen subject input, the exact
        extraction and the classifier identity (prompt, contract and parser
        versions), so a hit never changes the lineage of later stages.
        """
        if self._artifact_reuse is None:
            return None
        stage = ProductionArtifactStage.RELEVANCE_PROJECTION
        reuse = await self._artifact_reuse.find_or_reuse(
            run=run,
            stage=stage,
            input_hash=projection.input_hash,
            allow_cross_run=cross_run_reuse_allowed(run, stage),
        )
        if reuse is None or reuse.artifact.canonical_blob_id is None:
            return None
        stored = relevance_projection_from_json(
            await self._artifact_store.read_json(reuse.artifact.canonical_blob_id)
        )
        if stored.input_hash != projection.input_hash:
            return None
        return RelevanceProjectionExecution(
            RelevanceProjectionExecutionStatus.REUSED, reuse.artifact, stored
        )

    @staticmethod
    def _merge_model_proposals(
        baseline: RelevanceProjectionV1,
        extraction: ProductionExtractionV1,
        proposal: ModelRelevanceProposalExecution,
    ) -> RelevanceProjectionV1:
        evidence = dict(extraction_evidence_elements(extraction))
        decisions = {item.evidence_ref: item for item in baseline.classifications}
        rejections = list(proposal.rejections)
        fallback_supporting, fallback_relations = _counter_analysis_overlap_fallback(
            extraction,
            tuple(sorted(evidence.items(), key=lambda entry: evidence_ref_sort_key(entry[0]))),
            baseline.classifications,
        )
        model_relation_pairs = {item.source_pair for item in proposal.source_pair_relations}
        fallback_pair_by_ref = {
            ref: relation.source_pair
            for relation in fallback_relations
            for ref in relation.supporting_evidence_refs
            if ref in fallback_supporting
        }
        seen: set[ExtractionEvidenceRefV1] = set()
        for item in proposal.classifications:
            if item.evidence_ref in seen:
                rejections.append(
                    RelevanceProposalRejectionV1(
                        item.block_id,
                        RelevanceProposalRejectionReason.DUPLICATE_TARGET,
                        item.raw_sha256,
                    )
                )
                continue
            seen.add(item.evidence_ref)
            payload = evidence[item.evidence_ref]
            if item.evidence_ref.kind is EvidenceKind.INDICATOR:
                value = str(payload.get("value") or "").strip().casefold()
                if value in _GENERIC_FILENAMES:
                    rejections.append(
                        RelevanceProposalRejectionV1(
                            item.block_id,
                            RelevanceProposalRejectionReason.GENERIC_FILENAME_GUARD,
                            item.raw_sha256,
                        )
                    )
                    continue
                if _EMAIL.search(value):
                    rejections.append(
                        RelevanceProposalRejectionV1(
                            item.block_id,
                            RelevanceProposalRejectionReason.FOOTER_CONTACT_GUARD,
                            item.raw_sha256,
                        )
                    )
                    continue
            baseline_decision = decisions[item.evidence_ref]
            fallback_pair = fallback_pair_by_ref.get(item.evidence_ref)
            if (
                fallback_pair is not None
                and fallback_pair not in model_relation_pairs
                and item.classification
                not in {
                    RelevanceClassification.CONTEXT,
                    RelevanceClassification.COUNTER_INDICATION,
                }
            ):
                rejections.append(
                    RelevanceProposalRejectionV1(
                        item.block_id,
                        RelevanceProposalRejectionReason.COUNTER_ANALYSIS_RESERVE_GUARD,
                        item.raw_sha256,
                    )
                )
                continue
            supporting = tuple(
                sorted(
                    {
                        item.evidence_ref,
                        *item.supporting_evidence_refs,
                        *fallback_supporting.get(item.evidence_ref, ()),
                    },
                    key=evidence_ref_sort_key,
                )
            )
            if (
                item.evidence_ref.kind is EvidenceKind.INDICATOR
                and item.classification
                in {RelevanceClassification.DIRECT, RelevanceClassification.CORROBORATION}
                and (
                    len(supporting) < 2
                    or (
                        baseline_decision.classification is not RelevanceClassification.DIRECT
                        and not any(
                            ref != item.evidence_ref
                            and ref.source_document_id == item.evidence_ref.source_document_id
                            and decisions[ref].classification
                            in {
                                RelevanceClassification.DIRECT,
                                RelevanceClassification.CORROBORATION,
                            }
                            for ref in item.supporting_evidence_refs
                        )
                    )
                )
            ):
                rejections.append(
                    RelevanceProposalRejectionV1(
                        item.block_id,
                        RelevanceProposalRejectionReason.RELATION_NOT_DOCUMENTED,
                        item.raw_sha256,
                    )
                )
                if baseline_decision.classification is not RelevanceClassification.DIRECT:
                    decisions[item.evidence_ref] = RelevanceClassificationItemV1(
                        evidence_ref=item.evidence_ref,
                        classification=RelevanceClassification.INDETERMINATE,
                        reason_code=RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
                        supporting_evidence_refs=supporting,
                        provenance=RelevanceDecisionProvenance.MODEL_PROPOSAL,
                    )
                continue
            if (
                item.evidence_ref.kind is EvidenceKind.INDICATOR
                and baseline_decision.classification is RelevanceClassification.DIRECT
                and item.classification is RelevanceClassification.INDETERMINATE
            ):
                rejections.append(
                    RelevanceProposalRejectionV1(
                        item.block_id,
                        RelevanceProposalRejectionReason.CORE_PRIMARY_DEFAULT_GUARD,
                        item.raw_sha256,
                    )
                )
                continue
            decisions[item.evidence_ref] = RelevanceClassificationItemV1(
                evidence_ref=item.evidence_ref,
                classification=item.classification,
                reason_code=item.reason_code,
                supporting_evidence_refs=supporting,
                provenance=RelevanceDecisionProvenance.MODEL_PROPOSAL,
            )

        relation_by_pair = {item.source_pair: item for item in baseline.source_pair_relations}
        relation_by_pair.update({item.source_pair: item for item in proposal.source_pair_relations})
        projection = replace(
            baseline,
            classifications=tuple(decisions.values()),
            source_pair_relations=tuple(relation_by_pair.values()),
            model_proposal_rejections=tuple(rejections),
        )
        validate_relevance_projection_lineage(
            projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
        )
        return projection
