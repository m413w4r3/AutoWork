from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.domain.investigation import DetectionCandidateStatus, DetectionCandidateVersion
from cti_app.infrastructure.database.models.investigation import DetectionCandidateVersionRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.test_investigation_persistence_postgres import _make_investigation

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _version(
    investigation_id: UUID, *, candidate_id: UUID, source: str
) -> DetectionCandidateVersion:
    return DetectionCandidateVersion(
        investigation_id=investigation_id,
        candidate_id=candidate_id,
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        motif_provenance=(),
        status=DetectionCandidateStatus.CANDIDATE,
        created_at=NOW,
    )


async def _import(
    session_factory: async_sessionmaker[AsyncSession], version: DetectionCandidateVersion
) -> tuple[DetectionCandidateVersion, bool]:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        outcome = await uow.detection_candidates.append_version_idempotently(version)
        await uow.commit()
        return outcome


async def _row_count(engine: AsyncEngine, candidate_ids: Iterable[UUID]) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count())
            .select_from(DetectionCandidateVersionRow)
            .where(DetectionCandidateVersionRow.candidate_id.in_(list(candidate_ids)))
        )
    return int(count or 0)


@pytest.mark.asyncio
async def test_sequential_import_replay_returns_the_stored_first_version(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    version = _version(
        investigation.id, candidate_id=uuid4(), source="rule bridge_one { condition: true }"
    )
    try:
        stored, replayed = await _import(session_factory, version)
        again, replayed_again = await _import(session_factory, version)
        assert (replayed, replayed_again) == (False, True)
        assert stored == again
        assert again.source == version.source
        assert again.source_sha256 == version.source_sha256
        assert await _row_count(engine, [version.candidate_id]) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_imports_store_one_exact_source(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    version = _version(
        investigation.id, candidate_id=uuid4(), source="rule bridge_race { condition: true }"
    )
    try:
        outcomes = await asyncio.gather(
            _import(session_factory, version), _import(session_factory, version)
        )
        assert sorted(replayed for _, replayed in outcomes) == [False, True]
        assert outcomes[0][0] == outcomes[1][0]
        assert outcomes[0][0].source == version.source
        assert await _row_count(engine, [version.candidate_id]) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_divergent_imports_for_one_identity_keep_a_single_winner(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    candidate_id = uuid4()
    left = _version(
        investigation.id, candidate_id=candidate_id, source="rule left { condition: true }"
    )
    right = _version(
        investigation.id, candidate_id=candidate_id, source="rule right { condition: false }"
    )
    try:
        results = await asyncio.gather(
            _import(session_factory, left),
            _import(session_factory, right),
            return_exceptions=True,
        )
        failures = [item for item in results if isinstance(item, BaseException)]
        winners = [item for item in results if not isinstance(item, BaseException)]
        assert len(winners) == 1 and winners[0][1] is False
        assert len(failures) == 1 and isinstance(failures[0], VersionConflictError)
        assert await _row_count(engine, [candidate_id]) == 1
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored = await uow.detection_candidates.list_for_candidate(candidate_id)
        assert stored == (winners[0][0],)
    finally:
        await engine.dispose()
