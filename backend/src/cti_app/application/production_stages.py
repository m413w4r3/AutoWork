"""Business logic for each production stage."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from cti_app.application.discovery_report_parser import extract_http_urls
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    ReferenceReport,
    TechnicalExtraction,
    reference_report_from_json,
    technical_extraction_from_json,
)
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.application.production_rendering import (
    build_reference_numbering,
    collect_indicators,
    render_brief,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)


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
    """Manages reference research stage."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def prepare_references_stage(
        self,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        subject_description: str,
        actor_info: str,
        technical_summary: str,
        research_date: str,
        period_start: str,
        period_end: str,
        existing_sources_text: str = "",
    ) -> dict[str, Any]:
        """Prepare input for references research stage.

        Returns prompt and input hash for model execution.
        """
        prompt = ProductionPromptTemplates.get_references_prompt(
            subject_title=subject_title,
            subject_description=subject_description,
            actor_info=actor_info,
            technical_summary=technical_summary,
            research_date=research_date,
            period_start=period_start,
            period_end=period_end,
            existing_sources_text=existing_sources_text,
        )

        input_data = {
            "subject_id": str(subject_id),
            "subject_title": subject_title,
            "research_date": research_date,
            "template_version": "1.0.0",
        }
        input_hash = compute_input_hash(input_data)

        return {
            "prompt": prompt,
            "input_hash": input_hash,
            "mode": "fresh",
        }

    async def store_references_result(
        self,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        raw_result: str,
        canonical_json: dict[str, Any],
        model_run_id: UUID | None = None,
        conversation_turn_id: UUID | None = None,
        warnings: list[str] | None = None,
    ) -> ProductionArtifact:
        """Store references research result as artifact."""
        async with self._uow_factory() as uow:
            # Get current version
            current = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.REFERENCES.value
            )
            version = (current.version + 1) if current else 1

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
                conversation_turn_id=conversation_turn_id,
                metadata={
                    "event_count": len(canonical_json.get("events", [])),
                    "source_count": len(canonical_json.get("sources", [])),
                    "warnings": warnings or [],
                    "parser_version": canonical_json.get("parser_version"),
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            await uow.production_artifacts.append(artifact)

            # Mark extraction/synthesis/brief as stale
            await uow.production_artifacts.mark_downstream_stale(
                run_id, ProductionArtifactStage.REFERENCES.value
            )

            await uow.commit()
            return artifact


class ExtractionService(_ArtifactPayloadMixin):
    """Manages technical CTI extraction stage."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def prepare_extraction_stage(
        self,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        references_artifact: ProductionArtifact,
    ) -> dict[str, Any]:
        """Prepare input for CTI extraction stage.

        Uses references artifact as context.
        Returns prompt and input hash.
        """
        prompt = ProductionPromptTemplates.get_extraction_prompt(
            subject_title=subject_title,
        )

        input_data = {
            "subject_id": str(subject_id),
            "references_artifact_id": str(references_artifact.id),
            "references_hash": references_artifact.input_hash,
            "template_version": "1.0.0",
        }
        input_hash = compute_input_hash(input_data)

        return {
            "prompt": prompt,
            "input_hash": input_hash,
            "mode": "continue",
        }

    async def store_extraction_result(
        self,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        raw_result: str,
        canonical_json: dict[str, Any],
        model_run_id: UUID | None = None,
        conversation_turn_id: UUID | None = None,
        warnings: list[str] | None = None,
    ) -> ProductionArtifact:
        """Store extraction result as artifact."""
        async with self._uow_factory() as uow:
            current = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.EXTRACTION.value
            )
            version = (current.version + 1) if current else 1

            # Count extracted elements
            element_counts = {
                category: len(items)
                for category, items in canonical_json.items()
                if isinstance(items, list)
            }

            raw_id, canonical_id, _ = await self._store_payloads(
                raw=raw_result, canonical=canonical_json
            )
            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.EXTRACTION,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                canonical_blob_id=canonical_id,
                model_run_id=model_run_id,
                conversation_turn_id=conversation_turn_id,
                metadata={
                    "element_counts": element_counts,
                    "warnings": warnings or [],
                    "parser_version": canonical_json.get("parser_version"),
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            await uow.production_artifacts.append(artifact)

            # Mark synthesis/brief as stale
            await uow.production_artifacts.mark_downstream_stale(
                run_id, ProductionArtifactStage.EXTRACTION.value
            )

            await uow.commit()
            return artifact


class SynthesisService(_ArtifactPayloadMixin):
    """Manages technical synthesis stage."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def prepare_synthesis_stage(
        self,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        extraction_artifact: ProductionArtifact,
    ) -> dict[str, Any]:
        """Prepare input for technical synthesis stage.

        Uses extraction artifact as context.
        Returns prompt and input hash.
        """
        prompt = ProductionPromptTemplates.get_synthesis_prompt(
            subject_title=subject_title,
        )

        input_data = {
            "subject_id": str(subject_id),
            "extraction_artifact_id": str(extraction_artifact.id),
            "extraction_hash": extraction_artifact.input_hash,
            "template_version": "1.0.0",
        }
        input_hash = compute_input_hash(input_data)

        return {
            "prompt": prompt,
            "input_hash": input_hash,
            "mode": "continue",
        }

    async def store_synthesis_result(
        self,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        raw_result: str,
        markdown_content: str,
        model_run_id: UUID | None = None,
        conversation_turn_id: UUID | None = None,
    ) -> ProductionArtifact:
        """Store synthesis result as artifact."""
        async with self._uow_factory() as uow:
            current = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.SYNTHESIS.value
            )
            version = (current.version + 1) if current else 1

            # Extract word count and reference count
            word_count = len(markdown_content.split())
            reference_count = markdown_content.count("[S")

            raw_id, _, rendered_id = await self._store_payloads(
                raw=raw_result, rendered=markdown_content
            )
            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.SYNTHESIS,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                rendered_blob_id=rendered_id,
                model_run_id=model_run_id,
                conversation_turn_id=conversation_turn_id,
                metadata={
                    "word_count": word_count,
                    "reference_count": reference_count,
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            await uow.production_artifacts.append(artifact)

            # Mark brief as stale
            await uow.production_artifacts.mark_downstream_stale(
                run_id, ProductionArtifactStage.SYNTHESIS.value
            )

            await uow.commit()
            return artifact


class BriefAssemblyService(_ArtifactPayloadMixin):
    """Manages brief assembly stage (deterministic)."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def assemble_brief(
        self,
        run_id: UUID,
        subject_id: UUID,
        subject_title: str,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
    ) -> ProductionArtifact:
        """Render the final brief from the stored artifacts.

        Deterministic: no model call. Reads the real payloads rather than the
        counters kept in `metadata`.
        """
        report, extraction, synthesis_text = await self._load_inputs(
            references_artifact, extraction_artifact, synthesis_artifact
        )

        async with self._uow_factory() as uow:
            input_data = {
                "references_id": str(references_artifact.id),
                "references_hash": references_artifact.input_hash,
                "extraction_id": str(extraction_artifact.id),
                "extraction_hash": extraction_artifact.input_hash,
                "synthesis_id": str(synthesis_artifact.id),
                "synthesis_hash": synthesis_artifact.input_hash,
            }
            input_hash = compute_input_hash(input_data)

            current = await uow.production_artifacts.get_current(
                run_id, ProductionArtifactStage.BRIEF.value
            )
            version = (current.version + 1) if current else 1

            numbering = build_reference_numbering(report, synthesis_text)
            brief_markdown = render_brief(
                subject_title=subject_title,
                report=report,
                extraction=extraction,
                synthesis_text=synthesis_text,
                numbering=numbering,
            )

            raw_id, canonical_id, rendered_id = await self._store_payloads(
                canonical={
                    "title": subject_title,
                    "numbering": {sid: number for sid, number in numbering.items()},
                },
                rendered=brief_markdown,
            )
            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.BRIEF,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                canonical_blob_id=canonical_id,
                rendered_blob_id=rendered_id,
                metadata={
                    "word_count": len(brief_markdown.split()),
                    "reference_count": len(numbering),
                    "indicator_count": len(collect_indicators(extraction)),
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            await uow.production_artifacts.append(artifact)
            await uow.commit()
            return artifact

    async def _load_inputs(
        self,
        references_artifact: ProductionArtifact,
        extraction_artifact: ProductionArtifact,
        synthesis_artifact: ProductionArtifact,
    ) -> tuple[ReferenceReport, TechnicalExtraction, str]:
        if self._artifact_store is None:
            raise ValueError("Brief assembly requires an artifact store")
        if references_artifact.canonical_blob_id is None:
            raise ValueError("References artifact has no canonical payload")
        if extraction_artifact.canonical_blob_id is None:
            raise ValueError("Extraction artifact has no canonical payload")
        if synthesis_artifact.rendered_blob_id is None:
            raise ValueError("Synthesis artifact has no rendered payload")
        report = reference_report_from_json(
            await self._artifact_store.read_json(references_artifact.canonical_blob_id)
        )
        extraction = technical_extraction_from_json(
            await self._artifact_store.read_json(extraction_artifact.canonical_blob_id)
        )
        synthesis_text = await self._artifact_store.read_text(synthesis_artifact.rendered_blob_id)
        return report, extraction, synthesis_text


class ProductionQAService:
    """Automated QA gate between assembly and READY.

    Every check answers one question: can a reader trust what this brief
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
        brief_artifact: ProductionArtifact | None,
        *,
        report: ReferenceReport | None = None,
        extraction: TechnicalExtraction | None = None,
        synthesis_text: str = "",
        brief_markdown: str = "",
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
        require("brief_present", brief_artifact is not None, "Brève manquante")

        for label, artifact in (
            ("references", references_artifact),
            ("extraction", extraction_artifact),
            ("synthesis", synthesis_artifact),
            ("brief", brief_artifact),
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
            require(
                "no_unknown_reference_in_items",
                all(
                    set(item.reference_ids) <= known_events
                    and set(item.source_ids) <= known_sources
                    for item in extraction.supported_items()
                ),
                "Un élément d'extraction cite une référence inconnue",
            )
            if not extraction.supported_items():
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

        if brief_markdown:
            used = {int(match.group(1)) for match in _BRIEF_FOOTNOTE.finditer(brief_markdown)}
            declared = {int(match.group(1)) for match in _BRIEF_DECLARED.finditer(brief_markdown)}
            require(
                "no_orphan_footnote",
                used <= declared,
                "La brève contient une note de bas de page orpheline",
            )

        passed = all(checks.values()) and not errors
        return {
            "passed": passed,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
        }


_SYNTHESIS_MARKER = re.compile(r"\[(S\d{1,3})\]", re.IGNORECASE)
_BRIEF_FOOTNOTE = re.compile(r"(?<!^)\[(\d{1,3})\]", re.MULTILINE)
_BRIEF_DECLARED = re.compile(r"^\[(\d{1,3})\]\s", re.MULTILINE)
