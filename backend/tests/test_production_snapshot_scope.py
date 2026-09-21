from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cti_app.application.collection import SubjectCollectionService
from cti_app.application.jobs import JobHandlerError
from cti_app.application.production_context import build_subject_production_context
from cti_app.application.production_parsers import ParsedEvent, ParsedSource, ReferenceReport
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.subject_production import capture_production_input_snapshot
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceCollection, SourceOriginKind
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionRun,
    ProductionStage,
)


class _Collections:
    def __init__(self, items: Sequence[SourceCollection]) -> None:
        self.items = list(items)

    async def get(self, collection_id: UUID) -> SourceCollection | None:
        return next((item for item in self.items if item.id == collection_id), None)

    async def list_for_subject(self, subject_id: UUID) -> list[SourceCollection]:
        return [item for item in self.items if item.subject_id == subject_id]


class _Uow:
    def __init__(
        self,
        items: Sequence[SourceCollection],
        *,
        subject: object | None = None,
        edition: object | None = None,
    ) -> None:
        self.source_collections = _Collections(items)
        self.subjects = SimpleNamespace(
            get=AsyncMock(
                return_value=subject
                if subject is not None
                else SimpleNamespace(title="Subject", edition_id=uuid4())
            )
        )
        self.editions = SimpleNamespace(get=AsyncMock(return_value=edition))

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Context:
    job_id = uuid4()

    async def report_progress(self, *args: object) -> None:
        return None

    async def check_cancelled(self) -> None:
        return None

    async def record_diagnostics(self, details: dict[str, object]) -> None:
        return None

    async def heartbeat(self) -> None:
        return None


def _source(subject_id: UUID, url: str, *, origin: SourceOriginKind) -> SourceCollection:
    return SourceCollection(
        subject_id=subject_id,
        edition_id=uuid4(),
        requested_url=url,
        proposed_role=SourceRole.PRIMARY,
        origin_kind=origin,
        state=CollectionState.ARCHIVED,
    )


def _snapshot(subject_id: UUID, url: str, *, allowed: bool = True) -> ProductionInputSnapshot:
    candidate_id = uuid4()
    return ProductionInputSnapshot(
        production_run_id=uuid4(),
        subject_id=subject_id,
        edition_id=uuid4(),
        subject_version=1,
        subject_title="Subject",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=uuid4(),
        origin_discovery_subject_id=uuid4(),
        canonical_discovery_subject_id=uuid4(),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
        member_candidate_ids=(candidate_id,),
        discovery_summary="Description",
        actor_or_campaign="Actor",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        research_date=date(2026, 8, 28),
        core_sources=(
            ProductionInputSource(
                discovery_candidate_id=candidate_id,
                source_candidate_id=uuid4(),
                canonical_url=url,
                role=SourceRole.PRIMARY,
                title="Core source",
                publisher="Publisher",
                published_at=date(2026, 8, 1),
                tlp=TLP.CLEAR,
                sensitivity="public",
                external_llm_allowed=allowed,
            ),
        ),
    )


class _LineageUow:
    """Only the repositories the discovery lineage resolution really reads."""

    def __init__(
        self,
        *,
        subject: object,
        origin: object,
        canonical_id: UUID,
        snapshot: object,
        candidates: Sequence[object],
        edition: object,
    ) -> None:
        self.subjects = SimpleNamespace(get=AsyncMock(return_value=subject))
        self.subject_discovery_origins = SimpleNamespace(
            get_by_subject=AsyncMock(return_value=origin)
        )
        self.discovery_subject_identities = SimpleNamespace(
            resolve_canonical_subject=AsyncMock(return_value=canonical_id)
        )
        self.discovery_snapshots = SimpleNamespace(get_active=AsyncMock(return_value=snapshot))
        self.discovery_candidates = SimpleNamespace(
            list_for_edition=AsyncMock(return_value=list(candidates))
        )
        self.editions = SimpleNamespace(get=AsyncMock(return_value=edition))


