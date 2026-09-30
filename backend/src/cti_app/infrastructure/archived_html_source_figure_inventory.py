"""Inventory local image occurrences in archived HTML documents."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser

from cti_app.application.source_figure_inventory import (
    ArchivedSourceDocumentSnapshot,
    SourceFigureInventory,
    SourceFigureInventoryError,
    UnsupportedSourceFigureFormatError,
    is_external_reference,
    normalize_archive_path,
)
from cti_app.domain.production_source_figures import (
    SourceFigureCandidateV1,
    SourceFigureOriginKind,
    SourceFigureProvenanceV1,
)

# HTML URL parsing strips leading and trailing ASCII whitespace from attribute values.
_HTML_WHITESPACE = "\t\n\f\r "


class _ImageOccurrenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.occurrences: list[tuple[str | None, str | None, str | None, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return

        # HTML keeps the first occurrence of a duplicated attribute.
        attributes: dict[str, str | None] = {}
        for name, value in attrs:
            attributes.setdefault(name, value)
        src = attributes.get("src")
        self.occurrences.append(
            (
                src.strip(_HTML_WHITESPACE) if src is not None else None,
                attributes.get("alt"),
                attributes.get("title"),
                len(self.occurrences),
            )
        )


class ArchivedHtmlSourceFigureInventory(SourceFigureInventory):
    """Read image bytes only from the local assets in an archived snapshot."""

    async def inventory(
        self, source: ArchivedSourceDocumentSnapshot
    ) -> tuple[SourceFigureCandidateV1, ...]:
        if source.media_type != "text/html":
            raise UnsupportedSourceFigureFormatError(
                f"Unsupported archived source media type: {source.media_type}"
            )

        try:
            document = source.body_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceFigureInventoryError(
                "Archived HTML document body is not valid UTF-8"
            ) from exc

        parser = _ImageOccurrenceParser()
        parser.feed(document)
        parser.close()

        assets_by_path = {asset.path: asset for asset in source.local_assets}
        candidates: list[SourceFigureCandidateV1] = []
        for locator, alt_text, title_text, occurrence_index in parser.occurrences:
            if not locator or is_external_reference(locator):
                continue

            try:
                archive_path = normalize_archive_path(locator)
            except ValueError as exc:
                raise SourceFigureInventoryError(
                    "Archived HTML image src is not a safe archive-relative path"
                ) from exc

            asset = assets_by_path.get(archive_path)
            if asset is None:
                raise SourceFigureInventoryError(
                    f"Archived HTML image asset is missing: {archive_path}"
                )

            candidate_index = len(candidates) + 1
            candidates.append(
                SourceFigureCandidateV1(
                    key=f"figure-{candidate_index:03d}",
                    source_document_id=source.source_document_id,
                    media_type=asset.media_type,
                    media_bytes=asset.content,
                    media_sha256=hashlib.sha256(asset.content).hexdigest(),
                    provenance=SourceFigureProvenanceV1(
                        origin_kind=SourceFigureOriginKind.HTML_IMAGE,
                        source_locator=locator,
                        occurrence_index=occurrence_index,
                        alt_text=alt_text,
                        title_text=title_text,
                    ),
                )
            )

        return tuple(candidates)
