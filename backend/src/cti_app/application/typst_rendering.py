"""Pure projection of PublicationDocumentV4 into the fixed Typst input format."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from cti_app.application.typst_bundle import hash_bundle_contents, resolve_bundle_files
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceRefV1,
    PublicationSourceV1,
)
from cti_app.domain.publication_document import (
    PublicationDiagramV1,
    PublicationDocumentV4,
    PublicationSourceFigureV1,
    PublicationTableV1,
)

_RENDER_DATA_SCHEMA_VERSION = "typst-publication-model-v1"
_MEDIA_EXTENSIONS = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_INDICATOR_KEYS = {
    ArtifactType.IP: "ips",
    ArtifactType.DOMAIN: "domains",
    ArtifactType.URL: "urls",
    ArtifactType.EMAIL: "emails",
    ArtifactType.HASH: "hashes",
}


@dataclass(frozen=True, slots=True)
class TypstMediaRef:
    asset_id: UUID
    expected_kind: MediaAssetKind
    expected_mime_type: str
    expected_sha256: str | None
    expected_byte_size: int | None
    media_path: str


@dataclass(frozen=True, slots=True)
class TypstRenderSource:
    source_bytes: bytes
    source_sha256: str
    render_data_bytes: bytes
    render_data_sha256: str
    media_refs: tuple[TypstMediaRef, ...]


@dataclass(frozen=True, slots=True)
class TemplateFile:
    relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class TypstTemplateBundle:
    template_version: str
    sha256: str
    files: tuple[TemplateFile, ...]


class TypstRenderer:
    """Build deterministic data and return the fixed production entrypoint."""

    def render(
        self, document: PublicationDocumentV4, template_bundle: TypstTemplateBundle
    ) -> TypstRenderSource:
        entrypoint = next(
            (
                file
                for file in template_bundle.files
                if file.relative_path == "RENDERER/publication.typ"
            ),
            None,
        )
        if entrypoint is None:
            raise TemplateBundleInvalidError(
                "Typst renderer manifest does not include RENDERER/publication.typ"
            )
        source_bytes = entrypoint.content
        media_refs_by_id: dict[UUID, TypstMediaRef] = {}
        rich_by_placement: dict[
            tuple[EnrichmentPlacementKind, int | None], list[dict[str, Any]]
        ] = {}

        def media_ref(
            *,
            asset_id: UUID,
            expected_kind: MediaAssetKind,
            expected_mime_type: str,
            expected_sha256: str | None,
            expected_byte_size: int | None,
        ) -> TypstMediaRef:
            extension = _MEDIA_EXTENSIONS.get(expected_mime_type)
            if extension is None:
                raise ValueError(f"Unsupported Typst media MIME type: {expected_mime_type}")
            candidate = TypstMediaRef(
                asset_id=asset_id,
                expected_kind=expected_kind,
                expected_mime_type=expected_mime_type,
                expected_sha256=expected_sha256,
                expected_byte_size=expected_byte_size,
                media_path=f"media/{asset_id}{extension}",
            )
            existing = media_refs_by_id.get(asset_id)
            if existing is not None:
                if existing != candidate:
                    raise ValueError(f"Publication asset {asset_id} has conflicting media metadata")
                return existing
            media_refs_by_id[asset_id] = candidate
            return candidate

        def add_rich(
            kind: EnrichmentPlacementKind,
            section_index: int | None,
            block: dict[str, Any],
        ) -> None:
            if kind is EnrichmentPlacementKind.AFTER_SECTION and (
                section_index is None or section_index >= len(document.sections)
            ):
                raise ValueError(f"Enrichment placement refers to missing section {section_index}")
            rich_by_placement.setdefault((kind, section_index), []).append(block)

        for table in document.tables:
            add_rich(
                table.placement.kind,
                table.placement.section_index,
                _table_block(table),
            )
        for diagram in document.diagrams:
            add_rich(
                diagram.placement.kind,
                diagram.placement.section_index,
                _diagram_block(diagram, media_ref),
            )
        for figure in document.figures:
            add_rich(
                figure.placement.kind,
                figure.placement.section_index,
                _figure_block(figure, media_ref),
            )

        body_blocks: list[dict[str, Any]] = []
        body_blocks.extend(
            rich_by_placement.get((EnrichmentPlacementKind.AFTER_TIMELINE, None), ())
        )
        body_blocks.extend({"type": "paragraph", "text": item.text} for item in document.lead)
        body_blocks.extend(rich_by_placement.get((EnrichmentPlacementKind.AFTER_LEAD, None), ()))
        for section_index, section in enumerate(document.sections):
            body_blocks.append({"type": "section_heading", "text": section.heading})
            body_blocks.extend(
                {"type": "paragraph", "text": paragraph.text} for paragraph in section.paragraphs
            )
            body_blocks.extend(
                rich_by_placement.get((EnrichmentPlacementKind.AFTER_SECTION, section_index), ())
            )
        body_blocks.extend(rich_by_placement.get((EnrichmentPlacementKind.END, None), ()))

        sources_by_id = {source.source_document_id: source for source in document.sources}
        timeline = [
            {
                "display_date": _display_date(entry.date_text, entry.event_date),
                "text": entry.text,
                "source_urls": _timeline_source_urls(entry.evidence_refs, sources_by_id),
            }
            for entry in document.timeline
        ]

        indicators: dict[str, list[str]] = {
            key: [] for key in ("ips", "domains", "urls", "emails", "hashes")
        }
        for group in document.indicators:
            indicators[_INDICATOR_KEYS[group.artifact_type]] = [
                item.value for item in group.indicators
            ]

        render_data: dict[str, Any] = {
            "schema_version": _RENDER_DATA_SCHEMA_VERSION,
            "language": document.publication_language,
            "title": document.title,
            "timeline": timeline,
            "body_blocks": body_blocks,
            "indicators": indicators,
            "uncertainties": [item.text for item in document.uncertainties],
            "sources": [
                {
                    "title": source.title,
                    "publisher": source.publisher,
                    "date": source.published_at.isoformat()
                    if source.published_at is not None
                    else None,
                    "url": source.canonical_url,
                }
                for source in document.sources
            ],
        }
        render_data_bytes = json.dumps(
            render_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return TypstRenderSource(
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            render_data_bytes=render_data_bytes,
            render_data_sha256=hashlib.sha256(render_data_bytes).hexdigest(),
            media_refs=tuple(media_refs_by_id.values()),
        )


class TemplateBundleInvalidError(ValueError):
    """Raised when the renderer manifest or one of its listed files is invalid."""


def _load_renderer_manifest(chp_typst_root: Path) -> tuple[Path, str, tuple[str, ...]]:
    """Read renderer-manifest.json, returning (resolved root, version, files)."""
    try:
        root = chp_typst_root.resolve(strict=True)
    except OSError as exc:
        raise TemplateBundleInvalidError(
            f"Typst template root is missing or unreadable: {chp_typst_root}"
        ) from exc
    if not root.is_dir():
        raise TemplateBundleInvalidError(f"Typst template root is not a directory: {root}")

    manifest_path = root / "renderer-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TemplateBundleInvalidError(
            f"Typst renderer manifest is missing or invalid: {manifest_path}"
        ) from exc
    if not isinstance(manifest, dict):
        raise TemplateBundleInvalidError("Typst renderer manifest must be a JSON object")
    template_version = manifest.get("template_version")
    files = manifest.get("files")
    if not isinstance(template_version, str) or not template_version.strip():
        raise TemplateBundleInvalidError(
            "Typst renderer manifest requires a non-empty template_version"
        )
    if not isinstance(files, list) or any(
        not isinstance(path, str) or not path.strip() for path in files
    ):
        raise TemplateBundleInvalidError(
            "Typst renderer manifest files must be a list of non-empty relative paths"
        )
    if len(set(files)) != len(files):
        raise TemplateBundleInvalidError("Typst renderer manifest contains duplicate file paths")
    return root, template_version, tuple(files)


def load_template_bundle(chp_typst_root: Path) -> TypstTemplateBundle:
    """Read and hash the exact manifest-listed template bytes in one operation."""
    root, template_version, files = _load_renderer_manifest(chp_typst_root)
    resolved_files = resolve_bundle_files(root, files, error=TemplateBundleInvalidError)
    contents: list[TemplateFile] = []
    for relative_path, source_path in resolved_files:
        try:
            content = source_path.read_bytes()
        except OSError as exc:
            raise TemplateBundleInvalidError(
                f"Manifest-listed file cannot be read: {relative_path!r}"
            ) from exc
        contents.append(TemplateFile(relative_path=relative_path, content=content))
    return TypstTemplateBundle(
        template_version=template_version,
        sha256=hash_bundle_contents(tuple((file.relative_path, file.content) for file in contents)),
        files=tuple(contents),
    )


def compute_template_bundle_hash(chp_typst_root: Path) -> tuple[str, str]:
    """Return the manifest version and stable hash of exactly its listed files."""
    bundle = load_template_bundle(chp_typst_root)
    return bundle.template_version, bundle.sha256


def _display_date(date_text: str | None, event_date: date | None) -> str:
    if date_text:
        return date_text
    if event_date is not None:
        return event_date.isoformat()
    # The render-data contract requires text, and blank preserves the absence
    # of a date instead of inventing one.
    return ""


def _timeline_source_urls(
    evidence_refs: tuple[PublicationEvidenceRefV1, ...],
    sources_by_id: Mapping[UUID, PublicationSourceV1],
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for evidence_ref in evidence_refs:
        source = sources_by_id.get(evidence_ref.source_document_id)
        if source is None:
            raise ValueError(
                "Timeline evidence references an unknown source document: "
                f"{evidence_ref.source_document_id}"
            )
        if source.canonical_url not in seen:
            seen.add(source.canonical_url)
            urls.append(source.canonical_url)
    return urls


def _table_block(table: PublicationTableV1) -> dict[str, Any]:
    return {
        "type": "table",
        "key": table.key,
        "title": table.title,
        "caption": table.caption,
        "columns": [column.label for column in table.columns],
        "rows": [list(row.cells) for row in table.rows],
    }


def _diagram_block(
    diagram: PublicationDiagramV1,
    media_ref_factory: Callable[..., TypstMediaRef],
) -> dict[str, Any]:
    media = media_ref_factory(
        asset_id=diagram.asset_id,
        expected_kind=MediaAssetKind.DIAGRAM_SVG,
        expected_mime_type="image/svg+xml",
        expected_sha256=None,
        expected_byte_size=None,
    )
    return {
        "type": "diagram",
        "key": diagram.key,
        "title": diagram.title,
        "caption": diagram.caption,
        "media_path": media.media_path,
    }


def _figure_block(
    figure: PublicationSourceFigureV1,
    media_ref_factory: Callable[..., TypstMediaRef],
) -> dict[str, Any]:
    media = media_ref_factory(
        asset_id=figure.asset_id,
        expected_kind=MediaAssetKind.SOURCE_FIGURE,
        expected_mime_type=figure.mime_type,
        expected_sha256=figure.sha256,
        expected_byte_size=figure.byte_size,
    )
    locator_parts = []
    if figure.locator.page is not None:
        locator_parts.append(f"page {figure.locator.page}")
    if figure.locator.section is not None:
        locator_parts.append(f"section {figure.locator.section}")
    if figure.locator.figure_label is not None:
        locator_parts.append(f"figure {figure.locator.figure_label}")
    return {
        "type": "figure",
        "key": figure.key,
        "caption": figure.caption,
        "provenance": figure.provenance,
        "locator": ", ".join(locator_parts) if locator_parts else None,
        "media_path": media.media_path,
    }
