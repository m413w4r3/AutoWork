"""Business logic for each production stage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
    EDITORIAL_ENRICHMENT_VALIDATOR_VERSION,
    EditorialEnrichmentRevisionConflictError,
    compute_editorial_enrichment_input_hash,
    validate_editorial_enrichment,
)
from cti_app.application.production_extraction import (
    production_extraction_metadata,
)
from cti_app.application.production_references import (
    PRODUCTION_REFERENCE_PARSER_VERSION,
    production_reference_corpus_metadata,
    production_reference_corpus_to_json,
)
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
    render_synthesis_markdown,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SynthesisMode,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentV1,
    editorial_enrichment_to_json,
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
    production_synthesis_to_json,
    synthesis_evidence_refs,
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

        The RAW answer is retained for historical import inspection; the
        canonical payload is the versioned corpus and nothing else.

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


class EditorialEnrichmentService(_ArtifactPayloadMixin):
    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def store_editorial_enrichment_result(
        self,
        *,
        run_id: UUID,
        subject_id: UUID,
        input_hash: str,
        enrichment: EditorialEnrichmentV1,
        extraction: ProductionExtractionV1,
        synthesis: ProductionSynthesisV1,
        raw_result: str | None,
        model_run_id: UUID | None,
        evidence_pack_hash: str,
        access_policy_hash: str,
        model_policy_version: str,
        routing_policy_version: str,
        projection_hash: str | None = None,
        source_figure_inventory_hash: str | None = None,
        metadata_extra: Mapping[str, Any] | None = None,
        artifact_id: UUID | None = None,
        expected_current_artifact_id: UUID | None = None,
        expected_current_canonical_sha256: str | None = None,
        canonical_payload: dict[str, Any] | None = None,
    ) -> ProductionArtifact:
        if self._artifact_store is None:
            raise ValueError("editorial_enrichment_inputs_missing")
        if enrichment.subject_id != subject_id:
            raise ValueError("editorial_enrichment_lineage_mismatch")
        validate_editorial_enrichment(enrichment, extraction=extraction, synthesis=synthesis)
        if input_hash != compute_editorial_enrichment_input_hash(
            extraction=extraction,
            synthesis=synthesis,
            evidence_pack_hash=evidence_pack_hash,
            access_policy_hash=access_policy_hash,
            projection_hash=projection_hash,
            source_figure_inventory_hash=source_figure_inventory_hash,
        ):
            raise ValueError("editorial_enrichment_lineage_mismatch")
        payload = canonical_payload or editorial_enrichment_to_json(enrichment)
        if editorial_enrichment_to_json(enrichment) != payload:
            from cti_app.domain.production_editorial_enrichment import (
                editorial_enrichment_from_json,
            )

            if editorial_enrichment_from_json(payload) != enrichment:
                raise ValueError("editorial_enrichment_revision_payload_mismatch")
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        stage = ProductionArtifactStage.EDITORIAL_ENRICHMENT
        async with self._uow_factory() as uow:
            current = (
                await uow.production_artifacts.get_current_for_revision(run_id, stage.value)
                if expected_current_artifact_id is not None
                else await uow.production_artifacts.get_current(run_id, stage.value)
            )
            if expected_current_artifact_id is not None and (
                current is None or current.id != expected_current_artifact_id
            ):
                raise EditorialEnrichmentRevisionConflictError(
                    "The Editorial Enrichment base is no longer current"
                )
            if expected_current_artifact_id is not None:
                if current is None or current.input_hash != input_hash:
                    raise EditorialEnrichmentRevisionConflictError(
                        "The Editorial Enrichment base hash changed"
                    )
                if expected_current_canonical_sha256 is not None:
                    assert current.canonical_blob_id is not None
                    current_bytes = await self._artifact_store.read_bytes(current.canonical_blob_id)
                    if (
                        hashlib.sha256(current_bytes).hexdigest()
                        != expected_current_canonical_sha256
                    ):
                        raise EditorialEnrichmentRevisionConflictError(
                            "The Editorial Enrichment base content hash changed"
                        )
            if (
                expected_current_artifact_id is None
                and current is not None
                and current.status is ProductionArtifactStatus.VERIFIED
                and current.input_hash == input_hash
                and current.canonical_blob_id is not None
                and await self._artifact_store.read_bytes(current.canonical_blob_id) == encoded
            ):
                return current
            previous = await uow.production_artifacts.list_for_run(run_id)
            version = max((item.version for item in previous if item.stage is stage), default=0) + 1
            raw_id, canonical_id, _ = await self._store_payloads(raw=raw_result, canonical=payload)
            if canonical_id is None:
                raise ValueError("editorial_enrichment_validation_failed")
            artifact_metadata = {
                "schema_version": enrichment.schema_version,
                "policy_version": enrichment.enrichment_policy_version,
                "generator_version": EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
                "validator_version": EDITORIAL_ENRICHMENT_VALIDATOR_VERSION,
                "model_policy_version": model_policy_version,
                "routing_policy_version": routing_policy_version,
                "evidence_pack_hash": evidence_pack_hash,
                "access_policy_hash": access_policy_hash,
                "relevance_projection_hash": projection_hash,
                "table_count": len(enrichment.tables),
                "diagram_count": len(enrichment.diagrams),
                "source_figure_count": len(enrichment.source_figures),
                "warnings_count": len(enrichment.warnings),
                "extraction_hash": enrichment.extraction_hash,
                "synthesis_hash": enrichment.synthesis_hash,
            }
            if source_figure_inventory_hash is not None:
                artifact_metadata["source_figure_inventory_hash"] = source_figure_inventory_hash
            if metadata_extra is not None:
                artifact_metadata.update(dict(metadata_extra))
            artifact = ProductionArtifact(
                production_run_id=run_id,
                subject_id=subject_id,
                stage=stage,
                version=version,
                input_hash=input_hash,
                status=ProductionArtifactStatus.VERIFIED,
                raw_blob_id=raw_id,
                canonical_blob_id=canonical_id,
                rendered_blob_id=None,
                model_run_id=model_run_id,
                conversation_turn_id=None,
                metadata=artifact_metadata,
                id=artifact_id or uuid4(),
            )
            await uow.production_artifacts.append(artifact)
            await uow.production_artifacts.mark_downstream_stale(run_id, stage.value)
            await uow.commit()
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
        projection_hash: str | None = None,
        diagnostics: Mapping[str, Any] | None = None,
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
            "relevance_projection_hash": projection_hash,
            "synthesis_policy_version": synthesis.synthesis_policy_version,
            "diagnostics": dict(diagnostics or {}),
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
