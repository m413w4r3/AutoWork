from uuid import uuid4

import pytest

from cti_app.domain.collection import SourceCollection
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
