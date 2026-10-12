"""PostgreSQL proof for the read-only production control projection."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, text

from cti_app.api.production_control import router as control_router
from cti_app.application.edition_publication import EditionPublicationService
from cti_app.application.edition_review import EditionReviewService
from cti_app.application.production_control_view import ProductionControlViewService
from cti_app.application.production_repairs import ProductionRepairIssueService
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.jobs import Job, JobStatus
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchStatus,
    ProductionRun,
    ProductionRunStatus,
    ProductionStage,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

EDITION_ID = UUID("10000000-0000-4000-8000-000000000001")
SUBJECT_IDS = [UUID(f"20000000-0000-4000-8000-{number:012d}") for number in range(1, 11)]
HISTORICAL_RUN_ID = UUID("30000000-0000-4000-8000-000000000001")
READY_RUN_ID = UUID("30000000-0000-4000-8000-000000000002")
REVIEW_RUN_ID = UUID("30000000-0000-4000-8000-000000000003")
BATCH_ID = UUID("40000000-0000-4000-8000-000000000001")
NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def _view_service(uow_factory: object) -> ProductionControlViewService:
    repair_issues = ProductionRepairIssueService(uow_factory)  # type: ignore[arg-type]
    review = EditionReviewService(uow_factory, repair_issues)  # type: ignore[arg-type]
    publication = EditionPublicationService(
        uow_factory,  # type: ignore[arg-type]
        artifact_store=None,  # release_status is a repository-only read
        repair_issue_reader=repair_issues,
    )
    return ProductionControlViewService(
        uow_factory,  # type: ignore[arg-type]
        repair_issue_service=repair_issues,
        review_service=review,
        publication_service=publication,
    )


def _application(uow_factory: object) -> FastAPI:
    app = FastAPI()
    app.include_router(control_router)
    app.state.production_control_view_service = _view_service(uow_factory)
    return app


async def _seed(uow_factory: object) -> None:
    async with uow_factory() as uow:  # type: ignore[operator]
        edition = Edition(
            id=EDITION_ID,
            country="Control test",
            country_code="CT",
            period_start=date(2026, 10, 1),
            period_end=date(2026, 10, 31),
            tlp=TLP.GREEN,
            languages=("en",),
            created_at=NOW,
            updated_at=NOW,
        )
        assert await uow.editions.add_if_absent(edition)
        subjects = [
            Subject(
                id=subject_id,
                edition_id=EDITION_ID,
                title=f"Control subject {index + 1}",
                slug=f"control-subject-{index + 1:02d}",
                tlp=TLP.GREEN,
                created_at=NOW,
                updated_at=NOW,
            )
            for index, subject_id in enumerate(SUBJECT_IDS)
        ]
        for subject in subjects:
            await uow.subjects.add(subject)

        historical = ProductionRun(
            id=HISTORICAL_RUN_ID,
            subject_id=SUBJECT_IDS[0],
            edition_id=EDITION_ID,
            status=ProductionRunStatus.READY,
            current_stage=ProductionStage.ASSEMBLY,
            run_number=1,
            pipeline_generation=0,
            research_date=date(2026, 10, 1),
            created_at=NOW,
            updated_at=NOW,
        )
        ready = ProductionRun(
            id=READY_RUN_ID,
            subject_id=SUBJECT_IDS[0],
            edition_id=EDITION_ID,
            status=ProductionRunStatus.READY,
            current_stage=ProductionStage.ASSEMBLY,
            run_number=2,
            pipeline_generation=2,
            research_date=date(2026, 10, 1),
            created_at=NOW,
            updated_at=NOW,
        )
        needs_review = ProductionRun(
            id=REVIEW_RUN_ID,
            subject_id=SUBJECT_IDS[1],
            edition_id=EDITION_ID,
            status=ProductionRunStatus.NEEDS_REVIEW,
            current_stage=ProductionStage.SYNTHESIS,
            run_number=1,
            pipeline_generation=3,
            research_date=date(2026, 10, 1),
            error_code="synthesis_needs_review",
            error_message="A bounded fixture error summary.",
            created_at=NOW,
            updated_at=NOW,
        )
        for run in (historical, ready, needs_review):
            await uow.production_runs.add(run)

        artifact_id = 500000000000000000000000000000000000
        for run in (historical, ready, needs_review):
            for stage in ProductionArtifactStage:
                artifact_id += 1
                metadata = {}
                artifact_status = ProductionArtifactStatus.VERIFIED
                if run.id == REVIEW_RUN_ID and stage is ProductionArtifactStage.EXTRACTION:
                    metadata = {
                        "repair_index": [
                            {
                                "kind": "supplemental_source_unarchived",
                                "source_id": "control-source-1",
                                "source_url": "https://source.example/control",
                                "reason_code": "source_missing_from_references",
                            }
                        ]
                    }
                if run.id == REVIEW_RUN_ID and stage is ProductionArtifactStage.PUBLICATION:
                    artifact_status = ProductionArtifactStatus.STALE
                await uow.production_artifacts.append(
                    ProductionArtifact(
                        id=UUID(int=artifact_id),
                        production_run_id=run.id,
                        subject_id=run.subject_id,
                        stage=stage,
                        version=1,
                        input_hash=f"{artifact_id:064x}"[-64:],
                        status=artifact_status,
                        metadata=metadata,
                        created_at=NOW,
                    )
                )

        failed_job = Job(
            id=UUID("60000000-0000-4000-8000-000000000001"),
            kind="production.subject.synthesis",
            aggregate_type="subject",
            aggregate_id=SUBJECT_IDS[1],
            idempotency_key="control-view-failed-job",
            correlation_id="control-view-integration",
            input_parameters={
                "run_id": str(REVIEW_RUN_ID),
                "stage": "synthesis",
                "pipeline_generation": 3,
            },
            status=JobStatus.RUNNING,
            created_at=NOW,
            updated_at=NOW,
        )
        failed_job.fail("fixture_job_failed", "bounded failure", now=NOW)
        assert await uow.jobs.add_if_absent(failed_job)

        batch = EditionProductionBatch(
            id=BATCH_ID,
            edition_id=EDITION_ID,
            status=ProductionBatchStatus.QUEUED,
            idempotency_key="control-view-batch",
            request_fingerprint="b" * 64,
            actor_id="integration-analyst",
            correlation_id="control-view-integration",
            created_at=NOW,
        )
        batch.start(now=NOW)
        await uow.edition_production_batches.add(batch)
        await uow._require_session().flush()
        await uow.edition_production_batch_items.append_many(
            [
                EditionProductionBatchItem(
                    batch_id=BATCH_ID,
                    subject_id=SUBJECT_IDS[0],
                    production_run_id=READY_RUN_ID,
                    position=1,
                    created_at=NOW,
                ),
                EditionProductionBatchItem(
                    batch_id=BATCH_ID,
                    subject_id=SUBJECT_IDS[1],
                    production_run_id=REVIEW_RUN_ID,
                    position=2,
                    created_at=NOW,
                ),
            ]
        )
        await uow.commit()

    async with uow_factory() as uow:  # type: ignore[operator]
        batch = await uow.edition_production_batches.get_for_update(BATCH_ID)
        assert batch is not None
        assert batch.status is ProductionBatchStatus.RUNNING
        assert batch.pause(actor_id="integration-analyst", now=NOW)
        await uow.edition_production_batches.save(batch)
        await uow.commit()


async def _snapshot(uow_factory: object) -> tuple[tuple[int, int | None], ...]:
    async with uow_factory() as uow:  # type: ignore[operator]
        session = uow._require_session()
        statements = (
            "SELECT count(*), max(version) FROM editions WHERE id = :edition_id",
            "SELECT count(*), max(version) FROM subjects WHERE edition_id = :edition_id",
            "SELECT count(*), max(version) FROM production_runs WHERE edition_id = :edition_id",
            "SELECT count(*), max(a.version) FROM production_artifacts a "
            "JOIN production_runs r ON r.id = a.production_run_id "
            "WHERE r.edition_id = :edition_id",
            "SELECT count(*), max(version) FROM edition_production_batches "
            "WHERE edition_id = :edition_id",
            "SELECT count(*), max(attempt) FROM jobs WHERE aggregate_type = 'subject' "
            "AND aggregate_id IN (SELECT id FROM subjects WHERE edition_id = :edition_id)",
            "SELECT count(*), NULL::integer FROM production_repair_decisions "
            "WHERE edition_id = :edition_id",
            "SELECT count(*), NULL::integer FROM publication_review_decisions "
            "WHERE edition_id = :edition_id",
        )
        rows = []
        for statement in statements:
            result = await session.execute(text(statement), {"edition_id": EDITION_ID})
            rows.append(tuple(result.one()))
        return tuple(rows)


async def test_control_view_is_bounded_read_only_and_projects_historical_runs(
    uow_factory: object,
) -> None:
    await _seed(uow_factory)
    app = _application(uow_factory)
    before = await _snapshot(uow_factory)
    async with uow_factory() as uow:  # type: ignore[operator]
        engine = uow._require_session().bind.sync_engine

    query_count = 0
    page_query_count: int | None = None
    writes: list[str] = []

    def observe_sql(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        nonlocal query_count
        del connection, cursor, parameters, context, executemany
        command = statement.lstrip().split(maxsplit=1)[0].upper() if statement.strip() else ""
        if command == "SELECT":
            query_count += 1
        elif command in {"INSERT", "UPDATE", "DELETE", "MERGE"}:
            writes.append(command)

    event.listen(engine, "before_cursor_execute", observe_sql)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.get(f"/api/editions/{EDITION_ID}/production/control-view?limit=10")
            page_query_count = query_count
            page_again = await client.get(
                f"/api/editions/{EDITION_ID}/production/control-view?limit=10"
            )
            historical = await client.get(f"/api/production/runs/{HISTORICAL_RUN_ID}/control-view")
            sample_response = await client.get(
                f"/api/editions/{EDITION_ID}/production/control-view?limit=1"
            )
    finally:
        event.remove(engine, "before_cursor_execute", observe_sql)

    assert first.status_code == 200, first.text
    assert page_again.status_code == 200, page_again.text
    assert historical.status_code == 200, historical.text
    assert page_query_count is not None and page_query_count <= 35, (
        f"10-subject page used {page_query_count} SELECTs"
    )
    assert writes == []
    assert await _snapshot(uow_factory) == before

    body = first.json()
    assert body["schema_version"] == "agent-control.v1"
    assert body["scope"] == {"kind": "edition", "edition_id": str(EDITION_ID), "run_id": None}
    assert body["subjects"]["total"] == 10
    assert len(body["subjects"]["items"]) == 10
    assert len(body["subjects"]["items"][0]["run"]["stages"]) == 7
    assert "canonical_sha256" not in body["subjects"]["items"][0]["run"]["stages"][1]["artifact"]
    assert len(body["subjects"]["items"][1]["jobs"]["items"]) <= 20
    assert body["subjects"]["items"][1]["jobs"]["failed"] == 1
    assert body["subjects"]["items"][1]["repair_debt"]["unresolved"] == 1
    assert body["subjects"]["items"][1]["repair_debt"]["blocking"] == 1
    assert body["subjects"]["items"][1]["preview"]["state"] == "stale"
    assert body["batch"]["active"]["status"] == "paused"
    action_ids = {action["id"] for action in body["actions"]}
    assert "create_batch" not in action_ids
    assert "resume_batch" in action_ids
    assert len(first.content) < 100_000
    assert "bounded fixture error summary" not in first.text

    def without_as_of(payload: dict[str, object]) -> dict[str, object]:
        return {key: value for key, value in payload.items() if key != "as_of"}

    assert without_as_of(body) == without_as_of(page_again.json())
    historic_subject = historical.json()["subjects"]["items"][0]
    assert historical.json()["scope"]["kind"] == "run"
    assert historic_subject["run"]["run_number"] == 1
    assert historic_subject["run"]["pipeline_generation"] == 0
    assert historic_subject["current_run"] is False

    assert sample_response.status_code == 200, sample_response.text
    print("CONTROL_VIEW_EXAMPLE=" + json.dumps(sample_response.json(), indent=2))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first_page = await client.get(f"/api/editions/{EDITION_ID}/production/control-view?limit=3")
        cursor = first_page.json()["subjects"]["next_cursor"]
        second_page = await client.get(
            f"/api/editions/{EDITION_ID}/production/control-view?limit=3&cursor={cursor}"
        )
    assert first_page.status_code == second_page.status_code == 200
    first_ids = {item["subject_id"] for item in first_page.json()["subjects"]["items"]}
    second_ids = {item["subject_id"] for item in second_page.json()["subjects"]["items"]}
    assert first_ids.isdisjoint(second_ids)

    async with uow_factory() as uow:  # type: ignore[operator]
        edition = await uow.editions.get(EDITION_ID)
        assert edition is not None
        edition.archive(now=NOW)
        assert await uow.editions.update(edition, expected_version=edition.version - 1)
        await uow.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        archived = await client.get(f"/api/editions/{EDITION_ID}/production/control-view?limit=10")
    assert archived.status_code == 200
    assert archived.json()["edition"]["state"] == "archived"
    assert archived.json()["actions"] == []
    assert all(not subject["actions"] for subject in archived.json()["subjects"]["items"])
