"""Small deterministic French typography helpers."""

from __future__ import annotations

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


def format_french_date(value: date) -> str:
    day = "1^er^" if value.day == 1 else str(value.day)
    return f"{day} {_MONTHS[value.month - 1]} {value.year}"


def format_french_month(value: date) -> str:
    """Return the month and year of ``value``, as printed on a bulletin cover."""
    return f"{_MONTHS[value.month - 1]} {value.year}"
