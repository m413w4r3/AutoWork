from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

import httpx
import pytest

from cti_app.application.analyst_handoff import AnalystHandoffPolicy
from cti_app.application.article_novelty import (
    ArticleNoveltyService,
    NoveltyCandidate,
    NoveltyCandidateKind,
)
from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.external_effects import FrameworkNoSendProof
from cti_app.application.investigation_contracts import ArticleClaimV1, ArticleContextV1
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.investigation_state import (
    InMemoryInvestigationContextSnapshotRepository,
    InMemoryInvestigationStateRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
    ToolResult,
    validate_json_schema,
)
from cti_app.application.investigation_vt_tools import (
    BEHAVIOUR_PCAP_SCHEMA,
    BEHAVIOUR_SCHEMA,
    FILE_REPORT_SCHEMA,
    INTELLIGENCE_SEARCH_SCHEMA,
    RELATIONSHIPS_SCHEMA,
    register_virustotal_tools,
    virustotal_tool_capabilities,
)
from cti_app.application.research_context_builder import ArticleSnapshot, ResearchContextBuilder
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalDownloadResult,
    VirusTotalExternalEffectNeedsReviewError,
    VirusTotalExternalEffectRefusedError,
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
    VirusTotalNotAuthorizedError,
    VirusTotalObjectReport,
    VirusTotalOperationRoute,
    VirusTotalPage,
    VirusTotalQuotaExceededError,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
    VirusTotalSearchResult,
    VirusTotalTransportError,
    record_virustotal_external_effect,
    validate_search_query,
)
from cti_app.domain.analyst_opinion import (
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.entities import Sample
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    DisclosureBounds,
    EvidenceKind,
    FindingAssessment,
    FindingKind,
    FrameworkPreSubmissionProof,
    InvestigationBudget,
    InvestigationEnvelope,
    SampleAssociation,
    SampleOrigin,
    TLPLevel,
    ToolInvocationStatus,
    canonical_json_bytes,
    tool_operation_key,
)
from cti_app.domain.network_protocol import NetworkCaptureFormat, NetworkCaptureRefV1
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.virustotal import (
    VirusTotalBehaviourSummary,
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalObjectType,
    VirusTotalObservation,
    VirusTotalOperation,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter
from tests.virustotal_external_effect_fakes import (
    configure_test_vt_adapter,
    make_test_authority,
)

_SEED_HASH = "a" * 64
_CANDIDATE_HASH = "b" * 64
_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")


def _page(
    items: tuple[dict[str, Any], ...] = (),
    *,
    next_cursor: str | None = None,
    exhaustive: bool = True,
    behaviour_summaries: tuple[VirusTotalBehaviourSummary, ...] = (),
) -> VirusTotalPage:
    payload: dict[str, Any] = {"data": list(items)}
    if next_cursor is not None:
        payload["meta"] = {"cursor": next_cursor}
    raw = json.dumps(payload).encode()
    return VirusTotalPage(
        items=items,
        next_cursor=next_cursor,
        observed_count=len(items),
        stopped_due_to_limit=not exhaustive,
        exhaustive=exhaustive,
        raw_json_pages=(raw,),
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
        empty=not items,
        behaviour_summaries=behaviour_summaries,
    )


def _search_result(
    items: tuple[dict[str, Any], ...] = (),
    *,
    next_cursor: str | None = None,
    exhaustive: bool = True,
) -> VirusTotalSearchResult:
    payload: dict[str, Any] = {"data": list(items)}
    if next_cursor is not None:
        payload["meta"] = {"cursor": next_cursor}
    return VirusTotalSearchResult(
        items=items,
        next_cursor=next_cursor,
        observed_count=len(items),
        stopped_due_to_limit=not exhaustive,
        exhaustive=exhaustive,
        raw_json_pages=(json.dumps(payload).encode(),),
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
        empty=not items,
        descriptors_only=True,
        order=None,
    )


def _file_report(file_hash: str = _SEED_HASH) -> VirusTotalFileReport:
    raw = json.dumps(
        {
            "data": {
                "id": file_hash,
                "attributes": {
                    "last_analysis_date": 1_781_300_000,
                    "VT_API_KEY": "supersecret-response-value",
                    "signed_url": "https://example.test/download?token=signed-value",
                    "api_key": "cti-response-value",
                },
            }
        }
    ).encode()
    return VirusTotalFileReport(
        file=VirusTotalFile(
            id=file_hash,
            type="file",
            lookup_value=file_hash,
            meaningful_name="VT_API_KEY=supersecret-name api_key=cti-name",
            type_description="Win32 EXE",
            size=321,
            last_analysis_stats={"malicious": 4, "undetected": 59},
            last_submission_date=1_781_000_000,
            last_modification_date=1_781_100_000,
            tags=("https://example.test/?token=signed-value", "peexe"),
        ),
        raw_json=raw,
        http_status=200,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )


class FakeVirusTotalPort:
    """No-network fake with per-operation response queues and call counts."""

    def __init__(self) -> None:
        self.calls = {
            "file_report": 0,
            "object_report": 0,
            "object_relationship": 0,
            "intelligence_search": 0,
            "behaviours": 0,
            "behaviour_pcap": 0,
        }
        self.errors: dict[str, Exception] = {}
        self.report: VirusTotalFileReport | None = None
        self.object_reports: dict[tuple[str, str], VirusTotalObjectReport] = {}
        self.relationship_pages: list[VirusTotalPage] = []
        self.search_pages: list[VirusTotalSearchResult] = []
        self.behaviour_pages: list[VirusTotalPage] = []
        self.pcap_bytes = b""
        self.pcap_sha256_override: str | None = None

    def _raise(self, operation: str) -> None:
        error = self.errors.get(operation)
        if error is not None:
            raise error

    async def file_report(self, file_hash: str) -> VirusTotalFileReport:
        self.calls["file_report"] += 1
        self._raise("file_report")
        return self.report or _file_report(file_hash)

    async def file_download(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Sample download is outside this tool lot")

    async def object_report(self, kind: str, object_id: str) -> VirusTotalObjectReport:
        self.calls["object_report"] += 1
        self._raise("object_report")
        return self.object_reports[(kind, object_id)]

    async def file_relationship(self, *args: Any, **kwargs: Any) -> VirusTotalPage:
        raise AssertionError("Relationships must use the structured object operation")

    async def object_relationship(
        self,
        object_type: VirusTotalObjectType,
        object_id: str,
        relation: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage:
        self.calls["object_relationship"] += 1
        self._raise("object_relationship")
        return self.relationship_pages.pop(0) if self.relationship_pages else _page()

    async def intelligence_search(
        self,
        query: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
        descriptors_only: bool = True,
        order: str | None = None,
    ) -> VirusTotalSearchResult:
        self.calls["intelligence_search"] += 1
        self._raise("intelligence_search")
        return self.search_pages.pop(0) if self.search_pages else _search_result()

    async def behaviours(
        self,
        file_hash: str,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        paginate: bool = False,
    ) -> VirusTotalPage:
        self.calls["behaviours"] += 1
        self._raise("behaviours")
        return self.behaviour_pages.pop(0) if self.behaviour_pages else _page()

    async def behaviour_pcap(
        self, sandbox_id: str, *, sink: Any, max_bytes: int | None = None
    ) -> VirusTotalDownloadResult:
        del max_bytes
        self.calls["behaviour_pcap"] += 1
        self._raise("behaviour_pcap")
        sink.write(self.pcap_bytes)
        return VirusTotalDownloadResult(
            md5=hashlib.md5(self.pcap_bytes).hexdigest(),
            sha1=hashlib.sha1(self.pcap_bytes).hexdigest(),
            sha256=self.pcap_sha256_override or hashlib.sha256(self.pcap_bytes).hexdigest(),
            size=len(self.pcap_bytes),
        )


class _FakeEvidenceBlobStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        encoded = canonical_json_bytes(payload)
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id, hashlib.sha256(encoded).hexdigest()

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]


class FakeVirusTotalObservationService:
    def __init__(self) -> None:
        self.raw_blobs: dict[UUID, bytes] = {}
        self.raw_observations: list[VirusTotalObservation] = []
        self.imported_pcaps: list[dict[str, Any]] = []

    def _observation(
        self,
        *,
        operation: VirusTotalOperation,
        capability: VirusTotalCapability,
        source_identifier: str,
        body: bytes,
        http_status: int = 200,
        safe_parameters: dict[str, Any] | None = None,
        input_cursor: str | None = None,
        output_cursor: str | None = None,
        observed_count: int = 1,
        exhaustive: bool = True,
        page_order: int = 0,
    ) -> VirusTotalObservation:
        blob_id = uuid4()
        self.raw_blobs[blob_id] = body
        observation = VirusTotalObservation(
            operation=operation,
            capability=capability,
            source_identifier=source_identifier,
            safe_parameters={
                "transport": "proxy",
                "api_generation": "v3",
                **(safe_parameters or {}),
            },
            http_status=http_status,
            blob_id=blob_id,
            raw_sha256=hashlib.sha256(body).hexdigest(),
            raw_size=len(body),
            observed_at=datetime.now(UTC),
            input_cursor=input_cursor,
            output_cursor=output_cursor,
            observed_count=observed_count,
            exhaustive=exhaustive,
            page_order=page_order,
        )
        self.raw_observations.append(observation)
        return observation

    async def store_file_report(
        self,
        report: VirusTotalFileReport,
        *,
        subject_id: UUID | None = None,
        observed_at: datetime,
        checkpoint_id: str | None = None,
    ) -> VirusTotalObservation:
        return self._observation(
            operation=VirusTotalOperation.FILE_REPORT,
            capability=VirusTotalCapability.FILE_REPORT,
            source_identifier=report.file.lookup_value,
            body=report.raw_json,
            http_status=report.http_status,
        )

    def _page_observations(
        self,
        *,
        operation: VirusTotalOperation,
        capability: VirusTotalCapability,
        source_identifier: str,
        bodies: tuple[bytes, ...],
        statuses: tuple[int, ...],
        parameters: dict[str, Any],
        input_cursor: str | None,
        next_cursor: str | None,
        observed_count: int,
        exhaustive: bool,
    ) -> tuple[VirusTotalObservation, ...]:
        return tuple(
            self._observation(
                operation=operation,
                capability=capability,
                source_identifier=source_identifier,
                body=body,
                http_status=status,
                safe_parameters=parameters,
                input_cursor=input_cursor,
                output_cursor=next_cursor,
                observed_count=observed_count,
                exhaustive=exhaustive,
                page_order=index,
            )
            for index, (body, status) in enumerate(zip(bodies, statuses, strict=True))
        )

    async def store_object_relationship(
        self,
        page: VirusTotalPage,
        *,
        object_type: VirusTotalObjectType,
        object_id: str,
        relation: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return self._page_observations(
            operation=VirusTotalOperation.OBJECT_RELATIONSHIP,
            capability=VirusTotalCapability.FILE_RELATIONSHIPS,
            source_identifier=object_id,
            bodies=page.raw_json_pages,
            statuses=page.http_statuses,
            parameters={
                "object_type": object_type.value,
                "object_id": object_id,
                "relation": relation,
            },
            input_cursor=input_cursor,
            next_cursor=page.next_cursor,
            observed_count=page.observed_count,
            exhaustive=page.exhaustive,
        )

    async def store_intelligence_search(
        self,
        result: VirusTotalSearchResult,
        *,
        query: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
        descriptors_only: bool | None = None,
        order: str | None = None,
    ) -> tuple[VirusTotalObservation, ...]:
        return self._page_observations(
            operation=VirusTotalOperation.INTELLIGENCE_SEARCH,
            capability=VirusTotalCapability.INTELLIGENCE_SEARCH,
            source_identifier=query,
            bodies=result.raw_json_pages,
            statuses=result.http_statuses,
            parameters={"query": query, "transport": "proxy", "api_generation": "v3"},
            input_cursor=input_cursor,
            next_cursor=result.next_cursor,
            observed_count=result.observed_count,
            exhaustive=result.exhaustive,
        )

    async def store_behaviours(
        self,
        page: VirusTotalPage,
        *,
        file_hash: str,
        subject_id: UUID | None = None,
        input_cursor: str | None = None,
        observed_at: datetime,
    ) -> tuple[VirusTotalObservation, ...]:
        return self._page_observations(
            operation=VirusTotalOperation.BEHAVIOURS,
            capability=VirusTotalCapability.BEHAVIOUR_REPORTS,
            source_identifier=file_hash,
            bodies=page.raw_json_pages,
            statuses=page.http_statuses,
            parameters={"file_hash": file_hash},
            input_cursor=input_cursor,
            next_cursor=page.next_cursor,
            observed_count=page.observed_count,
            exhaustive=page.exhaustive,
        )

    async def store_behaviour_pcap(
        self,
        content: bytes,
        *,
        investigation_id: UUID,
        file_hash: str,
        sandbox_id: str,
        expected_sha256: str,
        max_bytes: int,
        observed_at: datetime,
        idempotency_key: str,
    ) -> Any:
        digest = hashlib.sha256(content).hexdigest()
        assert digest == expected_sha256
        assert len(content) <= max_bytes
        self.imported_pcaps.append(
            {
                "investigation_id": investigation_id,
                "file_hash": file_hash,
                "sandbox_id": sandbox_id,
                "sha256": digest,
                "content": content,
                "idempotency_key": idempotency_key,
            }
        )
        return SimpleNamespace(
            capture_ref=NetworkCaptureRefV1(
                blob_id=uuid4(),
                sha256=digest,
                size_bytes=len(content),
                format=NetworkCaptureFormat.PCAP,
            ),
            sample_id=uuid4(),
            association_id=uuid4(),
            evidence_id=uuid4(),
        )


@dataclass
class _ToolSet:
    registry: InMemoryToolRegistry
    dispatcher: RegistryToolDispatcher
    context: ToolExecutionContext
    vt: FakeVirusTotalPort
    vt_observations: FakeVirusTotalObservationService
    evidence: InMemoryEvidenceObservationRepository
    findings: InMemoryFindingRepository
    blob_store: _FakeEvidenceBlobStore


async def _article_snapshot_for_test(_investigation_id: UUID) -> Any:
    return SimpleNamespace(
        confirmed_iocs=(
            SimpleNamespace(indicator_type="ipv4", value="8.8.8.8"),
            SimpleNamespace(indicator_type="domain", value="example.com"),
            SimpleNamespace(indicator_type="url", value="https://example.com/path"),
        )
    )


def _tool_set(
    *,
    capabilities: VirusTotalCapabilities | None = None,
    tlp: TLPLevel | None = TLPLevel.GREEN,
    do_not_submit: bool = False,
    max_pages: int = 3,
) -> _ToolSet:
    selected = capabilities or VirusTotalCapabilities(
        file_report=True,
        file_relationships=True,
        intelligence_search=True,
        behaviour_reports=True,
        behaviour_pcap=True,
    )
    vt = FakeVirusTotalPort()
    vt_observations = FakeVirusTotalObservationService()
    evidence = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    blob_store = _FakeEvidenceBlobStore()
    recorder = ProductionEvidenceRecorder(blob_store, evidence)
    registry = InMemoryToolRegistry()
    register_virustotal_tools(
        registry,
        virustotal=vt,
        observations=vt_observations,
        recorder=recorder,
        evidence_observations=evidence,
        findings=findings,
        article_snapshot_resolver=_article_snapshot_for_test,
        capabilities=selected,
        evidence_payloads=blob_store,
        max_capture_bytes=1024 * 1024,
        max_pages=max_pages,
    )
    investigation_id = uuid4()
    budget = InvestigationBudget(limits={dimension: 10_000 for dimension in BudgetDimension})
    context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=InvestigationEnvelope(
            investigation_id=investigation_id,
            allowed_providers=("vt",),
            budget=budget,
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=tlp,
                do_not_submit=do_not_submit,
                external_llm_allowed=False,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst-test",
        ),
        budget=budget,
        idempotency_key=f"context:{uuid4()}",
        available_capabilities=virustotal_tool_capabilities(selected),
    )
    return _ToolSet(
        registry=registry,
        dispatcher=RegistryToolDispatcher(registry),
        context=context,
        vt=vt,
        vt_observations=vt_observations,
        evidence=evidence,
        findings=findings,
        blob_store=blob_store,
    )


async def _dispatch(
    tools: _ToolSet,
    name: str,
    arguments: dict[str, Any],
    *,
    operation_key: str | None = None,
) -> ToolResult:
    return await tools.dispatcher.dispatch(
        tools.context,
        tool_name=name,
        schema_version="1",
        arguments=arguments,
        purpose="investigation test",
        operation_key=operation_key or f"operation:{name}:{uuid4()}",
    )


def _search_arguments(evidence_ref: UUID, query: str = 'content:"ABCD"') -> dict[str, Any]:
    return {
        "query": query,
        "justification": "Check the marker observed in the seed report.",
        "evidence_refs": [str(evidence_ref)],
    }


def test_tool_schemas_are_2020_12_and_validate_registered_shapes() -> None:
    assert virustotal_tool_capabilities(
        VirusTotalCapabilities(
            file_report=True,
            file_relationships=True,
            intelligence_search=True,
            behaviour_reports=True,
        )
    ) == frozenset(
        {
            "vt.file_report",
            "vt.object_report",
            "vt.relationships",
            "vt.intelligence_search",
            "vt.behaviour",
        }
    )
    schemas = (
        FILE_REPORT_SCHEMA,
        RELATIONSHIPS_SCHEMA,
        INTELLIGENCE_SEARCH_SCHEMA,
        BEHAVIOUR_SCHEMA,
    )
    assert all(
        schema["$schema"] == "https://json-schema.org/draft/2020-12/schema" for schema in schemas
    )
    validate_json_schema({"file_hash": _SEED_HASH}, FILE_REPORT_SCHEMA)
    validate_json_schema(
        {"object_type": "domain", "object_id": "example.test", "relation": "subdomains"},
        RELATIONSHIPS_SCHEMA,
    )
    validate_json_schema(
        _search_arguments(uuid4()),
        INTELLIGENCE_SEARCH_SCHEMA,
    )
    validate_json_schema({"file_hash": _SEED_HASH}, BEHAVIOUR_SCHEMA)
    validate_json_schema(
        {"file_hash": _SEED_HASH, "sandbox_id": "sandbox-report-123"}, BEHAVIOUR_PCAP_SCHEMA
    )


@pytest.mark.asyncio
async def test_dispatcher_rejects_invalid_arguments_and_relation_before_provider_call() -> None:
    tools = _tool_set()
    invalid_hash = await _dispatch(tools, "vt.file_report", {"file_hash": "not-a-hash"})
    assert invalid_hash.status is ToolInvocationStatus.FAILED
    assert tools.vt.calls["file_report"] == 0

    invalid_relation = await _dispatch(
        tools,
        "vt.relationships",
        {"object_type": "domain", "object_id": "example.test", "relation": "submissions"},
    )
    assert invalid_relation.status is ToolInvocationStatus.FAILED
    assert invalid_relation.output["reason"] == "relation_not_allowed"
    assert tools.vt.calls["object_relationship"] == 0


@pytest.mark.asyncio
async def test_intelligence_search_rejects_long_control_short_content_and_unknown_evidence() -> (
    None
):
    tools = _tool_set()
    for query in ("name:" + "x" * 2_001, "name:a\nname:b"):
        result = await _dispatch(tools, "vt.intelligence_search", _search_arguments(uuid4(), query))
        assert result.status is ToolInvocationStatus.FAILED
    short_content = await _dispatch(
        tools, "vt.intelligence_search", _search_arguments(uuid4(), 'content:"abc"')
    )
    assert short_content.status is ToolInvocationStatus.FAILED

    unknown = await _dispatch(
        tools,
        "vt.intelligence_search",
        _search_arguments(uuid4(), 'name:"known marker"'),
    )
    assert unknown.status is ToolInvocationStatus.POLICY_DENIED
    assert unknown.output["reason"] == "evidence_refs_unverified"
    assert tools.vt.calls["intelligence_search"] == 0


@pytest.mark.asyncio
async def test_content_query_justification_and_evidence_refs_persist_on_candidates() -> None:
    tools = _tool_set()
    seed = await ProductionEvidenceRecorder(tools.blob_store, tools.evidence).record_json(
        investigation_id=tools.context.investigation_id,
        kind=EvidenceKind.BINARY_STRING,
        payload={"schema": "sample-obfuscated-strings.v1", "strings": []},
        source="autowork.floss",
        source_version="floss-test-v1",
        object_type="sample",
        object_id=_SEED_HASH,
    )
    tools.vt.search_pages = [_search_result(({"id": _CANDIDATE_HASH, "type": "file"},))]
    query = "content:{0102030405060708090a0b0c}"

    result = await _dispatch(
        tools,
        "vt.intelligence_search",
        _search_arguments(seed.id, query),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    persisted = await tools.evidence.list_for_investigation(tools.context.investigation_id)
    summaries = []
    for observation in persisted:
        if (
            observation.source != "VirusTotal"
            or observation.kind is not EvidenceKind.VT_RELATIONSHIP
        ):
            continue
        if observation.blob_id not in tools.blob_store.payloads:
            continue
        payload = await tools.blob_store.read_json(observation.blob_id)
        if isinstance(payload.get("candidate"), dict):
            summaries.append(payload)
    assert len(summaries) == 1
    assert summaries[0]["effective_query"] == query
    assert summaries[0]["justification"] == _search_arguments(seed.id, query)["justification"]
    assert summaries[0]["evidence_refs"] == [str(seed.id)]


def test_content_search_validation_accepts_measured_hex_and_spaces() -> None:
    assert validate_search_query("content:{01020304}") == "content:{01020304}"
    assert validate_search_query("content:{01 02 03 04}") == "content:{01 02 03 04}"
    assert validate_search_query('content:"ABCD"') == 'content:"ABCD"'


@pytest.mark.parametrize(
    "query",
    (
        "content:{01 02 03}",
        "content:{010203}",
        "content:{01 02 0g 04}",
        "content:{0102030}",
        "content:{01 0203 04}",
    ),
)
def test_content_search_validation_rejects_short_or_invalid_hex(query: str) -> None:
    with pytest.raises(VirusTotalInvalidInputError):
        validate_search_query(query)


def test_content_search_validation_enforces_the_2000_character_limit() -> None:
    inner = "00 " * 662 + "00  "
    query = f"content:{{{inner}}}"
    assert len(query) == 2_000
    assert validate_search_query(query) == query
    with pytest.raises(VirusTotalInvalidInputError):
        validate_search_query(query + " ")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tlp", "do_not_submit", "expected_status", "expected_calls"),
    [
        (TLPLevel.RED, False, ToolInvocationStatus.POLICY_DENIED, 0),
        (TLPLevel.AMBER, False, ToolInvocationStatus.POLICY_DENIED, 0),
        (TLPLevel.GREEN, True, ToolInvocationStatus.SUCCEEDED, 1),
    ],
)
async def test_disclosure_bounds_allow_do_not_submit_but_deny_tlp(
    tlp: TLPLevel,
    do_not_submit: bool,
    expected_status: ToolInvocationStatus,
    expected_calls: int,
) -> None:
    tools = _tool_set(tlp=tlp, do_not_submit=do_not_submit)
    result = await _dispatch(tools, "vt.file_report", {"file_hash": _SEED_HASH})
    assert result.status is expected_status
    assert tools.vt.calls["file_report"] == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "reason_code", "expected_status"),
    (
        ("PRE_CONSUME", "source_references_mismatch", ToolInvocationStatus.POLICY_DENIED),
        ("PREPARATION", "invalid_prepared_effect", ToolInvocationStatus.FAILED),
    ),
)
async def test_framework_presend_refusal_is_terminal_and_carries_no_send_proof(
    stage: str,
    reason_code: str,
    expected_status: ToolInvocationStatus,
) -> None:
    tools = _tool_set()
    tools.vt.errors["file_report"] = VirusTotalExternalEffectRefusedError(
        "The coordinator refused before consume.",
        code=f"virustotal_external_effect_refused_{reason_code}",
        framework_no_send_proof=FrameworkNoSendProof(stage, reason_code),
    )

    result = await _dispatch(tools, "vt.file_report", {"file_hash": _SEED_HASH})

    assert result.status is expected_status
    assert result.framework_pre_submission_proof is (
        FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
    )
    assert result.error_code == f"virustotal_external_effect_refused_{reason_code}"
    assert result.output["reason"] == "external_effect_refused"
    assert tools.vt.calls["file_report"] == 1


