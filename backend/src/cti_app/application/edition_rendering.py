"""Persistent Typst PDF rendering for immutable edition releases."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
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
from cti_app.application.render_target_identities import (
    publication_target_artifact_identities,
)
from cti_app.application.typst_compilation import (
    TYPST_COMPILER,
    TYPST_COMPILER_VERSION,
    FontBundleInvalidError,
    TypstCompiler,
)
from cti_app.application.typst_render_execution import (
    TypstRenderExecutionErrorTypes,
    load_typst_render_bundle_snapshots,
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
from cti_app.application.typst_rendering import TemplateBundleInvalidError, TypstRenderSource
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionRelease,
    PublicationManifestV1,
)
from cti_app.domain.edition_render import (
    EDITION_RENDER_POLICY_VERSION,
    EDITION_RENDERER,
    EDITION_RENDERER_VERSION,
    EditionRender,
    compute_edition_render_input_hash,
)
from cti_app.domain.publication_document import (
    PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
    PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
    PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION,
)
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus

EDITION_RENDERER_MANIFEST = "edition-renderer-manifest.json"
EDITION_RENDER_JOB_KIND = "publication.edition.render"
EDITION_RENDER_BUCKETS = TypstRenderBuckets(
    source="edition-renders-source",
    render_data="edition-renders-render-data",
    output="edition-renders-output",
)


class EditionRenderParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    edition_release_id: UUID


class EditionRenderError(TypstRenderError):
    """Base for stable edition-render failures."""

    code = "edition_render_error"


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


class EditionRenderFailedError(EditionRenderError):
    code = "edition_render_failed"


_EDITION_RENDER_ERRORS = TypstRenderErrors(
    execution=TypstRenderExecutionErrorTypes(
        media_missing=EditionRenderMediaMissingError,
        media_kind_mismatch=EditionRenderMediaKindMismatchError,
        media_integrity_mismatch=EditionRenderMediaIntegrityMismatchError,
        storage_failed=EditionRenderStorageFailedError,
    ),
    in_progress=EditionRenderInProgressError,
    unexpected=EditionRenderFailedError,
    label="Edition",
)


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
        mapping_cache: TypstMappingSidecarCache | None = None,
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
        self._runner = TypstRenderRunner[EditionRender](
            uow_factory=uow_factory,
            repository=lambda uow: uow.edition_renders,
            artifact_store=artifact_store,
            media_asset_store=media_asset_store,
            compiler=compiler,
            buckets=EDITION_RENDER_BUCKETS,
            errors=_EDITION_RENDER_ERRORS,
            mapping_cache=mapping_cache,
            wait_poll_interval_seconds=wait_poll_interval_seconds,
            wait_timeout_seconds=wait_timeout_seconds,
            running_lease_seconds=running_lease_seconds,
        )

    async def render_pdf(self, edition_release_id: UUID) -> EditionRender:
        async with self._uow_factory() as uow:
            release = await uow.edition_releases.get(edition_release_id)
            if release is None:
                raise EditionReleaseNotFoundError(
                    f"Edition release {edition_release_id} does not exist"
                )
            pinned_render = await uow.edition_renders.get_pinned_succeeded_for_release(release.id)
            await uow.commit()

        pinned_output_error: (
            TypstRenderOutputStorageError | TypstRenderOutputIntegrityError | None
        ) = None
        if pinned_render is not None:
            try:
                await read_verified_render_pdf(self._artifact_store, pinned_render)
            except (TypstRenderOutputStorageError, TypstRenderOutputIntegrityError) as exc:
                pinned_output_error = exc
            else:
                # A release's first successful PDF is its immutable render pin.
                # Replayed jobs must not silently adopt the current Typst bundle.
                return pinned_render

        document = await self._read_edition_document(release)
        artifact_identities = await self._read_manifest_artifact_identities(release)
        try:
            template_bundle, font_bundle = load_typst_render_bundle_snapshots(
                chp_typst_root=self._chp_typst_root,
                font_bundle_root=self._font_bundle_root,
                typst_fonts_lock_path=self._typst_fonts_lock_path,
                manifest_name=EDITION_RENDERER_MANIFEST,
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
            format=TypstRenderFormat.PDF,
        )
        if pinned_output_error is not None and pinned_render is not None:
            if pinned_render.input_hash != input_hash:
                raise pinned_output_error
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
                return self._renderer.render(
                    document,
                    template_bundle,
                    artifact_identities=artifact_identities,
                )
            except RendererSchemaUnsupportedError as exc:
                raise EditionRenderPublicationSchemaUnsupportedError(str(exc)) from exc
            except TemplateBundleInvalidError as exc:
                raise EditionRenderTemplateInvalidError(str(exc)) from exc
            except ValueError as exc:
                raise EditionRenderDocumentInvalidError(str(exc)) from exc

        return await self._runner.run(
            proposed,
            template_bundle=template_bundle,
            font_bundle=font_bundle,
            build_source=build_source,
        )

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
            _ensure_edition_publications_are_supported(payload)
            return EditionDocumentV2.from_json(payload)
        except EditionRenderPublicationSchemaUnsupportedError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise EditionRenderDocumentInvalidError(
                "Edition release does not contain a valid EditionDocumentV2"
            ) from exc

    async def _read_manifest_artifact_identities(
        self, release: EditionRelease
    ) -> dict[str, dict[str, Any]]:
        """Resolve canonical block owners from each publication in the frozen manifest."""
        try:
            async with self._uow_factory() as uow:
                manifest_blob_id = await uow.publication_manifests.get_blob_id(release.manifest_id)
                await uow.commit()
            if manifest_blob_id is None:
                return {}
            payload = await self._artifact_store.read_json(manifest_blob_id)
            if not isinstance(payload, dict):
                return {}
            manifest = PublicationManifestV1.from_json(payload)
            if manifest.id != release.manifest_id or manifest.edition_id != release.edition_id:
                return {}
            identities: dict[str, dict[str, Any]] = {}
            async with self._uow_factory() as uow:
                for entry in manifest.entries:
                    publication_artifact = await uow.production_artifacts.get(
                        entry.document_artifact_id
                    )
                    identities[str(entry.subject_id)] = (
                        await publication_target_artifact_identities(
                            uow.production_artifacts,
                            publication_artifact,
                            expected_publication_version=entry.document_artifact_version,
                            expected_publication_input_hash=entry.document_input_hash,
                        )
                        if publication_artifact is not None
                        else {}
                    )
            return identities
        except Exception:
            # Missing optional location provenance cannot block an immutable PDF render.
            return {}


def _ensure_edition_publications_are_supported(payload: dict[str, Any]) -> None:
    publications = payload.get("publications")
    if not isinstance(publications, list):
        return
    for item in publications:
        if not isinstance(item, dict):
            continue
        document = item.get("document")
        if isinstance(document, dict) and document.get("schema_version") not in {
            PUBLICATION_DOCUMENT_V4_SCHEMA_VERSION,
            PUBLICATION_DOCUMENT_V5_SCHEMA_VERSION,
            PUBLICATION_DOCUMENT_V6_SCHEMA_VERSION,
        }:
            raise EditionRenderPublicationSchemaUnsupportedError(
                "Edition rendering requires every publication to use schema version 4, 5 or 6"
            )


__all__ = [
    "EDITION_RENDERER_MANIFEST",
    "EDITION_RENDER_BUCKETS",
    "EDITION_RENDER_JOB_KIND",
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
