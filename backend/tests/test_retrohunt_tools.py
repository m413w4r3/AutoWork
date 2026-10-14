from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_retrohunt_tools import (
    _tool_result,
    register_retrohunt_tools,
    retrohunt_tool_capabilities,
)
from cti_app.application.retrohunt import (
    RetrohuntQualityError,
    RetrohuntService,
    RetrohuntSlotLimitError,
    _operation_key,
)
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
    RetrohuntAuthorization,
)
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DisclosureBounds,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.retrohunt import (
    RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT,
    RetrohuntHit,
    RetrohuntProviderErrorCode,
    RetrohuntProviderJob,
    RetrohuntProviderJobState,
    RetrohuntProviderResults,
    RetrohuntReservation,
    RetrohuntReservationState,
    RetrohuntSubmitRequest,
)
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyAnalystDecisionRepository,
    _analyst_decision_from_row,
)
from cti_app.infrastructure.virustotal import VirusTotalRetrohuntProxyAdapter

SUBJECT_ID = uuid4()
_EMPTY_REPORT_ROLES = (
    {
        "role": "out_of_cluster_candidate",
        "corpus_sha256s": [],
        "matches": [],
        "non_matches": [],
        "non_measurable": [],
        "errors": [],
    },
    {
        "role": "goodware",
        "corpus_sha256s": [],
        "matches": [],
        "non_matches": [],
        "non_measurable": [],
        "errors": [],
    },
    {
        "role": "holdout",
        "corpus_sha256s": [],
        "matches": [],
        "non_matches": [],
        "non_measurable": [],
        "errors": [],
    },
)


class _MemoryReservations:
    def __init__(self) -> None:
        self.rows: dict[UUID, RetrohuntReservation] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, reservation: RetrohuntReservation):
        async with self._lock:
            existing = next(
                (
                    item
                    for item in self.rows.values()
                    if item.subject_id == reservation.subject_id
                    and item.operation_key == reservation.operation_key
                ),
                None,
            )
            if existing is not None:
                return existing, False
            used = {
                item.slot_number
                for item in self.rows.values()
                if item.subject_id == reservation.subject_id
            }
            slot_number = next(
                (
                    slot
                    for slot in range(1, RETROHUNT_MAX_SUBMISSIONS_PER_SUBJECT + 1)
                    if slot not in used
                ),
                None,
            )
            if slot_number is None:
                return None
            saved = RetrohuntReservation(
                **{
                    **{
                        field: getattr(reservation, field)
                        for field in (
                            "subject_id",
                            "investigation_id",
                            "operation_key",
                            "rule_version_id",
                            "rule_source_sha256",
                            "corpus_id",
                            "corpus_sha256",
                            "parameters",
                            "id",
                        )
                    },
                    "slot_number": slot_number,
                }
            )
            self.rows[saved.id] = saved
            return saved, True

    async def get_for_subject(self, subject_id: UUID, reservation_id: UUID):
        item = self.rows.get(reservation_id)
        return item if item is not None and item.subject_id == subject_id else None

    async def count_for_subject(self, subject_id: UUID) -> int:
        return sum(item.subject_id == subject_id for item in self.rows.values())

    async def list_for_subject(self, subject_id: UUID):
        return tuple(
            sorted(
                (item for item in self.rows.values() if item.subject_id == subject_id),
                key=lambda item: (item.slot_number, item.created_at),
            )
        )

    async def update_if_version(self, reservation: RetrohuntReservation, *, expected_version: int):
        async with self._lock:
            current = self.rows.get(reservation.id)
            if current is None or current.version != expected_version:
                return None
            self.rows[reservation.id] = reservation
            return reservation


class _MemoryDecisionSession:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)


