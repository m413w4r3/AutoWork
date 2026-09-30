"""Historical Assembly and QA boundary for Repair Desk state import fixtures.

``production_repairs`` may depend on this module for imported legacy artifacts.
The canonical production pipeline and publication readers must not import it.
New Production runs use ``publication_assembly`` and ``publication_qa``.
Legacy source URLs without a stored document identity receive stable URL-scoped
identifiers only in the publication projection; source state is never updated.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery_report_parser import extract_http_urls
from cti_app.application.french_typography import apply_french_spacing
from cti_app.application.pandoc_rendering import PANDOC_RENDERER_VERSION, render_publication_pandoc
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import extraction_compatibility_view
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import (
    ReferenceReport,
    SemanticType,
    TechnicalExtraction,
)
from cti_app.application.production_references import (
    load_reference_projection,
    production_reference_corpus_from_json,
    report_source_labels,
)
from cti_app.application.production_rendering import collect_indicators
from cti_app.application.production_stages import _ArtifactPayloadMixin, compute_input_hash
from cti_app.application.production_synthesis import project_legacy_synthesis_markdown
from cti_app.application.publication_builder import build_publication_document_v3
from cti_app.application.semantic_annotation import SEMANTIC_ANNOTATOR_VERSION
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    production_synthesis_from_json,
    synthesis_evidence_refs,
)
from cti_app.domain.publication import (
    PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
    ArtifactType,
    PublicationDocumentV3,
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
from cti_app.domain.publication_document import serialize_publication_document

_VALID_TITLE = re.compile(r"^\[[^\]]+\]\s+.+")
_SYNTHESIS_MARKER = re.compile(r"\[(S\d{1,3})\]", re.IGNORECASE)


class LegacyPublicationAssemblyService(_ArtifactPayloadMixin):
    """Manages publication assembly stage (deterministic)."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def assemble_publication(
        self,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
        metadata_extra: Mapping[str, Any] | None = None,
    ) -> ProductionArtifact:
        """Render the final publication from the stored artifacts.

        Deterministic: no model call. Reads the real payloads rather than the
        counters kept in `metadata`.
        """
        report, extraction, synthesis_text = await self._load_inputs(
            references_artifact, extraction_artifact, synthesis_artifact
        )

        async with self._uow_factory() as uow:
            artifact = await self._assemble_publication_in_uow(
                uow,
                run_id=run_id,
                subject_id=subject_id,
                subject_title=subject_title,
                references_artifact=references_artifact,
                extraction_artifact=extraction_artifact,
                synthesis_artifact=synthesis_artifact,
                metadata_extra=metadata_extra,
                report=report,
                extraction=extraction,
                synthesis_text=synthesis_text,
            )
            await uow.commit()
            return artifact

    async def assemble_publication_in_uow(
        self,
        uow: Any,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
        metadata_extra: Mapping[str, Any] | None = None,
    ) -> ProductionArtifact:
        """Assemble without committing, for a caller holding repair fences."""
        report, extraction, synthesis_text = await self._load_inputs(
            references_artifact, extraction_artifact, synthesis_artifact
        )
        return await self._assemble_publication_in_uow(
            uow,
            run_id=run_id,
            subject_id=subject_id,
            subject_title=subject_title,
            references_artifact=references_artifact,
            extraction_artifact=extraction_artifact,
            synthesis_artifact=synthesis_artifact,
            metadata_extra=metadata_extra,
            report=report,
            extraction=extraction,
            synthesis_text=synthesis_text,
        )

    async def _assemble_publication_in_uow(
        self,
        uow: Any,
        *,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
        metadata_extra: Mapping[str, Any] | None = None,
        report: ReferenceReport,
        extraction: TechnicalExtraction,
        synthesis_text: str,
    ) -> ProductionArtifact:
        input_data = {
            "references_id": str(references_artifact.id),
            "references_hash": references_artifact.input_hash,
            "extraction_id": str(extraction_artifact.id),
            "extraction_hash": extraction_artifact.input_hash,
            "synthesis_id": str(synthesis_artifact.id),
            "synthesis_hash": synthesis_artifact.input_hash,
            "publication_schema_version": PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
            "semantic_annotator_version": SEMANTIC_ANNOTATOR_VERSION,
            "pandoc_renderer_version": PANDOC_RENDERER_VERSION,
        }
        input_hash = compute_input_hash(input_data)

        # Not get_current: a synthesis retry stales the previous publication
        # first, and get_current excludes STALE rows — using it here would
        # restart numbering at 1 and collide with the unique version key.
        existing = await uow.production_artifacts.list_for_run(run_id)
        prior_versions = [
            artifact.version
            for artifact in existing
            if artifact.stage is ProductionArtifactStage.PUBLICATION
        ]
        version = max(prior_versions) + 1 if prior_versions else 1

        document = await self._build_publication_document(
            uow,
            run_id=run_id,
            subject_id=subject_id,
            subject_title=subject_title,
            references_artifact=references_artifact,
            extraction_artifact=extraction_artifact,
            synthesis_artifact=synthesis_artifact,
            report=report,
            extraction=extraction,
            synthesis_text=synthesis_text,
        )
        publication_markdown = render_publication_pandoc(document)

        raw_id, canonical_id, rendered_id = await self._store_payloads(
            canonical=serialize_publication_document(document),
            rendered=publication_markdown,
        )
        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=version,
            input_hash=input_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=raw_id,
            canonical_blob_id=canonical_id,
            rendered_blob_id=rendered_id,
            metadata={
                "word_count": len(publication_markdown.split()),
                "reference_count": len(document.sources),
                "indicator_count": len(collect_indicators(extraction)),
                "analyst_override_indicator_count": sum(
                    item.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
                    for item in extraction.items
                ),
                "publication_schema_version": PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
                "semantic_annotator_version": SEMANTIC_ANNOTATOR_VERSION,
                "pandoc_renderer_version": PANDOC_RENDERER_VERSION,
                "generated_at": datetime.now(UTC).isoformat(),
                # The identity of what this document actually consumed.  The
                # freeze proves against it that a published article was built
                # from the effective Extraction, without trusting a marker
                # carried by the Extraction alone.
                "input_artifacts": {
                    "references_artifact_id": str(references_artifact.id),
                    "extraction_artifact_id": str(extraction_artifact.id),
                    "synthesis_artifact_id": str(synthesis_artifact.id),
                },
            },
        )
        if metadata_extra:
            extra = dict(metadata_extra)
            repair_materialization = extra.get("repair_materialization")
            if isinstance(repair_materialization, dict):
                repair_materialization = dict(repair_materialization)
                repair_materialization["result_publication_artifact_id"] = str(artifact.id)
                extra["repair_materialization"] = repair_materialization
            artifact.metadata.update(extra)
        await uow.production_artifacts.append(artifact)
        return artifact

    async def _build_publication_document(
        self,
        uow: Any,
        *,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
        report: ReferenceReport,
        extraction: TechnicalExtraction,
        synthesis_text: str,
    ) -> PublicationDocumentV3:
        if self._artifact_store is None:
            raise ValueError("Publication assembly requires an artifact store")

        references: ProductionReferenceCorpusV1 | None = None
        if references_artifact.canonical_blob_id is not None:
            payload = await self._artifact_store.read_json(references_artifact.canonical_blob_id)
            try:
                references = production_reference_corpus_from_json(payload)
            except ValueError:
                pass

        snapshot: ProductionInputSnapshot | None = await uow.production_input_snapshots.get_by_run(
            run_id
        )
        extraction_view = None
        if extraction_artifact.canonical_blob_id is not None:
            extraction_view = extraction_compatibility_view(
                await self._artifact_store.read_json(extraction_artifact.canonical_blob_id),
                source_labels=report_source_labels(report),
            )

        if (
            snapshot is not None
            and references is not None
            and extraction_view is not None
            and extraction_view.canonical is not None
            and synthesis_artifact.canonical_blob_id is not None
        ):
            synthesis = production_synthesis_from_json(
                await self._artifact_store.read_json(synthesis_artifact.canonical_blob_id)
            )
            return build_publication_document_v3(
                snapshot=snapshot,
                references=references,
                extraction=extraction_view.canonical,
                synthesis=synthesis,
            )

        source_ids_by_url = (
            {
                source.canonical_url: source.source_document_id
                for source in references.sources
                if source.source_document_id is not None
            }
            if references is not None
            else {}
        )
        return _legacy_publication_document_v3(
            subject_id=subject_id,
            subject_title=subject_title,
            publication_language=snapshot.publication_language if snapshot else "fr",
            report=report,
            extraction=extraction,
            synthesis_text=synthesis_text,
            source_ids_by_url=source_ids_by_url,
        )

    async def _load_inputs(
        self,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
    ) -> tuple[ReferenceReport, TechnicalExtraction, str]:
        if self._artifact_store is None:
            raise ValueError("Publication assembly requires an artifact store")
        if references_artifact.canonical_blob_id is None:
            raise ValueError("References artifact has no canonical payload")
        if extraction_artifact.canonical_blob_id is None:
            raise ValueError("Extraction artifact has no canonical payload")
        report = await load_reference_projection(self._artifact_store, references_artifact)
        if report is None:
            raise ValueError("References payload is not readable")
        view = extraction_compatibility_view(
            await self._artifact_store.read_json(extraction_artifact.canonical_blob_id),
            source_labels=report_source_labels(report),
        )
        synthesis_text = await assembly_synthesis_text(
            self._artifact_store, report, view.canonical, synthesis_artifact
        )
        return report, view.legacy, synthesis_text


