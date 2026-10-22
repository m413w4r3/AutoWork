"""Pure projection of EditionDocumentV2 into one deterministic Typst document."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from uuid import UUID

from cti_app.application.edition_classification import classify_edition_publications
from cti_app.application.edition_ioc_export import (
    edition_identifier,
    project_publication_iocs,
)
from cti_app.application.pdf_mapping import MAPPING_RENDERER_VERSION
from cti_app.application.publication_secret_guard import guard_publication_render_data
from cti_app.application.typst_rendering import (
    TemplateBundleInvalidError,
    TypstMediaRef,
    TypstRenderSource,
    TypstTemplateBundle,
    instrument_publication_targets,
    project_publication_to_typst_model,
)
from cti_app.domain.edition_publication import EditionDocumentV2
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
)

_EDITION_RENDER_DATA_SCHEMA_VERSION = (
    "typst-edition-model-v24-secret-guard-short-continuation-header"
)
_FRENCH_MONTH_NAMES = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def _period_label(edition: dict[str, Any]) -> str | None:
    try:
        start = date.fromisoformat(edition["period_start"])
        end = date.fromisoformat(edition["period_end"])
    except (KeyError, TypeError, ValueError):
        return None
    if start.year == end.year and start.month == end.month:
        return f"{_FRENCH_MONTH_NAMES[start.month - 1]} {start.year}"
    return (
        f"{_FRENCH_MONTH_NAMES[start.month - 1]} {start.year} — "
        f"{_FRENCH_MONTH_NAMES[end.month - 1]} {end.year}"
    )


class EditionTypstRendererError(ValueError):
    """Base class for stable edition projection errors."""

    code = "edition_typst_renderer_error"


class EditionRenderPublicationSchemaUnsupportedError(EditionTypstRendererError):
    code = "edition_render_publication_schema_unsupported"


class EditionTypstRenderer:
    """Render an ordered edition using the shared V4/V5/V6 publication projection."""

    def render(
        self,
        document: EditionDocumentV2,
        template_bundle: TypstTemplateBundle,
        *,
        document_label: str,
        artifact_identities: dict[str, dict[str, Any]] | None = None,
    ) -> TypstRenderSource:
        if not isinstance(document_label, str) or not document_label.strip():
            raise TemplateBundleInvalidError("Edition render requires document_label")
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
        country = edition.get("country")
        edition["bulletin_country"] = (
            country.strip() if isinstance(country, str) and country.strip() else None
        )
        edition["bulletin_period"] = _period_label(edition)
        edition["bulletin_title"] = "Bulletin de veille CTI"
        edition["bulletin_number"] = str(edition.get("version") or 1)
        edition["bulletin_identifier"] = edition_identifier(edition)
        if any(
            not isinstance(
                publication.document,
                (PublicationDocumentV4, PublicationDocumentV5, PublicationDocumentV6),
            )
            for publication in document.publications
        ):
            raise EditionRenderPublicationSchemaUnsupportedError(
                "Edition Typst rendering requires every publication to use a supported schema"
            )
        publications: list[dict[str, Any]] = []
        mapping_targets: list[dict[str, Any]] = []
        mapping_unmapped: list[dict[str, Any]] = []
        media_refs_by_id: dict[UUID, TypstMediaRef] = {}
        for classified in classify_edition_publications(document.publications):
            publication = classified.publication
            model = project_publication_to_typst_model(publication.document)
            article_title_target_id = None
            target_identities = (artifact_identities or {}).get(str(publication.subject_id))
            if target_identities is not None:
                targets, unmapped, article_title_target_id = instrument_publication_targets(
                    model,
                    article_title_identity=target_identities.get("article_title"),
                    enrichment_identity=target_identities.get("enrichment"),
                )
                mapping_targets.extend(targets)
                mapping_unmapped.extend(unmapped)
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
                "kind": classified.kind,
                "number": classified.number,
                "anchor": classified.anchor,
                "file_ioc_count": project_publication_iocs(publication.document).counts["file"],
                "content_sections": model.content_sections,
            }
            if article_title_target_id is not None:
                item["article_title_target_id"] = article_title_target_id
            publications.append(item)

        render_data = {
            "schema_version": _EDITION_RENDER_DATA_SCHEMA_VERSION,
            "document_label": document_label.strip(),
            "document_header": f"Bulletin de veille CTI — édition v{edition['bulletin_number']}",
            "edition": edition,
            "publications": publications,
            "file_ioc_count": sum(item["file_ioc_count"] for item in publications),
        }
        if mapping_targets or mapping_unmapped:
            render_data["mapping_instrumentation_version"] = MAPPING_RENDERER_VERSION
        guard_publication_render_data(render_data)
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
            mapping_targets=tuple(mapping_targets),
            mapping_unmapped=tuple(mapping_unmapped),
        )
