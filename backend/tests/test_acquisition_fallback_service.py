from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api.collection import router
from cti_app.application.acquisition_fallbacks import AcquisitionProbe, AcquisitionStrategy
from cti_app.application.collection import (
    SubjectCollectionService,
    _FallbackAcquisition,
    register_collection_jobs,
)
from cti_app.application.collection_errors import (
    CollectionItemNotFoundError,
    CollectionNotAllowedError,
)
from cti_app.application.collection_review import CollectionReviewService
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionPolicy,
    PinnedHttpRequest,
    RawHttpResponse,
    SafeHttpCollector,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import JobRegistry, JobService
from cti_app.domain.acquisition import (
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    AcquisitionPolicyDecision,
    AcquisitionQuality,
    EvidenceClass,
    SourceEquivalenceClass,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    CollectionState,
    DetectedMimeType,
    SourceAcquisitionMethod,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.test_collection import selected_subject

_ARTICLE = (
    b"A synthetic article describes ExampleRAT activity across affected networks. "
    b"It explains command infrastructure, persistence, credential theft, and mitigation "
    b"steps in enough detail for deterministic offline acquisition tests. "
) * 2


class Resolver:
    async def resolve(self, hostname: str) -> Sequence[str]:
        del hostname
        return ("93.184.216.34",)


class Transport:
    def __init__(self, responses: list[RawHttpResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.requests: list[PinnedHttpRequest] = []

    async def request(self, request: PinnedHttpRequest) -> RawHttpResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected direct HTTP request")
        return self.responses.pop(0)


def _response(url: str, body: bytes = _ARTICLE) -> CollectedResponse:
    digest = hashlib.sha256(body).hexdigest()
    return CollectedResponse(
        requested_url=url,
        final_url=url,
        redirect_chain=(),
        status=200,
        headers={"content-type": "text/plain"},
        declared_content_type="text/plain",
        detected_content_type=DetectedMimeType.TEXT,
        encoded_body=body,
        decoded_body=body,
        encoded_size=len(body),
        encoded_sha256=digest,
        decoded_size=len(body),
        decoded_sha256=digest,
        content_encoding="identity",
        acquired_at=datetime.now(UTC),
    )


class SuccessStrategy:
    name = "test_exact_title"
    version = "1"
    method = SourceAcquisitionMethod.EXACT_TITLE_SEARCH
    enabled = True
    timeout_seconds = 1.0

    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, request) -> Sequence[AcquisitionProbe]:
        self.calls += 1
        response = _response(request.requested_url)
        return (
            AcquisitionProbe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                requested_url=request.requested_url,
                outcome=AcquisitionOutcome.SUCCEEDED,
                reason_code=None,
                response=response,
                http_status=response.status,
                encoded_size=response.encoded_size,
                encoded_sha256=response.encoded_sha256,
                duration_ms=12,
                equivalence_class=SourceEquivalenceClass.EXACT_ORIGINAL,
                evidence_class=EvidenceClass.PRIMARY_OBSERVED,
                observation_kind=AcquisitionObservationKind.HTTP_RESPONSE,
                quality=AcquisitionQuality(
                    text_chars=len(_ARTICLE),
                    word_count=40,
                    title_similarity=0.9,
                    language="en",
                    boilerplate_ratio=0.0,
                    duplicate_ratio=0.0,
                    truncated=False,
                    challenge_detected=False,
                    paywall_detected=False,
                    score=0.95,
                    reasons=("title_match",),
                ),
                publisher="Example Publisher",
                primary_source_verified=True,
                ioc_primary_verified=True,
            ),
        )


class TwoSuccessStrategy(SuccessStrategy):
    async def acquire(self, request) -> Sequence[AcquisitionProbe]:
        first = (await super().acquire(request))[0]
        assert first.response is not None
        variant_url = f"{request.requested_url}?format=alternative"
        second_response = replace(
            first.response,
            requested_url=variant_url,
            final_url=variant_url,
        )
        second = replace(first, requested_url=variant_url, response=second_response)
        return first, second


class NoopStrategy:
    name = "test_skip"
    version = "1"
    method = SourceAcquisitionMethod.EXACT_TITLE_SEARCH
    enabled = True
    timeout_seconds = 1.0

    async def acquire(self, request) -> Sequence[AcquisitionProbe]:
        return (
            AcquisitionProbe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                requested_url=request.requested_url,
                outcome=AcquisitionOutcome.NOT_CONFIGURED,
                reason_code="test_endpoint_not_configured",
                response=None,
                http_status=None,
                encoded_size=None,
                encoded_sha256=None,
                duration_ms=0,
            ),
        )


