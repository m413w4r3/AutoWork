"""Versioned, replayable subject relevance projections over canonical extraction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    evidence_ref_sort_key,
    extraction_evidence_refs_v1,
)

RELEVANCE_PROJECTION_SCHEMA_VERSION = 1
RELEVANCE_PROJECTION_POLICY_VERSION = "subject-relevance-deterministic-v1"
DEFAULT_RELEVANCE_CLASSIFIER_VERSION = "deterministic-subject-scope-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RelevanceClassification(StrEnum):
    DIRECT = "direct"
    CORROBORATION = "corroboration"
    CONTEXT = "context"
    COUNTER_INDICATION = "counter_indication"
    OUT_OF_SCOPE = "out_of_scope"
    INDETERMINATE = "indeterminate"


class RelevanceReasonCode(StrEnum):
    PRIMARY_CORE_DEFAULT = "primary_core_default"
    SUBJECT_MATCHED_PRIMARY = "subject_matched_primary"
    SUBJECT_MATCHED_CORROBORATION = "subject_matched_corroboration"
    EXPLICIT_COUNTER_ANALYSIS = "explicit_counter_analysis"
    EXPLICIT_OTHER_ACTOR = "explicit_other_actor"
    RELATION_NOT_ESTABLISHED = "relation_not_established"
    CONTEXT_SOURCE_WITHOUT_RELATION = "context_source_without_relation"
    GENERIC_FILENAME = "generic_filename"
    FOOTER_CONTACT = "footer_contact"
    MALICIOUS_ROLE_NOT_DEMONSTRATED = "malicious_role_not_demonstrated"
    SUBJECT_LINK_NOT_DEMONSTRATED = "subject_link_not_demonstrated"
    MALICIOUS_SUBJECT_RELATION = "malicious_subject_relation"
    MALICIOUS_SUBJECT_CORROBORATION = "malicious_subject_corroboration"
    EXPLICIT_SUBJECT_DENIAL = "explicit_subject_denial"


class RelevanceDecisionProvenance(StrEnum):
    DETERMINISTIC_POLICY = "deterministic_policy"
    MODEL_PROPOSAL = "model_proposal"
    HUMAN_REVIEW = "human_review"


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _ref_payload(ref: ExtractionEvidenceRefV1) -> dict[str, str]:
    return {
        "source_document_id": str(ref.source_document_id),
        "kind": ref.kind.value,
        "evidence_key": ref.evidence_key,
    }


def _ref_from_json(value: Any) -> ExtractionEvidenceRefV1:
    if not isinstance(value, dict) or set(value) != {
        "source_document_id",
        "kind",
        "evidence_key",
    }:
        raise ValueError("Projection evidence reference fields are invalid")
    try:
        return ExtractionEvidenceRefV1(
            source_document_id=UUID(value["source_document_id"]),
            kind=EvidenceKind(value["kind"]),
            evidence_key=_sha256(value["evidence_key"], "Projection evidence key"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Projection evidence reference is invalid") from exc


@dataclass(frozen=True, slots=True)
class RelevanceClassificationItemV1:
    evidence_ref: ExtractionEvidenceRefV1
    classification: RelevanceClassification
    reason_code: RelevanceReasonCode
    supporting_evidence_refs: tuple[ExtractionEvidenceRefV1, ...]
    provenance: RelevanceDecisionProvenance = RelevanceDecisionProvenance.DETERMINISTIC_POLICY

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_ref, ExtractionEvidenceRefV1):
            raise ValueError("Projection item evidence reference is invalid")
        if not isinstance(self.classification, RelevanceClassification):
            raise ValueError("Projection classification is invalid")
        if not isinstance(self.reason_code, RelevanceReasonCode):
            raise ValueError("Projection reason code is invalid")
        if not isinstance(self.provenance, RelevanceDecisionProvenance):
            raise ValueError("Projection decision provenance is invalid")
        if (
            not isinstance(self.supporting_evidence_refs, tuple)
            or not self.supporting_evidence_refs
        ):
            raise ValueError("Projection decisions require supporting extraction evidence refs")
        if any(
            not isinstance(ref, ExtractionEvidenceRefV1) for ref in self.supporting_evidence_refs
        ):
            raise ValueError("Projection supporting evidence refs are invalid")
        refs = tuple(sorted(set(self.supporting_evidence_refs), key=evidence_ref_sort_key))
        if self.evidence_ref not in refs:
            raise ValueError("Projection decision must cite its classified extraction item")
        object.__setattr__(self, "supporting_evidence_refs", refs)


@dataclass(frozen=True, slots=True)
class RelevanceProjectionV1:
    subject_id: UUID
    production_input_hash: str
    extraction_hash: str
    input_hash: str
    extraction_evidence_refs: tuple[ExtractionEvidenceRefV1, ...]
    classifications: tuple[RelevanceClassificationItemV1, ...]
    schema_version: int = RELEVANCE_PROJECTION_SCHEMA_VERSION
    policy_version: str = RELEVANCE_PROJECTION_POLICY_VERSION
    classifier_version: str = DEFAULT_RELEVANCE_CLASSIFIER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Projection subject identity must be a UUID")
        _sha256(self.production_input_hash, "Projection production input hash")
        _sha256(self.extraction_hash, "Projection extraction hash")
        _sha256(self.input_hash, "Projection input hash")
        if (
            type(self.schema_version) is not int
            or self.schema_version != RELEVANCE_PROJECTION_SCHEMA_VERSION
        ):
            raise ValueError("Projection schema version is unsupported")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("Projection policy version is unsupported")
        if not isinstance(self.classifier_version, str) or not self.classifier_version.strip():
            raise ValueError("Projection classifier version is invalid")
        if not isinstance(self.extraction_evidence_refs, tuple) or any(
            not isinstance(ref, ExtractionEvidenceRefV1) for ref in self.extraction_evidence_refs
        ):
            raise ValueError("Projection extraction evidence refs are invalid")
        if not isinstance(self.classifications, tuple) or any(
            not isinstance(item, RelevanceClassificationItemV1) for item in self.classifications
        ):
            raise ValueError("Projection classifications are invalid")
        valid_refs = set(self.extraction_evidence_refs)
        if len(valid_refs) != len(self.extraction_evidence_refs):
            raise ValueError("Projection extraction evidence refs must be unique")
        if any(
            not refs <= valid_refs
            for item in self.classifications
            for refs in ({item.evidence_ref}, set(item.supporting_evidence_refs))
        ):
            raise ValueError("Projection decision references unknown extraction evidence")
        classified_refs = [item.evidence_ref for item in self.classifications]
        if len(classified_refs) != len(set(classified_refs)):
            raise ValueError("Projection must classify each extraction item exactly once")
        if set(classified_refs) != valid_refs:
            raise ValueError("Projection must classify every extraction evidence item")
        object.__setattr__(
            self,
            "extraction_evidence_refs",
            tuple(sorted(valid_refs, key=evidence_ref_sort_key)),
        )
        object.__setattr__(
            self,
            "classifications",
            tuple(
                sorted(
                    self.classifications,
                    key=lambda item: evidence_ref_sort_key(item.evidence_ref),
                )
            ),
        )

    def classification_for(
        self, evidence_ref: ExtractionEvidenceRefV1
    ) -> RelevanceClassificationItemV1:
        for item in self.classifications:
            if item.evidence_ref == evidence_ref:
                return item
        raise ValueError("Projection has no classification for extraction evidence")

    @property
    def projection_hash(self) -> str:
        payload = _canonical_json(_projection_payload(self, include_hash=False))
        return hashlib.sha256(payload).hexdigest()


def _projection_payload(
    projection: RelevanceProjectionV1, *, include_hash: bool = True
) -> dict[str, Any]:
    payload = {
        "schema_version": projection.schema_version,
        "policy_version": projection.policy_version,
        "classifier_version": projection.classifier_version,
        "subject_id": str(projection.subject_id),
        "production_input_hash": projection.production_input_hash,
        "extraction_hash": projection.extraction_hash,
        "input_hash": projection.input_hash,
        "extraction_evidence_refs": [
            _ref_payload(ref) for ref in projection.extraction_evidence_refs
        ],
        "classifications": [
            {
                "evidence_ref": _ref_payload(item.evidence_ref),
                "classification": item.classification.value,
                "reason_code": item.reason_code.value,
                "supporting_evidence_refs": [
                    _ref_payload(ref) for ref in item.supporting_evidence_refs
                ],
                "provenance": item.provenance.value,
            }
            for item in projection.classifications
        ],
    }
    if include_hash:
        payload["projection_hash"] = projection.projection_hash
    return payload


def relevance_projection_to_json(projection: RelevanceProjectionV1) -> dict[str, Any]:
    if not isinstance(projection, RelevanceProjectionV1):
        raise ValueError("Expected a RelevanceProjectionV1")
    return _projection_payload(projection)


def relevance_projection_from_json(payload: Any) -> RelevanceProjectionV1:
    keys = {
        "schema_version",
        "policy_version",
        "classifier_version",
        "subject_id",
        "production_input_hash",
        "extraction_hash",
        "input_hash",
        "extraction_evidence_refs",
        "classifications",
        "projection_hash",
    }
    if not isinstance(payload, dict) or set(payload) != keys:
        raise ValueError("Relevance projection fields are invalid")
    if type(payload["schema_version"]) is not int:
        raise ValueError("Projection schema version must be an integer")
    try:
        classifications = tuple(
            RelevanceClassificationItemV1(
                evidence_ref=_ref_from_json(item["evidence_ref"]),
                classification=RelevanceClassification(item["classification"]),
                reason_code=RelevanceReasonCode(item["reason_code"]),
                supporting_evidence_refs=tuple(
                    _ref_from_json(ref) for ref in item["supporting_evidence_refs"]
                ),
                provenance=RelevanceDecisionProvenance(item["provenance"]),
            )
            for item in payload["classifications"]
            if isinstance(item, dict)
            and set(item)
            == {
                "evidence_ref",
                "classification",
                "reason_code",
                "supporting_evidence_refs",
                "provenance",
            }
        )
        if len(classifications) != len(payload["classifications"]):
            raise ValueError("Projection classification fields are invalid")
        projection = RelevanceProjectionV1(
            schema_version=payload["schema_version"],
            policy_version=payload["policy_version"],
            classifier_version=payload["classifier_version"],
            subject_id=UUID(payload["subject_id"]),
            production_input_hash=payload["production_input_hash"],
            extraction_hash=payload["extraction_hash"],
            input_hash=payload["input_hash"],
            extraction_evidence_refs=tuple(
                _ref_from_json(ref) for ref in payload["extraction_evidence_refs"]
            ),
            classifications=classifications,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Relevance projection payload is invalid") from exc
    if projection.projection_hash != _sha256(payload["projection_hash"], "Projection hash"):
        raise ValueError("Relevance projection hash does not match its payload")
    return projection


def validate_relevance_projection_lineage(
    projection: RelevanceProjectionV1,
    extraction: ProductionExtractionV1,
    *,
    extraction_hash: str,
) -> None:
    if projection.subject_id != extraction.subject_id:
        raise ValueError("Projection subject does not match canonical extraction")
    if projection.production_input_hash != extraction.production_input_hash:
        raise ValueError("Projection input does not match canonical extraction")
    if projection.extraction_hash != extraction_hash:
        raise ValueError("Projection extraction hash does not match canonical extraction")
    if set(projection.extraction_evidence_refs) != set(extraction_evidence_refs_v1(extraction)):
        raise ValueError("Projection lineage references differ from canonical extraction")
