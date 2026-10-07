"""Approximate visible D2 SVG text and node bounds for layout regression tests."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from xml.etree import ElementTree

_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_PATH_TOKEN = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[A-Za-z]")
_TRANSFORM = re.compile(r"([A-Za-z]+)\(([^)]*)\)")
_PATH_ARITY = {"M": 2, "L": 2, "T": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "A": 7}


@dataclass(frozen=True, slots=True)
class _Box:
    left: float
    top: float
    right: float
    bottom: float

    def overlaps(self, other: _Box, *, tolerance: float = 0.5) -> bool:
        return (
            min(self.right, other.right) - max(self.left, other.left) > tolerance
            and min(self.bottom, other.bottom) - max(self.top, other.top) > tolerance
        )


@dataclass(frozen=True, slots=True)
class _TextBox:
    label: str
    role: str
    bounds: _Box


@dataclass(frozen=True, slots=True)
class SvgOverlapReport:
    """Collision counts and label pairs found by a documented text-width estimate."""

    text_overlap_pairs: tuple[tuple[str, str], ...]
    edge_node_overlap_pairs: tuple[tuple[str, str], ...]

    @property
    def text_overlap_count(self) -> int:
        return len(self.text_overlap_pairs)

    @property
    def edge_node_overlap_count(self) -> int:
        return len(self.edge_node_overlap_pairs)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _class_tokens(element: ElementTree.Element) -> set[str]:
    return set(element.attrib.get("class", "").split())


def _number(value: str | None, default: float = 0.0) -> float:
    if value is None:
        return default
    match = _NUMBER.search(value)
    return float(match.group()) if match else default


def _first_number(value: str | None, default: float = 0.0) -> float:
    return _number(value, default)


def _style_values(element: ElementTree.Element) -> dict[str, str]:
    values = {
        key: value for key, value in element.attrib.items() if key in {"font-size", "text-anchor"}
    }
    for declaration in element.attrib.get("style", "").split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            values[name.strip()] = value.strip()
    return values


def _apply_transform(element: ElementTree.Element, x: float, y: float) -> tuple[float, float]:
    """Apply the common SVG translate/scale/matrix forms emitted by D2."""
    for name, raw_values in _TRANSFORM.findall(element.attrib.get("transform", "")):
        values = [float(value) for value in _NUMBER.findall(raw_values)]
        normalized = name.lower()
        if normalized == "translate" and values:
            x += values[0]
            y += values[1] if len(values) > 1 else 0.0
        elif normalized == "scale" and values:
            x *= values[0]
            y *= values[1] if len(values) > 1 else values[0]
        elif normalized == "matrix" and len(values) == 6:
            a, b, c, d, e, f = values
            x, y = a * x + c * y + e, b * x + d * y + f
        # D2's pipeline output does not rotate labels or nodes. Preserve the
        # coordinates for any unknown transform instead of inventing bounds.
    return x, y


def _transformed_box(box: _Box, ancestors: tuple[ElementTree.Element, ...]) -> _Box:
    points = (
        (box.left, box.top),
        (box.left, box.bottom),
        (box.right, box.top),
        (box.right, box.bottom),
    )
    transformed = []
    for point in points:
        x, y = point
        for ancestor in ancestors:
            x, y = _apply_transform(ancestor, x, y)
        transformed.append((x, y))
    return _Box(
        min(x for x, _ in transformed),
        min(y for _, y in transformed),
        max(x for x, _ in transformed),
        max(y for _, y in transformed),
    )


def _ancestor_chain(
    element: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
) -> tuple[ElementTree.Element, ...]:
    chain: list[ElementTree.Element] = []
    current = parents.get(element)
    while current is not None:
        chain.append(current)
        current = parents.get(current)
    return tuple(reversed(chain))


def _union(boxes: list[_Box]) -> _Box | None:
    if not boxes:
        return None
    return _Box(
        min(box.left for box in boxes),
        min(box.top for box in boxes),
        max(box.right for box in boxes),
        max(box.bottom for box in boxes),
    )


def _path_points(path: str) -> list[tuple[float, float]]:
    """Read D2's M/L/H/V/C/Q/S/A path coordinates with control points included."""
    tokens = _PATH_TOKEN.findall(path)
    points: list[tuple[float, float]] = []
    index = 0
    command: str | None = None
    x = y = start_x = start_y = 0.0
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command.upper() == "Z":
                x, y = start_x, start_y
                points.append((x, y))
                command = None
                continue
        if command is None:
            continue
        upper = command.upper()
        arity = _PATH_ARITY.get(upper)
        if arity is None or index + arity > len(tokens) or tokens[index].isalpha():
            command = None
            continue
        raw = [float(value) for value in tokens[index : index + arity]]
        index += arity
        relative = command.islower()
        if upper in {"M", "L", "T"}:
            next_x, next_y = raw
            if relative:
                next_x, next_y = x + next_x, y + next_y
            x, y = next_x, next_y
            if upper == "M":
                start_x, start_y = x, y
                command = "l" if relative else "L"
            points.append((x, y))
        elif upper == "H":
            x = x + raw[0] if relative else raw[0]
            points.append((x, y))
        elif upper == "V":
            y = y + raw[0] if relative else raw[0]
            points.append((x, y))
        elif upper in {"C", "S", "Q"}:
            coordinate_pairs = list(zip(raw[::2], raw[1::2], strict=True))
            if relative:
                coordinate_pairs = [(x + px, y + py) for px, py in coordinate_pairs]
            points.extend(coordinate_pairs)
            x, y = coordinate_pairs[-1]
        elif upper == "A":
            end_x, end_y = raw[-2:]
            if relative:
                end_x, end_y = x + end_x, y + end_y
            radius_x, radius_y = abs(raw[0]), abs(raw[1])
            points.extend(
                (
                    (x - radius_x, y - radius_y),
                    (x + radius_x, y + radius_y),
                    (end_x - radius_x, end_y - radius_y),
                    (end_x + radius_x, end_y + radius_y),
                    (end_x, end_y),
                )
            )
            x, y = end_x, end_y
    return points


