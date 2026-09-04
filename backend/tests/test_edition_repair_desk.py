from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.publication import router
from cti_app.application.edition_review import (
    EditionRepairReadService,
    EditionReviewReadItem,
    EditionReviewService,
)
from cti_app.application.production_repairs import (
    ProductionRepairDecisionInput,
    ProductionRepairDecisionService,
    ProductionRepairIssueService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRepairAction,
    ProductionRepairIssueKind,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_A = UUID("22222222-2222-4222-8222-222222222222")
SUBJECT_B = UUID("33333333-3333-4333-8333-333333333333")


def _edition(status: EditionStatus = EditionStatus.REVIEW) -> Edition:
    return Edition(
        id=EDITION_ID,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=2,
        source_profile="test",
        status=status,
    )


def _row(subject_id: UUID, position: int) -> EditionReviewReadItem:
    run_id = uuid4()
    return EditionReviewReadItem(
        position=position,
        subject_id=subject_id,
        title=f"Article {position}",
        run_id=run_id,
        pipeline_generation=2,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=uuid4(),
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )


class _ReadModelUow:
    def __init__(
        self,
        rows: list[EditionReviewReadItem],
        edition_status: EditionStatus = EditionStatus.REVIEW,
    ) -> None:
        self.rows = rows
        self.editions = SimpleNamespace(
            get=lambda _edition_id: _async_value(_edition(edition_status))
        )
        self.edition_review_read_model = SimpleNamespace(
            list_for_edition=lambda _edition_id: _async_value(self.rows)
        )

    async def __aenter__(self) -> _ReadModelUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _ReadModelFactory:
    def __init__(self, uow: _ReadModelUow) -> None:
        self.uow = uow

    def __call__(self) -> _ReadModelUow:
        return self.uow


class _IssueReader:
    def __init__(self, issues: list[object]) -> None:
        self.issues = issues
        self.light_calls = 0

    async def list_issue_views(
        self, _edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[object, ...]:
        self.light_calls += 1
        return tuple(
            issue
            for issue in self.issues
            if subject_id is None or issue.subject_id == subject_id
        )

    async def list_supplemental_source_issues(
        self, _edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[object, ...]:
        return ()


def _issue(
    index: int,
    row: EditionReviewReadItem,
    *,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
    artifact_type: str = "domain",
    is_ioc: bool = True,
    resolved: bool = False,
    projection_applied: bool = False,
    action: ProductionRepairAction = ProductionRepairAction.EXCLUDE,
) -> SimpleNamespace:
    decision = (
        SimpleNamespace(
            id=uuid4(),
            action=action,
            reason="reviewed",
        )
        if resolved
        else None
    )
    value = f"value-{index}"
    return SimpleNamespace(
        repair_key=f"{index + 1:064x}",
        kind=kind,
        artifact_type=artifact_type,
        source_id=f"source-{index}",
        source_title=f"Source {index}",
        source_url=f"https://example.test/{index}",
        collection_id=None,
        collection_state=None,
        reason_code="source_evidence_missing",
        value_sha256=hashlib.sha256(value.encode()).hexdigest(),
        preview=value,
        payload_available=True,
        production_run_id=row.run_id,
        observed_artifact_id=uuid4(),
        observed_artifact_version=1,
        observed_pipeline_generation=row.pipeline_generation,
        effective_decision=decision,
        projection_applied=projection_applied,
        is_publication_ioc=is_ioc,
        subject_id=row.subject_id,
    )


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_repair_read_model_orders_filters_and_reaches_201_issues() -> None:
    row_a = _row(SUBJECT_A, 1)
    row_b = _row(SUBJECT_B, 2)
    issues = [_issue(index, row_a) for index in range(100)]
    issues.extend(_issue(index, row_b) for index in range(100, 201))
    reader = _IssueReader(issues)
    service = EditionRepairReadService(
        _ReadModelFactory(_ReadModelUow([row_a, row_b])), reader  # type: ignore[arg-type]
    )

    page = await service.list(EDITION_ID, limit=100)
    collected = list(page.items)
    while page.next_cursor is not None:
        page = await service.list(EDITION_ID, cursor=page.next_cursor, limit=100)
        collected.extend(page.items)

    assert len(collected) == 201
    assert [item.position for item in collected] == [1] * 100 + [2] * 101
    assert page.next_cursor is None
    assert page.summary.unresolved_total == 201
    assert page.summary.articles_with_repairs == 2
    assert [article.subject_id for article in page.articles] == [SUBJECT_A, SUBJECT_B]
    assert reader.light_calls == 3

    filtered = await service.list(
        EDITION_ID,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        subject_id=SUBJECT_B,
        artifact_type="domain",
        limit=200,
    )
    assert len(filtered.items) == 101
    assert all(item.subject_id == SUBJECT_B for item in filtered.items)


@pytest.mark.asyncio
async def test_edition_repair_http_list_exposes_summary_items_and_cursor() -> None:
    row = _row(SUBJECT_A, 1)
    issue = _issue(1, row)
    read_service = EditionRepairReadService(
        _ReadModelFactory(_ReadModelUow([row])), _IssueReader([issue])  # type: ignore[arg-type]
    )
    application = FastAPI()
    application.include_router(router)
    application.state.edition_repair_read_service = read_service

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/editions/{EDITION_ID}/review/repairs", params={"limit": 1}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["rejected_iocs_to_review"] == 1
    assert body["items"][0]["subject_id"] == str(SUBJECT_A)
    assert body["items"][0]["payload_available"] is True
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_repair_summary_separates_ioc_rule_other_and_resolved() -> None:
    row = _row(SUBJECT_A, 1)
    issues = [
        _issue(1, row, is_ioc=True),
        _issue(2, row, is_ioc=False),
        _issue(
            3,
            row,
            kind=ProductionRepairIssueKind.REJECTED_RULE,
            artifact_type="sigma",
        ),
        _issue(4, row, resolved=True, projection_applied=True),
    ]
    page = await EditionRepairReadService(
        _ReadModelFactory(_ReadModelUow([row])), _IssueReader(issues)  # type: ignore[arg-type]
    ).list(EDITION_ID, limit=20)

    assert page.summary == page.summary.__class__(
        unresolved_total=3,
        sources_to_supply=0,
        rejected_iocs_to_review=1,
        rejected_rules_to_review=1,
        rejected_other_artifacts=1,
        articles_with_repairs=1,
        articles_needing_rebuild=0,
    )
    resolved = await EditionRepairReadService(
        _ReadModelFactory(_ReadModelUow([row])), _IssueReader(issues)  # type: ignore[arg-type]
    ).list(EDITION_ID, status="resolved", limit=20)
    assert len(resolved.items) == 1
    assert resolved.items[0].resolved


def test_signoff_requires_actionable_repairs_to_be_arbitrated() -> None:
    row = _row(SUBJECT_A, 1)
    issues = [
        _issue(1, row, is_ioc=True),
        _issue(2, row, kind=ProductionRepairIssueKind.REJECTED_RULE),
        _issue(
            3,
            row,
            kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
        ),
        _issue(4, row, is_ioc=False),
    ]
    review = EditionReviewService.from_rows(EDITION_ID, [row], repair_issues=issues)
    assert (review.can_accept, review.unresolved_repair_count) == (False, 3)
    assert review.items[0].active_repair_count == 4

    for issue in issues[:3]:
        issue.effective_decision = SimpleNamespace(
            id=uuid4(),
            action=(
                ProductionRepairAction.CONTINUE_WITHOUT_SOURCE
                if issue.kind is ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED
                else ProductionRepairAction.INCLUDE
            ),
            reason="explicitly reviewed",
        )
    accepted = EditionReviewService.from_rows(EDITION_ID, [row], repair_issues=issues)
    assert accepted.can_accept
    assert accepted.repair_review_complete


def test_repair_items_expose_backend_rebuild_stages() -> None:
    row = _row(SUBJECT_A, 1)
    no_document = replace(
        row,
        document_artifact_id=None,
        document_artifact_version=None,
        document_input_hash=None,
        document_artifact_status=None,
    )
    source = _issue(
        1,
        row,
        kind=ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
    )
    pending_projection = _issue(
        2,
        row,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        resolved=True,
        action=ProductionRepairAction.INCLUDE,
    )
    pending_synthesis = _issue(
        3,
        no_document,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        resolved=True,
        projection_applied=True,
        action=ProductionRepairAction.INCLUDE,
    )
    published = _issue(
        4,
        row,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        resolved=True,
        projection_applied=True,
    )

    source_item = EditionRepairReadService._item_from_issue(source, row)
    projection_item = EditionRepairReadService._item_from_issue(pending_projection, row)
    synthesis_item = EditionRepairReadService._item_from_issue(pending_synthesis, no_document)
    published_item = EditionRepairReadService._item_from_issue(published, row)

    assert source_item is not None
    assert projection_item is not None
    assert synthesis_item is not None
    assert published_item is not None
    assert source_item.recommended_stage == "rebuild_references"
    assert projection_item.recommended_stage == "apply_projection"
    assert synthesis_item.recommended_stage == "synthesis"
    assert published_item.recommended_stage == "none"


class _NoBlobReadStore:
    async def read_repair_evidence(self, _blob_id: UUID) -> dict[str, object]:
        raise AssertionError("the Repair Desk list must not read the evidence blob")


class _LightIssueUow:
    def __init__(self, artifact: object, run: object) -> None:
        self.subject_production_runs = SimpleNamespace(
            list_for_edition=lambda _edition_id: _async_value([run])
        )
        self.production_artifacts = SimpleNamespace(
            get_current=lambda _run_id, _stage: _async_value(artifact)
        )
        self.production_repair_decisions = SimpleNamespace(
            effective_decisions=lambda _edition_id, _subject_id=None: _async_value(())
        )

    async def __aenter__(self) -> _LightIssueUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_light_issue_listing_uses_compact_index_without_blob_read() -> None:
    run = SimpleNamespace(id=uuid4(), subject_id=SUBJECT_A, pipeline_generation=2)
    value = "rule body"
    artifact = SimpleNamespace(
        id=uuid4(),
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "repair_evidence": {
                "blob_id": str(uuid4()),
                "index": [
                    {
                        "source_id": "S1",
                        "source_url": "https://example.test/rule",
                        "proposal_kind": "rule",
                        "artifact_type": "sigma",
                        "reason_code": "source_rule_evidence_missing",
                        "value_hash": hashlib.sha256(value.encode()).hexdigest(),
                        "preview": value,
                    }
                ],
            }
        },
    )
    uow = _LightIssueUow(artifact, run)
    service = ProductionRepairIssueService(
        lambda: uow, _NoBlobReadStore()  # type: ignore[arg-type]
    )

    issues = await service.list_issue_views(EDITION_ID, SUBJECT_A)

    assert len(issues) == 1
    assert issues[0].payload_available
    assert issues[0].preview == value


@pytest.mark.asyncio
async def test_source_issue_listing_uses_compact_reference_index_without_blob_read() -> None:
    source_url = "https://example.test/proposed"
    run = SimpleNamespace(
        id=uuid4(),
        subject_id=SUBJECT_A,
        pipeline_generation=2,
    )
    artifact = SimpleNamespace(
        id=uuid4(),
        version=1,
        raw_blob_id=None,
        canonical_blob_id=None,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "repair_source_index": {
                "proposed": [
                    {
                        "source_id": "S1",
                        "source_title": "Proposed report",
                        "source_url": source_url,
                        "publisher": "Publisher",
                    }
                ],
                "canonical": [],
            }
        },
    )
    collection = SimpleNamespace(
        id=uuid4(),
        canonical_url=source_url,
        state=CollectionState.FAILED_RETRYABLE,
        error_reason="collector_failed",
        attempt_count=1,
    )

    class _SourceUow:
        subject_production_runs = SimpleNamespace(
            list_for_edition=lambda _edition_id: _async_value([run])
        )
        production_artifacts = SimpleNamespace(
            get_current=lambda _run_id, _stage: _async_value(artifact)
        )
        source_collections = SimpleNamespace(
            list_for_subject=lambda _subject_id: _async_value([collection])
        )
        production_repair_decisions = SimpleNamespace(
            effective_decisions=lambda _edition_id, _subject_id=None: _async_value(())
        )

        async def __aenter__(self) -> _SourceUow:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    issues = await ProductionRepairIssueService(
        lambda: _SourceUow(), _NoBlobReadStore()  # type: ignore[arg-type]
    ).list_supplemental_source_issues(EDITION_ID, SUBJECT_A)

    assert len(issues) == 1
    assert issues[0].source_url == source_url


class _BulkArtifacts:
    def __init__(self, artifacts: dict[UUID, ProductionArtifact]) -> None:
        self.artifacts = artifacts

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return self.artifacts.get(artifact_id)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        return next(
            (
                artifact
                for artifact in self.artifacts.values()
                if artifact.production_run_id == run_id
                and artifact.stage.value == stage
                and artifact.status is not ProductionArtifactStatus.STALE
            ),
            None,
        )


class _BulkDecisions:
    def __init__(self) -> None:
        self.items: list[object] = []

    async def effective_decisions(
        self, _edition_id: UUID, _subject_id: UUID | None = None
    ) -> tuple[object, ...]:
        return ()

    async def append(self, decision: object) -> None:
        self.items.append(decision)


class _BulkUow:
    def __init__(
        self,
        runs: dict[UUID, SubjectProductionRun],
        artifacts: dict[UUID, ProductionArtifact],
        edition_status: EditionStatus = EditionStatus.REVIEW,
    ) -> None:
        self.editions = SimpleNamespace(
            get_for_update=lambda _edition_id: _async_value(
                SimpleNamespace(status=edition_status)
            )
        )
        self.subject_production_runs = SimpleNamespace(
            get_for_update=lambda run_id: _async_value(runs.get(run_id))
        )
        self.production_artifacts = _BulkArtifacts(artifacts)
        self.production_repair_decisions = _BulkDecisions()
        self.commits = 0

    async def __aenter__(self) -> _BulkUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def _bulk_case(
    status_b: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED,
    edition_status: EditionStatus = EditionStatus.REVIEW,
) -> tuple[_BulkUow, list[ProductionRepairDecisionInput]]:
    runs: dict[UUID, SubjectProductionRun] = {}
    artifacts: dict[UUID, ProductionArtifact] = {}
    inputs: list[ProductionRepairDecisionInput] = []
    for index, artifact_status in enumerate(
        (ProductionArtifactStatus.VERIFIED, status_b),
        start=1,
    ):
        subject_id = SUBJECT_A if index == 1 else SUBJECT_B
        run = SubjectProductionRun(
            subject_id=subject_id,
            edition_id=EDITION_ID,
            status=SubjectProductionStatus.READY,
            current_stage=SubjectProductionStage.EXTRACTION,
        )
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash="a" * 64,
            status=artifact_status,
        )
        runs[run.id] = run
        artifacts[artifact.id] = artifact
        inputs.append(
            ProductionRepairDecisionInput(
                subject_id=subject_id,
                production_run_id=run.id,
                observed_artifact_id=artifact.id,
                observed_pipeline_generation=run.pipeline_generation,
                repair_key=f"{index + 10:064x}",
                issue_kind=ProductionRepairIssueKind.REJECTED_RULE,
                action=ProductionRepairAction.EXCLUDE,
            )
        )
    return _BulkUow(runs, artifacts, edition_status), inputs


