"""Shared plain-text and markup guard for editorially entered display text."""

from __future__ import annotations

import re
import unicodedata

from cti_app.application.production_synthesis import _plain_text_violation

_EDITORIAL_UNSAFE_MARKUP = re.compile(
    r"(?m)(?<![\w-])#\s*(?:link|raw|import|read|include)\b"
    r"|<\s*/?\s*[A-Za-z][A-Za-z0-9_.:-]*(?=\s|/?>)"
    r"|https?://|www\.",
    re.IGNORECASE,
)


def editorial_patch_text_violation(value: str, *, allow_newline_tab: bool = False) -> bool:
    """Reject markup, URLs, controls, and non-plain display text."""
    allowed_controls = {"\n", "\t"} if allow_newline_tab else set()
    return (
        _plain_text_violation(value) is not None
        or _EDITORIAL_UNSAFE_MARKUP.search(value) is not None
        or any(
            char not in allowed_controls and unicodedata.category(char) in {"Cc", "Cf"}
            for char in value
        )
    )
