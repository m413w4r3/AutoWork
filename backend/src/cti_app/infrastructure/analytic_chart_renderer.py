"""Small deterministic SVG renderers for editorial timeline and bar charts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, date, datetime, time
from decimal import Decimal
from html import escape
from typing import cast

from cti_app.application.analytic_chart_compilation import (
    AnalyticChartCompilationError,
    CompiledChart,
)
from cti_app.domain.production_editorial_enrichment import ChartPointV1, ChartSpecV1
from cti_app.domain.production_synthesis import evidence_ref_sort_key

ANALYTIC_CHART_RENDERER = "autowork-editorial-chart-svg"
ANALYTIC_CHART_RENDERER_VERSION = "2.0"
ANALYTIC_CHART_RENDERER_POLICY_VERSION = "editorial-chart-svg-v2-bar-data"
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
    if point.date is None:
        raise ValueError("Timeline point is missing its date")
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
    instants = [_instant(cast(str, point.date)) for point in points]
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
        grouped[(point.series, _instant(cast(str, point.date)))].append((index, point))
    jitter: dict[int, float] = {}
    for (_series, _moment), siblings in grouped.items():
        ordered = sorted(siblings, key=lambda item: _point_identity(item[1]))
        for offset, (point_index, _point) in enumerate(ordered):
            jitter[point_index] = (offset - (len(ordered) - 1) / 2) * 5.0

    date_only = all(len(cast(str, point.date)) == 10 for point in points)
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
        instant = _instant(cast(str, point.date))
        base_x = x_position(instant)
        x = min(_PLOT_RIGHT - 2, max(_PLOT_LEFT + 2, base_x + jitter[point_index]))
        y = _PLOT_TOP + series_index[point.series] * _ROW_HEIGHT + 22
        color = series_color[point.series]
        parts.append(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4.2" fill="{color}" '
            f'stroke="#FFFFFF" stroke-width="1.2"><title>{_xml_text(point.label)} | '
            f"{_xml_text(cast(str, point.date))} | {_xml_text(point.series)}</title></circle>"
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


def _display_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _bar_label(value: str, *, maximum: int = 32) -> str:
    compact = " ".join(value.split())
    if len(compact) <= maximum:
        return compact
    return compact[: maximum - 1].rstrip() + "…"


def _is_xml_10_text(value: str) -> bool:
    return all(
        ord(character) in {0x9, 0xA, 0xD}
        or 0x20 <= ord(character) <= 0xD7FF
        or 0xE000 <= ord(character) <= 0xFFFD
        or 0x10000 <= ord(character) <= 0x10FFFF
        for character in value
    )


def _validate_bar_chart_xml_text(chart: ChartSpecV1) -> None:
    purpose = chart.purpose
    values = (
        chart.key,
        chart.title,
        chart.caption or "",
        chart.category_axis_label or "",
        chart.value_axis_label or "",
        chart.unit or "",
        *(value for point in chart.points for value in (point.label, point.series)),
        purpose.question,
        purpose.available_data,
        purpose.comprehension_gain,
        purpose.scope,
        purpose.knowledge_limits,
        purpose.placement_reason,
    )
    if any(not _is_xml_10_text(value) for value in values):
        raise ValueError("Bar chart text contains characters forbidden by XML 1.0")


def render_bar_svg(chart: ChartSpecV1) -> bytes:
    """Render exact finite values and labels without deriving data points."""
    if not isinstance(chart, ChartSpecV1) or chart.kind.value != "bar":
        raise ValueError("Expected a typed bar ChartSpecV1")
    _validate_bar_chart_xml_text(chart)
    if any(point.value is None for point in chart.points):
        raise ValueError("Bar chart point is missing its value")

    width = 1080
    left = 286
    right = 812
    top = 116
    row_height = 38
    bottom = top + len(chart.points) * row_height
    height = bottom + 92
    numeric_values = [point.value for point in chart.points if point.value is not None]
    low = min(Decimal(0), *numeric_values)
    high = max(Decimal(0), *numeric_values)
    if low == high:
        high = Decimal(1)
    low_float = float(low)
    high_float = float(high)
    plot_span = right - left
    zero_x = left + ((0.0 - low_float) / (high_float - low_float)) * plot_span
    series_names = tuple(
        sorted({point.series for point in chart.points}, key=lambda s: (s.casefold(), s))
    )
    series_index = {name: index for index, name in enumerate(series_names)}
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{_xml_text(chart.title)}</title>',
        f'<desc id="desc">Bar chart with {len(chart.points)} individually '
        "evidence-backed values.</desc>",
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        f'<text x="{left}" y="36" fill="#17202A" font-family="Arial, sans-serif" '
        f'font-size="20" font-weight="700">{_xml_text(chart.title)}</text>',
        f'<text x="{right}" y="63" text-anchor="end" fill="#475467" '
        f'font-family="Arial, sans-serif" font-size="13">'
        f"Unit: {_xml_text(chart.unit or '')}</text>",
    ]
    if chart.category_axis_label is None or chart.value_axis_label is None:
        raise ValueError("Bar chart axis labels are missing")
    parts.extend(
        (
            f'<text x="28" y="{(top + bottom) / 2:.2f}" text-anchor="middle" '
            f'transform="rotate(-90 28 {(top + bottom) / 2:.2f})" fill="#475467" '
            f'font-family="Arial, sans-serif" font-size="13">'
            f"{_xml_text(chart.category_axis_label)}</text>",
            f'<text x="{(left + right) / 2:.2f}" y="{height - 22}" text-anchor="middle" '
            f'fill="#475467" font-family="Arial, sans-serif" font-size="13">'
            f"{_xml_text(chart.value_axis_label)}</text>",
            f'<line x1="{zero_x:.2f}" y1="{top - 4}" x2="{zero_x:.2f}" '
            f'y2="{bottom}" stroke="#98A2B3" stroke-width="1.5"/>',
        )
    )

    palette = ANALYTIC_CHART_PALETTE
    bar_height = min(22.0, row_height * 0.62 / max(1, len(series_names)))
    for index, point in enumerate(chart.points):
        value = point.value
        assert value is not None
        y_center = top + (index + 0.5) * row_height
        y = y_center + (series_index[point.series] - (len(series_names) - 1) / 2) * (bar_height + 2)
        value_float = float(value)
        value_x = left + ((value_float - low_float) / (high_float - low_float)) * plot_span
        bar_x = min(zero_x, value_x)
        bar_width = max(1.0, abs(value_x - zero_x))
        color = palette[series_index[point.series] % len(palette)]
        parts.extend(
            (
                f'<text x="{left - 16}" y="{y_center + 4:.2f}" text-anchor="end" '
                f'fill="#344054" font-family="Arial, sans-serif" font-size="12">'
                f"{_xml_text(_bar_label(point.label))}</text>",
                f'<rect x="{bar_x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" '
                f'height="{bar_height:.2f}" rx="3" fill="{color}"/>',
            )
        )
        label_x = value_x + (8 if value_float >= 0 else -8)
        anchor = "start" if value_float >= 0 else "end"
        parts.append(
            f'<text x="{label_x:.2f}" y="{y + bar_height - 3:.2f}" text-anchor="{anchor}" '
            f'fill="#17202A" font-family="Arial, sans-serif" font-size="11">'
            f"{_xml_text(_display_decimal(value))}</text>"
        )

    legend_x = right + 34
    for index, name in enumerate(series_names):
        legend_y = top + 8 + index * 26
        color = palette[index % len(palette)]
        parts.extend(
            (
                f'<rect x="{legend_x}" y="{legend_y - 12}" width="11" height="11" '
                f'rx="2" fill="{color}"/>',
                f'<text x="{legend_x + 18}" y="{legend_y - 3}" fill="#344054" '
                f'font-family="Arial, sans-serif" font-size="12">'
                f"{_xml_text(_bar_label(name))}</text>",
            )
        )
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


class AnalyticChartRenderer:
    """Infrastructure implementation of the application's chart compiler port."""

    async def compile(self, chart: ChartSpecV1) -> CompiledChart:
        try:
            content = (
                render_bar_svg(chart) if chart.kind.value == "bar" else render_timeline_svg(chart)
            )
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