@pytest.mark.asyncio
async def test_bulk_repair_decision_is_single_commit_and_rolls_back_on_stale_item() -> None:
    uow, inputs = _bulk_case()
    events = await ProductionRepairDecisionService(lambda: uow).decide_bulk(
        edition_id=EDITION_ID,
        decisions=inputs,
        actor_id="analyst",
        reason="reviewed as a batch",
    )
    assert len(events) == 2
    assert len(uow.production_repair_decisions.items) == 2
    assert uow.commits == 1

    stale_uow, stale_inputs = _bulk_case(ProductionArtifactStatus.STALE)
    with pytest.raises(ValueError, match="production_repair_stale"):
        await ProductionRepairDecisionService(lambda: stale_uow).decide_bulk(
            edition_id=EDITION_ID,
            decisions=stale_inputs,
            actor_id="analyst",
        )
    assert stale_uow.production_repair_decisions.items == []
    assert stale_uow.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edition_status",
    [EditionStatus.ASSEMBLING, EditionStatus.PUBLISHED, EditionStatus.ARCHIVED],
)
async def test_frozen_edition_repair_desk_stays_readable(
    edition_status: EditionStatus,
) -> None:
    """A historical review shows its real queue; only writes are frozen."""
    row = _row(SUBJECT_A, 1)
    service = EditionRepairReadService(
        _ReadModelFactory(_ReadModelUow([row], edition_status)),  # type: ignore[arg-type]
        _IssueReader([_issue(index, row) for index in range(250)]),
    )

    collected = []
    page = await service.list(EDITION_ID, status="all", limit=100)
    collected.extend(page.items)
    while page.next_cursor is not None:
        page = await service.list(
            EDITION_ID, status="all", cursor=page.next_cursor, limit=100
        )
        collected.extend(page.items)

    assert len(collected) == 250
    assert page.summary.rejected_iocs_to_review == 250


