from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_governance import InvestigationEnvelopeService
from cti_app.application.sample_acquisition import SampleAcquisitionCacheScope
from cti_app.application.virustotal import (
    VirusTotalDownloadHashMismatchError,
    VirusTotalHttpError,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    InvestigationBudget,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
)
from tests.investigation_c2_fakes import (
    FakeBlob,
    FakeBlobDescriptor,
    FakeDatabase,
    FakeUow,
    FakeVirusTotalPort,
    make_approved_proposal,
    make_article_novelty_service,
    make_evidence_recorder,
    make_investigation,
    make_sample_acquisition_service,
)


class _FailingVirusTotalPort(FakeVirusTotalPort):
    def __init__(self, content: bytes, error: Exception, *, partial_bytes: int = 0) -> None:
        super().__init__(content)
        self.error = error
        self.partial_bytes = partial_bytes
        self.sample_acquisition_cache_scope = SampleAcquisitionCacheScope(
            provider="virustotal",
            account_capability_fingerprint="account-capability-a",
            route_fingerprint="/api/v3/files/{hash}/download",
            transport="proxy-v3",
            configuration_fingerprint="proxy-config-v1",
        )

    async def file_download(self, file_hash: str, *, sink) -> None:
        self.download_calls.append(file_hash)
        if self.partial_bytes:
            sink.write(self.sample_bytes[: self.partial_bytes])
        raise self.error


class _ResponseOriginHttpError(VirusTotalHttpError):
    def __init__(self, message: str, *, response_origin: str, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.response_origin = response_origin


def _approved_service(database: FakeDatabase, investigation, content: bytes):
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    proposal = make_approved_proposal(investigation, external_llm_allowed=True)
    port = FakeVirusTotalPort(content)
    return (
        envelope_service,
        proposal,
        HitAcquisitionService(
            lambda: FakeUow(database),
            acquisition=make_sample_acquisition_service(database, port),
            recorder=make_evidence_recorder(database),
            novelty=make_article_novelty_service(database, investigation),
        ),
        port,
    )


@pytest.mark.asyncio
async def test_register_hit_persists_new_association_before_acquisition() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    sample_hash = hashlib.sha256(b"synthetic unacquired VT hit").hexdigest()
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=None,
        recorder=make_evidence_recorder(database),
    )

    assert database.associations == {}
    association = await hits.register_hit(investigation.id, sample_hash, SampleOrigin.VT_PIVOT)

    async with FakeUow(database) as uow:
        persisted = await uow.sample_associations.get(association.id)
    assert persisted == association
    assert persisted is not None
    assert persisted.sample_sha256 == sample_hash
    assert persisted.origin is SampleOrigin.VT_PIVOT
    assert database.associations == {association.id: association}


@pytest.mark.asyncio
async def test_default_g2_envelope_refuses_acquisition_as_a_review_request() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    port = FakeVirusTotalPort(b"candidate fixture")
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(
        investigation.id,
        hashlib.sha256(port.sample_bytes).hexdigest(),
        SampleOrigin.VT_PIVOT,
    )

    result = await hits.acquire_hit(association.id, purpose="Review a VT pivot")

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.request_permission is True
    assert result.association.qualification is SampleQualification.CANDIDATE
    assert port.download_calls == []


