from __future__ import annotations

import hashlib
from dataclasses import dataclass

from tests.editorial.fixtures.enrichment import ArchivedFigureFixture


@dataclass(frozen=True, slots=True)
class BriefFixture:
    brief_id: str
    title: str
    summary: str
    candidate_figures: tuple[ArchivedFigureFixture, ...]


def _candidate_figures() -> tuple[ArchivedFigureFixture, ...]:
    return tuple(
        ArchivedFigureFixture(
            archive_id=f"synthetic-brief-figure-{index:03d}",
            content=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="320" '
                f'height="180"><title>Brief candidate {index}</title></svg>'
            ).encode(),
            sha256=hashlib.sha256(
                (
                    '<svg xmlns="http://www.w3.org/2000/svg" width="320" '
                    f'height="180"><title>Brief candidate {index}</title></svg>'
                ).encode()
            ).hexdigest(),
            mime_type="image/svg+xml",
            width_px=320,
            height_px=180,
            caption=f"Synthetic brief candidate {index}",
        )
        for index in range(1, 5)
    )


def synthetic_brief() -> BriefFixture:
    return BriefFixture(
        brief_id="synthetic-brief-001",
        title="Synthetic editorial brief",
        summary="A fixture used only for local editorial tests.",
        candidate_figures=_candidate_figures(),
    )
