"""Seed-only VirusTotal enrichment for analyst investigations.

This service performs read-only report lookups.  It never downloads a file
and never invokes the SUBMISSIONS capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalError,
    VirusTotalHttpError,
    VirusTotalPort,
    normalize_file_hash,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.domain.virustotal import VirusTotalCapability, VirusTotalFileView


class SeedEnrichmentIssue(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN_TO_VT = "unknown_to_vt"
    REPORT_WITHOUT_BYTES = "report_without_bytes"
    UNAVAILABLE = "unavailable"
    LOOKUP_FORBIDDEN = "lookup_forbidden"


@dataclass(frozen=True, slots=True)
class SeedEnrichmentResult:
    issue: SeedEnrichmentIssue | None = None
    observation_id: UUID | None = None
    features: dict[str, str] | None = None


def sample_features_from_vt_view(view: VirusTotalFileView) -> dict[str, str]:
    return {
        field: value
        for field, value in {
            "vhash": view.vhash,
            "imphash": view.imphash,
            "ssdeep": view.ssdeep,
            "tlsh": view.tlsh,
            "main_icon_dhash": view.main_icon_dhash,
            "rich_header_hash": view.rich_header_hash,
        }.items()
        if value
    }


class VirusTotalSeedEnrichmentService:
    def __init__(
        self,
        port: VirusTotalPort,
        observations: VirusTotalObservationService,
        capabilities: VirusTotalCapabilities,
    ) -> None:
        self._port = port
        self._observations = observations
        self._capabilities = capabilities

    async def enrich(
        self,
        file_hash: str | None,
        *,
        subject_id: UUID,
        external_lookup_allowed: bool,
        has_bytes: bool = True,
        observed_at: datetime | None = None,
    ) -> SeedEnrichmentResult:
        if not external_lookup_allowed or not self._capabilities.is_enabled(
            VirusTotalCapability.FILE_REPORT
        ):
            return SeedEnrichmentResult(issue=SeedEnrichmentIssue.LOOKUP_FORBIDDEN)
        if not file_hash:
            return SeedEnrichmentResult(issue=SeedEnrichmentIssue.NOT_APPLICABLE)
        try:
            normalized = normalize_file_hash(file_hash)
        except VirusTotalError:
            return SeedEnrichmentResult(issue=SeedEnrichmentIssue.NOT_APPLICABLE)

        # This is deliberately the first and required VT call.  Relationship
        # expansion is left to a later explicit analyst step/capability.
        try:
            report = await self._port.file_report(normalized)
        except VirusTotalHttpError as exc:
            if exc.status_code == 404:
                return SeedEnrichmentResult(issue=SeedEnrichmentIssue.UNKNOWN_TO_VT)
            return SeedEnrichmentResult(issue=SeedEnrichmentIssue.UNAVAILABLE)
        except VirusTotalError:
            return SeedEnrichmentResult(issue=SeedEnrichmentIssue.UNAVAILABLE)

        observation = await self._observations.store_file_report(
            report,
            subject_id=subject_id,
            observed_at=observed_at or datetime.now(UTC),
        )
        if not has_bytes:
            return SeedEnrichmentResult(
                issue=SeedEnrichmentIssue.REPORT_WITHOUT_BYTES,
                observation_id=observation.id,
            )
        features = {
            field: value
            for field, value in {
                "vhash": report.file.vhash,
                "imphash": report.file.imphash,
                "ssdeep": report.file.ssdeep,
                "tlsh": report.file.tlsh,
                "main_icon_dhash": report.file.main_icon_dhash,
                "rich_header_hash": report.file.rich_header_hash,
            }.items()
            if value
        }
        return SeedEnrichmentResult(observation_id=observation.id, features=features or None)