def _legacy_publication_document_v3(
    *,
    subject_id: UUID,
    subject_title: str,
    publication_language: str,
    report: ReferenceReport,
    extraction: TechnicalExtraction,
    synthesis_text: str,
    source_ids_by_url: Mapping[str, UUID],
) -> PublicationDocumentV3:
    """Project Repair Desk inputs into V3 at this one-way legacy boundary."""
    source_id_by_label: dict[str, UUID] = {}
    source_by_id: dict[UUID, PublicationSourceV1] = {}
    for source in report.sources:
        source_document_id = source_ids_by_url.get(source.canonical_url)
        if source_document_id is None:
            source_document_id = uuid5(
                NAMESPACE_URL, f"autowork:legacy-publication-source:{source.canonical_url}"
            )
        tier = (
            ProductionReferenceTier.CORE
            if source.role is SourceRole.PRIMARY
            else ProductionReferenceTier.SUPPORTING
        )
        source_id_by_label[source.local_id] = source_document_id
        source_by_id[source_document_id] = PublicationSourceV1(
            source_document_id=source_document_id,
            canonical_url=source.canonical_url,
            title=source.title,
            publisher=source.publisher,
            published_at=source.published_at,
            tier=tier,
            kind=ProductionReferenceKind.PUBLICATION,
            role=source.role,
        )

    all_source_labels = tuple(source_id_by_label)

    def evidence_refs(
        source_labels: tuple[str, ...], text: str, kind: PublicationEvidenceKind
    ) -> tuple[PublicationEvidenceRefV1, ...]:
        labels = source_labels or all_source_labels
        unknown = set(labels) - source_id_by_label.keys()
        if unknown:
            raise ValueError(f"Unknown publication source: {','.join(sorted(unknown))}")
        refs: list[PublicationEvidenceRefV1] = []
        for label in dict.fromkeys(labels):
            source_document_id = source_id_by_label[label]
            evidence_key = hashlib.sha256(
                f"{source_document_id}\0{kind.value}\0{text}".encode()
            ).hexdigest()
            refs.append(PublicationEvidenceRefV1(source_document_id, kind, evidence_key))
        return tuple(refs)

    timeline = tuple(
        PublicationTimelineEntryV1(
            event_date=event.event_date,
            date_text=None,
            text=event.text,
            evidence_refs=evidence_refs(
                event.source_ids, event.text, PublicationEvidenceKind.EVENT
            ),
        )
        for event in report.events
        if event.text.strip() and (event.source_ids or all_source_labels)
    )

    paragraphs: list[PublicationParagraphV1] = []
    for raw in re.split(r"\n\s*\n", synthesis_text.strip()):
        text = apply_french_spacing(_SYNTHESIS_MARKER.sub("", raw).strip())
        if not text:
            continue
        labels = tuple(match.group(1).upper() for match in _SYNTHESIS_MARKER.finditer(raw))
        refs = evidence_refs(labels, text, PublicationEvidenceKind.FACT)
        if refs:
            paragraphs.append(PublicationParagraphV1(text, refs))

    sections = (
        (
            PublicationSectionV1(
                PublicationSectionKind.OVERVIEW,
                "Synthèse",
                tuple(paragraphs),
            ),
        )
        if paragraphs
        else ()
    )

    indicators_by_type: dict[ArtifactType, list[PublicationIndicatorV1]] = {}
    for item in collect_indicators(extraction):
        assert item.artifact_type is not None
        artifact_type = (
            item.artifact_type
            if isinstance(item.artifact_type, ArtifactType)
            else ArtifactType(item.artifact_type)
        )
        try:
            normalized_value = item.normalized_value or normalize_indicator_value(
                item.value, artifact_type
            )
        except ValueError:
            continue
        refs = evidence_refs(item.source_ids, item.value, PublicationEvidenceKind.INDICATOR)
        if refs:
            indicators_by_type.setdefault(artifact_type, []).append(
                PublicationIndicatorV1(
                    value=item.value,
                    normalized_value=normalized_value,
                    artifact_type=artifact_type,
                    source_document_ids=tuple(ref.source_document_id for ref in refs),
                )
            )

    indicators = tuple(
        PublicationIndicatorGroupV1(artifact_type, tuple(values))
        for artifact_type, values in indicators_by_type.items()
    )
    uncertainties: list[PublicationUncertaintyV1] = []
    for text in dict.fromkeys((*report.uncertainties, *extraction.uncertainties)):
        refs = evidence_refs((), text, PublicationEvidenceKind.FACT)
        if refs:
            uncertainties.append(
                PublicationUncertaintyV1(
                    text,
                    tuple(ref.source_document_id for ref in refs),
                )
            )

    used_source_ids = {
        ref.source_document_id for paragraph in paragraphs for ref in paragraph.evidence_refs
    }
    used_source_ids.update(
        ref.source_document_id for entry in timeline for ref in entry.evidence_refs
    )
    used_source_ids.update(
        source_id
        for group in indicators
        for indicator in group.indicators
        for source_id in indicator.source_document_ids
    )
    used_source_ids.update(
        source_id for item in uncertainties for source_id in item.source_document_ids
    )

    title = " ".join((report.editorial_title or subject_title).split())
    if not _VALID_TITLE.fullmatch(title):
        actor = next(
            (
                item.value
                for item in extraction.items
                if item.supported and item.semantic_type is SemanticType.ACTOR
            ),
            "Publication",
        )
        title = f"[{actor}] {title}"

    return PublicationDocumentV3(
        schema_version=PUBLICATION_DOCUMENT_V3_SCHEMA_VERSION,
        subject_id=subject_id,
        publication_language=publication_language,
        title=title,
        lead=(),
        sections=sections,
        timeline=timeline,
        indicators=indicators,
        sources=tuple(source_by_id[source_id] for source_id in used_source_ids),
        uncertainties=tuple(uncertainties),
    )


