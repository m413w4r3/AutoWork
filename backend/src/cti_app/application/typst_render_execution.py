"""Shared, database-free execution of prepared Typst render inputs."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.typst_compilation import (
    TYPST_MAX_PDF_BYTES,
    CompiledTypstDocument,
    FontBundleSnapshot,
    TypstCompiler,
    TypstCompileRequest,
    TypstOutputInvalidError,
    TypstOutputTooLargeError,
    materialize_font_bundle,
)
from cti_app.application.typst_rendering import (
    TypstRenderSource,
    TypstTemplateBundle,
)
from cti_app.domain.errors import BlobIntegrityError, EntityNotFoundError
from cti_app.domain.media_assets import MediaAssetKind


class TypstRenderExecutionError(Exception):
    """Base class for media and workspace failures during Typst execution."""


class TypstRenderMediaMissingError(TypstRenderExecutionError):
    pass


class TypstRenderMediaKindMismatchError(TypstRenderExecutionError):
    pass


class TypstRenderMediaIntegrityMismatchError(TypstRenderExecutionError):
    pass


class TypstRenderStorageError(TypstRenderExecutionError):
    pass


class TypstRenderWorkspaceError(TypstRenderExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutedTypstRender:
    compiled_document: CompiledTypstDocument
    resolved_media: Mapping[UUID, bytes]


def load_typst_render_bundle_snapshots(
    *,
    chp_typst_root: Path,
    font_bundle_root: Path,
    typst_fonts_lock_path: Path,
    manifest_name: str,
    template_loader: Callable[..., TypstTemplateBundle],
    font_loader: Callable[[Path, Path], FontBundleSnapshot],
) -> tuple[TypstTemplateBundle, FontBundleSnapshot]:
    """Load the immutable template and font bytes shared by preview and final renders."""
    template_bundle = template_loader(chp_typst_root, manifest_name=manifest_name)
    font_bundle = font_loader(font_bundle_root, typst_fonts_lock_path)
    return template_bundle, font_bundle


class TypstRenderExecutor:
    """Resolve canonical media and compile a snapshotted Typst render source."""

    def __init__(self, *, media_asset_store: MediaAssetStore, compiler: TypstCompiler) -> None:
        self._media_asset_store = media_asset_store
        self._compiler = compiler

    async def resolve_media(self, render_source: TypstRenderSource) -> dict[UUID, bytes]:
        """Resolve and validate every distinct media asset referenced by a render."""
        resolved_media: dict[UUID, bytes] = {}
        for media_ref in render_source.media_refs:
            try:
                manifest = await self._media_asset_store.get(media_ref.asset_id)
            except Exception as exc:
                raise TypstRenderStorageError(
                    f"Unable to load media manifest {media_ref.asset_id}"
                ) from exc
            if manifest is None:
                raise TypstRenderMediaMissingError(
                    f"Media asset {media_ref.asset_id} does not exist"
                )
            if media_ref.expected_kind is MediaAssetKind.DIAGRAM_SVG:
                if (
                    manifest.kind is not MediaAssetKind.DIAGRAM_SVG
                    or manifest.mime_type != "image/svg+xml"
                ):
                    raise TypstRenderMediaKindMismatchError(
                        f"Diagram media asset {media_ref.asset_id} has an invalid kind or MIME type"
                    )
            elif media_ref.expected_kind is MediaAssetKind.SOURCE_FIGURE:
                if (
                    manifest.asset_id != media_ref.asset_id
                    or manifest.sha256 != media_ref.expected_sha256
                    or manifest.mime_type != media_ref.expected_mime_type
                    or manifest.byte_size != media_ref.expected_byte_size
                    or manifest.kind is not MediaAssetKind.SOURCE_FIGURE
                ):
                    raise TypstRenderMediaIntegrityMismatchError(
                        "Figure media asset "
                        f"{media_ref.asset_id} does not match its canonical reference"
                    )
            try:
                resolved_media[media_ref.asset_id] = await self._media_asset_store.read(
                    media_ref.asset_id
                )
            except BlobIntegrityError as exc:
                raise TypstRenderMediaIntegrityMismatchError(
                    f"Media asset {media_ref.asset_id} failed its integrity check"
                ) from exc
            except EntityNotFoundError as exc:
                raise TypstRenderMediaMissingError(
                    f"Media asset {media_ref.asset_id} does not exist"
                ) from exc
            except Exception as exc:
                raise TypstRenderStorageError(
                    f"Unable to read media asset {media_ref.asset_id}"
                ) from exc
        return resolved_media

    async def execute(
        self,
        *,
        render_source: TypstRenderSource,
        template_bundle: TypstTemplateBundle,
        font_bundle: FontBundleSnapshot,
        resolved_media: Mapping[UUID, bytes] | None = None,
        font_materializer: Callable[[FontBundleSnapshot, Path], tuple[Path, ...]] | None = None,
        on_workspace_ready: Callable[[], Awaitable[None]] | None = None,
    ) -> ExecutedTypstRender:
        """Build a private workspace, materialize fonts, compile, and validate PDF output."""
        media = dict(
            resolved_media
            if resolved_media is not None
            else await self.resolve_media(render_source)
        )
        try:
            with tempfile.TemporaryDirectory(prefix="autowork-typst-fonts-") as font_path:
                materialize = font_materializer or materialize_font_bundle
                font_paths = materialize(font_bundle, Path(font_path))
                with tempfile.TemporaryDirectory(
                    prefix="autowork-publication-render-"
                ) as workspace_path:
                    workspace_root = Path(workspace_path)
                    self._populate_workspace(workspace_root, template_bundle, render_source, media)
                    if on_workspace_ready is not None:
                        await on_workspace_ready()
                    compiled = await self._compiler.compile(
                        TypstCompileRequest(
                            workspace_root=workspace_root,
                            entrypoint_relative_path=render_source.entrypoint_relative_path,
                            font_paths=font_paths,
                        )
                    )
                    self._validate_pdf(compiled)
        except OSError as exc:
            raise TypstRenderWorkspaceError(
                f"Unable to materialize Typst render inputs: {exc}"
            ) from exc
        return ExecutedTypstRender(compiled_document=compiled, resolved_media=media)

    @staticmethod
    def _validate_pdf(compiled: CompiledTypstDocument) -> None:
        if compiled.byte_size > TYPST_MAX_PDF_BYTES:
            raise TypstOutputTooLargeError(f"Typst PDF output exceeded {TYPST_MAX_PDF_BYTES} bytes")
        if (
            compiled.media_type != "application/pdf"
            or not compiled.content.startswith(b"%PDF-")
            or compiled.byte_size != len(compiled.content)
            or compiled.sha256 != hashlib.sha256(compiled.content).hexdigest()
        ):
            raise TypstOutputInvalidError("Typst compiler returned an invalid PDF document")

    @staticmethod
    def _populate_workspace(
        workspace_root: Path,
        template_bundle: TypstTemplateBundle,
        render_source: TypstRenderSource,
        resolved_media: Mapping[UUID, bytes],
    ) -> None:
        try:
            for template_file in template_bundle.files:
                destination = workspace_root.joinpath(
                    *PurePosixPath(template_file.relative_path).parts
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(template_file.content)
        except OSError as exc:
            raise TypstRenderWorkspaceError(
                f"Unable to materialize Typst template bundle: {exc}"
            ) from exc

        try:
            renderer_directory = (
                workspace_root / PurePosixPath(render_source.entrypoint_relative_path).parent
            )
            renderer_directory.mkdir(parents=True, exist_ok=True)
            render_data_path = workspace_root.joinpath(
                *PurePosixPath(render_source.render_data_relative_path).parts
            )
            render_data_path.resolve().relative_to(workspace_root.resolve())
            render_data_path.parent.mkdir(parents=True, exist_ok=True)
            render_data_path.write_bytes(render_source.render_data_bytes)

            media_directory = renderer_directory / "media"
            media_directory.mkdir(parents=True, exist_ok=True)
            for media_ref in render_source.media_refs:
                media_relative_path = PurePosixPath(media_ref.media_path)
                if (
                    media_relative_path.is_absolute()
                    or "\\" in media_ref.media_path
                    or ".." in media_relative_path.parts
                ):
                    raise ValueError(f"Invalid workspace media path {media_ref.media_path!r}")
                media_path = renderer_directory.joinpath(*media_relative_path.parts)
                media_path.resolve().relative_to(renderer_directory.resolve())
                media_path.parent.mkdir(parents=True, exist_ok=True)
                media_path.write_bytes(resolved_media[media_ref.asset_id])
        except (OSError, ValueError, KeyError) as exc:
            raise TypstRenderWorkspaceError(
                f"Unable to write Typst render workspace data: {exc}"
            ) from exc
