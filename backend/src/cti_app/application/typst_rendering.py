"""Pure projection of canonical publication documents into Typst input data."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
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
    CanonicalPublicationDocument,
    PublicationChartV1,
    PublicationDiagramV1,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationSourceFigureV1,
    PublicationTableV1,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ROLE_TYPST_FUNCTION_V1,
    lead_paragraph_anchor,
    section_paragraph_anchor,
    timeline_anchor,
)

_RENDER_DATA_SCHEMA_VERSION = "typst-publication-model-v8-unified-ioc-rendering"
_PUBLICATION_RENDERER_MANIFEST = "renderer-manifest.json"
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
_BREAKABLE_SEMANTIC_ROLES = frozenset({"ioc", "path", "command"})
_TYPOGRAPHIC_BREAK_INTERVAL = 16
_TABLE_SEMANTIC_STYLE_BY_STYLE = {
    "semantic-command": "semantic-table-command",
    "semantic-ioc": "semantic-table-ioc",
    "semantic-path": "semantic-table-path",
    "semantic-technical-literal": "semantic-table-technical",
}
_TABLE_SHA256 = re.compile(r"^(?:sha-?256(?:\s*:\s*|\s+))?[0-9a-f]{64}$", re.IGNORECASE)
_TABLE_DOMAIN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$",
    re.IGNORECASE,
)
_TABLE_WINDOWS_PATH = re.compile(r"^[a-z]:[\\/].+", re.IGNORECASE)


def _breakable_typst_display_text(value: str) -> str:
    """Add line-break opportunities to render data without changing canonical text."""
    if len(value) <= _TYPOGRAPHIC_BREAK_INTERVAL:
        return value
    return "\u200b".join(
        value[index : index + _TYPOGRAPHIC_BREAK_INTERVAL]
        for index in range(0, len(value), _TYPOGRAPHIC_BREAK_INTERVAL)
    )


def _indicator_values(groups: tuple[Any, ...]) -> dict[str, list[str]]:
    """Merge display IOC values and deduplicate without changing canonical records."""
    result: dict[str, list[str]] = {
        key: [] for key in ("ips", "domains", "urls", "emails", "hashes")
    }
    seen: dict[str, set[str]] = {key: set() for key in result}
    for group in groups:
        key = _INDICATOR_KEYS[group.artifact_type]
        for indicator in group.indicators:
            normalized_value = indicator.normalized_value.casefold()
            if normalized_value in seen[key]:
                continue
            seen[key].add(normalized_value)
            result[key].append(_breakable_typst_display_text(indicator.value))
    return result


def _table_cell_typst_spans(value: str) -> list[dict[str, str]]:
    """Format exact technical table values and add invisible wrap points to long cells."""
    style = "semantic-plain"
    if _TABLE_SHA256.fullmatch(value):
        style = "semantic-table-technical"
    elif value.startswith(("/", "\\")) or _TABLE_WINDOWS_PATH.fullmatch(value):
        style = "semantic-table-path"
    elif value.startswith(("http://", "https://")) and not any(char.isspace() for char in value):
        style = "semantic-table-ioc"
    elif _TABLE_DOMAIN.fullmatch(value):
        style = "semantic-table-ioc"
    else:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            style = "semantic-table-ioc"
    return [
        {
            "style": style,
            "text": _breakable_typst_display_text(value),
        }
    ]


def _table_cell_render_spans(
    value: str,
    semantic_cell_spans: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    if semantic_cell_spans is None:
        return _table_cell_typst_spans(value)
    return [
        {
            "style": _TABLE_SEMANTIC_STYLE_BY_STYLE.get(span["style"], span["style"]),
            "text": _breakable_typst_display_text(span["text"].replace("\u200b", "")),
        }
        for span in semantic_cell_spans
    ]


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
    entrypoint_relative_path: str = "RENDERER/publication.typ"
    render_data_relative_path: str = "RENDERER/render-data.json"


@dataclass(frozen=True, slots=True)
class TypstPublicationModelV2:
    title: str
    content_sections: list[dict[str, Any]]
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
        self, document: CanonicalPublicationDocument, template_bundle: TypstTemplateBundle
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
        model = project_publication_to_typst_model(document)

        render_data: dict[str, Any] = {
            "schema_version": _RENDER_DATA_SCHEMA_VERSION,
            "language": document.publication_language,
            "title": model.title,
            "content_sections": model.content_sections,
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
            media_refs=model.media_refs,
        )


def project_publication_to_typst_model(
    value: CanonicalPublicationDocument,
) -> TypstPublicationModelV2:
    """Project a canonical publication while keeping semantic content as data."""
    if isinstance(value, PublicationDocumentV5):
        document = value.document
        semantic_by_anchor = {item.anchor: item for item in value.semantic_text.paragraphs}
        reference_entries = value.references
        original_indicator_groups = value.original_indicators
    elif isinstance(value, PublicationDocumentV4):
        document = value
        semantic_by_anchor = {}
        reference_entries = ()
        original_indicator_groups = ()
    else:
        raise ValueError("Unsupported canonical publication document")

    def semantic_spans(anchor: str, text: str) -> list[dict[str, str]] | None:
        paragraph = semantic_by_anchor.get(anchor)
        if paragraph is None:
            return None
        if paragraph.text != text:
            raise ValueError(f"Semantic text differs from publication content at {anchor}")
        return [
            {
                "style": SEMANTIC_ROLE_TYPST_FUNCTION_V1[span.role],
                "text": (
                    _breakable_typst_display_text(span.text)
                    if span.role.value in _BREAKABLE_SEMANTIC_ROLES
                    else span.text
                ),
            }
            for span in paragraph.spans
        ]

    def rich_block(block: dict[str, Any], field_name: str, anchor: str, text: str) -> None:
        spans = semantic_spans(anchor, text)
        if spans is not None:
            block[field_name] = spans

    media_refs_by_id: dict[UUID, TypstMediaRef] = {}
    rich_by_placement: dict[tuple[EnrichmentPlacementKind, int | None], list[dict[str, Any]]] = {}

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
        block = _table_block(table)
        rich_block(block, "semantic_title", f"table:{table.key}:title", table.title)
        if table.caption is not None:
            rich_block(block, "semantic_caption", f"table:{table.key}:caption", table.caption)
        column_spans = [
            semantic_spans(f"table:{table.key}:column:{column_index:04d}", column.label)
            for column_index, column in enumerate(table.columns, start=1)
        ]
        if column_spans and all(item is not None for item in column_spans):
            block["semantic_columns"] = column_spans
        cell_spans = [
            _table_cell_render_spans(
                cell,
                semantic_spans(
                    f"table:{table.key}:row:{row_index:04d}:cell:{cell_index:04d}",
                    cell,
                ),
            )
            for row_index, row in enumerate(table.rows, start=1)
            for cell_index, cell in enumerate(row.cells, start=1)
        ]
        if cell_spans:
            # Keep the renderer's flat row-cell sequence; each entry is one cell's
            # semantic spans, not a recursively flattened span array.
            block["semantic_cells"] = cell_spans
        add_rich(
            table.placement.kind,
            table.placement.section_index,
            block,
        )
    for diagram in document.diagrams:
        block = _diagram_block(diagram, media_ref)
        rich_block(block, "semantic_title", f"diagram:{diagram.key}:title", diagram.title)
        if diagram.caption is not None:
            rich_block(block, "semantic_caption", f"diagram:{diagram.key}:caption", diagram.caption)
        add_rich(
            diagram.placement.kind,
            diagram.placement.section_index,
            block,
        )
    for chart in document.charts:
        block = _chart_block(chart, media_ref)
        rich_block(block, "semantic_title", f"chart:{chart.key}:title", chart.title)
        if chart.caption is not None:
            rich_block(block, "semantic_caption", f"chart:{chart.key}:caption", chart.caption)
        add_rich(
            chart.placement.kind,
            chart.placement.section_index,
            block,
        )
    for figure in document.figures:
        block = _figure_block(figure, media_ref)
        rich_block(block, "semantic_caption", f"figure:{figure.key}:caption", figure.caption)
        rich_block(
            block,
            "semantic_provenance",
            f"figure:{figure.key}:provenance",
            figure.provenance,
        )
        add_rich(
            figure.placement.kind,
            figure.placement.section_index,
            block,
        )

    reference_blocks = list(
        rich_by_placement.get((EnrichmentPlacementKind.AFTER_TIMELINE, None), ())
    )
    body_blocks: list[dict[str, Any]] = []
    body_blocks.extend(
        {
            "type": "paragraph",
            "text": item.text,
            **(
                {"semantic_spans": spans}
                if (spans := semantic_spans(lead_paragraph_anchor(index), item.text)) is not None
                else {}
            ),
        }
        for index, item in enumerate(document.lead, start=1)
    )
    body_blocks.extend(rich_by_placement.get((EnrichmentPlacementKind.AFTER_LEAD, None), ()))
    lead_fingerprints = {" ".join(item.text.casefold().split()) for item in document.lead}
    for section_index, section in enumerate(document.sections):
        body_blocks.extend(
            {
                "type": "paragraph",
                "text": paragraph.text,
                **(
                    {"semantic_spans": spans}
                    if (
                        spans := semantic_spans(
                            section_paragraph_anchor(section_index, paragraph_index),
                            paragraph.text,
                        )
                    )
                    is not None
                    else {}
                ),
            }
            for paragraph_index, paragraph in enumerate(section.paragraphs, start=1)
            if " ".join(paragraph.text.casefold().split()) not in lead_fingerprints
        )
        body_blocks.extend(
            rich_by_placement.get((EnrichmentPlacementKind.AFTER_SECTION, section_index), ())
        )
    body_blocks.extend(rich_by_placement.get((EnrichmentPlacementKind.END, None), ()))

    figure_number = 0
    for block in (*reference_blocks, *body_blocks):
        if block.get("type") in {"diagram", "figure", "chart"}:
            figure_number += 1
            block["figure_number"] = figure_number

    sources_by_id = {source.source_document_id: source for source in document.sources}
    timeline = []
    for index, entry in enumerate(document.timeline, start=1):
        item: dict[str, Any] = {
            "display_date": _display_date(entry.date_text, entry.event_date),
            "text": entry.text,
            "source_urls": _timeline_source_urls(entry.evidence_refs, sources_by_id),
        }
        spans = semantic_spans(timeline_anchor(index), entry.text)
        if spans is not None:
            item["semantic_spans"] = spans
        timeline.append(item)
    source_references = []
    for index, reference in enumerate(reference_entries, start=1):
        source = sources_by_id.get(reference.source_document_id)
        if source is None:
            raise ValueError(
                "Publication reference identifies an unknown source document: "
                f"{reference.source_document_id}"
            )
        item = {
            "display_date": (
                _display_date(None, source.published_at)
                if source.published_at is not None
                else "Date de publication non précisée"
            ),
            "text": reference.text,
            "source_urls": [source.canonical_url],
        }
        spans = semantic_spans(f"reference:{index:04d}", reference.text)
        if spans is not None:
            item["semantic_spans"] = spans
        source_references.append(item)
    sources = [
        {
            "title": source.title,
            "publisher": source.publisher,
            "date": source.published_at.isoformat() if source.published_at is not None else None,
            "url": source.canonical_url,
        }
        for source in document.sources
    ]

    indicators = _indicator_values((*document.indicators, *original_indicator_groups))
    original_indicators = _indicator_values(original_indicator_groups)
    original_publishers = sorted(
        {
            (sources_by_id[source_id].publisher or "").strip()
            or (sources_by_id[source_id].canonical_url.partition("://")[2].split("/", 1)[0])
            for group in original_indicator_groups
            for indicator in group.indicators
            for source_id in indicator.source_document_ids
            if source_id in sources_by_id
        },
        key=str.casefold,
    )
    original_indicator_note = (
        "Le lien avec le sujet n\u2019est pas démontré. Sources : "
        f"{' ; '.join(original_publishers)}."
        if original_publishers
        else "Le lien avec le sujet n\u2019est pas démontré."
    )

    content_sections: list[dict[str, Any]] = [
        {
            "type": "references",
            "timeline": source_references if isinstance(value, PublicationDocumentV5) else timeline,
            "blocks": reference_blocks,
            "sources": sources,
        },
        {"type": "synthesis", "blocks": body_blocks},
    ]
    if any(indicators.values()) or any(original_indicators.values()):
        content_sections.append(
            {
                "type": "technical_annex",
                "indicators": indicators,
                "original_indicators": original_indicators,
                "original_indicator_note": original_indicator_note,
            }
        )

    return TypstPublicationModelV2(
        title=document.title,
        content_sections=content_sections,
        media_refs=tuple(media_refs_by_id.values()),
    )


class TemplateBundleInvalidError(ValueError):
    """Raised when the renderer manifest or one of its listed files is invalid."""


def _load_renderer_manifest(
    chp_typst_root: Path, manifest_name: str = _PUBLICATION_RENDERER_MANIFEST
) -> tuple[Path, str, tuple[str, ...]]:
    """Read one renderer manifest, returning (resolved root, version, files)."""
    try:
        root = chp_typst_root.resolve(strict=True)
    except OSError as exc:
        raise TemplateBundleInvalidError(
            f"Typst template root is missing or unreadable: {chp_typst_root}"
        ) from exc
    if not root.is_dir():
        raise TemplateBundleInvalidError(f"Typst template root is not a directory: {root}")

    manifest_relative_path = PurePosixPath(manifest_name)
    if (
        manifest_relative_path.is_absolute()
        or "\\" in manifest_name
        or ".." in manifest_relative_path.parts
        or len(manifest_relative_path.parts) != 1
    ):
        raise TemplateBundleInvalidError("Typst renderer manifest name must be a relative filename")
    manifest_path = root / manifest_name
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


def load_template_bundle(
    chp_typst_root: Path,
    *,
    manifest_name: str = _PUBLICATION_RENDERER_MANIFEST,
) -> TypstTemplateBundle:
    """Read and hash the exact manifest-listed template bytes in one operation."""
    root, template_version, files = _load_renderer_manifest(chp_typst_root, manifest_name)
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


def compute_template_bundle_hash(
    chp_typst_root: Path,
    *,
    manifest_name: str = _PUBLICATION_RENDERER_MANIFEST,
) -> tuple[str, str]:
    """Return the manifest version and stable hash of exactly its listed files."""
    bundle = load_template_bundle(chp_typst_root, manifest_name=manifest_name)
    return bundle.template_version, bundle.sha256


def _display_date(date_text: str | None, event_date: date | None) -> str:
    if date_text:
        return date_text
    if event_date is not None:
        day = "1er" if event_date.day == 1 else str(event_date.day)
        return f"{day} {_FRENCH_MONTH_NAMES[event_date.month - 1]} {event_date.year}"
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
        "column_weights": _table_column_weights(table),
        "rows": [list(row.cells) for row in table.rows],
    }


def _table_column_weights(table: PublicationTableV1) -> list[float]:
    """Use bounded, content-derived Typst fractions so wider fields get more room."""
    weights: list[float] = []
    for index, column in enumerate(table.columns):
        values = [column.label, *(row.cells[index] for row in table.rows)]
        longest = max((len(value.strip()) for value in values), default=1)
        weight = min(2.4, max(0.8, longest / 16))
        weights.append(round(weight, 2))
    return weights


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


def _chart_block(
    chart: PublicationChartV1,
    media_ref_factory: Callable[..., TypstMediaRef],
) -> dict[str, Any]:
    media = media_ref_factory(
        asset_id=chart.asset_id,
        expected_kind=MediaAssetKind.CHART_SVG,
        expected_mime_type="image/svg+xml",
        expected_sha256=None,
        expected_byte_size=None,
    )
    return {
        "type": "chart",
        "key": chart.key,
        "kind": chart.kind.value,
        "title": chart.title,
        "caption": chart.caption,
        "media_path": media.media_path,
    }
