"""Read-only bulletin preview built from current verified production artifacts."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.application.docx_postprocessing import edition_template_values
from cti_app.application.edition_document import (
    EditionDocumentArtifactRef,
    EditionDocumentBuildError,
    build_edition_document,
)
from cti_app.application.edition_review import EditionReviewService, ProductionRepairIssueReader
from cti_app.application.pandoc_export import export_markdown_docx
from cti_app.application.pandoc_rendering import render_edition_html, render_edition_pandoc
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.edition_publication import EditionDocumentV2


class EditionPreviewError(ValueError):
    pass


class EditionPreviewStaleError(EditionPreviewError):
    pass


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
    canonical_markdown: str
    sanitized_html: str
    stale: bool
    document: EditionDocumentV2


class EditionPreviewService:
    """Build a disposable preview without creating publication state."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        *,
        repair_issue_reader: ProductionRepairIssueReader | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._review_service = EditionReviewService(uow_factory, repair_issue_reader)
        self._last_preview_input_hash: dict[UUID, str] = {}

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

        markdown = render_edition_pandoc(document)
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
            canonical_markdown=markdown,
            sanitized_html=render_edition_html(document),
            stale=observed_hash is not None and observed_hash != fingerprint,
            document=document,
        )

    async def docx(
        self,
        edition_id: UUID,
        *,
        expected_preview_input_hash: str | None = None,
    ) -> bytes:
        preview = await self.preview(edition_id)
        if (
            expected_preview_input_hash is not None
            and expected_preview_input_hash != preview.preview_input_hash
        ):
            raise EditionPreviewStaleError("edition_preview_stale")
        with tempfile.TemporaryDirectory(prefix="autowork-edition-preview-") as directory:
            path = Path(directory) / "bulletin-preview.docx"
            export_markdown_docx(
                preview.canonical_markdown,
                path,
                template_values=edition_template_values(preview.document.edition),
            )
            return path.read_bytes()


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
    "EditionPreviewService",
    "EditionPreviewStaleError",
]