@pytest.mark.asyncio
async def test_frozen_edition_still_refuses_a_repair_decision() -> None:
    """Read policy and write policy are separate: the freeze only blocks writes."""
    uow, inputs = _bulk_case(edition_status=EditionStatus.PUBLISHED)

    with pytest.raises(ValueError, match="edition_frozen_for_publication"):
        await ProductionRepairDecisionService(lambda: uow).decide_bulk(
            edition_id=EDITION_ID,
            decisions=inputs,
            actor_id="analyst",
        )

    assert uow.production_repair_decisions.items == []
    assert uow.commits == 0


@pytest.mark.asyncio
async def test_repair_items_expose_the_application_state_of_their_decision() -> None:
    """"Decided" and "materialized" are two different facts for the audit."""
    row = _row(SUBJECT_A, 1)
    issues = [
        _issue(1, row),
        _issue(
            2,
            row,
            resolved=True,
            action=ProductionRepairAction.INCLUDE,
        ),
        _issue(
            3,
            row,
            resolved=True,
            action=ProductionRepairAction.INCLUDE,
            projection_applied=True,
        ),
    ]
    read_service = EditionRepairReadService(
        _ReadModelFactory(_ReadModelUow([row])), _IssueReader(issues)  # type: ignore[arg-type]
    )
    application = FastAPI()
    application.include_router(router)
    application.state.edition_repair_read_service = read_service

    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/editions/{EDITION_ID}/review/repairs", params={"status": "all"}
        )

    assert response.status_code == 200
    assert [item["application_state"] for item in response.json()["items"]] == [
        "unresolved",
        "projection_required",
        "already_effective",
    ]
