"""Application orchestration for versioned publication PDF renders."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import PublicationRenderUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
)
from cti_app.application.typst_compilation import (
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    TYPST_MAX_PDF_BYTES,
    CompiledTypstDocument,
    FontBundleInvalidError,
    TypstCompilationError,
    TypstCompiler,
    TypstCompileRequest,
    compute_font_bundle_version,
)
from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    TypstRenderer,
    TypstRenderSource,
    compute_template_bundle_hash,
    resolve_template_bundle_files,
)
from cti_app.domain.errors import BlobIntegrityError, EntityNotFoundError
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_document import (
    publication_document_v4_from_json,
    publication_document_v4_to_json,
)
from cti_app.domain.publication_render import (
    PUBLICATION_RENDER_POLICY_VERSION,
    PublicationRender,
    PublicationRenderFormat,
    PublicationRenderStatus,
    compute_publication_render_input_hash,
)

PUBLICATION_RENDERER = "typst"
PUBLICATION_RENDERER_VERSION = "publication-v4-typst-v1"

PUBLICATION_RENDER_SOURCE_BUCKET = "publication-renders-source"
PUBLICATION_RENDER_DATA_BUCKET = "publication-renders-render-data"
PUBLICATION_RENDER_OUTPUT_BUCKET = "publication-renders-output"


class PublicationRenderError(Exception):
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
        font_paths: tuple[Path, ...],
        typst_fonts_lock_path: Path,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._media_asset_store = media_asset_store
        self._renderer = renderer
        self._compiler = compiler
        self._chp_typst_root = chp_typst_root
        self._font_paths = font_paths
        self._typst_fonts_lock_path = typst_fonts_lock_path

    async def render_pdf(self, publication_artifact_id: UUID) -> PublicationRender:
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

            try:
                payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            except (ValueError, KeyError) as exc:
                raise PublicationRenderDocumentInvalidError(
                    "Publication artifact does not contain valid PublicationDocumentV4 JSON"
                ) from exc
            except Exception as exc:
                raise PublicationRenderStorageFailedError(
                    "Unable to read the canonical publication blob"
                ) from exc
            try:
                document = publication_document_v4_from_json(payload)
            except (ValueError, KeyError) as exc:
                raise PublicationRenderDocumentInvalidError(
                    "Publication artifact does not contain a valid PublicationDocumentV4"
                ) from exc

            publication_content_sha256 = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(
                    publication_document_v4_to_json(document)
                )
            ).hexdigest()
            try:
                template_version, template_sha256 = compute_template_bundle_hash(
                    self._chp_typst_root
                )
                font_bundle_version = compute_font_bundle_version(
                    self._chp_typst_root, self._typst_fonts_lock_path
                )
            except (TemplateBundleInvalidError, FontBundleInvalidError) as exc:
                raise PublicationRenderTemplateInvalidError(str(exc)) from exc

            input_hash = compute_publication_render_input_hash(
                publication_artifact_id=artifact.id,
                publication_content_sha256=publication_content_sha256,
                renderer=PUBLICATION_RENDERER,
                renderer_version=PUBLICATION_RENDERER_VERSION,
                template_version=template_version,
                template_sha256=template_sha256,
                compiler=TYPST_COMPILER,
                compiler_version=TYPST_COMPILER_VERSION,
                format=PublicationRenderFormat.PDF,
                font_bundle_version=font_bundle_version,
                render_policy_version=PUBLICATION_RENDER_POLICY_VERSION,
            )
            existing = await uow.publication_renders.get_by_input_hash(input_hash)
            if existing is not None and existing.status is PublicationRenderStatus.SUCCEEDED:
                output_valid = False
                if existing.output_blob_id is not None:
                    try:
                        output = await self._artifact_store.read_bytes(
                            existing.output_blob_id,
                            max_bytes=TYPST_MAX_PDF_BYTES,
                        )
                        output_valid = (
                            len(output) == existing.output_byte_size
                            and hashlib.sha256(output).hexdigest() == existing.output_sha256
                        )
                    except Exception:
                        output_valid = False
                if output_valid:
                    await uow.commit()
                    return existing

            now = datetime.now(UTC)
            if existing is None:
                render_id = uuid4()
                running = PublicationRender(
                    id=render_id,
                    publication_artifact_id=artifact.id,
                    renderer=PUBLICATION_RENDERER,
                    renderer_version=PUBLICATION_RENDERER_VERSION,
                    template_version=template_version,
                    template_sha256=template_sha256,
                    compiler=TYPST_COMPILER,
                    compiler_version=TYPST_COMPILER_VERSION,
                    format=PublicationRenderFormat.PDF,
                    input_hash=input_hash,
                    source_blob_id=None,
                    render_data_blob_id=None,
                    output_blob_id=None,
                    output_sha256=None,
                    output_byte_size=None,
                    status=PublicationRenderStatus.RUNNING,
                    error_code=None,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
                await uow.publication_renders.add(running)
            else:
                render_id = existing.id
                await uow.publication_renders.mark_retrying(render_id)
            await uow.commit()

        source_blob_id: UUID | None = None
        render_data_blob_id: UUID | None = None
        try:
            try:
                render_source = self._renderer.render(document)
            except ValueError as exc:
                raise PublicationRenderDocumentInvalidError(str(exc)) from exc
            except OSError as exc:
                raise PublicationRenderTemplateInvalidError(str(exc)) from exc

            resolved_media: dict[UUID, bytes] = {}
            for media_ref in render_source.media_refs:
                try:
                    manifest = await self._media_asset_store.get(media_ref.asset_id)
                except Exception as exc:
                    raise PublicationRenderStorageFailedError(
                        f"Unable to load media manifest {media_ref.asset_id}"
                    ) from exc
                if manifest is None:
                    raise PublicationRenderMediaMissingError(
                        f"Media asset {media_ref.asset_id} does not exist"
                    )
                if media_ref.expected_kind is MediaAssetKind.DIAGRAM_SVG:
                    if (
                        manifest.kind is not MediaAssetKind.DIAGRAM_SVG
                        or manifest.mime_type != "image/svg+xml"
                    ):
                        raise PublicationRenderMediaKindMismatchError(
                            "Diagram media asset "
                            f"{media_ref.asset_id} has an invalid kind or MIME type"
                        )
                elif media_ref.expected_kind is MediaAssetKind.SOURCE_FIGURE:
                    if (
                        manifest.asset_id != media_ref.asset_id
                        or manifest.sha256 != media_ref.expected_sha256
                        or manifest.mime_type != media_ref.expected_mime_type
                        or manifest.byte_size != media_ref.expected_byte_size
                        or manifest.kind is not MediaAssetKind.SOURCE_FIGURE
                    ):
                        raise PublicationRenderMediaIntegrityMismatchError(
                            "Figure media asset "
                            f"{media_ref.asset_id} does not match its canonical reference"
                        )
                try:
                    resolved_media[media_ref.asset_id] = await self._media_asset_store.read(
                        media_ref.asset_id
                    )
                except BlobIntegrityError as exc:
                    raise PublicationRenderMediaIntegrityMismatchError(
                        f"Media asset {media_ref.asset_id} failed its integrity check"
                    ) from exc
                except EntityNotFoundError as exc:
                    raise PublicationRenderMediaMissingError(
                        f"Media asset {media_ref.asset_id} does not exist"
                    ) from exc
                except Exception as exc:
                    raise PublicationRenderStorageFailedError(
                        f"Unable to read media asset {media_ref.asset_id}"
                    ) from exc

            try:
                workspace = tempfile.TemporaryDirectory(prefix="autowork-publication-render-")
            except OSError as exc:
                raise PublicationRenderStorageFailedError(
                    "Unable to create the Typst render workspace"
                ) from exc
            with workspace as workspace_path:
                workspace_root = Path(workspace_path)
                self._populate_workspace(workspace_root, render_source, resolved_media)
                try:
                    source_blob_id = await self._artifact_store.put_bytes(
                        render_source.source_bytes,
                        bucket=PUBLICATION_RENDER_SOURCE_BUCKET,
                        mime_type="text/plain; charset=utf-8",
                    )
                    render_data_blob_id = await self._artifact_store.put_bytes(
                        render_source.render_data_bytes,
                        bucket=PUBLICATION_RENDER_DATA_BUCKET,
                        mime_type="application/json",
                    )
                except Exception as exc:
                    raise PublicationRenderStorageFailedError(
                        "Unable to persist Typst source or render data"
                    ) from exc

                compiled: CompiledTypstDocument = await self._compiler.compile(
                    TypstCompileRequest(
                        workspace_root=workspace_root,
                        entrypoint_relative_path="RENDERER/publication.typ",
                        font_paths=self._font_paths,
                    )
                )
                try:
                    output_blob_id = await self._artifact_store.put_bytes(
                        compiled.content,
                        bucket=PUBLICATION_RENDER_OUTPUT_BUCKET,
                        mime_type="application/pdf",
                    )
                except Exception as exc:
                    raise PublicationRenderStorageFailedError(
                        "Unable to persist the compiled publication PDF"
                    ) from exc

            async with self._uow_factory() as uow:
                succeeded = await uow.publication_renders.mark_succeeded(
                    render_id,
                    output_blob_id=output_blob_id,
                    output_sha256=compiled.sha256,
                    output_byte_size=compiled.byte_size,
                    source_blob_id=source_blob_id,
                    render_data_blob_id=render_data_blob_id,
                )
                await uow.commit()
            return succeeded
        except (PublicationRenderError, TypstCompilationError) as exc:
            try:
                async with self._uow_factory() as uow:
                    await uow.publication_renders.mark_failed(
                        render_id,
                        error_code=exc.code,
                        error_message=str(exc)[:500],
                        source_blob_id=source_blob_id,
                        render_data_blob_id=render_data_blob_id,
                    )
                    await uow.commit()
            except Exception as mark_failed_exc:
                exc.add_note(f"Could not persist PublicationRender failure: {mark_failed_exc}")
            raise

    def _populate_workspace(
        self,
        workspace_root: Path,
        render_source: TypstRenderSource,
        resolved_media: dict[UUID, bytes],
    ) -> None:
        try:
            bundle_files = resolve_template_bundle_files(self._chp_typst_root)
            for relative_path, source_path in bundle_files:
                destination = workspace_root.joinpath(*PurePosixPath(relative_path).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source_path.read_bytes())
        except TemplateBundleInvalidError as exc:
            raise PublicationRenderTemplateInvalidError(
                f"Unable to materialize Typst template bundle: {exc}"
            ) from exc
        except OSError as exc:
            raise PublicationRenderStorageFailedError(
                f"Unable to materialize Typst template bundle: {exc}"
            ) from exc

        renderer_directory = workspace_root / "RENDERER"
        try:
            renderer_directory.mkdir(parents=True, exist_ok=True)
            (renderer_directory / "render-data.json").write_bytes(render_source.render_data_bytes)
            media_directory = renderer_directory / "media"
            media_directory.mkdir(parents=True, exist_ok=True)
            for media_ref in render_source.media_refs:
                media_relative_path = PurePosixPath(media_ref.media_path)
                if media_relative_path.is_absolute() or "\\" in media_ref.media_path:
                    raise ValueError(f"Invalid workspace media path {media_ref.media_path!r}")
                media_path = renderer_directory.joinpath(*media_relative_path.parts)
                media_path.resolve().relative_to(renderer_directory.resolve())
                media_path.parent.mkdir(parents=True, exist_ok=True)
                media_path.write_bytes(resolved_media[media_ref.asset_id])
        except (OSError, ValueError, KeyError) as exc:
            raise PublicationRenderStorageFailedError(
                f"Unable to write Typst render workspace data: {exc}"
            ) from exc
