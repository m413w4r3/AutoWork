"""LOT 24 — AUDIT 4: a supplied Q1 source stays a repair until REFERENCES catches up.

The defect this file pins down is a durability defect, not a rendering one:
once the analyst uploads the missing publication, the collection turns
ARCHIVED and every naive reader concludes the issue is gone -- while the
canonical ReferenceReport still ignores the source.  The debt therefore has to
survive a brand-new client, a brand-new set of services, and it must refuse the
publication freeze until the deterministic rebuild actually ran.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import replace as dataclass_replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.collection import router as collection_router
from cti_app.api.publication import router as publication_router
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.edition_publication import EditionPublicationService
from cti_app.application.edition_review import (
    EditionRepairReadService,
    EditionReviewReadItem,
    EditionReviewService,
)
from cti_app.application.http_collection import (
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    parse_reference_report,
    reconcile_reference_report_with_archives,
    reference_report_to_json,
)
from cti_app.application.production_repairs import ProductionRepairIssueService
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    GroupingConfidence,
    GroupingOutcome,
)
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionBatchStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    SupplementalSourceRepairState,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory

SOURCE_ONE = "https://one.example/report"
SOURCE_TWO = "https://two.example/report"
S2_HTML = (
    b"<!doctype html><html><body>Second report: ExampleRAT and evil[.]example."
    b"</body></html>"
)

RAW_Q1 = """# REFERENCES
## SOURCE S1
title: First
url: https://one.example/report
publisher: One
role: independent
## SOURCE S2
title: Second
url: https://two.example/report
publisher: Two
role: independent
## EVENT R1
date: 2026-08-02
sources: S1, S2
text: Shared event
## EVENT R2
date: 2026-08-03
sources: S2
text: Second-only event
"""


class _NoNetworkResolver:
    async def resolve(self, hostname: str) -> tuple[str, ...]:
        del hostname
        return ("93.184.216.34",)


class _NoNetworkTransport:
    """The repair path never fetches; any HTTP attempt is a defect."""

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        raise AssertionError(f"unexpected network fetch: {request.url}")


def _collector() -> SafeHttpCollector:
    return SafeHttpCollector(_NoNetworkTransport(), _NoNetworkResolver())  # type: ignore[arg-type]


class _ExplodingModelGateway:
    """Any model call at all fails the audit; the repair is deterministic."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        async def _fail(*_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError(f"the repair must not call the model ({name})")

        return _fail


