"""Small deterministic French typography helpers."""

from __future__ import annotations

import re
from datetime import date

_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def apply_french_spacing(text: str) -> str:
    """Use non-breaking spaces around French double punctuation and guillemets."""
    text = re.sub(r"[ \u00a0]*([:;!?])", "\u00a0\\1", text)
    text = re.sub(r"«[ \u00a0]*", "«\u00a0", text)
    return re.sub(r"[ \u00a0]*»", "\u00a0»", text)


def format_french_date(value: date) -> str:
    day = "1^er^" if value.day == 1 else str(value.day)
    return f"{day} {_MONTHS[value.month - 1]} {value.year}"
