"""Infrastructure-only primitives shared across repository modules.

Ownership rule (RF-P3 / R08b): this module holds *only* generic
infrastructure primitives that carry no business/domain knowledge — the
kind of helper any bounded context could need when mapping between JSON
payloads / ORM rows and domain objects:

- generic coercion (e.g. ``object`` -> ``UUID``);
- generic datetime/UUID (de)serialization helpers;
- generic, business-agnostic serialization utilities.

It must NOT contain business-owned helpers — domain payload/value/row
serializers (e.g. the Discovery ``CandidateTopic`` payload builders, the
Collection row mappers, business enums, or any domain-specific
conversion) stay in the repository module that owns that bounded context,
even if a helper's *shape* looks generic. Ownership here is decided by
actual cross-context reuse, not by the mere absence of a domain type in a
signature: see the note on ``string_tuple`` below.

Do not duplicate one of these primitives in a domain module — import it
from here instead. If a helper you need is business-specific, is not
already here, and has no documented owner yet, stop and pick an explicit
owner module rather than copy-pasting logic.

Considered and deliberately left out of this module: ``_string_tuple``
(``object`` -> ``tuple[str, ...]``, currently defined next to the
Discovery payload helpers). Its signature carries no domain type, but as
of R08b every one of its callers belongs to the future ``discovery.py``
(R14) module — it is not reused across bounded contexts today, so per the
ownership rule above ("ne contient que des primitives ... réutilisées par
plusieurs bounded contexts") it stays owned by ``discovery.py``. Promote
it here if a second, unrelated bounded context ever needs the same
coercion.
"""

from datetime import date, datetime
from uuid import UUID


def coerce_uuid(value: object) -> UUID:
    """Coerce a serialized identifier (``str`` or ``UUID``) to ``UUID``."""
    return value if isinstance(value, UUID) else UUID(str(value))


def coerce_optional_uuid(value: object | None) -> UUID | None:
    """Same as :func:`coerce_uuid`, passing ``None`` through unchanged."""
    return None if value is None else coerce_uuid(value)


def isoformat_or_none(value: datetime | date | None) -> str | None:
    """Serialize a ``datetime``/``date`` to ISO 8601, or ``None`` through."""
    return None if value is None else value.isoformat()


def parse_datetime_or_none(value: object | None) -> datetime | None:
    """Parse an ISO 8601 string to ``datetime``, or ``None`` through."""
    return None if value is None else datetime.fromisoformat(str(value))


def parse_date_or_none(value: object | None) -> date | None:
    """Parse an ISO 8601 string to ``date``, or ``None`` through."""
    return None if value is None else date.fromisoformat(str(value))
