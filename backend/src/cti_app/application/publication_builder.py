"""Build the canonical publication document from verified production artifacts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final
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
from cti_app.domain.media_assets import media_asset_id
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_editorial_enrichment import (
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
    ProductionExtractionV1,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceSourceV1,
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
    PublicationSectionKind,
    PublicationSectionV1,
    PublicationSourceV1,
    PublicationTimelineEntryV1,
    PublicationUncertaintyV1,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDiagramV1,
    PublicationDocumentV4,
    PublicationSourceFigureV1,
    PublicationTableColumnV1,
    PublicationTableRowV1,
    PublicationTableV1,
)


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
    used_source_document_ids: frozenset[UUID]


def _project_synthesis_publication(
    *, extraction: ProductionExtractionV1, synthesis: ProductionSynthesisV1
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
    timeline = tuple(
        PublicationTimelineEntryV1(
            event_date=entry.event_date,
            date_text=entry.date_text,
            text=entry.text,
            evidence_refs=evidence_refs(entry.evidence_refs),
        )
        for entry in synthesis.timeline
    )
    uncertainties = tuple(
        PublicationUncertaintyV1(
            text=uncertainty.text,
            source_document_ids=uncertainty.source_document_ids,
        )
        for uncertainty in synthesis.uncertainties
    )
    used_source_document_ids.update(
        source_document_id
        for uncertainty in uncertainties
        for source_document_id in uncertainty.source_document_ids
    )
    return _PublicationNarrativeProjection(
        lead=lead,
        sections=sections,
        timeline=timeline,
        uncertainties=uncertainties,
        used_source_document_ids=frozenset(used_source_document_ids),
    )


def _project_publication_iocs(
    *,
    extraction: ProductionExtractionV1,
    narrative: _PublicationNarrativeProjection,
    relevance_projection: RelevanceProjectionV1 | None = None,
) -> _PublicationContentProjection:
    """Add confirmed canonical IOCs to the narrative projection."""
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
    occurrences: dict[tuple[ArtifactType, str], tuple[set[str], set[UUID]]] = {}
    for source in extraction.sources:
        for item in source.indicators:
            if (
                item.indicator_status is not ExtractionIndicatorStatus.CONFIRMED_IOC
                or item.artifact_type not in PUBLICATION_IOC_ARTIFACT_TYPES
            ):
                continue
            if relevance_projection is not None:
                ref = indicator_ref_by_identity[
                    (
                        source.source_document_id,
                        item.value,
                        item.artifact_type.value,
                        item.context,
                        item.evidence_quote,
                    )
                ]
                decision = relevance_projection.classification_for(ref)
                if decision.classification not in {
                    RelevanceClassification.DIRECT,
                    RelevanceClassification.CORROBORATION,
                } or decision.reason_code not in {
                    RelevanceReasonCode.MALICIOUS_SUBJECT_RELATION,
                    RelevanceReasonCode.MALICIOUS_SUBJECT_CORROBORATION,
                }:
                    continue
            try:
                normalized_value = normalize_indicator_value(item.value, item.artifact_type)
            except ValueError as exc:
                raise PublicationAssemblyValidationError(
                    PublicationAssemblyErrorCode.VALIDATION_FAILED,
                    "Confirmed IOC cannot be normalized for publication: "
                    f"{item.artifact_type.value} from {source.source_document_id}",
                ) from exc
            identity = (item.artifact_type, normalized_value)
            values, source_document_ids = occurrences.setdefault(identity, (set(), set()))
            values.add(item.value)
            source_document_ids.update(item.source_document_ids)

    grouped: dict[ArtifactType, list[PublicationIndicatorV1]] = defaultdict(list)
    used_source_document_ids = set(narrative.used_source_document_ids)
    for (artifact_type, normalized_value), (values, source_document_ids) in sorted(
        occurrences.items(),
        key=lambda item: (item[0][0].value, item[0][1]),
    ):
        ordered_source_ids = tuple(sorted(source_document_ids, key=str))
        used_source_document_ids.update(ordered_source_ids)
        grouped[artifact_type].append(
            PublicationIndicatorV1(
                value=min(values),
                normalized_value=normalized_value,
                artifact_type=artifact_type,
                source_document_ids=ordered_source_ids,
            )
        )

    indicators = tuple(
        PublicationIndicatorGroupV1(artifact_type, tuple(grouped[artifact_type]))
        for artifact_type in sorted(grouped, key=lambda value: value.value)
    )
    return _PublicationContentProjection(
        lead=narrative.lead,
        sections=narrative.sections,
        timeline=narrative.timeline,
        uncertainties=narrative.uncertainties,
        indicators=indicators,
        used_source_document_ids=frozenset(used_source_document_ids),
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
    if synthesis.extraction_hash != canonical_extraction_hash(extraction):
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


ASSEMBLY_POLICY_VERSION: Final[str] = "3-subject-relevance-projection"


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
        "publication_document_schema_version": PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
        "assembly_policy_version": ASSEMBLY_POLICY_VERSION,
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
    sources_by_id: dict[UUID, ProductionReferenceSourceV1] = {}
    for source in references.sources:
        source_document_id = source.source_document_id
        if source_document_id is None:
            continue
        if source_document_id in sources_by_id:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.VALIDATION_FAILED,
                f"Canonical reference corpus repeats source_document_id {source_document_id}",
            )
        sources_by_id[source_document_id] = source

    publication_sources: list[PublicationSourceV1] = []
    for source_document_id in sorted(used_source_document_ids, key=str):
        resolved_source = sources_by_id.get(source_document_id)
        if resolved_source is None:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.SOURCE_MISSING,
                "Used publication source is absent from the canonical reference corpus: "
                f"{source_document_id}",
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
            )
        )
    return tuple(publication_sources)


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
    )
