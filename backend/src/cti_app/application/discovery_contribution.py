"""Service for handling temporal updates and contribution tracking in Discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryContribution,
)


def calculate_history_snapshot(batch: DiscoveryBatch) -> str:
    """Generate a summary of known intelligence for prompt injection.

    Summarizes accepted contributions to help Discovery focus on NEW information.
    """
    # Collect all accepted contributions
    accepted_contributions = [c for c in batch.contributions if c.status == ContributionStatus.ACCEPTED]
    if not accepted_contributions:
        return ""

    candidates = [c.candidate for c in accepted_contributions]

    # Collect actors, campaigns, malware
    actors = set()
    campaigns = set()
    malware = set()
    ioc_counts: dict[str, set] = {}

    for candidate in candidates:
        actors.update(candidate.actors)
        campaigns.update(candidate.campaigns)
        malware.update(candidate.malware)

        # Count IOCs by type (rough heuristic)
        for ioc in candidate.iocs:
            ioc_type = classify_ioc_type(ioc)
            if ioc_type not in ioc_counts:
                ioc_counts[ioc_type] = set()
            ioc_counts[ioc_type].add(ioc)

    # Format snapshot
    lines = [f"## Known Intelligence (Updated {batch.updated_at.date()})\n"]

    if actors:
        lines.append("### Known Actors")
        for actor in sorted(actors):
            lines.append(f"- {actor}")
        lines.append("")

    if campaigns:
        lines.append("### Known Campaigns")
        for campaign in sorted(campaigns):
            lines.append(f"- {campaign}")
        lines.append("")

    if malware:
        lines.append("### Known Malware")
        for malw in sorted(malware):
            lines.append(f"- {malw}")
        lines.append("")

    if ioc_counts:
        lines.append("### Known IOCs (Summary)")
        for ioc_type in sorted(ioc_counts.keys()):
            count = len(ioc_counts[ioc_type])
            lines.append(f"- {ioc_type}: {count} unique")
        lines.append("")

    lines.append("### Instructions")
    lines.append("Find NEW information about these threats that we haven't yet documented:")
    lines.append("1. New attribution evidence")
    lines.append("2. New infrastructure indicators")
    lines.append("3. New targeting patterns")
    lines.append("4. New malware capabilities")

    return "\n".join(lines)


def diff_contribution(old: CandidateTopic | None, new: CandidateTopic) -> dict:
    """Calculate structured diff between old and new contributions.

    Returns dict with:
    - title_changed: bool
    - new_sources: list[str] (URLs)
    - new_actors: list[str]
    - new_campaigns: list[str]
    - new_malware: list[str]
    - source_count_delta: int
    """
    if old is None:
        # Entirely new contribution
        return {
            "title_changed": False,
            "is_new": True,
            "new_sources": [s.url for s in new.sources],
            "new_actors": list(new.actors),
            "new_campaigns": list(new.campaigns),
            "new_malware": list(new.malware),
            "source_count_delta": len(new.sources),
        }

    old_source_urls = {s.canonical_url for s in old.sources}
    new_source_urls = {s.canonical_url for s in new.sources}
    new_sources = new_source_urls - old_source_urls

    old_actors = set(old.actors)
    new_actors_set = set(new.actors)
    new_actors = list(new_actors_set - old_actors)

    old_campaigns = set(old.campaigns)
    new_campaigns_set = set(new.campaigns)
    new_campaigns = list(new_campaigns_set - old_campaigns)

    old_malware = set(old.malware)
    new_malware_set = set(new.malware)
    new_malware = list(new_malware_set - old_malware)

    return {
        "title_changed": old.title_fingerprint != new.title_fingerprint,
        "is_new": False,
        "new_sources": list(new_sources),
        "new_actors": new_actors,
        "new_campaigns": new_campaigns,
        "new_malware": new_malware,
        "source_count_delta": len(new_source_urls) - len(old_source_urls),
    }


def apply_contribution_status(
    batch: DiscoveryBatch,
    contrib_id: UUID,
    new_status: ContributionStatus,
    note: str = "",
) -> DiscoveryBatch:
    """Update contribution status and timestamps.

    Modifies batch in place and returns it.
    """
    contribution = next(
        (c for c in batch.contributions if c.candidate.id == contrib_id),
        None,
    )
    if contribution is None:
        raise ValueError(f"Contribution {contrib_id} not found in batch {batch.id}")

    contribution.status = new_status
    contribution.human_note = note

    if new_status == ContributionStatus.ACCEPTED:
        contribution.accepted_at = datetime.now(UTC)

    batch.updated_at = datetime.now(UTC)
    return batch


def classify_ioc_type(ioc_value: str) -> str:
    """Simple IOC type classifier for summary generation."""
    # This is a rough heuristic; in production you'd use the actual IOC parser
    ioc_lower = ioc_value.lower()

    if ioc_lower.startswith("cve-"):
        return "CVE"
    if "@" in ioc_lower:
        return "Email"
    if ioc_lower.startswith(("http://", "https://")):
        return "URL"
    if "." in ioc_lower and not any(c in ioc_lower for c in ["//", ":"]):
        return "Domain"
    if all(c in "0123456789abcdef:" for c in ioc_lower) and ":" in ioc_lower:
        return "IPv6"
    if all(c in "0123456789." for c in ioc_lower):
        return "IPv4"
    if len(ioc_lower) == 32 and all(c in "0123456789abcdef" for c in ioc_lower):
        return "MD5"
    if len(ioc_lower) == 40 and all(c in "0123456789abcdef" for c in ioc_lower):
        return "SHA-1"
    if len(ioc_lower) == 64 and all(c in "0123456789abcdef" for c in ioc_lower):
        return "SHA-256"

    return "Other"
