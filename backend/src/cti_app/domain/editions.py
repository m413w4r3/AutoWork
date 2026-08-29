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
SOURCE_PROFILE_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class EditionStatus(StrEnum):
    DRAFT = "draft"
    DISCOVERY = "discovery"
    SELECTION = "selection"
    PRODUCTION = "production"
    REVIEW = "review"
    ASSEMBLING = "assembling"
    PUBLISHED = "published"
    ARCHIVED = "archived"


EDITION_TRANSITIONS: dict[EditionStatus, tuple[EditionStatus, ...]] = {
    EditionStatus.DRAFT: (EditionStatus.DISCOVERY, EditionStatus.ARCHIVED),
    EditionStatus.DISCOVERY: (EditionStatus.SELECTION, EditionStatus.ARCHIVED),
    EditionStatus.SELECTION: (EditionStatus.PRODUCTION, EditionStatus.ARCHIVED),
    EditionStatus.PRODUCTION: (EditionStatus.REVIEW, EditionStatus.ARCHIVED),
    EditionStatus.REVIEW: (
        EditionStatus.PRODUCTION,
        EditionStatus.ASSEMBLING,
        EditionStatus.ARCHIVED,
    ),
    EditionStatus.ASSEMBLING: (
        EditionStatus.REVIEW,
        EditionStatus.PUBLISHED,
    ),
    EditionStatus.PUBLISHED: (EditionStatus.ARCHIVED,),
    EditionStatus.ARCHIVED: (),
}

EDITION_PROGRESS: dict[EditionStatus, int] = {
    EditionStatus.DRAFT: 0,
    EditionStatus.DISCOVERY: 15,
    EditionStatus.SELECTION: 30,
    EditionStatus.PRODUCTION: 55,
    EditionStatus.REVIEW: 75,
    EditionStatus.ASSEMBLING: 90,
    EditionStatus.PUBLISHED: 100,
    EditionStatus.ARCHIVED: 100,
}


class InvalidEditionTransitionError(ValueError):
    pass


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
    source_profile: str
    target_articles: int
    previous_edition_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    status: EditionStatus = EditionStatus.DRAFT
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        self.country = self.country.strip()
        self.country_code = self.country_code.strip().upper()
        self.languages = tuple(dict.fromkeys(self.languages))
        self.source_profile = self.source_profile.strip()
        self._validate()

    @property
    def allowed_transitions(self) -> tuple[EditionStatus, ...]:
        return EDITION_TRANSITIONS[self.status]

    @property
    def progress_percent(self) -> int:
        return EDITION_PROGRESS[self.status]

    def update_metadata(
        self,
        *,
        country: str,
        country_code: str,
        period_start: date,
        period_end: date,
        tlp: TLP,
        languages: tuple[str, ...],
        target_articles: int,
        previous_edition_id: UUID | None,
        source_profile: str,
        now: datetime | None = None,
    ) -> None:
        if self.status in {
            EditionStatus.ASSEMBLING,
            EditionStatus.PUBLISHED,
            EditionStatus.ARCHIVED,
        }:
            raise EditionImmutableError(
                "Assembling, published or archived editions cannot be modified"
            )
        ensure_tlp_not_downgraded(self.tlp, tlp)
        self.country = country.strip()
        self.country_code = country_code.strip().upper()
        self.period_start = period_start
        self.period_end = period_end
        self.tlp = tlp
        self.languages = tuple(dict.fromkeys(languages))
        self.target_articles = target_articles
        self.previous_edition_id = previous_edition_id
        self.source_profile = source_profile.strip()
        self._validate()
        self._bump(now)

    def transition(self, target: EditionStatus, now: datetime | None = None) -> None:
        if target not in self.allowed_transitions:
            raise InvalidEditionTransitionError(
                f"Transition from {self.status.value} to {target.value} is not allowed"
            )
        self.status = target
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
            "target_articles": self.target_articles,
            "previous_edition_id": (
                str(self.previous_edition_id) if self.previous_edition_id else None
            ),
            "source_profile": self.source_profile,
            "status": self.status.value,
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
        if not 0 <= self.target_articles <= 120:
            raise ValueError("Target articles must be between 0 and 120")
        if not SOURCE_PROFILE_PATTERN.fullmatch(self.source_profile):
            raise ValueError("Invalid source profile")
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
