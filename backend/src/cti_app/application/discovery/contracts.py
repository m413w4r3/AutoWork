from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator

from cti_app.application.jobs import JobParameters
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryRequestSnapshot, DiscoveryRun
from cti_app.domain.editions import Edition

SOURCE_PROFILE_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class DiscoverEditionParameters(JobParameters):
    edition_id: UUID
    country: str = Field(min_length=2, max_length=100)
    country_code: str = Field(min_length=2, max_length=16)
    discovery_run_id: UUID
    country_aliases: list[str] = Field(min_length=1, max_length=30)
    period_start: date
    period_end: date
    as_of_date: date = Field(default_factory=date.today)
    languages: list[str] = Field(min_length=1, max_length=10)
    source_profile: str = Field(
        min_length=1,
        max_length=128,
        pattern=SOURCE_PROFILE_PATTERN.pattern,
    )
    keywords: list[str] = Field(default_factory=list, max_length=100)
    exclusions: list[str] = Field(default_factory=list, max_length=100)
    complementary_axis: str = Field(default="initial", min_length=1, max_length=500)
    tlp: TLP
    sensitivity: str = Field(default="internal", min_length=1, max_length=64)
    external_llm_allowed: bool = True

    @field_validator("edition_id", "discovery_run_id", mode="before")
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


class ReprocessDiscoveryReportParameters(JobParameters):
    edition_id: UUID
    discovery_run_id: UUID
    research_model_run_id: UUID
    actor_id: str = Field(min_length=1, max_length=255)

    @field_validator("edition_id", "discovery_run_id", "research_model_run_id", mode="before")
    @classmethod
    def parse_uuid(cls, value: object) -> object:
        return UUID(value) if isinstance(value, str) and value else value


def discovery_request_hash(parameters: DiscoverEditionParameters) -> str:
    value = parameters.model_dump(mode="json")
    value.pop("discovery_run_id", None)
    for key in (
        "country",
        "country_code",
        "source_profile",
        "complementary_axis",
        "sensitivity",
    ):
        value[key] = value[key].strip()
    for key in ("country_aliases", "languages", "keywords", "exclusions"):
        cleaned = [item.strip() for item in value[key] if item.strip()]
        value[key] = (
            sorted({item.casefold() for item in cleaned})
            if key in {"country_aliases", "languages"}
            else sorted(dict.fromkeys(cleaned))
        )
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def discovery_request_snapshot(
    parameters: DiscoverEditionParameters,
) -> DiscoveryRequestSnapshot:
    return DiscoveryRequestSnapshot(
        country=parameters.country,
        country_code=parameters.country_code,
        country_aliases=tuple(parameters.country_aliases),
        period_start=parameters.period_start,
        period_end=parameters.period_end,
        as_of_date=parameters.as_of_date,
        languages=tuple(parameters.languages),
        source_profile=parameters.source_profile,
        keywords=tuple(parameters.keywords),
        exclusions=tuple(parameters.exclusions),
        complementary_axis=parameters.complementary_axis,
        tlp=parameters.tlp,
        sensitivity=parameters.sensitivity,
        external_llm_allowed=parameters.external_llm_allowed,
    )


def discover_parameters_from_edition(
    edition: Edition,
    *,
    discovery_run_id: UUID,
    source_profile: str,
    complementary_axis: str,
    sensitivity: str,
    external_llm_allowed: bool,
    country_aliases: list[str] | None = None,
    keywords: list[str] | None = None,
    exclusions: list[str] | None = None,
    as_of_date: date | None = None,
) -> DiscoverEditionParameters:
    aliases = list(dict.fromkeys([edition.country, edition.country_code, *(country_aliases or [])]))
    return DiscoverEditionParameters(
        edition_id=edition.id,
        discovery_run_id=discovery_run_id,
        country=edition.country,
        country_code=edition.country_code,
        country_aliases=aliases,
        period_start=edition.period_start,
        period_end=edition.period_end,
        as_of_date=as_of_date or date.today(),
        languages=list(edition.languages),
        source_profile=source_profile,
        keywords=keywords or [],
        exclusions=exclusions or [],
        complementary_axis=complementary_axis,
        tlp=edition.tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
    )


def discover_parameters_from_run(run: DiscoveryRun) -> DiscoverEditionParameters:
    snapshot = run.request_snapshot
    return DiscoverEditionParameters(
        edition_id=run.edition_id,
        discovery_run_id=run.id,
        country=snapshot.country,
        country_code=snapshot.country_code,
        country_aliases=list(snapshot.country_aliases),
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
        as_of_date=snapshot.as_of_date,
        languages=list(snapshot.languages),
        source_profile=run.source_profile,
        keywords=list(snapshot.keywords),
        exclusions=list(snapshot.exclusions),
        complementary_axis=run.complementary_axis,
        tlp=snapshot.tlp,
        sensitivity=snapshot.sensitivity,
        external_llm_allowed=snapshot.external_llm_allowed,
    )


def discovery_job_idempotency_key(discovery_run_id: UUID) -> str:
    return f"discover-run:{discovery_run_id}"


def discovery_research_model_run_id(discovery_run_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"cti-discovery-model-run:{discovery_run_id}")


def discovery_conversation_id(discovery_run_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"cti-discovery-conversation:{discovery_run_id}")


def discovery_initial_batch_id(discovery_run_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"cti-discovery-initial-batch:{discovery_run_id}")
