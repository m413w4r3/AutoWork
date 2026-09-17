"""Process-wide allocation of edition country codes for integration tests.

The integration PostgreSQL database is session-scoped and shared by every
test, while editions are unique on ``(country_code, period_start,
period_end)`` and virtually all scenarios reuse the same monthly period.
Codes must therefore be allocated from one place instead of being picked
independently -- and deterministically, so a collision is never a coin flip
that fails a different test on every run.

Modules that hardcode a readable code (``FR``, ``IR``, ...) declare it in
:data:`RESERVED_CODES`; the allocator never hands those out.
"""

from __future__ import annotations

from itertools import product
from string import ascii_uppercase

#: Codes written literally by integration test modules. Keep in sync when a
#: test introduces a new hardcoded code.
RESERVED_CODES: frozenset[str] = frozenset(
    {
        "CI",
        "DB",
        "DE",
        "FR",
        "FT",
        "IQ",
        "IR",
        "IT",
        "IV",
        "RA",
        "RB",
        "RC",
        "RD",
        "RX",
        "ZZ",
    }
)

_AVAILABLE_CODES = iter(
    code
    for code in ("".join(pair) for pair in product(ascii_uppercase, repeat=2))
    if code not in RESERVED_CODES
)


def reserve_edition_code() -> str:
    """Return a country code not used by any other scenario in this process."""
    try:
        return next(_AVAILABLE_CODES)
    except StopIteration:  # pragma: no cover - 661 codes is far beyond the suite
        raise AssertionError(
            "Exhausted the integration edition country code space; "
            "scenarios must share a session-scoped database more sparingly."
        ) from None
