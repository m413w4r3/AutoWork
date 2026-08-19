"""Test helpers for discovery batch creation."""

from datetime import UTC, datetime

from cti_app.domain.discovery import CandidateTopic, ContributionStatus, DiscoveryContribution


def wrap_candidates_in_contributions(
    candidates: list[CandidateTopic],
    status: ContributionStatus = ContributionStatus.PENDING,
) -> list[DiscoveryContribution]:
    """Helper to convert candidates to contributions for testing."""
    now = datetime.now(UTC)
    return [
        DiscoveryContribution(
            candidate=candidate,
            status=status,
            created_at=now,
            accepted_at=now if status == ContributionStatus.ACCEPTED else None,
        )
        for candidate in candidates
    ]