class _BlobCatalog:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}

    async def ingest(self, source: Any, *, logical_bucket: str, mime_type: str) -> Any:
        del logical_bucket, mime_type
        content = source.read()
        blob_id = uuid4()
        self.contents[blob_id] = content
        return SimpleNamespace(
            id=blob_id,
            descriptor=SimpleNamespace(sha256=hashlib.sha256(content).hexdigest()),
        )

    async def read(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        del max_bytes
        return self.contents[blob_id]


class _Artifacts:
    def __init__(self) -> None:
        self.items: list[ProductionArtifact] = []

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
        await self._stale(run_id, stage, inclusive=False)

    async def mark_from_stage_stale(self, run_id: UUID, stage: str) -> list[str]:
        return await self._stale(run_id, stage, inclusive=True)

    async def _stale(self, run_id: UUID, stage: str, *, inclusive: bool) -> list[str]:
        order = ["references", "extraction", "synthesis", "publication"]
        if stage not in order:
            return []
        affected = set(order[order.index(stage) + (0 if inclusive else 1) :])
        staled: list[str] = []
        for index, item in enumerate(self.items):
            if (
                item.production_run_id == run_id
                and item.stage.value in affected
                and item.status is not ProductionArtifactStatus.STALE
            ):
                self.items[index] = dataclass_replace(
                    item, status=ProductionArtifactStatus.STALE
                )
                staled.append(item.stage.value)
        return staled


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.items: dict[UUID, SubjectProductionRun] = {run.id: run}

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def get_for_update(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.items.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.items[run.id] = run

    async def get_current_for_subject(self, subject_id: UUID) -> SubjectProductionRun | None:
        matches = [run for run in self.items.values() if run.subject_id == subject_id]
        return matches[-1] if matches else None

    async def list_for_edition(self, edition_id: UUID) -> list[SubjectProductionRun]:
        return [run for run in self.items.values() if run.edition_id == edition_id]


class _Manifests:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def get_latest_for_edition(self, _edition_id: UUID) -> Any | None:
        return self.items[-1] if self.items else None

    async def add(self, manifest: Any, _blob_id: UUID) -> None:
        self.items.append(manifest)


class _ProductionUow:
    """One transaction over the production side, sharing the collection store."""

    def __init__(self, world: _World) -> None:
        self._world = world
        self.subject_production_runs = world.runs
        self.production_artifacts = world.artifacts
        self.production_repair_decisions = world.decisions
        self.publication_manifests = world.manifests
        self.source_collections = SimpleNamespace(
            list_for_subject=world.list_collections,
            list_for_subjects=world.list_collections_bulk,
        )
        self.source_documents = SimpleNamespace(list_for_subject=world.list_documents)
        self.editions = SimpleNamespace(
            get=world.get_edition,
            get_for_update=world.get_edition,
            update=world.update_edition,
        )
        self.edition_review_read_model = SimpleNamespace(
            list_for_edition=world.review_rows
        )
        self.edition_production_batches = SimpleNamespace(
            get_latest_for_edition=world.get_batch,
            get=world.get_batch_by_id,
            get_for_update=world.get_batch_by_id,
            save=world.save_batch,
            list_for_edition=world.list_batches,
        )
        self.edition_production_batch_items = SimpleNamespace(
            get_by_run=world.get_batch_item,
            save=world.save_batch_item,
            list_for_batch=world.list_batch_items,
        )
        self.publication_manifest_entries = SimpleNamespace(
            append_many=_noop_many
        )
        self.publication_manifest_exclusions = SimpleNamespace(
            append_many=_noop_many
        )
        self.edition_audit = SimpleNamespace(append=_noop_one)

    async def __aenter__(self) -> _ProductionUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self._world.commits += 1


async def _noop_many(_id: UUID, _items: Any) -> None:
    return None


async def _noop_one(_item: Any) -> None:
    return None


class _Decisions:
    async def effective_decisions(
        self, _edition_id: UUID, _subject_id: UUID | None = None
    ) -> tuple[Any, ...]:
        return ()

    async def list_for_edition(
        self, _edition_id: UUID, _subject_id: UUID | None = None
    ) -> tuple[Any, ...]:
        return ()

    async def append(self, _decision: Any) -> None:
        return None


class _Jobs:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    async def submit(self, **kwargs: Any) -> Any:
        self.submitted.append(kwargs)
        return SimpleNamespace(id=uuid4())


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID, **_kwargs: Any) -> None:
        self.dispatched.append(job_id)


class _World:
    """The shared, mutable state of one edition under repair."""

    def __init__(
        self,
        *,
        edition: Edition,
        subject_id: UUID,
        run: SubjectProductionRun,
        collections: dict[UUID, Any],
        documents: dict[UUID, Any],
    ) -> None:
        self.edition = edition
        self.subject_id = subject_id
        self.runs = _Runs(run)
        self.artifacts = _Artifacts()
        self.decisions = _Decisions()
        self.manifests = _Manifests()
        self._collections = collections
        self._documents = documents
        self.batch = EditionProductionBatch(
            edition_id=edition.id, status=ProductionBatchStatus.COMPLETED
        )
        self.batch_item = EditionProductionBatchItem(
            batch_id=self.batch.id,
            subject_id=subject_id,
            production_run_id=run.id,
            position=1,
        )
        self.commits = 0

    def __call__(self) -> _ProductionUow:
        return _ProductionUow(self)

    async def get_edition(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None

    async def update_edition(self, edition: Edition, _version: int) -> bool:
        self.edition = edition
        return True

    async def list_collections(self, subject_id: UUID) -> list[Any]:
        return [
            item for item in self._collections.values() if item.subject_id == subject_id
        ]

    async def list_collections_bulk(self, subject_ids: Any) -> list[Any]:
        wanted = set(subject_ids)
        return [
            item for item in self._collections.values() if item.subject_id in wanted
        ]

    async def list_documents(self, subject_id: UUID) -> list[Any]:
        del subject_id
        return list(self._documents.values())

    async def get_batch(self, edition_id: UUID) -> Any | None:
        return self.batch if edition_id == self.edition.id else None

    async def get_batch_by_id(self, batch_id: UUID) -> Any | None:
        return self.batch if batch_id == self.batch.id else None

    async def save_batch(self, batch: Any) -> None:
        self.batch = batch

    async def list_batches(self, edition_id: UUID) -> list[Any]:
        return [self.batch] if edition_id == self.edition.id else []

    async def get_batch_item(self, run_id: UUID) -> Any | None:
        return self.batch_item if self.batch_item.production_run_id == run_id else None

    async def save_batch_item(self, item: Any) -> None:
        self.batch_item = item

    async def list_batch_items(self, batch_id: UUID) -> list[Any]:
        return [self.batch_item] if batch_id == self.batch.id else []

    async def review_rows(self, edition_id: UUID) -> list[EditionReviewReadItem]:
        del edition_id
        run = next(iter(self.runs.items.values()))
        document = await self.artifacts.get_current(
            run.id, ProductionArtifactStage.PUBLICATION.value
        )
        return [
            EditionReviewReadItem(
                position=1,
                subject_id=self.subject_id,
                title="Article LOT 24",
                run_id=run.id,
                pipeline_generation=run.pipeline_generation,
                run_status=run.status,
                document_artifact_id=getattr(document, "id", None),
                document_artifact_version=getattr(document, "version", None),
                document_input_hash=getattr(document, "input_hash", None),
                document_artifact_status=getattr(document, "status", None),
                error_code=None,
                error_message=None,
                effective_decision=None,
            )
        ]


def _selected_subject(
    factory: InMemoryCollectionUnitOfWorkFactory, urls: tuple[str, ...]
) -> tuple[Subject, Edition]:
    edition = Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, calendar.monthrange(2026, 8)[1]),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_articles=2,
        source_profile="default",
    )
    sources = [
        SourceCandidate(
            url=url,
            title=f"Report {index}",
            publisher="Research team",
            role=SourceRole.PRIMARY if index == 1 else SourceRole.INDEPENDENT,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=False,
        )
        for index, url in enumerate(urls, start=1)
    ]
    candidate = CandidateTopic(
        title="ExampleRAT campaign",
        summary="Technical source",
        novelty="new",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("technical",),
        actors=(),
        campaigns=(),
        malware=("ExampleRAT",),
        cves=(),
        victims=(),
        sectors=(),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=sources,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        request_hash="a" * 64,
        complementary_axis="initial",
        queries=("query",),
        citations=(),
        candidates=[candidate],
        discovery_model_run_id=uuid4(),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=False,
        parser_version="test-parser-v1",
    )
    subject = Subject(
        external_id=f"subject-{uuid4()}", slug=f"subject-{uuid4().hex}", tlp=TLP.AMBER
    )
    group = EditorialGroup(
        edition_id=edition.id,
        title=candidate.title,
        candidate_references=(CandidateReference(batch.id, candidate.id),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=EditorialScore(2, 2, 2, 2, 2, 2, {"impact": "test"}),
        source_relationship_status=sources[0].relationship_status,
        needs_source_verification=True,
        needs_source_expansion=True,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="test",
    )
    group.select(subject.id)
    factory.editions[edition.id] = edition
    factory.subjects[subject.id] = subject
    factory.batches[batch.id] = batch
    factory.groups[group.id] = group
    return subject, edition


def _stage_artifact(
    run: SubjectProductionRun,
    stage: ProductionArtifactStage,
    version: int,
    *,
    input_hash: str,
) -> ProductionArtifact:
    return ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=stage,
        version=version,
        input_hash=input_hash,
        status=ProductionArtifactStatus.VERIFIED,
        # The freeze refuses a publication artifact without a canonical body.
        canonical_blob_id=(
            uuid4() if stage is ProductionArtifactStage.PUBLICATION else None
        ),
    )


