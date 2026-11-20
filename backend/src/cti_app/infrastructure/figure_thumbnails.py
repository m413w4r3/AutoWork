"""Safely prepare bounded responses for archived source figures."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import cast
from xml.etree import ElementTree

from cti_app.application.source_figure_inventory import MAX_SOURCE_FIGURE_BYTES
from cti_app.application.source_media_collection import (
    SOURCE_MEDIA_MAX_PIXELS,
    SOURCE_MEDIA_MAX_SIDE_LENGTH,
    image_dimensions,
    image_dimensions_exceed_limits,
)

MAX_FIGURE_THUMBNAIL_EDGE_PX = 320
MAX_FIGURE_PREVIEW_EDGE_PX = 1024
_FIGURE_CACHE_TTL_SECONDS = 30.0
_FIGURE_CACHE_MAX_ENTRIES = 64
_FIGURE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_RASTER_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_FIGURE_CONTENT_SECURITY_POLICY = "default-src 'none'"
_SVG_CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'none'; sandbox"
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_SVG_LOCAL_NAME = re.compile(r"\{[^}]*\}(.+)$")
_SVG_DIMENSION = re.compile(r"^([0-9]{1,5}(?:\.[0-9]{1,4})?)(?:px)?$")
_SVG_NUMBER = re.compile(r"^-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_SVG_PAINT = re.compile(r"^(?:none|transparent|black|white|red|green|blue|#[0-9a-fA-F]{3,8})$")
_SVG_PATH = re.compile(r"^[MmZzLlHhVvCcSsQqTtAa0-9.,+\-\sEe]*$")
_SVG_POINTS = re.compile(r"^[0-9.,+\-\sEe]*$")
_SVG_TRANSFORM = re.compile(r"^(?:(?:translate|scale|rotate|matrix)\([0-9eE+.,\-\s]{1,120}\)\s*)+$")
_SVG_FORBIDDEN_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class FigureThumbnailError(RuntimeError):
    """A source figure could not be rendered into a safe bounded image."""

    def __init__(self, code: str = "figure_thumbnail_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedFigureContent:
    content: bytes
    media_type: str
    content_security_policy: str


class FigureThumbnailCache:
    """Process-local LRU for verified display bytes, bounded by age and size."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: OrderedDict[tuple[str, int, str], tuple[float, PreparedFigureContent]] = (
            OrderedDict()
        )
        self._total_bytes = 0

    def get(self, sha256: str, requested_size: int, mime_type: str) -> PreparedFigureContent | None:
        key = (sha256, requested_size, mime_type)
        now = monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, prepared = entry
            if expires_at <= now:
                self._remove(key)
                return None
            self._entries.move_to_end(key)
            return prepared

    def put(
        self,
        sha256: str,
        requested_size: int,
        mime_type: str,
        prepared: PreparedFigureContent,
    ) -> None:
        if not prepared.content or len(prepared.content) > MAX_SOURCE_FIGURE_BYTES:
            return
        key = (sha256, requested_size, mime_type)
        with self._lock:
            self._remove(key)
            while self._entries and (
                len(self._entries) >= _FIGURE_CACHE_MAX_ENTRIES
                or self._total_bytes + len(prepared.content) > _FIGURE_CACHE_MAX_BYTES
            ):
                oldest = next(iter(self._entries))
                self._remove(oldest)
            if len(prepared.content) > _FIGURE_CACHE_MAX_BYTES:
                return
            self._entries[key] = (monotonic() + _FIGURE_CACHE_TTL_SECONDS, prepared)
            self._total_bytes += len(prepared.content)

    def _remove(self, key: tuple[str, int, str]) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._total_bytes -= len(entry[1].content)


figure_thumbnail_cache = FigureThumbnailCache()