class FailedProbeStrategy:
    name = "test_failed_probe"
    version = "1"
    method = SourceAcquisitionMethod.EXACT_TITLE_SEARCH
    enabled = True
    timeout_seconds = 1.0

    async def acquire(self, request) -> Sequence[AcquisitionProbe]:
        return (
            AcquisitionProbe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                requested_url=request.requested_url,
                outcome=AcquisitionOutcome.FAILED,
                reason_code="synthetic_probe_failure",
                response=None,
                http_status=None,
                encoded_size=None,
                encoded_sha256=None,
                duration_ms=4,
            ),
        )


class PolicyCaptureStrategy(NoopStrategy):
    def __init__(self) -> None:
        self.policy: AcquisitionPolicyContext | None = None

    async def acquire(self, request) -> Sequence[AcquisitionProbe]:
        self.policy = request.policy
        return await super().acquire(request)


class JobContext:
    def __init__(self) -> None:
        self.diagnostics: list[dict[str, object]] = []

    async def check_cancelled(self) -> None:
        return None

    async def record_diagnostics(self, details: dict[str, object]) -> None:
        self.diagnostics.append(details)


class NoopDispatcher:
    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del job_id, delay_ms


async def _service(
    tmp_path: Path,
    *,
    transport: Transport,
    strategies: tuple[AcquisitionStrategy, ...],
    source_url: str = "https://discovery.example/report",
    endpoint_enabled: bool = True,
    third_party_provider_enabled: bool = False,
    third_party_provider_url: str = "",
    third_party_provider_token: str = "",
):
    factory = InMemoryCollectionUnitOfWorkFactory()
    subject = selected_subject(factory, (source_url,))
    blob_store = FilesystemBlobStore(tmp_path / "blobs")
    service = SubjectCollectionService(
        factory,
        SafeHttpCollector(transport, Resolver(), CollectionPolicy()),
        blob_store,
        fallback_strategies=strategies,
        fallback_endpoint_enabled=endpoint_enabled,
        third_party_provider_enabled=third_party_provider_enabled,
        third_party_provider_url=third_party_provider_url,
        third_party_provider_token=third_party_provider_token,
    )
    source = (await service.initialize(subject.id))[0]
    factory.collections[source.id].state = CollectionState.BLOCKED
    return factory, service, subject, factory.collections[source.id], blob_store


@pytest.mark.asyncio
async def test_operator_fallback_uses_only_orchestrator_and_round_trips_provenance(tmp_path):
    transport = Transport()
    factory, service, subject, source, _blob_store = await _service(
        tmp_path, transport=transport, strategies=(SuccessStrategy(),)
    )

    state = await service.fallback_acquire(
        subject.id,
        source.id,
        uuid4(),
        JobContext(),  # type: ignore[arg-type]
    )

    assert state is CollectionState.ARCHIVED
    assert transport.requests == []
    attempts = await service.attempts(source.id)
    strategy_attempts = [item for item in attempts if item.strategy_name == "test_exact_title"]
    assert len(strategy_attempts) == 1
    assert strategy_attempts[0].outcome.value == "succeeded"
    assert strategy_attempts[0].acquisition_method is SourceAcquisitionMethod.EXACT_TITLE_SEARCH
    assert strategy_attempts[0].source_equivalence_class is SourceEquivalenceClass.EXACT_ORIGINAL
    assert strategy_attempts[0].quality_score == 0.95
    assert strategy_attempts[0].quality_assessment == {
        "score": 0.95,
        "reasons": ["title_match"],
    }

    archived = next(
        event for event in factory.provenance if event.event_type == "source.archived_fallback"
    )
    payload = archived.payload
    assert payload["policy_snapshot_id"] == service.policy_snapshot.id
    assert payload["acquisition_job_id"]
    assert payload["actor"] == "system:acquisition_fallback"
    assert payload["original_url"] == "https://discovery.example/"
    assert payload["requested_variant_url"] == "https://discovery.example/"
    assert payload["final_url"] == "https://discovery.example/"
    assert payload["raw_blob_reference"].startswith("source-raw/")
    assert payload["decoded_blob_reference"].startswith("source-decoded/")
    assert payload["sha256"] == hashlib.sha256(_ARTICLE).hexdigest()
    assert payload["attempt_id"] == str(strategy_attempts[0].id)
    assert payload["quality_assessment"] == {"score": 0.95, "reasons": ["title_match"]}
    assert payload["primary_source_verified"] is True
    assert payload["ioc_primary_verified"] is True
    assert _ARTICLE.decode() not in str(payload)

    receipt = await service.archive_receipt(factory.collections[source.id])
    assert receipt is not None
    assert receipt.acquisition_method is SourceAcquisitionMethod.EXACT_TITLE_SEARCH
    assert receipt.strategy_name == "test_exact_title"
    assert receipt.source_equivalence_class is SourceEquivalenceClass.EXACT_ORIGINAL
    assert receipt.evidence_class is EvidenceClass.PRIMARY_OBSERVED
    assert receipt.quality_score == 0.95
    assert receipt.quality_reasons == ("title_match",)
    assert receipt.primary_source_verified is True
    assert receipt.ioc_primary_verified is True