def _geometry_box(element: ElementTree.Element) -> _Box | None:
    name = _local_name(element.tag)
    if name == "rect":
        x, y = _number(element.get("x")), _number(element.get("y"))
        width, height = _number(element.get("width")), _number(element.get("height"))
        box = _Box(x, y, x + width, y + height)
    elif name in {"ellipse", "circle"}:
        cx, cy = _number(element.get("cx")), _number(element.get("cy"))
        rx = _number(element.get("rx")) if name == "ellipse" else _number(element.get("r"))
        ry = _number(element.get("ry")) if name == "ellipse" else rx
        box = _Box(cx - rx, cy - ry, cx + rx, cy + ry)
    elif name in {"polygon", "polyline"}:
        numbers = [float(value) for value in _NUMBER.findall(element.get("points", ""))]
        points = list(zip(numbers[::2], numbers[1::2], strict=False))
        box = _union([_Box(x, y, x, y) for x, y in points])
    elif name == "path":
        points = _path_points(element.get("d", ""))
        box = _union([_Box(x, y, x, y) for x, y in points])
    else:
        return None
    if box is None:
        return None
    # Include the two-pixel D2 stroke around a node's fill geometry.
    return _Box(box.left - 1, box.top - 1, box.right + 1, box.bottom + 1)


def _glyph_width(character: str, font_size: float) -> float:
    if character.isspace():
        return font_size * 0.34
    if unicodedata.east_asian_width(character) in {"F", "W"}:
        return font_size
    if character in "ilI.,'!:;|`":
        return font_size * 0.34
    if character in "mwMW@%&":
        return font_size * 0.82
    if character.isupper():
        return font_size * 0.66
    return font_size * 0.57


def _text_box(
    element: ElementTree.Element,
    role: str,
    ancestors: tuple[ElementTree.Element, ...],
) -> _TextBox | None:
    style = _style_values(element)
    font_size = _number(style.get("font-size"), 16.0)
    anchor = style.get("text-anchor", "start").lower()
    text_x = _first_number(element.get("x"))
    text_y = _first_number(element.get("y"))
    spans = [child for child in element if _local_name(child.tag) == "tspan"]
    lines: list[tuple[str, float, float]] = []
    if spans:
        baseline = text_y
        for span_index, span in enumerate(spans):
            content = "".join(span.itertext())
            if span.get("x") is not None:
                text_x = _first_number(span.get("x"), text_x)
            if span.get("y") is not None:
                baseline = _first_number(span.get("y"), baseline)
            elif span.get("dy") is not None:
                baseline += _number(span.get("dy"))
            elif span_index:
                baseline += font_size * 1.2
            if content:
                lines.append((content, text_x, baseline))
    else:
        content = "".join(element.itertext())
        for line_index, line in enumerate(content.splitlines() or [content]):
            if line:
                lines.append((line, text_x, text_y + font_size * 1.2 * line_index))
    if not lines:
        return None

    boxes: list[_Box] = []
    for line, x, baseline in lines:
        width = sum(_glyph_width(character, font_size) for character in line)
        left = x - width if anchor == "end" else x - width / 2 if anchor == "middle" else x
        boxes.append(
            _Box(
                left,
                baseline - font_size * 0.85,
                left + width,
                baseline + font_size * 0.25,
            )
        )
    bounds = _union(boxes)
    if bounds is None:
        return None
    bounds = _transformed_box(bounds, ancestors)
    return _TextBox(" ".join(line for line, _, _ in lines).strip(), role, bounds)


