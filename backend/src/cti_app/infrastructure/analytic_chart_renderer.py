"""Small deterministic SVG renderer for the editorial timeline chart contract."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime, time
from html import escape

from cti_app.application.analytic_chart_compilation import (
    AnalyticChartCompilationError,
    CompiledChart,
)
from cti_app.domain.production_editorial_enrichment import ChartPointV1, ChartSpecV1
from cti_app.domain.production_synthesis import evidence_ref_sort_key

ANALYTIC_CHART_RENDERER = "autowork-timeline-svg"
ANALYTIC_CHART_RENDERER_VERSION = "1.0"
ANALYTIC_CHART_RENDERER_POLICY_VERSION = "editorial-timeline-svg-v1"
ANALYTIC_CHART_PALETTE = (
    "#B3243B",
    "#9A6B00",
    "#C2410C",
    "#1D4E89",
    "#2E7D32",
    "#5B3E96",
    "#667085",
)

_WIDTH = 1000
_PLOT_LEFT = 112
_PLOT_RIGHT = 770
_PLOT_TOP = 78
_ROW_HEIGHT = 48
_TICK_COUNT = 5
_LABEL_LIMIT = 20


def _instant(value: str) -> datetime:
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _xml_text(value: str) -> str:
    return escape(value, quote=True)


def _short_label(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= _LABEL_LIMIT:
        return compact
    return compact[: _LABEL_LIMIT - 1].rstrip() + "…"


def _tick_label(moment: datetime, *, date_only: bool, span_seconds: float) -> str:
    if span_seconds < 2 * 24 * 60 * 60 and not date_only:
        return moment.strftime("%H:%M")
    if span_seconds > 180 * 24 * 60 * 60:
        return moment.strftime("%Y-%m")
    if date_only:
        return moment.strftime("%d/%m/%Y")
    return moment.strftime("%d/%m")


def _point_identity(point: ChartPointV1) -> tuple[object, ...]:
    return (
        _instant(point.date),
        point.series.casefold(),
        point.series,
        point.label.casefold(),
        point.label,
        tuple(evidence_ref_sort_key(ref) for ref in point.evidence_refs),
    )


def render_timeline_svg(chart: ChartSpecV1) -> bytes:
    """Render all points in a stable order; labels may be omitted on collision."""
    if not isinstance(chart, ChartSpecV1):
        raise ValueError("Expected a ChartSpecV1")
    points = sorted(chart.points, key=_point_identity)
    series_names = tuple(
        sorted({point.series for point in points}, key=lambda s: (s.casefold(), s))
    )
    series_index = {name: index for index, name in enumerate(series_names)}
    series_color = {
        name: ANALYTIC_CHART_PALETTE[index % len(ANALYTIC_CHART_PALETTE)]
        for index, name in enumerate(series_names)
    }
    instants = [_instant(point.date) for point in points]
    low = min(instants)
    high = max(instants)
    span = (high - low).total_seconds()
    height = max(190, _PLOT_TOP + len(series_names) * _ROW_HEIGHT + 56)
    plot_bottom = _PLOT_TOP + (len(series_names) - 1) * _ROW_HEIGHT + 22

    def x_position(moment: datetime) -> float:
        if span == 0:
            return (_PLOT_LEFT + _PLOT_RIGHT) / 2
        fraction = (moment - low).total_seconds() / span
        return _PLOT_LEFT + fraction * (_PLOT_RIGHT - _PLOT_LEFT)

    # Exact duplicates in one series stay visible by receiving a small stable x offset.
    grouped: dict[tuple[str, datetime], list[tuple[int, ChartPointV1]]] = defaultdict(list)
    for index, point in enumerate(points):
        grouped[(point.series, _instant(point.date))].append((index, point))
    jitter: dict[int, float] = {}
    for (_series, _moment), siblings in grouped.items():
        ordered = sorted(siblings, key=lambda item: _point_identity(item[1]))
        for offset, (point_index, _point) in enumerate(ordered):
            jitter[point_index] = (offset - (len(ordered) - 1) / 2) * 5.0

    date_only = all(len(point.date) == 10 for point in points)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{height}" '
        f'viewBox="0 0 {_WIDTH} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{_xml_text(chart.title)}</title>',
        f'<desc id="desc">Timeline with {len(points)} evidence-backed points across '
        f"{len(series_names)} series.</desc>",
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        f'<text x="{_PLOT_LEFT}" y="28" fill="#17202A" font-family="Arial, sans-serif" '
        f'font-size="16" font-weight="700">{_xml_text(chart.title)}</text>',
    ]

    # Series rows and the time axis are both explicit; no value is interpolated.
    tick_count = 1 if span == 0 else _TICK_COUNT
    for index in range(tick_count):
        fraction = 0.5 if tick_count == 1 else index / (tick_count - 1)
        x = _PLOT_LEFT + (_PLOT_RIGHT - _PLOT_LEFT) * fraction
        parts.append(
            f'<line x1="{x:.2f}" y1="{_PLOT_TOP + 8}" x2="{x:.2f}" '
            f'y2="{plot_bottom + 4}" stroke="#EAECF0" stroke-width="1"/>'
        )

    for name in series_names:
        y = _PLOT_TOP + series_index[name] * _ROW_HEIGHT + 22
        parts.append(
            f'<line x1="{_PLOT_LEFT}" y1="{y:.2f}" x2="{_PLOT_RIGHT}" y2="{y:.2f}" '
            'stroke="#D0D5DD" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_PLOT_LEFT - 12}" y="{y + 4:.2f}" text-anchor="end" '
            f'fill="{series_color[name]}" font-family="Arial, sans-serif" font-size="11">'
            f"{_xml_text(name)}</text>"
        )

    label_boxes: list[tuple[float, float, float, float]] = []
    for point_index, point in enumerate(points):
        instant = _instant(point.date)
        base_x = x_position(instant)
        x = min(_PLOT_RIGHT - 2, max(_PLOT_LEFT + 2, base_x + jitter[point_index]))
        y = _PLOT_TOP + series_index[point.series] * _ROW_HEIGHT + 22
        color = series_color[point.series]
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.2" fill="{color}" '
            f'stroke="#FFFFFF" stroke-width="1.2"><title>{_xml_text(point.label)} | '
            f"{_xml_text(point.date)} | {_xml_text(point.series)}</title></circle>"
        )

        label = _short_label(point.label)
        width = max(16.0, len(label) * 6.1)
        candidates = (
            (x + 7, y - 7, "start"),
            (x + 7, y + 15, "start"),
            (x - 7, y - 7, "end"),
            (x - 7, y + 15, "end"),
        )
        for label_x, label_y, anchor in candidates:
            left = label_x if anchor == "start" else label_x - width
            right = label_x + width if anchor == "start" else label_x
            box = (left, right, label_y - 11, label_y + 3)
            if left < _PLOT_LEFT or right > _PLOT_RIGHT:
                continue
            if any(
                box[0] < existing[1]
                and box[1] > existing[0]
                and box[2] < existing[3]
                and box[3] > existing[2]
                for existing in label_boxes
            ):
                continue
            label_boxes.append(box)
            parts.append(
                f'<text x="{label_x:.2f}" y="{label_y:.2f}" text-anchor="{anchor}" '
                f'fill="#344054" font-family="Arial, sans-serif" font-size="10">'
                f"{_xml_text(label)}</text>"
            )
            break

    tick_y = plot_bottom + 26
    for index in range(tick_count):
        fraction = 0.5 if tick_count == 1 else index / (tick_count - 1)
        moment = low + (high - low) * fraction
        x = _PLOT_LEFT + (_PLOT_RIGHT - _PLOT_LEFT) * fraction
        parts.append(
            f'<text x="{x:.2f}" y="{tick_y}" text-anchor="middle" fill="#667085" '
            f'font-family="Arial, sans-serif" font-size="10">'
            f"{_tick_label(moment, date_only=date_only, span_seconds=span)}</text>"
        )

    legend_y = _PLOT_TOP + 10
    for index, name in enumerate(series_names):
        y = legend_y + index * 24
        parts.extend(
            (
                f'<circle cx="820" cy="{y - 4}" r="5" fill="{series_color[name]}"/>',
                f'<text x="834" y="{y}" fill="#344054" font-family="Arial, sans-serif" '
                f'font-size="12">{_xml_text(name)}</text>',
            )
        )

    parts.append("</svg>")
    content = "\n".join(parts).encode("utf-8")
    return content


class AnalyticChartRenderer:
    """Infrastructure implementation of the application's chart compiler port."""

    async def compile(self, chart: ChartSpecV1) -> CompiledChart:
        try:
            content = render_timeline_svg(chart)
        except (OverflowError, ValueError) as exc:
            raise AnalyticChartCompilationError() from exc
        return CompiledChart(
            chart_key=chart.key,
            media_type="image/svg+xml",
            media_bytes=content,
            media_sha256=hashlib.sha256(content).hexdigest(),
            compiler=ANALYTIC_CHART_RENDERER,
            compiler_version=ANALYTIC_CHART_RENDERER_VERSION,
            compiler_policy_version=ANALYTIC_CHART_RENDERER_POLICY_VERSION,
        )
