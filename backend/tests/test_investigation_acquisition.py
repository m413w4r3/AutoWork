from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_governance import InvestigationEnvelopeService
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

    async def file_download(self, file_hash: str, *, sink) -> None:
        self.download_calls.append(file_hash)
        if self.partial_bytes:
            sink.write(self.sample_bytes[: self.partial_bytes])
        raise self.error


def _approved_service(database: FakeDatabase, investigation, content: bytes):
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    proposal = make_approved_proposal(investigation)
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
    ("error", "error_code", "reason"),
    [
        pytest.param(
            VirusTotalHttpError(
                "synthetic route absent",
                code="virustotal_not_found",
                status_code=404,
                route="/files/{hash}/download",
            ),
            "virustotal_download_route_not_found",
            "provider_download_route_not_found",
            id="download-route-404",
        ),
        pytest.param(
            VirusTotalHttpError(
                "synthetic route absent",
                code="virustotal_not_found",
                status_code=404,
                route="/files/{hash}/download_url",
            ),
            "virustotal_download_route_not_found",
            "provider_download_route_not_found",
            id="download-url-route-404",
        ),
        pytest.param(
            VirusTotalHttpError(
                "synthetic authorization refused",
                code="virustotal_not_authorized",
                status_code=403,
            ),
            "virustotal_download_authorization_refused",
            "provider_download_authorization_refused",
            id="authorization-403",
        ),
        pytest.param(
            TimeoutError("synthetic timeout before bytes"),
            "virustotal_download_failed_before_bytes",
            "provider_download_failed_before_bytes",
            id="timeout-before-bytes",
        ),
    ],
)
async def test_definitive_download_failures_are_unavailable_and_cached_by_hash(
    error: Exception, error_code: str, reason: str
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
    assert second.cached is True
    assert port.download_calls == [sample_hash]
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
    assert port.download_calls == [sample_hash, different_hash]


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
async def test_known_article_sample_is_not_downloaded_or_reported_as_new() -> None:
    database = FakeDatabase()
    content = b"already documented sample fixture"
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    await envelope_service.approve(
        make_approved_proposal(investigation),
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

    assert result.status is ToolInvocationStatus.EMPTY
    assert result.novelty is not None
    assert result.novelty.novelty.value == "not_confirmed"
    assert result.novelty.reason.code.value == "already_documented"
    assert result.association.qualification is SampleQualification.CANDIDATE
    assert port.download_calls == []
    assert not any(
        item.qualification is SampleQualification.CONFIRMED
        for item in database.associations.values()
    )


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
            make_approved_proposal(investigation),
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
    assert second_result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert second_result.request_permission is True
    assert len(database.attempts) == 1
    assert {attempt.investigation_id for attempt in database.attempts} == {
        first_investigation.id,
    }
    assert port.download_calls == [sha256]
