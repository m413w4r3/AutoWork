from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Any, TypedDict, cast
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.publication import router
from cti_app.application.edition_review import (
    EditionReviewReadItem,
    EditionReviewService,
    ReviewItemStaleError,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.model_runs import ModelSubmissionState
from cti_app.domain.production import (
    PRODUCTION_RECONCILIATION_ERROR_CODE,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
)
from cti_app.domain.publication_review import PublicationDecision, PublicationReviewDecision

EDITION_ID = UUID("11111111-1111-4111-8111-111111111111")
SUBJECT_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
ARTIFACT_ID = UUID("44444444-4444-4444-8444-444444444444")
INPUT_HASH = "a" * 64
MODEL_RUN_ID = UUID("55555555-5555-4555-8555-555555555555")


class _DecisionArguments(TypedDict):
    edition_id: UUID
    subject_id: UUID
    decision: PublicationDecision
    production_run_id: UUID
    pipeline_generation: int
    document_artifact_id: UUID
    document_artifact_version: int
    document_input_hash: str
    actor_id: str


def _edition(status: EditionStatus = EditionStatus.REVIEW) -> Edition:
    from datetime import date

    return Edition(
        id=EDITION_ID,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=1,
        source_profile="test",
        status=status,
    )


def _run(status: SubjectProductionStatus, generation: int = 2) -> SubjectProductionRun:
    from cti_app.domain.production import SubjectProductionStage

    return SubjectProductionRun(
        id=RUN_ID,
        subject_id=SUBJECT_ID,
        edition_id=EDITION_ID,
        status=status,
        current_stage=SubjectProductionStage.ASSEMBLY,
        pipeline_generation=generation,
    )


def _row(
    status: SubjectProductionStatus,
    *,
    artifact_status: ProductionArtifactStatus | None = ProductionArtifactStatus.VERIFIED,
    decision: PublicationDecision | None = None,
    effective_decision_id: UUID | None = None,
    generation: int = 2,
    position: int = 1,
    retry_stage: SubjectProductionStage | None = None,
    error_code: str | None = None,
    reconciliation: ProductionSubmissionReconciliation | None = None,
    rejected_indicator_count: int = 0,
    rejected_rule_count: int = 0,
    published_rule_count: int = 0,
) -> EditionReviewReadItem:
    return EditionReviewReadItem(
        position=position,
        subject_id=SUBJECT_ID,
        title="Sujet de test",
        run_id=RUN_ID,
        pipeline_generation=generation,
        run_status=status,
        document_artifact_id=ARTIFACT_ID if artifact_status is not None else None,
        document_artifact_version=1 if artifact_status is not None else None,
        document_input_hash=INPUT_HASH if artifact_status is not None else None,
        document_artifact_status=artifact_status,
        error_code=error_code or ("failed" if status is SubjectProductionStatus.FAILED else None),
        error_message="Échec public" if status is SubjectProductionStatus.FAILED else None,
        effective_decision=decision,
        effective_decision_id=effective_decision_id,
        retry_stage=retry_stage,
        reconciliation=reconciliation,
        rejected_indicator_count=rejected_indicator_count,
        rejected_rule_count=rejected_rule_count,
        published_rule_count=published_rule_count,
    )


def _reconciliation() -> ProductionSubmissionReconciliation:
    return ProductionSubmissionReconciliation(
        production_run_id=RUN_ID,
        model_run_id=MODEL_RUN_ID,
        stage=SubjectProductionStage.SYNTHESIS,
        bridge_response_id="bridge-1",
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
    )


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None

    async def get_for_update(self, edition_id: UUID) -> Edition | None:
        return await self.get(edition_id)


class _ReadModel:
    def __init__(self, rows: Sequence[EditionReviewReadItem]) -> None:
        self.rows = list(rows)
        self.calls = 0

    async def list_for_edition(self, edition_id: UUID) -> Sequence[EditionReviewReadItem]:
        self.calls += 1
        return self.rows if edition_id == EDITION_ID else []


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.run = run

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.run if run_id == self.run.id else None


class _Artifacts:
    def __init__(self, artifact_status: ProductionArtifactStatus | None) -> None:
        self.artifact = (
            ProductionArtifact(
                id=ARTIFACT_ID,
                production_run_id=RUN_ID,
                subject_id=SUBJECT_ID,
                stage=ProductionArtifactStage.PUBLICATION,
                version=1,
                input_hash=INPUT_HASH,
                status=artifact_status,
            )
            if artifact_status is not None
            else None
        )

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        return self.artifact if run_id == RUN_ID and stage == "publication" else None


class _Decisions:
    def __init__(self) -> None:
        self.items: list[PublicationReviewDecision] = []

    async def append(self, decision: PublicationReviewDecision) -> None:
        self.items.append(decision)


class _Uow:
    def __init__(self, edition: Edition, row: EditionReviewReadItem) -> None:
        self.editions = _Editions(edition)
        self.subject_production_runs = _Runs(_run(row.run_status, row.pipeline_generation))
        self.production_artifacts = _Artifacts(row.document_artifact_status)
        self.edition_review_read_model = _ReadModel([row])
        self.publication_review_decisions = _Decisions()
        self.commits = 0

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self) -> _Uow:
        return self.uow


