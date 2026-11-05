"""Helpers for keeping wire-format control lines out of publication text."""

from __future__ import annotations

import re

_WIRE_MARKER_LINE = re.compile(r"^\s*@@\s*[^@\r\n]*?\s*@@\s*$")


def contains_wire_marker_line(value: str) -> bool:
    """Return whether text contains a whole-line ``@@...@@`` wire marker."""
    return any(_WIRE_MARKER_LINE.fullmatch(line) is not None for line in value.splitlines())