@pytest.mark.asyncio
async def test_structured_retrohunt_authorization_round_trips_and_legacy_missing_is_denied():
    session = _MemoryDecisionSession()
    version_id = uuid4()
    source_sha256 = hashlib.sha256(b"reviewed exact rule").hexdigest()
    authorization = RetrohuntAuthorization(
        authorized=True,
        rule_version_id=version_id,
        source_sha256=source_sha256,
    )
    decision = AnalystDecision(
        investigation_id=uuid4(),
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=version_id,
        actor_id="analyst@example.test",
        reason="Approval reason is not an authorization input.",
        correlation_id=str(uuid4()),
        details={"source_sha256": source_sha256},
        retrohunt_authorization=authorization,
    )

    await SqlAlchemyAnalystDecisionRepository(session).append(decision)
    persisted_row = session.rows[0]
    restored = _analyst_decision_from_row(persisted_row)

    assert persisted_row.details["retrohunt_authorization"] == {
        "schema": "detection-retrohunt-authorization.v1",
        "authorized": True,
        "rule_version_id": str(version_id),
        "source_sha256": source_sha256,
    }
    assert restored.retrohunt_authorization == authorization
    persisted_row.details = {"source_sha256": source_sha256}
    legacy = _analyst_decision_from_row(persisted_row)
    assert legacy.retrohunt_authorization is None


class _Uow:
    def __init__(self, repo: _MemoryReservations, investigations: dict[UUID, Any]) -> None:
        self.retrohunt_reservations = repo
        self.analyst_investigations = SimpleNamespace(
            get=lambda investigation_id: investigations.get(investigation_id)
        )
        self._investigations = investigations

    async def __aenter__(self):
        # The service awaits repository methods; this compact fake uses a
        # coroutine for the investigation lookup, matching the real port.
        async def get(investigation_id: UUID):
            return self._investigations.get(investigation_id)

        self.analyst_investigations.get = get
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self) -> None:
        return None


class _UowFactory:
    def __init__(self, repo: _MemoryReservations, investigations: dict[UUID, Any]) -> None:
        self.repo = repo
        self.investigations = investigations

    def __call__(self) -> _Uow:
        return _Uow(self.repo, self.investigations)


class _StrictProvider:
    available = True

    def __init__(self) -> None:
        self.submit_calls: list[RetrohuntSubmitRequest] = []
        self.status_calls: list[str] = []
        self.results_calls: list[str] = []
        self.submit_error: Exception | None = None
        self.status_error: Exception | None = None
        self.job_state = RetrohuntProviderJobState.RUNNING
        self.hits: tuple[RetrohuntHit, ...] = ()

    async def submit_retrohunt(self, request: RetrohuntSubmitRequest) -> RetrohuntProviderJob:
        assert isinstance(request, RetrohuntSubmitRequest)
        self.submit_calls.append(request)
        if self.submit_error is not None:
            raise self.submit_error
        return RetrohuntProviderJob(job_id=f"job-{len(self.submit_calls)}", state=self.job_state)

    async def retrohunt_status(self, job_id: str) -> RetrohuntProviderJob:
        assert job_id.startswith("job-")
        self.status_calls.append(job_id)
        if self.status_error is not None:
            raise self.status_error
        return RetrohuntProviderJob(job_id=job_id, state=self.job_state)

    async def retrohunt_results(self, job_id: str) -> RetrohuntProviderResults:
        assert job_id.startswith("job-")
        self.results_calls.append(job_id)
        return RetrohuntProviderResults(job_id=job_id, hits=self.hits)


class _Evidence:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def record_json(self, **kwargs: Any) -> EvidenceObservation:
        payload = dict(kwargs["payload"])
        self.payloads.append(payload)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return EvidenceObservation(
            investigation_id=kwargs["investigation_id"],
            kind=kwargs["kind"],
            blob_id=uuid4(),
            sha256=hashlib.sha256(body).hexdigest(),
            source=kwargs["source"],
            source_version=kwargs["source_version"],
            observed_at=datetime.now(UTC),
            object_type=kwargs["object_type"],
            object_id=kwargs["object_id"],
            limitations=kwargs["limitations"],
        )


class _Review:
    def __init__(self) -> None:
        self.rows: dict[UUID, tuple[Any, ...]] = {}

    async def list_for_investigation(self, investigation_id: UUID):
        return self.rows.get(investigation_id, ())


