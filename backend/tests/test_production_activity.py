from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from cti_app.application.production_read_model import (
    ProductionActivitySnapshot,
    derive_production_activity,
)
from cti_app.domain.production import ProductionRunStatus, ProductionStage
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyBatchStatusReadRepository,
)

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _snapshot(**overrides: object) -> ProductionActivitySnapshot:
    values: dict[str, object] = {
        "subject_id": uuid4(),
        "run_id": uuid4(),
        "stage": ProductionStage.EXTRACTION,
        "run_status": ProductionRunStatus.RUNNING,
    }
    values.update(overrides)
    return ProductionActivitySnapshot(**values)  # type: ignore[arg-type]


def test_reconciliation_probe_is_read_only_activity() -> None:
    activity = derive_production_activity(
        _snapshot(
            job_kind="production.subject.reconciliation_probe",
            job_status="queued",
            job_created_at=NOW - timedelta(seconds=20),
            model_run_status=None,
        ),
        now=NOW,
    )

    assert activity.kind == "reconciliation_probe"
    assert activity.stage is ProductionStage.EXTRACTION
    assert activity.since_seconds == 20
    assert "aucune requête modèle" in (activity.detail or "")


def test_running_model_run_is_model_call_activity() -> None:
    started_at = NOW - timedelta(minutes=2, seconds=10)
    activity = derive_production_activity(
        _snapshot(
            job_kind="production.subject.extraction",
            job_status="running",
            model_run_status="running",
            model_run_started_at=started_at,
        ),
        now=NOW,
    )

    assert activity.kind == "model_call"
    assert activity.started_at == started_at
    assert activity.since_seconds == 130


def test_queued_job_with_retry_time_is_scheduled_retry() -> None:
    retry_at = NOW + timedelta(minutes=4)
    activity = derive_production_activity(
        _snapshot(
            job_kind="production.subject.extraction",
            job_status="queued",
            next_retry_at=retry_at,
            attempt=2,
        ),
        now=NOW,
    )

    assert activity.kind == "retry_scheduled"
    assert activity.started_at == retry_at
    assert activity.since_seconds == 0
    assert activity.attempt == 2


def test_queued_run_waits_for_batch_and_terminal_run_is_idle() -> None:
    waiting = derive_production_activity(_snapshot(run_status=ProductionRunStatus.QUEUED), now=NOW)
    idle = derive_production_activity(
        _snapshot(run_status=ProductionRunStatus.READY, job_status=None), now=NOW
    )

    assert waiting.kind == "waiting_batch"
    assert idle.kind == "idle"
    assert idle.since_seconds is None


async def test_activity_read_model_uses_one_set_based_query() -> None:
    class _Result:
        def mappings(self) -> tuple[()]:
            return ()

    class _Session:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement: object) -> _Result:
            self.statements.append(statement)
            return _Result()

    session = _Session()
    repository = SqlAlchemyBatchStatusReadRepository(session)  # type: ignore[arg-type]

    result = await repository.list_activity_for_runs([uuid4(), uuid4()])

    assert result == ()
    assert len(session.statements) == 1
    statement = session.statements[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[union-attr]
    assert "production_runs" in sql
    assert "jobs" in sql
    assert "model_runs" in sql
    assert "row_number()" in sql
