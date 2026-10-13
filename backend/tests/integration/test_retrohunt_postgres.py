from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.retrohunt import (
    RetrohuntService,
    RetrohuntSlotLimitError,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import AnalystDecision, AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DisclosureBounds,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
)
from cti_app.domain.retrohunt import (
    RetrohuntHit,
    RetrohuntProviderJob,
    RetrohuntProviderJobState,
    RetrohuntProviderResults,
    RetrohuntSubmitRequest,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

from .edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


class _PgUowFactory:
    def __init__(self, session_factory, investigations: dict[UUID, Any]) -> None:
        self.session_factory = session_factory
        self.investigations = investigations

    def __call__(self):
        return _PgUow(self.session_factory, self.investigations)


class _PgUow:
    def __init__(self, session_factory, investigations: dict[UUID, Any]) -> None:
        self.inner = SqlAlchemyUnitOfWork(session_factory)
        self.investigations = investigations

    async def __aenter__(self):
        await self.inner.__aenter__()

        async def get(investigation_id: UUID):
            return self.investigations.get(investigation_id)

        self.inner.analyst_investigations.get = get
        return self.inner

    async def __aexit__(self, exc_type, exc, traceback):
        return await self.inner.__aexit__(exc_type, exc, traceback)


class _Provider:
    available = True

    def __init__(self) -> None:
        self.submit_calls: list[RetrohuntSubmitRequest] = []
        self.status_state = RetrohuntProviderJobState.RUNNING
        self.raise_timeout = False

    async def submit_retrohunt(self, request: RetrohuntSubmitRequest) -> RetrohuntProviderJob:
        self.submit_calls.append(request)
        if self.raise_timeout:
            raise TimeoutError("simulated ambiguous provider response")
        return RetrohuntProviderJob(
            job_id=f"pg-job-{len(self.submit_calls)}",
            state=RetrohuntProviderJobState.RUNNING,
        )

    async def retrohunt_status(self, job_id: str) -> RetrohuntProviderJob:
        assert job_id.startswith("pg-job-")
        return RetrohuntProviderJob(job_id=job_id, state=self.status_state)

    async def retrohunt_results(self, job_id: str) -> RetrohuntProviderResults:
        assert job_id.startswith("pg-job-")
        return RetrohuntProviderResults(
            job_id=job_id,
            hits=(RetrohuntHit(sha256=hashlib.sha256(b"postgres-hit").hexdigest()),),
        )


class _EvidenceRecorder:
    async def record_json(self, **kwargs: Any) -> EvidenceObservation:
        payload = json.dumps(kwargs["payload"], sort_keys=True, separators=(",", ":")).encode()
        return EvidenceObservation(
            investigation_id=kwargs["investigation_id"],
            kind=kwargs["kind"],
            blob_id=uuid4(),
            sha256=hashlib.sha256(payload).hexdigest(),
            source=kwargs["source"],
            source_version=kwargs["source_version"],
            observed_at=datetime.now(UTC),
            object_type=kwargs["object_type"],
            object_id=kwargs["object_id"],
            limitations=kwargs["limitations"],
        )


class _Review:
    def __init__(self, rows: dict[UUID, tuple[Any, ...]]) -> None:
        self.rows = rows

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(
            item[1] if isinstance(item, tuple) else item
            for item in self.rows.get(investigation_id, ())
        )


def _approved_review(investigation_id: UUID, suffix: str):
    from cti_app.domain.investigation import DetectionCandidateVersion

    source = f"rule retrohunt_{suffix} {{ condition: true }}"
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    version = DetectionCandidateVersion(
        investigation_id=investigation_id,
        source=source,
        source_sha256=source_sha256,
        motif_provenance=(),
    )
    positive = hashlib.sha256(f"positive:{suffix}".encode()).hexdigest()
    roles = [
        {
            "role": "confirmed_positive",
            "corpus_sha256s": [positive],
            "matches": [positive],
            "non_matches": [],
            "non_measurable": [],
            "errors": [],
        }
    ]
    roles.extend(
        {
            "role": role,
            "corpus_sha256s": [],
            "matches": [],
            "non_matches": [],
            "non_measurable": [],
            "errors": [],
        }
        for role in ("goodware", "out_of_cluster_candidate", "holdout")
    )
    decision = AnalystDecision(
        investigation_id=investigation_id,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=version.id,
        actor_id="analyst@example.test",
        reason="Prévalidation Retrohunt autorisée pour cette version exacte.",
        correlation_id=f"retrohunt-{suffix}",
        details={"source_sha256": source_sha256, "known_false_positives": []},
    )
    test_report = SimpleNamespace(
        payload={
            "schema": "investigation-yara-test.v1",
            "candidate_id": str(version.candidate_id),
            "version_id": str(version.id),
            "proposal_source_sha256": source_sha256,
            "execution_status": "ok",
            "engine_version": "YARA 4.5.0",
            "source_sha256": source_sha256,
            "selection_bias": "Article seed sample.",
            "coverage_statement": "Positive sample measured.",
            "roles": roles,
        }
    )
    return source_sha256, SimpleNamespace(
        version=version,
        status=DetectionCandidateStatus.APPROVED,
        latest_decision=decision,
        test_reports=(test_report,),
    )


def _envelope(investigation_id: UUID) -> InvestigationEnvelope:
    return InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("vt",),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(tlp=TLPLevel.GREEN),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst@example.test",
    )