async def assembly_synthesis_text(
    artifact_store: ProductionArtifactStore,
    report: ReferenceReport,
    extraction: ProductionExtractionV1 | None,
    synthesis_artifact: ProductionArtifact,
) -> str:
    """Return the legacy Assembly text of one SYNTHESIS artifact (until AW-013).

    A canonical ``ProductionSynthesisV1`` goes through the one-way compatibility
    projection, with each evidence source mapped to its legacy report label by
    exact canonical URL. Only an imported V4 artifact, which has no canonical
    payload, keeps its historical rendered Markdown.
    """
    if synthesis_artifact.canonical_blob_id is None:
        if synthesis_artifact.rendered_blob_id is None:
            raise ValueError("Synthesis artifact has no readable payload")
        return await artifact_store.read_text(synthesis_artifact.rendered_blob_id)
    if extraction is None:
        raise ValueError("Canonical synthesis requires a canonical extraction")
    synthesis = production_synthesis_from_json(
        await artifact_store.read_json(synthesis_artifact.canonical_blob_id)
    )
    document_urls = {
        source.source_document_id: source.canonical_url for source in extraction.sources
    }
    labels_by_url = report_source_labels(report)
    source_labels: dict[UUID, str] = {}
    for ref in synthesis_evidence_refs(synthesis):
        canonical_url = document_urls.get(ref.source_document_id)
        if canonical_url is None:
            raise ValueError(
                "Canonical synthesis evidence source is absent from canonical extraction: "
                f"{ref.source_document_id}"
            )
        local_id = labels_by_url.get(canonical_url)
        if local_id is None:
            raise ValueError(
                "Canonical synthesis evidence source cannot be mapped to the legacy "
                f"ReferenceReport by exact canonical URL: {canonical_url}"
            )
        source_labels[ref.source_document_id] = local_id
    return project_legacy_synthesis_markdown(synthesis, extraction, source_labels)


