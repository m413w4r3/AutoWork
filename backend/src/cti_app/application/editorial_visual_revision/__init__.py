"""Shared, pure planner for typed revisions of existing enrichment elements.

Public surface: ``VisualRevisionContext``, ``VisualRevisionPlan``,
``VisualRevisionRejected`` and ``plan_visual_revision``. Prepare and adoption
both cross this seam; later slices add variant modules without widening the
interface.
"""

from .context import (
    VISUAL_REVISION_ERROR_CODES,
    VisualRevisionContext,
    VisualRevisionPlan,
    VisualRevisionRejected,
)
from .loader import VisualRevisionContextLoader, VisualRevisionPreparation
from .placement_ops import (
    enrichment_order_group_hash,
    find_element,
    replace_element,
    with_order_schema,
)
from .plan import plan_visual_revision

__all__ = [
    "VISUAL_REVISION_ERROR_CODES",
    "VisualRevisionContext",
    "VisualRevisionContextLoader",
    "VisualRevisionPlan",
    "VisualRevisionPreparation",
    "VisualRevisionRejected",
    "enrichment_order_group_hash",
    "find_element",
    "plan_visual_revision",
    "replace_element",
    "with_order_schema",
]
