from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cti_app.domain.collection import CollectionState, SourceCollection
from cti_app.domain.discovery import SourceRelationshipStatus, SourceRole


def collection() -> SourceCollection:
    return SourceCollection(
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        batch_id=uuid4(),
        source_candidate_id=uuid4(),
        requested_url="https://research.example/report",
        proposed_role=SourceRole.PRIMARY,
    )


def test_model_proposal_cannot_mark_relationship_verified() -> None:
    with pytest.raises(ValueError, match="deterministic evidence or a human decision"):
        SourceCollection(
            subject_id=uuid4(),
            edition_id=uuid4(),
            group_id=uuid4(),
            batch_id=uuid4(),
            source_candidate_id=uuid4(),
            requested_url="https://research.example/report",
            proposed_role=SourceRole.PRIMARY,
            relationship_status=SourceRelationshipStatus.VERIFIED,
            relationship_evidence="model_proposal",
        )


def test_verified_relationship_requires_deterministic_or_human_evidence() -> None:
    source = collection()

    with pytest.raises(ValueError, match="deterministic evidence or a human decision"):
        source.verify_relationship(SourceRole.INDEPENDENT)

    source.verify_relationship(SourceRole.INDEPENDENT, actor_id="dev-analyst")
    assert source.relationship_status is SourceRelationshipStatus.VERIFIED
    assert source.relationship_evidence == "human:dev-analyst"


def test_blocked_source_cannot_be_forced_for_retry() -> None:
    source = collection()
    source.state = CollectionState.BLOCKED

    with pytest.raises(ValueError, match="cannot bypass"):
        source.prepare_explicit_retry(policy_changed=True)


@pytest.mark.parametrize(
    "state",
    [
        CollectionState.BLOCKED,
        CollectionState.FAILED_TERMINAL,
        CollectionState.UNAVAILABLE,
        CollectionState.PENDING,
    ],
)
def test_manual_upload_can_claim_failed_or_pending_source(state: CollectionState) -> None:
    source = collection()
    source.state = state
    now = datetime(2026, 9, 4, tzinfo=UTC)

    claimed = source.claim_manual_upload(
        uuid4(),
        lease_duration=timedelta(minutes=2),
        policy_snapshot_id="policy-snapshot",
        now=now,
    )

    assert claimed is True
    assert source.state is CollectionState.FETCHING
    assert source.fetch_started_at == now


@pytest.mark.parametrize(
    "state",
    [CollectionState.ARCHIVED, CollectionState.EXTRACTED, CollectionState.COMPLETED],
)
def test_manual_upload_refuses_archived_evidence_states(state: CollectionState) -> None:
    source = collection()
    source.state = state

    assert (
        source.claim_manual_upload(
            uuid4(),
            lease_duration=timedelta(minutes=2),
            policy_snapshot_id="policy-snapshot",
        )
        is False
    )
