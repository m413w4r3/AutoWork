"""Deterministic subject-scoping policy and artifact lifecycle for L3b."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_extraction import (
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
    RelevanceClassification,
    RelevanceClassificationItemV1,
    RelevanceDecisionProvenance,
    RelevanceProjectionV1,
    RelevanceReasonCode,
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
        malicious_role = _contains_marker(text, _MALICIOUS_ROLE_MARKERS)
        if value in _GENERIC_FILENAMES and not malicious_role:
            return decision(RelevanceClassification.CONTEXT, RelevanceReasonCode.GENERIC_FILENAME)
        if _EMAIL.search(value) and not malicious_role:
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
        if not scope_match:
            return decision(
                RelevanceClassification.INDETERMINATE,
                RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
            )
        if primary_core:
            return decision(
                RelevanceClassification.DIRECT,
                RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
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
        "projection_schema_version": 1,
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
    projection = RelevanceProjectionV1(
        subject_id=snapshot.subject_id,
        production_input_hash=snapshot.input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        input_hash=relevance_projection_input_hash(
            snapshot, extraction, classifier_version=selected_classifier.version
        ),
        extraction_evidence_refs=extraction_evidence_refs_v1(extraction),
        classifications=selected_classifier.classify(snapshot, extraction, evidence),
        classifier_version=selected_classifier.version,
    )
    validate_relevance_projection_lineage(
        projection, extraction, extraction_hash=canonical_extraction_hash(extraction)
    )
    return projection


class RelevanceProjectionExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REUSED = "reused"


@dataclass(frozen=True, slots=True)
class RelevanceProjectionExecution:
    status: RelevanceProjectionExecutionStatus
    artifact: ProductionArtifact
    projection: RelevanceProjectionV1


async def persist_relevance_projection_in_uow(
    uow: Any,
    run: ProductionRun,
    snapshot: ProductionInputSnapshot,
    projection: RelevanceProjectionV1,
    artifact_store: ProductionArtifactStore,
    *,
    mark_downstream_stale: bool = True,
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
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._classifier = classifier or DeterministicRelevanceClassifier()

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
        projection = build_relevance_projection(snapshot, extraction, classifier=self._classifier)
        async with self._uow_factory() as uow:
            execution = await persist_relevance_projection_in_uow(
                uow, run, snapshot, projection, self._artifact_store
            )
            if execution.status is RelevanceProjectionExecutionStatus.SUCCEEDED:
                await uow.commit()
            return execution
