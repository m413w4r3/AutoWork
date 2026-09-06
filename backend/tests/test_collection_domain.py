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
    # A manual upload is not a collector job: no jobs.id is ever invented.
    assert source.fetch_job_id is None
    assert source.manual_lease_id is not None


def test_manual_archive_requires_the_current_manual_lease() -> None:
    source = collection()
    source.state = CollectionState.FAILED_RETRYABLE
    first_lease = uuid4()
    assert source.claim_manual_upload(
        first_lease,
        lease_duration=timedelta(minutes=2),
        policy_snapshot_id="policy-snapshot",
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    # A second upload takes over the expired lease with its own token.
    second_lease = uuid4()
    assert source.claim_manual_upload(
        second_lease,
        lease_duration=timedelta(minutes=2),
        policy_snapshot_id="policy-snapshot",
        now=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="current manual lease owner"):
        source.archive_manual(
            manual_lease_id=first_lease,
            attempt_id=uuid4(),
            source_document_id=uuid4(),
            decoded_blob_id=uuid4(),
        )

    source.archive_manual(
        manual_lease_id=second_lease,
        attempt_id=uuid4(),
        source_document_id=uuid4(),
        decoded_blob_id=uuid4(),
    )
    assert source.state is CollectionState.ARCHIVED
    assert source.manual_lease_id is None


def test_manual_lease_cannot_archive_a_source_taken_over_by_the_collector() -> None:
    source = collection()
    source.state = CollectionState.FAILED_RETRYABLE
    manual_lease = uuid4()
    assert source.claim_manual_upload(
        manual_lease,
        lease_duration=timedelta(minutes=2),
        policy_snapshot_id="policy-snapshot",
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    job_id = uuid4()
    assert source.claim_fetch(
        job_id,
        lease_duration=timedelta(minutes=2),
        policy_snapshot_id="policy-snapshot",
        now=datetime(2026, 9, 4, 1, tzinfo=UTC),
    )
    assert source.manual_lease_id is None

    with pytest.raises(ValueError, match="current manual lease owner"):
        source.archive_manual(
            manual_lease_id=manual_lease,
            attempt_id=uuid4(),
            source_document_id=uuid4(),
            decoded_blob_id=uuid4(),
        )


def test_a_live_lease_blocks_the_other_acquisition_path() -> None:
    collector = collection()
    collector.state = CollectionState.PENDING
    now = datetime(2026, 9, 4, tzinfo=UTC)
    assert collector.claim_fetch(
        uuid4(),
        lease_duration=timedelta(minutes=10),
        policy_snapshot_id="policy-snapshot",
        now=now,
    )
    assert (
        collector.claim_manual_upload(
            uuid4(),
            lease_duration=timedelta(minutes=10),
            policy_snapshot_id="policy-snapshot",
            now=now,
        )
        is False
    )

    manual = collection()
    manual.state = CollectionState.BLOCKED
    assert manual.claim_manual_upload(
        uuid4(),
        lease_duration=timedelta(minutes=10),
        policy_snapshot_id="policy-snapshot",
        now=now,
    )
    assert (
        manual.claim_fetch(
            uuid4(),
            lease_duration=timedelta(minutes=10),
            policy_snapshot_id="policy-snapshot",
            now=now,
        )
        is False
    )


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