class LegacyProductionQAService:
    """Automated QA gate between assembly and READY.

    Every check answers one question: can a reader trust what this publication
    asserts, given only the sources we actually hold?
    """

    def __init__(self, uow_factory: ProductionUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def run_qa(
        self,
        run_id: UUID,
        references_artifact: ProductionArtifact | None,
        extraction_artifact: ProductionArtifact | None,
        synthesis_artifact: ProductionArtifact | None,
        publication_artifact: ProductionArtifact | None = None,
        *,
        report: ReferenceReport | None = None,
        extraction: TechnicalExtraction | None = None,
        synthesis_text: str = "",
        publication_markdown: str = "",
        archived_urls: set[str] | None = None,
        research_date: date | None = None,
    ) -> dict[str, Any]:
        checks: dict[str, bool] = {}
        errors: list[str] = []
        warnings: list[str] = []
        archived = archived_urls or set()

        def require(name: str, ok: bool, message: str) -> None:
            checks[name] = ok
            if not ok:
                errors.append(message)

        require("references_present", references_artifact is not None, "Références manquantes")
        require("extraction_present", extraction_artifact is not None, "Extraction manquante")
        require("synthesis_present", synthesis_artifact is not None, "Synthèse manquante")
        require("publication_present", publication_artifact is not None, "Publication manquante")

        for label, artifact in (
            ("references", references_artifact),
            ("extraction", extraction_artifact),
            ("synthesis", synthesis_artifact),
            ("publication", publication_artifact),
        ):
            if artifact is not None:
                require(
                    f"no_stale_{label}",
                    artifact.status != ProductionArtifactStatus.STALE,
                    f"Artifact {label} périmé",
                )

        known_sources: set[str] = set()
        known_events: set[str] = set()
        if report is not None:
            known_sources = report.source_ids()
            known_events = {event.local_id for event in report.events}
            require("source_count", bool(report.sources), "Aucune source retenue")
            require("event_count", bool(report.events), "Aucun événement retenu")
            require(
                "every_event_has_an_archived_source",
                all(
                    any(
                        source.canonical_url in archived
                        for source in report.sources
                        if source.local_id in event.source_ids
                    )
                    for event in report.events
                ),
                "Un événement n'est adossé à aucune source archivée",
            )
            require(
                "at_least_one_archived_source",
                any(source.canonical_url in archived for source in report.sources),
                "Aucune source archivée",
            )
            if research_date is not None:
                require(
                    "no_future_date",
                    all(
                        event.event_date is None or event.event_date <= research_date
                        for event in report.events
                    ),
                    "Un événement porte une date postérieure à la recherche",
                )

        if extraction is not None:
            analyst_override_count = sum(
                item.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
                for item in extraction.items
            ) + sum(
                rule.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
                for rule in extraction.rules
            )
            if analyst_override_count:
                warnings.append(f"analyst_override_not_source_proof:count={analyst_override_count}")
            require(
                "no_unknown_reference_in_items",
                all(
                    set(item.reference_ids) <= known_events
                    and set(item.source_ids) <= known_sources
                    for item in extraction.supported_items()
                )
                and all(
                    set(rule.source_ids) <= known_sources
                    for rule in extraction.rules
                    if rule.supported
                ),
                "Un élément d'extraction cite une référence inconnue",
            )
            if not extraction.supported_items() and not any(
                rule.supported for rule in extraction.rules
            ):
                warnings.append("Aucun élément d'extraction n'est étayé")

        if synthesis_text:
            markers = {
                match.group(1).upper() for match in _SYNTHESIS_MARKER.finditer(synthesis_text)
            }
            require(
                "no_unknown_marker_in_synthesis",
                markers <= known_sources,
                "La synthèse cite une source inconnue",
            )
            corpus_urls = {source.canonical_url for source in report.sources} if report else set()
            require(
                "no_url_outside_corpus",
                all(
                    canonical in corpus_urls
                    for _raw, canonical in extract_http_urls(synthesis_text)
                ),
                "La synthèse cite une URL hors corpus",
            )

        if publication_markdown:
            # Le rendu Pandoc émet des notes inline `^[...]`, jamais une
            # bibliographie numérotée : une note orpheline est ici une note
            # vide, c'est-à-dire une citation dont aucune source du corpus
            # n'a pu fournir d'URL.
            require(
                "no_empty_footnote",
                not _PUBLICATION_EMPTY_FOOTNOTE.search(publication_markdown),
                "La publication contient une note de bas de page vide",
            )

        passed = all(checks.values()) and not errors
        return {
            "passed": passed,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
        }


_SYNTHESIS_MARKER = re.compile(r"\[(S\d{1,3})\]", re.IGNORECASE)
_PUBLICATION_EMPTY_FOOTNOTE = re.compile(r"\^\[\s*\]")
