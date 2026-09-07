"""Deterministic inputs used when Q4 revises an earlier synthesis."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

SYNTHESIS_REVISION_PROMPT_VERSION = "1"
# Keep the draft bounded independently from the provider's output limit.  The
# full current evidence pack remains the authority and must still fit beside it.
MAX_SYNTHESIS_REVISION_TEXT_BYTES = 2_000_000
MAX_SYNTHESIS_REVISION_CONTEXT_BYTES = 10_000_000


@dataclass(frozen=True, slots=True)
class SynthesisRevisionContext:
    """The non-authoritative prior draft and its deterministic semantic delta."""

    previous_artifact_id: UUID
    previous_input_hash: str
    previous_text: str
    added_source_ids: tuple[str, ...]
    removed_source_ids: tuple[str, ...]
    added_repair_keys: tuple[str, ...]
    removed_repair_keys: tuple[str, ...]
    previous_semantic_hash: str
    current_semantic_hash: str


def synthesis_content_hash(text: str) -> str:
    """Hash the exact previous draft content, without artifact identity."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def synthesis_semantic_source_ids(pack: Mapping[str, Any]) -> tuple[str, ...]:
    """Return source IDs that contribute narrative evidence to a Q4 pack.

    The projection deliberately ignores the report's unreferenced source list:
    a source that contributes only publication-only IOC/rule material must not
    appear in a narrative delta.
    """

    source_ids: set[str] = set()
    report = pack.get("reference_report")
    if isinstance(report, Mapping):
        events = report.get("events")
        if isinstance(events, list):
            for event in events:
                if isinstance(event, Mapping):
                    values = event.get("source_ids")
                    if isinstance(values, list | tuple):
                        source_ids.update(str(value) for value in values)
    extraction = pack.get("technical_extraction")
    if isinstance(extraction, Mapping):
        items = extraction.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    values = item.get("source_ids")
                    if isinstance(values, list | tuple):
                        source_ids.update(str(value) for value in values)
    return tuple(sorted(source_ids))


def narrative_repair_keys(extraction: Any, metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Return only repair keys represented in the narrative projection.

    Repair metadata contains all accepted decisions, including publication-only
    IOC and detection-rule decisions.  Matching the deterministic RPA local ID
    back to a contributing extraction item excludes both categories from the
    narrative delta.
    """

    if not isinstance(metadata, Mapping):
        return ()
    projection = metadata.get("repair_projection")
    if not isinstance(projection, Mapping):
        return ()
    values = projection.get("included_repair_keys")
    if not isinstance(values, list | tuple):
        return ()
    items = getattr(extraction, "items", ())
    keys: list[str] = []
    for value in values:
        key = str(value)
        marker = f"RPA-{key[:16]}"
        if any(
            getattr(item, "local_id", None) == marker
            and _item_contributes_to_synthesis(item)
            for item in items
        ):
            keys.append(key)
    return tuple(sorted(set(keys)))


def _item_contributes_to_synthesis(item: Any) -> bool:
    """Mirror the pure Q4 item gate without importing the repair module."""

    def value_of(value: Any) -> Any:
        return getattr(value, "value", value)

    if not getattr(item, "supported", False):
        return False
    if value_of(getattr(item, "indicator_status", None)) == "excluded":
        return False
    display_policy = value_of(getattr(item, "display_policy", None))
    if display_policy == "hidden":
        return False
    if (
        display_policy == "ioc_section"
        and not str(getattr(item, "context", "") or "").strip()
        and (
            value_of(getattr(item, "evidence_basis", None)) == "analyst_override"
            or value_of(getattr(item, "provenance", None)) == "analyst"
        )
    ):
        return False
    return True


def build_synthesis_revision_context(
    *,
    previous_artifact_id: UUID,
    previous_input_hash: str,
    previous_text: str,
    previous_semantic_hash: str,
    current_semantic_hash: str,
    previous_source_ids: tuple[str, ...] | list[str] | set[str],
    current_source_ids: tuple[str, ...] | list[str] | set[str],
    previous_repair_keys: tuple[str, ...] | list[str] | set[str] = (),
    current_repair_keys: tuple[str, ...] | list[str] | set[str] = (),
) -> SynthesisRevisionContext:
    """Build a stable, sorted before/after delta for the revision prompt."""

    previous_sources = {str(value) for value in previous_source_ids}
    current_sources = {str(value) for value in current_source_ids}
    previous_repairs = {str(value) for value in previous_repair_keys}
    current_repairs = {str(value) for value in current_repair_keys}
    return SynthesisRevisionContext(
        previous_artifact_id=previous_artifact_id,
        previous_input_hash=previous_input_hash,
        previous_text=previous_text,
        added_source_ids=tuple(sorted(current_sources - previous_sources)),
        removed_source_ids=tuple(sorted(previous_sources - current_sources)),
        added_repair_keys=tuple(sorted(current_repairs - previous_repairs)),
        removed_repair_keys=tuple(sorted(previous_repairs - current_repairs)),
        previous_semantic_hash=previous_semantic_hash,
        current_semantic_hash=current_semantic_hash,
    )


def revision_context_size_bytes(context: SynthesisRevisionContext) -> int:
    """Return the exact UTF-8 size of the draft plus its delta fields."""

    payload = {
        "previous_text": context.previous_text,
        "added_source_ids": context.added_source_ids,
        "removed_source_ids": context.removed_source_ids,
        "added_repair_keys": context.added_repair_keys,
        "removed_repair_keys": context.removed_repair_keys,
    }
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
