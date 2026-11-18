from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArchivedFigureFixture:
    archive_id: str
    content: bytes
    sha256: str
    mime_type: str
    width_px: int
    height_px: int
    caption: str


@dataclass(frozen=True, slots=True)
class EditorialEnrichmentFixture:
    enrichment_id: str
    figures: tuple[ArchivedFigureFixture, ...]


_ARCHIVED_FIGURE_CONTENT = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">'
    b"<title>Synthetic archive figure</title></svg>"
)
ARCHIVED_FIGURE_SHA256 = "44a1d590acdf039e4e198f3ef812e1e6f37ccef2226f0471be79c65331446dc2"


def archived_figure_enrichment() -> EditorialEnrichmentFixture:
    return EditorialEnrichmentFixture(
        enrichment_id="synthetic-enrichment-001",
        figures=(
            ArchivedFigureFixture(
                archive_id="synthetic-figure-001",
                content=_ARCHIVED_FIGURE_CONTENT,
                sha256=ARCHIVED_FIGURE_SHA256,
                mime_type="image/svg+xml",
                width_px=320,
                height_px=180,
                caption="Synthetic archived figure",
            ),
        ),
    )