@pytest.mark.asyncio
async def test_approved_g2_acquires_unknown_hit_without_g3_and_replay_is_free() -> None:
    database = FakeDatabase()
    content = b"harmless unknown hit fixture"
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service, proposal, hits, port = _approved_service(database, investigation, content)
    await envelope_service.approve(
        proposal,
        actor_id="analyst:alice",
        reason="Allow up to ten hash-verified VT hit downloads.",
        correlation_id="g2:acquisition",
    )
    sha256 = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)

    first = await hits.acquire_hit(
        association.id, purpose="Review the new pivot", operation_key="op:download:1"
    )
    g2_after_first = await envelope_service.current(investigation.id)
    second = await hits.acquire_hit(
        association.id, purpose="Review the new pivot", operation_key="op:download:1"
    )

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert first.association.qualification is SampleQualification.CANDIDATE
    assert first.novelty is not None
    assert first.novelty.novelty.value == "new_to_article"
    assert len(first.observation_refs) == 1
    assert len(database.decisions) == 1
    assert second.status is ToolInvocationStatus.SUCCEEDED
    assert second.reused_existing_sample is True
    assert second.association.qualification is SampleQualification.CANDIDATE
    assert port.download_calls == [sha256]
    assert len(database.attempts) == 1
    assert investigation.budget.consumed_vt_read_units == 1
    assert investigation.budget.consumed_hits_acquired == 1
    assert g2_after_first is not None
    assert g2_after_first.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert g2_after_first.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    g2_after_replay = await envelope_service.current(investigation.id)
    assert g2_after_replay is not None
    assert g2_after_replay.budget.consumed == g2_after_first.budget.consumed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "error_code", "reason", "cacheable"),
    [
        pytest.param(
            _ResponseOriginHttpError(
                "synthetic route absent",
                code="virustotal_not_found",
                status_code=404,
                route="/files/{hash}/download",
                response_origin="proxy",
            ),
            "virustotal_download_route_not_found",
            "proxy_route_not_found",
            False,
            id="download-route-404",
        ),
        pytest.param(
            _ResponseOriginHttpError(
                "synthetic provider file absent",
                code="virustotal_not_found",
                status_code=404,
                route="/files/{hash}/download",
                response_origin="provider",
            ),
            "virustotal_download_file_not_found",
            "provider_file_not_found",
            True,
            id="upstream-file-404",
        ),
        pytest.param(
            _ResponseOriginHttpError(
                "synthetic authorization refused",
                code="virustotal_not_authorized",
                status_code=403,
                response_origin="provider",
            ),
            "virustotal_download_authorization_refused",
            "provider_download_authorization_refused",
            True,
            id="authorization-403",
        ),
        pytest.param(
            _ResponseOriginHttpError(
                "synthetic rate limit",
                code="virustotal_quota_exceeded",
                status_code=429,
                response_origin="provider",
            ),
            "virustotal_download_rate_limited",
            "provider_download_rate_limited",
            False,
            id="rate-limit-429",
        ),
        pytest.param(
            TimeoutError("synthetic timeout before bytes"),
            "virustotal_download_timeout",
            "provider_download_timeout",
            False,
            id="timeout-before-bytes",
        ),
    ],
)
async def test_download_failures_follow_scoped_refusal_cache_policy(
    error: Exception, error_code: str, reason: str, cacheable: bool
) -> None:
    database = FakeDatabase()
    content = b"synthetic sample bytes"
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    await envelope_service.approve(
        make_approved_proposal(investigation, vt_units=8, downloads=8),
        actor_id="analyst:synthetic",
        reason="Allow bounded synthetic sample review.",
        correlation_id="g2:synthetic-download-failure",
    )
    port = _FailingVirusTotalPort(content, error)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sample_hash = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sample_hash, SampleOrigin.VT_PIVOT)

    first = await hits.acquire_hit(
        association.id, purpose="Review the synthetic hit", operation_key="synthetic-download-1"
    )
    second = await hits.acquire_hit(
        association.id, purpose="Retry the synthetic hit", operation_key="synthetic-download-2"
    )

    assert first.status is ToolInvocationStatus.UNAVAILABLE
    assert first.error_code == error_code
    assert first.reason == reason
    assert first.confirmed_non_execution is True
    assert str(error) not in first.message
    assert first.budget_consumed == {
        BudgetDimension.VT_UNITS: 1,
        BudgetDimension.DOWNLOADS: 0,
    }
    assert second.status is ToolInvocationStatus.UNAVAILABLE
    assert second.error_code == error_code
    assert second.cached is cacheable
    if cacheable:
        assert port.download_calls == [sample_hash]
        cached_budget = await envelope_service.current(investigation.id)
        assert cached_budget is not None
        assert cached_budget.budget.consumed[BudgetDimension.VT_UNITS] == 1
        refusal_attempt = next(
            attempt for attempt in reversed(database.attempts) if attempt.outcome.value == "error"
        )
        expected_kind_marker = "n" if error_code == "virustotal_download_file_not_found" else "a"
        assert refusal_attempt.error_code.startswith(f"rc1{expected_kind_marker}")
        current_scope = port.sample_acquisition_cache_scope
        scope_changes = (
            {"provider": "virustotal-v2"},
            {"account_capability_fingerprint": "account-capability-b"},
            {"route_fingerprint": "/vtapi/v2/file/download"},
            {"transport": "direct-v2"},
            {"configuration_fingerprint": "proxy-config-v2"},
        )
        for index, changed in enumerate(scope_changes, start=1):
            values = {
                "provider": current_scope.provider,
                "account_capability_fingerprint": current_scope.account_capability_fingerprint,
                "route_fingerprint": current_scope.route_fingerprint,
                "transport": current_scope.transport,
                "configuration_fingerprint": current_scope.configuration_fingerprint,
            }
            values.update(changed)
            port.sample_acquisition_cache_scope = SampleAcquisitionCacheScope(**values)
            retry = await hits.acquire_hit(
                association.id,
                purpose="Retry after the adapter scope changes",
                operation_key=f"synthetic-download-scope-changed-{index}",
            )
            assert retry.status is ToolInvocationStatus.UNAVAILABLE
            assert retry.cached is False
            assert retry.budget_consumed == {
                BudgetDimension.VT_UNITS: 1,
                BudgetDimension.DOWNLOADS: 0,
            }
            current_scope = port.sample_acquisition_cache_scope
        assert port.download_calls == [sample_hash] * (1 + len(scope_changes))
    else:
        assert port.download_calls == [sample_hash, sample_hash]
    different_hash = hashlib.sha256(b"another synthetic hash").hexdigest()
    different_association = await hits.register_hit(
        investigation.id, different_hash, SampleOrigin.VT_PIVOT
    )
    different_result = await hits.acquire_hit(
        different_association.id,
        purpose="Review a different synthetic hit",
        operation_key="synthetic-download-different-hash",
    )
    assert different_result.status is ToolInvocationStatus.UNAVAILABLE
    assert port.download_calls[-1] == different_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "partial_bytes", "error_code"),
    [
        pytest.param(
            TimeoutError("synthetic timeout after partial bytes"),
            5,
            "virustotal_download_interrupted_after_bytes",
            id="partial-write-then-timeout",
        ),
        pytest.param(
            VirusTotalDownloadHashMismatchError("synthetic hash mismatch"),
            22,
            "virustotal_download_hash_mismatch",
            id="hash-mismatch",
        ),
    ],
)
async def test_ambiguous_download_failures_remain_needs_review(
    error: Exception, partial_bytes: int, error_code: str
) -> None:
    database = FakeDatabase()
    content = b"synthetic sample bytes"
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    await envelope_service.approve(
        make_approved_proposal(investigation, vt_units=4, downloads=4),
        actor_id="analyst:synthetic",
        reason="Allow bounded synthetic sample review.",
        correlation_id="g2:synthetic-ambiguous-download",
    )
    port = _FailingVirusTotalPort(content, error, partial_bytes=partial_bytes)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sample_hash = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sample_hash, SampleOrigin.VT_PIVOT)

    result = await hits.acquire_hit(
        association.id, purpose="Review the synthetic hit", operation_key="synthetic-ambiguous-1"
    )

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.error_code == error_code
    assert result.confirmed_non_execution is False
    assert port.download_calls == [sample_hash]