@pytest.mark.asyncio
async def test_new_snapshot_freezes_the_active_discovery_lineage() -> None:
    subject_id = uuid4()
    edition_id = uuid4()
    origin_discovery_subject_id = uuid4()
    canonical_discovery_subject_id = uuid4()
    candidate_id = uuid4()
    selection_decision_id = uuid4()
    discovery_snapshot_id = uuid4()
    source = SimpleNamespace(
        id=uuid4(),
        canonical_url="https://example.test/candidate-source",
        role=SourceRole.PRIMARY,
        title="Candidate source",
        publisher="Publisher",
        published_at=date(2026, 8, 2),
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
    )
    candidate = SimpleNamespace(
        id=candidate_id,
        discovery_batch_id=uuid4(),
        actor_or_campaign="Campaign X",
        evidence=SimpleNamespace(actors=("Actor Y",), campaigns=(), sources=(source,)),
    )
    discovery_snapshot = SimpleNamespace(
        id=discovery_snapshot_id,
        version=7,
        subjects=(
            SimpleNamespace(
                subject_id=canonical_discovery_subject_id,
                member_references=(SimpleNamespace(candidate_id=candidate_id),),
                candidate=SimpleNamespace(summary="Résumé Discovery canonique"),
            ),
        ),
    )
    edition = SimpleNamespace(period_start=date(2026, 8, 1), period_end=date(2026, 8, 31))
    uow = _LineageUow(
        subject=SimpleNamespace(
            id=subject_id,
            title="Titre canonique du Subject",
            edition_id=edition_id,
            version=3,
            tlp=TLP.AMBER,
        ),
        origin=SimpleNamespace(
            subject_id=subject_id,
            edition_id=edition_id,
            discovery_subject_id=origin_discovery_subject_id,
            selection_decision_id=selection_decision_id,
        ),
        canonical_id=canonical_discovery_subject_id,
        snapshot=discovery_snapshot,
        candidates=[candidate],
        edition=edition,
    )

    snapshot = await capture_production_input_snapshot(
        cast(Any, uow),
        production_run_id=uuid4(),
        subject_id=subject_id,
        edition_id=edition_id,
        research_date=date(2026, 8, 28),
        captured_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert snapshot.subject_title == "Titre canonique du Subject"
    assert snapshot.subject_version == 3
    assert snapshot.subject_tlp is TLP.AMBER
    assert snapshot.edition_id == edition_id
    assert snapshot.period_start == edition.period_start
    assert snapshot.period_end == edition.period_end
    assert snapshot.selection_decision_id == selection_decision_id
    assert snapshot.origin_discovery_subject_id == origin_discovery_subject_id
    assert snapshot.canonical_discovery_subject_id == canonical_discovery_subject_id
    assert snapshot.discovery_snapshot_id == discovery_snapshot_id
    assert snapshot.discovery_snapshot_version == 7
    assert snapshot.member_candidate_ids == (candidate_id,)
    assert snapshot.discovery_summary == "Résumé Discovery canonique"
    assert snapshot.actor_or_campaign == "Campaign X · Actor Y"
    assert [item.canonical_url for item in snapshot.core_sources] == [source.canonical_url]
    assert snapshot.core_sources[0].discovery_candidate_id == candidate_id


@pytest.mark.asyncio
async def test_snapshot_initialization_is_closed_to_manual_reference_and_new_discovery() -> None:
    subject_id = uuid4()
    core_url = "https://example.test/core"
    snapshot = _snapshot(subject_id, core_url)
    items = [
        _source(subject_id, core_url, origin=SourceOriginKind.DISCOVERY),
        _source(subject_id, "https://example.test/manual", origin=SourceOriginKind.MANUAL),
        _source(
            subject_id,
            "https://example.test/old-reference",
            origin=SourceOriginKind.REFERENCE_RESEARCH,
        ),
        _source(
            subject_id, "https://example.test/new-discovery", origin=SourceOriginKind.DISCOVERY
        ),
    ]
    service = SubjectCollectionService.__new__(SubjectCollectionService)
    service._uow_factory = cast(Any, lambda: _Uow(items))

    initialized = await service.initialize_from_snapshot(subject_id, snapshot)

    assert [item.canonical_url for item in initialized] == [core_url]


@pytest.mark.asyncio
async def test_targeted_collection_does_not_initialize_the_subject() -> None:
    subject_id = uuid4()
    target = _source(subject_id, "https://example.test/target", origin=SourceOriginKind.MANUAL)
    target.state = CollectionState.PENDING
    uow = _Uow([target])
    snapshot = _snapshot(subject_id, "https://example.test/core")
    service = SubjectCollectionService.__new__(SubjectCollectionService)
    service._uow_factory = cast(Any, lambda: uow)
    cast(Any, service).initialize = AsyncMock(side_effect=AssertionError("global initialization"))
    cast(Any, service).initialize_from_snapshot = AsyncMock(
        side_effect=AssertionError("snapshot initialization")
    )
    cast(Any, service)._candidate_for = AsyncMock(return_value=None)
    cast(Any, service).archive_one = AsyncMock(return_value=CollectionState.ARCHIVED)
    cast(Any, service)._materialize_workspace = AsyncMock()
    cast(Any, service)._record_summary = AsyncMock(return_value="provenance://test")

    context = _Context()
    await service.collect_subject(
        subject_id,
        context.job_id,
        cast(Any, context),
        collection_id=target.id,
        snapshot=snapshot,
    )

    cast(Any, service).initialize.assert_not_awaited()
    cast(Any, service).initialize_from_snapshot.assert_not_awaited()
    cast(Any, service).archive_one.assert_awaited_once()
    archive_args = cast(Any, service).archive_one.call_args
    assert archive_args.args[0] == target.id
    assert archive_args.args[1] == context.job_id
    assert archive_args.kwargs["context"] is context
    assert archive_args.kwargs["position"] == (1, 1)
    assert archive_args.kwargs["candidate"] is None


@pytest.mark.asyncio
async def test_snapshot_context_excludes_out_of_scope_policy_and_supporting_sources() -> None:
    subject_id = uuid4()
    core_url = "https://example.test/core"
    manual = _source(subject_id, "https://example.test/manual", origin=SourceOriginKind.MANUAL)
    manual.do_not_submit = True
    manual.external_llm_allowed = False
    old_reference = _source(
        subject_id,
        "https://example.test/old-reference",
        origin=SourceOriginKind.REFERENCE_RESEARCH,
    )
    old_reference.do_not_submit = True
    current_reference = _source(
        subject_id,
        "https://example.test/current-reference",
        origin=SourceOriginKind.REFERENCE_RESEARCH,
    )
    snapshot = _snapshot(subject_id, core_url)
    uow = _Uow(
        [
            _source(subject_id, core_url, origin=SourceOriginKind.DISCOVERY),
            manual,
            old_reference,
            current_reference,
        ]
    )
    q1 = await build_subject_production_context(
        cast(Any, uow),
        subject_id,
        snapshot=snapshot,
        relevant_source_urls=None,
    )
    q2 = await build_subject_production_context(
        cast(Any, uow),
        subject_id,
        snapshot=snapshot,
        relevant_source_urls={current_reference.canonical_url},
    )

    assert "core" in q1.core_sources_text
    assert "manual" not in q1.core_sources_text
    assert q1.supporting_sources_text == ""
    assert q1.external_llm_allowed is True
    assert q1.blocking_sources == ()
    assert "1 publication(s)" in q1.technical_summary
    assert "current-reference" in q2.supporting_sources_text
    assert "old-reference" not in q2.supporting_sources_text
    assert "2 publication(s)" in q2.technical_summary


@pytest.mark.asyncio
async def test_sources_stage_counts_only_snapshot_sources() -> None:
    subject_id = uuid4()
    core_url = "https://example.test/core"
    snapshot = _snapshot(subject_id, core_url)
    core = _source(subject_id, core_url, origin=SourceOriginKind.DISCOVERY)
    outside = _source(subject_id, "https://example.test/new", origin=SourceOriginKind.MANUAL)

    collection_service = SimpleNamespace(
        collect_subject=AsyncMock(),
        list_sources=AsyncMock(return_value=[core, outside]),
    )
    orchestrator = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    orchestrator._collection_service = cast(Any, collection_service)
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
        current_stage=ProductionStage.SOURCES,
    )

    result = await orchestrator._execute_sources_stage(run, cast(Any, _Context()), snapshot)

    assert result["status"] == "success"
    assert result["sources_count"] == 1
    assert result["archived"] == 1