class _ReleaseRematerializer:
    def __init__(self) -> None:
        self.edition_ids: list[UUID] = []

    async def materialize(self, edition_id: UUID) -> None:
        self.edition_ids.append(edition_id)


async def _review(status: SubjectProductionStatus, **kwargs: Any) -> tuple[Any, _Uow]:
    row = _row(status, **kwargs)
    uow = _Uow(_edition(), row)
    result = await EditionReviewService(cast(Any, _Factory(uow))).get(EDITION_ID)
    return result.items[0], uow


@pytest.mark.asyncio
async def test_review_rules_and_exact_acceptance() -> None:
    item, _ = await _review(SubjectProductionStatus.READY)
    assert (item.included, item.blocking, item.effective_decision) == (
        True,
        False,
        PublicationDecision.INCLUDE,
    )

    item, _ = await _review(SubjectProductionStatus.READY, decision=PublicationDecision.EXCLUDE)
    assert (item.included, item.blocking) == (False, False)

    item, _ = await _review(
        SubjectProductionStatus.READY,
        artifact_status=ProductionArtifactStatus.NEEDS_REVIEW,
    )
    assert (item.included, item.blocking) == (False, True)

    for status in (SubjectProductionStatus.FAILED, SubjectProductionStatus.NEEDS_REVIEW):
        item, _ = await _review(status)
        assert item.blocking is True
        item, _ = await _review(status, decision=PublicationDecision.EXCLUDE)
        assert (item.included, item.blocking) == (False, False)

    for status in (SubjectProductionStatus.QUEUED, SubjectProductionStatus.RUNNING):
        item, _ = await _review(status, artifact_status=None)
        assert item.blocking is True


def test_loss_counters_are_editorial_signals_only() -> None:
    ordinary = _row(SubjectProductionStatus.READY)
    signalled = _row(
        SubjectProductionStatus.READY,
        rejected_indicator_count=7,
        rejected_rule_count=2,
        published_rule_count=3,
    )

    ordinary_review = EditionReviewService.from_rows(EDITION_ID, [ordinary])
    signalled_review = EditionReviewService.from_rows(EDITION_ID, [signalled])

    assert signalled_review.items[0].rejected_indicator_count == 7
    assert signalled_review.items[0].rejected_rule_count == 2
    assert signalled_review.items[0].published_rule_count == 3
    assert (signalled_review.items[0].blocking, signalled_review.can_accept) == (
        ordinary_review.items[0].blocking,
        ordinary_review.can_accept,
    )


@pytest.mark.asyncio
async def test_read_model_preserves_batch_order_and_has_one_call_for_twenty_items() -> None:
    rows = [_row(SubjectProductionStatus.READY, position=position) for position in range(20, 0, -1)]
    uow = _Uow(_edition(), rows[0])
    uow.edition_review_read_model = _ReadModel(rows)
    result = await EditionReviewService(cast(Any, _Factory(uow))).get(EDITION_ID)
    assert [item.position for item in result.items] == list(range(20, 0, -1))
    assert uow.edition_review_read_model.calls == 1


