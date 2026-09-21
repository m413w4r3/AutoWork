"""PostgreSQL coverage for the production batch status read model."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event

from cti_app.domain.classification import TLP
from cti_app.domain.production import ProductionRunStatus, ProductionStage
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.discovery import (
    DiscoveryMergeRunRow,
    DiscoverySnapshotRow,
    DiscoverySubjectIdentityRow,
)
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.models.production import (
    EditionProductionBatchItemRow,
    EditionProductionBatchRow,
    ProductionInputSnapshotRow,
    ProductionRunRow,
)
from cti_app.infrastructure.database.models.selection import SelectionDecisionRow
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
        state="open",
        version=1,
        created_at=now,
        updated_at=now,
    )
    subjects = [
        SubjectRow(
            id=subject_id,
            edition_id=edition_id,
            title=f"Subject title {index}",
            slug=f"batch-read-{index}-{uuid4().hex}",
            tlp=TLP.AMBER.value,
            version=1,
            created_at=now,
            updated_at=now,
        )
        for index, subject_id in enumerate(subject_ids, 1)
    ]
    runs = [
        ProductionRunRow(
            id=run_id,
            subject_id=subject_id,
            edition_id=edition_id,
            status=status,
            current_stage=stage,
            references_conversation_id=None,
            synthesis_conversation_id=None,
            run_number=1,
            pipeline_generation=pipeline_generation,
            research_date=now.date(),
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
                ProductionRunStatus.READY.value,
                ProductionStage.SOURCES.value,
                4,
                None,
                None,
            ),
            (
                run_ids[1],
                subject_ids[1],
                ProductionRunStatus.NEEDS_REVIEW.value,
                ProductionStage.REFERENCES.value,
                2,
                "needs_more_context",
                "Review the source context",
            ),
            (
                run_ids[2],
                subject_ids[2],
                ProductionRunStatus.FAILED.value,
                ProductionStage.ASSEMBLY.value,
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
        idempotency_key=f"fixture-{batch_id.hex}",
        request_fingerprint="0" * 64,
        actor_id="fixture",
        correlation_id="fixture",
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
    # The snapshot is the canonical production input: its discovery lineage
    # must really exist, so the read model is exercised against enforced FKs.
    merge_run_id = uuid4()
    merge_run = DiscoveryMergeRunRow(
        id=merge_run_id,
        edition_id=edition_id,
        parent_snapshot_id=None,
        intake_id=None,
        planner_kind="deterministic_bootstrap",
        merge_model_run_id=None,
        prompt_version="1",
        policy_version="1",
        blocking_version="1",
        merge_input_hash="c" * 64,
        handle_map={},
        included_subject_ids=[],
        excluded_subject_count=0,
        raw_output_reference=None,
        normalized_output_reference=None,
        validation_status="valid",
        warnings=[],
        review_reasons=[],
        plan_payload=None,
        supersedes_merge_run_id=None,
        rebase_count=0,
        created_at=now,
    )
    discovery_snapshot_id = uuid4()
    discovery_snapshot = DiscoverySnapshotRow(
        id=discovery_snapshot_id,
        edition_id=edition_id,
        version=1,
        parent_snapshot_id=None,
        intake_id=None,
        merge_run_id=merge_run_id,
        planner_kind="deterministic_bootstrap",
        subjects=[],
        snapshot_hash="d" * 64,
        is_active=True,
        created_at=now,
    )
    identity_ids = [uuid4() for _ in subject_ids]
    identities = [
        DiscoverySubjectIdentityRow(
            id=identity_id,
            edition_id=edition_id,
            origin_key=f"subject:{subject_id}",
            created_by_merge_run_id=merge_run_id,
            status="active",
            merged_into_id=None,
            created_at=now,
        )
        for identity_id, subject_id in zip(identity_ids, subject_ids, strict=True)
    ]
    decision_ids = [uuid4() for _ in subject_ids]
    decisions = [
        SelectionDecisionRow(
            id=decision_id,
            edition_id=edition_id,
            discovery_subject_id=identity_id,
            snapshot_id=discovery_snapshot_id,
            snapshot_version=1,
            subject_id=subject_id,
            action="select",
            actor_id="read-model-test",
            correlation_id="read-model-test",
            idempotency_key=f"read-model-{index}",
            occurred_at=now,
        )
        for index, (decision_id, identity_id, subject_id) in enumerate(
            zip(decision_ids, identity_ids, subject_ids, strict=True)
        )
    ]
    snapshots = [
        ProductionInputSnapshotRow(
            id=uuid4(),
            production_run_id=run_id,
            subject_id=subject_id,
            edition_id=edition_id,
            subject_version=1,
            subject_title=title,
            subject_tlp=TLP.AMBER.value,
            selection_decision_id=decision_id,
            origin_discovery_subject_id=identity_id,
            canonical_discovery_subject_id=identity_id,
            discovery_snapshot_id=discovery_snapshot_id,
            discovery_snapshot_version=1,
            member_candidate_ids=[],
            discovery_summary="Description",
            actor_or_campaign="Actor",
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            research_date=date(2026, 8, 29),
            core_sources=[],
            input_hash="a" * 64,
            reuse_basis_hash="b" * 64,
            captured_at=now,
        )
        for run_id, subject_id, identity_id, decision_id, title in (
            (
                run_ids[0],
                subject_ids[0],
                identity_ids[0],
                decision_ids[0],
                "Snapshot title one",
            ),
            (
                run_ids[1],
                subject_ids[1],
                identity_ids[1],
                decision_ids[1],
                "Snapshot title two",
            ),
        )
    ]

    try:
        async with session_factory() as real_async_session:
            real_async_session.add(edition)
            await real_async_session.flush()
            real_async_session.add_all(subjects)
            await real_async_session.flush()
            real_async_session.add(merge_run)
            await real_async_session.flush()
            real_async_session.add(discovery_snapshot)
            real_async_session.add_all(identities)
            await real_async_session.flush()
            real_async_session.add_all(decisions)
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
        "Subject title 3",
    ]
    assert [item.pipeline_generation for item in result] == [2, 4, 3]
    assert [item.auto_recovery_count for item in result] == [1, 0, 0]
    assert [item.status for item in result] == [
        ProductionRunStatus.NEEDS_REVIEW,
        ProductionRunStatus.READY,
        ProductionRunStatus.FAILED,
    ]
    assert [item.current_stage for item in result] == [
        ProductionStage.REFERENCES,
        ProductionStage.SOURCES,
        ProductionStage.ASSEMBLY,
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