def measure_svg_overlaps(svg_bytes: bytes) -> SvgOverlapReport:
    """Approximate text extents and count text/text and edge-label/node-shape overlaps.

    D2 emits one SVG ``text`` element per label. Since it does not include glyph
    outline bounds, each line is estimated from its font size: spaces use 0.34 em,
    narrow letters 0.34 em, wide letters 0.82 em, other capitals 0.66 em, other
    glyphs 0.57 em, and East Asian wide glyphs 1 em. Baselines in separate tspans
    are spaced by 1.2 em. Text bounds use an 0.85 em ascent and 0.25 em descent.
    Node bounds come from the geometry inside each D2 ``g.shape`` group, with its
    stroke added. This intentionally favors simple, repeatable estimates over a
    font-rendering dependency; touching boxes within 0.5 SVG units are not counted.
    """
    root = ElementTree.fromstring(svg_bytes)
    parents = {child: parent for parent in root.iter() for child in parent}
    node_shapes: list[tuple[str, _Box]] = []
    node_groups: set[ElementTree.Element] = set()
    edge_groups: set[ElementTree.Element] = set()

    for element in root.iter():
        if _local_name(element.tag) != "g":
            continue
        children = list(element)
        if any(
            _local_name(child.tag) == "g" and "shape" in _class_tokens(child) for child in children
        ):
            node_groups.add(element)
        if any(
            _local_name(child.tag) == "path" and "connection" in _class_tokens(child)
            for child in children
        ):
            edge_groups.add(element)

    element_order = {element: index for index, element in enumerate(root.iter())}
    for node_index, group in enumerate(sorted(node_groups, key=element_order.__getitem__)):
        owner = f"node {node_index + 1}"
        shape_groups = [
            item
            for item in group.iter()
            if _local_name(item.tag) == "g" and "shape" in _class_tokens(item)
        ]
        geometry: list[_Box] = []
        for shape_group in shape_groups:
            shape_ancestors = _ancestor_chain(shape_group, parents)
            for item in shape_group.iter():
                bounds = _geometry_box(item)
                if bounds is not None:
                    geometry.append(_transformed_box(bounds, shape_ancestors))
        merged = _union(geometry)
        if merged is not None:
            node_shapes.append((owner, merged))

    text_boxes: list[_TextBox] = []
    for element in root.iter():
        if _local_name(element.tag) != "text":
            continue
        ancestors = _ancestor_chain(element, parents)
        owner = next((item for item in reversed(ancestors) if item in edge_groups), None)
        role = "edge" if owner is not None else "node"
        if owner is None:
            owner = next((item for item in reversed(ancestors) if item in node_groups), None)
            if owner is None:
                role = "other"
        box = _text_box(element, role, ancestors)
        if box is not None:
            text_boxes.append(box)

    text_overlaps = tuple(
        (left.label, right.label)
        for index, left in enumerate(text_boxes)
        for right in text_boxes[index + 1 :]
        if left.bounds.overlaps(right.bounds)
    )
    edge_node_overlaps = tuple(
        (text.label, node_label)
        for text in text_boxes
        if text.role == "edge"
        for node_label, node_bounds in node_shapes
        if text.bounds.overlaps(node_bounds)
    )
    return SvgOverlapReport(text_overlaps, edge_node_overlaps)


def estimate_printed_font_sizes(
    svg_bytes: bytes,
    *,
    printable_width_pt: float = 450.0,
    printable_height_pt: float = 660.0,
    max_height_fraction: float = 0.55,
) -> tuple[float, float]:
    """Estimate minimum node/edge font sizes after the publication image is fitted.

    The fit mirrors Typst's publication helper: SVG CSS pixels convert at 0.75 pt,
    then the image is scaled to the smaller of printable width and 55% of the
    available height. The page area defaults are deliberately tighter than A4's
    margin-only dimensions to leave room for its header and footer.
    """
    root = ElementTree.fromstring(svg_bytes)
    view_box = [_number(value) for value in re.split(r"[ ,]+", root.get("viewBox", "").strip())]
    if len(view_box) != 4 or view_box[2] <= 0 or view_box[3] <= 0:
        raise ValueError("D2 SVG must declare a positive four-value viewBox")
    scale = min(
        1.0,
        printable_width_pt / (view_box[2] * 0.75),
        printable_height_pt * max_height_fraction / (view_box[3] * 0.75),
    )

    parents = {child: parent for parent in root.iter() for child in parent}
    node_groups: set[ElementTree.Element] = set()
    edge_groups: set[ElementTree.Element] = set()
    for element in root.iter():
        if _local_name(element.tag) != "g":
            continue
        children = list(element)
        if any(
            _local_name(child.tag) == "g" and "shape" in _class_tokens(child) for child in children
        ):
            node_groups.add(element)
        if any(
            _local_name(child.tag) == "path" and "connection" in _class_tokens(child)
            for child in children
        ):
            edge_groups.add(element)

    node_sizes: list[float] = []
    edge_sizes: list[float] = []
    for element in root.iter():
        if _local_name(element.tag) != "text":
            continue
        ancestors = _ancestor_chain(element, parents)
        if any(item in edge_groups for item in reversed(ancestors)):
            edge_sizes.append(_number(_style_values(element).get("font-size"), 16.0))
        elif any(item in node_groups for item in reversed(ancestors)):
            node_sizes.append(_number(_style_values(element).get("font-size"), 16.0))
    if not node_sizes or not edge_sizes:
        raise ValueError("D2 SVG must contain node and edge labels")
    return min(node_sizes) * 0.75 * scale, min(edge_sizes) * 0.75 * scale