@pytest.mark.asyncio
async def test_sources_stage_preserves_retryable_collection_failure() -> None:
    subject_id = uuid4()

    class CollectionService:
        async def collect_subject(
            self, subject_id: UUID, job_id: UUID, context: object, **kwargs: object
        ) -> str:
            del subject_id, job_id, context, kwargs
            raise JobHandlerError(
                "source_collection_no_success",
                "Aucune publication n'a pu être archivée.",
                transient=False,
                details={"failed_retryable": 1},
            )

    orchestrator = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    orchestrator._collection_service = cast(Any, CollectionService())
    run = ProductionRun(subject_id=subject_id, edition_id=uuid4())

    result = await orchestrator._execute_sources_stage(run, cast(Any, _Context()))

    assert result["status"] == "transient_error"
    assert result["error_code"] == "source_collection_no_success"
    assert result["details"] == {"failed_retryable": 1}


@pytest.mark.asyncio
async def test_archived_source_outside_snapshot_cannot_make_sources_succeed() -> None:
    subject_id = uuid4()
    snapshot = _snapshot(subject_id, "https://example.test/core")
    outside = _source(subject_id, "https://example.test/new", origin=SourceOriginKind.MANUAL)
    collection_service = SimpleNamespace(
        collect_subject=AsyncMock(),
        list_sources=AsyncMock(return_value=[outside]),
    )
    orchestrator = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    orchestrator._collection_service = cast(Any, collection_service)
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
    )

    result = await orchestrator._execute_sources_stage(run, cast(Any, _Context()), snapshot)

    assert result["status"] == "error"
    assert result["error"] == "No source could be archived for this subject"