@pytest.mark.asyncio
async def test_g2_download_extension_allows_second_and_third_acquisitions() -> None:
    database = FakeDatabase()
    investigation = make_investigation(max_hits=1, max_vt_units=1)
    investigation.budget.max_new_samples = 1
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    initial = await envelope_service.approve(
        make_approved_proposal(investigation, vt_units=1, downloads=1),
        actor_id="analyst:alice",
        reason="Allow one initial candidate download.",
        correlation_id="g2:download-extension:initial",
    )

    results = []
    ports = []
    for index in range(1, 4):
        if index == 2:
            current = await envelope_service.current(investigation.id)
            assert current is not None
            await envelope_service.extend(
                investigation.id,
                expected_version=current.version,
                allowed_providers=("vt",),
                budget=InvestigationBudget(
                    limits={BudgetDimension.VT_UNITS: 3, BudgetDimension.DOWNLOADS: 3}
                ),
                acquire_hits_allowed=True,
                disclosure=current.disclosure,
                actor_id="analyst:alice",
                reason="The first sample exposed two additional branches.",
                correlation_id="g2:download-extension:increase",
            )

        content = f"harmless candidate fixture {index}".encode()
        port = FakeVirusTotalPort(content)
        ports.append(port)
        hits = HitAcquisitionService(
            lambda: FakeUow(database),
            acquisition=make_sample_acquisition_service(database, port),
            recorder=make_evidence_recorder(database),
            novelty=make_article_novelty_service(database, investigation),
        )
        association = await hits.register_hit(
            investigation.id,
            hashlib.sha256(content).hexdigest(),
            SampleOrigin.VT_PIVOT,
        )
        results.append(
            await hits.acquire_hit(
                association.id,
                purpose="Review an additional branch.",
                operation_key=f"op:download:extension:{index}",
            )
        )

    assert initial.budget.limits[BudgetDimension.DOWNLOADS] == 1
    assert [result.status for result in results] == [
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.SUCCEEDED,
    ]
    assert [len(port.download_calls) for port in ports] == [1, 1, 1]
    assert investigation.budget.consumed_hits_acquired == 3
    assert investigation.budget.consumed_vt_read_units == 3