@pytest.mark.asyncio
async def test_failed_probe_is_persisted_by_recording_wrapper(tmp_path):
    factory, service, subject, source, _blob_store = await _service(
        tmp_path,
        transport=Transport(),
        strategies=(FailedProbeStrategy(),),
    )

    state = await service.fallback_acquire(
        subject.id,
        source.id,
        uuid4(),
        JobContext(),  # type: ignore[arg-type]
    )

    assert state is CollectionState.BLOCKED
    attempts = await service.attempts(source.id)
    failed_attempts = [item for item in attempts if item.strategy_name == "test_failed_probe"]
    assert len(failed_attempts) == 1
    assert failed_attempts[0].outcome.value == "error"
    assert failed_attempts[0].failure_reason == "Acquisition strategy synthetic_probe_failure"
    event = next(
        event
        for event in factory.provenance
        if event.event_type == "source.acquisition_fallback_attempted"
        and event.payload.get("strategy_name") == "test_failed_probe"
    )
    assert event.payload["outcome"] == AcquisitionOutcome.FAILED.value
    assert event.payload["reason_code"] == "synthetic_probe_failure"


@pytest.mark.asyncio
async def test_fallback_policy_uses_typed_tlp_and_provider_setting(tmp_path):
    strategy = PolicyCaptureStrategy()
    _factory, service, subject, source, _blob_store = await _service(
        tmp_path,
        transport=Transport(),
        strategies=(strategy,),
        third_party_provider_enabled=True,
        third_party_provider_url="https://provider.example/extract",
        third_party_provider_token="synthetic-test-token",
    )
    source.do_not_submit = True
    source.external_llm_allowed = False

    await service.fallback_acquire(
        subject.id,
        source.id,
        uuid4(),
        JobContext(),  # type: ignore[arg-type]
    )

    policy = strategy.policy
    assert policy is not None
    assert isinstance(policy.tlp, TLP)
    assert policy.external_llm_allowed is False
    assert policy.third_party_provider_allowed is True
    assert policy.provider_configured is True
    assert (
        policy.check_destination(source.canonical_url, third_party_provider=True)
        is AcquisitionPolicyDecision.DENIED_PROVIDER_DISCLOSURE
    )


