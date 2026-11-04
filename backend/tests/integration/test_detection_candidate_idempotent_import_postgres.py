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
from cti_app.domain.investigation import (
    DetectionCandidateProvenance,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
)
from cti_app.infrastructure.database.models.investigation import DetectionCandidateVersionRow
from cti_app.infrastructure.database.repositories.investigation import (
    SqlAlchemyDetectionCandidateRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.test_investigation_persistence_postgres import _make_investigation

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
PREFLIGHT_PAUSE_TIMEOUT_SECONDS = 30


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
async def test_candidate_level_provenance_round_trips_without_motifs(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    source = "rule bridge_candidate { condition: true }"
    version = DetectionCandidateVersion(
        investigation_id=investigation.id,
        candidate_id=uuid4(),
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        motif_provenance=(),
        provenance=DetectionCandidateProvenance(
            kind="bridge_yara_draft",
            metadata={"bridge_invocation_id": str(uuid4()), "compile_status": "ok"},
        ),
        created_at=NOW,
    )
    try:
        stored, replayed = await _import(session_factory, version)
        assert replayed is False
        assert stored.provenance == version.provenance
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            loaded = await uow.detection_candidates.list_for_candidate(version.candidate_id)
        assert loaded == (version,)
        replay, replayed_again = await _import(session_factory, version)
        assert replayed_again is True
        assert replay.provenance == version.provenance
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_imports_store_one_exact_source(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory)
    version = _version(
        investigation.id, candidate_id=uuid4(), source="rule bridge_race { condition: true }"
    )
    second_after_preflight: asyncio.Event = asyncio.Event()
    release_second_preflight: asyncio.Event = asyncio.Event()
    second_task: asyncio.Task[tuple[DetectionCandidateVersion, bool]] | None = None
    paused_second_preflight = False
    list_for_candidate = SqlAlchemyDetectionCandidateRepository.list_for_candidate

    async def pause_second_after_preflight(
        repository: SqlAlchemyDetectionCandidateRepository,
        candidate_id: UUID,
    ) -> tuple[DetectionCandidateVersion, ...]:
        nonlocal paused_second_preflight
        versions = await list_for_candidate(repository, candidate_id)
        if asyncio.current_task() is second_task and not paused_second_preflight:
            assert versions == ()
            paused_second_preflight = True
            second_after_preflight.set()
            await release_second_preflight.wait()
        return versions

    monkeypatch.setattr(
        SqlAlchemyDetectionCandidateRepository,
        "list_for_candidate",
        pause_second_after_preflight,
    )
    preflight_waiter: asyncio.Task[bool] | None = None
    try:
        second_task = asyncio.create_task(_import(session_factory, version))
        preflight_waiter = asyncio.create_task(second_after_preflight.wait())
        done, _ = await asyncio.wait(
            {preflight_waiter, second_task},
            timeout=PREFLIGHT_PAUSE_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            second_task.cancel()
            pytest.fail("second import did not reach the controlled preflight in time")
        if second_task in done:
            await second_task
            pytest.fail("second import completed before its preflight was paused")
        await preflight_waiter
        first_outcome = await _import(session_factory, version)
        release_second_preflight.set()
        second_outcome = await second_task
        outcomes = [first_outcome, second_outcome]
        assert sorted(replayed for _, replayed in outcomes) == [False, True]
        assert outcomes[0][0] == outcomes[1][0]
        assert outcomes[0][0].source == version.source
        assert outcomes[0][0].source_sha256 == version.source_sha256
        assert await _row_count(engine, [version.candidate_id]) == 1
    finally:
        release_second_preflight.set()
        if preflight_waiter is not None and not preflight_waiter.done():
            preflight_waiter.cancel()
            await asyncio.gather(preflight_waiter, return_exceptions=True)
        if second_task is not None and not second_task.done():
            await asyncio.gather(second_task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_divergent_imports_for_one_identity_keep_a_single_winner(
    migrated_postgres_url: str, monkeypatch: pytest.MonkeyPatch
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
    right_after_preflight: asyncio.Event = asyncio.Event()
    release_right_preflight: asyncio.Event = asyncio.Event()
    right_task: asyncio.Task[tuple[DetectionCandidateVersion, bool]] | None = None
    paused_right_preflight = False
    list_for_candidate = SqlAlchemyDetectionCandidateRepository.list_for_candidate

    async def pause_right_after_preflight(
        repository: SqlAlchemyDetectionCandidateRepository,
        requested_candidate_id: UUID,
    ) -> tuple[DetectionCandidateVersion, ...]:
        nonlocal paused_right_preflight
        versions = await list_for_candidate(repository, requested_candidate_id)
        if asyncio.current_task() is right_task and not paused_right_preflight:
            assert versions == ()
            paused_right_preflight = True
            right_after_preflight.set()
            await release_right_preflight.wait()
        return versions

    monkeypatch.setattr(
        SqlAlchemyDetectionCandidateRepository,
        "list_for_candidate",
        pause_right_after_preflight,
    )
    preflight_waiter: asyncio.Task[bool] | None = None
    try:
        right_task = asyncio.create_task(_import(session_factory, right))
        preflight_waiter = asyncio.create_task(right_after_preflight.wait())
        done, _ = await asyncio.wait(
            {preflight_waiter, right_task},
            timeout=PREFLIGHT_PAUSE_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            right_task.cancel()
            pytest.fail("right import did not reach the controlled preflight in time")
        if right_task in done:
            await right_task
            pytest.fail("right import completed before its preflight was paused")
        await preflight_waiter

        winner, replayed = await _import(session_factory, left)
        assert replayed is False
        release_right_preflight.set()
        with pytest.raises(VersionConflictError):
            await right_task
        assert await _row_count(engine, [candidate_id]) == 1
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored = await uow.detection_candidates.list_for_candidate(candidate_id)
        assert stored == (winner,)
        assert stored[0].source == left.source
        assert stored[0].source_sha256 == left.source_sha256
    finally:
        release_right_preflight.set()
        if preflight_waiter is not None and not preflight_waiter.done():
            preflight_waiter.cancel()
            await asyncio.gather(preflight_waiter, return_exceptions=True)
        if right_task is not None and not right_task.done():
            await asyncio.gather(right_task, return_exceptions=True)
        await engine.dispose()
