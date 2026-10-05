from __future__ import annotations

import asyncio
import hashlib
import xml.etree.ElementTree as ET
from uuid import UUID

import pytest

from cti_app.application.analytic_chart_compilation import CompiledChart
from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    EditorialAnalyticPurposeV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure.analytic_chart_renderer import (
    ANALYTIC_CHART_PALETTE,
    AnalyticChartRenderer,
    render_timeline_svg,
)

_REF = ExtractionEvidenceRefV1(
    source_document_id=UUID("00000000-0000-0000-0000-000000000001"),
    kind=EvidenceKind.FACT,
    evidence_key="a" * 64,
)
_PURPOSE = EditorialAnalyticPurposeV1(
    question="When did these documented events occur?",
    available_data="Exact dates and labels from the source.",
    comprehension_gain="A shared axis makes timing easier to compare.",
    scope="Only evidenced events.",
    evidence_refs=(_REF,),
    knowledge_limits="No unreported events are inferred.",
    placement_reason="Place beside the relevant analysis.",
)


def _point(
    label: str,
    point_date: str,
    series: str = "Actor A",
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...] = (_REF,),
) -> ChartPointV1:
    return ChartPointV1(label, point_date, series, evidence_refs)


def _chart(points: tuple[ChartPointV1, ...], *, title: str = "Documented events") -> ChartSpecV1:
    return ChartSpecV1(
        key="documented_events",
        kind=ChartKind.TIMELINE,
        title=title,
        caption="Dates copied from cited evidence.",
        placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
        purpose=_PURPOSE,
        points=points,
    )


def _elements(svg: bytes) -> tuple[ET.Element, list[ET.Element]]:
    root = ET.fromstring(svg)
    return root, list(root.iter())


def test_renders_one_series_with_three_evidence_backed_points() -> None:
    svg = render_timeline_svg(
        _chart(
            (
                _point("alpha.example", "2026-08-25"),
                _point("beta.example", "2026-08-27"),
                _point("gamma.example", "2026-08-31"),
            )
        )
    )
    _, elements = _elements(svg)

    assert sum(item.tag.endswith("circle") and "cy" in item.attrib for item in elements) == 4
    assert svg.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"')


def test_renders_three_series_with_fixed_editorial_palette() -> None:
    chart = _chart(
        (
            _point("alpha.example", "2026-08-25", "Actor C"),
            _point("beta.example", "2026-08-27", "Actor A"),
            _point("gamma.example", "2026-08-31", "Actor B"),
        )
    )

    svg = render_timeline_svg(chart).decode()

    assert all(color in svg for color in ANALYTIC_CHART_PALETTE[:3])
    assert svg.count("<circle") == 6


def test_renders_twenty_points_and_preserves_long_labels_in_point_titles() -> None:
    points = tuple(
        _point(f"domain-{index:02d}.example", f"2026-08-{index + 1:02d}") for index in range(20)
    )
    svg = render_timeline_svg(_chart(points)).decode()

    assert svg.count("<circle") == 21  # Twenty evidence points plus the legend marker.
    assert "domain-00.example" in svg and "domain-19.example" in svg


def test_same_day_same_second_and_identical_dates_keep_each_point_visible() -> None:
    points = (
        _point("first.example", "2026-08-25"),
        _point("second.example", "2026-08-25"),
        _point("third.example", "2026-08-25T12:13:14Z"),
        _point("fourth.example", "2026-08-25T12:13:14Z"),
    )
    svg = render_timeline_svg(_chart(points))
    _, elements = _elements(svg)
    circles = [
        item
        for item in elements
        if item.tag.endswith("circle") and "cy" in item.attrib and list(item)
    ]

    assert len(circles) == len(points)
    assert len({circle.attrib["cx"] for circle in circles[:2]}) == 2
    assert len({circle.attrib["cx"] for circle in circles[2:]}) == 2


def test_labels_are_shortened_visually_but_full_text_remains_in_accessible_title() -> None:
    long_label = "very-long-domain-registration-name.example"
    svg = render_timeline_svg(_chart((_point(long_label, "2026-08-25"),))).decode()

    assert "…" in svg
    assert f"<title>{long_label} | 2026-08-25 | Actor A</title>" in svg


@pytest.mark.parametrize("invalid", ("yesterday", "2026-02-30", "2026-08-25T12:13:14"))
def test_rejects_relative_invalid_and_timezone_free_dates(invalid: str) -> None:
    with pytest.raises(ValueError):
        _point("alpha.example", invalid)


def test_rejects_points_without_evidence() -> None:
    with pytest.raises(ValueError):
        _point("alpha.example", "2026-08-25", evidence_refs=())


def test_unordered_input_and_repeated_entry_are_deterministic_and_visible() -> None:
    repeated = _point("same.example", "2026-08-25")
    points = (
        _point("later.example", "2026-08-27"),
        repeated,
        repeated,
    )
    first = render_timeline_svg(_chart(points))
    shuffled = render_timeline_svg(_chart(tuple(reversed(points))))
    _, elements = _elements(first)
    circles = [
        item
        for item in elements
        if item.tag.endswith("circle") and "cy" in item.attrib and list(item)
    ]

    assert first == shuffled
    assert len(circles) == 3
    assert circles[0].attrib["cx"] != circles[1].attrib["cx"]


def test_compiler_returns_repeatable_svg_bytes_and_sha256() -> None:
    chart = _chart(
        (
            _point("alpha.example", "2026-08-25"),
            _point("beta.example", "2026-08-27", "Actor B"),
        )
    )

    first: CompiledChart = asyncio.run(AnalyticChartRenderer().compile(chart))
    second: CompiledChart = asyncio.run(AnalyticChartRenderer().compile(chart))

    assert first.media_bytes == second.media_bytes
    digest = hashlib.sha256(first.media_bytes).hexdigest()
    assert first.media_sha256 == second.media_sha256 == digest
    ET.fromstring(first.media_bytes)
