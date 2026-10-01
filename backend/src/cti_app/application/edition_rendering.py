"""Persistent Typst PDF rendering for immutable edition releases."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import ConfigDict

from cti_app.application.edition_typst_rendering import (
    EditionRenderPublicationSchemaUnsupportedError as RendererSchemaUnsupportedError,
)
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.jobs import JobParameters
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.application.typst_compilation import (
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    TYPST_MAX_PDF_BYTES,
    FontBundleInvalidError,
    TypstCompilationError,
    TypstCompiler,
    load_font_bundle_snapshot,
    materialize_font_bundle,
)
from cti_app.application.typst_render_execution import (
    TypstRenderExecutor,
    TypstRenderMediaIntegrityMismatchError,
    TypstRenderMediaKindMismatchError,
    TypstRenderMediaMissingError,
    TypstRenderStorageError,
    TypstRenderWorkspaceError,
    load_typst_render_bundle_snapshots,
)
from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    load_template_bundle,
)
from cti_app.domain.edition_publication import EditionDocumentV2, EditionRelease
from cti_app.domain.edition_render import (
    EDITION_RENDER_POLICY_VERSION,
    EDITION_RENDERER,
    EDITION_RENDERER_VERSION,
    EditionRender,
    EditionRenderAcquisition,
    EditionRenderAcquisitionOutcome,
    EditionRenderFormat,
    EditionRenderStatus,
    compute_edition_render_input_hash,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PublicationDocumentV4,
)

EDITION_RENDERER_MANIFEST = "edition-renderer-manifest.json"
EDITION_RENDER_JOB_KIND = "publication.edition.render"
EDITION_RENDER_SOURCE_BUCKET = "edition-renders-source"
EDITION_RENDER_DATA_BUCKET = "edition-renders-render-data"
EDITION_RENDER_OUTPUT_BUCKET = "edition-renders-output"


class EditionRenderParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    edition_release_id: UUID


class EditionRenderError(Exception):
    """Base for stable edition-render failures."""

    code = "edition_render_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class EditionReleaseNotFoundError(EditionRenderError):
    code = "edition_release_not_found"


class EditionRenderDocumentIntegrityError(EditionRenderError):
    code = "edition_document_integrity_mismatch"


class EditionRenderDocumentInvalidError(EditionRenderError):
    code = "edition_render_document_invalid"


class EditionRenderPublicationSchemaUnsupportedError(EditionRenderError):
    code = "edition_render_publication_schema_unsupported"


class EditionRenderTemplateInvalidError(EditionRenderError):
    code = "edition_render_template_invalid"


class EditionRenderMediaMissingError(EditionRenderError):
    code = "edition_render_media_missing"


class EditionRenderMediaKindMismatchError(EditionRenderError):
    code = "edition_render_media_kind_mismatch"


class EditionRenderMediaIntegrityMismatchError(EditionRenderError):
    code = "edition_render_media_integrity_mismatch"


class EditionRenderStorageFailedError(EditionRenderError):
    code = "edition_render_storage_failed"


class EditionRenderInProgressError(EditionRenderError):
    code = "edition_render_in_progress"


class EditionRenderService:
    """Render the exact EditionDocumentV2 frozen in an EditionRelease."""

    def __init__(
        self,
        *,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        media_asset_store: MediaAssetStore,
        renderer: EditionTypstRenderer,
        compiler: TypstCompiler,
        chp_typst_root: Path,
        font_bundle_root: Path,
        typst_fonts_lock_path: Path,
        wait_poll_interval_seconds: float = 0.1,
        wait_timeout_seconds: float = 60.0,
        running_lease_seconds: float = 300.0,
    ) -> None:
        if wait_poll_interval_seconds <= 0:
            raise ValueError("wait_poll_interval_seconds must be positive")
        if wait_timeout_seconds <= 0:
            raise ValueError("wait_timeout_seconds must be positive")
        if running_lease_seconds <= 0:
            raise ValueError("running_lease_seconds must be positive")
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._renderer = renderer
        self._compiler = compiler
        self._typst_executor = TypstRenderExecutor(
            media_asset_store=media_asset_store,
            compiler=compiler,
        )
        self._chp_typst_root = chp_typst_root
        self._font_bundle_root = font_bundle_root
        self._typst_fonts_lock_path = typst_fonts_lock_path
        self._wait_poll_interval_seconds = wait_poll_interval_seconds
        self._wait_timeout_seconds = wait_timeout_seconds
        self._running_lease_seconds = running_lease_seconds

    async def render_pdf(self, edition_release_id: UUID) -> EditionRender:
        async with self._uow_factory() as uow:
            release = await uow.edition_releases.get(edition_release_id)
            if release is None:
                raise EditionReleaseNotFoundError(
                    f"Edition release {edition_release_id} does not exist"
                )
            await uow.commit()

        document = await self._read_edition_document(release)
        for publication in document.publications:
            if not isinstance(publication.document, PublicationDocumentV4):
                raise EditionRenderPublicationSchemaUnsupportedError(
                    "Edition rendering requires every publication to be PublicationDocumentV4"
                )

        try:
            template_bundle, font_bundle = load_typst_render_bundle_snapshots(
                chp_typst_root=self._chp_typst_root,
                font_bundle_root=self._font_bundle_root,
                typst_fonts_lock_path=self._typst_fonts_lock_path,
                manifest_name=EDITION_RENDERER_MANIFEST,
                template_loader=load_template_bundle,
                font_loader=load_font_bundle_snapshot,
            )
        except (TemplateBundleInvalidError, FontBundleInvalidError) as exc:
            raise EditionRenderTemplateInvalidError(str(exc)) from exc
        except Exception as exc:
            raise EditionRenderTemplateInvalidError(
                "Unable to load edition render input snapshots"
            ) from exc

        input_hash = compute_edition_render_input_hash(
            edition_release_id=release.id,
            edition_document_sha256=release.edition_document_sha256,
            renderer=EDITION_RENDERER,
            renderer_version=EDITION_RENDERER_VERSION,
            template_version=template_bundle.template_version,
            template_sha256=template_bundle.sha256,
            compiler=TYPST_COMPILER,
            compiler_version=TYPST_COMPILER_VERSION,
            font_bundle_version=font_bundle.font_bundle_version,
            render_policy_version=EDITION_RENDER_POLICY_VERSION,
            format=EditionRenderFormat.PDF,
        )
        now = datetime.now(UTC)
        proposed = EditionRender(
            id=uuid4(),
            edition_release_id=release.id,
            renderer=EDITION_RENDERER,
            renderer_version=EDITION_RENDERER_VERSION,
            template_version=template_bundle.template_version,
            template_sha256=template_bundle.sha256,
            compiler=TYPST_COMPILER,
            compiler_version=TYPST_COMPILER_VERSION,
            font_bundle_version=font_bundle.font_bundle_version,
            render_policy_version=EDITION_RENDER_POLICY_VERSION,
            format=EditionRenderFormat.PDF,
            input_hash=input_hash,
            source_blob_id=None,
            render_data_blob_id=None,
            output_blob_id=None,
            output_sha256=None,
            output_byte_size=None,
            status=EditionRenderStatus.RUNNING,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )

        wait_deadline: float | None = None
        acquisition: EditionRenderAcquisition | None = None
        while True:
            if acquisition is None:
                async with self._uow_factory() as uow:
                    acquisition = await uow.edition_renders.acquire_for_render(
                        proposed,
                        stale_running_before=(
                            datetime.now(UTC) - timedelta(seconds=self._running_lease_seconds)
                        ),
                    )
                    await uow.commit()

            if acquisition.outcome is EditionRenderAcquisitionOutcome.ACQUIRED:
                render_id = acquisition.render.id
                break

            if acquisition.outcome is EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED:
                if await self._cached_output_is_valid(acquisition.render):
                    return acquisition.render
                succeeded = acquisition.render
                assert succeeded.output_blob_id is not None
                assert succeeded.output_sha256 is not None
                retrying = replace(
                    succeeded,
                    status=EditionRenderStatus.RUNNING,
                    error_code=None,
                    error_message=None,
                    updated_at=datetime.now(UTC),
                )
                async with self._uow_factory() as uow:
                    acquisition = await uow.edition_renders.reacquire_invalid_succeeded(
                        retrying,
                        observed_output_blob_id=succeeded.output_blob_id,
                        observed_output_sha256=succeeded.output_sha256,
                    )
                    await uow.commit()
                continue

            if wait_deadline is None:
                wait_deadline = monotonic() + self._wait_timeout_seconds
            completed = await self._wait_for_render(input_hash, wait_deadline=wait_deadline)
            if completed is None or completed.status is EditionRenderStatus.FAILED:
                acquisition = None
            else:
                acquisition = EditionRenderAcquisition(
                    EditionRenderAcquisitionOutcome.REUSABLE_SUCCEEDED,
                    completed,
                )

        source_blob_id: UUID | None = None
        render_data_blob_id: UUID | None = None
        try:
            try:
                render_source = self._renderer.render(document, template_bundle)
            except RendererSchemaUnsupportedError as exc:
                raise EditionRenderPublicationSchemaUnsupportedError(str(exc)) from exc
            except TemplateBundleInvalidError as exc:
                raise EditionRenderTemplateInvalidError(str(exc)) from exc
            except ValueError as exc:
                raise EditionRenderDocumentInvalidError(str(exc)) from exc

            try:
                resolved_media = await self._typst_executor.resolve_media(render_source)
            except TypstRenderMediaMissingError as exc:
                raise EditionRenderMediaMissingError(str(exc)) from exc
            except TypstRenderMediaKindMismatchError as exc:
                raise EditionRenderMediaKindMismatchError(str(exc)) from exc
            except TypstRenderMediaIntegrityMismatchError as exc:
                raise EditionRenderMediaIntegrityMismatchError(str(exc)) from exc
            except TypstRenderStorageError as exc:
                raise EditionRenderStorageFailedError(str(exc)) from exc

            async def persist_render_inputs() -> None:
                nonlocal source_blob_id, render_data_blob_id
                try:
                    source_blob_id = await self._artifact_store.put_bytes(
                        render_source.source_bytes,
                        bucket=EDITION_RENDER_SOURCE_BUCKET,
                        mime_type="text/plain; charset=utf-8",
                    )
                    render_data_blob_id = await self._artifact_store.put_bytes(
                        render_source.render_data_bytes,
                        bucket=EDITION_RENDER_DATA_BUCKET,
                        mime_type="application/json",
                    )
                except Exception as exc:
                    raise EditionRenderStorageFailedError(
                        "Unable to persist edition Typst source or render data"
                    ) from exc

            try:
                executed = await self._typst_executor.execute(
                    render_source=render_source,
                    template_bundle=template_bundle,
                    font_bundle=font_bundle,
                    resolved_media=resolved_media,
                    font_materializer=materialize_font_bundle,
                    on_workspace_ready=persist_render_inputs,
                )
            except TypstRenderWorkspaceError as exc:
                raise EditionRenderStorageFailedError(str(exc)) from exc
            compiled = executed.compiled_document
            try:
                output_blob_id = await self._artifact_store.put_bytes(
                    compiled.content,
                    bucket=EDITION_RENDER_OUTPUT_BUCKET,
                    mime_type="application/pdf",
                )
            except Exception as exc:
                raise EditionRenderStorageFailedError(
                    "Unable to persist the compiled edition PDF"
                ) from exc

            try:
                async with self._uow_factory() as uow:
                    succeeded = await uow.edition_renders.mark_succeeded(
                        render_id,
                        output_blob_id=output_blob_id,
                        output_sha256=compiled.sha256,
                        output_byte_size=compiled.byte_size,
                        source_blob_id=source_blob_id,
                        render_data_blob_id=render_data_blob_id,
                    )
                    await uow.commit()
            except Exception as exc:
                raise EditionRenderStorageFailedError(
                    "Unable to persist the completed edition render"
                ) from exc
            return succeeded
        except Exception as error:
            failure = self._stable_error(error)
            try:
                async with self._uow_factory() as uow:
                    await uow.edition_renders.mark_failed(
                        render_id,
                        error_code=failure.code,
                        error_message=str(failure)[:500],
                        source_blob_id=source_blob_id,
                        render_data_blob_id=render_data_blob_id,
                    )
                    await uow.commit()
            except Exception as mark_failed_error:
                failure.add_note(f"Could not persist EditionRender failure: {mark_failed_error}")
            if failure is error:
                raise
            raise failure from error

    async def _read_edition_document(self, release: EditionRelease) -> EditionDocumentV2:
        try:
            raw = await self._artifact_store.read_bytes(
                release.edition_document_blob_id,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        except Exception as exc:
            raise EditionRenderStorageFailedError(
                "Unable to read the canonical edition document blob"
            ) from exc
        if hashlib.sha256(raw).hexdigest() != release.edition_document_sha256:
            raise EditionRenderDocumentIntegrityError(
                "Edition document bytes do not match the release SHA-256"
            )
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Edition document must be a JSON object")
            _ensure_edition_publications_are_v4(payload)
            return EditionDocumentV2.from_json(payload)
        except EditionRenderPublicationSchemaUnsupportedError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise EditionRenderDocumentInvalidError(
                "Edition release does not contain a valid EditionDocumentV2"
            ) from exc

    async def _cached_output_is_valid(self, render: EditionRender) -> bool:
        if render.status is not EditionRenderStatus.SUCCEEDED or render.output_blob_id is None:
            return False
        try:
            output = await self._artifact_store.read_bytes(
                render.output_blob_id,
                max_bytes=TYPST_MAX_PDF_BYTES,
            )
        except Exception:
            return False
        return (
            len(output) == render.output_byte_size
            and hashlib.sha256(output).hexdigest() == render.output_sha256
        )

    async def _wait_for_render(
        self, input_hash: str, *, wait_deadline: float
    ) -> EditionRender | None:
        while True:
            remaining = wait_deadline - monotonic()
            if remaining <= 0:
                raise EditionRenderInProgressError(
                    f"Edition render {input_hash} is still in progress"
                )
            await asyncio.sleep(min(self._wait_poll_interval_seconds, remaining))
            async with self._uow_factory() as uow:
                render = await uow.edition_renders.get_by_input_hash(input_hash)
                await uow.commit()
            if render is None or render.status is EditionRenderStatus.FAILED:
                return None
            if render.status is EditionRenderStatus.SUCCEEDED:
                return render
            if render.updated_at < datetime.now(UTC) - timedelta(
                seconds=self._running_lease_seconds
            ):
                return None

    @staticmethod
    def _stable_error(error: Exception) -> EditionRenderError | TypstCompilationError:
        if isinstance(error, (EditionRenderError, TypstCompilationError)):
            return error
        return EditionRenderError(
            "Unable to render the edition release",
            code="edition_render_failed",
        )


def _ensure_edition_publications_are_v4(payload: dict[str, Any]) -> None:
    publications = payload.get("publications")
    if not isinstance(publications, list):
        return
    for item in publications:
        if not isinstance(item, dict):
            continue
        document = item.get("document")
        if (
            isinstance(document, dict)
            and document.get("schema_version") != PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION
        ):
            raise EditionRenderPublicationSchemaUnsupportedError(
                "Edition rendering requires every publication to use schema version 4"
            )


__all__ = [
    "EDITION_RENDERER_MANIFEST",
    "EDITION_RENDER_DATA_BUCKET",
    "EDITION_RENDER_JOB_KIND",
    "EDITION_RENDER_OUTPUT_BUCKET",
    "EDITION_RENDER_SOURCE_BUCKET",
    "EditionReleaseNotFoundError",
    "EditionRenderDocumentIntegrityError",
    "EditionRenderDocumentInvalidError",
    "EditionRenderError",
    "EditionRenderInProgressError",
    "EditionRenderParameters",
    "EditionRenderPublicationSchemaUnsupportedError",
    "EditionRenderService",
    "EditionRenderStorageFailedError",
    "EditionRenderTemplateInvalidError",
]