@pytest.mark.asyncio
async def test_known_article_hash_without_bytes_is_acquired_without_becoming_confirmed() -> None:
    database = FakeDatabase()
    content = b"already documented sample fixture"
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    await envelope_service.approve(
        make_approved_proposal(investigation, external_llm_allowed=True),
        actor_id="analyst:alice",
        reason="Allow this bounded VT pivot.",
        correlation_id="g2:known-sample",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
        novelty=make_article_novelty_service(
            database,
            investigation,
            documented_samples=(hashlib.sha256(content).hexdigest(),),
        ),
    )
    association = await hits.register_hit(
        investigation.id,
        hashlib.sha256(content).hexdigest(),
        SampleOrigin.RELATIONSHIP,
    )

    result = await hits.acquire_hit(association.id, purpose="Check article novelty")

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.sample_id is not None
    assert result.novelty is not None
    assert result.novelty.novelty.value == "not_confirmed"
    assert result.novelty.reason.code.value == "already_documented"
    assert result.association.qualification is SampleQualification.CANDIDATE
    assert port.download_calls == [hashlib.sha256(content).hexdigest()]
    sample = database.samples[result.sample_id]
    assert sample.state.value == "review_candidate"
    replay = await hits.acquire_hit(
        association.id,
        purpose="Replay the same known article hash",
        operation_key="known-sample-replay",
    )
    assert replay.status is ToolInvocationStatus.SUCCEEDED
    assert replay.reused_existing_sample is True
    assert port.download_calls == [hashlib.sha256(content).hexdigest()]
    assert not any(
        item.qualification is SampleQualification.CONFIRMED
        for item in database.associations.values()
    )


@pytest.mark.asyncio
async def test_quarantine_blob_without_sample_is_not_reused_or_downloaded_again() -> None:
    database = FakeDatabase()
    content = b"orphan quarantine bytes"
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow a bounded hash review.",
        correlation_id="g2:orphan-quarantine-blob",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sha256 = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    blob = FakeBlob(
        uuid4(),
        FakeBlobDescriptor(
            logical_bucket="samples-quarantine",
            sha256=sha256,
            size=len(content),
        ),
    )
    database.blobs[blob.id] = blob

    result = await hits.acquire_hit(
        association.id,
        purpose="Reconcile existing quarantined bytes without a Sample record.",
    )

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "without a linked Sample" in result.message
    assert port.download_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tlp", "do_not_submit"),
    ((TLPLevel.AMBER, False), (TLPLevel.CLEAR, True)),
)
async def test_tlp_and_do_not_submit_prohibit_vt_download(tlp, do_not_submit) -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"policy fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    await envelope_service.approve(
        make_approved_proposal(investigation, tlp=tlp, do_not_submit=do_not_submit),
        actor_id="analyst:alice",
        reason="The policy boundary is deliberate.",
        correlation_id=f"g2:policy:{tlp.value}:{do_not_submit}",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    association = await hits.register_hit(
        investigation.id,
        hashlib.sha256(content).hexdigest(),
        SampleOrigin.BEHAVIOR,
    )

    result = await hits.acquire_hit(association.id, purpose="Policy denial check")

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert port.download_calls == []