@pytest.mark.asyncio
async def test_decision_is_append_only_and_same_request_appends_a_history_event() -> None:
    row = _row(SubjectProductionStatus.READY)
    uow = _Uow(_edition(), row)
    service = EditionReviewService(cast(Any, _Factory(uow)))
    arguments: _DecisionArguments = {
        "edition_id": EDITION_ID,
        "subject_id": SUBJECT_ID,
        "decision": PublicationDecision.INCLUDE,
        "production_run_id": RUN_ID,
        "pipeline_generation": 2,
        "document_artifact_id": ARTIFACT_ID,
        "document_artifact_version": 1,
        "document_input_hash": INPUT_HASH,
        "actor_id": "analyst-1",
    }
    await service.decide(**arguments)
    await service.decide(**arguments)
    assert len(uow.publication_review_decisions.items) == 2
    assert uow.commits == 2


@pytest.mark.asyncio
async def test_stale_generation_does_not_append() -> None:
    row = _row(SubjectProductionStatus.READY)
    uow = _Uow(_edition(), row)
    with pytest.raises(ReviewItemStaleError):
        await EditionReviewService(cast(Any, _Factory(uow))).decide(
            EDITION_ID,
            SUBJECT_ID,
            decision=PublicationDecision.INCLUDE,
            production_run_id=RUN_ID,
            pipeline_generation=1,
            document_artifact_id=ARTIFACT_ID,
            document_artifact_version=1,
            document_input_hash=INPUT_HASH,
            actor_id="analyst-1",
        )
    assert uow.publication_review_decisions.items == []


@pytest.mark.asyncio
async def test_wrong_edition_status_is_rejected() -> None:
    row = _row(SubjectProductionStatus.READY)
    uow = _Uow(_edition(EditionStatus.PRODUCTION), row)
    with pytest.raises(ValueError, match="edition_must_be_in_review"):
        await EditionReviewService(cast(Any, _Factory(uow))).get(EDITION_ID)


def _api(uow: _Uow) -> FastAPI:
    application = FastAPI()
    application.include_router(router)
    application.state.edition_review_service = EditionReviewService(cast(Any, _Factory(uow)))
    application.state.identity_provider = LocalIdentityProvider("analyst-1")
    return application