@pytest.mark.asyncio
async def test_orchestrator_persists_nonwinning_success_probe_before_archiving(tmp_path):
    strategy = TwoSuccessStrategy()
    factory, service, subject, source, _blob_store = await _service(
        tmp_path, transport=Transport(), strategies=(strategy,)
    )

    state = await service.fallback_acquire(
        subject.id,
        source.id,
        uuid4(),
        JobContext(),  # type: ignore[arg-type]
    )

    assert state is CollectionState.ARCHIVED
    attempts = await service.attempts(source.id)
    assert len(attempts) == 2
    assert all(item.outcome.value == "succeeded" for item in attempts)
    assert factory.collections[source.id].source_document_id is not None
    assert len(factory.documents) == 1
    assert (
        len(
            [
                event
                for event in factory.provenance
                if event.event_type == "source.archived_fallback"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_fallback_rejects_wrong_subject_disabled_and_invalid_state(tmp_path):
    factory, service, subject, source, _blob_store = await _service(
        tmp_path, transport=Transport(), strategies=(SuccessStrategy(),)
    )
    with pytest.raises(CollectionItemNotFoundError):
        await service.fallback_acquire(uuid4(), source.id, uuid4(), JobContext())  # type: ignore[arg-type]

    source.state = CollectionState.PENDING
    with pytest.raises(CollectionNotAllowedError):
        await service.fallback_acquire(subject.id, source.id, uuid4(), JobContext())  # type: ignore[arg-type]

    factory.collections[source.id].state = CollectionState.BLOCKED
    disabled = SubjectCollectionService(
        factory,
        SafeHttpCollector(Transport(), Resolver(), CollectionPolicy()),
        FilesystemBlobStore(tmp_path / "disabled"),
        fallback_strategies=(SuccessStrategy(),),
        fallback_endpoint_enabled=False,
    )
    with pytest.raises(CollectionNotAllowedError, match="fallback acquisition disabled"):
        await disabled.fallback_acquire(
            subject.id,
            source.id,
            uuid4(),
            JobContext(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_policy_denied_private_source_never_enters_fallback_chain(tmp_path):
    transport = Transport()
    strategy = SuccessStrategy()
    _factory, service, _subject, source, _blob_store = await _service(
        tmp_path,
        transport=transport,
        strategies=(strategy,),
        source_url="http://127.0.0.1/private",
    )

    state = await service.archive_one(source.id, uuid4())

    assert state is CollectionState.BLOCKED
    assert transport.requests == []
    assert strategy.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True], ids=["direct", "manual"])
async def test_fallback_cannot_replace_attached_direct_or_manual_document(tmp_path, manual):
    body = b"<html><body>" + _ARTICLE + b"</body></html>"
    transport = Transport([RawHttpResponse(200, {"content-type": "text/html"}, body)])
    factory, service, _subject, source, _blob_store = await _service(
        tmp_path, transport=transport, strategies=(SuccessStrategy(),)
    )
    if manual:
        archived = await service.archive_manual_content(
            source.id,
            content=_ARTICLE,
            declared_mime_type="text/plain",
            actor_id="analyst:test",
        )
        document_id = archived.source_document_id
    else:
        source.state = CollectionState.PENDING
        assert await service.archive_one(source.id, uuid4()) is CollectionState.ARCHIVED
        document_id = factory.collections[source.id].source_document_id
        # Exercise the failed-processing state that still retains the direct pointer.
        factory.collections[source.id].state = CollectionState.FAILED_TERMINAL
    assert document_id is not None
    before_documents = len(factory.documents)

    await service._archive(
        source.id,
        datetime.now(UTC),
        _response(source.canonical_url),
        acquisition=_FallbackAcquisition(
            job_id=uuid4(),
            method=SourceAcquisitionMethod.EXACT_TITLE_SEARCH,
            strategy_name="test_exact_title",
            strategy_version="1",
        ),
    )

    assert factory.collections[source.id].source_document_id == document_id
    assert len(factory.documents) == before_documents == 1
    assert not any(event.event_type == "source.archived_fallback" for event in factory.provenance)


@pytest.mark.asyncio
async def test_two_sequential_fallback_commits_attach_one_document(tmp_path):
    factory, service, _subject, source, _blob_store = await _service(
        tmp_path, transport=Transport(), strategies=(SuccessStrategy(),)
    )
    first = _FallbackAcquisition(
        job_id=uuid4(),
        method=SourceAcquisitionMethod.EXACT_TITLE_SEARCH,
        strategy_name="test_exact_title",
        strategy_version="1",
    )
    second = _FallbackAcquisition(
        job_id=uuid4(),
        method=SourceAcquisitionMethod.WAYBACK_SNAPSHOT,
        strategy_name="test_wayback",
        strategy_version="1",
    )

    # The SQL repository implements get_for_update with SELECT FOR UPDATE; the
    # archive pointer/state guard in _archive runs while that row lock is held.
    await service._archive(
        source.id, datetime.now(UTC), _response(source.canonical_url), acquisition=first
    )
    first_document_id = factory.collections[source.id].source_document_id
    await service._archive(
        source.id, datetime.now(UTC), _response(source.canonical_url), acquisition=second
    )

    assert first_document_id is not None
    assert factory.collections[source.id].source_document_id == first_document_id
    assert len(factory.documents) == 1
    assert (
        len(
            [
                event
                for event in factory.provenance
                if event.event_type == "source.archived_fallback"
            ]
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_ADMIN_DSN"),
    reason="TEST_POSTGRES_ADMIN_DSN is required for PostgreSQL lock integration",
)
async def test_postgres_fallback_archive_serializes_pointer_check(tmp_path):
    """A competing fallback sees the first commit after waiting on get_for_update."""
    import asyncio

    from alembic import command
    from sqlalchemy.engine import make_url

    from cti_app.domain.jobs import Job
    from cti_app.infrastructure.database.session import (
        create_postgres_engine,
        create_session_factory,
    )
    from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
    from tests.integration.conftest import _alembic_config, _create_database, _drop_database
    from tests.integration.test_manual_source_archival import _seed_subject

    admin_url = os.environ["TEST_POSTGRES_ADMIN_DSN"]
    database_name = f"cti_test_acquisition_lock_{uuid4().hex}"
    await _create_database(admin_url, database_name)
    try:
        database_url = (
            make_url(admin_url).set(database=database_name).render_as_string(hide_password=False)
        )
        command.upgrade(_alembic_config(database_url), "head")
        engine = create_postgres_engine(database_url)
        try:
            session_factory = create_session_factory(engine)

            def postgres_uow_factory() -> SqlAlchemyUnitOfWork:
                return SqlAlchemyUnitOfWork(session_factory)

            subject, (source,) = await _seed_subject(
                postgres_uow_factory, (f"https://lock-{uuid4().hex}.example/report",)
            )
            service = SubjectCollectionService(
                postgres_uow_factory,
                SafeHttpCollector(Transport(), Resolver(), CollectionPolicy()),
                FilesystemBlobStore(tmp_path / "postgres-blobs"),
            )
            job = Job(
                kind="source.collect",
                aggregate_type="source_collection",
                aggregate_id=source.id,
                idempotency_key=f"source.collect:{source.id}",
                correlation_id="fallback-lock-test",
                input_parameters={},
            )
            async with postgres_uow_factory() as uow:
                await uow.collection_policy_snapshots.add_if_absent(service._policy_snapshot)
                assert await uow.jobs.add_if_absent(job)
                await uow.commit()

            first = _FallbackAcquisition(
                job_id=job.id,
                method=SourceAcquisitionMethod.EXACT_TITLE_SEARCH,
                strategy_name="test_exact_title",
            )
            second = _FallbackAcquisition(
                job_id=job.id,
                method=SourceAcquisitionMethod.WAYBACK_SNAPSHOT,
                strategy_name="test_wayback",
            )
            await asyncio.gather(
                service._archive(
                    source.id,
                    datetime.now(UTC),
                    _response(source.canonical_url, _ARTICLE + b" first result"),
                    acquisition=first,
                ),
                service._archive(
                    source.id,
                    datetime.now(UTC),
                    _response(source.canonical_url, _ARTICLE + b" second result"),
                    acquisition=second,
                ),
            )

            async with postgres_uow_factory() as uow:
                stored = await uow.source_collections.get(source.id)
                documents = await uow.source_documents.list_for_subject(subject.id)
                events = await uow.provenance.list_for_aggregate("source_collection", source.id)
            assert stored is not None and stored.source_document_id is not None
            assert len(documents) == 1
            assert (
                len([event for event in events if event.event_type == "source.archived_fallback"])
                == 1
            )
        finally:
            await engine.dispose()
    finally:
        await _drop_database(admin_url, database_name)


@pytest.mark.asyncio
async def test_fallback_acquire_endpoint_is_idempotent_and_uses_actor(tmp_path):
    factory, service, subject, source, blob_store = await _service(
        tmp_path, transport=Transport(), strategies=(NoopStrategy(),)
    )
    review_service = CollectionReviewService(factory, blob_store)
    jobs_uow = InMemoryJobUnitOfWorkFactory()
    registry = JobRegistry()
    register_collection_jobs(registry, service)
    job_service = JobService(jobs_uow, registry)
    app = FastAPI()
    app.include_router(router)
    app.state.collection_service = service
    app.state.collection_review_service = review_service
    app.state.job_service = job_service
    app.state.job_dispatcher = NoopDispatcher()
    app.state.identity_provider = LocalIdentityProvider("operator-7")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/api/subjects/{subject.id}/sources/{source.id}/fallback-acquire"
        first = await client.post(path)
        duplicate = await client.post(path)
        wrong_subject = await client.post(
            f"/api/subjects/{uuid4()}/sources/{source.id}/fallback-acquire"
        )

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["job_id"] == first.json()["job_id"]
    assert wrong_subject.status_code == 404
    job = await job_service.get(UUID(first.json()["job_id"]))
    assert job.kind == "source.fallback_acquire"
    assert job.input_parameters["requested_by"] == "operator-7"
    assert job.idempotency_key == (
        f"source.fallback_acquire:{subject.id}:{source.id}:"
        f"{service.policy_snapshot.id}:{source.attempt_count + 1}"
    )