@pytest.mark.asyncio
async def test_same_subject_investigations_keep_associations_and_attempts_scoped() -> None:
    database = FakeDatabase()
    subject_id = uuid4()
    first_investigation = make_investigation(subject_id=subject_id)
    second_investigation = make_investigation(subject_id=subject_id)
    database.investigations[first_investigation.id] = first_investigation
    database.investigations[second_investigation.id] = second_investigation
    first_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    second_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    for service, investigation, correlation in (
        (first_service, first_investigation, "g2:first"),
        (second_service, second_investigation, "g2:second"),
    ):
        await service.approve(
            make_approved_proposal(investigation, external_llm_allowed=True),
            actor_id="analyst:alice",
            reason="Approve this investigation's own hit budget.",
            correlation_id=correlation,
        )
    content = b"same subject but separate investigations"
    port = FakeVirusTotalPort(content)
    service = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sha256 = hashlib.sha256(content).hexdigest()

    first = await service.register_hit(first_investigation.id, sha256, SampleOrigin.VT_PIVOT)
    second = await service.register_hit(second_investigation.id, sha256, SampleOrigin.RELATIONSHIP)
    assert first.id != second.id
    assert first.investigation_id != second.investigation_id
    assert (
        await service.register_hit(first_investigation.id, sha256, SampleOrigin.BEHAVIOR)
    ).id == first.id

    first_result = await service.acquire_hit(first.id, purpose="First investigation hit")
    second_result = await service.acquire_hit(second.id, purpose="Second investigation hit")

    assert first_result.status is ToolInvocationStatus.SUCCEEDED
    assert second_result.status is ToolInvocationStatus.SUCCEEDED
    assert second_result.reused_existing_sample is True
    assert second_result.sample_id == first_result.sample_id
    assert second_result.association.id == second.id
    assert second_result.budget_consumed == {
        BudgetDimension.VT_UNITS: 0,
        BudgetDimension.DOWNLOADS: 0,
    }
    assert len(database.attempts) == 1
    assert {attempt.investigation_id for attempt in database.attempts} == {
        first_investigation.id,
    }
    assert port.download_calls == [sha256]
    source_sample = database.samples[first_result.sample_id]
    assert source_sample.origin == "virustotal_file_download"
    assert source_sample.origin_kind.value == "vt_hunt_hit"
    reused_events = [
        event for event in database.provenance if event.event_type == "reused_from_subject_sample"
    ]
    assert len(reused_events) == 1
    assert reused_events[0].aggregate_id == source_sample.id
    assert reused_events[0].payload["sample_origin"] == source_sample.origin
    assert reused_events[0].payload["investigation_id"] == str(second_investigation.id)
    assert second_investigation.budget.consumed_hits_acquired == 0
    assert second_investigation.budget.consumed_vt_read_units == 0

    replay = await service.acquire_hit(
        second.id, purpose="Replay subject sample reuse", operation_key="second-run-replay"
    )
    assert replay.status is ToolInvocationStatus.SUCCEEDED
    assert replay.association.id == second_result.association.id
    assert replay.sample_id == first_result.sample_id
    assert (
        len([e for e in database.provenance if e.event_type == "reused_from_subject_sample"]) == 1
    )
    assert port.download_calls == [sha256]


@pytest.mark.asyncio
async def test_subject_sample_from_another_subject_is_not_reused() -> None:
    database = FakeDatabase()
    source_investigation = make_investigation()
    target_investigation = make_investigation()
    database.investigations[source_investigation.id] = source_investigation
    database.investigations[target_investigation.id] = target_investigation
    source_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    target_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    for svc, investigation, correlation in (
        (source_envelopes, source_investigation, "g2:other-subject-source"),
        (target_envelopes, target_investigation, "g2:other-subject-target"),
    ):
        await svc.approve(
            make_approved_proposal(investigation),
            actor_id="analyst:alice",
            reason="Approve bounded sample review.",
            correlation_id=correlation,
        )
    content = b"different subject sample fixture"
    port = FakeVirusTotalPort(content)
    service = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sha256 = hashlib.sha256(content).hexdigest()
    source_association = await service.register_hit(
        source_investigation.id, sha256, SampleOrigin.VT_PIVOT
    )
    source_result = await service.acquire_hit(
        source_association.id, purpose="Acquire for source subject"
    )
    target_association = await service.register_hit(
        target_investigation.id, sha256, SampleOrigin.VT_PIVOT
    )

    result = await service.acquire_hit(target_association.id, purpose="Review other subject hash")

    assert source_result.status is ToolInvocationStatus.SUCCEEDED
    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "subject ownership" in result.message
    assert result.budget_consumed == {}
    assert port.download_calls == [sha256]


