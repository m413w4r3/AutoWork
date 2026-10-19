from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.virustotal import VirusTotalPage
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalObservation,
    VirusTotalOperation,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.core import (
    VirusTotalObservationRow,
)
from cti_app.infrastructure.database.repositories.core import (
    SqlAlchemyVirusTotalObservationRepository,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DRIFT_SCRIPT = BACKEND_ROOT / "scripts" / "db_drift_vt_observation_capability.py"
CONSTRAINT_NAME = "ck_vt_observation_capability"
_STALE_CAPABILITIES_SQL = (
    "'file_report','file_relationships','intelligence_search',"
    "'file_download','submissions','behaviour_pcap','retrohunt'"
)


def _page() -> VirusTotalPage:
    return VirusTotalPage(
        items=(),
        next_cursor=None,
        observed_count=0,
        stopped_due_to_limit=False,
        exhaustive=True,
        raw_json_pages=(b'{"data":[]}',),
        http_statuses=(200,),
        limit_used=10,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
        empty=True,
    )


async def _replace_capability_check(engine: AsyncEngine, check_sql: str) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(f"ALTER TABLE virustotal_observations DROP CONSTRAINT {CONSTRAINT_NAME}")
        )
        await connection.execute(
            text(
                "ALTER TABLE virustotal_observations "
                f"ADD CONSTRAINT {CONSTRAINT_NAME} CHECK ({check_sql})"
            )
        )


def _run_drift_check(dsn: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), dsn],
        cwd=BACKEND_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def _upgrade_database(database_url: str) -> None:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.config_file_name = None
    command.upgrade(config, "head")


@pytest.fixture
def migrated_temporary_postgres_url(temporary_postgres_url: str) -> str:
    _upgrade_database(temporary_postgres_url)
    return temporary_postgres_url


@pytest.mark.asyncio
async def test_stale_capability_check_blocks_behaviour_then_current_schema_persists_all(
    migrated_temporary_postgres_url: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_postgres_engine(migrated_temporary_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    catalog = BlobCatalogService(FilesystemBlobStore(tmp_path / "blobs"), uow_factory)
    service = VirusTotalObservationService(catalog, uow_factory)
    observed_at = datetime.now(UTC)

    try:
        await _replace_capability_check(
            engine,
            f"capability IN ({_STALE_CAPABILITIES_SQL})",
        )

        with pytest.raises(IntegrityError):
            await service.store_behaviours(_page(), file_hash="a" * 64, observed_at=observed_at)
        failure_log = next(
            (
                record
                for record in caplog.records
                if getattr(record, "event", None) == "vt_observation_persistence_failed"
            ),
            None,
        )
        assert failure_log is not None
        assert failure_log.constraint_name == CONSTRAINT_NAME

        drift = _run_drift_check(migrated_temporary_postgres_url)
        assert drift.returncode == 1, drift.stdout + drift.stderr
        assert "behaviour_reports" in drift.stdout

        model_constraint = next(
            constraint
            for constraint in VirusTotalObservationRow.__table__.constraints
            if constraint.name == CONSTRAINT_NAME
        )
        await _replace_capability_check(engine, str(model_constraint.sqltext))

        clean = _run_drift_check(migrated_temporary_postgres_url)
        assert clean.returncode == 0, clean.stdout + clean.stderr

        behaviour_observations = await service.store_behaviours(
            _page(), file_hash="a" * 64, observed_at=observed_at
        )
        assert len(behaviour_observations) == 1
        behaviour = behaviour_observations[0]
        assert behaviour.capability is VirusTotalCapability.BEHAVIOUR_REPORTS

        operations = {
            VirusTotalCapability.FILE_REPORT: VirusTotalOperation.FILE_REPORT,
            VirusTotalCapability.FILE_RELATIONSHIPS: VirusTotalOperation.FILE_RELATIONSHIP,
            VirusTotalCapability.INTELLIGENCE_SEARCH: VirusTotalOperation.INTELLIGENCE_SEARCH,
            VirusTotalCapability.FILE_DOWNLOAD: VirusTotalOperation.FILE_DOWNLOAD,
            VirusTotalCapability.SUBMISSIONS: VirusTotalOperation.RETROHUNT_SUBMIT,
            VirusTotalCapability.BEHAVIOUR_REPORTS: VirusTotalOperation.BEHAVIOURS,
            VirusTotalCapability.BEHAVIOUR_PCAP: VirusTotalOperation.BEHAVIOURS,
            VirusTotalCapability.RETROHUNT: VirusTotalOperation.RETROHUNT_RESULTS,
        }
        observation_session_factory = async_sessionmaker(engine, expire_on_commit=False)
        observation_ids = []
        async with observation_session_factory() as session:
            repository = SqlAlchemyVirusTotalObservationRepository(session)
            for capability in VirusTotalCapability:
                observation = VirusTotalObservation(
                    operation=operations[capability],
                    capability=capability,
                    source_identifier=f"schema-check-{capability.value}-{uuid4().hex}",
                    safe_parameters={},
                    http_status=200,
                    blob_id=behaviour.blob_id,
                    raw_sha256=behaviour.raw_sha256,
                    raw_size=behaviour.raw_size,
                    observed_at=observed_at,
                )
                await repository.add(observation)
                observation_ids.append(observation.id)
            await session.commit()

            persisted = await session.scalars(
                select(VirusTotalObservationRow.capability).where(
                    VirusTotalObservationRow.id.in_(observation_ids)
                )
            )
            assert {VirusTotalCapability(value) for value in persisted} == set(VirusTotalCapability)
    finally:
        await engine.dispose()
