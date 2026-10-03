"""Application orchestration for versioned publication PDF renders."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import PublicationRenderUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
)
from cti_app.application.typst_compilation import (
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    FontBundleInvalidError,
    FontBundleSnapshot,
    TypstCompilationError,
    TypstCompiler,
    load_font_bundle_snapshot,
)
from cti_app.application.typst_render_execution import TypstRenderExecutionErrorTypes
from cti_app.application.typst_render_lifecycle import (
    TypstRenderBuckets,
    TypstRenderError,
    TypstRenderErrors,
    TypstRenderRunner,
)
from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    TypstRenderer,
    TypstRenderSource,
    TypstTemplateBundle,
    load_template_bundle,
)
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_document import (
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
PUBLICATION_RENDERER_VERSION = "publication-v4-typst-v1"

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
        wait_poll_interval_seconds: float = 0.1,
        wait_timeout_seconds: float = 60.0,
        running_lease_seconds: float = 300.0,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._renderer = renderer
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
            wait_poll_interval_seconds=wait_poll_interval_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
            running_lease_seconds=running_lease_seconds,
        )

    async def render_pdf(self, publication_artifact_id: UUID) -> PublicationRender:
        prepared = await self._prepare(publication_artifact_id)
        return await self._runner.run(
            prepared.proposed,
            template_bundle=prepared.template_bundle,
            font_bundle=prepared.font_bundle,
            build_source=prepared.build_source,
        )

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

        publication_content_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(document))
        ).hexdigest()
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

        def build_source() -> TypstRenderSource:
            try:
                return self._renderer.render(document, template_bundle)
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
