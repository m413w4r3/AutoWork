from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from cti_app.domain.selection import SelectionAction, SelectionDecision, SubjectDiscoveryOrigin
from cti_app.infrastructure.database.models.core import SubjectRow
from cti_app.infrastructure.database.models.discovery import (
    DiscoveryMergeRunRow,
    DiscoverySnapshotRow,
    DiscoverySubjectIdentityRow,
)
from cti_app.infrastructure.database.models.editions import EditionRow
from cti_app.infrastructure.database.models.selection import (
    SelectionDecisionRow,
    SubjectDiscoveryOriginRow,
)
from cti_app.infrastructure.database.repositories.selection import (
    SqlAlchemySelectionDecisionRepository,
    SqlAlchemySubjectDiscoveryOriginRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_selection_and_origin_repositories_are_append_only_and_fk_backed(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    now = datetime.now(UTC)
    edition_id = uuid4()
    merge_run_id = uuid4()
    discovery_subject_id = uuid4()
    snapshot_id = uuid4()
    subject_id = uuid4()
    try:
        async with session_factory() as session:
            session.add(
                EditionRow(
                    id=edition_id,
                    country="Selectionland",
                    country_code="SL",
                    period_start=date(2099, 1, 1),
                    period_end=date(2099, 1, 31),
                    tlp="GREEN",
                    languages=["en"],
                    state="open",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()
            session.add(
                DiscoveryMergeRunRow(
                    id=merge_run_id,
                    edition_id=edition_id,
                    parent_snapshot_id=None,
                    intake_id=None,
                    planner_kind="deterministic_bootstrap",
                    merge_model_run_id=None,
                    prompt_version="1",
                    policy_version="1",
                    blocking_version="1",
                    merge_input_hash="a" * 64,
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
            )
            await session.flush()
            session.add(
                DiscoverySubjectIdentityRow(
                    id=discovery_subject_id,
                    edition_id=edition_id,
                    origin_key="candidate-1",
                    created_by_merge_run_id=merge_run_id,
                    status="active",
                    merged_into_id=None,
                    created_at=now,
                )
            )
            session.add(
                DiscoverySnapshotRow(
                    id=snapshot_id,
                    edition_id=edition_id,
                    version=1,
                    parent_snapshot_id=None,
                    intake_id=None,
                    merge_run_id=merge_run_id,
                    planner_kind="deterministic_bootstrap",
                    subjects=[],
                    snapshot_hash="b" * 64,
                    is_active=True,
                    created_at=now,
                )
            )
            session.add(
                SubjectRow(
                    id=subject_id,
                    edition_id=edition_id,
                    title="Selected subject",
                    slug="selected-subject",
                    tlp="GREEN",
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.flush()

            decisions = SqlAlchemySelectionDecisionRepository(session)
            origins = SqlAlchemySubjectDiscoveryOriginRepository(session)
            selected = SelectionDecision(
                edition_id=edition_id,
                discovery_subject_id=discovery_subject_id,
                snapshot_id=snapshot_id,
                snapshot_version=1,
                action=SelectionAction.SELECT,
                subject_id=subject_id,
                actor_id="analyst-1",
                correlation_id="correlation-1",
                idempotency_key="idempotency-1",
                occurred_at=now,
            )
            ignored = SelectionDecision(
                edition_id=edition_id,
                discovery_subject_id=discovery_subject_id,
                snapshot_id=snapshot_id,
                snapshot_version=1,
                action=SelectionAction.IGNORE,
                subject_id=None,
                actor_id="analyst-1",
                correlation_id="correlation-2",
                idempotency_key="idempotency-2",
                occurred_at=now + timedelta(seconds=1),
            )
            await decisions.append(selected)
            await decisions.append(ignored)
            origin = SubjectDiscoveryOrigin(
                subject_id=subject_id,
                edition_id=edition_id,
                discovery_subject_id=discovery_subject_id,
                selection_decision_id=selected.id,
                selected_snapshot_id=snapshot_id,
                selected_snapshot_version=1,
                created_at=now,
            )
            await origins.add(origin)
            await session.commit()

        async with session_factory() as session:
            decisions = SqlAlchemySelectionDecisionRepository(session)
            origins = SqlAlchemySubjectDiscoveryOriginRepository(session)
            listed = await decisions.list_for_edition(edition_id)
            assert [item.id for item in listed] == [selected.id, ignored.id]
            assert (
                await decisions.get_by_idempotency_key(
                    edition_id, discovery_subject_id, "idempotency-1"
                )
                == selected
            )
            assert await origins.get_by_subject(subject_id) == origin
            assert await origins.list_for_edition(edition_id) == [origin]

            duplicate_origin = SubjectDiscoveryOrigin(
                subject_id=subject_id,
                edition_id=edition_id,
                discovery_subject_id=discovery_subject_id,
                selection_decision_id=ignored.id,
                selected_snapshot_id=snapshot_id,
                selected_snapshot_version=1,
                created_at=now,
            )
            with pytest.raises(IntegrityError):
                await origins.add(duplicate_origin)
            await session.rollback()

            duplicate_decision = SelectionDecision(
                edition_id=edition_id,
                discovery_subject_id=discovery_subject_id,
                snapshot_id=snapshot_id,
                snapshot_version=1,
                action=SelectionAction.IGNORE,
                subject_id=None,
                actor_id="analyst-1",
                correlation_id="correlation-3",
                idempotency_key="idempotency-1",
            )
            with pytest.raises(IntegrityError):
                await decisions.append(duplicate_decision)
            await session.rollback()

            with pytest.raises(DBAPIError):
                await session.execute(
                    update(SelectionDecisionRow)
                    .where(SelectionDecisionRow.id == selected.id)
                    .values(actor_id="mutated")
                )
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    delete(SelectionDecisionRow).where(SelectionDecisionRow.id == selected.id)
                )
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    delete(SubjectDiscoveryOriginRow).where(
                        SubjectDiscoveryOriginRow.subject_id == subject_id
                    )
                )
            await session.rollback()

            with pytest.raises(IntegrityError):
                await session.execute(delete(SubjectRow).where(SubjectRow.id == subject_id))
            await session.rollback()
            with pytest.raises(IntegrityError):
                await session.execute(
                    delete(DiscoverySnapshotRow).where(DiscoverySnapshotRow.id == snapshot_id)
                )
            await session.rollback()
    finally:
        await engine.dispose()