@pytest.mark.asyncio
async def test_presend_refusal_after_prior_send_does_not_attest_whole_tool_nonexecution() -> None:
    tools = _tool_set()
    refusal = VirusTotalExternalEffectRefusedError(
        "The coordinator refused before consume.",
        framework_no_send_proof=FrameworkNoSendProof("PRE_CONSUME", "source_references_mismatch"),
    )

    async def send_then_refuse(_file_hash: str) -> VirusTotalFileReport:
        record_virustotal_external_effect(reused=False)
        raise refusal

    tools.vt.file_report = send_then_refuse
    result = await _dispatch(tools, "vt.file_report", {"file_hash": _SEED_HASH})

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.framework_pre_submission_proof is None


@pytest.mark.asyncio
async def test_quota_authorization_and_transport_errors_map_to_local_tool_statuses() -> None:
    quota_tools = _tool_set()
    quota_tools.vt.errors["file_report"] = VirusTotalQuotaExceededError(
        "quota", status_code=429, retry_after=13, error_code="QuotaExceeded"
    )
    quota = await _dispatch(quota_tools, "vt.file_report", {"file_hash": _SEED_HASH})
    assert quota.status is ToolInvocationStatus.UNAVAILABLE
    assert quota.output["reason"] == "quota"
    assert quota.output["retry_after"] == 13

    unauthorized_tools = _tool_set()
    unauthorized_tools.vt.errors["file_report"] = VirusTotalNotAuthorizedError(
        "not authorized", status_code=403, error_code="NotAuthorized"
    )
    unauthorized = await _dispatch(unauthorized_tools, "vt.file_report", {"file_hash": _SEED_HASH})
    assert unauthorized.status is ToolInvocationStatus.UNAVAILABLE
    assert unauthorized.output["reason"] == "provider_authorization_refused"

    unauthorized_401_tools = _tool_set()
    unauthorized_401_tools.vt.errors["file_report"] = VirusTotalHttpError(
        "synthetic unauthorized response", status_code=401
    )
    unauthorized_401 = await _dispatch(
        unauthorized_401_tools, "vt.file_report", {"file_hash": _SEED_HASH}
    )
    assert unauthorized_401.status is ToolInvocationStatus.UNAVAILABLE
    assert unauthorized_401.output["reason"] == "provider_authorization_refused"

    transport_tools = _tool_set()
    transport_tools.vt.errors["file_report"] = VirusTotalTransportError("offline")
    transport = await _dispatch(transport_tools, "vt.file_report", {"file_hash": _SEED_HASH})
    assert transport.status is ToolInvocationStatus.FAILED
    assert transport.suggested_alternatives


