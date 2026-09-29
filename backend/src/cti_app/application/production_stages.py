"""Business logic for each production stage."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

from cti_app.application.discovery_report_parser import extract_http_urls
from cti_app.application.pandoc_rendering import PANDOC_RENDERER_VERSION, render_publication_pandoc
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_extraction import (
    extraction_compatibility_view,
    production_extraction_metadata,
)
from cti_app.application.production_parsers import (
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.application.production_references import (
    PRODUCTION_REFERENCE_PARSER_VERSION,
    load_reference_projection,
    production_reference_corpus_metadata,
    production_reference_corpus_to_json,
    report_source_labels,
)
from cti_app.application.production_rendering import collect_indicators
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
    project_legacy_synthesis_markdown,
    render_synthesis_markdown,
)
from cti_app.application.publication_builder import build_publication_document
from cti_app.application.semantic_annotation import SEMANTIC_ANNOTATOR_VERSION
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    SynthesisMode,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
)
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    extraction_evidence_refs_v1,
    production_synthesis_from_json,
    production_synthesis_to_json,
    synthesis_evidence_refs,
)
from cti_app.domain.publication import PUBLICATION_SCHEMA_VERSION


def compute_input_hash(input_data: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of input data."""
    json_str = json.dumps(input_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(json_str.encode()).hexdigest()


class _ArtifactPayloadMixin:
    """Stores stage payloads as blobs, when a store is configured."""

    _artifact_store: ProductionArtifactStore | None

    async def _store_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        if self._artifact_store is None:
            return None, None, None
        return await self._artifact_store.store_stage_payloads(
            raw=raw, canonical=canonical, rendered=rendered
        )


class ReferenceResearchService(_ArtifactPayloadMixin):
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def store_references_result(
        self,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        raw_result: str,
        corpus: ProductionReferenceCorpusV1,
        model_run_id: UUID | None = None,
    ) -> tuple[ProductionArtifact, bool]:
        """Persist the RAW wire format and only the canonical corpus.

        The RAW answer is kept because the not-yet-migrated stages rebuild the
        legacy ``ReferenceReport`` from it; the canonical payload is the
        versioned corpus and nothing else.

        Returns the artifact and whether a new functional version was created.
        A rebuild whose corpus is byte-identical to the current one is a no-op.
        """
        if not isinstance(corpus, ProductionReferenceCorpusV1):
            raise ValueError("REFERENCES canonical state must be a ProductionReferenceCorpusV1")
        canonical_json = production_reference_corpus_to_json(corpus)
        encoded = ProductionArtifactStore.canonical_json_bytes(canonical_json)

        async with self._uow_factory() as uow:
            current = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.REFERENCES.value
            )
            if (
                current is not None
                and current.status is ProductionArtifactStatus.VERIFIED
                and current.input_hash == input_hash
                and current.canonical_blob_id is not None
                and self._artifact_store is not None
            ):
                stored = await self._artifact_store.read_bytes(current.canonical_blob_id)
                if stored == encoded:
                    return current, False

            prior_versions = [
                artifact.version
                for artifact in await uow.production_artifacts.list_for_run(run_id)
                if artifact.stage is ProductionArtifactStage.REFERENCES
            ]
            version = max(prior_versions, default=0) + 1

            raw_id, canonical_id, _ = await self._store_payloads(
                raw=raw_result, canonical=canonical_json
            )
            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.REFERENCES,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                canonical_blob_id=canonical_id,
                model_run_id=model_run_id,
                metadata={
                    **production_reference_corpus_metadata(corpus),
                    "warnings": list(corpus.warnings),
                    "parser_version": PRODUCTION_REFERENCE_PARSER_VERSION,
                    "research_model_run_id": (
                        str(model_run_id) if model_run_id is not None else None
                    ),
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            await uow.production_artifacts.append(artifact)

            await uow.production_artifacts.mark_downstream_stale(
                run_id, ProductionArtifactStage.REFERENCES.value
            )

            await uow.commit()
            return artifact, True


class ExtractionService(_ArtifactPayloadMixin):
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def store_extraction_result(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        extraction: ProductionExtractionV1,
        warnings: list[str] | None = None,
        verification_diagnostics: dict[str, Any] | None = None,
        repair_evidence_blob_id: UUID | None = None,
        repair_evidence_entry_count: int | None = None,
        repair_evidence_index: list[dict[str, Any]] | None = None,
    ) -> ProductionArtifact:
        """Persist one EXTRACTION artifact from the canonical V1 contract.

        The service owns the serialization: the canonical blob is the
        ``ProductionExtractionV1`` payload and PostgreSQL only stores a bounded
        counter/version projection.  An extraction may need several model
        calls, so the run-level artifact names no single model run and keeps no
        RAW: model provenance lives on the source checkpoints.
        """

        canonical_payload = production_extraction_to_json(extraction)
        bounded_metadata = production_extraction_metadata(extraction)
        async with self._uow_factory() as uow:
            prior_versions = [
                artifact.version
                for artifact in await uow.production_artifacts.list_for_run(run_id)
                if artifact.stage is ProductionArtifactStage.EXTRACTION
            ]
            version = max(prior_versions, default=0) + 1

            raw_id, canonical_id, _ = await self._store_payloads(
                raw=None, canonical=canonical_payload
            )
            repair_evidence_metadata = None
            if repair_evidence_blob_id is not None:
                entry_count = repair_evidence_entry_count or 0
                if entry_count < 0:
                    raise ValueError("repair_evidence_entry_count must be >= 0")
                repair_evidence_metadata = {
                    "schema_version": "1",
                    "blob_id": str(repair_evidence_blob_id),
                    "entry_count": entry_count,
                }
                if repair_evidence_index is not None:
                    repair_evidence_metadata["index"] = [
                        dict(entry) for entry in repair_evidence_index
                    ]

            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                canonical_blob_id=canonical_id,
                metadata={
                    **bounded_metadata,
                    "warnings": warnings or [],
                    "generated_at": datetime.now(UTC).isoformat(),
                    "deterministic_verification": verification_diagnostics or {},
                    **(
                        {"repair_evidence": repair_evidence_metadata}
                        if repair_evidence_metadata is not None
                        else {}
                    ),
                },
            )
            await uow.production_artifacts.append(artifact)

            await uow.production_artifacts.mark_downstream_stale(
                run_id, ProductionArtifactStage.EXTRACTION.value
            )

            await uow.commit()
            return artifact

    async def store_repair_projection(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        canonical_json: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ProductionArtifact:
        """Persist an effective extraction produced by analyst decisions.

        This path deliberately has no raw/model payload: a repair projection
        is a deterministic derivative of an existing extraction and decision
        log, not a new Q2 submission.
        """
        if self._artifact_store is None:
            raise ValueError("Repair projection requires an artifact store")
        async with self._uow_factory() as uow:
            artifact = await self._store_repair_projection_in_uow(
                uow,
                run_id=run_id,
                subject_id=subject_id,
                input_hash=input_hash,
                canonical_json=canonical_json,
                metadata=metadata,
            )
            await uow.commit()
            return artifact

    async def _store_repair_projection_in_uow(
        self,
        uow: Any,
        *,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        canonical_json: dict[str, Any],
        metadata: dict[str, Any],
    ) -> ProductionArtifact:
        """Transaction-local variant used while Edition/Run locks are held."""
        get_current = getattr(uow.production_artifacts, "get_current", None)
        current = (
            await get_current(run_id, ProductionArtifactStage.EXTRACTION.value)
            if callable(get_current)
            else None
        )
        if (
            current is not None
            and current.input_hash == input_hash
            and isinstance(current.metadata, dict)
            and isinstance(current.metadata.get("repair_projection"), dict)
        ):
            return cast(ProductionArtifact, current)

        canonical_id = (
            await self._artifact_store.put_json(
                canonical_json, bucket="production-artifacts-canonical"
            )
            if self._artifact_store is not None
            else None
        )
        if canonical_id is None:
            raise ValueError("Repair projection canonical payload was not stored")

        prior_versions = [
            artifact.version
            for artifact in await uow.production_artifacts.list_for_run(run_id)
            if artifact.stage is ProductionArtifactStage.EXTRACTION
        ]
        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=max(prior_versions, default=0) + 1,
            input_hash=input_hash,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=None,
            canonical_blob_id=canonical_id,
            model_run_id=None,
            conversation_turn_id=None,
            metadata=dict(metadata),
        )
        # The extraction row is immutable once appended.  Fill its own audit
        # identity before the insert instead of updating it afterwards.
        repair_materialization = artifact.metadata.get("repair_materialization")
        if isinstance(repair_materialization, dict):
            repair_materialization = dict(repair_materialization)
            repair_materialization["result_extraction_artifact_id"] = str(artifact.id)
            artifact.metadata["repair_materialization"] = repair_materialization
        # A repair projection is an analyst-level derivative.  Its semantic
        # impact is decided by ProductionRepairMaterializationService; a
        # linear downstream invalidation here would erase a still-valid
        # synthesis and publication.
        await uow.production_artifacts.append(artifact)
        return artifact


