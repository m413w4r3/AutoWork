"""Persist canonical publication documents as production artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from cti_app.application.brief_publication_builder import (
    BRIEF_ASSEMBLY_POLICY_VERSION,
    build_brief_publication_document_v5,
    wrap_publication_document_v7,
)
from cti_app.application.diagram_compilation import (
    DIAGRAM_COMPILATION_POLICY_VERSION,
    DiagramCompiler,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    read_pointer,
    resolve_occurrence,
    resolve_paragraph_anchor,
)
from cti_app.application.media_assets import MediaAssetStore, compile_and_store_diagrams
from cti_app.application.persistence import ProductionArtifactRepository
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION,
    canonical_editorial_enrichment_hash,
    canonical_synthesis_hash,
    validate_editorial_enrichment,
)
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_synthesis import (
    canonical_extraction_hash,
)
from cti_app.application.publication_builder import (
    ARTICLE_FORMAT_POLICY_VERSION,
    ASSEMBLY_POLICY_VERSION,
    PUBLICATION_ANCHOR_ASSEMBLY_POLICY_VERSION,
    PublicationAssemblyValidationError,
    build_article_format_v1,
    build_publication_document_v5,
    compute_assembly_input_hash,
)
from cti_app.domain.editorial_workspace import (
    EditorialSourceStage,
    EditorialWorkspaceError,
    canonical_content_sha256,
)
from cti_app.domain.errors import BlobIntegrityError, EntityNotFoundError
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentV1,
    EnrichmentPlacementKind,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_references import ProductionReferenceCorpusV1
from cti_app.domain.production_relevance import RelevanceProjectionV1
from cti_app.domain.production_synthesis import ProductionSynthesisV1, production_synthesis_to_json
from cti_app.domain.publication import PublicationAssemblyErrorCode
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V8_SCHEMA_VERSION,
    PUBLICATION_DOCUMENT_V9_SCHEMA_VERSION,
    PublicationDocumentV8,
    PublicationDocumentV9,
    ResolvedPublicationPlacementV1,
    parse_publication_document,
    serialize_publication_document,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.source_media import (
    SourceFigureProvenanceDiagnostic,
    SourceFigureProvenanceStage,
)


class PublicationAssemblyResult(NamedTuple):
    """Publication plus its exact projection inputs.

    Returning refreshed diagram asset references keeps QA aligned without
    rewriting the fenced EDITORIAL_ENRICHMENT artifact.
    """

    artifact: ProductionArtifact
    editorial_enrichment: EditorialEnrichmentV1 | None


def _has_after_paragraph(enrichment: EditorialEnrichmentV1 | None) -> bool:
    return enrichment is not None and any(
        item.placement.kind is EnrichmentPlacementKind.AFTER_PARAGRAPH
        for collection in (
            enrichment.tables,
            enrichment.charts,
            enrichment.diagrams,
            enrichment.source_figures,
        )
        for item in collection
    )


def _resolve_editorial_placements(
    *,
    record: WorkspaceSnapshotRecord | None,
    run: ProductionRun,
    synthesis: ProductionSynthesisV1,
    enrichment: EditorialEnrichmentV1,
    enrichment_artifact_id: UUID | None,
) -> tuple[ResolvedPublicationPlacementV1, ...]:
    if record is None or (
        record.subject_id != run.subject_id
        or record.production_run_id != run.id
        or record.synthesis_canonical_sha256 != canonical_synthesis_hash(synthesis)
        or record.enrichment_canonical_sha256 != canonical_editorial_enrichment_hash(enrichment)
        or (
            enrichment_artifact_id is not None
            and record.enrichment_artifact_id != enrichment_artifact_id
        )
    ):
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
            "editorial_anchor_stale",
        )

    synthesis_payload = production_synthesis_to_json(synthesis)
    enrichment_payload = editorial_enrichment_to_json(enrichment)
    family_by_kind = {
        "table": "tables",
        "chart": "charts",
        "diagram": "diagrams",
        "figure": "source_figures",
    }
    placements: list[ResolvedPublicationPlacementV1] = []
    for entry in record.block_registry:
        if entry.source_stage is not EditorialSourceStage.EDITORIAL_ENRICHMENT:
            continue
        try:
            occurrence = resolve_occurrence(record, entry.block_id)
            family = family_by_kind[entry.kind.value]
            item = read_pointer(enrichment_payload, entry.structural_pointer)
            if (
                occurrence.binding_state != "valid"
                or not isinstance(item, Mapping)
                or canonical_content_sha256(item) != entry.content_sha256
            ):
                raise ValueError("editorial enrichment registry entry is stale")
            if entry.structural_pointer.split("/")[1] != family:
                raise ValueError("editorial enrichment registry family is inconsistent")
            placement = item.get("placement")
            if not isinstance(placement, Mapping):
                raise ValueError("editorial enrichment placement is missing")
            if placement.get("kind") != EnrichmentPlacementKind.AFTER_PARAGRAPH.value:
                continue
            anchor_block_id = UUID(str(placement["anchor_block_id"]))
            anchor = resolve_paragraph_anchor(record, anchor_block_id, synthesis=synthesis_payload)
            placements.append(
                ResolvedPublicationPlacementV1(
                    element_kind=entry.kind.value,
                    element_key=str(item["key"]),
                    element_position=entry.position,
                    anchor_block_id=anchor.block_id,
                    anchor_pointer=anchor.structural_pointer,
                    anchor_canonical_position=anchor.canonical_position,
                    paragraph_kind=anchor.paragraph_kind,
                    section_index=anchor.section_index,
                    paragraph_index=anchor.paragraph_index,
                )
            )
        except (EditorialWorkspaceError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
                "editorial_anchor_stale",
            ) from exc

    expected_count = sum(
        1
        for family in family_by_kind.values()
        for item in getattr(enrichment, family)
        if item.placement.kind is EnrichmentPlacementKind.AFTER_PARAGRAPH
    )
    if len(placements) != expected_count:
        raise PublicationAssemblyValidationError(
            PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
            "editorial_anchor_stale",
        )
    return tuple(
        sorted(
            placements,
            key=lambda item: (
                item.anchor_canonical_position,
                item.element_position,
                item.element_key,
            ),
        )
    )


class PublicationAssemblyService:
    """Build and persist the canonical publication for one production run."""

    def __init__(
        self,
        artifact_store: ProductionArtifactStore,
        production_artifacts: ProductionArtifactRepository,
        *,
        media_asset_store: MediaAssetStore | None = None,
        diagram_compiler: DiagramCompiler | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._production_artifacts = production_artifacts
        self._media_asset_store = media_asset_store
        self._diagram_compiler = diagram_compiler

    async def assemble_publication(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot,
        references: ProductionReferenceCorpusV1,
        extraction: ProductionExtractionV1,
        synthesis: ProductionSynthesisV1,
        editorial_enrichment: EditorialEnrichmentV1 | None,
        relevance_projection: RelevanceProjectionV1 | None = None,
        publication_kind: PublicationKind | None = None,
        editorial_enrichment_artifact_id: UUID | None = None,
        editorial_enrichment_artifact_input_hash: str | None = None,
        workspace_snapshot_record: WorkspaceSnapshotRecord | None = None,
        routing_decision_hash: str | None = None,
        routing_policy_version: str | None = None,
        metadata_extra: Mapping[str, Any] | None = None,
    ) -> PublicationAssemblyResult:
        resolved_placements: tuple[ResolvedPublicationPlacementV1, ...] = ()
        if (
            not isinstance(publication_kind, PublicationKind)
            or routing_decision_hash is None
            or routing_policy_version is None
        ):
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                "Assembly requires an explicit publication kind and routing decision",
            )
        if (
            snapshot.production_run_id != run.id
            or snapshot.edition_id != run.edition_id
            or snapshot.subject_id != run.subject_id
        ):
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                "Production input snapshot must belong to the production run",
            )
        if publication_kind is PublicationKind.ARTICLE:
            if editorial_enrichment is None:
                raise PublicationAssemblyValidationError(
                    PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                    "Article assembly requires editorial enrichment",
                )
            try:
                validate_editorial_enrichment(
                    editorial_enrichment, extraction=extraction, synthesis=synthesis
                )
            except ValueError as exc:
                raise PublicationAssemblyValidationError(
                    PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                    str(exc),
                ) from exc
            if _has_after_paragraph(editorial_enrichment):
                resolved_placements = _resolve_editorial_placements(
                    record=workspace_snapshot_record,
                    run=run,
                    synthesis=synthesis,
                    enrichment=editorial_enrichment,
                    enrichment_artifact_id=editorial_enrichment_artifact_id,
                )
            if editorial_enrichment.production_input_hash != snapshot.input_hash:
                raise PublicationAssemblyValidationError(
                    PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                    "Editorial enrichment does not match the production input snapshot",
                )
            editorial_enrichment = await self._refresh_diagram_assets(
                editorial_enrichment,
                production_run_id=run.id,
            )
            base_document = build_publication_document_v5(
                snapshot=snapshot,
                references=references,
                extraction=extraction,
                relevance_projection=relevance_projection,
                synthesis=synthesis,
                editorial_enrichment=editorial_enrichment,
            )
        else:
            if relevance_projection is None:
                raise PublicationAssemblyValidationError(
                    PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                    "Brief assembly requires a relevance projection",
                )
            if editorial_enrichment is not None:
                if (editorial_enrichment_artifact_id is None) != (
                    editorial_enrichment_artifact_input_hash is None
                ):
                    raise PublicationAssemblyValidationError(
                        PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                        "Brief enrichment artifact identity is incomplete",
                    )
                validate_editorial_enrichment(
                    editorial_enrichment, extraction=extraction, synthesis=synthesis
                )
                if _has_after_paragraph(editorial_enrichment):
                    resolved_placements = _resolve_editorial_placements(
                        record=workspace_snapshot_record,
                        run=run,
                        synthesis=synthesis,
                        enrichment=editorial_enrichment,
                        enrichment_artifact_id=editorial_enrichment_artifact_id,
                    )
                if editorial_enrichment.production_input_hash != snapshot.input_hash:
                    raise PublicationAssemblyValidationError(
                        PublicationAssemblyErrorCode.INPUTS_MISMATCH,
                        "Editorial enrichment does not match the production input snapshot",
                    )
                editorial_enrichment = await self._refresh_diagram_assets(
                    editorial_enrichment,
                    production_run_id=run.id,
                )
            base_document = build_brief_publication_document_v5(
                snapshot=snapshot,
                references=references,
                extraction=extraction,
                relevance_projection=relevance_projection,
                synthesis=synthesis,
                editorial_enrichment=editorial_enrichment,
            )
            if editorial_enrichment is not None and not (
                base_document.document.tables
                or base_document.document.diagrams
                or base_document.document.figures
                or base_document.document.charts
            ):
                empty_document = build_brief_publication_document_v5(
                    snapshot=snapshot,
                    references=references,
                    extraction=extraction,
                    relevance_projection=relevance_projection,
                    synthesis=synthesis,
                )
                if base_document.semantic_text == empty_document.semantic_text:
                    base_document = empty_document
                    editorial_enrichment = None
                    editorial_enrichment_artifact_id = None
                    editorial_enrichment_artifact_input_hash = None
                    resolved_placements = ()

        assembly_metadata_extra = dict(metadata_extra or {})
        if publication_kind is PublicationKind.BRIEF and editorial_enrichment is None:
            input_artifacts = assembly_metadata_extra.get("input_artifacts")
            if isinstance(input_artifacts, Mapping):
                filtered_artifacts = dict(input_artifacts)
                filtered_artifacts.pop("editorial_enrichment_artifact_id", None)
                assembly_metadata_extra["input_artifacts"] = filtered_artifacts
            diagnostics = assembly_metadata_extra.get("diagnostics")
            if isinstance(diagnostics, Mapping):
                filtered_diagnostics = dict(diagnostics)
                filtered_diagnostics.pop("editorial_enrichment", None)
                assembly_metadata_extra["diagnostics"] = filtered_diagnostics

        assembly_input_hash = self._assembly_input_hash(
            publication_kind=publication_kind,
            snapshot=snapshot,
            references=references,
            extraction=extraction,
            synthesis=synthesis,
            editorial_enrichment=editorial_enrichment,
            editorial_enrichment_artifact_id=editorial_enrichment_artifact_id,
            editorial_enrichment_artifact_input_hash=editorial_enrichment_artifact_input_hash,
            workspace_snapshot_record=workspace_snapshot_record,
            relevance_projection=relevance_projection,
            routing_decision_hash=routing_decision_hash,
            routing_policy_version=routing_policy_version,
        )
        published_figure_diagnostics: list[dict[str, str | None]] = []
        if editorial_enrichment is not None:
            source_figures_by_key = {item.key: item for item in editorial_enrichment.source_figures}
            figure_handles_by_id = {
                item.figure_id: item.handle for item in editorial_enrichment.figure_decisions
            }
            for publication_figure in base_document.figures:
                source_figure_candidate = source_figures_by_key.get(publication_figure.key)
                resolved = (
                    source_figure_candidate.resolved_figure
                    if source_figure_candidate is not None
                    else None
                )
                if (
                    source_figure_candidate is None
                    or resolved is None
                    or resolved.blob_id is None
                    or resolved.sha256 != publication_figure.sha256
                    or resolved.mime_type != publication_figure.mime_type
                    or resolved.byte_size != publication_figure.byte_size
                    or source_figure_candidate.source_document_id
                    != publication_figure.source_document_id
                ):
                    raise PublicationAssemblyValidationError(
                        PublicationAssemblyErrorCode.SOURCE_FIGURE_INVALID,
                        "Published source figure does not match its archived local media",
                    )
                published_figure_diagnostics.append(
                    SourceFigureProvenanceDiagnostic(
                        stage=SourceFigureProvenanceStage.PUBLISHED,
                        source_document_id=resolved.source_document_id,
                        figure_id=resolved.figure_id,
                        figure_handle=figure_handles_by_id.get(resolved.figure_id),
                        blob_id=resolved.blob_id,
                        sha256=resolved.sha256,
                    ).to_json()
                )

        stage = ProductionArtifactStage.PUBLICATION
        current = await self._production_artifacts.get_current(run.id, stage.value)
        reusable_base = None
        if (
            current is not None
            and current.input_hash == assembly_input_hash
            and current.status is ProductionArtifactStatus.VERIFIED
            and current.canonical_blob_id is not None
        ):
            reusable_base = current
        if reusable_base is None:
            candidate = await self._production_artifacts.find_reusable(
                edition_id=run.edition_id,
                subject_id=run.subject_id,
                stage=stage.value,
                input_hash=assembly_input_hash,
            )
            if (
                candidate is not None
                and candidate.stage is stage
                and candidate.status is ProductionArtifactStatus.VERIFIED
                and candidate.canonical_blob_id is not None
            ):
                reusable_base = candidate
        publication_artifact_id = uuid4()
        if reusable_base is not None:
            base_identity = await self._base_publication_identity(reusable_base)
            if base_identity is None:
                reusable_base = None
            else:
                publication_artifact_id = base_identity

        def canonical_v8(
            base_publication_artifact_id: UUID,
        ) -> tuple[PublicationDocumentV8, dict[str, Any], bytes]:
            base_v7 = wrap_publication_document_v7(
                base_document,
                base_publication_artifact_id=base_publication_artifact_id,
                publication_kind=publication_kind,
                routing_decision_hash=routing_decision_hash,
                routing_policy_version=routing_policy_version,
            )
            article_format = (
                build_article_format_v1(base_document, extraction)
                if publication_kind is PublicationKind.ARTICLE
                else None
            )
            publication_v8 = PublicationDocumentV8(
                base_publication=base_v7,
                article_format=article_format,
            )
            publication: PublicationDocumentV8 = publication_v8
            if _has_after_paragraph(editorial_enrichment):
                if workspace_snapshot_record is None:
                    raise PublicationAssemblyValidationError(
                        PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
                        "editorial_anchor_stale",
                    )
                enrichment_canonical_sha256 = workspace_snapshot_record.enrichment_canonical_sha256
                if enrichment_canonical_sha256 is None:
                    raise PublicationAssemblyValidationError(
                        PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
                        "editorial_anchor_stale",
                    )
                publication = PublicationDocumentV9(
                    base_publication=base_v7,
                    article_format=article_format,
                    registry_hash=workspace_snapshot_record.registry_hash,
                    synthesis_canonical_sha256=workspace_snapshot_record.synthesis_canonical_sha256,
                    enrichment_canonical_sha256=enrichment_canonical_sha256,
                    resolved_placements=resolved_placements,
                )
            payload = serialize_publication_document(publication)
            return (
                publication,
                payload,
                ProductionArtifactStore.canonical_json_bytes(payload),
            )

        _document, canonical_document, canonical_bytes = canonical_v8(publication_artifact_id)
        metadata = dict(assembly_metadata_extra)
        if publication_kind is PublicationKind.BRIEF:
            metadata["brief_assembly_policy_version"] = BRIEF_ASSEMBLY_POLICY_VERSION
        if published_figure_diagnostics:
            existing_figure_diagnostics = metadata.get("source_figure_provenance_diagnostics")
            metadata["source_figure_provenance_version"] = (
                SOURCE_FIGURE_PROVENANCE_DIAGNOSTICS_VERSION
            )
            metadata["source_figure_provenance_diagnostics"] = [
                *(
                    existing_figure_diagnostics
                    if isinstance(existing_figure_diagnostics, list)
                    else []
                ),
                *published_figure_diagnostics,
            ]
        upstream_diagnostics = metadata.pop("diagnostics", {})
        metadata["diagnostics"] = {
            "warnings_by_stage": {
                "references": list(references.warnings),
                "extraction": list(extraction.warnings),
                "synthesis": list(synthesis.warnings),
                **(
                    {"editorial_enrichment": list(editorial_enrichment.warnings)}
                    if editorial_enrichment is not None
                    else {}
                ),
            },
            "upstream": (
                dict(upstream_diagnostics) if isinstance(upstream_diagnostics, Mapping) else {}
            ),
        }
        if (
            current is not None
            and current.input_hash == assembly_input_hash
            and current.status is ProductionArtifactStatus.VERIFIED
            and current.canonical_blob_id is not None
            and (
                metadata_extra is None
                or current.metadata.get("input_artifacts")
                == assembly_metadata_extra.get("input_artifacts")
            )
            and current.metadata.get("diagnostics") == metadata.get("diagnostics")
        ):
            try:
                stored = await self._artifact_store.read_bytes(current.canonical_blob_id)
                if stored == canonical_bytes:
                    return PublicationAssemblyResult(current, editorial_enrichment)
            except (BlobIntegrityError, EntityNotFoundError, FileNotFoundError):
                pass
        if reusable_base is not None and current is not None and reusable_base.id == current.id:
            reusable_base = None
            publication_artifact_id = uuid4()
            _document, canonical_document, canonical_bytes = canonical_v8(publication_artifact_id)

        prior_versions = [
            artifact.version
            for artifact in await self._production_artifacts.list_for_run(run.id)
            if artifact.stage is stage
        ]
        version = max(prior_versions, default=0) + 1

        if reusable_base is not None and reusable_base.id != (current.id if current else None):
            if reusable_base.canonical_blob_id is None:
                stored_candidate = b""
            else:
                try:
                    stored_candidate = await self._artifact_store.read_bytes(
                        reusable_base.canonical_blob_id
                    )
                except (BlobIntegrityError, EntityNotFoundError, FileNotFoundError):
                    stored_candidate = b""
            if stored_candidate != canonical_bytes:
                reusable_base = None
                publication_artifact_id = uuid4()
                _document, canonical_document, canonical_bytes = canonical_v8(
                    publication_artifact_id
                )
        if reusable_base is not None and reusable_base.id != (current.id if current else None):
            reused_metadata = dict(metadata)
            reused_metadata.update(
                {
                    "reused": True,
                    "reused_from_artifact_id": str(reusable_base.id),
                    "reused_from_created_at": reusable_base.created_at.isoformat(),
                }
            )
            artifact = ProductionArtifact(
                id=uuid4(),
                production_run_id=run.id,
                subject_id=run.subject_id,
                stage=stage,
                version=version,
                input_hash=assembly_input_hash,
                canonical_blob_id=reusable_base.canonical_blob_id,
                reused_from_artifact_id=reusable_base.id,
                metadata=reused_metadata,
            )
            self._set_repair_result_id(artifact)
            await self._production_artifacts.append(artifact)
            return PublicationAssemblyResult(artifact, editorial_enrichment)

        _, canonical_blob_id, _ = await self._artifact_store.store_stage_payloads(
            canonical=canonical_document
        )
        if canonical_blob_id is None:
            raise RuntimeError("Canonical publication body was not persisted")

        artifact = ProductionArtifact(
            id=publication_artifact_id,
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=version,
            input_hash=assembly_input_hash,
            canonical_blob_id=canonical_blob_id,
            metadata=metadata,
        )
        self._set_repair_result_id(artifact)
        await self._production_artifacts.append(artifact)
        return PublicationAssemblyResult(artifact, editorial_enrichment)

    @staticmethod
    def _assembly_input_hash(
        *,
        publication_kind: PublicationKind,
        snapshot: ProductionInputSnapshot,
        references: ProductionReferenceCorpusV1,
        extraction: ProductionExtractionV1,
        synthesis: ProductionSynthesisV1,
        editorial_enrichment: EditorialEnrichmentV1 | None,
        editorial_enrichment_artifact_id: UUID | None = None,
        editorial_enrichment_artifact_input_hash: str | None = None,
        workspace_snapshot_record: WorkspaceSnapshotRecord | None = None,
        relevance_projection: RelevanceProjectionV1 | None,
        routing_decision_hash: str,
        routing_policy_version: str,
    ) -> str:
        if publication_kind is PublicationKind.ARTICLE:
            if editorial_enrichment is None:
                raise ValueError("Article assembly input hash requires editorial enrichment")
            content_hash = compute_assembly_input_hash(
                snapshot=snapshot,
                references=references,
                extraction=extraction,
                synthesis=synthesis,
                editorial_enrichment=editorial_enrichment,
                relevance_projection=relevance_projection,
            )
            if _has_after_paragraph(editorial_enrichment):
                if workspace_snapshot_record is None:
                    raise PublicationAssemblyValidationError(
                        PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
                        "editorial_anchor_stale",
                    )
                content_hash = hashlib.sha256(
                    ProductionArtifactStore.canonical_json_bytes(
                        {
                            "content_input_hash": content_hash,
                            "registry_hash": workspace_snapshot_record.registry_hash,
                        }
                    )
                ).hexdigest()
        else:
            if relevance_projection is None:
                raise ValueError("Brief assembly input hash requires relevance projection")
            is_v9 = _has_after_paragraph(editorial_enrichment)
            document_schema_version = (
                PUBLICATION_DOCUMENT_V9_SCHEMA_VERSION
                if is_v9
                else PUBLICATION_DOCUMENT_V8_SCHEMA_VERSION
            )
            brief_identity: dict[str, Any] = {
                "snapshot_input_hash": snapshot.input_hash,
                "references_hash": references_corpus_hash(references),
                "extraction_hash": canonical_extraction_hash(extraction),
                "synthesis_hash": canonical_synthesis_hash(synthesis),
                "relevance_projection_hash": relevance_projection.projection_hash,
                "publication_document_schema_version": document_schema_version,
                "brief_assembly_policy_version": BRIEF_ASSEMBLY_POLICY_VERSION,
                "article_format_policy_version": ARTICLE_FORMAT_POLICY_VERSION,
            }
            if editorial_enrichment is not None:
                if is_v9:
                    if workspace_snapshot_record is None or (
                        workspace_snapshot_record.subject_id != snapshot.subject_id
                        or workspace_snapshot_record.production_run_id != snapshot.production_run_id
                        or workspace_snapshot_record.synthesis_canonical_sha256
                        != canonical_synthesis_hash(synthesis)
                        or workspace_snapshot_record.enrichment_canonical_sha256
                        != canonical_editorial_enrichment_hash(editorial_enrichment)
                    ):
                        raise PublicationAssemblyValidationError(
                            PublicationAssemblyErrorCode.EDITORIAL_ANCHOR_STALE,
                            "editorial_anchor_stale",
                        )
                    brief_identity["registry_hash"] = workspace_snapshot_record.registry_hash
                brief_identity["editorial_enrichment_hash"] = canonical_editorial_enrichment_hash(
                    editorial_enrichment
                )
                if editorial_enrichment_artifact_input_hash is not None:
                    brief_identity["editorial_enrichment_input_hash"] = (
                        editorial_enrichment_artifact_input_hash
                    )
                if editorial_enrichment_artifact_id is not None:
                    brief_identity["editorial_enrichment_artifact_id"] = str(
                        editorial_enrichment_artifact_id
                    )
            content_hash = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(brief_identity)
            ).hexdigest()
        is_v9 = _has_after_paragraph(editorial_enrichment)
        document_schema_version = (
            PUBLICATION_DOCUMENT_V9_SCHEMA_VERSION
            if is_v9
            else PUBLICATION_DOCUMENT_V8_SCHEMA_VERSION
        )
        assembly_policy_version = (
            PUBLICATION_ANCHOR_ASSEMBLY_POLICY_VERSION if is_v9 else ASSEMBLY_POLICY_VERSION
        )
        return hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(
                {
                    "content_input_hash": content_hash,
                    "publication_kind": publication_kind.value,
                    "routing_decision_hash": routing_decision_hash,
                    "routing_policy_version": routing_policy_version,
                    "publication_document_schema_version": document_schema_version,
                    "assembly_policy_version": assembly_policy_version,
                    "article_format_policy_version": ARTICLE_FORMAT_POLICY_VERSION,
                }
            )
        ).hexdigest()

    async def _refresh_diagram_assets(
        self,
        enrichment: EditorialEnrichmentV1,
        *,
        production_run_id: UUID,
    ) -> EditorialEnrichmentV1:
        """Recompile stale derived SVGs from saved specs without a model call."""
        media_asset_store = self._media_asset_store
        diagram_compiler = self._diagram_compiler
        if not enrichment.diagrams or media_asset_store is None or diagram_compiler is None:
            return enrichment

        stale_diagrams = []
        for diagram in enrichment.diagrams:
            manifest = (
                await media_asset_store.get(diagram.compiled_asset_id)
                if diagram.compiled_asset_id is not None
                else None
            )
            if manifest is None or manifest.policy_version != DIAGRAM_COMPILATION_POLICY_VERSION:
                stale_diagrams.append(diagram)
        if not stale_diagrams:
            return enrichment

        compilation = await compile_and_store_diagrams(
            tuple(stale_diagrams),
            compiler=diagram_compiler,
            media_asset_store=media_asset_store,
            production_run_id=production_run_id,
        )
        compiled_by_key = {diagram.key: diagram for diagram in compilation.diagrams}
        if compilation.rejections or len(compiled_by_key) != len(stale_diagrams):
            rejected_keys = [rejection.diagram_key for rejection in compilation.rejections]
            if not rejected_keys:
                rejected_keys = [
                    diagram.key for diagram in stale_diagrams if diagram.key not in compiled_by_key
                ]
            raise PublicationAssemblyValidationError(
                PublicationAssemblyErrorCode.DIAGRAM_ASSET_MISSING,
                "Current diagram render assets could not be compiled: " + ",".join(rejected_keys),
            )

        diagrams = tuple(
            compiled_by_key.get(diagram.key, diagram) for diagram in enrichment.diagrams
        )
        return replace(enrichment, diagrams=diagrams)

    async def _base_publication_identity(self, artifact: ProductionArtifact) -> UUID | None:
        """Resolve the stable V6 identity embedded in a reusable V8 artifact."""
        if artifact.canonical_blob_id is None:
            return None
        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            publication = parse_publication_document(payload)
        except (BlobIntegrityError, EntityNotFoundError, FileNotFoundError, TypeError, ValueError):
            return None
        if not isinstance(publication, PublicationDocumentV8):
            return None
        return publication.base_publication.base_publication.provenance.base_publication_artifact_id

    @staticmethod
    def _set_repair_result_id(artifact: ProductionArtifact) -> None:
        marker = artifact.metadata.get("repair_materialization")
        if isinstance(marker, dict):
            marker = dict(marker)
            marker["result_publication_artifact_id"] = str(artifact.id)
            artifact.metadata["repair_materialization"] = marker
