"""Read-only bulletin preview built from current verified production artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.application.edition_document import (
    EditionDocumentArtifactRef,
    EditionDocumentBuildError,
    build_edition_document,
)
from cti_app.application.edition_review import EditionReviewService, ProductionRepairIssueReader
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.typst_compilation import (
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
from cti_app.application.typst_rendering import TemplateBundleInvalidError, load_template_bundle
from cti_app.domain.edition_publication import EditionDocumentV2

EDITION_RENDERER_MANIFEST = "edition-renderer-manifest.json"


class EditionPreviewError(ValueError):
    pass


class EditionPreviewStaleError(EditionPreviewError):
    pass


class EditionPreviewRenderError(Exception):
    """A stable, non-persistent preview PDF rendering failure."""

    code = "edition_preview_render_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class EditionPreviewTemplateInvalidError(EditionPreviewRenderError):
    code = "edition_preview_template_invalid"


class EditionPreviewDocumentInvalidError(EditionPreviewRenderError):
    code = "edition_preview_document_invalid"


class EditionPreviewMediaMissingError(EditionPreviewRenderError):
    code = "edition_preview_media_missing"


class EditionPreviewMediaKindMismatchError(EditionPreviewRenderError):
    code = "edition_preview_media_kind_mismatch"


class EditionPreviewMediaIntegrityMismatchError(EditionPreviewRenderError):
    code = "edition_preview_media_integrity_mismatch"


class EditionPreviewStorageFailedError(EditionPreviewRenderError):
    code = "edition_preview_storage_failed"


@dataclass(frozen=True, slots=True)
class EditionPreviewArtifact:
    position: int
    subject_id: UUID
    artifact_id: UUID
    artifact_version: int
    input_hash: str


@dataclass(frozen=True, slots=True)
class EditionPreview:
    edition_id: UUID
    edition_version: int
    preview_input_hash: str
    artifacts: tuple[EditionPreviewArtifact, ...]
    document: EditionDocumentV2
    stale: bool


@dataclass(frozen=True, slots=True)
class EditionPreviewPdf:
    content: bytes
    filename: str


class EditionPreviewPdfRenderer:
    """Compile an EditionDocumentV2 using the final edition Typst inputs."""

    def __init__(
        self,
        *,
        renderer: EditionTypstRenderer,
        media_asset_store: MediaAssetStore,
        compiler: TypstCompiler,
        chp_typst_root: Path,
        font_bundle_root: Path,
        typst_fonts_lock_path: Path,
    ) -> None:
        self._renderer = renderer
        self._executor = TypstRenderExecutor(
            media_asset_store=media_asset_store,
            compiler=compiler,
        )
        self._chp_typst_root = chp_typst_root
        self._font_bundle_root = font_bundle_root
        self._typst_fonts_lock_path = typst_fonts_lock_path

    async def render(self, document: EditionDocumentV2) -> bytes:
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
            raise EditionPreviewTemplateInvalidError(str(exc)) from exc
        except Exception as exc:
            raise EditionPreviewTemplateInvalidError(
                "Unable to load edition preview render inputs"
            ) from exc

        try:
            render_source = self._renderer.render(document, template_bundle)
        except TemplateBundleInvalidError as exc:
            raise EditionPreviewTemplateInvalidError(str(exc)) from exc
        except ValueError as exc:
            raise EditionPreviewDocumentInvalidError(str(exc)) from exc
        except Exception as exc:
            raise EditionPreviewDocumentInvalidError(
                "Unable to project the edition preview document"
            ) from exc

        try:
            resolved_media = await self._executor.resolve_media(render_source)
        except TypstRenderMediaMissingError as exc:
            raise EditionPreviewMediaMissingError(str(exc)) from exc
        except TypstRenderMediaKindMismatchError as exc:
            raise EditionPreviewMediaKindMismatchError(str(exc)) from exc
        except TypstRenderMediaIntegrityMismatchError as exc:
            raise EditionPreviewMediaIntegrityMismatchError(str(exc)) from exc
        except TypstRenderStorageError as exc:
            raise EditionPreviewStorageFailedError(str(exc)) from exc

        try:
            executed = await self._executor.execute(
                render_source=render_source,
                template_bundle=template_bundle,
                font_bundle=font_bundle,
                resolved_media=resolved_media,
                font_materializer=materialize_font_bundle,
            )
        except TypstRenderWorkspaceError as exc:
            raise EditionPreviewStorageFailedError(str(exc)) from exc
        except TypstCompilationError as exc:
            raise EditionPreviewRenderError(str(exc), code=exc.code) from exc
        except Exception as exc:
            raise EditionPreviewRenderError(
                "Unable to compile the edition preview PDF",
                code="edition_preview_render_failed",
            ) from exc
        return executed.compiled_document.content


class EditionPreviewService:
    """Build a disposable preview without creating publication state."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        *,
        repair_issue_reader: ProductionRepairIssueReader | None = None,
        pdf_renderer: EditionPreviewPdfRenderer | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._review_service = EditionReviewService(uow_factory, repair_issue_reader)
        self._last_preview_input_hash: dict[UUID, str] = {}
        self._pdf_renderer = pdf_renderer

    async def preview(
        self,
        edition_id: UUID,
        *,
        previous_preview_input_hash: str | None = None,
    ) -> EditionPreview:
        review = await self._review_service.get(edition_id)
        if not review.can_accept:
            raise EditionPreviewError("edition_preview_unavailable")

        refs = tuple(
            EditionDocumentArtifactRef(
                position=item.position,
                subject_id=item.subject_id,
                production_run_id=item.run_id,
                pipeline_generation=item.pipeline_generation,
                artifact_id=item.document_artifact_id,
                artifact_version=item.document_artifact_version,
                input_hash=item.document_input_hash,
            )
            for item in review.items
            if item.included
            and item.document_artifact_id is not None
            and item.document_artifact_version is not None
            and item.document_input_hash is not None
        )
        if not refs:
            raise EditionPreviewError("edition_preview_has_no_publications")

        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionPreviewError("edition_not_found")
            fingerprint = _preview_fingerprint(edition, review, refs)
            try:
                document = await build_edition_document(
                    uow,
                    self._artifact_store,
                    edition,
                    refs,
                    require_current=True,
                )
            except EditionDocumentBuildError as exc:
                raise EditionPreviewError(exc.code) from exc

        observed_hash = previous_preview_input_hash or self._last_preview_input_hash.get(edition_id)
        self._last_preview_input_hash[edition_id] = fingerprint
        return EditionPreview(
            edition_id=edition_id,
            edition_version=edition.version,
            preview_input_hash=fingerprint,
            artifacts=tuple(
                EditionPreviewArtifact(
                    position=ref.position,
                    subject_id=ref.subject_id,
                    artifact_id=ref.artifact_id,
                    artifact_version=ref.artifact_version,
                    input_hash=ref.input_hash,
                )
                for ref in refs
            ),
            document=document,
            stale=observed_hash is not None and observed_hash != fingerprint,
        )

    async def preview_pdf(
        self,
        edition_id: UUID,
        *,
        expected_preview_input_hash: str,
    ) -> EditionPreviewPdf:
        preview = await self.preview(edition_id)
        if expected_preview_input_hash != preview.preview_input_hash:
            raise EditionPreviewStaleError("edition_preview_stale")
        if self._pdf_renderer is None:
            raise EditionPreviewRenderError(
                "Typst PDF preview is not configured",
                code="edition_preview_pdf_unavailable",
            )
        content = await self._pdf_renderer.render(preview.document)
        return EditionPreviewPdf(
            content=content,
            filename=_preview_pdf_filename(preview.document),
        )


