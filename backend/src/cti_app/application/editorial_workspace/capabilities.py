"""Resolve the editorial snapshot capabilities from live availability.

A capability is a promise to the operator: the action must be executable by the
mounted application. Every dependency is therefore explicit and the resolution
is fail-closed — a missing route, worker, accept writer or actionable block
turns the action off instead of rendering a dead control.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Manual insertions are exposed per block by the workspace registry only when the
# pinned synthesis/enrichment artifacts can really prepare them. The figure
# catalog availability is already encoded by `create_source_figure` presence.
INSERTION_OPERATIONS: frozenset[str] = frozenset(
    {
        "create_table",
        "create_bar_chart",
        "create_source_figure",
    }
)

_MANUAL_EDIT_OPERATION_BY_KIND: Mapping[str, str] = {
    "title": "rewrite_title",
    "lead_paragraph": "rewrite_lead",
    "paragraph": "rewrite_paragraph",
}

_INSERTION_ANCHOR_KINDS = frozenset({"paragraph", "lead_paragraph"})


@dataclass(frozen=True, slots=True)
class EditorialCapabilityFacts:
    """Availability facts collected from the snapshot and the mounted app.

    Wiring facts default to false: an application service alone cannot know
    whether the HTTP route, the Bridge worker or the accept writer exist.
    """

    publication_kind_known: bool
    production_run_known: bool
    read_only: bool
    discussion_routes_wired: bool = False
    discussion_runtime_wired: bool = False
    revision_preview_wired: bool = False
    proposal_path_wired: bool = False
    manual_edit_anchor_ready: bool = False
    insertion_anchor_ready: bool = False


@dataclass(frozen=True, slots=True)
class EditorialCapabilities:
    can_discuss: bool
    can_preview_revision: bool
    manual_edit: bool
    can_add_enrichment: bool


def resolve_editorial_capabilities(
    facts: EditorialCapabilityFacts,
) -> EditorialCapabilities:
    """Return the four mutating capabilities for one snapshot."""
    writable = facts.publication_kind_known and facts.production_run_known and not facts.read_only
    return EditorialCapabilities(
        can_discuss=(writable and facts.discussion_routes_wired and facts.discussion_runtime_wired),
        can_preview_revision=(
            writable
            and facts.discussion_routes_wired
            and facts.discussion_runtime_wired
            and facts.revision_preview_wired
        ),
        manual_edit=(writable and facts.proposal_path_wired and facts.manual_edit_anchor_ready),
        can_add_enrichment=(
            writable and facts.proposal_path_wired and facts.insertion_anchor_ready
        ),
    )


def _allowed_operations(block: Mapping[str, object]) -> Sequence[str]:
    allowed = block.get("allowed_operations")
    if not isinstance(allowed, list):
        return ()
    return tuple(item for item in allowed if isinstance(item, str))


def manual_edit_anchor_ready(blocks: Sequence[Mapping[str, object]]) -> bool:
    """Return whether one block exposes a manual text operation the route accepts."""
    for block in blocks:
        kind = block.get("kind")
        operation = _MANUAL_EDIT_OPERATION_BY_KIND.get(kind) if isinstance(kind, str) else None
        if (
            operation is not None
            and isinstance(block.get("text"), str)
            and block.get("before_hash") is not None
            and block.get("revision_status") == "ready"
            and operation in _allowed_operations(block)
        ):
            return True
    return False


def insertion_anchor_ready(blocks: Sequence[Mapping[str, object]]) -> bool:
    """Return whether one block can anchor a wired manual insertion route."""
    for block in blocks:
        if (
            block.get("kind") in _INSERTION_ANCHOR_KINDS
            and block.get("before_hash") is not None
            and block.get("revision_status") == "ready"
            and INSERTION_OPERATIONS.intersection(_allowed_operations(block))
        ):
            return True
    return False
