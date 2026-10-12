"""Deterministic projections from canonical Typst targets to laid out PDF slots."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from typing import Any

MAPPING_RENDERER_VERSION = "typst-block-location-v1"
_PAGE_TOP_MARGIN_POINTS = 65.197
_PAGE_BOTTOM_MARGIN_POINTS = 65.197

_CHANNELS: dict[str, tuple[str, tuple[str, ...]]] = {
    "article_title": ("synthesis", ("rewrite_title",)),
    "table": ("editorial_enrichment", ("rewrite_table", "delete_table")),
    "diagram": ("editorial_enrichment", ("rewrite_diagram", "delete_diagram")),
    "chart": ("editorial_enrichment", ("rewrite_chart", "delete_chart")),
    "source_figure": (
        "editorial_enrichment",
        (
            "rewrite_source_figure",
            "replace_source_figure",
            "delete_source_figure",
        ),
    ),
}


def mapping_cache_key(pdf_sha256: str, renderer_version: str, render_id: str) -> str:
    return f"pdf-mapping:{pdf_sha256}:{MAPPING_RENDERER_VERSION}:{renderer_version}:{render_id}"


def canonical_target_id(
    *,
    subject_id: str,
    artifact_id: str,
    artifact_version: int,
    artifact_input_hash: str,
    block_type: str,
    block_key: str,
) -> str:
    """Hash only immutable canonical identity fields, never layout or position."""
    payload = {
        "subject_id": subject_id,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "artifact_input_hash": artifact_input_hash,
        "block_type": block_type,
        "block_key": block_key,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def target_descriptor(
    *,
    subject_id: str,
    artifact_id: str,
    artifact_version: int,
    artifact_input_hash: str,
    block_type: str,
    block_key: str,
) -> dict[str, Any]:
    target_id = canonical_target_id(
        subject_id=subject_id,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_input_hash=artifact_input_hash,
        block_type=block_type,
        block_key=block_key,
    )
    target: dict[str, Any] = {
        "subject_id": subject_id,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "artifact_input_hash": artifact_input_hash,
        "block_type": block_type,
        "block_key": block_key,
    }
    channel = _CHANNELS.get(block_type)
    if channel is not None:
        family, operations = channel
        target["revision_channel"] = {
            "family": family,
            "operations": list(operations),
        }
    return {"target_id": target_id, "target": target}


def build_mapping_projection(
    *,
    pdf_sha256: str,
    requested_pdf_sha256: str,
    descriptors: list[dict[str, Any]],
    unmapped: list[dict[str, Any]],
    query_result: Any,
    page_sizes: Mapping[int, tuple[float, float]],
) -> dict[str, Any]:
    """Join verified query markers to canonical targets and conservative layout slots."""
    if pdf_sha256 != requested_pdf_sha256:
        return {
            "status": "unverified",
            "reason": "pdf_sha256_mismatch",
            "items": [],
            "unmapped": [],
        }

    markers: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(query_result, list):
        for row in query_result:
            if not isinstance(row, Mapping) or row.get("func") != "metadata":
                continue
            value = row.get("value")
            if not isinstance(value, Mapping):
                continue
            target_id = value.get("target_id")
            boundary = value.get("boundary")
            if isinstance(target_id, str) and boundary in {"location_start", "location_end"}:
                markers[(target_id, str(boundary))] = dict(value)

    items: list[dict[str, Any]] = []
    missing_by_type: Counter[str] = Counter()
    for descriptor in descriptors:
        if not isinstance(descriptor, Mapping):
            continue
        target_id = descriptor.get("target_id")
        target = descriptor.get("target")
        if not isinstance(target_id, str) or not isinstance(target, Mapping):
            continue
        start = markers.get((target_id, "location_start"))
        end = markers.get((target_id, "location_end"))
        locations = _locations(start, end, page_sizes) if start and end else []
        if not locations:
            missing_by_type[str(target.get("block_type", "unknown"))] += 1
            continue
        items.append(
            {
                "target_id": target_id,
                "target": dict(target),
                "locations": locations,
                "mapping_quality": "canonical_block_key",
            }
        )

    result_unmapped = [dict(item) for item in unmapped if isinstance(item, Mapping)]
    result_unmapped.extend(
        {"block_type": block_type, "reason": "layout_location_unavailable", "count": count}
        for block_type, count in sorted(missing_by_type.items())
    )
    return {
        "status": "partial" if items else "not_available",
        "reason": None if items else "layout_location_unavailable",
        "items": items,
        "unmapped": result_unmapped,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.endswith("pt"):
        try:
            return float(value[:-2])
        except ValueError:
            return None
    return None


def _locations(
    start: Mapping[str, Any],
    end: Mapping[str, Any],
    page_sizes: Mapping[int, tuple[float, float]],
) -> list[dict[str, Any]]:
    first_page = start.get("page")
    last_page = end.get("page")
    x = _number(start.get("x"))
    y = _number(start.get("y"))
    width = _number(start.get("width"))
    measured_height = _number(start.get("height"))
    end_y = _number(end.get("y"))
    if (
        not isinstance(first_page, int)
        or not isinstance(last_page, int)
        or first_page < 1
        or last_page < first_page
        or x is None
        or y is None
        or width is None
        or measured_height is None
        or end_y is None
        or width <= 0
        or measured_height <= 0
    ):
        return []

    locations: list[dict[str, Any]] = []
    for page in range(first_page, last_page + 1):
        size = page_sizes.get(page)
        if size is None:
            return []
        _, page_height = size
        if first_page == last_page:
            fragment_y = y
            fragment_height = max(measured_height, end_y - y)
        elif page == first_page:
            fragment_y = y
            fragment_height = max(0.0, page_height - _PAGE_BOTTOM_MARGIN_POINTS - y)
        elif page == last_page:
            fragment_y = _PAGE_TOP_MARGIN_POINTS
            fragment_height = max(0.0, end_y - fragment_y)
        else:
            fragment_y = _PAGE_TOP_MARGIN_POINTS
            fragment_height = max(
                0.0,
                page_height - _PAGE_TOP_MARGIN_POINTS - _PAGE_BOTTOM_MARGIN_POINTS,
            )
        if fragment_height <= 0:
            return []
        locations.append(
            {
                "page": page,
                "bbox_points": {
                    "x": round(x, 3),
                    "y": round(fragment_y, 3),
                    "width": round(width, 3),
                    "height": round(fragment_height, 3),
                },
            }
        )
    return locations