def _application(world: _World, collection_service: SubjectCollectionService) -> FastAPI:
    """A fresh app: no service instance is ever reused between phases."""
    store = ProductionArtifactStore(world.store_catalog)  # type: ignore[arg-type]
    issues = ProductionRepairIssueService(world, store)  # type: ignore[arg-type]
    application = FastAPI()
    application.include_router(publication_router)
    application.include_router(collection_router)
    application.state.uow_factory = world
    application.state.production_artifact_store = store
    application.state.production_repair_issue_service = issues
    application.state.edition_repair_read_service = EditionRepairReadService(
        world,  # type: ignore[arg-type]
        issues,
    )
    application.state.edition_review_service = EditionReviewService(
        world,  # type: ignore[arg-type]
        issues,
    )
    application.state.edition_publication_service = EditionPublicationService(
        world,  # type: ignore[arg-type]
        store,
        repair_issue_reader=issues,
    )
    application.state.collection_service = collection_service
    application.state.collection_review_service = SimpleNamespace()
    application.state.job_service = world.jobs
    application.state.job_dispatcher = world.dispatcher
    application.state.identity_provider = LocalIdentityProvider()
    application.state.model_gateway = world.gateway
    return application


def _client(application: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    )


@pytest.mark.asyncio
async def test_audit4_supplied_source_blocks_publication_until_references_rebuild(
    tmp_path: Path,
) -> None:
    collection_factory = InMemoryCollectionUnitOfWorkFactory()
    subject, edition = _selected_subject(collection_factory, (SOURCE_ONE, SOURCE_TWO))
    for step in (
        EditionStatus.DISCOVERY,
        EditionStatus.SELECTION,
        EditionStatus.PRODUCTION,
        EditionStatus.REVIEW,
    ):
        edition.transition(step)
    collection_service = SubjectCollectionService(
        collection_factory,
        _collector(),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    sources = await collection_service.initialize(subject.id)
    first = next(item for item in sources if item.canonical_url == SOURCE_ONE)
    second = next(item for item in sources if item.canonical_url == SOURCE_TWO)
    # S1 was collected; S2 failed for good.
    collection_factory.collections[first.id].state = CollectionState.ARCHIVED
    collection_factory.collections[first.id].origin_kind = SourceOriginKind.DISCOVERY
    collection_factory.collections[second.id].state = CollectionState.FAILED_TERMINAL

    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        research_date=date(2026, 8, 15),
    )
    world = _World(
        edition=edition,
        subject_id=subject.id,
        run=run,
        collections=collection_factory.collections,
        documents=collection_factory.documents,
    )
    world.store_catalog = _BlobCatalog()  # type: ignore[attr-defined]
    world.jobs = _Jobs()  # type: ignore[attr-defined]
    world.dispatcher = _Dispatcher()  # type: ignore[attr-defined]
    world.gateway = _ExplodingModelGateway()  # type: ignore[attr-defined]
    store = ProductionArtifactStore(world.store_catalog)  # type: ignore[arg-type]

    parsed = parse_reference_report(RAW_Q1, date(2026, 8, 15))
    assert parsed.value is not None
    canonical_v1 = reconcile_reference_report_with_archives(
        parsed.value, {SOURCE_ONE}
    ).report
    assert {item.local_id for item in canonical_v1.sources} == {"S1"}
    raw_id, canonical_id, _ = await store.store_stage_payloads(
        raw=RAW_Q1, canonical=reference_report_to_json(canonical_v1)
    )
    await world.artifacts.append(
        ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash="a" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=raw_id,
            canonical_blob_id=canonical_id,
            metadata={
                "repair_source_index": {
                    "proposed": [
                        {"source_id": "S1", "source_url": SOURCE_ONE, "source_title": "First"},
                        {"source_id": "S2", "source_url": SOURCE_TWO, "source_title": "Second"},
                    ],
                    "canonical": [{"source_id": "S1", "source_url": SOURCE_ONE}],
                }
            },
        )
    )
    for stage, digest in (
        (ProductionArtifactStage.EXTRACTION, "b"),
        (ProductionArtifactStage.SYNTHESIS, "c"),
        (ProductionArtifactStage.PUBLICATION, "d"),
    ):
        await world.artifacts.append(
            _stage_artifact(run, stage, 1, input_hash=digest * 64)
        )

    # --- 1. The desk shows S2 as an unarchived Q1 proposal. -----------------
    application = _application(world, collection_service)
    async with _client(application) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs")
    assert listed.status_code == 200
    body = listed.json()
    assert [item["source_id"] for item in body["items"]] == ["S2"]
    assert body["items"][0]["repair_state"] == SupplementalSourceRepairState.UNARCHIVED
    assert body["items"][0]["resolved"] is False
    assert body["summary"]["sources_to_supply"] == 1
    assert body["summary"]["articles_needing_rebuild"] == 0

    # --- 2. The analyst supplies the publication. ---------------------------
    async with _client(application) as client:
        archived = await client.post(
            f"/api/subjects/{subject.id}/sources/{second.id}/content",
            json={
                "content": S2_HTML.decode("utf-8"),
                "declared_mime_type": "text/html",
                "final_url": SOURCE_TWO,
            },
        )
    assert archived.status_code == 200, archived.text
    assert collection_factory.collections[second.id].state is CollectionState.ARCHIVED

    # --- 3. A brand-new client AND brand-new services still see the debt. ---
    fresh = _application(world, collection_service)
    async with _client(fresh) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs?status=all")
        review = await client.get(f"/api/editions/{edition.id}/review")
    body = listed.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["source_id"] == "S2"
    assert (
        item["repair_state"]
        == SupplementalSourceRepairState.ARCHIVED_PENDING_REFERENCES
    )
    assert item["rebuild_required"] is True
    assert item["recommended_stage"] == "rebuild_references"
    assert body["summary"]["sources_to_supply"] == 0
    assert body["summary"]["articles_needing_rebuild"] == 1
    assert body["articles"][0]["resolved_since_last_build_count"] == 1

    # --- 4/5. Sign-off is refused, and no manifest can be created. ----------
    assert review.status_code == 200
    assert review.json()["can_accept"] is False
    assert review.json()["pending_rebuild_count"] == 1

    async with _client(fresh) as client:
        refused = await client.post(f"/api/editions/{edition.id}/publication/accept")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "review_cannot_be_accepted"
    assert world.manifests.items == []
    assert world.edition.status is EditionStatus.REVIEW

    # --- 6. The rebuild reparses the archived Q1 with no model call. --------
    async with _client(fresh) as client:
        rebuilt = await client.post(
            f"/api/editions/{edition.id}/review/items/{subject.id}/rebuild"
        )
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["action"] == "rebuild_references_and_retry"
    assert rebuilt.json()["stage"] == SubjectProductionStage.EXTRACTION.value
    assert world.gateway.calls == 0

    references_v2 = await world.artifacts.get_current(
        run.id, ProductionArtifactStage.REFERENCES.value
    )
    assert references_v2 is not None and references_v2.version == 2
    assert references_v2.raw_blob_id == raw_id  # the same archived Q1 answer
    rebuilt_report = await store.read_json(references_v2.canonical_blob_id)  # type: ignore[arg-type]
    assert {source["url"] for source in rebuilt_report["sources"]} == {
        SOURCE_ONE,
        SOURCE_TWO,
    }
    # The S2-only event is back too.
    assert [event["source_ids"] for event in rebuilt_report["events"]] == [
        ["S1", "S2"],
        ["S2"],
    ]
    # EXTRACTION and everything after it was staled and re-queued.
    assert all(
        artifact.status is ProductionArtifactStatus.STALE
        for artifact in world.artifacts.items
        if artifact.stage
        in {
            ProductionArtifactStage.EXTRACTION,
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.PUBLICATION,
        }
    )
    assert world.jobs.submitted[-1]["kind"] == "production.subject.extraction"
    assert world.dispatcher.dispatched

    # --- 7. The replayed pipeline publishes a new document. -----------------
    replayed = await world.runs.get(run.id)
    assert replayed is not None
    for stage, digest in (
        (ProductionArtifactStage.EXTRACTION, "e"),
        (ProductionArtifactStage.SYNTHESIS, "f"),
        (ProductionArtifactStage.PUBLICATION, "0"),
    ):
        await world.artifacts.append(
            _stage_artifact(replayed, stage, 2, input_hash=digest * 64)
        )
    assert replayed.status is SubjectProductionStatus.RUNNING
    replayed.mark_ready()

    # --- 8/9. The issue is gone and the edition can be accepted again. ------
    final = _application(world, collection_service)
    async with _client(final) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs?status=all")
        review = await client.get(f"/api/editions/{edition.id}/review")
    assert listed.json()["items"] == []
    assert listed.json()["summary"]["articles_needing_rebuild"] == 0
    assert review.json()["pending_rebuild_count"] == 0
    assert review.json()["can_accept"] is True

    async with _client(final) as client:
        accepted = await client.post(f"/api/editions/{edition.id}/publication/accept")
    assert accepted.status_code == 202, accepted.text
    assert len(world.manifests.items) == 1
    # The whole scenario ran without a single model call.
    assert world.gateway.calls == 0


