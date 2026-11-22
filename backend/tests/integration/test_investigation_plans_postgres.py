"""PostgreSQL oracles for migration 0020 and the plan/prose-authority guards.

Disposable databases only: application, conforming re-application, partial-state
refusal, append-only triggers, prose authority immutability, downgrade guards
and the compare-and-swap race on ``investigation_plans.current_version``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_persistence import VersionConflictError
from cti_app.application.investigation_plans import (
    InvestigationPlanProvenanceError,
    InvestigationPlanService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_plan import (
    InvestigationLeadV1,
    InvestigationPlanV1,
    PlanParseStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
    ProseAuthority,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

PREVIOUS = "0019_workspace_bindings"
HEAD = "0023_agent_sessions"
PLAN_TABLES = {"investigation_plans", "investigation_plan_versions"}
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _table_names(database_url: str) -> set[str]:
    async def run() -> set[str]:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                return set(
                    await connection.run_sync(
                        lambda sync_connection: sa.inspect(sync_connection).get_table_names()
                    )
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _alembic_version(database_url: str) -> str:
    async def run() -> str:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                assert isinstance(value, str)
                return value
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _column_default(database_url: str, table: str, column: str) -> str | None:
    async def run() -> str | None:
        engine = create_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                value = await connection.scalar(
                    sa.text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = :table "
                        "AND column_name = :column"
                    ),
                    {"table": table, "column": column},
                )
                return cast(str | None, value)
        finally:
            await engine.dispose()

    return asyncio.run(run())


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    prose_authority: ProseAuthority = ProseAuthority.LEGACY,
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Plan fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Plan fixture",
            slug=f"plan-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="e" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
            prose_authority=prose_authority,
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


async def _advisor_exchange(
    session_factory: async_sessionmaker[AsyncSession],
    blob_root: Path,
    investigation_id: UUID,
    *,
    source: str = "advisor.consult.response",
    object_type: str = "advisor_response_received",
) -> tuple[UUID, UUID]:
    blob_store = FilesystemBlobStore(blob_root)
    catalog = BlobCatalogService(blob_store, lambda: SqlAlchemyUnitOfWork(session_factory))
    blob = await catalog.ingest(
        BytesIO(json.dumps({"schema": "advisor-response-link.v1"}).encode()),
        logical_bucket="model-outputs",
        mime_type="application/json",
    )
    observation_id = uuid4()
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=f"advisor-plan-{uuid4()}",
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        normalized_parameters={
            "consultation_type": "plan_investigation",
            "question": "Build an investigation plan",
        },
        purpose="Ask Bridge for an investigation plan",
        status=ToolInvocationStatus.SUCCEEDED,
        response_refs=(observation_id,),
        created_at=NOW,
        updated_at=NOW,
    )
    observation = EvidenceObservation(
        id=observation_id,
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob.id,
        sha256=blob.descriptor.sha256,
        source=source,
        source_version="advisor-consult.v1",
        observed_at=NOW,
        object_type=object_type,
        object_id=f"advisor-plan-{uuid4()}",
        invocation_ref=invocation.id,
        limitations=("Raw Bridge text is untrusted advisory material.",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.tool_invocations.add_if_absent(invocation)
        await uow.evidence_observations.append_if_absent(observation)
        await uow.commit()
    return invocation.id, observation.id


def _plan(
    investigation_id: UUID,
    version: int,
    *,
    title: str = "Inspect the loader",
) -> InvestigationPlanV1:
    return InvestigationPlanV1(
        investigation_id=investigation_id,
        version=version,
        leads=(
            InvestigationLeadV1(
                title=title,
                objective="Identify the loader's network gates.",
                cti_justification="The article claims a two-stage downloader.",
                claim_refs=("claim-1",),
                start_evidence_refs=(SHA_A,),
                sample_refs=(SHA_B,),
                tools=("pe.strings",),
                preconditions=("sample available in quarantine",),
                interest="high",
                conclusion_criteria="Two independent C2 gates are proven.",
                limits="No execution outside the confined profile.",
            ),
        ),
    )


async def _service_for(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[InvestigationPlanService, SqlAlchemyUnitOfWork]:
    uow = SqlAlchemyUnitOfWork(session_factory)
    await uow.__aenter__()
    service = InvestigationPlanService(
        plans=uow.investigation_plans,
        invocations=uow.tool_invocations,
        observations=uow.evidence_observations,
        investigations=uow.analyst_investigations,
    )
    return service, uow


async def _create_version(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    plan: InvestigationPlanV1,
    invocation_id: UUID,
    observation_id: UUID,
):
    service, uow = await _service_for(session_factory)
    try:
        version = await service.create_version(
            plan,
            parse_status=PlanParseStatus.PARSED,
            source_invocation_id=invocation_id,
            source_observation_id=observation_id,
            now=NOW,
        )
        await uow.commit()
        return version
    finally:
        await uow.__aexit__(None, None, None)


def test_plan_migration_creates_tables_and_refuses_used_downgrade(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    assert PLAN_TABLES <= _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD
    prose_authority_default = _column_default(
        temporary_postgres_url, "analyst_investigations", "prose_authority"
    )
    assert "legacy" in (prose_authority_default or "")

    command.downgrade(config, PREVIOUS)
    assert not PLAN_TABLES & _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == PREVIOUS

    command.upgrade(config, "head")

    async def create_one() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            invocation_id, observation_id = await _advisor_exchange(
                factory, tmp_path / "plan-blobs", investigation.id
            )
            await _create_version(
                factory,
                plan=_plan(investigation.id, 1),
                invocation_id=invocation_id,
                observation_id=observation_id,
            )
        finally:
            await engine.dispose()

    asyncio.run(create_one())
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD
    assert PLAN_TABLES <= _table_names(temporary_postgres_url)


def test_plan_migration_accepts_an_already_conforming_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)
    command.upgrade(config, "head")
    assert PLAN_TABLES <= _table_names(temporary_postgres_url)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_plan_migration_rejects_a_partial_plan_shape(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    command.stamp(config, PREVIOUS)

    async def drop_versions() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                # 0021 adds investigation_tasks.plan_version_id -> plan versions;
                # detach it so this 0020 partial-shape fixture can drop the table.
                await connection.execute(
                    sa.text(
                        "ALTER TABLE investigation_tasks "
                        "DROP CONSTRAINT fk_investigation_tasks_plan_version"
                    )
                )
                await connection.execute(sa.text("DROP TABLE investigation_plan_versions"))
        finally:
            await engine.dispose()

    asyncio.run(drop_versions())
    with pytest.raises(RuntimeError, match="preflight rejected"):
        command.upgrade(config, "head")
    assert _alembic_version(temporary_postgres_url) == PREVIOUS
    assert "investigation_plans" in _table_names(temporary_postgres_url)
    assert "investigation_plan_versions" not in _table_names(temporary_postgres_url)


def test_downgrade_refused_while_a_bridge_only_investigation_exists(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def seed() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            await _seed_investigation(factory, prose_authority=ProseAuthority.BRIDGE_ONLY)
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_downgrade_refused_while_plan_decisions_exist(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def seed() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            async with SqlAlchemyUnitOfWork(factory) as uow:
                await uow.analyst_decisions.append(
                    AnalystDecision(
                        investigation_id=investigation.id,
                        decision_type=AnalystDecisionType.PLAN_ACCEPT,
                        target_type=AnalystDecisionTargetType.PLAN_VERSION,
                        target_id=uuid4(),
                        actor_id="analyst:test",
                        reason="Accept the plan under review",
                        correlation_id=f"plan-decision-{uuid4()}",
                    )
                )
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, PREVIOUS)
    assert _alembic_version(temporary_postgres_url) == HEAD


def test_plan_versions_and_prose_authority_are_immutable_at_head(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            invocation_id, observation_id = await _advisor_exchange(
                factory, tmp_path / "immutable-blobs", investigation.id
            )
            version = await _create_version(
                factory,
                plan=_plan(investigation.id, 1),
                invocation_id=invocation_id,
                observation_id=observation_id,
            )

            async with factory() as session:
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(
                        sa.text(
                            "UPDATE investigation_plan_versions SET parse_status = "
                            "'partially_parsed' WHERE id = :id"
                        ),
                        {"id": version.id},
                    )
                await session.rollback()
            async with factory() as session:
                with pytest.raises(DBAPIError, match="append-only"):
                    await session.execute(
                        sa.text("DELETE FROM investigation_plan_versions WHERE id = :id"),
                        {"id": version.id},
                    )
                await session.rollback()
            async with factory() as session:
                # 0021's investigation_tasks.plan_version_id foreign key makes a
                # bare TRUNCATE fail at the FK check before the append-only
                # trigger; either way the table cannot be truncated.
                with pytest.raises(DBAPIError, match=r"append-only|cannot truncate"):
                    await session.execute(sa.text("TRUNCATE investigation_plan_versions"))
                await session.rollback()
            async with factory() as session:
                with pytest.raises(DBAPIError, match="immutable"):
                    await session.execute(
                        sa.text(
                            "UPDATE analyst_investigations SET prose_authority = 'bridge_only' "
                            "WHERE id = :id"
                        ),
                        {"id": investigation.id},
                    )
                await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_plan_creation_requires_a_persisted_advisor_response(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            invocation_id, observation_id = await _advisor_exchange(
                factory, tmp_path / "provenance-blobs", investigation.id
            )
            created = await _create_version(
                factory,
                plan=_plan(investigation.id, 1),
                invocation_id=invocation_id,
                observation_id=observation_id,
            )
            assert created.version == 1

            foreign = await _seed_investigation(factory)
            call_invocation_id, call_observation_id = await _advisor_exchange(
                factory,
                tmp_path / "provenance-blobs-call",
                foreign.id,
                source="advisor.consult",
                object_type="advisor_call",
            )
            service, uow = await _service_for(factory)
            try:
                with pytest.raises(InvestigationPlanProvenanceError):
                    await service.create_version(
                        _plan(foreign.id, 1),
                        parse_status=PlanParseStatus.PARSED,
                        source_invocation_id=call_invocation_id,
                        source_observation_id=call_observation_id,
                        now=NOW,
                    )
                await uow.rollback()
            finally:
                await uow.__aexit__(None, None, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_plan_current_version_compare_and_swap_has_one_winner(
    temporary_postgres_url: str, tmp_path: Path
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_postgres_engine(temporary_postgres_url)
        try:
            factory = create_session_factory(engine)
            investigation = await _seed_investigation(factory)
            invocation_id, observation_id = await _advisor_exchange(
                factory, tmp_path / "race-blobs", investigation.id
            )
            await _create_version(
                factory,
                plan=_plan(investigation.id, 1),
                invocation_id=invocation_id,
                observation_id=observation_id,
            )
            second_left = await _advisor_exchange(
                factory, tmp_path / "race-blobs-left", investigation.id
            )
            second_right = await _advisor_exchange(
                factory, tmp_path / "race-blobs-right", investigation.id
            )

            async def attempt(
                invocation: UUID,
                observation: UUID,
                title: str,
            ) -> str:
                try:
                    await _create_version(
                        factory,
                        plan=_plan(investigation.id, 2, title=title),
                        invocation_id=invocation,
                        observation_id=observation,
                    )
                except VersionConflictError:
                    return "conflict"
                return "created"

            results = await asyncio.gather(
                attempt(second_left[0], second_left[1], "left"),
                attempt(second_right[0], second_right[1], "right"),
            )
            assert sorted(results) == ["conflict", "created"]

            async with SqlAlchemyUnitOfWork(factory) as uow:
                head = await uow.investigation_plans.get_head(investigation.id)
                assert head is not None and head.current_version == 2
                versions = await uow.investigation_plans.list_for_investigation(investigation.id)
                assert [item.version for item in versions] == [1, 2]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