async def _create_subject(session_factory):
    suffix = uuid4().hex
    edition = Edition(
        country="Retrohunt test",
        country_code=reserve_edition_code(),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title=f"Retrohunt test {suffix}",
        slug=f"retrohunt-test-{suffix}",
        tlp=TLP.GREEN,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.commit()
    return subject


def _service(session_factory, subject_id: UUID, investigation_ids: tuple[UUID, ...], reviews):
    investigations = {
        investigation_id: SimpleNamespace(subject_id=subject_id)
        for investigation_id in investigation_ids
    }
    provider = _Provider()
    service = RetrohuntService(
        uow_factory=_PgUowFactory(session_factory, investigations),
        review_service=_Review(reviews),
        provider=provider,
        evidence_recorder=_EvidenceRecorder(),
    )
    return service, provider


async def _submit(service, investigation_id: UUID, review, corpus: str):
    source_sha256, version_review = review
    return await service.submit(
        investigation_id=investigation_id,
        version_id=version_review.version.id,
        source_sha256=source_sha256,
        corpus_id=corpus,
        corpus_sha256=hashlib.sha256(corpus.encode()).hexdigest(),
        parameters={"fixture": "postgres"},
        envelope=_envelope(investigation_id),
    )


@pytest.mark.asyncio
async def test_concurrent_investigations_reserve_exactly_two_subject_slots(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    subject = await _create_subject(session_factory)
    investigation_a, investigation_b = uuid4(), uuid4()
    reviews_a = (
        _approved_review(investigation_a, uuid4().hex),
        _approved_review(investigation_a, uuid4().hex),
    )
    reviews_b = (_approved_review(investigation_b, uuid4().hex),)
    all_reviews = {investigation_a: reviews_a, investigation_b: reviews_b}
    service, provider = _service(
        session_factory, subject.id, (investigation_a, investigation_b), all_reviews
    )
    attempts = (
        (investigation_a, reviews_a[0]),
        (investigation_b, reviews_b[0]),
        (investigation_a, reviews_a[1]),
    )
    try:
        results = await asyncio.gather(
            *(
                _submit(service, investigation_id, review, "shared-corpus")
                for investigation_id, review in attempts
            ),
            return_exceptions=True,
        )
        successes = [item for item in results if not isinstance(item, Exception)]
        rejected = [item for item in results if isinstance(item, RetrohuntSlotLimitError)]
        assert len(successes) == 2
        assert len(rejected) == 1
        assert {item.reservation.slot_number for item in successes} == {1, 2}
        assert len(provider.submit_calls) == 2
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.retrohunt_reservations.count_for_subject(subject.id) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replay_revision_ambiguity_and_read_tools_keep_postgres_count(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    subject = await _create_subject(session_factory)
    investigation_a, investigation_b = uuid4(), uuid4()
    first = _approved_review(investigation_a, uuid4().hex)
    revised = _approved_review(investigation_b, uuid4().hex)
    reviews = {investigation_a: (first,), investigation_b: (revised,)}
    service, provider = _service(
        session_factory, subject.id, (investigation_a, investigation_b), reviews
    )
    try:
        provider.raise_timeout = True
        ambiguous = await _submit(service, investigation_a, first, "shared-corpus")
        assert ambiguous.reservation.state.value == "reconciliation_required"
        provider.raise_timeout = False
        replay = await _submit(service, investigation_a, first, "shared-corpus")
        assert replay.reservation.id == ambiguous.reservation.id
        assert replay.reservation.state.value == "reconciliation_required"
        assert len(provider.submit_calls) == 1

        second = await _submit(service, investigation_b, revised, "shared-corpus")
        assert second.reservation.slot_number == 2
        provider.status_state = RetrohuntProviderJobState.COMPLETED
        status = await service.status(
            investigation_id=investigation_b,
            reservation_id=second.reservation.id,
        )
        results = await service.results(
            investigation_id=investigation_b,
            reservation_id=second.reservation.id,
        )
        assert results.candidate_hits
        assert status.slots_consumed == results.slots_consumed == 2
        assert status.slots_remaining == results.slots_remaining == 0
        with pytest.raises(RetrohuntSlotLimitError):
            await _submit(service, investigation_a, first, "another-corpus")
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.retrohunt_reservations.count_for_subject(subject.id) == 2
    finally:
        await engine.dispose()
