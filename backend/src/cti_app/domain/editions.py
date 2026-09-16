from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from cti_app.domain.classification import TLP, ensure_tlp_not_downgraded

COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")


class EditionStatus(StrEnum):
    OPEN = "open"
    ARCHIVED = "archived"


class EditionImmutableError(ValueError):
    pass


@dataclass(slots=True)
class Edition:
    country: str
    country_code: str
    period_start: date
    period_end: date
    tlp: TLP
    languages: tuple[str, ...]
    id: UUID = field(default_factory=uuid4)
    state: EditionStatus = EditionStatus.OPEN
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.country = self.country.strip()
        self.country_code = self.country_code.strip().upper()
        self.languages = tuple(dict.fromkeys(self.languages))
        self._validate()

    def update_metadata(
        self,
        *,
        country: str,
        country_code: str,
        period_start: date,
        period_end: date,
        tlp: TLP,
        languages: tuple[str, ...],
        now: datetime | None = None,
    ) -> None:
        if self.state is EditionStatus.ARCHIVED:
            raise EditionImmutableError("Archived editions cannot be modified")
        ensure_tlp_not_downgraded(self.tlp, tlp)
        self.country = country.strip()
        self.country_code = country_code.strip().upper()
        self.period_start = period_start
        self.period_end = period_end
        self.tlp = tlp
        self.languages = tuple(dict.fromkeys(languages))
        self._validate()
        self._bump(now)

    def archive(self, now: datetime | None = None) -> None:
        if self.state is EditionStatus.ARCHIVED:
            raise EditionImmutableError("Archived editions cannot be archived again")
        self.state = EditionStatus.ARCHIVED
        self._bump(now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "country": self.country,
            "country_code": self.country_code,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "tlp": self.tlp.value,
            "languages": list(self.languages),
            "state": self.state.value,
            "version": self.version,
        }

    def _bump(self, now: datetime | None) -> None:
        self.version += 1
        self.updated_at = now or datetime.now(UTC)

    def _validate(self) -> None:
        if not 2 <= len(self.country) <= 100:
            raise ValueError("Country must contain between 2 and 100 characters")
        if not COUNTRY_CODE_PATTERN.fullmatch(self.country_code):
            raise ValueError("Country code must be an ISO-like alpha-2 code")
        _validate_month_period(self.period_start, self.period_end)
        if not self.languages:
            raise ValueError("At least one language is required")
        if len(self.languages) > 10 or any(
            not LANGUAGE_PATTERN.fullmatch(language) for language in self.languages
        ):
            raise ValueError("Languages must be unique BCP47-like codes")
        if self.version < 1:
            raise ValueError("Edition version must be positive")


@dataclass(frozen=True, slots=True)
class EditionAuditEvent:
    edition_id: UUID
    actor_id: str
    action: str
    before: dict[str, Any] | None
    after: dict[str, Any]
    correlation_id: str
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _validate_month_period(period_start: date, period_end: date) -> None:
    if period_start > period_end:
        raise ValueError("Period start must be before period end")
    if (period_start.year, period_start.month) != (period_end.year, period_end.month):
        raise ValueError("A monthly edition must stay within one calendar month")
    expected_end = calendar.monthrange(period_start.year, period_start.month)[1]
    if period_start.day != 1 or period_end.day != expected_end:
        raise ValueError("A monthly edition must cover the complete calendar month")