class SynthesisService(_ArtifactPayloadMixin):
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def store_synthesis_result(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        synthesis: ProductionSynthesisV1,
        extraction: ProductionExtractionV1,
        raw_result: str | None,
        model_run_id: UUID | None,
        mode: SynthesisMode = SynthesisMode.FRESH,
        model_policy_version: str = "",
        routing_policy_version: str = "",
    ) -> ProductionArtifact:
        if self._artifact_store is None:
            raise ValueError("Canonical synthesis requires an artifact store")
        if synthesis.subject_id != subject_id or extraction.subject_id != subject_id:
            raise ValueError("Synthesis subject does not match artifact subject")
        if synthesis.extraction_hash != canonical_extraction_hash(extraction):
            raise ValueError("Synthesis extraction hash does not match canonical extraction")
        cited_refs = synthesis_evidence_refs(synthesis)
        if not cited_refs <= set(extraction_evidence_refs_v1(extraction)):
            raise ValueError("Synthesis cites evidence absent from extraction")
        source_ids = {source.source_document_id for source in extraction.sources}
        if any(not set(item.source_document_ids) <= source_ids for item in synthesis.uncertainties):
            raise ValueError("Synthesis uncertainty cites an absent source")
        canonical_payload = production_synthesis_to_json(synthesis)
        paragraphs = (*synthesis.lead, *(p for s in synthesis.sections for p in s.paragraphs))
        preview = render_synthesis_markdown(synthesis, extraction)
        narrative = [synthesis.title, *(p.text for p in paragraphs)]
        narrative.extend(entry.text for entry in synthesis.timeline)
        narrative.extend(item.text for item in synthesis.uncertainties)
        metadata = {
            "schema_version": synthesis.schema_version,
            "language": synthesis.publication_language,
            "mode": mode.value,
            "section_count": len(synthesis.sections),
            "paragraph_count": len(paragraphs),
            "timeline_entry_count": len(synthesis.timeline),
            "evidence_ref_count": len(cited_refs),
            "uncertainty_count": len(synthesis.uncertainties),
            "warnings_count": len(synthesis.warnings),
            "word_count": sum(len(value.split()) for value in narrative),
            "model_policy_version": model_policy_version,
            "routing_policy_version": routing_policy_version,
            "synthesis_policy_version": synthesis.synthesis_policy_version,
        }
        async with self._uow_factory() as uow:
            # Not get_current: a synthesis retry stales every prior synthesis
            # artifact first, and get_current excludes STALE rows — using it
            # here would restart numbering at 1 and collide with the
            # (production_run_id, stage, version) uniqueness of the original.
            existing = await uow.production_artifacts.list_for_run(run_id)
            prior_versions = [
                artifact.version
                for artifact in existing
                if artifact.stage is ProductionArtifactStage.SYNTHESIS
            ]
            version = max(prior_versions) + 1 if prior_versions else 1

            raw_id, canonical_id, rendered_id = await self._store_payloads(
                raw=raw_result, canonical=canonical_payload, rendered=preview
            )
            if canonical_id is None:
                raise ValueError("Canonical synthesis blob was not stored")
            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                canonical_blob_id=canonical_id,
                rendered_blob_id=rendered_id,
                model_run_id=model_run_id,
                metadata=metadata,
            )
            await uow.production_artifacts.append(artifact)

            await uow.production_artifacts.mark_downstream_stale(
                run_id, ProductionArtifactStage.SYNTHESIS.value
            )

            await uow.commit()
            return artifact


class PublicationAssemblyService(_ArtifactPayloadMixin):
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
            "publication_schema_version": PUBLICATION_SCHEMA_VERSION,
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

        document = build_publication_document(
            subject_title=subject_title,
            report=report,
            extraction=extraction,
            synthesis_text=synthesis_text,
        )
        publication_markdown = render_publication_pandoc(document)

        raw_id, canonical_id, rendered_id = await self._store_payloads(
            canonical=document.to_json(),
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
                "publication_schema_version": PUBLICATION_SCHEMA_VERSION,
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


class ProductionQAService:
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