def _review(
    investigation_id: UUID,
    *,
    approved: bool = True,
    rule_source: str | None = None,
    retrohunt_authorized: bool | None = True,
    authorization_version_id: UUID | None = None,
    authorization_sha256: str | None = None,
    decision_reason: str = "Prévalidation Retrohunt autorisée pour cette version exacte.",
):
    from cti_app.domain.investigation import DetectionCandidateVersion

    source = rule_source or f"rule-{uuid4()} {{ condition: true }}"
    source_sha256 = hashlib.sha256(source.encode()).hexdigest()
    version = DetectionCandidateVersion(
        investigation_id=investigation_id,
        source=source,
        source_sha256=source_sha256,
        motif_provenance=(),
    )
    positive = hashlib.sha256(f"positive-{uuid4()}".encode()).hexdigest()
    payload = {
        "schema": "investigation-yara-test.v1",
        "candidate_id": str(version.candidate_id),
        "version_id": str(version.id),
        "proposal_source_sha256": source_sha256,
        "execution_status": "ok",
        "engine_version": "YARA 4.5.0",
        "source_sha256": source_sha256,
        "selection_bias": "Seeded from article samples.",
        "coverage_statement": "One representative positive measured.",
        "roles": [
            {
                "role": "confirmed_positive",
                "corpus_sha256s": [positive],
                "matches": [positive],
                "non_matches": [],
                "non_measurable": [],
                "errors": [],
            },
            *_EMPTY_REPORT_ROLES,
        ],
    }
    decision = AnalystDecision(
        investigation_id=investigation_id,
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=version.id,
        actor_id="analyst@example.test",
        reason=decision_reason,
        correlation_id=str(uuid4()),
        details={"source_sha256": source_sha256, "known_false_positives": []},
        retrohunt_authorization=(
            RetrohuntAuthorization(
                authorized=retrohunt_authorized,
                rule_version_id=authorization_version_id or version.id,
                source_sha256=authorization_sha256 or source_sha256,
            )
            if retrohunt_authorized is not None
            else None
        ),
    )
    return source_sha256, SimpleNamespace(
        version=version,
        status=(
            DetectionCandidateStatus.APPROVED if approved else DetectionCandidateStatus.CANDIDATE
        ),
        latest_decision=decision,
        test_reports=(SimpleNamespace(payload=payload),),
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


def _service():
    investigation_ids = (uuid4(), uuid4())
    investigations = {item: SimpleNamespace(subject_id=SUBJECT_ID) for item in investigation_ids}
    repo = _MemoryReservations()
    review = _Review()
    sources: list[tuple[UUID, UUID, str]] = []
    for investigation_id in investigation_ids:
        source_sha256, row = _review(investigation_id)
        review.rows[investigation_id] = (row,)
        sources.append((investigation_id, row.version.id, source_sha256))
    provider = _StrictProvider()
    evidence = _Evidence()
    service = RetrohuntService(
        uow_factory=_UowFactory(repo, investigations),
        review_service=review,
        provider=provider,
        evidence_recorder=evidence,
    )
    return service, repo, review, sources, provider, evidence


async def _submit(
    service: RetrohuntService,
    investigation_id: UUID,
    version_id: UUID,
    source_sha256: str,
    *,
    corpus: str = "corpus-a",
    parameters: dict[str, Any] | None = None,
):
    return await service.submit(
        investigation_id=investigation_id,
        version_id=version_id,
        source_sha256=source_sha256,
        corpus_id=corpus,
        corpus_sha256=hashlib.sha256(corpus.encode()).hexdigest(),
        parameters=parameters or {"family": "test"},
        envelope=_envelope(investigation_id),
    )


@pytest.mark.asyncio
async def test_retrohunt_capability_requires_explicit_proxy_route() -> None:
    caps = VirusTotalCapabilities(retrohunt=True)
    provider = _StrictProvider()
    assert not retrohunt_tool_capabilities(caps, VirusTotalRoutingPolicy(), provider)

    adapter = VirusTotalRetrohuntProxyAdapter(
        capabilities=caps,
        routing_policy=VirusTotalRoutingPolicy(),
    )
    assert not adapter.available
    with pytest.raises(VirusTotalRouteUnavailableError):
        await adapter.retrohunt_status("job-1")

    proxy_route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    policy = VirusTotalRoutingPolicy(routes={VirusTotalCapability.RETROHUNT: proxy_route})
    routed_adapter = VirusTotalRetrohuntProxyAdapter(
        capabilities=caps,
        routing_policy=policy,
        proxy_client=provider,
    )
    assert routed_adapter.available
    assert retrohunt_tool_capabilities(caps, policy, routed_adapter) == frozenset({"vt.retrohunt"})
    await routed_adapter.retrohunt_status("job-1")
    assert provider.status_calls == ["job-1"]
    assert not retrohunt_tool_capabilities(
        VirusTotalCapabilities(retrohunt=False), policy, provider
    )

    direct_route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.DIRECT,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    direct_adapter = VirusTotalRetrohuntProxyAdapter(
        capabilities=caps,
        routing_policy=VirusTotalRoutingPolicy(
            routes={VirusTotalCapability.RETROHUNT: direct_route}
        ),
        proxy_client=provider,
    )
    assert not direct_adapter.available


@pytest.mark.asyncio
async def test_tools_register_submit_status_and_results_without_other_vt_budget() -> None:
    service, _, _, _, _, _ = _service()

    class Registry:
        def __init__(self) -> None:
            self.definitions = {}

        def register(self, definition, adapter):
            self.definitions[definition.name] = (definition, adapter)

    registry = Registry()
    register_retrohunt_tools(registry, service=service)
    assert set(registry.definitions) == {
        "vt.retrohunt.submit",
        "vt.retrohunt.status",
        "vt.retrohunt.results",
    }
    assert all(definition.cost_model == {} for definition, _ in registry.definitions.values())


@pytest.mark.asyncio
async def test_submit_is_idempotent_by_exact_rule_corpus_and_parameters() -> None:
    service, repo, _, sources, provider, _ = _service()
    investigation_id, version_id, source_sha256 = sources[0]
    first = await _submit(service, investigation_id, version_id, source_sha256)
    replay = await _submit(service, investigation_id, version_id, source_sha256)

    assert first.reservation.state is RetrohuntReservationState.SUBMITTED
    assert replay.reservation.id == first.reservation.id
    assert replay.slots_consumed == 1
    assert replay.slots_remaining == 1
    assert len(provider.submit_calls) == 1
    assert await repo.count_for_subject(SUBJECT_ID) == 1


@pytest.mark.asyncio
async def test_ambiguous_submission_and_crash_replay_never_resubmit() -> None:
    service, repo, _, sources, provider, _ = _service()
    investigation_id, version_id, source_sha256 = sources[0]
    provider.submit_error = TimeoutError("request outcome unknown")
    ambiguous = await _submit(service, investigation_id, version_id, source_sha256)
    assert ambiguous.reservation.state is RetrohuntReservationState.RECONCILIATION_REQUIRED
    replay = await _submit(service, investigation_id, version_id, source_sha256)
    assert replay.reservation.id == ambiguous.reservation.id
    assert len(provider.submit_calls) == 1

    investigation_id_2, version_id_2, source_sha256_2 = sources[1]
    params = {"family": "crash-replay"}
    corpus_id = "corpus-crash"
    corpus_sha256 = hashlib.sha256(corpus_id.encode()).hexdigest()
    operation_key = _operation_key(source_sha256_2, corpus_id, corpus_sha256, params)
    seeded = RetrohuntReservation(
        subject_id=SUBJECT_ID,
        investigation_id=investigation_id_2,
        slot_number=1,
        operation_key=operation_key,
        rule_version_id=version_id_2,
        rule_source_sha256=source_sha256_2,
        corpus_id=corpus_id,
        corpus_sha256=corpus_sha256,
        parameters=params,
    )
    row, created = await repo.reserve(seeded)
    assert created
    token = uuid4()
    claimed = row.transition(RetrohuntReservationState.SUBMITTING, submission_token=token)
    assert await repo.update_if_version(claimed, expected_version=row.version) == claimed
    crash_replay = await _submit(
        service,
        investigation_id_2,
        version_id_2,
        source_sha256_2,
        corpus=corpus_id,
        parameters=params,
    )
    assert crash_replay.reservation.id == row.id
    assert crash_replay.reservation.state is RetrohuntReservationState.RECONCILIATION_REQUIRED
    assert len(provider.submit_calls) == 1


@pytest.mark.asyncio
async def test_revised_version_uses_second_slot_and_third_operation_is_refused() -> None:
    service, repo, review, sources, provider, _ = _service()
    investigation_id, version_id, source_sha256 = sources[0]
    first = await _submit(service, investigation_id, version_id, source_sha256, corpus="corpus-v1")

    revised_source = f"rule revised-{uuid4()} {{ condition: true }}"
    revised_sha256, revised_review = _review(investigation_id, rule_source=revised_source)
    review.rows[investigation_id] = (revised_review,)
    second = await _submit(
        service,
        investigation_id,
        revised_review.version.id,
        revised_sha256,
        corpus="corpus-v1",
    )
    assert (first.reservation.slot_number, second.reservation.slot_number) == (1, 2)
    assert second.slots_consumed == 2 and second.slots_remaining == 0

    third_source = f"rule third-{uuid4()} {{ condition: true }}"
    third_sha256, third_review = _review(investigation_id, rule_source=third_source)
    review.rows[investigation_id] = (third_review,)
    with pytest.raises(RetrohuntSlotLimitError) as raised:
        await _submit(
            service, investigation_id, third_review.version.id, third_sha256, corpus="corpus-v1"
        )
    assert raised.value.slots_consumed == 2
    assert raised.value.slots_remaining == 0
    assert len(provider.submit_calls) == 2
    assert await repo.count_for_subject(SUBJECT_ID) == 2


@pytest.mark.asyncio
async def test_rule_requires_human_retrohunt_prevalidation_and_measured_quality() -> None:
    service, repo, review, sources, provider, _ = _service()
    investigation_id, version_id, source_sha256 = sources[0]
    _, unapproved = _review(investigation_id, approved=False)
    review.rows[investigation_id] = (unapproved,)
    with pytest.raises(RetrohuntQualityError, match="human prevalidation"):
        await _submit(service, investigation_id, version_id, source_sha256)
    assert await repo.count_for_subject(SUBJECT_ID) == 0
    assert provider.submit_calls == []

    authorized_sha, invalid_report = _review(investigation_id)
    invalid_report.test_reports[0].payload["execution_status"] = "compile_error"
    review.rows[investigation_id] = (invalid_report,)
    with pytest.raises(RetrohuntQualityError, match="successful exact-version test"):
        await _submit(
            service,
            investigation_id,
            invalid_report.version.id,
            authorized_sha,
        )
    assert await repo.count_for_subject(SUBJECT_ID) == 0
    assert provider.submit_calls == []

    _, approved = _review(investigation_id)
    approved.latest_decision = SimpleNamespace(
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=approved.version.id,
        details={"source_sha256": approved.version.source_sha256},
        reason="Approved for detection only.",
    )
    review.rows[investigation_id] = (approved,)
    with pytest.raises(RetrohuntQualityError, match="explicit human Retrohunt authorization"):
        await _submit(
            service, investigation_id, approved.version.id, approved.version.source_sha256
        )
    assert await repo.count_for_subject(SUBJECT_ID) == 0
    assert provider.submit_calls == []


@pytest.mark.asyncio
async def test_status_and_results_do_not_consume_slots_and_hits_are_candidates() -> None:
    service, repo, _, sources, provider, evidence = _service()
    investigation_id, version_id, source_sha256 = sources[0]
    submitted = await _submit(service, investigation_id, version_id, source_sha256)
    provider.job_state = RetrohuntProviderJobState.COMPLETED
    provider.hits = (RetrohuntHit(sha256=hashlib.sha256(b"hit").hexdigest()),)

    status = await service.status(
        investigation_id=investigation_id,
        reservation_id=submitted.reservation.id,
    )
    results = await service.results(
        investigation_id=investigation_id,
        reservation_id=submitted.reservation.id,
    )
    assert status.slots_consumed == results.slots_consumed == 1
    assert status.slots_remaining == results.slots_remaining == 1
    assert results.candidate_hits == (provider.hits[0].sha256,)
    assert results.observation_refs
    assert evidence.payloads[0]["qualification"] == "candidate"
    assert evidence.payloads[0]["confirmed_member"] is False
    assert evidence.payloads[0]["schema"] == "vt-retrohunt-candidate-hit.v1"
    assert await repo.count_for_subject(SUBJECT_ID) == 1


@pytest.mark.asyncio
async def test_status_provider_exception_is_typed_without_changing_reservation() -> None:
    service, repo, _, sources, provider, _ = _service()
    investigation_id, version_id, source_sha256 = sources[0]
    submitted = await _submit(service, investigation_id, version_id, source_sha256)
    provider.status_error = TimeoutError("simulated provider outage")

    status = await service.status(
        investigation_id=investigation_id,
        reservation_id=submitted.reservation.id,
    )

    assert status.provider_error is RetrohuntProviderErrorCode.PROVIDER_UNAVAILABLE
    assert status.provider_job_state == RetrohuntReservationState.SUBMITTED.value
    assert status.reservation.state is RetrohuntReservationState.SUBMITTED
    assert status.slots_consumed == 1 and status.slots_remaining == 1
    assert await repo.count_for_subject(SUBJECT_ID) == 1
    tool_result = _tool_result(status)
    assert tool_result.status is ToolInvocationStatus.UNAVAILABLE
    assert tool_result.output["provider_error"] == "provider_unavailable"
    assert tool_result.output["job_state"] == RetrohuntReservationState.SUBMITTED.value


@pytest.mark.parametrize(
    "reason",
    [
        "Ne pas autoriser Retrohunt",
        "not approved for retrohunt",
        "no retrohunt",
        "Prévalidation Retrohunt autorisée pour cette version exacte.",
    ],
)
@pytest.mark.asyncio
async def test_free_text_never_grants_retrohunt_authorization(reason: str) -> None:
    service, repo, review, sources, provider, _ = _service()
    investigation_id, _, _ = sources[0]
    source_sha256, unstructured = _review(
        investigation_id,
        retrohunt_authorized=None,
        decision_reason=reason,
    )
    review.rows[investigation_id] = (unstructured,)

    with pytest.raises(RetrohuntQualityError, match="explicit human Retrohunt authorization"):
        await _submit(
            service,
            investigation_id,
            unstructured.version.id,
            source_sha256,
        )

    assert await repo.count_for_subject(SUBJECT_ID) == 0
    assert provider.submit_calls == []


@pytest.mark.parametrize("mismatch", ["version", "sha256"])
@pytest.mark.asyncio
async def test_authorization_bound_to_another_version_or_sha_is_rejected(mismatch: str) -> None:
    service, repo, review, sources, provider, _ = _service()
    investigation_id, _, _ = sources[0]
    source_sha256, reviewed = _review(investigation_id)
    wrong_version_id = uuid4() if mismatch == "version" else reviewed.version.id
    wrong_sha256 = hashlib.sha256(b"different reviewed rule").hexdigest()
    authorization = RetrohuntAuthorization(
        authorized=True,
        rule_version_id=wrong_version_id,
        source_sha256=wrong_sha256 if mismatch == "sha256" else source_sha256,
    )
    reviewed.latest_decision = SimpleNamespace(
        decision_type=AnalystDecisionType.DETECTION_APPROVE,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=wrong_version_id,
        details={"source_sha256": source_sha256},
        reason="Reviewed for this version.",
        retrohunt_authorization=authorization,
    )
    review.rows[investigation_id] = (reviewed,)

    with pytest.raises(RetrohuntQualityError, match="explicit human Retrohunt authorization"):
        await _submit(service, investigation_id, reviewed.version.id, source_sha256)

    assert await repo.count_for_subject(SUBJECT_ID) == 0
    assert provider.submit_calls == []


@pytest.mark.asyncio
async def test_revoked_detection_decision_cannot_authorize_retrohunt() -> None:
    service, repo, review, sources, provider, _ = _service()
    investigation_id, _, _ = sources[0]
    source_sha256, previously_approved = _review(investigation_id)
    previously_approved.status = DetectionCandidateStatus.REJECTED
    previously_approved.latest_decision = AnalystDecision(
        investigation_id=investigation_id,
        decision_type=AnalystDecisionType.DETECTION_REJECT,
        target_type=AnalystDecisionTargetType.DETECTION,
        target_id=previously_approved.version.id,
        actor_id="analyst@example.test",
        reason="Revoke the prior detection approval.",
        correlation_id=str(uuid4()),
        details={"source_sha256": source_sha256},
    )
    review.rows[investigation_id] = (previously_approved,)

    with pytest.raises(RetrohuntQualityError, match="human prevalidation"):
        await _submit(
            service,
            investigation_id,
            previously_approved.version.id,
            source_sha256,
        )

    assert await repo.count_for_subject(SUBJECT_ID) == 0
    assert provider.submit_calls == []
