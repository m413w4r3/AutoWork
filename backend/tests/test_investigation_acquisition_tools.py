from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import (
    LOCAL_ANALYSIS_CAPABILITY,
    LOCAL_QUALIFICATION_CAPABILITY,
    VT_FILE_DOWNLOAD_CAPABILITY,
    LocalSampleAnalysisService,
    register_acquisition_tools,
)
from cti_app.application.investigation_governance import (
    InvestigationEnvelopeService,
    SampleQualificationService,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolEffect,
    ToolExecutionContext,
    validate_json_schema,
)
from cti_app.domain.analysis import SampleFeatureSetV1, SampleFormat
from cti_app.domain.classification import TLP
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    SampleOrigin,
    ToolInvocationStatus,
)
from tests.investigation_c2_fakes import (
    FakeDatabase,
    FakeUow,
    FakeVirusTotalPort,
    make_approved_proposal,
    make_evidence_recorder,
    make_investigation,
    make_sample_acquisition_service,
)


def _register(database: FakeDatabase, hits: HitAcquisitionService):
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
    )
    return registry


@pytest.mark.asyncio
async def test_tool_capabilities_and_unavailable_analysis_are_explicit() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve bounded VT access.",
        correlation_id="g2:tools",
    )
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=None,
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    download_definition, _ = registry.get("vt.download_sample", "1")
    analyze_definition, _ = registry.get("sample.analyze", "1")
    qualification_definition, _ = registry.get("sample.set_qualification", "1")
    assert download_definition.capability_key == VT_FILE_DOWNLOAD_CAPABILITY
    assert download_definition.effect is ToolEffect.ACQUIRE
    assert analyze_definition.capability_key == LOCAL_ANALYSIS_CAPABILITY
    assert qualification_definition.capability_key == LOCAL_QUALIFICATION_CAPABILITY
    validate_json_schema(
        {
            "association_id": str(UUID(int=1)),
            "qualification": "uncertain",
            "reason": "Keep the relationship tentative.",
            "evidence_refs": [],
        },
        qualification_definition.input_schema,
    )
    with pytest.raises(ValueError):
        validate_json_schema(
            {
                "association_id": str(UUID(int=1)),
                "qualification": "confirmed",
                "reason": "The agent cannot make G3.",
                "evidence_refs": [],
            },
            qualification_definition.input_schema,
        )

    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:analyze:unavailable",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )
    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(UUID(int=2)),
            "purpose": "Analyze quarantined sample",
            "evidence_refs": [],
        },
        purpose="Analyze quarantined sample",
        operation_key="op:sample:analyze:unavailable",
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    missing_capability_context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:analyze:missing-capability",
        available_capabilities=frozenset(),
    )
    missing_capability_result = await RegistryToolDispatcher(registry).dispatch(
        missing_capability_context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(UUID(int=2)),
            "purpose": "Capability absence check",
            "evidence_refs": [],
        },
        purpose="Capability absence check",
        operation_key="op:sample:analyze:missing-capability",
    )
    assert missing_capability_result.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_download_tool_requests_permission_outside_g2_envelope() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"request-permission sample"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation, allowed_providers=(), acquire_hits_allowed=False),
        actor_id="analyst:alice",
        reason="Local analysis only; defer VT acquisition.",
        correlation_id="g2:local-only",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:download:out-of-envelope",
        available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
    )
    _definition, adapter = registry.get("vt.download_sample", "1")

    adapter_result = await adapter.execute(
        {
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "Request an approved acquisition",
            "evidence_refs": [],
        },
        context,
    )
    dispatched_result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments={
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "Request an approved acquisition",
            "evidence_refs": [],
        },
        purpose="Request an approved acquisition",
        operation_key="op:download:out-of-envelope",
    )

    assert adapter_result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert adapter_result.output["request_permission"] is True
    # The existing dispatcher rejects ACQUIRE before reaching the adapter.
    assert dispatched_result.status is ToolInvocationStatus.POLICY_DENIED
    assert port.download_calls == []