@pytest.mark.asyncio
async def test_q1_collects_only_report_urls_by_exact_collection_id() -> None:
    subject_id = uuid4()
    report_url = "https://example.test/q1"
    unrelated = _source(
        subject_id, "https://example.test/unrelated", origin=SourceOriginKind.MANUAL
    )
    q1_collection = _source(
        subject_id,
        report_url,
        origin=SourceOriginKind.REFERENCE_RESEARCH,
    )
    q1_collection.state = CollectionState.PENDING
    items = [unrelated, q1_collection]

    class CollectionService:
        def __init__(self) -> None:
            self.calls: list[UUID] = []

        async def add_supplemental_sources(self, subject_id: UUID, sources: object) -> list[object]:
            return []

        async def list_sources(self, subject_id: UUID) -> list[SourceCollection]:
            return items

        async def collect_subject(
            self, subject_id: UUID, job_id: UUID, context: object, **kwargs: object
        ) -> str:
            collection_id = kwargs["collection_id"]
            assert isinstance(collection_id, UUID)
            self.calls.append(collection_id)
            q1_collection.state = CollectionState.ARCHIVED
            return "provenance://q1"

    collection_service = CollectionService()
    orchestrator = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    orchestrator._collection_service = cast(Any, collection_service)
    orchestrator._uow_factory = cast(Any, lambda: _Uow(items))
    report = ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="Q1",
                url=report_url,
                canonical_url=report_url,
                publisher="Publisher",
                published_at=date(2026, 8, 1),
                role=SourceRole.INDEPENDENT,
            ),
        ),
        events=(
            ParsedEvent(
                local_id="E1", event_date=date(2026, 8, 2), source_ids=("S1",), text="Event"
            ),
        ),
    )
    run = ProductionRun(
        subject_id=subject_id,
        edition_id=uuid4(),
    )

    result = await orchestrator._integrate_reference_sources(run, report, cast(Any, _Context()))

    assert collection_service.calls == [q1_collection.id]
    assert result["archived_sources"] == 1
    assert result["report"].source_ids() == {"S1"}