@pytest.mark.asyncio
async def test_api_stale_returns_public_409_and_does_not_append() -> None:
    uow = _Uow(_edition(), _row(SubjectProductionStatus.READY))
    async with AsyncClient(
        transport=ASGITransport(app=_api(uow)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{EDITION_ID}/review/items/{SUBJECT_ID}/include",
            json={
                "production_run_id": str(RUN_ID),
                "pipeline_generation": 1,
                "document_artifact_id": str(ARTIFACT_ID),
                "document_artifact_version": 1,
                "document_input_hash": INPUT_HASH,
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "review_item_stale"
    assert uow.publication_review_decisions.items == []


@pytest.mark.asyncio
async def test_api_rematerializes_release_with_verified_identity() -> None:
    uow = _Uow(_edition(), _row(SubjectProductionStatus.READY))
    application = _api(uow)
    rematerializer = _ReleaseRematerializer()
    application.state.edition_release_rematerializer = rematerializer
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.post(f"/api/editions/{EDITION_ID}/release/materialize")

    assert response.status_code == 200
    assert response.json() == {"edition_id": str(EDITION_ID), "materialized": True}
    assert rematerializer.edition_ids == [EDITION_ID]


@pytest.mark.asyncio
async def test_api_exclude_requires_a_non_blank_reason_and_review_is_public() -> None:
    uow = _Uow(_edition(), _row(SubjectProductionStatus.READY))
    async with AsyncClient(
        transport=ASGITransport(app=_api(uow)), base_url="http://test"
    ) as client:
        blank = await client.post(
            f"/api/editions/{EDITION_ID}/review/items/{SUBJECT_ID}/exclude",
            json={
                "production_run_id": str(RUN_ID),
                "pipeline_generation": 2,
                "document_artifact_id": str(ARTIFACT_ID),
                "document_artifact_version": 1,
                "document_input_hash": INPUT_HASH,
                "reason": "  ",
            },
        )
        accepted = await client.post(
            f"/api/editions/{EDITION_ID}/review/items/{SUBJECT_ID}/exclude",
            json={
                "production_run_id": str(RUN_ID),
                "pipeline_generation": 2,
                "document_artifact_id": str(ARTIFACT_ID),
                "document_artifact_version": 1,
                "document_input_hash": INPUT_HASH,
                "reason": "  hors périmètre  ",
            },
        )
    assert blank.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["reason"] == "hors périmètre"


@pytest.mark.asyncio
async def test_api_exclude_without_document_is_allowed_when_run_has_no_document() -> None:
    uow = _Uow(_edition(), _row(SubjectProductionStatus.FAILED, artifact_status=None))
    async with AsyncClient(
        transport=ASGITransport(app=_api(uow)), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/editions/{EDITION_ID}/review/items/{SUBJECT_ID}/exclude",
            json={
                "production_run_id": str(RUN_ID),
                "pipeline_generation": 2,
                "reason": "No publication was produced",
            },
        )
    assert response.status_code == 200
    assert response.json()["document_artifact_id"] is None
    assert response.json()["document_artifact_version"] is None
    assert response.json()["document_input_hash"] is None
    assert len(uow.publication_review_decisions.items) == 1


@pytest.mark.asyncio
async def test_api_separates_include_and_exclude_document_contracts() -> None:
    uow = _Uow(_edition(), _row(SubjectProductionStatus.FAILED, artifact_status=None))
    async with AsyncClient(
        transport=ASGITransport(app=_api(uow)), base_url="http://test"
    ) as client:
        missing_include = await client.post(
            f"/api/editions/{EDITION_ID}/review/items/{SUBJECT_ID}/include",
            json={
                "production_run_id": str(RUN_ID),
                "pipeline_generation": 2,
            },
        )
        partial_exclude = await client.post(
            f"/api/editions/{EDITION_ID}/review/items/{SUBJECT_ID}/exclude",
            json={
                "production_run_id": str(RUN_ID),
                "pipeline_generation": 2,
                "document_artifact_version": 1,
                "reason": "Incomplete identity",
            },
        )
    assert missing_include.status_code == 422
    assert partial_exclude.status_code == 422
    assert uow.publication_review_decisions.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [SubjectProductionStatus.FAILED, SubjectProductionStatus.NEEDS_REVIEW],
)
async def test_exclude_without_document_is_allowed_for_terminal_failed_runs(
    status: SubjectProductionStatus,
) -> None:
    row = _row(status, artifact_status=None)
    uow = _Uow(_edition(), row)
    event = await EditionReviewService(cast(Any, _Factory(uow))).decide(
        EDITION_ID,
        SUBJECT_ID,
        decision=PublicationDecision.EXCLUDE,
        production_run_id=RUN_ID,
        pipeline_generation=2,
        document_artifact_id=None,
        document_artifact_version=None,
        document_input_hash=None,
        actor_id="analyst-1",
        reason="No usable document was produced",
    )
    assert (event.document_artifact_id, event.document_artifact_version) == (None, None)
    assert event.document_input_hash is None
    assert len(uow.publication_review_decisions.items) == 1


@pytest.mark.asyncio
async def test_documentless_exclusion_is_stale_when_a_document_exists() -> None:
    uow = _Uow(_edition(), _row(SubjectProductionStatus.READY))
    with pytest.raises(ReviewItemStaleError):
        await EditionReviewService(cast(Any, _Factory(uow))).decide(
            EDITION_ID,
            SUBJECT_ID,
            decision=PublicationDecision.EXCLUDE,
            production_run_id=RUN_ID,
            pipeline_generation=2,
            document_artifact_id=None,
            document_artifact_version=None,
            document_input_hash=None,
            actor_id="analyst-1",
            reason="No document identity supplied",
        )
    assert uow.publication_review_decisions.items == []


@pytest.mark.asyncio
async def test_review_acceptance_requires_an_included_item() -> None:
    empty_uow = _Uow(_edition(), _row(SubjectProductionStatus.READY))
    empty_uow.edition_review_read_model = _ReadModel([])
    empty_review = await EditionReviewService(cast(Any, _Factory(empty_uow))).get(EDITION_ID)
    assert not empty_review.can_accept

    all_excluded_uow = _Uow(
        _edition(), _row(SubjectProductionStatus.READY, decision=PublicationDecision.EXCLUDE)
    )
    assert not (
        await EditionReviewService(cast(Any, _Factory(all_excluded_uow))).get(EDITION_ID)
    ).can_accept

    included = _row(SubjectProductionStatus.READY)
    failed_excluded = _row(
        SubjectProductionStatus.FAILED,
        artifact_status=None,
        decision=PublicationDecision.EXCLUDE,
        position=2,
    )
    mixed_uow = _Uow(_edition(), included)
    mixed_uow.edition_review_read_model = _ReadModel([included, failed_excluded])
    assert (await EditionReviewService(cast(Any, _Factory(mixed_uow))).get(EDITION_ID)).can_accept


@pytest.mark.asyncio
async def test_review_exposes_effective_decision_id_and_backend_retry_stage() -> None:
    explicit_id = UUID("55555555-5555-4555-8555-555555555555")
    item, _ = await _review(
        SubjectProductionStatus.FAILED,
        artifact_status=None,
        decision=PublicationDecision.EXCLUDE,
        effective_decision_id=explicit_id,
        retry_stage=SubjectProductionStage.SYNTHESIS,
    )
    assert item.effective_decision_id == explicit_id
    assert item.retry_stage is SubjectProductionStage.SYNTHESIS

    ready_item, _ = await _review(
        SubjectProductionStatus.READY, retry_stage=SubjectProductionStage.SYNTHESIS
    )
    assert ready_item.retry_stage is None


@pytest.mark.asyncio
async def test_cancelled_review_item_is_never_offered_a_retry() -> None:
    """The domain refuses retry_from_stage on a cancelled run.

    Offering the action anyway would only produce a conflict, so the read model
    and the domain now agree: a cancelled article is resolved by excluding it.
    """
    item, _ = await _review(
        SubjectProductionStatus.CANCELLED,
        artifact_status=None,
        retry_stage=SubjectProductionStage.SYNTHESIS,
    )

    assert item.can_retry is False
    assert item.retry_stage is None
    assert item.requires_reconciliation is False
    # It still blocks the edition until it is explicitly excluded.
    assert item.blocking is True


@pytest.mark.asyncio
async def test_reconciliation_item_asks_for_recovery_instead_of_a_retry() -> None:
    item, _ = await _review(
        SubjectProductionStatus.NEEDS_REVIEW,
        artifact_status=None,
        retry_stage=SubjectProductionStage.SYNTHESIS,
        error_code=PRODUCTION_RECONCILIATION_ERROR_CODE,
        reconciliation=_reconciliation(),
    )

    assert item.requires_reconciliation is True
    assert item.can_retry is False
    assert item.retry_stage is None
    assert item.reconciliation is not None
    # The frontend addresses the exact ModelRun, never a parsed message.
    assert item.reconciliation.model_run_id == MODEL_RUN_ID
    assert item.reconciliation.bridge_response_id == "bridge-1"


@pytest.mark.asyncio
async def test_an_ordinary_needs_review_item_stays_retryable() -> None:
    item, _ = await _review(
        SubjectProductionStatus.NEEDS_REVIEW,
        artifact_status=None,
        retry_stage=SubjectProductionStage.SYNTHESIS,
        error_code="synthesis_error",
    )

    assert (item.can_retry, item.requires_reconciliation) == (True, False)
    assert item.retry_stage is SubjectProductionStage.SYNTHESIS
    assert item.reconciliation is None
