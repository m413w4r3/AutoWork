from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import UUID

from pydantic import Field, field_validator

from cti_app.application.jobs import JobParameters
from cti_app.domain.classification import TLP


class DiscoverEditionParameters(JobParameters):
    edition_id: UUID
    country: str = Field(min_length=2, max_length=100)
    country_aliases: list[str] = Field(min_length=1, max_length=30)
    period_start: date
    period_end: date
    as_of_date: date = Field(default_factory=date.today)
    languages: list[str] = Field(min_length=1, max_length=10)
    source_profile: str = Field(min_length=1, max_length=128)
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    tlp: TLP
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True
    research_nonce: UUID | None = None

    @field_validator("edition_id", "research_nonce", mode="before")
    @classmethod
    def parse_edition_id(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) and value else value

    @field_validator("period_start", "period_end", "as_of_date", mode="before")
    @classmethod
    def parse_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("tlp", mode="before")
    @classmethod
    def parse_tlp(cls, value: object) -> object:
        return TLP(value) if isinstance(value, str) else value


def discovery_request_hash(parameters: DiscoverEditionParameters) -> str:
    value = parameters.model_dump(mode="json")
    for key in ("country_aliases", "languages", "keywords", "exclusions"):
        cleaned = [item.strip() for item in value[key] if item.strip()]
        value[key] = (
            sorted({item.casefold() for item in cleaned})
            if key in {"country_aliases", "languages"}
            else sorted(dict.fromkeys(cleaned))
        )
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def discovery_idempotency_key(parameters: DiscoverEditionParameters) -> str:
    return f"discover-edition:{parameters.edition_id}:{discovery_request_hash(parameters)}"