@pytest.mark.asyncio
async def test_dispatcher_download_reuses_one_g2_reservation_on_replay() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"dispatcher idempotency fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Approve one hash-verified hit download.",
        correlation_id="g2:dispatcher-download",
    )
    port = FakeVirusTotalPort(content)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=make_evidence_recorder(database),
    )
    registry = _register(database, hits)
    dispatcher = RegistryToolDispatcher(registry)
    operation_key = "vt.download_sample:stable-operation-1"
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key=operation_key,
        available_capabilities=frozenset({VT_FILE_DOWNLOAD_CAPABILITY}),
    )
    arguments = {
        "sha256": hashlib.sha256(content).hexdigest(),
        "purpose": "Review this VT pivot.",
        "evidence_refs": [],
    }

    first = await dispatcher.dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Review this VT pivot.",
        operation_key=operation_key,
    )
    replay = await dispatcher.dispatch(
        context,
        tool_name="vt.download_sample",
        schema_version="1",
        arguments=arguments,
        purpose="Review this VT pivot.",
        operation_key=operation_key,
    )

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert replay == first
    assert port.download_calls == [arguments["sha256"]]
    assert len(database.attempts) == 1
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    assert len(database.decisions) == 1


@pytest.mark.asyncio
async def test_sample_analyze_records_bounded_binary_observations() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"MZ harmless static analysis fixture strings"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow bounded candidate acquisition and local analysis.",
        correlation_id="g2:analysis",
    )
    port = FakeVirusTotalPort(content)
    recorder = make_evidence_recorder(database)
    hits = HitAcquisitionService(
        lambda: FakeUow(database),
        acquisition=make_sample_acquisition_service(database, port),
        recorder=recorder,
    )
    sha256 = hashlib.sha256(content).hexdigest()
    association = await hits.register_hit(investigation.id, sha256, SampleOrigin.VT_PIVOT)
    acquired = await hits.acquire_hit(association.id, purpose="Review candidate fixture")
    assert acquired.sample_id is not None

    class ExistingStaticService:
        async def analyze(self, sample_id: UUID) -> SampleFeatureSetV1:
            sample = database.samples[sample_id]
            blob = database.blobs[sample.blob_id]
            return SampleFeatureSetV1(
                sample_id=sample_id,
                blob_id=sample.blob_id,
                extractor_version="static-v1",
                parameters_sha256="a" * 64,
                format=SampleFormat.UNKNOWN,
                size=len(content),
                md5=hashlib.md5(content).hexdigest(),
                sha1=hashlib.sha1(content).hexdigest(),
                sha256=blob.descriptor.sha256,
                strings=({"value": "harmless static analysis fixture", "kind": "ascii"},),
                sections=(),
                imports=(),
                exports=(),
                resources=(),
                signature=None,
                imphash=None,
                ssdeep=None,
                tlsh=None,
                rich_header_hash=None,
                opcode_fragment16=(),
                partial_errors=(),
                tlp=TLP.CLEAR,
                do_not_submit=True,
                external_llm_allowed=False,
            )

    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: FakeUow(database),
        recorder,
        static_analysis=ExistingStaticService(),
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
    )
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:sample:analysis",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Extract local static strings",
            "evidence_refs": [],
        },
        purpose="Extract local static strings",
        operation_key="op:sample:analysis",
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["sample_sha256"] == sha256
    assert result.output["qualification"] == "candidate"
    assert result.output["goodware"]["available"] is False
    assert {
        item.kind
        for item in database.observations.values()
        if item.kind in {EvidenceKind.BINARY_STRING, EvidenceKind.BINARY_ANALYSIS}
    } == {
        EvidenceKind.BINARY_STRING,
        EvidenceKind.BINARY_ANALYSIS,
    }
    assert len(result.observation_refs) == 2