def _preview_pdf_filename(document: EditionDocumentV2) -> str:
    """Build a download name from whitelisted edition metadata only."""
    period_start = document.edition.get("period_start")
    month = period_start[:7] if isinstance(period_start, str) else "edition"
    if len(month) != 7 or month[4] != "-" or not (month[:4] + month[5:]).isdigit():
        month = "edition"
    raw_country_code = document.edition.get("country_code")
    country_code = (
        raw_country_code
        if isinstance(raw_country_code, str)
        and raw_country_code.isascii()
        and raw_country_code.isalnum()
        and 2 <= len(raw_country_code) <= 8
        else "edition"
    )
    return f"bulletin-preview-{month}-{country_code.upper()}.pdf"


def _preview_fingerprint(
    edition: Any, review: Any, refs: tuple[EditionDocumentArtifactRef, ...]
) -> str:
    payload = {
        "edition_id": str(edition.id),
        "edition_version": edition.version,
        "scope": [
            {
                "subject_id": str(item.subject_id),
                "position": item.position,
                "included": item.included,
                "decision": item.effective_decision.value if item.effective_decision else None,
                "decision_id": str(item.effective_decision_id)
                if item.effective_decision_id
                else None,
            }
            for item in review.items
        ],
        "artifacts": [
            {
                "position": ref.position,
                "subject_id": str(ref.subject_id),
                "artifact_id": str(ref.artifact_id),
                "artifact_version": ref.artifact_version,
                "input_hash": ref.input_hash,
            }
            for ref in refs
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "EditionPreview",
    "EditionPreviewArtifact",
    "EditionPreviewError",
    "EditionPreviewPdf",
    "EditionPreviewPdfRenderer",
    "EditionPreviewRenderError",
    "EditionPreviewService",
    "EditionPreviewStaleError",
]
