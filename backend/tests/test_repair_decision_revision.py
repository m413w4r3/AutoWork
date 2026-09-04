"""LOT 25 — one adjudication policy, revisable decisions, honest projections.

Every endpoint that records a repair decision must apply the same business
rule, a decision must be revisable without any UPDATE, and "applied" must mean
the content is really materialized in the current projection.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.production import router as production_router
from cti_app.api.publication import router as publication_router
from cti_app.application.edition_review import (
    EditionRepairReadService,
    EditionReviewReadItem,
    EditionReviewService,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    TechnicalExtraction,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_repairs import (
    ProductionRepairAdjudicationRequest,
    ProductionRepairAdjudicationService,
    ProductionRepairDecisionChangedError,
    ProductionRepairDecisionNoopError,
    ProductionRepairDecisionService,
    ProductionRepairIssueService,
    ProductionRepairProjectionService,
    ProductionRepairValueNotVerifiableError,
    build_repair_evidence_pack,
    repair_decision_application_state,
    repair_key_for_rejection,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    RepairDecisionApplicationState,
    SubjectProductionStatus,
)

EDITION_ID = UUID("aaaaaaaa-9999-4999-8999-aaaaaaaaaaaa")
SUBJECT_ID = UUID("bbbbbbbb-9999-4999-8999-bbbbbbbbbbbb")
RUN_ID = UUID("cccccccc-9999-4999-8999-cccccccccccc")
SOURCE_URL = "https://lot25.example/report"
GOOD_VALUE = "good.lot25-desk.com"
MALFORMED_VALUE = "not a domain at all"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _key(value: str) -> str:
    return repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_URL,
        artifact_type="domain",
        value=value,
    )


GOOD_KEY = _key(GOOD_VALUE)
MALFORMED_KEY = _key(MALFORMED_VALUE)


# ---------------------------------------------------------------------------
# In-memory infrastructure
# ---------------------------------------------------------------------------


class _BlobCatalog:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}
        self.addresses: dict[tuple[str, str], UUID] = {}

    async def ingest(self, source: Any, *, logical_bucket: str, mime_type: str) -> Any:
        del mime_type
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        blob_id = self.addresses.setdefault((logical_bucket, digest), uuid4())
        self.contents[blob_id] = content
        return SimpleNamespace(id=blob_id, descriptor=SimpleNamespace(sha256=digest))

    async def read(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        del max_bytes
        return self.contents[blob_id]


class _Artifacts:
    def __init__(self, items: list[ProductionArtifact]) -> None:
        self.items = list(items)

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return next((item for item in self.items if item.id == artifact_id), None)

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        values = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(values, key=lambda item: item.version) if values else None

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def list_current_for_edition(
        self, _edition_id: UUID, stage: str
    ) -> list[ProductionArtifact]:
        by_run: dict[UUID, ProductionArtifact] = {}
        for item in self.items:
            if item.stage.value != stage or item.status is ProductionArtifactStatus.STALE:
                continue
            current = by_run.get(item.production_run_id)
            if current is None or item.version > current.version:
                by_run[item.production_run_id] = item
        return list(by_run.values())

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        order = ["references", "extraction", "synthesis", "publication"]
        downstream = set(order[order.index(stage) + 1 :]) if stage in order else set()
        for index, item in enumerate(self.items):
            if item.production_run_id == run_id and item.stage.value in downstream:
                self.items[index] = replace(item, status=ProductionArtifactStatus.STALE)


class _Decisions:
    """Append-only log: no update, no delete, last write per key wins."""

    def __init__(self) -> None:
        self.history: list[ProductionRepairDecision] = []

    async def append(self, decision: ProductionRepairDecision) -> None:
        self.history.append(decision)

    async def list_for_edition(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> list[ProductionRepairDecision]:
        return sorted(
            (
                decision
                for decision in self.history
                if decision.edition_id == edition_id
                and (subject_id is None or decision.subject_id == subject_id)
            ),
            key=lambda item: (item.created_at, str(item.id)),
        )

    async def effective_decisions(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        latest: dict[tuple[UUID, str], ProductionRepairDecision] = {}
        for decision in await self.list_for_edition(edition_id, subject_id):
            latest[(decision.subject_id, decision.repair_key)] = decision
        return tuple(latest.values())


class _Uow:
    def __init__(self, state: _State) -> None:
        self._state = state
        self.production_artifacts = state.artifacts
        self.production_repair_decisions = state.decisions
        self.subject_production_runs = SimpleNamespace(
            get=self._get_run,
            get_for_update=self._get_run,
            get_current_for_subject=self._current_run,
            list_for_edition=self._runs_for_edition,
        )
        self.editions = SimpleNamespace(get=self._edition, get_for_update=self._edition)
        self.publication_manifests = SimpleNamespace(get_latest_for_edition=self._no_manifest)
        self.edition_review_read_model = SimpleNamespace(
            list_for_edition=self._rows,
        )
        self.source_collections = SimpleNamespace(
            list_for_subject=self._collections,
            list_for_subjects=self._collections_bulk,
        )

    async def _get_run(self, run_id: UUID) -> Any:
        return self._state.run if run_id == self._state.run.id else None

    async def _current_run(self, subject_id: UUID) -> Any:
        return self._state.run if subject_id == SUBJECT_ID else None

    async def _runs_for_edition(self, edition_id: UUID) -> list[Any]:
        return [self._state.run] if edition_id == EDITION_ID else []

    async def _edition(self, _edition_id: UUID) -> Edition:
        return _edition()

    async def _no_manifest(self, _edition_id: UUID) -> None:
        return None

    async def _rows(self, _edition_id: UUID) -> list[EditionReviewReadItem]:
        return [self._state.row]

    async def _collections(self, _subject_id: UUID) -> list[Any]:
        return []

    async def _collections_bulk(self, _subject_ids: Any) -> list[Any]:
        return []

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self._state.commits += 1


class _State:
    def __init__(
        self,
        artifacts: list[ProductionArtifact],
        run: Any,
        row: EditionReviewReadItem,
    ) -> None:
        self.artifacts = _Artifacts(artifacts)
        self.decisions = _Decisions()
        self.run = run
        self.row = row
        self.commits = 0

    def factory(self) -> Any:
        return lambda: _Uow(self)


def _edition() -> Edition:
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
        status=EditionStatus.REVIEW,
    )


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id=RUN_ID,
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        status=SubjectProductionStatus.READY,
        requires_reconciliation=False,
        pipeline_generation=0,
        research_date=date(2026, 8, 15),
    )


def _row() -> EditionReviewReadItem:
    return EditionReviewReadItem(
        position=1,
        subject_id=SUBJECT_ID,
        title="Article 1",
        run_id=RUN_ID,
        pipeline_generation=0,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=uuid4(),
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=None,
    )


def _entry(value: str, *, reason_code: str = "source_evidence_missing") -> dict[str, Any]:
    return {
        "source_id": "S1",
        "source_title": "Report S1",
        "source_url": SOURCE_URL,
        "proposal_kind": "artifact",
        "artifact_type": "domain",
        "reason_code": reason_code,
        "value": value,
        "value_sha256": _sha256(value),
        "model_run_id": "model-run-1",
    }


async def _extraction(
    store: ProductionArtifactStore, entries: list[dict[str, Any]]
) -> ProductionArtifact:
    evidence_id = await store.put_repair_evidence(build_repair_evidence_pack(entries))
    canonical_id = await store.put_json(
        technical_extraction_to_json(TechnicalExtraction(items=(), rules=())),
        bucket="production-artifacts-canonical",
    )
    return ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="b" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_id,
        metadata={
            "repair_evidence": {
                "schema_version": "1",
                "blob_id": str(evidence_id),
                "entry_count": len(entries),
                "index": [
                    {
                        key: entry[key]
                        for key in (
                            "source_id",
                            "source_title",
                            "source_url",
                            "proposal_kind",
                            "artifact_type",
                            "reason_code",
                            "value_sha256",
                        )
                    }
                    | {"preview": entry["value"]}
                    for entry in entries
                ],
            }
        },
    )


async def _desk(entries: list[dict[str, Any]]) -> tuple[_State, ProductionArtifactStore]:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    artifact = await _extraction(store, entries)
    return _State([artifact], _run(), _row()), store


def _application(state: _State, store: ProductionArtifactStore) -> FastAPI:
    application = FastAPI()
    application.include_router(publication_router)
    application.include_router(production_router)
    factory = state.factory()
    issue_service = ProductionRepairIssueService(factory, store)
    application.state.uow_factory = factory
    application.state.production_artifact_store = store
    application.state.production_repair_issue_service = issue_service
    application.state.edition_repair_read_service = EditionRepairReadService(factory, issue_service)
    application.state.edition_review_service = EditionReviewService(factory, issue_service)
    application.state.identity_provider = SimpleNamespace(
        current=lambda: _value(SimpleNamespace(actor_id="analyst"))
    )
    application.state.job_service = None
    application.state.job_dispatcher = None
    return application


async def _value(value: Any) -> Any:
    return value


def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


def _adjudication(
    state: _State, store: ProductionArtifactStore
) -> ProductionRepairAdjudicationService:
    factory = state.factory()
    return ProductionRepairAdjudicationService(
        factory, ProductionRepairIssueService(factory, store)
    )


def _artifact_id(state: _State) -> UUID:
    return state.artifacts.items[0].id


# ---------------------------------------------------------------------------
# A & B — the unbuildable INCLUDE is refused by BOTH routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_subject_route_refuses_an_include_the_projection_cannot_rebuild() -> None:
    state, store = await _desk([_entry(MALFORMED_VALUE, reason_code="normalization_error")])
    async with _client(_application(state, store)) as client:
        response = await client.post(
            f"/api/subjects/{SUBJECT_ID}/production/repairs/{MALFORMED_KEY}/decision",
            json={
                "action": "include",
                "observed_artifact_id": str(_artifact_id(state)),
                "observed_pipeline_generation": 0,
                "expected_effective_decision_id": None,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (ProductionRepairValueNotVerifiableError.code)
    assert state.decisions.history == []


@pytest.mark.asyncio
async def test_b_edition_route_refuses_the_same_include_identically() -> None:
    state, store = await _desk([_entry(MALFORMED_VALUE, reason_code="normalization_error")])
    async with _client(_application(state, store)) as client:
        response = await client.post(
            f"/api/editions/{EDITION_ID}/review/repairs/{MALFORMED_KEY}/decision",
            json={
                "action": "include",
                "observed_subject_id": str(SUBJECT_ID),
                "observed_run_id": str(RUN_ID),
                "observed_artifact_id": str(_artifact_id(state)),
                "observed_pipeline_generation": 0,
                "expected_effective_decision_id": None,
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (ProductionRepairValueNotVerifiableError.code)
    assert state.decisions.history == []


@pytest.mark.asyncio
async def test_ab_both_routes_share_one_adjudication_service() -> None:
    """The two endpoints resolve the same service instance, not two policies."""
    from cti_app.api.production import _production_repair_adjudication_service
    from cti_app.api.publication import _repair_adjudication_service

    state, store = await _desk([_entry(GOOD_VALUE)])
    application = _application(state, store)
    shared = _adjudication(state, store)
    application.state.production_repair_adjudication_service = shared
    request = SimpleNamespace(app=application)

    assert _repair_adjudication_service(request) is shared  # type: ignore[arg-type]
    assert (
        _production_repair_adjudication_service(request) is shared  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# C — one impossible item rolls the whole batch back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_c_bulk_rolls_back_entirely_when_one_include_is_impossible() -> None:
    values = [f"host-{index:03d}.lot25-desk.com" for index in range(19)]
    entries = [_entry(value) for value in values]
    entries.append(_entry(MALFORMED_VALUE, reason_code="normalization_error"))
    state, store = await _desk(entries)
    decisions = [
        {
            "repair_key": _key(value),
            "action": "include",
            "observed_subject_id": str(SUBJECT_ID),
            "observed_run_id": str(RUN_ID),
            "observed_artifact_id": str(_artifact_id(state)),
            "observed_pipeline_generation": 0,
            "expected_effective_decision_id": None,
        }
        for value in [*values, MALFORMED_VALUE]
    ]

    async with _client(_application(state, store)) as client:
        response = await client.post(
            f"/api/editions/{EDITION_ID}/review/repairs/decisions",
            json={"decisions": decisions},
        )

    assert response.status_code == 409
    body = response.json()["detail"]
    assert body["code"] == ProductionRepairValueNotVerifiableError.code
    assert body["repair_key"] == MALFORMED_KEY
    # Nothing was appended and nothing was committed.
    assert state.decisions.history == []
    assert state.commits == 0


# ---------------------------------------------------------------------------
# D, E, J — decisions are revisable, retries are unambiguous
# ---------------------------------------------------------------------------


async def _decide(
    client: AsyncClient,
    state: _State,
    *,
    action: str,
    expected: str | None,
    repair_key: str = GOOD_KEY,
) -> Any:
    return await client.post(
        f"/api/editions/{EDITION_ID}/review/repairs/{repair_key}/decision",
        json={
            "action": action,
            "observed_subject_id": str(SUBJECT_ID),
            "observed_run_id": str(RUN_ID),
            "observed_artifact_id": str(_artifact_id(state)),
            "observed_pipeline_generation": 0,
            "expected_effective_decision_id": expected,
        },
    )


@pytest.mark.asyncio
async def test_d_include_can_be_revised_to_exclude_with_a_two_entry_audit() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    async with _client(_application(state, store)) as client:
        first = await _decide(client, state, action="include", expected=None)
        assert first.status_code == 200
        include_id = first.json()["decision_id"]

        second = await _decide(client, state, action="exclude", expected=include_id)
        assert second.status_code == 200

        detail = await client.get(f"/api/editions/{EDITION_ID}/review/repairs/{GOOD_KEY}")

    body = detail.json()
    assert [item["action"] for item in body["decision_history"]] == [
        "include",
        "exclude",
    ]
    assert body["effective_decision"]["action"] == "exclude"
    assert body["effective_decision"]["id"] == second.json()["decision_id"]
    assert len(state.decisions.history) == 2


@pytest.mark.asyncio
async def test_e_exclude_can_be_revised_back_to_include() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    async with _client(_application(state, store)) as client:
        first = await _decide(client, state, action="exclude", expected=None)
        second = await _decide(
            client, state, action="include", expected=first.json()["decision_id"]
        )
        detail = await client.get(f"/api/editions/{EDITION_ID}/review/repairs/{GOOD_KEY}")

    assert second.status_code == 200
    body = detail.json()
    assert [item["action"] for item in body["decision_history"]] == [
        "exclude",
        "include",
    ]
    assert body["effective_decision"]["action"] == "include"


@pytest.mark.asyncio
async def test_f_optimistic_fence_refuses_the_second_writer() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    async with _client(_application(state, store)) as client:
        first = await _decide(client, state, action="include", expected=None)
        d1 = first.json()["decision_id"]

        # Both clients read D1; A revises it, B tries to revise the same D1.
        client_a = await _decide(client, state, action="exclude", expected=d1)
        client_b = await _decide(client, state, action="include", expected=d1)

    assert client_a.status_code == 200
    assert client_b.status_code == 409
    assert client_b.json()["detail"]["code"] == (ProductionRepairDecisionChangedError.code)
    assert len(state.decisions.history) == 2
    assert state.decisions.history[-1].id == UUID(client_a.json()["decision_id"])


@pytest.mark.asyncio
async def test_j_replaying_the_same_request_never_appends_twice() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    async with _client(_application(state, store)) as client:
        first = await _decide(client, state, action="include", expected=None)
        retry = await _decide(client, state, action="include", expected=None)

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == ProductionRepairDecisionNoopError.code
    assert len(state.decisions.history) == 1


@pytest.mark.asyncio
async def test_j_subject_route_rejects_a_same_action_revision_without_appending() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    async with _client(_application(state, store)) as client:
        first = await client.post(
            f"/api/subjects/{SUBJECT_ID}/production/repairs/{GOOD_KEY}/decision",
            json={
                "action": "include",
                "observed_artifact_id": str(_artifact_id(state)),
                "observed_pipeline_generation": 0,
                "expected_effective_decision_id": None,
            },
        )
        retry = await client.post(
            f"/api/subjects/{SUBJECT_ID}/production/repairs/{GOOD_KEY}/decision",
            json={
                "action": "include",
                "observed_artifact_id": str(_artifact_id(state)),
                "observed_pipeline_generation": 0,
                "expected_effective_decision_id": first.json()["decision_id"],
            },
        )

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == ProductionRepairDecisionNoopError.code
    assert len(state.decisions.history) == 1


@pytest.mark.asyncio
async def test_decision_service_rejects_same_action_under_the_decision_lock() -> None:
    state, _store = await _desk([_entry(GOOD_VALUE)])
    existing = _decision(
        GOOD_KEY,
        ProductionRepairAction.INCLUDE,
        artifact_id=_artifact_id(state),
        when=datetime.now(UTC),
    )
    await state.decisions.append(existing)
    service = ProductionRepairDecisionService(state.factory())

    with pytest.raises(ProductionRepairDecisionNoopError):
        await service.decide(
            edition_id=EDITION_ID,
            subject_id=SUBJECT_ID,
            production_run_id=RUN_ID,
            observed_artifact_id=_artifact_id(state),
            observed_pipeline_generation=0,
            repair_key=GOOD_KEY,
            issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
            action=ProductionRepairAction.INCLUDE,
            actor_id="analyst",
            expected_effective_decision_id=existing.id,
        )

    assert state.decisions.history == [existing]


@pytest.mark.asyncio
async def test_db_contract_only_ever_appends() -> None:
    """The revision path performs no UPDATE and no DELETE."""
    state, store = await _desk([_entry(GOOD_VALUE)])
    async with _client(_application(state, store)) as client:
        first = await _decide(client, state, action="include", expected=None)
        await _decide(client, state, action="exclude", expected=first.json()["decision_id"])

    assert len(state.decisions.history) == 2
    assert state.decisions.history[0].action is ProductionRepairAction.INCLUDE
    assert state.decisions.history[0].id == UUID(first.json()["decision_id"])
    # The first event is byte-for-byte the one that was appended.
    assert state.decisions.history[0].actor_id == "analyst"
    assert not hasattr(state.decisions, "update")
    assert not hasattr(state.decisions, "delete")


# ---------------------------------------------------------------------------
# G & H — a revision produces the projection that makes it true
# ---------------------------------------------------------------------------


def _decision(
    repair_key: str,
    action: ProductionRepairAction,
    *,
    artifact_id: UUID,
    when: datetime,
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=artifact_id,
        observed_pipeline_generation=0,
        repair_key=repair_key,
        issue_kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        action=action,
        actor_id="analyst",
        created_at=when,
    )


@pytest.mark.asyncio
async def test_g_exclude_after_include_builds_a_projection_without_the_value() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    base_id = _artifact_id(state)
    projection = ProductionRepairProjectionService(state.factory(), store)
    now = datetime.now(UTC)

    include = _decision(GOOD_KEY, ProductionRepairAction.INCLUDE, artifact_id=base_id, when=now)
    await state.decisions.append(include)
    first = await projection.project_effective_extraction(RUN_ID, actor_id="analyst")
    projected = technical_extraction_from_json(
        await store.read_json(first.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert [item.value for item in projected.items] == [GOOD_VALUE]
    marker = first.artifact.metadata["repair_projection"]
    assert marker["applied_decisions"] == [
        {
            "repair_key": GOOD_KEY,
            "decision_id": str(include.id),
            "action": "include",
        }
    ]
    assert marker["unbuildable_decisions"] == []

    exclude = _decision(
        GOOD_KEY,
        ProductionRepairAction.EXCLUDE,
        artifact_id=base_id,
        when=now + timedelta(minutes=1),
    )
    await state.decisions.append(exclude)
    second = await projection.project_effective_extraction(RUN_ID, actor_id="analyst")

    assert second.changed
    assert second.artifact.version > first.artifact.version
    removed = technical_extraction_from_json(
        await store.read_json(second.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert [item.value for item in removed.items] == []
    assert second.artifact.metadata["repair_projection"]["applied_decisions"] == [
        {
            "repair_key": GOOD_KEY,
            "decision_id": str(exclude.id),
            "action": "exclude",
        }
    ]


@pytest.mark.asyncio
async def test_h_the_revision_is_reported_as_a_rebuild_debt_then_cleared() -> None:
    """A revised EXCLUDE owes a projection; once applied, nothing is owed."""
    state, store = await _desk([_entry(GOOD_VALUE)])
    base_id = _artifact_id(state)
    factory = state.factory()
    issues = ProductionRepairIssueService(factory, store)
    read_service = EditionRepairReadService(factory, issues)
    projection = ProductionRepairProjectionService(factory, store)
    now = datetime.now(UTC)

    await state.decisions.append(
        _decision(GOOD_KEY, ProductionRepairAction.INCLUDE, artifact_id=base_id, when=now)
    )
    await projection.project_effective_extraction(RUN_ID, actor_id="analyst")
    applied = (await read_service.list(EDITION_ID, status="all")).items[0]
    assert (applied.rebuild_required, applied.recommended_stage) == (False, "none")

    await state.decisions.append(
        _decision(
            GOOD_KEY,
            ProductionRepairAction.EXCLUDE,
            artifact_id=base_id,
            when=now + timedelta(minutes=1),
        )
    )
    owed = (await read_service.list(EDITION_ID, status="all")).items[0]
    assert (owed.rebuild_required, owed.recommended_stage) == (True, "apply_projection")
    owed_review = EditionReviewService.from_rows(
        EDITION_ID,
        [state.row],
        repair_issues=[(await issues.list_issue_views(EDITION_ID, SUBJECT_ID))[0]],
    )
    assert (owed_review.pending_rebuild_count, owed_review.can_accept) == (1, False)

    await projection.project_effective_extraction(RUN_ID, actor_id="analyst")
    cleared = (await read_service.list(EDITION_ID, status="all")).items[0]
    assert (cleared.rebuild_required, cleared.recommended_stage) == (False, "none")
    cleared_review = EditionReviewService.from_rows(
        EDITION_ID,
        [state.row],
        repair_issues=[(await issues.list_issue_views(EDITION_ID, SUBJECT_ID))[0]],
    )
    assert (cleared_review.pending_rebuild_count, cleared_review.can_accept) == (0, True)


# ---------------------------------------------------------------------------
# Part 5 — the pure application-state helper
# ---------------------------------------------------------------------------


def _issue(repair_key: str = GOOD_KEY) -> SimpleNamespace:
    return SimpleNamespace(repair_key=repair_key, kind=ProductionRepairIssueKind.REJECTED_INDICATOR)


def _marker(action: str, decision_id: str) -> dict[str, Any]:
    return {
        "applied_decisions": [
            {
                "repair_key": GOOD_KEY,
                "decision_id": decision_id,
                "action": action,
            }
        ],
        "unbuildable_decisions": [],
    }


def test_application_state_covers_every_indispensable_case() -> None:
    artifact_id = uuid4()
    now = datetime.now(UTC)
    include = _decision(GOOD_KEY, ProductionRepairAction.INCLUDE, artifact_id=artifact_id, when=now)
    exclude = _decision(GOOD_KEY, ProductionRepairAction.EXCLUDE, artifact_id=artifact_id, when=now)
    state = repair_decision_application_state

    # No decision at all.
    assert state(_issue(), None, None) is RepairDecisionApplicationState.UNRESOLVED
    # Base rejected the value and nothing was projected.
    assert state(_issue(), exclude, None) is RepairDecisionApplicationState.ALREADY_EFFECTIVE
    assert state(_issue(), include, None) is RepairDecisionApplicationState.PROJECTION_REQUIRED
    # A previous projection included it: the new EXCLUDE owes a projection.
    assert (
        state(_issue(), exclude, _marker("include", str(uuid4())))
        is RepairDecisionApplicationState.PROJECTION_REQUIRED
    )
    # A previous projection excluded it: the new INCLUDE owes one too.
    assert (
        state(_issue(), include, _marker("exclude", str(uuid4())))
        is RepairDecisionApplicationState.PROJECTION_REQUIRED
    )
    # The very decision id is already materialized.
    assert (
        state(_issue(), include, _marker("include", str(include.id)))
        is RepairDecisionApplicationState.ALREADY_EFFECTIVE
    )
    # Recorded but impossible to rebuild.
    unbuildable = {
        "applied_decisions": [],
        "unbuildable_decisions": [{"repair_key": GOOD_KEY, "decision_id": str(include.id)}],
    }
    assert state(_issue(), include, unbuildable) is RepairDecisionApplicationState.UNBUILDABLE


# ---------------------------------------------------------------------------
# I — a legacy impossible INCLUDE stays blocking and stays revisable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_i_legacy_unbuildable_include_never_counts_as_applied() -> None:
    state, store = await _desk([_entry(MALFORMED_VALUE, reason_code="normalization_error")])
    base_id = _artifact_id(state)
    factory = state.factory()
    issues = ProductionRepairIssueService(factory, store)
    read_service = EditionRepairReadService(factory, issues)
    now = datetime.now(UTC)

    # Injected the way corrupted/legacy data would exist: the endpoints refuse
    # to create this, but the desk must still be able to clean it up.
    legacy = _decision(MALFORMED_KEY, ProductionRepairAction.INCLUDE, artifact_id=base_id, when=now)
    await state.decisions.append(legacy)

    result = await ProductionRepairProjectionService(factory, store).project_effective_extraction(
        RUN_ID, actor_id="analyst"
    )

    marker = result.artifact.metadata["repair_projection"]
    assert result.included_repair_keys == ()
    assert result.unbuildable_repair_keys == (MALFORMED_KEY,)
    assert marker["applied_decisions"] == []
    assert marker["unbuildable_decisions"] == [
        {"repair_key": MALFORMED_KEY, "decision_id": str(legacy.id)}
    ]
    projected = technical_extraction_from_json(
        await store.read_json(result.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert projected.items == ()

    item = (await read_service.list(EDITION_ID, status="all")).items[0]
    assert item.effective_decision_id == legacy.id
    assert (item.rebuild_required, item.recommended_stage) == (True, "revise_decision")
    view = (await issues.list_issue_views(EDITION_ID, SUBJECT_ID))[0]
    assert view.projection_applied is False
    assert view.application_state is RepairDecisionApplicationState.UNBUILDABLE

    # The edition cannot be signed off while that debt exists.
    review = EditionReviewService.from_rows(EDITION_ID, [state.row], repair_issues=[view])
    assert review.pending_rebuild_count == 1
    assert not review.can_accept

    # The analyst clears it by revising the decision to EXCLUDE.
    revision = await _adjudication(state, store).decide_current_issue(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        repair_key=MALFORMED_KEY,
        action=ProductionRepairAction.EXCLUDE,
        observed_artifact_id=result.artifact.id,
        observed_pipeline_generation=0,
        expected_effective_decision_id=legacy.id,
        actor_id="analyst",
    )

    assert revision.action is ProductionRepairAction.EXCLUDE
    assert len(state.decisions.history) == 2
    cleared = (await issues.list_issue_views(EDITION_ID, SUBJECT_ID))[0]
    assert cleared.application_state is (RepairDecisionApplicationState.ALREADY_EFFECTIVE)
    assert (
        EditionReviewService.from_rows(
            EDITION_ID, [state.row], repair_issues=[cleared]
        ).pending_rebuild_count
        == 0
    )


# ---------------------------------------------------------------------------
# Part 6 — a source waiver is never erased by the source finally arriving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_waived_source_that_is_finally_archived_owes_a_references_rebuild() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    references = ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "repair_source_index": {
                "proposed": [
                    {
                        "source_id": "S9",
                        "source_url": SOURCE_URL,
                        "source_title": "Missing report",
                    }
                ],
                "canonical": [],
            }
        },
    )
    state = _State([references], _run(), _row())
    collection = SimpleNamespace(
        id=uuid4(),
        subject_id=SUBJECT_ID,
        canonical_url=SOURCE_URL,
        state=CollectionState.FAILED_TERMINAL,
        error_reason="collector_failed",
        attempt_count=3,
    )
    collections = [collection]
    factory = state.factory()

    def _factory_with_collections() -> Any:
        uow = factory()
        uow.source_collections = SimpleNamespace(
            list_for_subject=lambda _subject_id: _value(collections),
            list_for_subjects=lambda _subject_ids: _value(collections),
        )
        return uow

    issues = ProductionRepairIssueService(_factory_with_collections, store)
    service = ProductionRepairAdjudicationService(_factory_with_collections, issues)
    issue = (await issues.list_supplemental_source_issues(EDITION_ID, SUBJECT_ID))[0]

    waiver = await service.decide_current_issue(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        repair_key=issue.repair_key,
        action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
        observed_artifact_id=references.id,
        observed_pipeline_generation=0,
        expected_effective_decision_id=None,
        actor_id="analyst",
        reason="published without this source",
    )
    waived = (await issues.list_supplemental_source_issues(EDITION_ID, SUBJECT_ID))[0]
    assert waived.recommended_action == "continue_without_source"
    assert not waived.rebuild_required

    # The analyst finally supplies the content: the newer fact wins, but the
    # waiver stays in the audit and is never rewritten.
    collections[0] = replace_namespace(collection, state=CollectionState.ARCHIVED)
    archived = (await issues.list_supplemental_source_issues(EDITION_ID, SUBJECT_ID))[0]

    assert archived.repair_state.value == "archived_pending_references"
    assert archived.rebuild_required
    assert archived.recommended_action == "rebuild_references"
    assert archived.effective_decision is not None
    assert archived.effective_decision.id == waiver.id
    history = await service.decision_history(EDITION_ID, issue.repair_key, SUBJECT_ID)
    assert [item.action.value for item in history] == ["continue_without_source"]
    assert history[0].reason == "published without this source"


def replace_namespace(value: SimpleNamespace, **changes: Any) -> SimpleNamespace:
    return SimpleNamespace(**(vars(value) | changes))


# ---------------------------------------------------------------------------
# Bulk revisions honour the fence too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_revision_uses_the_same_fence_as_the_single_route() -> None:
    state, store = await _desk([_entry(GOOD_VALUE)])
    service = _adjudication(state, store)
    request = ProductionRepairAdjudicationRequest(
        subject_id=SUBJECT_ID,
        repair_key=GOOD_KEY,
        action=ProductionRepairAction.INCLUDE,
        observed_artifact_id=_artifact_id(state),
        observed_pipeline_generation=0,
        expected_effective_decision_id=None,
        observed_run_id=RUN_ID,
    )
    (first,) = await service.decide_current_issues(
        edition_id=EDITION_ID, requests=[request], actor_id="analyst"
    )

    with pytest.raises(ProductionRepairDecisionNoopError):
        await service.decide_current_issues(
            edition_id=EDITION_ID, requests=[request], actor_id="analyst"
        )

    (revised,) = await service.decide_current_issues(
        edition_id=EDITION_ID,
        requests=[
            replace(
                request,
                action=ProductionRepairAction.EXCLUDE,
                expected_effective_decision_id=first.id,
            )
        ],
        actor_id="analyst",
    )
    assert revised.action is ProductionRepairAction.EXCLUDE
    assert len(state.decisions.history) == 2
