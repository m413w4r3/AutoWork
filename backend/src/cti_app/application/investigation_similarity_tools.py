"""Read-only investigation tools for exact sample feature comparisons."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import ceil
from time import monotonic
from typing import Any
from uuid import UUID

from cti_app.application.evidence_recorder import EvidenceRecorder
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
from cti_app.application.sample_similarity import (
    SampleSimilarityError,
    SampleSimilarityService,
    StoredFamilyFeatureComparison,
)
from cti_app.domain.investigation import BudgetDimension, EvidenceKind, ToolInvocationStatus
from cti_app.domain.sample_similarity import (
    FeatureComparison,
    SampleFeaturePresence,
    SignatureCandidate,
)

JSON_SCHEMA_2020_12 = "https://json-schema.org/draft/2020-12/schema"
SIMILARITY_TOOL_NAMES = (
    "samples.compare",
    "features.common",
    "signatures.candidates",
    "features.explain",
)
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_RESULT_ITEMS = 64
_MAX_OUTPUT_TEXT = 512
_MAX_EXPOSED_PRESENCE = 40
_TOOL_LIMITATIONS = (
    "Les outils comparent des SHA-256 exacts et des feature packs déjà persistés; ils ne lisent "
    "ni n'exécutent les octets des échantillons.",
    "Un score, un hash de similarité ou un motif candidat ne prouve pas une appartenance "
    "de famille.",
    "Les motifs ne sont que des propositions structurées; aucune règle YARA n'est créée, testée "
    "ou approuvée ici.",
    "Le résultat complet est un artefact JSON immuable adressé par SHA-256; la sortie outil est "
    "un extrait borné avec une référence de preuve.",
)


def _object_schema(
    properties: Mapping[str, Mapping[str, Any]], required: tuple[str, ...], title: str
) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_2020_12,
        "type": "object",
        "title": title,
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


_SAMPLE_HASHES = {
    "type": "array",
    "minItems": 1,
    "maxItems": 32,
    "uniqueItems": True,
    "items": {"type": "string", "minLength": 64, "maxLength": 64, "pattern": _SHA256_PATTERN},
}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": _MAX_RESULT_ITEMS}
_FEATURE_KEY = {"type": "string", "minLength": 64, "maxLength": 64, "pattern": _SHA256_PATTERN}

SAMPLES_COMPARE_SCHEMA = _object_schema(
    {"sample_sha256s": _SAMPLE_HASHES},
    ("sample_sha256s",),
    "Compare exact samples in the investigation corpus",
)
FEATURES_COMMON_SCHEMA = _object_schema(
    {"sample_sha256s": _SAMPLE_HASHES, "limit": _LIMIT},
    ("sample_sha256s",),
    "List bounded common and discriminating measured features",
)
SIGNATURES_CANDIDATES_SCHEMA = _object_schema(
    {"sample_sha256s": _SAMPLE_HASHES, "limit": _LIMIT},
    ("sample_sha256s",),
    "Propose evidence-backed signature motifs without generating a rule",
)
FEATURES_EXPLAIN_SCHEMA = _object_schema(
    {"sample_sha256s": _SAMPLE_HASHES, "feature_key": _FEATURE_KEY},
    ("sample_sha256s", "feature_key"),
    "Explain one measured feature and its provenance or rejection causes",
)

_TOOL_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "samples.compare": SAMPLES_COMPARE_SCHEMA,
    "features.common": FEATURES_COMMON_SCHEMA,
    "signatures.candidates": SIGNATURES_CANDIDATES_SCHEMA,
    "features.explain": FEATURES_EXPLAIN_SCHEMA,
}


def similarity_tool_capabilities(
    *,
    service: SampleSimilarityService | None,
    recorder: EvidenceRecorder | None,
    sample_associations: SampleAssociationRepository | None = None,
) -> frozenset[str]:
    """Return registry keys enabled by the required comparison and proof ports."""
    if service is None or recorder is None or sample_associations is None:
        return frozenset()
    return frozenset(SIMILARITY_TOOL_NAMES)


def register_similarity_tools(
    registry: ToolRegistry,
    *,
    service: SampleSimilarityService | None,
    sample_associations: SampleAssociationRepository | None,
    recorder: EvidenceRecorder | None,
    max_tool_seconds: int = 30,
    max_result_items: int = _MAX_RESULT_ITEMS,
) -> None:
    """Register four read-only tools backed by one deterministic comparison service."""
    if not 1 <= max_tool_seconds <= 600:
        raise ValueError("max_tool_seconds must be between 1 and 600")
    if not 1 <= max_result_items <= _MAX_RESULT_ITEMS:
        raise ValueError(f"max_result_items must be between 1 and {_MAX_RESULT_ITEMS}")
    for name, schema in _TOOL_SCHEMAS.items():
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=ToolEffect.LOCAL_READ,
                cost_model={BudgetDimension.TOOL_SECONDS: max_tool_seconds},
                confidentiality=ToolConfidentiality.RESTRICTED,
                limitations=_TOOL_LIMITATIONS,
                capability_key=name,
            ),
            _SimilarityToolAdapter(
                service=service,
                sample_associations=sample_associations,
                recorder=recorder,
                tool_name=name,
                max_result_items=max_result_items,
                reserved_seconds=max_tool_seconds,
            ),
        )


class _SimilarityToolAdapter(ToolAdapter):
    def __init__(
        self,
        *,
        service: SampleSimilarityService | None,
        sample_associations: SampleAssociationRepository | None,
        recorder: EvidenceRecorder | None,
        tool_name: str | None = None,
        max_result_items: int,
        reserved_seconds: int,
    ) -> None:
        self.service = service
        self.sample_associations = sample_associations
        self.recorder = recorder
        self.tool_name = tool_name
        self.max_result_items = max_result_items
        self.reserved_seconds = reserved_seconds

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        started_at = monotonic()
        service = self.service
        sample_associations = self.sample_associations
        recorder = self.recorder
        tool_name = self.tool_name
        if service is None or sample_associations is None or recorder is None:
            return ToolResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                message="similarity_dependencies_unavailable",
            )
        if tool_name is None:
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                message="similarity_tool_name_missing",
            )
        sample_hashes = arguments.get("sample_sha256s")
        if not isinstance(sample_hashes, Sequence) or isinstance(sample_hashes, (str, bytes)):
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                message="invalid_sample_hashes",
            )
        try:
            await self._require_investigation_samples(
                sample_associations,
                context.investigation_id,
                sample_hashes,
            )
            stored = await service.compare(
                investigation_id=context.investigation_id,
                sample_sha256s=sample_hashes,
            )
            observation = await recorder.record_existing_blob(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.BINARY_ANALYSIS,
                blob_id=stored.artifact_blob_id,
                sha256=stored.artifact_sha256,
                source="sample_similarity",
                source_version=stored.comparison.schema_version,
                object_type="family_feature_comparison",
                object_id=stored.comparison.sha256,
                limitations=(
                    "La comparaison mesure des traits extraits et ne confirme pas une parenté.",
                    "Les motifs structurés sont des candidats non validés; aucune règle "
                    "n'est publiée.",
                ),
                invocation_ref=None,
                idempotency_key=(
                    f"similarity:{context.idempotency_key}:{tool_name}:{stored.comparison.sha256}"
                )[:512],
            )
        except (SampleSimilarityError, ValueError) as exc:
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                message=f"similarity_comparison_failed:{type(exc).__name__}",
            )
        except Exception as exc:
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                message=f"similarity_evidence_failed:{type(exc).__name__}",
            )
        output = _tool_output(
            tool_name,
            stored,
            feature_key=arguments.get("feature_key"),
            limit=min(_requested_limit(arguments), self.max_result_items),
        )
        status = (
            ToolInvocationStatus.EMPTY
            if output.get("empty") is True
            else ToolInvocationStatus.SUCCEEDED
        )
        return ToolResult(
            status=status,
            observation_refs=(observation.id,),
            budget_consumed={
                BudgetDimension.TOOL_SECONDS: min(
                    self.reserved_seconds,
                    max(1, ceil(monotonic() - started_at)),
                )
            },
            output=output,
            message="sample_similarity_comparison_recorded",
        )

    async def _require_investigation_samples(
        self,
        associations_port: SampleAssociationRepository,
        investigation_id: UUID,
        sample_hashes: Sequence[Any],
    ) -> None:
        if not sample_hashes:
            raise ValueError("at least one exact sample hash is required")
        hashes: set[str] = set()
        for value in sample_hashes:
            if not isinstance(value, str) or len(value) != 64:
                raise ValueError("sample hash is invalid")
            hashes.add(value)
        associations = await associations_port.list_for_investigation(investigation_id)
        associated = {item.sample_sha256 for item in associations}
        if not hashes.issubset(associated):
            raise SampleSimilarityError(
                "sample hashes are not all associated with this investigation"
            )


def _tool_output(
    tool_name: str,
    stored: StoredFamilyFeatureComparison,
    *,
    feature_key: Any,
    limit: int,
) -> dict[str, Any]:
    comparison = stored.comparison
    base = {
        "schema_version": "investigation-similarity-tool-output-v1",
        "comparison_sha256": comparison.sha256,
        "artifact_blob_id": str(stored.artifact_blob_id),
        "artifact_sha256": stored.artifact_sha256,
        "artifact_size": stored.artifact_size,
        "corpus_snapshot_sha256": comparison.corpus_snapshot_sha256,
        "status": comparison.status.value,
        "sample_sha256s": list(comparison.comparison_sample_sha256s),
    }
    if tool_name == "samples.compare":
        class_counts: dict[str, int] = {}
        for feature in comparison.features:
            class_counts[feature.feature_class.value] = (
                class_counts.get(feature.feature_class.value, 0) + 1
            )
        return {
            **base,
            "corpus_sample_count": len(comparison.corpus_sample_sha256s),
            "feature_count": len(comparison.features),
            "feature_class_counts": dict(sorted(class_counts.items())),
            "limits": comparison.limits.to_json(),
            "extractor_versions": [
                item.to_json() for item in comparison.extractor_versions[:_MAX_EXPOSED_PRESENCE]
            ],
            "top_features": [_bounded_feature(feature) for feature in comparison.features[:limit]],
            "recommended_next_tests": list(comparison.recommended_next_tests[:16]),
            "warnings": list(comparison.warnings[:16]),
            "empty": not comparison.features,
        }
    if tool_name == "features.common":
        return {
            **base,
            "features": [_bounded_feature(feature) for feature in comparison.features[:limit]],
            "empty": not comparison.features,
        }
    if tool_name == "signatures.candidates":
        candidates = [
            (feature, candidate)
            for feature in comparison.features
            for candidate in feature.signature_candidates
        ]
        candidates.sort(
            key=lambda pair: (
                not pair[1].qualified,
                -pair[1].positive_support,
                pair[0].rank,
                pair[1].motif_kind.value,
                pair[1].pattern,
            )
        )
        bounded = [
            _bounded_signature_candidate(feature, candidate)
            for feature, candidate in candidates[:limit]
        ]
        return {**base, "candidates": bounded, "empty": not candidates}
    if tool_name == "features.explain":
        if not isinstance(feature_key, str):
            return {**base, "feature": None, "empty": True, "reason": "feature_key_required"}
        feature = comparison.feature(feature_key)
        return {
            **base,
            "feature": _bounded_feature(feature) if feature is not None else None,
            "empty": feature is None,
            "reason": None if feature is not None else "feature_key_not_in_comparison",
        }
    return {**base, "empty": True, "reason": "unsupported_similarity_tool"}


def _bounded_feature(feature: FeatureComparison | None) -> dict[str, Any]:
    if feature is None:
        return {}
    presences = sorted(
        feature.sample_presence,
        key=lambda item: (not item.selected, item.sample_sha256),
    )
    selected = [item for item in presences if item.selected]
    context = [item for item in presences if not item.selected]
    bounded_presence = [*_presence_list(selected), *_presence_list(context[:8])]
    return {
        "feature_key": feature.feature_key,
        "kind": feature.kind.value,
        "value": _truncate(feature.value, _MAX_OUTPUT_TEXT),
        "encoding": feature.encoding,
        "feature_class": feature.feature_class.value,
        "rank": feature.rank,
        "support": {
            "positive": feature.positive_support,
            "positive_sample_count": feature.positive_sample_count,
            "corpus": feature.corpus_support,
            "corpus_sample_count": feature.corpus_sample_count,
            "goodware_occurrence_count": feature.goodware_occurrence_count,
            "comparison_exclusive": feature.comparison_exclusive,
        },
        "sample_presence": bounded_presence,
        "presence_truncated": len(context) > 8,
        "rejection_reasons": [item.value for item in feature.rejection_reasons],
        "rationale": [_truncate(item, 256) for item in feature.rationale[:16]],
        "signature_candidate_count": len(feature.signature_candidates),
        "qualified_signature_candidate_count": sum(
            candidate.qualified for candidate in feature.signature_candidates
        ),
    }


def _presence_list(presences: Sequence[SampleFeaturePresence]) -> list[dict[str, Any]]:
    return [
        {
            "sample_sha256": item.sample_sha256,
            "selected": item.selected,
            "qualification": item.qualification,
            "measured": item.measured,
            "present": item.present,
            "offsets": list(item.offsets[:16]),
            "function_offsets": list(item.function_offsets[:16]),
            "sources": [source.to_json() for source in item.sources[:8]],
        }
        for item in presences[:_MAX_EXPOSED_PRESENCE]
    ]


def _bounded_signature_candidate(
    feature: FeatureComparison, candidate: SignatureCandidate
) -> dict[str, Any]:
    return {
        "feature_key": candidate.feature_key,
        "kind": candidate.motif_kind.value,
        "pattern": _truncate(candidate.pattern, _MAX_OUTPUT_TEXT),
        "qualified": candidate.qualified,
        "positive_support": candidate.positive_support,
        "positive_sample_sha256s": list(candidate.positive_sample_sha256s[:32]),
        "source_offsets": _presence_list(candidate.source_offsets[:32]),
        "rejection_reasons": [item.value for item in candidate.rejection_reasons],
        "feature_class": feature.feature_class.value,
        "rank": feature.rank,
    }


def _requested_limit(arguments: Mapping[str, Any]) -> int:
    value = arguments.get("limit", _MAX_RESULT_ITEMS)
    if isinstance(value, int) and not isinstance(value, bool):
        return max(1, min(value, _MAX_RESULT_ITEMS))
    return _MAX_RESULT_ITEMS


def _truncate(value: str, maximum: int) -> str:
    return value if len(value) <= maximum else f"{value[:maximum]}…"


__all__ = [
    "FEATURES_COMMON_SCHEMA",
    "FEATURES_EXPLAIN_SCHEMA",
    "SAMPLES_COMPARE_SCHEMA",
    "SIGNATURES_CANDIDATES_SCHEMA",
    "SIMILARITY_TOOL_NAMES",
    "register_similarity_tools",
    "similarity_tool_capabilities",
]