@pytest.mark.asyncio
async def test_explicit_not_found_and_successful_empty_results_are_negative_evidence() -> None:
    not_found_tools = _tool_set()
    not_found_tools.vt.errors["file_report"] = VirusTotalHttpError(
        "not found", status_code=404, error_code="NotFound"
    )
    not_found = await _dispatch(not_found_tools, "vt.file_report", {"file_hash": _SEED_HASH})
    assert not_found.status is ToolInvocationStatus.EMPTY
    assert any(
        item.kind is EvidenceKind.NEGATIVE_RESULT
        for item in await not_found_tools.evidence.list_for_investigation(
            not_found_tools.context.investigation_id
        )
    )


@pytest.mark.asyncio
async def test_object_report_is_available_for_verified_investigation_iocs() -> None:
    tools = _tool_set()
    raw = json.dumps(
        {
            "data": {
                "id": "8.8.8.8",
                "type": "ip_address",
                "attributes": {
                    "reputation": 12,
                    "last_analysis_stats": {"malicious": 1, "harmless": 9},
                    "signed_url": "https://example.test/download?token=secret",
                },
            }
        }
    ).encode()
    tools.vt.object_reports[("ip_addresses", "8.8.8.8")] = VirusTotalObjectReport(
        kind="ip_addresses",
        object_id="8.8.8.8",
        raw_json=raw,
        http_status=200,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )

    result = await _dispatch(
        tools,
        "vt.object_report",
        {"object_type": "ip_addresses", "object_id": "8.8.8.8"},
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert tools.vt.calls["object_report"] == 1
    assert result.output["object_type"] == "ip_address"
    assert result.output["object_id"] == "8.8.8.8"
    assert result.output["reputation"] == 12
    assert "signed_url" not in result.output
    observations = await tools.evidence.list_for_investigation(tools.context.investigation_id)
    assert len(observations) == 2
    assert {item.object_id for item in observations} == {"8.8.8.8"}


@pytest.mark.asyncio
async def test_object_report_projection_retry_reserves_and_consumes_each_physical_send() -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "relationships" in request.url.params:
            return httpx.Response(403, json={"error": {"code": "Forbidden"}}, request=request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "8.8.8.8",
                    "type": "ip_address",
                    "attributes": {"reputation": 1, "last_analysis_stats": {}},
                }
            },
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        headers={"x-apikey": "synthetic-test-key"},
        trust_env=False,
    )
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url="https://www.virustotal.com/api/v3",
        capabilities=VirusTotalCapabilities(file_report=True),
        routing_policy=VirusTotalRoutingPolicy(
            routes={
                VirusTotalCapability.FILE_REPORT: VirusTotalOperationRoute(
                    primary=VirusTotalRouteStep(
                        transport=VirusTotalTransportKind.PROXY,
                        variant=VirusTotalEndpointVariant.V3,
                    )
                )
            }
        ),
    )
    claim_store, _archive = configure_test_vt_adapter(adapter)
    tools = _tool_set(capabilities=VirusTotalCapabilities(file_report=True))
    registry = InMemoryToolRegistry()
    register_virustotal_tools(
        registry,
        virustotal=adapter,
        observations=tools.vt_observations,
        recorder=ProductionEvidenceRecorder(tools.blob_store, tools.evidence),
        evidence_observations=tools.evidence,
        findings=tools.findings,
        article_snapshot_resolver=_article_snapshot_for_test,
        capabilities=VirusTotalCapabilities(file_report=True),
        evidence_payloads=tools.blob_store,
    )
    budget = InvestigationBudget(
        limits={
            dimension: 3 if dimension is BudgetDimension.VT_UNITS else 1_000
            for dimension in BudgetDimension
        }
    )
    assert tools.context.envelope is not None
    context = replace(
        tools.context,
        envelope=replace(tools.context.envelope, budget=budget),
        budget=budget,
        external_effect_authority=make_test_authority(),
    )
    try:
        result = await RegistryToolDispatcher(registry).dispatch(
            context,
            tool_name="vt.object_report",
            schema_version="1",
            arguments={"object_type": "ip_addresses", "object_id": "8.8.8.8"},
            purpose="Count object projection fallback sends.",
            operation_key="vt-object-report-projection-retry",
        )
    finally:
        await client.aclose()

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(requests) == 2
    assert requests[0].url.params
    assert "relationships" not in requests[1].url.params
    assert len(claim_store.claims) == 2
    assert result.budget_consumed[BudgetDimension.VT_UNITS] == 2
    reservation = budget.reservations[0]
    assert reservation.amounts[BudgetDimension.VT_UNITS] == 3
    assert reservation.committed_amounts[BudgetDimension.VT_UNITS] == 2
    assert reservation.status is BudgetReservationStatus.COMMITTED