@pytest.mark.asyncio
async def test_corrupt_subject_sample_bytes_are_not_reused() -> None:
    database = FakeDatabase()
    subject_id = uuid4()
    first_investigation = make_investigation(subject_id=subject_id)
    second_investigation = make_investigation(subject_id=subject_id)
    database.investigations[first_investigation.id] = first_investigation
    database.investigations[second_investigation.id] = second_investigation
    first_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    second_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    for svc, investigation, correlation in (
        (first_envelopes, first_investigation, "g2:corrupt-source"),
        (second_envelopes, second_investigation, "g2:corrupt-target"),
    ):
        await svc.approve(
            make_approved_proposal(investigation, external_llm_allowed=True),
            actor_id="analyst:alice",
            reason="Approve bounded sample review.",
            correlation_id=correlation,
        )
    content = b"same subject corruptible bytes"
    port = FakeVirusTotalPort(content)
    service = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sha256 = hashlib.sha256(content).hexdigest()
    first = await service.register_hit(first_investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await service.acquire_hit(first.id, purpose="Acquire verified source bytes")
    assert acquired.sample_id is not None
    sample = database.samples[acquired.sample_id]
    database.blob_bytes[sample.blob_id] = b"corrupted bytes"
    second = await service.register_hit(second_investigation.id, sha256, SampleOrigin.VT_PIVOT)

    result = await service.acquire_hit(second.id, purpose="Reject corrupt source bytes")

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "SHA-256 verification" in result.message
    assert result.budget_consumed == {}
    assert port.download_calls == [sha256]


@pytest.mark.asyncio
async def test_incompatible_subject_sample_disclosure_requires_review() -> None:
    database = FakeDatabase()
    subject_id = uuid4()
    first_investigation = make_investigation(subject_id=subject_id)
    second_investigation = make_investigation(subject_id=subject_id)
    database.investigations[first_investigation.id] = first_investigation
    database.investigations[second_investigation.id] = second_investigation
    first_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    second_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    await first_envelopes.approve(
        make_approved_proposal(first_investigation, external_llm_allowed=True),
        actor_id="analyst:alice",
        reason="The original investigation permits external LLM processing.",
        correlation_id="g2:external-source",
    )
    await second_envelopes.approve(
        make_approved_proposal(second_investigation, external_llm_allowed=False),
        actor_id="analyst:alice",
        reason="The new investigation forbids external LLM processing.",
        correlation_id="g2:external-target",
    )
    content = b"disclosure mismatch sample"
    port = FakeVirusTotalPort(content)
    service = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sha256 = hashlib.sha256(content).hexdigest()
    first = await service.register_hit(first_investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await service.acquire_hit(first.id, purpose="Acquire permissive source sample")
    assert acquired.sample_id is not None
    # The new VT acquisition contract records True; the target investigation's
    # stricter disclosure policy still prevents cross-investigation reuse.
    assert database.samples[acquired.sample_id].external_llm_allowed is True
    second = await service.register_hit(second_investigation.id, sha256, SampleOrigin.VT_PIVOT)

    result = await service.acquire_hit(second.id, purpose="Check policy compatibility")

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "policy is incompatible" in result.message
    assert port.download_calls == [sha256]


@pytest.mark.asyncio
async def test_subject_sample_with_higher_tlp_requires_review() -> None:
    database = FakeDatabase()
    subject_id = uuid4()
    first_investigation = make_investigation(subject_id=subject_id)
    second_investigation = make_investigation(subject_id=subject_id)
    database.investigations[first_investigation.id] = first_investigation
    database.investigations[second_investigation.id] = second_investigation
    first_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    second_envelopes = InvestigationEnvelopeService(lambda: FakeUow(database))
    await first_envelopes.approve(
        make_approved_proposal(first_investigation, tlp=TLPLevel.GREEN),
        actor_id="analyst:alice",
        reason="The source sample is classified GREEN.",
        correlation_id="g2:tlp-source",
    )
    await second_envelopes.approve(
        make_approved_proposal(second_investigation, tlp=TLPLevel.CLEAR),
        actor_id="analyst:alice",
        reason="The new investigation is classified CLEAR.",
        correlation_id="g2:tlp-target",
    )
    content = b"TLP mismatch sample"
    port = FakeVirusTotalPort(content)
    service = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    sha256 = hashlib.sha256(content).hexdigest()
    first = await service.register_hit(first_investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await service.acquire_hit(first.id, purpose="Acquire GREEN source sample")
    assert acquired.status is ToolInvocationStatus.SUCCEEDED
    second = await service.register_hit(second_investigation.id, sha256, SampleOrigin.VT_PIVOT)

    result = await service.acquire_hit(second.id, purpose="Check TLP compatibility")

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert "classified GREEN" in result.message
    assert "clear disclosure bound" in result.message
    assert port.download_calls == [sha256]
