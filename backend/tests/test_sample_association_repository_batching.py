from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.domain.investigation import (
    QualificationActorKind,
    QualificationChange,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
)
from cti_app.infrastructure.database.repositories.investigation import (
    SqlAlchemySampleAssociationRepository,
)


class _ScalarsSession:
    def __init__(self, results: list[tuple[object, ...]]) -> None:
        self._results = iter(results)
        self.statements = []

    async def scalars(self, statement: object) -> tuple[object, ...]:
        self.statements.append(statement)
        return next(self._results)


@pytest.mark.asyncio
async def test_sample_association_listing_batches_versions_and_history() -> None:
    investigation_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    first = SimpleNamespace(
        id=first_id,
        investigation_id=investigation_id,
        sample_sha256="a" * 64,
        origin="vt_pivot",
    )
    second = SimpleNamespace(
        id=second_id,
        investigation_id=investigation_id,
        sample_sha256="b" * 64,
        origin="manual",
    )
    versions = (
        SimpleNamespace(
            association_id=second_id,
            version=1,
            qualification="candidate",
            related_sample_sha256=[],
        ),
        SimpleNamespace(
            association_id=first_id,
            version=2,
            qualification="confirmed",
            related_sample_sha256=["c" * 64],
        ),
    )
    changed_at = datetime(2026, 10, 1, tzinfo=UTC)
    changes = (
        SimpleNamespace(
            association_id=first_id,
            version=2,
            from_qualification="candidate",
            to_qualification="confirmed",
            actor_kind="human",
            actor_id="analyst-7",
            reason="Compared the synthetic sample with cited evidence.",
            changed_at=changed_at,
        ),
    )
    session = _ScalarsSession([(first, second), versions, changes])
    repository = SqlAlchemySampleAssociationRepository(session)  # type: ignore[arg-type]

    actual = await repository.list_for_investigation(investigation_id)

    expected = (
        SampleAssociation(
            id=first_id,
            investigation_id=investigation_id,
            sample_sha256="a" * 64,
            origin=SampleOrigin.VT_PIVOT,
            qualification=SampleQualification.CONFIRMED,
            changes=(
                QualificationChange(
                    from_qualification=SampleQualification.CANDIDATE,
                    to_qualification=SampleQualification.CONFIRMED,
                    actor_kind=QualificationActorKind.HUMAN,
                    actor_id="analyst-7",
                    reason="Compared the synthetic sample with cited evidence.",
                    changed_at=changed_at,
                ),
            ),
            related_sample_sha256=("c" * 64,),
            version=2,
        ),
        SampleAssociation(
            id=second_id,
            investigation_id=investigation_id,
            sample_sha256="b" * 64,
            origin=SampleOrigin.MANUAL,
        ),
    )
    assert actual == expected
    assert len(session.statements) == 3


@pytest.mark.asyncio
async def test_sample_association_listing_does_not_query_versions_when_empty() -> None:
    session = _ScalarsSession([()])
    repository = SqlAlchemySampleAssociationRepository(session)  # type: ignore[arg-type]

    assert await repository.list_for_investigation(uuid4()) == ()
    assert len(session.statements) == 1