@pytest.mark.asyncio
async def test_audit4_source_without_collection_is_visible_and_preparable(
    tmp_path: Path,
) -> None:
    """A Q1 proposal the collection pass never registered is not a dead end."""
    collection_factory = InMemoryCollectionUnitOfWorkFactory()
    subject, edition = _selected_subject(collection_factory, (SOURCE_ONE,))
    for step in (
        EditionStatus.DISCOVERY,
        EditionStatus.SELECTION,
        EditionStatus.PRODUCTION,
        EditionStatus.REVIEW,
    ):
        edition.transition(step)
    collection_service = SubjectCollectionService(
        collection_factory,
        _collector(),
        FilesystemBlobStore(tmp_path / "blobs"),
    )
    first = (await collection_service.initialize(subject.id))[0]
    collection_factory.collections[first.id].state = CollectionState.ARCHIVED

    run = SubjectProductionRun(
        subject_id=subject.id,
        edition_id=edition.id,
        status=SubjectProductionStatus.READY,
        current_stage=SubjectProductionStage.ASSEMBLY,
        research_date=date(2026, 8, 15),
    )
    world = _World(
        edition=edition,
        subject_id=subject.id,
        run=run,
        collections=collection_factory.collections,
        documents=collection_factory.documents,
    )
    world.store_catalog = _BlobCatalog()  # type: ignore[attr-defined]
    world.jobs = _Jobs()  # type: ignore[attr-defined]
    world.dispatcher = _Dispatcher()  # type: ignore[attr-defined]
    world.gateway = _ExplodingModelGateway()  # type: ignore[attr-defined]
    await world.artifacts.append(
        ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash="a" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            metadata={
                "repair_source_index": {
                    "proposed": [
                        {"source_id": "S1", "source_url": SOURCE_ONE, "source_title": "First"},
                        {"source_id": "S2", "source_url": SOURCE_TWO, "source_title": "Second"},
                    ],
                    "canonical": [{"source_id": "S1", "source_url": SOURCE_ONE}],
                }
            },
        )
    )
    await world.artifacts.append(
        _stage_artifact(run, ProductionArtifactStage.PUBLICATION, 1, input_hash="d" * 64)
    )

    application = _application(world, collection_service)
    async with _client(application) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs")
        repair_key = listed.json()["items"][0]["repair_key"]
        detail = await client.get(
            f"/api/editions/{edition.id}/review/repairs/{repair_key}"
        )
        prepared = await client.post(
            f"/api/editions/{edition.id}/review/repairs/{repair_key}/source"
        )
        # The command is idempotent: a second call returns the same collection.
        again = await client.post(
            f"/api/editions/{edition.id}/review/repairs/{repair_key}/source"
        )

    assert listed.json()["items"][0]["repair_state"] == (
        SupplementalSourceRepairState.COLLECTION_MISSING
    )
    assert listed.json()["items"][0]["collection_id"] is None
    assert detail.json()["repair_state"] == (
        SupplementalSourceRepairState.COLLECTION_MISSING
    )
    assert prepared.status_code == 200, prepared.text
    collection_id = prepared.json()["collection_id"]
    assert again.json()["collection_id"] == collection_id
    assert len(collection_factory.collections) == 2

    # The prepared collection is attachable and the desk now offers the upload.
    async with _client(_application(world, collection_service)) as client:
        listed = await client.get(f"/api/editions/{edition.id}/review/repairs")
    item = listed.json()["items"][0]
    assert item["collection_id"] == collection_id
    assert item["repair_state"] == SupplementalSourceRepairState.UNARCHIVED
    assert world.gateway.calls == 0
    assert datetime.now(UTC).tzinfo is UTC
