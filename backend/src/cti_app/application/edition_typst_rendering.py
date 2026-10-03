"""Pure projection of EditionDocumentV2 into one deterministic Typst document."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    TypstMediaRef,
    TypstRenderSource,
    TypstTemplateBundle,
    project_publication_to_typst_model,
)
from cti_app.domain.edition_publication import EditionDocumentV2
from cti_app.domain.publication_document import PublicationDocumentV4, PublicationDocumentV5

_EDITION_RENDER_DATA_SCHEMA_VERSION = "typst-edition-model-v3-semantic-text"


class EditionTypstRendererError(ValueError):
    """Base class for stable edition projection errors."""

    code = "edition_typst_renderer_error"


class EditionRenderPublicationSchemaUnsupportedError(EditionTypstRendererError):
    code = "edition_render_publication_schema_unsupported"


class EditionTypstRenderer:
    """Render an ordered edition using the shared V4 publication projection."""

    def render(
        self,
        document: EditionDocumentV2,
        template_bundle: TypstTemplateBundle,
    ) -> TypstRenderSource:
        entrypoint = next(
            (
                file
                for file in template_bundle.files
                if file.relative_path == "RENDERER/edition.typ"
            ),
            None,
        )
        if entrypoint is None:
            raise TemplateBundleInvalidError(
                "Typst renderer manifest does not include RENDERER/edition.typ"
            )

        edition_fields = (
            "id",
            "country",
            "country_code",
            "period_start",
            "period_end",
            "tlp",
            "languages",
            "version",
        )
        edition = {field: document.edition[field] for field in edition_fields}
        publications: list[dict[str, Any]] = []
        media_refs_by_id: dict[UUID, TypstMediaRef] = {}
        for publication in sorted(document.publications, key=lambda item: item.position):
            if not isinstance(publication.document, (PublicationDocumentV4, PublicationDocumentV5)):
                raise EditionRenderPublicationSchemaUnsupportedError(
                    "Edition Typst rendering requires every publication to use a supported schema"
                )
            model = project_publication_to_typst_model(publication.document)
            for media_ref in model.media_refs:
                existing = media_refs_by_id.get(media_ref.asset_id)
                if existing is not None and existing != media_ref:
                    raise ValueError(
                        f"Edition asset {media_ref.asset_id} has conflicting media metadata"
                    )
                media_refs_by_id.setdefault(media_ref.asset_id, media_ref)
            item: dict[str, Any] = {
                "position": publication.position,
                "subject_id": str(publication.subject_id),
                "title": model.title,
                "content_sections": model.content_sections,
            }
            if model.semantic_title is not None:
                item["title_spans"] = model.semantic_title
            publications.append(item)

        render_data = {
            "schema_version": _EDITION_RENDER_DATA_SCHEMA_VERSION,
            "edition": edition,
            "publications": publications,
        }
        render_data_bytes = json.dumps(
            render_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        source_bytes = entrypoint.content
        return TypstRenderSource(
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            render_data_bytes=render_data_bytes,
            render_data_sha256=hashlib.sha256(render_data_bytes).hexdigest(),
            media_refs=tuple(media_refs_by_id.values()),
            entrypoint_relative_path="RENDERER/edition.typ",
            render_data_relative_path="RENDERER/edition-render-data.json",
        )
