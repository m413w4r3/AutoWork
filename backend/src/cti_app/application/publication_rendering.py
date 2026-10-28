"""Application orchestration for versioned publication PDF renders."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from cti_app.application.diagram_compilation import CompiledDiagram
from cti_app.application.investigation_figures import build_investigation_graph_figure
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import PublicationRenderUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
)
from cti_app.application.render_target_identities import (
    publication_target_artifact_identities,
)
from cti_app.application.typst_compilation import (
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    CompiledTypstDocument,
    FontBundleInvalidError,
    FontBundleSnapshot,
    TypstCompilationError,
    TypstCompiler,
    load_font_bundle_snapshot,
)
from cti_app.application.typst_render_execution import (
    TypstRenderExecutionError,
    TypstRenderExecutionErrorTypes,
    TypstRenderExecutor,
    load_typst_render_bundle_snapshots,
    translate_execution_error,
)
from cti_app.application.typst_render_lifecycle import (
    TypstMappingSidecarCache,
    TypstRenderBuckets,
    TypstRenderError,
    TypstRenderErrors,
    TypstRenderRunner,
)
from cti_app.application.typst_render_output import (
    TypstRenderOutputIntegrityError,
    TypstRenderOutputStorageError,
    read_verified_render_pdf,
)
from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    TypstDraftPreviewDocument,
    TypstMediaRef,
    TypstRenderer,
    TypstRenderSource,
    TypstTemplateBundle,
    load_template_bundle,
)
from cti_app.domain.analyst_opinion import AnalystOpinionV1
from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.investigation import EvidenceObservation, InvestigationFinding
from cti_app.domain.investigation_graph import InvestigationGraph
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_from_json,
    serialize_publication_document,
)
from cti_app.domain.publication_render import (
    PUBLICATION_RENDER_POLICY_VERSION,
    PublicationRender,
    compute_publication_render_input_hash,
)
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus

PUBLICATION_RENDERER = "typst"
PUBLICATION_RENDERER_VERSION = "publication-v15-typst-v33-opinion-pipe-tables"
PUBLICATION_IDENTITY_LABEL_FIELD = "document_label"
PDF_LINEAGE_RENDER_DATA_FIELD = "pdf_lineage"


@dataclass(frozen=True, slots=True)
class DraftPublicationRender:
    compiled_document: CompiledTypstDocument
    render_data_sha256: str
    lineage: dict[str, Any]


def publication_identity_label(
    *,
    nature: str,
    artifact_id: UUID,
    artifact_version: int | None,
    artifact_sha256: str,
    opinion_id: UUID | None = None,
    opinion_version: int | None = None,
    opinion_status: str | None = None,
) -> str:
    """Return a display label based on persisted identities, never schema version."""
    artifact_version_label = (
        f"v{artifact_version}" if artifact_version is not None else "version inconnue"
    )
    if nature == "opinion_draft_preview":
        return (
            f"Aperçu d'avis {opinion_id} v{opinion_version} — {opinion_status}; "
            f"publication de base {artifact_id} {artifact_version_label} — "
            f"SHA-256 {artifact_sha256}"
        )
    if nature == "augmented_publication":
        return (
            f"Publication enrichie — avis {opinion_id} v{opinion_version} "
            f"({opinion_status}); artefact {artifact_id} {artifact_version_label} — "
            f"SHA-256 {artifact_sha256}"
        )
    return (
        f"Publication de base — artefact {artifact_id} {artifact_version_label} — "
        f"SHA-256 {artifact_sha256}"
    )


def with_publication_lineage(
    render_source: TypstRenderSource,
    *,
    identity_label: str,
    lineage: dict[str, Any],
) -> TypstRenderSource:
    """Add documented lineage to the canonical render-data bytes and their hash."""
    payload = json.loads(render_source.render_data_bytes)
    if not isinstance(payload, dict):
        raise ValueError("Publication render-data must be a JSON object")
    payload[PUBLICATION_IDENTITY_LABEL_FIELD] = identity_label
    payload[PDF_LINEAGE_RENDER_DATA_FIELD] = lineage
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return replace(
        render_source,
        render_data_bytes=encoded,
        render_data_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _version_identity_sha256(name: str, version: str) -> str:
    return hashlib.sha256(f"{name}\0{version}".encode()).hexdigest()


class InfrastructureGraphDiagramCompiler(Protocol):
    async def compile_source(self, *, diagram_key: str, source_bytes: bytes) -> CompiledDiagram: ...


PUBLICATION_RENDER_BUCKETS = TypstRenderBuckets(
    source="publication-renders-source",
    render_data="publication-renders-render-data",
    output="publication-renders-output",
)


class PublicationRenderError(TypstRenderError):
    """Base class for stable publication render failures."""

    code = "publication_render_error"


class PublicationRenderArtifactMissingError(PublicationRenderError):
    code = "publication_render_artifact_missing"


class PublicationRenderArtifactInvalidError(PublicationRenderError):
    code = "publication_render_artifact_invalid"


class PublicationRenderDocumentInvalidError(PublicationRenderError):
    code = "publication_render_document_invalid"


class PublicationRenderMediaMissingError(PublicationRenderError):
    code = "publication_render_media_missing"


class PublicationRenderMediaKindMismatchError(PublicationRenderError):
    code = "publication_render_media_kind_mismatch"


class PublicationRenderMediaIntegrityMismatchError(PublicationRenderError):
    code = "publication_render_media_integrity_mismatch"


class PublicationRenderTemplateInvalidError(PublicationRenderError):
    code = "publication_render_template_invalid"


class PublicationRenderStorageFailedError(PublicationRenderError):
    code = "publication_render_storage_failed"


class PublicationRenderInProgressError(PublicationRenderError):
    code = "publication_render_in_progress"


class PublicationRenderFailedError(PublicationRenderError):
    code = "publication_render_failed"


_PUBLICATION_RENDER_ERRORS = TypstRenderErrors(
    execution=TypstRenderExecutionErrorTypes(
        media_missing=PublicationRenderMediaMissingError,
        media_kind_mismatch=PublicationRenderMediaKindMismatchError,
        media_integrity_mismatch=PublicationRenderMediaIntegrityMismatchError,
        storage_failed=PublicationRenderStorageFailedError,
    ),
    in_progress=PublicationRenderInProgressError,
    unexpected=PublicationRenderFailedError,
    label="Publication",
)


@dataclass(frozen=True, slots=True)
class PublicationRenderAttempt:
    """Render lifecycle state for the publication review preview."""

    status: TypstRenderStatus
    render: PublicationRender | None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class _PreparedPublicationRender:
    proposed: PublicationRender
    template_bundle: TypstTemplateBundle
    font_bundle: FontBundleSnapshot
    build_source: Callable[[], TypstRenderSource]


class PublicationRenderService:
    def __init__(
        self,
        *,
        uow_factory: PublicationRenderUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        media_asset_store: MediaAssetStore,
        renderer: TypstRenderer,
        compiler: TypstCompiler,
        chp_typst_root: Path,
        font_bundle_root: Path,
        typst_fonts_lock_path: Path,
        graph_diagram_compiler: InfrastructureGraphDiagramCompiler | None = None,
        mapping_cache: TypstMappingSidecarCache | None = None,
        wait_poll_interval_seconds: float = 0.1,
        wait_timeout_seconds: float = 60.0,
        running_lease_seconds: float = 300.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._renderer = renderer
        self._media_asset_store = media_asset_store
        self._compiler = compiler
        if graph_diagram_compiler is None:
            from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler

            graph_diagram_compiler = D2DiagramCompiler()
        self._graph_diagram_compiler = graph_diagram_compiler
        self._preview_executor = TypstRenderExecutor(
            media_asset_store=media_asset_store,
            compiler=compiler,
        )
        self._chp_typst_root = chp_typst_root
        self._font_bundle_root = font_bundle_root
        self._typst_fonts_lock_path = typst_fonts_lock_path
        self._runner = TypstRenderRunner[PublicationRender](
            uow_factory=uow_factory,
            repository=lambda uow: uow.publication_renders,
            artifact_store=artifact_store,
            media_asset_store=media_asset_store,
            compiler=compiler,
            buckets=PUBLICATION_RENDER_BUCKETS,
            errors=_PUBLICATION_RENDER_ERRORS,
            mapping_cache=mapping_cache,
            wait_poll_interval_seconds=wait_poll_interval_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
            running_lease_seconds=running_lease_seconds,
        )

    async def render_draft_preview(
        self,
        document: PublicationDocumentV5,
        opinion: AnalystOpinionV1,
        synthesis: ProductionSynthesisV1 | None = None,
        *,
        base_publication_identity: dict[str, Any] | None = None,
        infrastructure_graph: InvestigationGraph | None = None,
        findings: tuple[InvestigationFinding, ...] = (),
        evidence_observations: tuple[EvidenceObservation, ...] = (),
    ) -> DraftPublicationRender:
        """Compile an ephemeral, watermarked draft without creating render state."""
        try:
            projection = TypstDraftPreviewDocument(
                document,
                opinion,
                synthesis,
                # Opinion previews must not materialize a new diagram SVG.
                None,
                findings,
                evidence_observations,
            )
        except ValueError as exc:
            raise PublicationRenderDocumentInvalidError(str(exc)) from exc
        try:
            template_bundle, font_bundle = load_typst_render_bundle_snapshots(
                chp_typst_root=self._chp_typst_root,
                font_bundle_root=self._font_bundle_root,
                typst_fonts_lock_path=self._typst_fonts_lock_path,
                manifest_name="renderer-manifest.json",
            )
        except (TemplateBundleInvalidError, FontBundleInvalidError) as exc:
            raise PublicationRenderTemplateInvalidError(str(exc)) from exc
        try:
            del infrastructure_graph
            base_publication_identity = base_publication_identity or {
                "artifact": {
                    "id": str(opinion.base_publication_artifact_id),
                    "version": None,
                    "sha256": opinion.base_publication_sha256,
                    "document_schema": "PublicationDocumentV5",
                },
                "investigation_id": str(opinion.investigation_id),
                "production_run_id": None,
            }
            artifact_identity = base_publication_identity.get("artifact", {})
            lineage = {
                **base_publication_identity,
                "schema_version": "pdf-lineage.v1",
                "nature": "opinion_draft_preview",
                "opinion": {
                    "id": str(opinion.id),
                    "version": opinion.version,
                    "status": opinion.status.value,
                    "sha256": opinion.content_sha256,
                },
                "renderer": {
                    "name": PUBLICATION_RENDERER,
                    "version": PUBLICATION_RENDERER_VERSION,
                    "identity_sha256": _version_identity_sha256(
                        PUBLICATION_RENDERER, PUBLICATION_RENDERER_VERSION
                    ),
                },
                "template": {
                    "version": template_bundle.template_version,
                    "sha256": template_bundle.sha256,
                },
                "compiler": {
                    "name": TYPST_COMPILER,
                    "version": TYPST_COMPILER_VERSION,
                    "identity_sha256": _version_identity_sha256(
                        TYPST_COMPILER, TYPST_COMPILER_VERSION
                    ),
                    "binary_sha256": None,
                },
                "rendered_at": datetime.now(UTC).isoformat(),
            }
            label = publication_identity_label(
                nature="opinion_draft_preview",
                artifact_id=UUID(str(artifact_identity["id"])),
                artifact_version=(
                    int(artifact_identity["version"])
                    if artifact_identity.get("version") is not None
                    else None
                ),
                artifact_sha256=str(artifact_identity["sha256"]),
                opinion_id=opinion.id,
                opinion_version=opinion.version,
                opinion_status=opinion.status.value,
            )
            render_source = self._renderer.render(
                projection,
                template_bundle,
                document_label=label,
            )
            render_source = with_publication_lineage(
                render_source,
                identity_label=label,
                lineage=lineage,
            )
            executed = await self._preview_executor.execute(
                render_source=render_source,
                template_bundle=template_bundle,
                font_bundle=font_bundle,
            )
        except TypstRenderExecutionError as exc:
            raise translate_execution_error(exc, _PUBLICATION_RENDER_ERRORS.execution) from exc
        except TemplateBundleInvalidError as exc:
            raise PublicationRenderTemplateInvalidError(str(exc)) from exc
        except ValueError as exc:
            raise PublicationRenderDocumentInvalidError(str(exc)) from exc
        except TypstCompilationError:
            raise
        except Exception as exc:
            raise PublicationRenderFailedError("Unable to compile the draft preview") from exc
        return DraftPublicationRender(
            compiled_document=executed.compiled_document,
            render_data_sha256=render_source.render_data_sha256,
            lineage=lineage,
        )

    async def render_pdf(self, publication_artifact_id: UUID) -> PublicationRender:
        async with self._uow_factory() as uow:
            pinned_render = await uow.publication_renders.get_pinned_succeeded_for_artifact(
                publication_artifact_id
            )
        pinned_output_error: (
            TypstRenderOutputStorageError | TypstRenderOutputIntegrityError | None
        ) = None
        if pinned_render is not None:
            try:
                await read_verified_render_pdf(self._artifact_store, pinned_render)
            except (TypstRenderOutputStorageError, TypstRenderOutputIntegrityError) as exc:
                pinned_output_error = exc
            else:
                # Publication artifacts are immutable; a bundle update is a new
                # render choice only when a new artifact version is published.
                return pinned_render
        prepared = await self._prepare(publication_artifact_id)
        if pinned_output_error is not None and pinned_render is not None:
            if pinned_render.input_hash != prepared.proposed.input_hash:
                raise pinned_output_error
        return await self._runner.run(
            prepared.proposed,
            template_bundle=prepared.template_bundle,
            font_bundle=prepared.font_bundle,
            build_source=prepared.build_source,
        )

    async def lineage_manifest_for_render(
        self,
        render: PublicationRender,
        *,
        is_current: bool | None = None,
        history_reasons: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Reconstruct the durable manifest from the immutable render row and data blob."""
        async with self._uow_factory() as uow:
            runtime_uow = cast(Any, uow)
            artifact = await uow.production_artifacts.get(render.publication_artifact_id)
            current_run = (
                await runtime_uow.production_runs.get_current_for_subject(artifact.subject_id)
                if artifact is not None
                else None
            )
            current_artifact = (
                await uow.production_artifacts.get_current(
                    artifact.production_run_id, ProductionArtifactStage.PUBLICATION.value
                )
                if artifact is not None
                else None
            )
            source_investigation = (
                await runtime_uow.analyst_investigations.get_for_run(artifact.production_run_id)
                if artifact is not None
                else None
            )
            current_investigation = (
                await runtime_uow.analyst_investigations.get_for_run(current_run.id)
                if current_run is not None
                else None
            )
            augmentation = (
                artifact.metadata.get("publication_augmentation")
                if artifact is not None and isinstance(artifact.metadata, dict)
                else None
            )
            opinion = None
            if isinstance(augmentation, dict):
                try:
                    opinion_version = augmentation.get("opinion_version")
                    opinion_id = augmentation.get("opinion_id")
                    if isinstance(opinion_version, int) and isinstance(opinion_id, str):
                        opinion = await runtime_uow.analyst_opinions.get(
                            UUID(opinion_id), version=opinion_version
                        )
                except (TypeError, ValueError):
                    opinion = None
            current_opinion = (
                await runtime_uow.analyst_opinions.get(opinion.id) if opinion is not None else None
            )
            await uow.commit()

        if artifact is None:
            raise PublicationRenderArtifactMissingError(
                f"Publication artifact {render.publication_artifact_id} does not exist"
            )
        if render.render_data_blob_id is None:
            render_data = b""
            render_data_payload: dict[str, Any] = {}
        else:
            render_data = await self._artifact_store.read_bytes(render.render_data_blob_id)
            try:
                parsed = json.loads(render_data)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PublicationRenderDocumentInvalidError(
                    "Persisted render-data is not valid JSON"
                ) from exc
            render_data_payload = parsed if isinstance(parsed, dict) else {}

        stored_lineage = render_data_payload.get(PDF_LINEAGE_RENDER_DATA_FIELD)
        if artifact.canonical_blob_id is None:
            raise PublicationRenderArtifactInvalidError(
                "Publication artifact has no canonical blob for lineage"
            )
        canonical_bytes = await self._artifact_store.read_bytes(artifact.canonical_blob_id)
        canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
        if isinstance(stored_lineage, dict):
            lineage = dict(stored_lineage)
        else:
            augmentation_dict = augmentation if isinstance(augmentation, dict) else None
            lineage = {
                "schema_version": "pdf-lineage.v1",
                "nature": (
                    "augmented_publication" if augmentation_dict is not None else "base_publication"
                ),
                "artifact": {
                    "id": str(artifact.id),
                    "version": artifact.version,
                    "sha256": canonical_sha256,
                    "document_schema": None,
                },
                "opinion": (
                    {
                        "id": str(augmentation_dict.get("opinion_id")),
                        "version": augmentation_dict.get("opinion_version"),
                        "status": opinion.status.value if opinion is not None else "unknown",
                        "sha256": str(augmentation_dict.get("opinion_content_sha256", "")),
                    }
                    if augmentation_dict is not None
                    else None
                ),
                "investigation_id": (
                    str(augmentation_dict.get("investigation_id"))
                    if augmentation_dict is not None
                    else str(current_investigation.id)
                    if current_investigation is not None
                    else None
                ),
                "production_run_id": str(artifact.production_run_id),
            }

        reasons = list(history_reasons)
        if current_run is None:
            reasons.append("current_production_run_unavailable")
        elif current_run.id != artifact.production_run_id:
            reasons.append("production_run_is_not_current")
        if current_artifact is None:
            reasons.append("current_publication_artifact_unavailable")
        elif current_artifact.id != artifact.id:
            reasons.append("publication_artifact_is_not_current")
        opinion_lineage = lineage.get("opinion")
        if isinstance(opinion_lineage, dict):
            pinned_investigation_id = opinion_lineage.get("investigation_id") or lineage.get(
                "investigation_id"
            )
            if current_investigation is None:
                reasons.append("current_investigation_unavailable")
            elif str(current_investigation.id) != str(pinned_investigation_id):
                reasons.append("pinned_opinion_investigation_mismatch")
            if opinion is None or current_opinion is None:
                reasons.append("pinned_opinion_identity_unavailable")
            elif (
                current_opinion.version != opinion.version
                or current_opinion.content_sha256 != opinion.content_sha256
            ):
                reasons.append("pinned_opinion_is_older_than_current")
            elif opinion_lineage.get("status") not in {None, opinion.status.value}:
                reasons.append("pinned_opinion_status_mismatch")
            opinion_lineage.update(
                {
                    "status": opinion.status.value if opinion is not None else "unknown",
                    "sha256": opinion.content_sha256
                    if opinion is not None
                    else opinion_lineage.get("sha256"),
                    "investigation_id": (
                        str(opinion.investigation_id)
                        if opinion is not None
                        else opinion_lineage.get("investigation_id")
                    ),
                }
            )
        if lineage.get("investigation_id") is None and source_investigation is not None:
            lineage["investigation_id"] = str(source_investigation.id)

        renderer_hash = _version_identity_sha256(render.renderer, render.renderer_version)
        compiler_hash = _version_identity_sha256(render.compiler, render.compiler_version)
        lineage["render_id"] = str(render.id)
        lineage["render_data_sha256"] = (
            hashlib.sha256(render_data).hexdigest() if render_data else None
        )
        lineage["renderer"] = {
            "name": render.renderer,
            "version": render.renderer_version,
            "identity_sha256": renderer_hash,
        }
        lineage["template"] = {
            "version": render.template_version,
            "sha256": render.template_sha256,
        }
        lineage["compiler"] = {
            "name": render.compiler,
            "version": render.compiler_version,
            "identity_sha256": compiler_hash,
            "binary_sha256": None,
        }
        lineage["rendered_at"] = render.created_at.isoformat()
        lineage["pdf_sha256"] = render.output_sha256
        lineage["is_current"] = not reasons and is_current is not False
        lineage["historical"] = not lineage["is_current"]
        lineage["history_reasons"] = list(dict.fromkeys(reasons))
        if render_data_payload.get(PUBLICATION_IDENTITY_LABEL_FIELD):
            lineage["document_label"] = render_data_payload[PUBLICATION_IDENTITY_LABEL_FIELD]
        artifact_lineage = lineage.get("artifact")
        if (
            lineage.get("nature") == "base_publication"
            and isinstance(artifact_lineage, dict)
            and artifact_lineage.get("document_schema") in {"6", "PublicationDocumentV6"}
        ):
            lineage["lineage_warning"] = "augmentation_metadata_absent_not_presented_as_enriched"
        return lineage

    async def render_preview(self, publication_artifact_id: UUID) -> PublicationRenderAttempt:
        """Run or inspect the shared render lifecycle without hiding its state."""
        try:
            prepared = await self._prepare(publication_artifact_id)
        except (PublicationRenderError, TypstCompilationError) as exc:
            return PublicationRenderAttempt(
                status=TypstRenderStatus.FAILED,
                render=None,
                error_code=exc.code,
                error_message=str(exc),
            )

        try:
            render = await self._runner.run(
                prepared.proposed,
                template_bundle=prepared.template_bundle,
                font_bundle=prepared.font_bundle,
                build_source=prepared.build_source,
            )
        except (PublicationRenderError, TypstCompilationError) as exc:
            async with self._uow_factory() as uow:
                persisted_render = await uow.publication_renders.get_by_input_hash(
                    prepared.proposed.input_hash
                )
            return PublicationRenderAttempt(
                status=(
                    persisted_render.status
                    if persisted_render is not None
                    else TypstRenderStatus.FAILED
                ),
                render=persisted_render,
                error_code=exc.code,
                error_message=str(exc),
            )

        return PublicationRenderAttempt(status=render.status, render=render)

    async def _prepare(self, publication_artifact_id: UUID) -> _PreparedPublicationRender:
        async with self._uow_factory() as uow:
            artifact = await uow.production_artifacts.get(publication_artifact_id)
            if artifact is None:
                raise PublicationRenderArtifactMissingError(
                    f"Publication artifact {publication_artifact_id} does not exist"
                )
            if (
                artifact.stage is not ProductionArtifactStage.PUBLICATION
                or artifact.status is not ProductionArtifactStatus.VERIFIED
                or artifact.canonical_blob_id is None
            ):
                raise PublicationRenderArtifactInvalidError(
                    "Publication render requires a verified PUBLICATION artifact "
                    "with a canonical blob"
                )
            target_artifact_identities = await publication_target_artifact_identities(
                uow.production_artifacts, artifact
            )
            base_publication_artifact_version: int | None = None
            base_publication_artifact_matches = False
            augmentation_metadata = artifact.metadata.get("publication_augmentation")
            raw_base_publication_artifact_id = (
                augmentation_metadata.get(
                    "base_publication_artifact_id",
                    artifact.metadata.get("previous_publication_artifact_id"),
                )
                if isinstance(augmentation_metadata, dict)
                else artifact.metadata.get("previous_publication_artifact_id")
            )
            try:
                base_publication_artifact_id = UUID(str(raw_base_publication_artifact_id))
            except (TypeError, ValueError, AttributeError):
                base_publication_artifact_id = None
            if base_publication_artifact_id is not None:
                base_publication_artifact = await uow.production_artifacts.get(
                    base_publication_artifact_id
                )
                if (
                    base_publication_artifact is not None
                    and base_publication_artifact.id == base_publication_artifact_id
                    and base_publication_artifact.stage is ProductionArtifactStage.PUBLICATION
                    and base_publication_artifact.subject_id == artifact.subject_id
                    and base_publication_artifact.production_run_id == artifact.production_run_id
                ):
                    base_publication_artifact_version = base_publication_artifact.version
                    base_publication_artifact_matches = True
            await uow.commit()

        try:
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
        except (ValueError, KeyError) as exc:
            raise PublicationRenderDocumentInvalidError(
                "Publication artifact does not contain valid canonical publication JSON"
            ) from exc
        except Exception as exc:
            raise PublicationRenderStorageFailedError(
                "Unable to read the canonical publication blob"
            ) from exc
        try:
            document = publication_document_from_json(payload)
        except (ValueError, KeyError) as exc:
            raise PublicationRenderDocumentInvalidError(
                "Publication artifact does not contain a valid canonical publication document"
            ) from exc

        if isinstance(document, PublicationDocumentV6) and not base_publication_artifact_matches:
            raise PublicationRenderArtifactInvalidError(
                "V6 publication base artifact cannot be resolved to a matching publication artifact"
            )

        reviewer_approval_decision: Any | None = None
        if isinstance(document, PublicationDocumentV6):
            provenance = document.provenance
            async with self._uow_factory() as uow:
                analyst_decisions = getattr(uow, "analyst_decisions", None)
                list_for_investigation = getattr(analyst_decisions, "list_for_investigation", None)
                if callable(list_for_investigation):
                    decisions = await list_for_investigation(provenance.investigation_id)
                    approval_decisions = [
                        decision
                        for decision in decisions
                        if decision.decision_type is AnalystDecisionType.NOTE_APPROVE
                        and decision.target_type is AnalystDecisionTargetType.NOTE
                        and decision.target_id == provenance.opinion_id
                    ]
                    if approval_decisions:
                        reviewer_approval_decision = max(
                            approval_decisions,
                            key=lambda decision: (decision.occurred_at, decision.id),
                        )
                        if reviewer_approval_decision.actor_id != provenance.reviewer_id:
                            raise PublicationRenderArtifactInvalidError(
                                "V6 reviewer provenance does not match its NOTE_APPROVE decision"
                            )

        publication_content_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(document))
        ).hexdigest()
        graph_media_ref: TypstMediaRef | None = None
        if isinstance(document, PublicationDocumentV6):
            graph_media_ref = await self._compile_infrastructure_graph(
                document.base_publication, document.infrastructure_graph
            )
        try:
            template_bundle = load_template_bundle(self._chp_typst_root)
            font_bundle = load_font_bundle_snapshot(
                self._font_bundle_root, self._typst_fonts_lock_path
            )
        except (TemplateBundleInvalidError, FontBundleInvalidError) as exc:
            raise PublicationRenderTemplateInvalidError(str(exc)) from exc
        template_version = template_bundle.template_version
        template_sha256 = template_bundle.sha256
        font_bundle_version = font_bundle.font_bundle_version

        input_hash = compute_publication_render_input_hash(
            publication_artifact_id=artifact.id,
            publication_content_sha256=publication_content_sha256,
            renderer=PUBLICATION_RENDERER,
            renderer_version=PUBLICATION_RENDERER_VERSION,
            template_version=template_version,
            template_sha256=template_sha256,
            compiler=TYPST_COMPILER,
            compiler_version=TYPST_COMPILER_VERSION,
            format=TypstRenderFormat.PDF,
            font_bundle_version=font_bundle_version,
            render_policy_version=PUBLICATION_RENDER_POLICY_VERSION,
        )
        now = datetime.now(UTC)
        proposed = PublicationRender(
            id=uuid4(),
            publication_artifact_id=artifact.id,
            renderer=PUBLICATION_RENDERER,
            renderer_version=PUBLICATION_RENDERER_VERSION,
            template_version=template_version,
            template_sha256=template_sha256,
            compiler=TYPST_COMPILER,
            compiler_version=TYPST_COMPILER_VERSION,
            font_bundle_version=font_bundle_version,
            render_policy_version=PUBLICATION_RENDER_POLICY_VERSION,
            format=TypstRenderFormat.PDF,
            input_hash=input_hash,
            source_blob_id=None,
            render_data_blob_id=None,
            output_blob_id=None,
            output_sha256=None,
            output_byte_size=None,
            status=TypstRenderStatus.RUNNING,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )

        augmentation = artifact.metadata.get("publication_augmentation")
        if isinstance(document, PublicationDocumentV6):
            if not isinstance(augmentation, dict):
                raise PublicationRenderArtifactInvalidError(
                    "A V6 publication requires its pinned augmentation metadata"
                )
            provenance = document.provenance
            if (
                str(provenance.base_publication_artifact_id)
                != str(
                    augmentation.get(
                        "base_publication_artifact_id",
                        artifact.metadata.get("previous_publication_artifact_id"),
                    )
                )
                or str(provenance.investigation_id) != str(augmentation.get("investigation_id"))
                or str(provenance.opinion_id) != str(augmentation.get("opinion_id"))
                or provenance.opinion_version != augmentation.get("opinion_version")
                or provenance.opinion_content_sha256 != augmentation.get("opinion_content_sha256")
            ):
                raise PublicationRenderArtifactInvalidError(
                    "V6 publication metadata does not match its document provenance"
                )
            nature = "augmented_publication"
            opinion_identity: dict[str, Any] | None = {
                "id": str(provenance.opinion_id),
                "version": provenance.opinion_version,
                "status": "approved",
                "sha256": provenance.opinion_content_sha256,
                "investigation_id": str(provenance.investigation_id),
            }
            investigation_id: str | None = str(provenance.investigation_id)
        else:
            if augmentation is not None:
                raise PublicationRenderArtifactInvalidError(
                    "A base publication cannot carry augmentation metadata"
                )
            nature = "base_publication"
            opinion_identity = None
            investigation_id = None

        lineage = {
            "schema_version": "pdf-lineage.v1",
            "nature": nature,
            "artifact": {
                "id": str(artifact.id),
                "version": artifact.version,
                "sha256": publication_content_sha256,
                "document_schema": document.schema_version,
            },
            "opinion": opinion_identity,
            "investigation_id": investigation_id,
            "production_run_id": str(artifact.production_run_id),
            "renderer": {
                "name": PUBLICATION_RENDERER,
                "version": PUBLICATION_RENDERER_VERSION,
                "identity_sha256": _version_identity_sha256(
                    PUBLICATION_RENDERER, PUBLICATION_RENDERER_VERSION
                ),
            },
            "template": {"version": template_version, "sha256": template_sha256},
            "compiler": {
                "name": TYPST_COMPILER,
                "version": TYPST_COMPILER_VERSION,
                "identity_sha256": _version_identity_sha256(TYPST_COMPILER, TYPST_COMPILER_VERSION),
                "binary_sha256": None,
            },
            "rendered_at": now.isoformat(),
        }
        if isinstance(document, PublicationDocumentV6):
            provenance = document.provenance
            lineage["actors"] = {
                "reviewer": {
                    "actor_id": provenance.reviewer_id,
                    "role": "reviewer",
                    "decision_label": "opinion_approved",
                    "investigation_id": str(provenance.investigation_id),
                    "opinion_id": str(provenance.opinion_id),
                    "opinion_version": provenance.opinion_version,
                    "opinion_content_sha256": provenance.opinion_content_sha256,
                    "decision_id": (
                        str(reviewer_approval_decision.id)
                        if reviewer_approval_decision is not None
                        else None
                    ),
                    "occurred_at": (
                        reviewer_approval_decision.occurred_at.isoformat()
                        if reviewer_approval_decision is not None
                        else None
                    ),
                    "decision_reference_status": (
                        "verified" if reviewer_approval_decision is not None else "unavailable"
                    ),
                },
                "approver": {
                    "actor_id": provenance.approver_id,
                    "role": "approver",
                    "decision_label": "publication_augmented",
                    "base_publication": {
                        "artifact_id": str(provenance.base_publication_artifact_id),
                        "version": base_publication_artifact_version,
                        "sha256": provenance.base_publication_sha256,
                    },
                    "augmented_publication": {
                        "artifact_id": str(artifact.id),
                        "version": artifact.version,
                        "sha256": publication_content_sha256,
                    },
                },
            }
        label = publication_identity_label(
            nature=nature,
            artifact_id=artifact.id,
            artifact_version=artifact.version,
            artifact_sha256=publication_content_sha256,
            opinion_id=UUID(opinion_identity["id"]) if opinion_identity else None,
            opinion_version=opinion_identity["version"] if opinion_identity else None,
            opinion_status=opinion_identity["status"] if opinion_identity else None,
        )

        def build_source() -> TypstRenderSource:
            try:
                source = self._renderer.render(
                    document,
                    template_bundle,
                    document_label=label,
                    target_artifact_identities=target_artifact_identities,
                    **(
                        {"infrastructure_graph_media_ref": graph_media_ref}
                        if graph_media_ref is not None
                        else {}
                    ),
                )
                return with_publication_lineage(
                    source,
                    identity_label=label,
                    lineage=lineage,
                )
            except TemplateBundleInvalidError as exc:
                raise PublicationRenderTemplateInvalidError(str(exc)) from exc
            except ValueError as exc:
                raise PublicationRenderDocumentInvalidError(str(exc)) from exc

        return _PreparedPublicationRender(
            proposed=proposed,
            template_bundle=template_bundle,
            font_bundle=font_bundle,
            build_source=build_source,
        )

    async def _compile_infrastructure_graph(
        self,
        publication: PublicationDocumentV5,
        graph: InvestigationGraph | None,
    ) -> TypstMediaRef | None:
        if graph is None:
            return None
        figure = build_investigation_graph_figure(publication, graph)
        if figure is None:
            return None
        source_bytes = str(figure["d2_source"]).encode("utf-8")
        try:
            compiled = await self._graph_diagram_compiler.compile_source(
                diagram_key=str(figure["figure_id"]), source_bytes=source_bytes
            )
        except Exception as exc:
            raise PublicationRenderFailedError(
                "Unable to compile the canonical infrastructure graph with D2"
            ) from exc
        if compiled.source_sha256 != figure["d2_source_sha256"]:
            raise PublicationRenderDocumentInvalidError(
                "Compiled infrastructure graph source does not match its pinned identity"
            )
        manifest = await self._media_asset_store.store_diagram(
            compiled,
            source=(f"investigation:{graph.investigation_id}:infrastructure_graph:{graph.sha256}"),
        )
        return TypstMediaRef(
            asset_id=manifest.asset_id,
            expected_kind=MediaAssetKind.DIAGRAM_SVG,
            expected_mime_type=manifest.mime_type,
            expected_sha256=manifest.sha256,
            expected_byte_size=manifest.byte_size,
            media_path=f"media/{manifest.asset_id}.svg",
        )
