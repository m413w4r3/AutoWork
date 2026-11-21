"""Stable figure key namespaces used by editorial publication contracts."""

from uuid import UUID

ADOPTED_SOURCE_FIGURE_KEY_PREFIX = "adopted_source_figure_"


def adopted_source_figure_key(source_figure_id: UUID) -> str:
    """Return the stable per-figure key used by manual adoption and publication."""
    return f"{ADOPTED_SOURCE_FIGURE_KEY_PREFIX}{source_figure_id.hex}"