@pytest.mark.asyncio
async def test_object_report_rejects_an_ioc_not_in_the_investigation() -> None:
    tools = _tool_set()

    result = await _dispatch(
        tools,
        "vt.object_report",
        {"object_type": "domains", "object_id": "attacker.example"},
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["reason"] == "ioc_not_in_investigation"
    assert tools.vt.calls["object_report"] == 0


@pytest.mark.asyncio
async def test_url_object_report_uses_the_investigation_ioc_and_hides_the_url() -> None:
    tools = _tool_set()
    url = "https://example.com/path"
    encoded_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    raw = json.dumps(
        {
            "data": {
                "id": encoded_id,
                "type": "url",
                "attributes": {"reputation": -2},
            }
        }
    ).encode()
    tools.vt.object_reports[("urls", url)] = VirusTotalObjectReport(
        kind="urls",
        object_id=encoded_id,
        raw_json=raw,
        http_status=200,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )

    result = await _dispatch(tools, "vt.object_report", {"object_type": "urls", "object_id": url})

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["object_id"].startswith("url-sha256:")
    assert url not in repr(result.output)


def test_behaviour_pcap_is_not_registered_without_the_runtime_flag() -> None:
    tools = _tool_set(capabilities=VirusTotalCapabilities(file_report=True, behaviour_reports=True))

    assert tools.registry.get("vt.behaviour_pcap", "1") is None
    assert "vt.behaviour_pcap" not in tools.context.available_capabilities


@pytest.mark.asyncio
async def test_behaviour_pcap_transport_error_is_specific_and_confirmed_non_execution() -> None:
    tools = _tool_set()
    tools.vt.errors["behaviour_pcap"] = VirusTotalTransportError(
        "offline", code="virustotal_download_timeout"
    )
    registered = tools.registry.get("vt.behaviour_pcap", "1")
    assert registered is not None
    _definition, adapter = registered

    async def saved_report_contains_capture(_investigation_id, _file_hash, _sandbox_id):
        return True

    adapter._saved_report_contains_capture = saved_report_contains_capture
    result = await adapter.execute({"file_hash": _SEED_HASH, "sandbox_id": "Zenbox"}, tools.context)

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["reason"] == "pcap_transport_timeout"
    assert result.output["confirmed_non_execution"] is True


@pytest.mark.asyncio
async def test_behaviour_pcap_preserves_ambiguous_effect_as_needs_review() -> None:
    tools = _tool_set()
    tools.vt.errors["behaviour_pcap"] = VirusTotalExternalEffectNeedsReviewError(
        "The completed request could not be verified."
    )
    registered = tools.registry.get("vt.behaviour_pcap", "1")
    assert registered is not None
    _definition, adapter = registered

    async def saved_report_contains_capture(_investigation_id, _file_hash, _sandbox_id):
        return True

    adapter._saved_report_contains_capture = saved_report_contains_capture
    result = await adapter.execute({"file_hash": _SEED_HASH, "sandbox_id": "Zenbox"}, tools.context)

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.output["reason"] == "external_effect_needs_review"
    assert result.output.get("confirmed_non_execution") is not True


def _minimal_pcap() -> bytes:
    frame = b"\x00" * 14
    global_header = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
    packet_header = struct.pack("<IIII", 1, 0, len(frame), len(frame))
    return global_header + packet_header + frame


@pytest.mark.asyncio
async def test_behaviour_pcap_requires_saved_report_then_imports_quarantined_capture() -> None:
    tools = _tool_set()
    tools.vt.behaviour_pages.append(
        _page(
            (
                {
                    "id": "sandbox-report-123",
                    "type": "file_behaviour",
                    "attributes": {"has_pcap": True, "sandbox_name": "sandbox-a"},
                },
            )
        )
    )
    behavior = await _dispatch(tools, "vt.behaviour", {"file_hash": _SEED_HASH})
    assert behavior.status is ToolInvocationStatus.SUCCEEDED
    tools.vt.pcap_bytes = _minimal_pcap()

    result = await _dispatch(
        tools,
        "vt.behaviour_pcap",
        {"file_hash": _SEED_HASH, "sandbox_id": "sandbox-report-123"},
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert tools.vt.calls["behaviour_pcap"] == 1
    assert len(tools.vt_observations.imported_pcaps) == 1
    imported = tools.vt_observations.imported_pcaps[0]
    assert imported["file_hash"] == _SEED_HASH
    assert imported["sandbox_id"] == "sandbox-report-123"
    assert result.output["capture_ref"]["sha256"] == imported["sha256"]
    assert result.output["active_c2_observed"] is False
    assert any("pas une observation d'un C2 actif" in item for item in result.output["limitations"])


@pytest.mark.asyncio
async def test_behaviour_pcap_does_not_fetch_an_unpersisted_sandbox_id() -> None:
    tools = _tool_set()

    result = await _dispatch(
        tools,
        "vt.behaviour_pcap",
        {"file_hash": _SEED_HASH, "sandbox_id": "invented-sandbox"},
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["reason"] == "behaviour_report_not_persisted"
    assert tools.vt.calls["behaviour_pcap"] == 0


@pytest.mark.asyncio
async def test_behaviour_pcap_rejects_a_port_hash_mismatch_before_quarantine() -> None:
    tools = _tool_set()
    tools.vt.behaviour_pages.append(
        _page(
            (
                {
                    "id": "sandbox-report-123",
                    "type": "file_behaviour",
                    "attributes": {"has_pcap": True},
                },
            )
        )
    )
    await _dispatch(tools, "vt.behaviour", {"file_hash": _SEED_HASH})
    tools.vt.pcap_bytes = _minimal_pcap()
    tools.vt.pcap_sha256_override = "f" * 64

    result = await _dispatch(
        tools,
        "vt.behaviour_pcap",
        {"file_hash": _SEED_HASH, "sandbox_id": "sandbox-report-123"},
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["reason"] == "pcap_invalid_response"
    assert result.output["confirmed_non_execution"] is True
    assert tools.vt_observations.imported_pcaps == []

    empty_tools = _tool_set()
    empty_tools.vt.relationship_pages = [_page()]
    empty = await _dispatch(
        empty_tools,
        "vt.relationships",
        {"object_type": "file", "object_id": _SEED_HASH, "relation": "dropped_files"},
    )
    assert empty.status is ToolInvocationStatus.EMPTY
    assert empty.suggested_alternatives
    evidence = await empty_tools.evidence.list_for_investigation(
        empty_tools.context.investigation_id
    )
    assert any(item.kind is EvidenceKind.NEGATIVE_RESULT for item in evidence)
    assert any(item.kind is EvidenceKind.VT_RELATIONSHIP for item in evidence)


@pytest.mark.asyncio
async def test_page_ceiling_returns_partial_with_cursor_and_provider_route() -> None:
    tools = _tool_set(max_pages=2)
    tools.vt.relationship_pages = [
        _page(({"id": _CANDIDATE_HASH, "type": "file"},), next_cursor="cursor-1", exhaustive=False),
        _page(({"id": "c" * 64, "type": "file"},), next_cursor="cursor-2", exhaustive=False),
    ]
    result = await _dispatch(
        tools,
        "vt.relationships",
        {"object_type": "file", "object_id": _SEED_HASH, "relation": "dropped_files"},
    )
    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["pages"] == 2
    assert result.output["next_cursor"] == "cursor-2"
    assert result.output["provider_routes"] == ("proxy:v3",)
    assert result.budget_consumed[BudgetDimension.VT_UNITS] == 2


@pytest.mark.asyncio
async def test_relationship_page_archive_is_untyped_and_summary_remains_typed() -> None:
    body = json.dumps(
        {
            "data": [{"id": _CANDIDATE_HASH, "type": "file"}],
            "padding": "x" * (128 * 1024),
        },
        separators=(",", ":"),
    ).encode()
    page = replace(
        _page(({"id": _CANDIDATE_HASH, "type": "file"},)),
        raw_json_pages=(body,),
    )
    tools = _tool_set()
    tools.vt.relationship_pages = [page]

    result = await _dispatch(
        tools,
        "vt.relationships",
        {"object_type": "file", "object_id": _SEED_HASH, "relation": "dropped_files"},
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    evidence = await tools.evidence.list_for_investigation(tools.context.investigation_id)
    raw_evidence = [item for item in evidence if item.sha256 == hashlib.sha256(body).hexdigest()]
    summaries = [item for item in evidence if item.kind is EvidenceKind.VT_RELATIONSHIP]
    assert len(raw_evidence) == 1
    assert raw_evidence[0].kind is EvidenceKind.OTHER
    assert len(summaries) == 1
    assert summaries[0].sha256 != raw_evidence[0].sha256


@pytest.mark.asyncio
async def test_dispatcher_replay_does_not_call_virustotal_twice() -> None:
    tools = _tool_set()
    key = "operation:file-report:stable"
    first = await _dispatch(tools, "vt.file_report", {"file_hash": _SEED_HASH}, operation_key=key)
    replay = await _dispatch(tools, "vt.file_report", {"file_hash": _SEED_HASH}, operation_key=key)
    assert first is replay
    assert tools.vt.calls["file_report"] == 1


@pytest.mark.asyncio
async def test_raw_archive_is_typed_and_summaries_exclude_secrets_and_signed_urls() -> None:
    tools = _tool_set()
    tools.vt.report = _file_report()
    result = await _dispatch(tools, "vt.file_report", {"file_hash": _SEED_HASH})
    evidence = await tools.evidence.list_for_investigation(tools.context.investigation_id)
    raw = next(item for item in evidence if item.kind is EvidenceKind.VT_FILE_REPORT)
    assert raw.blob_id == tools.vt_observations.raw_observations[0].blob_id
    assert result.output["last_analysis_date"] == 1_781_300_000
    assert result.output["provider_routes"] == ("proxy:v3",)
    rendered = repr(result.output) + repr(evidence) + repr(tools.blob_store.payloads)
    assert "supersecret" not in rendered
    assert "signed-value" not in rendered
    assert "https://example.test" not in rendered
    assert result.observation_refs


class _InvestigationRepository:
    def __init__(self) -> None:
        self.items: dict[UUID, AnalystInvestigation] = {}

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        return self.items.get(investigation_id)

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id), None
        )

    async def add(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation

    async def save(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation


class _Handoff:
    def __init__(self, repository: _InvestigationRepository) -> None:
        self.repository = repository

    async def ensure_for_verified_synthesis(
        self, *, run: Any, synthesis: ProductionArtifact, **kwargs: Any
    ) -> Any:
        investigation = await self.repository.get_for_run(run.id)
        if investigation is None:
            investigation = AnalystInvestigation.from_verified_synthesis(
                synthesis=synthesis,
                budget=LoopBudget(
                    max_cycles=12,
                    max_pivot_runs=20,
                    max_hits_acquired=10,
                    max_new_samples=10,
                    max_vt_read_units=50,
                ),
            )
            await self.repository.add(investigation)
        return SimpleNamespace(investigation_id=investigation.id)


class _ArticlePort:
    def __init__(self, snapshot: ArticleSnapshot) -> None:
        self.snapshot = snapshot

    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot:
        assert synthesis_artifact_id == self.snapshot.synthesis_artifact_id
        return self.snapshot

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[Any, ...]:
        if not claim_ids:
            return self.snapshot.article.claims
        return tuple(item for item in self.snapshot.article.claims if item.id in set(claim_ids))


def _proposal_payload(
    *,
    kind: FindingKind,
    assessment: FindingAssessment,
    statement: str,
    observation_refs: tuple[UUID, ...],
    novelty: str | None,
    scope: str,
    limitations: tuple[str, ...],
    subject_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind.value,
        "assessment": assessment.value,
        "statement": statement,
        "subject_ref": subject_ref,
        "observation_refs": [str(item) for item in observation_refs],
        "claim_refs": [],
        "novelty": novelty,
        "scope": scope,
        "limitations": list(limitations),
    }


def _finding_id(investigation_id: UUID, proposal: dict[str, Any]) -> UUID:
    digest = hashlib.sha256(canonical_json_bytes(proposal)).hexdigest()
    return uuid5(_FINDING_NAMESPACE, f"{investigation_id}:{digest}")


class _EvidenceAwareScriptedTransport(ScriptedTurnTransport):
    """Use real scripted Bridge turns and derive citations after tool evidence exists."""

    def __init__(
        self,
        *,
        evidence: InMemoryEvidenceObservationRepository,
        investigations: _InvestigationRepository,
        novelty: ArticleNoveltyService,
        search_enabled: bool,
    ) -> None:
        first_turn = {
            "reasoning_summary": "Read the documented seed report first.",
            "goal": "Inspect the article seed in VirusTotal.",
            "hypothesis_refs": [],
            "proposed_tool_calls": [
                {
                    "tool_name": "vt.file_report",
                    "schema_version": "1",
                    "arguments": {"file_hash": _SEED_HASH},
                    "purpose": "Check the documented seed report.",
                }
            ],
            "finding_proposals": [],
            "opinion_delta": None,
            "next_action": "continue",
        }
        super().__init__([first_turn])
        self.evidence = evidence
        self.investigations = investigations
        self.novelty_service = novelty
        self.search_enabled = search_enabled
        self.classification: Any | None = None

    async def submit(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        turn_index: int,
        context_sha256: str,
        prompt: str,
        external_llm_allowed: bool,
        timeout_seconds: float | None = None,
    ) -> Any:
        current = await self.evidence.list_for_investigation(investigation_id)
        if turn_index == 1:
            seed_ref = next(item.id for item in current if item.kind is EvidenceKind.VT_FILE_REPORT)
            self._responses.append(
                {
                    "reasoning_summary": (
                        "Use the archived seed observation to justify a native VT dork."
                    ),
                    "goal": "Search for a related candidate.",
                    "hypothesis_refs": [],
                    "proposed_tool_calls": [
                        {
                            "tool_name": "vt.intelligence_search",
                            "schema_version": "1",
                            "arguments": {
                                "query": 'content:"ABCD"',
                                "justification": "Check the marker observed in the seed report.",
                                "evidence_refs": [str(seed_ref)],
                            },
                            "purpose": "Find an indexed candidate carrying the seed marker.",
                        }
                    ],
                    "finding_proposals": [],
                    "opinion_delta": None,
                    "next_action": "continue",
                }
            )
        elif turn_index == 2:
            candidate_refs = tuple(
                item.id
                for item in current
                if item.kind is EvidenceKind.VT_RELATIONSHIP
                and item.object_type == "file"
                and item.object_id == _CANDIDATE_HASH
            )
            if self.search_enabled and candidate_refs:
                investigation = await self.investigations.get(investigation_id)
                assert investigation is not None
                self.classification = await self.novelty_service.classify(
                    investigation_id=investigation_id,
                    candidate=NoveltyCandidate(
                        investigation_id=investigation_id,
                        kind=NoveltyCandidateKind.SAMPLE,
                        value=_CANDIDATE_HASH,
                        object_type="file",
                        object_id=_CANDIDATE_HASH,
                        observation_refs=candidate_refs,
                    ),
                )
                evidence_refs = candidate_refs[:1]
                finding_kind = FindingKind.NEW_SAMPLE
                subject_ref = _CANDIDATE_HASH
                statement = f"VirusTotal Intelligence Search returned candidate {_CANDIDATE_HASH}."
                novelty = self.classification.novelty.value
                scope = self.classification.scope
                limitations = (
                    "Non documenté dans cet article.",
                    *self.classification.limitations,
                )
            else:
                report_ref = next(
                    item.id for item in current if item.kind is EvidenceKind.VT_FILE_REPORT
                )
                evidence_refs = (report_ref,)
                finding_kind = FindingKind.CAPABILITY
                subject_ref = _SEED_HASH
                statement = "The documented seed has a persisted VirusTotal file report."
                novelty = None
                scope = "The reported seed only."
                limitations = ("This finding is based on the file report alone.",)
            proposal = _proposal_payload(
                kind=finding_kind,
                assessment=FindingAssessment.SUPPORTED,
                statement=statement,
                observation_refs=evidence_refs,
                novelty=novelty,
                scope=scope,
                limitations=limitations,
                subject_ref=subject_ref,
            )
            finding_id = _finding_id(investigation_id, proposal)
            self._responses.append(
                {
                    "reasoning_summary": (
                        "Cite the persisted VT evidence and draft the analyst opinion."
                    ),
                    "goal": "Explain the result with its article-relative scope.",
                    "hypothesis_refs": [],
                    "proposed_tool_calls": [],
                    "finding_proposals": [proposal],
                    "opinion_delta": {
                        "blocks": [
                            {
                                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                                "text": statement,
                                "finding_refs": [str(finding_id)],
                                "evidence_refs": [str(evidence_refs[0])],
                                "qualification": OpinionClaimQualification.FACT.value,
                                "certainty": 0.9,
                            }
                        ],
                        "unresolved_points": [],
                    },
                    "next_action": "finish",
                }
            )
        return await super().submit(
            investigation_id=investigation_id,
            subject_id=subject_id,
            turn_index=turn_index,
            context_sha256=context_sha256,
            prompt=prompt,
            external_llm_allowed=external_llm_allowed,
            timeout_seconds=timeout_seconds,
        )


@dataclass
class _VerticalHarness:
    service: InvestigationService
    transport: _EvidenceAwareScriptedTransport
    investigation_id: UUID
    investigations: _InvestigationRepository
    snapshot: ArticleSnapshot
    evidence: InMemoryEvidenceObservationRepository
    findings: InMemoryFindingRepository
    opinions: InMemoryAnalystOpinionRepository
    invocations: InMemoryToolInvocationRepository
    vt: FakeVirusTotalPort


class _VerticalSampleAssociationRepository:
    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=sample_hash,
                origin=SampleOrigin.MANUAL,
            )
            for sample_hash in (_SEED_HASH, _CANDIDATE_HASH)
        )


class _VerticalSampleRepository:
    async def list_by_subject_and_sha256(
        self, _subject_id: UUID, _sha256: str
    ) -> tuple[Sample, ...]:
        return ()


def _vertical_harness(*, search_enabled: bool) -> _VerticalHarness:
    run_id = uuid4()
    subject_id = uuid4()
    synthesis = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="1" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    publication_id = uuid4()
    snapshot = ArticleSnapshot(
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis.id,
        synthesis_sha256="2" * 64,
        article=ArticleContextV1(
            short_summary="Article documenting one seed file.",
            claims=(
                ArticleClaimV1(
                    id="C1",
                    statement="The article describes the seed.",
                    source_refs=(),
                ),
            ),
            documented_samples=(_SEED_HASH,),
            known_chain=(),
            open_questions=("Is a related candidate missing from the article?",),
        ),
        base_publication_artifact_id=publication_id,
        base_publication_sha256="3" * 64,
        tlp="GREEN",
        do_not_submit=False,
        external_llm_allowed=True,
    )
    article_port = _ArticlePort(snapshot)
    investigations = _InvestigationRepository()
    handoff = _Handoff(investigations)
    evidence = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    opinions = InMemoryAnalystOpinionRepository()
    registry = InMemoryToolRegistry()
    vt = FakeVirusTotalPort()
    vt.report = _file_report(_SEED_HASH)
    vt.search_pages = [
        _search_result(
            (
                {
                    "id": _CANDIDATE_HASH,
                    "type": "file",
                    "attributes": {
                        "meaningful_name": "related-payload.bin",
                        "last_analysis_stats": {"malicious": 8, "undetected": 40},
                    },
                },
            )
        )
    ]
    archive = FakeVirusTotalObservationService()
    blob_store = _FakeEvidenceBlobStore()
    recorder = ProductionEvidenceRecorder(blob_store, evidence)
    caps = VirusTotalCapabilities(file_report=True, intelligence_search=search_enabled)
    register_virustotal_tools(
        registry,
        virustotal=vt,
        observations=archive,
        recorder=recorder,
        evidence_observations=evidence,
        findings=findings,
    )
    article_novelty = ArticleNoveltyService(
        observations=evidence,
        investigations=investigations,
        article_snapshots=article_port,
    )
    transport = _EvidenceAwareScriptedTransport(
        evidence=evidence,
        investigations=investigations,
        novelty=article_novelty,
        search_enabled=search_enabled,
    )
    context_builder = ResearchContextBuilder(
        article_snapshots=article_port,
        observations=evidence,
        findings=findings,
    )
    invocations = InMemoryToolInvocationRepository()
    service = InvestigationService(
        post_synthesis=handoff,
        investigations=investigations,
        states=InMemoryInvestigationStateRepository(),
        context_snapshots=InMemoryInvestigationContextSnapshotRepository(),
        envelopes=InMemoryInvestigationEnvelopeRepository(),
        context_builder=context_builder,
        bridge=BridgeInvestigator(transport),
        registry=registry,
        dispatcher=RegistryToolDispatcher(registry),
        invocations=invocations,
        observations=evidence,
        findings=findings,
        opinions=opinions,
        sample_associations=_VerticalSampleAssociationRepository(),
        samples=_VerticalSampleRepository(),
        available_capabilities=virustotal_tool_capabilities(caps),
        no_progress_limit=3,
    )
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(
            max_cycles=12,
            max_pivot_runs=20,
            max_hits_acquired=10,
            max_new_samples=10,
            max_vt_read_units=50,
        ),
    )
    investigation_id = investigation.id
    investigations.items[investigation_id] = investigation
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 30,
                BudgetDimension.TOOL_SECONDS: 1_000,
            }
        ),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-test",
    )
    service._vertical_values = (  # type: ignore[attr-defined]
        SimpleNamespace(id=run_id, subject_id=subject_id),
        synthesis,
        envelope,
        snapshot,
    )
    return _VerticalHarness(
        service=service,
        transport=transport,
        investigation_id=investigation_id,
        investigations=investigations,
        snapshot=snapshot,
        evidence=evidence,
        findings=findings,
        opinions=opinions,
        invocations=invocations,
        vt=vt,
    )


