"""Build the canonical publication document from verified production artifacts."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentValidationError,
    canonical_editorial_enrichment_hash,
    canonical_synthesis_hash,
    validate_editorial_enrichment,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.semantic_annotation import (
    SemanticAnnotator,
    semantic_entities_from_extraction,
)
from cti_app.domain.legitimate_services import LEGITIMATE_SERVICES_POLICY_VERSION
from cti_app.domain.media_assets import media_asset_id
from cti_app.domain.production import ExtractionProfile, ProductionInputSnapshot
from cti_app.domain.production_editorial_enrichment import (
    ChartSpecV1,
    DiagramSpecV1,
    EditorialEnrichmentV1,
    SourceFigureCandidateV1,
    SourceFigureDecision,
    SourceFigureInclusionStatus,
    TableSpecV1,
    editorial_enrichment_evidence_refs,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionReuseState,
    ProductionExtractionV1,
    decode_indicator_section_paths,
    is_production_extraction_hash,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    production_source_criticality,
)
from cti_app.domain.production_relevance import (
    RelevanceClassification,
    RelevanceProjectionV1,
    RelevanceReasonCode,
    validate_relevance_projection_lineage,
)
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    ExtractionEvidenceRefV1,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    evidence_ref_sort_key,
    extraction_evidence_elements,
    extraction_evidence_refs_v1,
    synthesis_evidence_refs,
)
from cti_app.domain.publication import (
    PUBLICATION_IOC_ARTIFACT_TYPES,
    ArtifactType,
    PublicationAssemblyErrorCode,
    PublicationEvidenceKind,
    PublicationEvidenceRefV1,
    PublicationIndicatorGroupV1,
    PublicationIndicatorV1,
    PublicationParagraphV1,
    PublicationReferenceEntryV1,
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
    indicator_source_ids,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
    PublicationChartV1,
    PublicationDiagramV1,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationSourceFigureV1,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    PublicationTableV1,
    order_publication_references,
    publication_document_text_anchors,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SEMANTIC_ANNOTATION_SCHEMA_VERSION,
    SemanticTextV1,
)
from cti_app.infrastructure.legitimate_services import load_legitimate_services


class PublicationAssemblyValidationError(ValueError):
    def __init__(self, code: PublicationAssemblyErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _PublicationNarrativeProjection:
    lead: tuple[PublicationParagraphV1, ...]
    sections: tuple[PublicationSectionV1, ...]
    timeline: tuple[PublicationTimelineEntryV1, ...]
    uncertainties: tuple[PublicationUncertaintyV1, ...]
    used_source_document_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class _PublicationContentProjection:
    lead: tuple[PublicationParagraphV1, ...]
    sections: tuple[PublicationSectionV1, ...]
    timeline: tuple[PublicationTimelineEntryV1, ...]
    uncertainties: tuple[PublicationUncertaintyV1, ...]
    indicators: tuple[PublicationIndicatorGroupV1, ...]
    original_indicators: tuple[PublicationIndicatorGroupV1, ...]
    used_source_document_ids: frozenset[UUID]


def _project_synthesis_publication(
    *,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    include_timeline: bool = True,
) -> _PublicationNarrativeProjection:
    """Project validated canonical synthesis narrative without changing editorial order."""
    _validate_synthesis_evidence_refs(extraction=extraction, synthesis=synthesis)
    used_source_document_ids: set[UUID] = set()

    def evidence_refs(
        refs: tuple[ExtractionEvidenceRefV1, ...],
    ) -> tuple[PublicationEvidenceRefV1, ...]:
        projected = tuple(
            PublicationEvidenceRefV1(
                source_document_id=ref.source_document_id,
                kind=PublicationEvidenceKind(ref.kind.value),
                evidence_key=ref.evidence_key,
            )
            for ref in refs
        )
        used_source_document_ids.update(ref.source_document_id for ref in projected)
        return projected

    def paragraph(value: SynthesisParagraphV1) -> PublicationParagraphV1:
        return PublicationParagraphV1(
            text=value.text,
            evidence_refs=evidence_refs(value.evidence_refs),
        )

    lead = tuple(paragraph(value) for value in synthesis.lead)
    sections = tuple(
        PublicationSectionV1(
            kind=PublicationSectionKind(section.kind.value),
            heading=section.heading,
            paragraphs=tuple(paragraph(value) for value in section.paragraphs),
        )
        for section in synthesis.sections
    )
    timeline = (
        tuple(
            PublicationTimelineEntryV1(
                event_date=entry.event_date,
                date_text=entry.date_text,
                text=entry.text,
                evidence_refs=evidence_refs(entry.evidence_refs),
            )
            for entry in synthesis.timeline
        )
        if include_timeline
        else ()
    )
    # Projected uncertainties inform the synthesis conclusion and review
    # diagnostics; publishing their source list separately would duplicate the
    # analysis instead of presenting it in prose.
    uncertainties: tuple[PublicationUncertaintyV1, ...] = ()
    return _PublicationNarrativeProjection(
        lead=lead,
        sections=sections,
        timeline=timeline,
        uncertainties=uncertainties,
        used_source_document_ids=frozenset(used_source_document_ids),
    )


_MAIN_IOC_REASONS = frozenset(
    {
        RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
        RelevanceReasonCode.MALICIOUS_SUBJECT_CORROBORATION,
    }
)
_UNDEMONSTRATED_IOC_REASONS = frozenset(
    {
        RelevanceReasonCode.SUBJECT_LINK_NOT_DEMONSTRATED,
        RelevanceReasonCode.MALICIOUS_ROLE_NOT_DEMONSTRATED,
    }
)
_MAIN_IOC_CLASSIFICATIONS = frozenset(
    {RelevanceClassification.DIRECT, RelevanceClassification.CORROBORATION}
)
_UNDEMONSTRATED_IOC_CLASSIFICATIONS = frozenset(
    {RelevanceClassification.INDETERMINATE, RelevanceClassification.CONTEXT}
)

# MD5, SHA-1 and SHA-256 digests; any other hash-typed value is noise.
_HASH_VALUE = re.compile(r"[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64}")

type _IocOccurrences = dict[tuple[ArtifactType, str], set[UUID]]


def _indicator_groups(occurrences: _IocOccurrences) -> tuple[PublicationIndicatorGroupV1, ...]:
    grouped: dict[ArtifactType, list[PublicationIndicatorV1]] = defaultdict(list)
    for (artifact_type, value), source_ids in sorted(
        occurrences.items(), key=lambda item: (item[0][0].value, item[0][1])
    ):
        grouped[artifact_type].append(
            PublicationIndicatorV1(
                value=value,
                normalized_value=value,
                artifact_type=artifact_type,
                source_document_ids=tuple(sorted(source_ids, key=str)),
            )
        )
    return tuple(
        PublicationIndicatorGroupV1(artifact_type, tuple(items))
        for artifact_type, items in sorted(grouped.items(), key=lambda item: item[0].value)
    )


def _project_publication_iocs(
    *,
    extraction: ProductionExtractionV1,
    narrative: _PublicationNarrativeProjection,
    relevance_projection: RelevanceProjectionV1 | None = None,
) -> _PublicationContentProjection:
    """Split confirmed IOCs into the subject's IOCs and the original sources' IOCs.

    IOCs that the relevance projection ties to the subject are the subject's IOCs.
    Confirmed IOCs of non-core sources read in full, whose link with the subject is not
    demonstrated, are published apart as original IOCs. Indicators in an explicitly
    identified other case of a primary CORE source are out of scope and omitted. A value
    present in both groups is published once, in the subject's.
    """
    indicator_ref_by_identity = {
        (
            ref.source_document_id,
            str(payload["value"]),
            str(payload["artifact_type"]),
            str(payload["context"]),
            str(payload["evidence_quote"]),
        ): ref
        for ref, payload in extraction_evidence_elements(extraction)
        if ref.kind is EvidenceKind.INDICATOR
    }
    main: _IocOccurrences = defaultdict(set)
    original: _IocOccurrences = defaultdict(set)
    legitimate_services = load_legitimate_services()
    for source in extraction.sources:
        for item in source.indicators:
            if (
                item.indicator_status is not ExtractionIndicatorStatus.CONFIRMED_IOC
                or item.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
            ):
                continue
            destination: _IocOccurrences | None = main
            if relevance_projection is not None:
                decision = relevance_projection.classification_for(
                    indicator_ref_by_identity[
                        (
                            source.source_document_id,
                            item.value,
                            item.artifact_type.value,
                            item.context,
                            item.evidence_quote,
                        )
                    ]
                )
                if (
                    decision.classification in _MAIN_IOC_CLASSIFICATIONS
                    and decision.reason_code in _MAIN_IOC_REASONS
                ):
                    destination = main
                elif (
                    (
                        source.tier is not ProductionReferenceTier.CORE
                        or decode_indicator_section_paths(item.context) is not None
                    )
                    and source.profile is ExtractionProfile.FULL
                    and decision.classification in _UNDEMONSTRATED_IOC_CLASSIFICATIONS
                    and decision.reason_code in _UNDEMONSTRATED_IOC_REASONS
                ):
                    destination = original
                else:
                    destination = None
            if destination is None:
                continue
            try:
                value = normalize_indicator_value(item.value, item.artifact_type)
            except ValueError as exc:
                raise PublicationAssemblyValidationError(
                    PublicationAssemblyErrorCode.VALIDATION_FAILED,
                    "Confirmed IOC cannot be normalized for publication: "
                    f"{item.artifact_type.value} from {source.source_document_id}",
                ) from exc
            if item.artifact_type is ArtifactType.HASH and not _HASH_VALUE.fullmatch(value):
                continue
            if legitimate_services.match_indicator(item.artifact_type, value) is not None:
                continue
            destination[(item.artifact_type, value)].update(item.source_document_ids)
    for identity in main:
        original.pop(identity, None)

    indicators = _indicator_groups(main)
    original_indicators = _indicator_groups(original)
    used_source_document_ids = narrative.used_source_document_ids | indicator_source_ids(
        indicators + original_indicators
    )
    return _PublicationContentProjection(
        lead=narrative.lead,
        sections=narrative.sections,
        timeline=narrative.timeline,
        uncertainties=narrative.uncertainties,
        indicators=indicators,
        original_indicators=original_indicators,
        used_source_document_ids=used_source_document_ids,
    )


def _validate_publication_lineage(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    relevance_projection: RelevanceProjectionV1 | None = None,
) -> None:
    """Require all canonical artifacts to belong to the same frozen inputs."""
    if (
        len(
            {
                snapshot.subject_id,
                references.subject_id,
                extraction.subject_id,
                synthesis.subject_id,
            }
        )
        != 1
    ):
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "Publication artifacts must share the same subject identity",
        )
    if extraction.production_input_hash != snapshot.input_hash:
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "Extraction does not match the production input snapshot",
        )
    if references_corpus_hash(references) != extraction.references_corpus_hash:
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "Extraction does not match the canonical references corpus",
        )
    if synthesis.production_input_hash != snapshot.input_hash:
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "Synthesis does not match the production input snapshot",
        )
    if not is_production_extraction_hash(extraction, synthesis.extraction_hash):
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "Synthesis does not match the canonical extraction",
        )
    if relevance_projection is not None:
        try:
            validate_relevance_projection_lineage(
                relevance_projection,
                extraction,
                extraction_hash=canonical_extraction_hash(extraction),
            )
        except ValueError as exc:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                "Relevance projection does not match canonical extraction",
            ) from exc
        if relevance_projection.production_input_hash != snapshot.input_hash:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                "Relevance projection does not match the production input snapshot",
            )


# V12 changes only PUBLICATION: generic legitimate-service hosts are filtered
# from IOC groups. Upstream artifacts remain valid.
ASSEMBLY_POLICY_VERSION: Final[str] = "12-legitimate-service-ioc-filter"


def _canonical_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(payload)).hexdigest()


def compute_assembly_input_hash(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1,
    relevance_projection: RelevanceProjectionV1 | None = None,
) -> str:
    """Return the deterministic functional identity of canonical Assembly inputs."""
    payload = {
        "snapshot_input_hash": snapshot.input_hash,
        "references_hash": references_corpus_hash(references),
        "extraction_hash": canonical_extraction_hash(extraction),
        "synthesis_hash": canonical_synthesis_hash(synthesis),
        "editorial_enrichment_hash": canonical_editorial_enrichment_hash(editorial_enrichment),
        "relevance_projection_hash": (
            relevance_projection.projection_hash if relevance_projection is not None else None
        ),
        "publication_document_schema_version": PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
        "assembly_policy_version": ASSEMBLY_POLICY_VERSION,
        "legitimate_services_policy_version": LEGITIMATE_SERVICES_POLICY_VERSION,
        "semantic_annotation_policy_version": SEMANTIC_ANNOTATION_POLICY_VERSION,
    }
    return _canonical_digest(payload)


def _validate_synthesis_evidence_refs(
    *, extraction: ProductionExtractionV1, synthesis: ProductionSynthesisV1
) -> None:
    """Require each synthesis citation to identify current extraction evidence."""
    current_identities = {
        (ref.source_document_id, ref.kind.value, ref.evidence_key)
        for ref in extraction_evidence_refs_v1(extraction)
    }
    for ref in synthesis_evidence_refs(synthesis):
        identity = (ref.source_document_id, ref.kind.value, ref.evidence_key)
        if identity not in current_identities:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.EVIDENCE_MISSING,
                "Synthesis evidence reference is absent from the current extraction",
            )


def _project_publication_sources(
    *,
    references: ProductionReferenceCorpusV1,
    used_source_document_ids: frozenset[UUID] | set[UUID],
) -> tuple[PublicationSourceV1, ...]:
    """Project exactly the canonical reference sources used by a publication."""
    sources_by_id: dict[UUID, list[ProductionReferenceSourceV1]] = defaultdict(list)
    for source in references.sources:
        source_document_id = source.source_document_id
        if source_document_id is None:
            continue
        sources_by_id[source_document_id].append(source)

    publication_sources: list[PublicationSourceV1] = []
    for source_document_id in sorted(used_source_document_ids, key=str):
        matching_sources = sources_by_id.get(source_document_id)
        if not matching_sources:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.SOURCE_MISSING,
                "Used publication source is absent from the canonical reference corpus: "
                f"{source_document_id}",
            )
        resolved_source = min(matching_sources, key=_publication_reference_source_order)
        criticality, criticality_reason = production_source_criticality(
            resolved_source, sources=references.sources
        )
        publication_sources.append(
            PublicationSourceV1(
                source_document_id=source_document_id,
                canonical_url=resolved_source.canonical_url,
                title=resolved_source.title,
                publisher=resolved_source.publisher,
                published_at=resolved_source.published_at,
                tier=resolved_source.tier,
                kind=resolved_source.kind,
                role=resolved_source.role,
                editorial_role=resolved_source.editorial_role,
                source_criticality=criticality,
                criticality_reason=criticality_reason,
                source_collection_id=resolved_source.source_collection_id,
                content_sha256=resolved_source.content_sha256,
            )
        )
    return tuple(publication_sources)


def _reference_fallback_text(source: ProductionReferenceSourceV1) -> str:
    """Deterministic note for a source whose synthesis note is missing."""
    publisher = (source.publisher or "").strip() or (
        urlsplit(source.canonical_url).hostname or "Publication"
    ).removeprefix("www.")
    title = (source.title or "").strip() or source.canonical_url
    if (
        source.kind is ProductionReferenceKind.TECHNICAL_RESOURCE
        and source.editorial_role is ProductionEditorialRole.CONTEXT
    ):
        return f"{publisher} : {title} — ressource technique de contexte."
    return f"{publisher} publie « {title} »."


def _publication_reference_source_order(source: ProductionReferenceSourceV1) -> tuple[Any, ...]:
    """Stable reader-facing representative order for proven duplicate sources."""
    tier_order = {
        ProductionReferenceTier.CORE: 0,
        ProductionReferenceTier.SUPPORTING: 1,
        ProductionReferenceTier.TECHNICAL: 2,
    }
    return (
        tier_order[source.tier],
        bool(urlsplit(source.canonical_url).query),
        len(source.canonical_url),
        source.canonical_url,
        str(source.source_document_id or ""),
    )


def _publication_reference_groups(
    *,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> tuple[tuple[ProductionReferenceSourceV1, ...], ...]:
    """Group reader-facing publications and cited technical context by proven identity.

    Equal archive SHA-256 values are sufficient proof. Extraction's
    ``duplicate_content`` state can also connect rows that share the same
    durable extraction checkpoint. That state does not persist the representative
    source ID, so the flag alone is not enough to guess a pairing.
    """
    corpus_sources_by_id: dict[UUID, list[ProductionReferenceSourceV1]] = defaultdict(list)
    for source in references.sources:
        if source.source_document_id is not None:
            corpus_sources_by_id[source.source_document_id].append(source)
    extraction_by_id = {source.source_document_id: source for source in extraction.sources}

    cited_source_ids = {ref.source_document_id for ref in synthesis_evidence_refs(synthesis)}
    candidate_ids = set(cited_source_ids)
    candidate_ids.update(
        source.source_document_id
        for source in extraction.sources
        if source.profile is ExtractionProfile.FULL
    )

    candidates: dict[UUID, ProductionReferenceSourceV1] = {}
    for source_id in candidate_ids:
        matching_sources = corpus_sources_by_id.get(source_id)
        if not matching_sources:
            continue
        source = min(matching_sources, key=_publication_reference_source_order)
        is_reader_facing_publication = (
            source.kind is ProductionReferenceKind.PUBLICATION
            and source.tier in {ProductionReferenceTier.CORE, ProductionReferenceTier.SUPPORTING}
        )
        is_cited_technical_context = (
            source_id in cited_source_ids
            and source.kind is ProductionReferenceKind.TECHNICAL_RESOURCE
            and source.tier is ProductionReferenceTier.TECHNICAL
            and source.editorial_role is ProductionEditorialRole.CONTEXT
        )
        if is_reader_facing_publication or is_cited_technical_context:
            candidates[source_id] = source

    parent_by_id = {source_id: source_id for source_id in candidates}

    def root(source_id: UUID) -> UUID:
        parent = parent_by_id[source_id]
        while parent != parent_by_id[parent]:
            parent = parent_by_id[parent]
        while source_id != parent:
            next_id = parent_by_id[source_id]
            parent_by_id[source_id] = parent
            source_id = next_id
        return parent

    def union(left: UUID, right: UUID) -> None:
        left_root = root(left)
        right_root = root(right)
        if left_root == right_root:
            return
        lower, higher = sorted((left_root, right_root), key=str)
        parent_by_id[higher] = lower

    by_content_sha256: dict[str, list[UUID]] = defaultdict(list)
    by_checkpoint: dict[UUID, list[UUID]] = defaultdict(list)
    for source_id, source in candidates.items():
        extraction_source = extraction_by_id.get(source_id)
        content_sha256 = source.content_sha256 or (
            extraction_source.content_sha256 if extraction_source is not None else None
        )
        if content_sha256 is not None:
            by_content_sha256[content_sha256].append(source_id)
        if extraction_source is not None and extraction_source.checkpoint_id is not None:
            by_checkpoint[extraction_source.checkpoint_id].append(source_id)

    for grouped_ids in by_content_sha256.values():
        if len(grouped_ids) < 2:
            continue
        first_id = min(grouped_ids, key=str)
        for source_id in grouped_ids:
            union(first_id, source_id)

    for grouped_ids in by_checkpoint.values():
        if len(grouped_ids) < 2 or not any(
            extraction_by_id[source_id].reuse_state is ExtractionReuseState.CONTENT_DUPLICATE
            for source_id in grouped_ids
        ):
            continue
        first_id = min(grouped_ids, key=str)
        for source_id in grouped_ids:
            union(first_id, source_id)

    groups: dict[UUID, list[ProductionReferenceSourceV1]] = defaultdict(list)
    for source_id, source in candidates.items():
        groups[root(source_id)].append(source)

    return tuple(
        tuple(sorted(group, key=_publication_reference_source_order))
        for _, group in sorted(groups.items(), key=lambda item: str(item[0]))
    )


def _project_publication_references(
    *,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> tuple[PublicationReferenceEntryV1, ...]:
    """Project cited technical context and core/supporting publications cited or read in full.

    A representative's synthesis note wins; otherwise the first note in stable
    representative order supplies the text. Evidence refs from every note in the
    proven duplicate group remain attached to the reader-facing representative.
    """
    notes = {note.source_document_id: note for note in synthesis.source_notes}
    entries: list[PublicationReferenceEntryV1] = []
    for group in _publication_reference_groups(
        references=references,
        extraction=extraction,
        synthesis=synthesis,
    ):
        source = group[0]
        source_id = source.source_document_id
        assert source_id is not None
        group_notes = tuple(
            note
            for member in group
            if member.source_document_id is not None
            and (note := notes.get(member.source_document_id)) is not None
        )
        note = group_notes[0] if group_notes else None
        evidence_refs = tuple(
            sorted(
                {ref for group_note in group_notes for ref in group_note.evidence_refs},
                key=evidence_ref_sort_key,
            )
        )
        entries.append(
            PublicationReferenceEntryV1(
                source_document_id=source_id,
                text=note.text if note else _reference_fallback_text(source),
                evidence_refs=tuple(
                    PublicationEvidenceRefV1(
                        source_document_id=ref.source_document_id,
                        kind=PublicationEvidenceKind(ref.kind.value),
                        evidence_key=ref.evidence_key,
                    )
                    for ref in evidence_refs
                ),
            )
        )
    return tuple(entries)


def _validate_publication_enrichment(
    *,
    editorial_enrichment: EditorialEnrichmentV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
) -> None:
    """Apply the canonical Editorial Enrichment cross-artifact validator."""
    try:
        validate_editorial_enrichment(
            editorial_enrichment,
            extraction=extraction,
            synthesis=synthesis,
        )
    except EditorialEnrichmentValidationError as exc:
        code = {
            "editorial_enrichment_lineage_mismatch": PublicationAssemblyErrorCode.INPUTS_MISMATCH,
            "editorial_enrichment_evidence_missing": PublicationAssemblyErrorCode.EVIDENCE_MISSING,
            "editorial_enrichment_source_figure_invalid": (
                PublicationAssemblyErrorCode.SOURCE_FIGURE_INVALID
            ),
        }.get(exc.code, PublicationAssemblyErrorCode.VALIDATION_FAILED)
        raise PublicationAssemblyValidationError(code, str(exc)) from exc
    except ValueError as exc:
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.VALIDATION_FAILED,
            str(exc),
        ) from exc


def _project_publication_tables(
    tables: tuple[TableSpecV1, ...],
) -> tuple[PublicationTableV1, ...]:
    return tuple(
        PublicationTableV1(
            key=table.key,
            kind=table.kind,
            title=table.title,
            caption=table.caption,
            columns=tuple(
                PublicationTableColumnV1(key=column.key, label=column.label)
                for column in table.columns
            ),
            rows=tuple(
                PublicationTableRowV1(cells=row.cells, evidence_refs=row.evidence_refs)
                for row in table.rows
            ),
            placement=table.placement,
        )
        for table in tables
    )


def _project_publication_diagrams(
    diagrams: tuple[DiagramSpecV1, ...],
) -> tuple[PublicationDiagramV1, ...]:
    projected: list[PublicationDiagramV1] = []
    for diagram in diagrams:
        if diagram.compiled_asset_id is None:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.DIAGRAM_ASSET_MISSING,
                f"Publication diagram {diagram.key} has no compiled asset",
            )
        projected.append(
            PublicationDiagramV1(
                key=diagram.key,
                kind=diagram.kind,
                title=diagram.title,
                caption=diagram.caption,
                direction=diagram.direction,
                nodes=diagram.nodes,
                edges=diagram.edges,
                groups=diagram.groups,
                placement=diagram.placement,
                asset_id=diagram.compiled_asset_id,
            )
        )
    return tuple(projected)


def _project_publication_charts(
    charts: tuple[ChartSpecV1, ...],
) -> tuple[PublicationChartV1, ...]:
    projected: list[PublicationChartV1] = []
    for chart in charts:
        if chart.compiled_asset_id is None:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.CHART_ASSET_MISSING,
                f"Publication chart {chart.key} has no compiled asset",
            )
        evidence_refs = {ref for point in chart.points for ref in point.evidence_refs} | set(
            chart.purpose.evidence_refs
        )
        projected.append(
            PublicationChartV1(
                key=chart.key,
                kind=chart.kind,
                title=chart.title,
                caption=chart.caption,
                placement=chart.placement,
                asset_id=chart.compiled_asset_id,
                evidence_refs=tuple(sorted(evidence_refs, key=evidence_ref_sort_key)),
            )
        )
    return tuple(projected)


def _project_publication_figures(
    source_figures: tuple[SourceFigureCandidateV1, ...],
) -> tuple[PublicationSourceFigureV1, ...]:
    projected: list[PublicationSourceFigureV1] = []
    for candidate in source_figures:
        if candidate.inclusion_status is not SourceFigureInclusionStatus.INCLUDED:
            continue

        resolved = candidate.resolved_figure
        if resolved is None:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.SOURCE_FIGURE_UNRESOLVED,
                f"Included source figure {candidate.key} has no resolved local figure",
            )
        if resolved.decision is not SourceFigureDecision.ACCEPTED:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.SOURCE_FIGURE_INVALID,
                f"Included source figure {candidate.key} was not accepted",
            )

        blob_id = resolved.blob_id
        sha256 = resolved.sha256
        mime_type = resolved.mime_type
        byte_size = resolved.byte_size
        if blob_id is None or sha256 is None or mime_type is None or byte_size is None:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.SOURCE_FIGURE_METADATA_MISSING,
                f"Included source figure {candidate.key} has incomplete local metadata",
            )

        try:
            projected.append(
                PublicationSourceFigureV1(
                    key=candidate.key,
                    asset_id=media_asset_id(sha256, mime_type),
                    sha256=sha256,
                    mime_type=mime_type,
                    byte_size=byte_size,
                    source_document_id=candidate.source_document_id,
                    source_url=candidate.source_url,
                    caption=candidate.caption,
                    provenance=candidate.provenance,
                    locator=candidate.locator,
                    placement=candidate.placement,
                )
            )
        except ValueError as exc:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.SOURCE_FIGURE_INVALID,
                f"Included source figure {candidate.key} is invalid: {exc}",
            ) from exc
    return tuple(projected)


def build_publication_document_v4(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1,
    relevance_projection: RelevanceProjectionV1 | None = None,
) -> PublicationDocumentV4:
    """Build the renderer-independent V4 publication from canonical inputs."""
    _validate_publication_lineage(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        relevance_projection=relevance_projection,
    )
    _validate_publication_enrichment(
        editorial_enrichment=editorial_enrichment,
        extraction=extraction,
        synthesis=synthesis,
    )

    narrative = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)
    projection = _project_publication_iocs(
        extraction=extraction,
        narrative=narrative,
        relevance_projection=relevance_projection,
    )
    tables = _project_publication_tables(editorial_enrichment.tables)
    charts = _project_publication_charts(editorial_enrichment.charts)
    diagrams = _project_publication_diagrams(editorial_enrichment.diagrams)
    figures = _project_publication_figures(editorial_enrichment.source_figures)

    used_source_document_ids = set(projection.used_source_document_ids)
    used_source_document_ids.update(
        ref.source_document_id for ref in editorial_enrichment_evidence_refs(editorial_enrichment)
    )
    used_source_document_ids.update(figure.source_document_id for figure in figures)

    return PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=snapshot.subject_id,
        publication_language=synthesis.publication_language,
        title=synthesis.title,
        lead=projection.lead,
        sections=projection.sections,
        timeline=projection.timeline,
        indicators=projection.indicators,
        sources=_project_publication_sources(
            references=references,
            used_source_document_ids=used_source_document_ids,
        ),
        uncertainties=projection.uncertainties,
        tables=tables,
        diagrams=diagrams,
        figures=figures,
        charts=charts,
    )


def build_publication_document_v5(
    *,
    snapshot: ProductionInputSnapshot,
    references: ProductionReferenceCorpusV1,
    extraction: ProductionExtractionV1,
    synthesis: ProductionSynthesisV1,
    editorial_enrichment: EditorialEnrichmentV1,
    relevance_projection: RelevanceProjectionV1 | None = None,
) -> PublicationDocumentV5:
    """Build the publication: dated source references and two labelled IOC groups."""
    _validate_publication_lineage(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        relevance_projection=relevance_projection,
    )
    _validate_publication_enrichment(
        editorial_enrichment=editorial_enrichment,
        extraction=extraction,
        synthesis=synthesis,
    )
    narrative = _project_synthesis_publication(extraction=extraction, synthesis=synthesis)
    projection = _project_publication_iocs(
        extraction=extraction,
        narrative=narrative,
        relevance_projection=relevance_projection,
    )
    reference_entries = _project_publication_references(
        references=references, extraction=extraction, synthesis=synthesis
    )
    tables = _project_publication_tables(editorial_enrichment.tables)
    diagrams = _project_publication_diagrams(editorial_enrichment.diagrams)
    figures = _project_publication_figures(editorial_enrichment.source_figures)
    additional_source_ids = {item.source_document_id for item in reference_entries}
    additional_source_ids.update(
        ref.source_document_id for item in reference_entries for ref in item.evidence_refs
    )
    additional_source_ids.update(
        source_id
        for group in projection.original_indicators
        for indicator in group.indicators
        for source_id in indicator.source_document_ids
    )
    used_source_document_ids = projection.used_source_document_ids | additional_source_ids
    used_source_document_ids |= {
        ref.source_document_id for ref in editorial_enrichment_evidence_refs(editorial_enrichment)
    }
    used_source_document_ids |= {figure.source_document_id for figure in figures}

    sources = _project_publication_sources(
        references=references, used_source_document_ids=used_source_document_ids
    )
    reference_entries = order_publication_references(reference_entries, sources)
    document = PublicationDocumentV4(
        schema_version=PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        subject_id=snapshot.subject_id,
        publication_language=synthesis.publication_language,
        title=synthesis.title,
        lead=projection.lead,
        sections=projection.sections,
        timeline=projection.timeline,
        indicators=projection.indicators,
        sources=sources,
        uncertainties=projection.uncertainties,
        tables=tables,
        diagrams=diagrams,
        figures=figures,
        additional_source_ids=frozenset(additional_source_ids),
    )
    annotator = SemanticAnnotator()
    entities = semantic_entities_from_extraction(extraction)
    semantic_text = SemanticTextV1(
        schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
        paragraphs=annotator.annotate_paragraphs(
            tuple(
                publication_document_text_anchors(document, references=reference_entries).items()
            ),
            entities=entities,
            proposals=editorial_enrichment.annotations,
        ),
    )
    return PublicationDocumentV5(
        document=document,
        semantic_text=semantic_text,
        references=reference_entries,
        original_indicators=projection.original_indicators,
    )
