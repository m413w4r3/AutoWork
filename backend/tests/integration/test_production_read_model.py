"""PostgreSQL coverage for the production batch status read model."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event

from cti_app.domain.classification import TLP
from cti_app.domain.production import SubjectProductionStage, SubjectProductionStatus
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.models.editorial import EditorialGroupRow
from cti_app.infrastructure.database.models.production import (
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionInputSnapshotRow,
    SubjectProductionRunRow,
)
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyBatchStatusReadRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_batch_status_read_model_is_one_real_postgres_select(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    now = datetime.now(UTC)
    edition_id = uuid4()
    subject_ids = [uuid4() for _ in range(3)]
    group_ids = [uuid4() for _ in range(3)]
    run_ids = [uuid4() for _ in range(3)]
    batch_id = uuid4()

    edition = EditionRow(
        id=edition_id,
        country="Readland",
        country_code="RD",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER.value,
        languages=["fr"],
        target_articles=4,
        source_profile="test",
        status="production",
        version=1,
        created_at=now,
        updated_at=now,
    )
    subjects = [
        SubjectRow(
            id=subject_id,
            external_id=f"BATCH-READ-{index}-{uuid4().hex}",
            slug=f"batch-read-{index}-{uuid4().hex}",
            tlp=TLP.AMBER.value,
            created_at=now,
        )
        for index, subject_id in enumerate(subject_ids, 1)
    ]
    groups = [
        EditorialGroupRow(
            id=group_id,
            edition_id=edition_id,
            title=f"Editorial title {index}",
            outcome="new_subject",
            status="selected",
            source_relationship_status="provisional",
            needs_source_verification=False,
            needs_source_expansion=False,
            grouping_confidence="high",
            grouping_justification="integration test",
            subject_id=subject_id,
            discovery_subject_id=None,
            payload={},
            version=1,
            created_at=now,
            updated_at=now,
        )
        for index, (group_id, subject_id) in enumerate(zip(group_ids, subject_ids, strict=True), 1)
    ]
    runs = [
        SubjectProductionRunRow(
            id=run_id,
            subject_id=subject_id,
            edition_id=edition_id,
            status=status,
            current_stage=stage,
            references_conversation_id=None,
            synthesis_conversation_id=None,
            run_number=1,
            pipeline_generation=pipeline_generation,
            research_date=None,
            error_code=error_code,
            error_message=error_message,
            error_details=None,
            started_at=now,
            finished_at=None,
            created_at=now,
            updated_at=now,
            version=1,
        )
        for run_id, subject_id, status, stage, pipeline_generation, error_code, error_message in (
            (
                run_ids[0],
                subject_ids[0],
                SubjectProductionStatus.READY.value,
                SubjectProductionStage.SOURCES.value,
                4,
                None,
                None,
            ),
            (
                run_ids[1],
                subject_ids[1],
                SubjectProductionStatus.NEEDS_REVIEW.value,
                SubjectProductionStage.REFERENCES.value,
                2,
                "needs_more_context",
                "Review the source context",
            ),
            (
                run_ids[2],
                subject_ids[2],
                SubjectProductionStatus.FAILED.value,
                SubjectProductionStage.ASSEMBLY.value,
                3,
                "stage_failed",
                "Assembly failed",
            ),
        )
    ]
    batch = EditionProductionBatchRow(
        id=batch_id,
        edition_id=edition_id,
        status="running",
        phase="initial",
        next_dispatch_at=None,
        created_at=now,
        started_at=now,
        finished_at=None,
        version=1,
    )
    batch_items = [
        EditionProductionBatchItemRow(
            id=uuid4(),
            batch_id=batch_id,
            subject_id=subject_id,
            production_run_id=run_id,
            position=position,
            auto_recovery_count=auto_recovery_count,
            created_at=now,
        )
        for subject_id, run_id, position, auto_recovery_count in (
            (subject_ids[0], run_ids[0], 2, 0),
            (subject_ids[1], run_ids[1], 1, 1),
            (subject_ids[2], run_ids[2], 3, 0),
        )
    ]
    snapshots = [
        ProductionInputSnapshotRow(
            id=uuid4(),
            production_run_id=run_id,
            subject_id=subject_id,
            edition_id=edition_id,
            editorial_group_id=group_id,
            editorial_group_version=1,
            subject_title=title,
            subject_description="Description",
            actor_or_campaign="Actor",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            research_date=date(2026, 8, 29),
            core_sources=[],
            input_hash="a" * 64,
            captured_at=now,
        )
        for run_id, subject_id, group_id, title in (
            (run_ids[0], subject_ids[0], group_ids[0], "Snapshot title one"),
            (run_ids[1], subject_ids[1], group_ids[1], "Snapshot title two"),
        )
    ]

    try:
        async with session_factory() as real_async_session:
            real_async_session.add(edition)
            await real_async_session.flush()
            real_async_session.add_all(subjects)
            await real_async_session.flush()
            real_async_session.add_all(groups)
            await real_async_session.flush()
            real_async_session.add_all(runs)
            await real_async_session.flush()
            real_async_session.add(batch)
            await real_async_session.flush()
            real_async_session.add_all(batch_items)
            await real_async_session.flush()
            real_async_session.add_all(snapshots)
            await real_async_session.commit()

            select_statements: list[str] = []

            def count_selects(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                if statement.lstrip().upper().startswith("SELECT"):
                    select_statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
            try:
                result = await SqlAlchemyBatchStatusReadRepository(
                    real_async_session
                ).list_for_batch(batch.id)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count_selects)

    finally:
        await engine.dispose()

    assert len(select_statements) == 1
    assert len(result) == 3
    assert [(item.position, item.subject_id, item.run_id) for item in result] == [
        (1, subject_ids[1], run_ids[1]),
        (2, subject_ids[0], run_ids[0]),
        (3, subject_ids[2], run_ids[2]),
    ]
    assert [item.title for item in result] == [
        "Snapshot title two",
        "Snapshot title one",
        "Editorial title 3",
    ]
    assert [item.pipeline_generation for item in result] == [2, 4, 3]
    assert [item.auto_recovery_count for item in result] == [1, 0, 0]
    assert [item.status for item in result] == [
        SubjectProductionStatus.NEEDS_REVIEW,
        SubjectProductionStatus.READY,
        SubjectProductionStatus.FAILED,
    ]
    assert [item.current_stage for item in result] == [
        SubjectProductionStage.REFERENCES,
        SubjectProductionStage.SOURCES,
        SubjectProductionStage.ASSEMBLY,
    ]
    assert [item.error_code for item in result] == [
        "needs_more_context",
        None,
        "stage_failed",
    ]
    assert [item.error_message for item in result] == [
        "Review the source context",
        None,
        "Assembly failed",
    ]