async def _run_vertical(harness: _VerticalHarness) -> None:
    run, synthesis, envelope, _snapshot = harness.service._vertical_values  # type: ignore[attr-defined]
    await harness.service.start(
        run=run,
        synthesis=synthesis,
        extraction_artifacts=(),
        extraction_items=(),
        policy=AnalystHandoffPolicy(tlp="GREEN", do_not_submit=False, external_llm_allowed=True),
        envelope=envelope,
    )
    await harness.service.run(harness.investigation_id)


@pytest.mark.asyncio
async def test_vertical_file_report_search_article_novelty_finding_and_opinion() -> None:
    harness = _vertical_harness(search_enabled=True)
    await _run_vertical(harness)
    assert harness.transport.submission_count == 3
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.calls["intelligence_search"] == 1
    assert harness.transport.classification is not None
    assert harness.transport.classification.novelty.value == "new_to_article"
    finding = next(iter(await harness.findings.list_for_investigation(harness.investigation_id)))
    assert finding.novelty is not None and finding.novelty.value == "new_to_article"
    assert "Non documenté dans cet article." in finding.limitations
    assert "pas une absence mondiale" in " ".join(finding.limitations)
    assert any(
        "@@OPINION@@" in (item.output_text or "") for item in harness.transport._by_key.values()
    )
    opinion = await harness.opinions.get(uuid5(_OPINION_NAMESPACE, str(harness.investigation_id)))
    assert opinion is not None
    assert opinion.blocks[0].finding_refs == (finding.id,)
    assert opinion.blocks[0].evidence_refs[0] in finding.observation_refs


