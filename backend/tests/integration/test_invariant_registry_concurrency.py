from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from uuid import uuid4
from itertools import product
from string import ascii_uppercase

import pytest
from sqlalchemy import func, select

from cti_app.application.invariants import InvariantRegistryService
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.goodware import Banality
from cti_app.domain.invariants import (
    AnalystManualProvenance,
    InvariantCategory,
    InvariantType,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionProfile,
    SubjectProductionRun,
)
from cti_app.infrastructure.database.models.invariants import (
    CandidateInvariantRow,
    InvariantRejectionRow,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
_COUNTRY_CODES = iter("".join(pair) for pair in product(ascii_uppercase, repeat=2))

class AlwaysBanal:
    def score(self, occurrence_count: int | None) -> Banality:
        return Banality.BANAL


async def _make_investigation(session_factory, suffix: str) -> AnalystInvestigation:
    edition = Edition(
        country=f"P09 {suffix}",
        country_code=next(_COUNTRY_CODES),
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("en",),
        target_major_articles=1,
        target_briefs=1,
        source_profile="p09-test",
    )
    subject = Subject(external_id=f"P09-{suffix}", slug=f"p09-{suffix}", tlp=TLP.AMBER)
    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        profile=ProductionProfile.BRIEF_AUTO,
    )
    artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="a" * 64,
    )
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=artifact.id,
        budget=LoopBudget(),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.subject_production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


def _service(session_factory, *, scorer=None) -> InvariantRegistryService:
    return InvariantRegistryService(
        lambda: SqlAlchemyUnitOfWork(session_factory),
        settings=Settings(
            goodware_suspicious_count=3,
            goodware_banal_count=10,
            invariant_max_pattern_chars=96,
            code_ngram_max_mask_ratio=0.20,
            code_ngram_min_contiguous_fixed_bytes=6,
        ),
        banality_scorer=scorer,
    )


async def _counts(engine, investigation_id):
    async with engine.connect() as connection:
        return (
            await connection.scalar(
                select(func.count()).select_from(CandidateInvariantRow).where(
                    CandidateInvariantRow.investigation_id == investigation_id
                )
            ),
            await connection.scalar(
                select(func.count()).select_from(InvariantRejectionRow).where(
                    InvariantRejectionRow.investigation_id == investigation_id
                )
            ),
        )


@pytest.mark.asyncio
async def test_concurrent_replay_has_one_durable_outcome(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory, uuid4().hex)
    service = _service(session_factory)
    try:
        results = await asyncio.gather(
            *(
                service.propose_manual(
                    investigation_id=investigation.id,
                    sample_ids=(),
                    type=InvariantType.LITERAL_STRING,
                    category=InvariantCategory.UNKNOWN,
                    motif="analyst note",
                    pattern="metadata-key",
                    actor_id="analyst",
                    occurred_at=NOW,
                )
                for _ in range(2)
            )
        )
        assert all(result.accepted for result in results)
        assert results[0].invariant == results[1].invariant
        assert await _counts(engine, investigation.id) == (1, 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_accept_reject_race_has_one_durable_outcome(migrated_postgres_url: str) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory, uuid4().hex)
    accepted_service = _service(session_factory)
    rejected_service = _service(session_factory, scorer=AlwaysBanal())
    kwargs = {
        "investigation_id": investigation.id,
        "sample_ids": (),
        "type": InvariantType.LITERAL_STRING,
        "category": InvariantCategory.UNKNOWN,
        "motif": "analyst note",
        "pattern": "race-key",
        "actor_id": "analyst",
        "occurred_at": NOW,
    }
    try:
        results = await asyncio.gather(
            accepted_service.propose_manual(**kwargs),
            rejected_service.propose_manual(**kwargs),
        )
        assert sum(result.accepted for result in results) in (0, 2)
        assert await _counts(engine, investigation.id) in ((1, 0), (0, 1))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_multi_provenance_persists_and_reloads_canonically(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    investigation = await _make_investigation(session_factory, uuid4().hex)
    service = _service(session_factory)
    first = AnalystManualProvenance(actor_id="z-analyst", occurred_at=NOW, motif="first note")
    second = AnalystManualProvenance(
        actor_id="a-analyst", occurred_at=NOW.replace(microsecond=1), motif="second note"
    )
    try:
        result = await service.propose(
            investigation_id=investigation.id,
            sample_ids=(),
            type=InvariantType.LITERAL_STRING,
            category=InvariantCategory.UNKNOWN,
            pattern="metadata-key",
            provenances=(first, second),
        )
        assert result.invariant is not None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            reloaded = await uow.invariants.get_invariant(result.invariant.id)
        assert reloaded is not None
        expected = tuple(
            sorted((first, second), key=lambda item: item.as_canonical_dict()["actor_id"])
        )
        assert reloaded.provenances == expected
    finally:
        await engine.dispose()