@pytest.mark.asyncio
async def test_supplemental_failure_drops_only_unbacked_events_and_keeps_shared_event() -> None:
    subject_id = uuid4()
    failed = _source(
        subject_id,
        "https://hatching.example/article",
        origin=SourceOriginKind.REFERENCE_RESEARCH,
    )
    failed.state = CollectionState.FAILED_RETRYABLE
    archived = _source(
        subject_id,
        "https://tria.ge/sample",
        origin=SourceOriginKind.REFERENCE_RESEARCH,
    )
    items = [failed, archived]

    class CollectionService:
        def __init__(self) -> None:
            self.retry_calls: list[UUID] = []
            self.collect_calls: list[UUID] = []

        async def add_supplemental_sources(self, subject_id: UUID, sources: object) -> list[object]:
            del subject_id, sources
            return []

        async def list_sources(self, subject_id: UUID) -> list[SourceCollection]:
            del subject_id
            return items

        async def prepare_retry(self, collection_id: UUID) -> SourceCollection:
            self.retry_calls.append(collection_id)
            failed.state = CollectionState.PENDING
            return failed

        async def collect_subject(
            self, subject_id: UUID, job_id: UUID, context: object, **kwargs: object
        ) -> str:
            del subject_id, job_id, context
            collection_id = kwargs["collection_id"]
            assert collection_id == failed.id
            self.collect_calls.append(collection_id)
            failed.state = CollectionState.FAILED_RETRYABLE
            raise JobHandlerError(
                "source_collection_no_success",
                "Aucune publication n'a pu être archivée.",
                transient=False,
                details={
                    "total": 1,
                    "failed_retryable": 1,
                    "blocked": 0,
                    "unavailable": 0,
                    "failed_terminal": 0,
                },
            )

    diagnostics = SimpleNamespace(events=[])
    diagnostics.record = lambda **fields: diagnostics.events.append(fields)
    collection_service = CollectionService()
    orchestrator = ProductionWorkflowOrchestrator.__new__(ProductionWorkflowOrchestrator)
    orchestrator._collection_service = cast(Any, collection_service)
    orchestrator._uow_factory = cast(Any, lambda: _Uow(items))
    orchestrator._diagnostics = diagnostics
    orchestrator._correlation_id = "test"
    report = ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S3",
                title="Hatching",
                url=failed.requested_url,
                canonical_url=failed.canonical_url,
                publisher="Hatching",
                published_at=date(2026, 8, 1),
                role=SourceRole.INDEPENDENT,
            ),
            ParsedSource(
                local_id="S4",
                title="Triage",
                url=archived.requested_url,
                canonical_url=archived.canonical_url,
                publisher="Triage",
                published_at=date(2026, 8, 2),
                role=SourceRole.INDEPENDENT,
            ),
        ),
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2026, 8, 1),
                source_ids=("S3",),
                text="only failed",
            ),
            ParsedEvent(
                local_id="R2",
                event_date=date(2026, 8, 2),
                source_ids=("S3", "S4"),
                text="shared evidence",
            ),
        ),
    )
    run = ProductionRun(subject_id=subject_id, edition_id=uuid4())

    result = await orchestrator._integrate_reference_sources(run, report, _Context())

    assert collection_service.retry_calls == [failed.id]
    assert collection_service.collect_calls == [failed.id]
    assert [event.local_id for event in result["kept_events"]] == ["R2"]
    assert result["kept_events"][0].source_ids == ("S4",)
    assert result["report"].source_ids() == {"S4"}
    assert result["supplemental_collection_failures"][0]["canonical_url"] == failed.canonical_url
    assert result["supplemental_collection_failures"][0]["failed_retryable"] == 1
    assert any(
        "supplemental_collection_failed:url=https://hatching.example/article:"
        "code=source_collection_no_success:failed_retryable=1:blocked=0:unavailable=0:failed_terminal=0"
        in warning
        for warning in result["warnings"]
    )
    assert any(
        event["event"] == "q1.supplemental_collection_failed"
        and event["canonical_url"] == failed.canonical_url
        and event["error_code"] == "source_collection_no_success"
        and event["retry_attempted"] is True
        for event in diagnostics.events
    )
