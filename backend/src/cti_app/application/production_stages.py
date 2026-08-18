"""Business logic for each production stage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_prompts import ProductionPromptTemplates
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
        """Assemble brief from artifacts (deterministic).

        No LLM call - pure rendering.
        """
        async with self._uow_factory() as uow:
            # Compute input hash from all dependencies
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

            # Brief content would be assembled here from artifacts
            brief_markdown = self._render_brief(
                subject_title,
                references_artifact.metadata,
                extraction_artifact.metadata,
                synthesis_artifact.metadata,
            )

            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=ProductionArtifactStage.BRIEF,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                metadata={
                    "word_count": len(brief_markdown.split()),
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
            await uow.production_artifacts.append(artifact)
            await uow.commit()
            return artifact

    def _render_brief(
        self,
        subject_title: str,
        references_metadata: dict[str, Any],
        extraction_metadata: dict[str, Any],
        synthesis_metadata: dict[str, Any],
    ) -> str:
        """Render brief markdown from artifact metadata.

        Assembles references, synthesis, and IOC from extracted artifacts.
        """
        # Parse synthesis markdown content if available
        synthesis_content = synthesis_metadata.get("markdown_content", "")
        if not synthesis_content:
            synthesis_content = (
                f"(Synthèse technique — {synthesis_metadata.get('word_count', 0)} mots)"
            )

        # Build references section
        references_section = "## Références\n\n"
        sources = references_metadata.get("sources", [])
        if sources:
            for i, source in enumerate(sources[:10], 1):
                url = source.get("url", "")
                title = source.get("title", "Source")
                references_section += f"[{i}] {title}\n"
                if url:
                    references_section += f"    {url}\n"
        else:
            references_section += (
                f"{references_metadata.get('source_count', 0)} publications identifiées\n"
            )

        # Build IOC section
        ioc_section = "## Indicateurs de Compromission (IOC)\n\n"
        indicators = extraction_metadata.get("indicators", {})
        if indicators:
            # Network indicators
            if indicators.get("network_artifacts"):
                ioc_section += "### Artefacts Réseau\n\n"
                for artifact in indicators["network_artifacts"][:20]:
                    ioc_section += f"- {artifact}\n"
            # Malware hashes
            if indicators.get("malware_hashes"):
                ioc_section += "\n### Hachés de Malware\n\n"
                for hash_val in indicators["malware_hashes"][:10]:
                    ioc_section += f"- {hash_val}\n"
            # CVEs
            if indicators.get("cves"):
                ioc_section += "\n### Vulnérabilités (CVE)\n\n"
                for cve in indicators["cves"][:10]:
                    ioc_section += f"- {cve}\n"
        else:
            network_artifacts = extraction_metadata.get("element_counts", {}).get(
                "network_artifacts", 0
            )
            ioc_section += f"{network_artifacts} artefacts réseau identifiés\n"

        # Build final brief
        brief = f"""# {subject_title}

## Contexte

Analyse produite automatiquement basée sur les sources collectées et l'analyse technique.

{references_section}

## Synthèse Technique

{synthesis_content}

{ioc_section}

---

*Brève générée automatiquement par le système de production CTI.*
"""

        return brief


class ProductionQAService:
    """Automated QA checks for production."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def run_qa(
        self,
        run_id: UUID,
        references_artifact: ProductionArtifact | None,
        extraction_artifact: ProductionArtifact | None,
        synthesis_artifact: ProductionArtifact | None,
        brief_artifact: ProductionArtifact | None,
    ) -> dict[str, Any]:
        """Run QA checks on production artifacts.

        Returns:
            {
                "passed": bool,
                "checks": {check_name: bool},
                "errors": [error messages],
                "warnings": [warning messages],
            }
        """
        checks = {}
        errors = []
        warnings: list[str] = []

        # Check 1: References present
        checks["references_present"] = references_artifact is not None
        if not checks["references_present"]:
            errors.append("References artifact missing")

        # Check 2: Extraction present
        checks["extraction_present"] = extraction_artifact is not None
        if not checks["extraction_present"]:
            errors.append("Extraction artifact missing")

        # Check 3: Synthesis present
        checks["synthesis_present"] = synthesis_artifact is not None
        if not checks["synthesis_present"]:
            errors.append("Synthesis artifact missing")

        # Check 4: Brief present
        checks["brief_present"] = brief_artifact is not None
        if not checks["brief_present"]:
            errors.append("Brief artifact missing")

        # Check 5: No stale artifacts
        if references_artifact:
            checks["no_stale_references"] = (
                references_artifact.status != ProductionArtifactStatus.STALE.value
            )
        if extraction_artifact:
            checks["no_stale_extraction"] = (
                extraction_artifact.status != ProductionArtifactStatus.STALE.value
            )
        if synthesis_artifact:
            checks["no_stale_synthesis"] = (
                synthesis_artifact.status != ProductionArtifactStatus.STALE.value
            )
        if brief_artifact:
            checks["no_stale_brief"] = brief_artifact.status != ProductionArtifactStatus.STALE.value

        # Overall result
        passed = all(checks.values()) and not errors

        return {
            "passed": passed,
            "checks": checks,
            "errors": errors,
            "warnings": warnings,
        }
