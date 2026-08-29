"""Machine verification of claims and indicators.

Some evidence can be checked by the application itself: a quote that is really
present at its span in an archived source, an indicator the deterministic
extractor found in that same text. Recording that as a `HumanDecision` would
fabricate a human review that never happened, so it is a separate status the
review projection reports — a real human decision always overrides it.
"""

from __future__ import annotations

from cti_app.domain.collection import (
    Claim,
    Indicator,
    ReviewStatus,
)

# What the unified publication pipeline may put in front of a reader without a
# human having looked at it.
ACCEPTED_FOR_PUBLICATION = frozenset(
    {
        ReviewStatus.MACHINE_VERIFIED,
        ReviewStatus.VALIDATED,
        ReviewStatus.CORRECTED,
    }
)


def claim_is_machine_verified(claim: Claim, archived_text: str | None) -> bool:
    """True when the claim's quote is really at its span in the archived text."""
    if archived_text is None:
        return False
    span = claim.span
    if span.end > len(archived_text):
        return False
    quoted = archived_text[span.start : span.end]
    payload_quote = claim.extraction_payload.get("exact_quote")
    expected = str(payload_quote) if payload_quote else claim.value
    return bool(expected) and expected.strip() in quoted


def indicator_is_machine_verified(indicator: Indicator, archived_text: str | None) -> bool:
    """True when the indicator's literal value is at its span in the source."""
    if archived_text is None:
        return False
    span = indicator.span
    if span.end > len(archived_text):
        return False
    return indicator.original_value.strip() in archived_text[span.start : span.end]


def project_review_status(
    human_status: ReviewStatus | None,
    *,
    machine_verified: bool,
) -> ReviewStatus:
    """Combine a human decision with machine verification.

    A human decision always wins. Without one, machine verification lifts the
    item out of the raw `extracted` state.
    """
    if human_status is not None and human_status is not ReviewStatus.EXTRACTED:
        return human_status
    return ReviewStatus.MACHINE_VERIFIED if machine_verified else ReviewStatus.EXTRACTED


def accepted_for_publication(status: ReviewStatus) -> bool:
    """Whether an item may feed the unified publication."""
    return status in ACCEPTED_FOR_PUBLICATION