@pytest.mark.asyncio
async def test_vertical_opinion_is_still_drafted_when_intelligence_search_is_unavailable() -> None:
    harness = _vertical_harness(search_enabled=False)
    await _run_vertical(harness)
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.calls["intelligence_search"] == 0
    operation_key = tool_operation_key(
        investigation_id=harness.investigation_id,
        tool_name="vt.intelligence_search",
        schema_version="1",
        normalized_parameters={
            "query": 'content:"ABCD"',
            "justification": "Check the marker observed in the seed report.",
            "evidence_refs": [
                str(
                    next(
                        item.id
                        for item in await harness.evidence.list_for_investigation(
                            harness.investigation_id
                        )
                        if item.kind is EvidenceKind.VT_FILE_REPORT
                    )
                )
            ],
        },
        purpose="Find an indexed candidate carrying the seed marker.",
    )
    unavailable_invocation = await harness.invocations.get_by_operation_key(operation_key)
    assert unavailable_invocation is not None
    assert unavailable_invocation.status is ToolInvocationStatus.UNAVAILABLE
    finding = next(iter(await harness.findings.list_for_investigation(harness.investigation_id)))
    assert finding.observation_refs
    evidence_items = await harness.evidence.list_for_investigation(harness.investigation_id)
    evidence_ids = {item.id for item in evidence_items}
    assert all(ref in evidence_ids for ref in finding.observation_refs)
    opinion = await harness.opinions.get(uuid5(_OPINION_NAMESPACE, str(harness.investigation_id)))
    assert opinion is not None
    assert opinion.blocks[0].finding_refs == (finding.id,)
    assert "file report" in opinion.blocks[0].text.lower()