def sanitize_svg(content: bytes) -> bytes:
    """Retain a small static SVG subset and drop scripts, links and references."""
    if not content or len(content) > MAX_SOURCE_FIGURE_BYTES:
        raise FigureThumbnailError("figure_svg_invalid")
    if _SVG_FORBIDDEN_DECLARATION.search(content):
        raise FigureThumbnailError("figure_svg_invalid")
    try:
        root = ElementTree.fromstring(content)
    except (ElementTree.ParseError, ValueError):
        raise FigureThumbnailError("figure_svg_invalid") from None
    if _local_name(root.tag) != "svg" or _namespace(root.tag) not in {"", _SVG_NAMESPACE}:
        raise FigureThumbnailError("figure_svg_invalid")

    width, height, view_box = _safe_svg_dimensions(root.attrib)
    clean_root = ElementTree.Element(
        f"{{{_SVG_NAMESPACE}}}svg",
        {"width": str(width), "height": str(height), "viewBox": view_box},
    )
    remaining_nodes = 5_000

    def copy_static(node: ElementTree.Element, parent: ElementTree.Element) -> None:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0:
            raise FigureThumbnailError("figure_svg_invalid")
        name = _local_name(node.tag)
        if name not in {"g", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path"}:
            return
        allowed_attributes = _safe_svg_attributes(name, node.attrib)
        clean_node = ElementTree.SubElement(
            parent, f"{{{_SVG_NAMESPACE}}}{name}", allowed_attributes
        )
        if name == "g":
            for child in node:
                copy_static(child, clean_node)

    for child in root:
        copy_static(child, clean_root)
    ElementTree.register_namespace("", _SVG_NAMESPACE)
    return cast(bytes, ElementTree.tostring(clean_root, encoding="utf-8", xml_declaration=False))


def prepare_figure_content(content: bytes, mime_type: str) -> PreparedFigureContent:
    """Keep verified raster bytes unchanged and sanitize SVG to a closed subset."""
    if not content or len(content) > MAX_SOURCE_FIGURE_BYTES:
        raise FigureThumbnailError()
    normalized_mime = mime_type.partition(";")[0].strip().casefold()
    if normalized_mime == "image/svg+xml":
        sanitized = sanitize_svg(content)
        if len(sanitized) > MAX_SOURCE_FIGURE_BYTES:
            raise FigureThumbnailError()
        return PreparedFigureContent(
            sanitized,
            "image/svg+xml; charset=utf-8",
            _SVG_CONTENT_SECURITY_POLICY,
        )
    if normalized_mime not in _RASTER_MEDIA_TYPES:
        raise FigureThumbnailError()
    width, height = image_dimensions(content, normalized_mime)
    if (
        width is None
        or height is None
        or image_dimensions_exceed_limits(
            width,
            height,
            maximum_pixels=SOURCE_MEDIA_MAX_PIXELS,
            maximum_side_length=SOURCE_MEDIA_MAX_SIDE_LENGTH,
        )
    ):
        raise FigureThumbnailError()
    return PreparedFigureContent(content, normalized_mime, _FIGURE_CONTENT_SECURITY_POLICY)


def _safe_svg_dimensions(attributes: dict[str, str]) -> tuple[int, int, str]:
    view_box = attributes.get("viewBox") or attributes.get("viewbox")
    parsed_view_box: tuple[float, float, float, float] | None = None
    if view_box is not None:
        parts = re.split(r"[\s,]+", view_box.strip())
        if len(parts) == 4 and all(_SVG_NUMBER.fullmatch(part) for part in parts):
            values = tuple(float(part) for part in parts)
            if all(abs(value) <= 100_000 for value in values) and values[2] > 0 and values[3] > 0:
                parsed_view_box = values  # type: ignore[assignment]
    width = _safe_dimension(attributes.get("width"))
    height = _safe_dimension(attributes.get("height"))
    if parsed_view_box is not None:
        width = width or max(1, min(10_000, round(parsed_view_box[2])))
        height = height or max(1, min(10_000, round(parsed_view_box[3])))
        safe_view_box = " ".join(_format_number(value) for value in parsed_view_box)
    else:
        width, height = width or 300, height or 150
        safe_view_box = f"0 0 {width} {height}"
    return min(width, 10_000), min(height, 10_000), safe_view_box


def _safe_dimension(value: str | None) -> int | None:
    if value is None:
        return None
    match = _SVG_DIMENSION.fullmatch(value.strip())
    if match is None:
        return None
    numeric = float(match.group(1))
    if not (1 <= numeric <= 10_000):
        return None
    return round(numeric)


def _safe_svg_attributes(name: str, attributes: dict[str, str]) -> dict[str, str]:
    common = {"fill", "stroke", "stroke-width", "opacity", "transform"}
    specific = {
        "g": set(),
        "rect": {"x", "y", "width", "height", "rx", "ry"},
        "circle": {"cx", "cy", "r"},
        "ellipse": {"cx", "cy", "rx", "ry"},
        "line": {"x1", "y1", "x2", "y2"},
        "polyline": {"points"},
        "polygon": {"points"},
        "path": {"d"},
    }[name]
    safe: dict[str, str] = {}
    for key, value in attributes.items():
        if key in common and key in {"fill", "stroke"} and _SVG_PAINT.fullmatch(value):
            safe[key] = value
        elif key == "transform" and _SVG_TRANSFORM.fullmatch(value):
            safe[key] = value
        elif key == "opacity" and _safe_unit_interval(value):
            safe[key] = value
        elif key == "stroke-width" and _safe_nonnegative(value):
            safe[key] = value
        elif key in specific and key in {"points", "d"}:
            matcher = _SVG_POINTS if key == "points" else _SVG_PATH
            if len(value) <= 50_000 and matcher.fullmatch(value):
                safe[key] = value
        elif key in specific and _safe_numeric_attribute(value):
            safe[key] = value
    return safe


def _safe_numeric_attribute(value: str) -> bool:
    return (
        len(value) <= 32
        and _SVG_NUMBER.fullmatch(value) is not None
        and abs(float(value)) <= 100_000
    )


def _safe_nonnegative(value: str) -> bool:
    return _safe_numeric_attribute(value) and float(value) >= 0


def _safe_unit_interval(value: str) -> bool:
    return _safe_numeric_attribute(value) and 0 <= float(value) <= 1


def _format_number(value: float) -> str:
    return str(round(value, 4))


def _local_name(tag: str) -> str:
    match = _SVG_LOCAL_NAME.fullmatch(tag)
    return match.group(1) if match is not None else tag


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""