@pytest.mark.asyncio
async def test_sample_analyze_injects_code_features_and_goodware_measurement() -> None:
    database = FakeDatabase()
    investigation = make_investigation()
    database.investigations[investigation.id] = investigation
    content = b"MZ harmless code feature and goodware fixture"
    envelope_service = InvestigationEnvelopeService(lambda: FakeUow(database))
    envelope = await envelope_service.approve(
        make_approved_proposal(investigation),
        actor_id="analyst:alice",
        reason="Allow bounded candidate acquisition and local analysis.",
        correlation_id="g2:code-features-goodware",
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
        SampleOrigin.VT_PIVOT,
    )
    acquired = await hits.acquire_hit(association.id, purpose="Review candidate fixture")
    assert acquired.sample_id is not None

    class ExistingStaticService:
        async def analyze(self, sample_id: UUID) -> SampleFeatureSetV1:
            sample = database.samples[sample_id]
            blob = database.blobs[sample.blob_id]
            return SampleFeatureSetV1(
                sample_id=sample_id,
                blob_id=sample.blob_id,
                extractor_version="static-v1",
                parameters_sha256="a" * 64,
                format=SampleFormat.UNKNOWN,
                size=len(content),
                md5=hashlib.md5(content).hexdigest(),
                sha1=hashlib.sha1(content).hexdigest(),
                sha256=blob.descriptor.sha256,
                strings=({"value": "fixture string", "kind": "ascii"},),
                sections=(),
                imports=(),
                exports=(),
                resources=(),
                signature=None,
                imphash=None,
                ssdeep=None,
                tlsh=None,
                rich_header_hash=None,
                opcode_fragment16=(),
                partial_errors=(),
                tlp=TLP.CLEAR,
                do_not_submit=True,
                external_llm_allowed=False,
            )

    class ExistingCodeFeatureSet:
        tool_version = "smda-v1"
        escaper_compatibility_version = "escaper-v1"
        intel_pic_hash_escape_version = "pic-v1"
        status = type("Status", (), {"value": "SUCCEEDED"})()
        architecture = "x86"
        ngrams = ()
        errors = ()

        def as_json(self):
            return {"packing": {"packed": False}}

    class ExistingCodeFeatureService:
        async def extract(self, **_kwargs):
            return ExistingCodeFeatureSet()

    baseline_id = uuid4()

    class BaselineLookup:
        async def get(self, _investigation_id):
            return baseline_id

    class AnalysisUow(FakeUow):
        async def __aenter__(self):
            await super().__aenter__()
            self.investigation_goodware_baselines = BaselineLookup()
            return self

    class ExistingGoodwareService:
        prepared = None
        features = ()

        async def prepare(self, baseline):
            self.prepared = baseline

        async def lookup_batch(self, baseline, features):
            self.features = tuple(features)
            return {self.features[0]: 4} if self.features else {}

    goodware = ExistingGoodwareService()
    analyzer = LocalSampleAnalysisService(
        hits,
        lambda: AnalysisUow(database),
        make_evidence_recorder(database),
        static_analysis=ExistingStaticService(),
        code_features=ExistingCodeFeatureService(),
        goodware=goodware,
    )
    registry = InMemoryToolRegistry()
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(lambda: FakeUow(database)),
        analyzer=analyzer,
    )
    context = ToolExecutionContext(
        investigation_id=investigation.id,
        envelope=envelope,
        budget=envelope.budget,
        idempotency_key="tool:sample:code-features-goodware",
        available_capabilities=frozenset({LOCAL_ANALYSIS_CAPABILITY}),
    )

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="sample.analyze",
        schema_version="1",
        arguments={
            "association_id": str(association.id),
            "purpose": "Compare local code features with the bound goodware corpus",
            "evidence_refs": [],
        },
        purpose="Run local feature extraction and goodware comparison.",
        operation_key="op:sample:code-features-goodware",
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["extractors"]["smda"]["version"] == "smda-v1"
    assert result.output["goodware"]["available"] is True
    assert result.output["goodware"]["features_present_in_goodware"] == 1
    assert goodware.prepared == baseline_id
    assert goodware.features
    analysis_observation = next(
        database.observations[reference]
        for reference in result.observation_refs
        if database.observations[reference].kind is EvidenceKind.BINARY_ANALYSIS
    )
    assert analysis_observation.source_version != "unknown"
