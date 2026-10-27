from __future__ import annotations

import hashlib
import json
import struct
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import (
    ScriptedTurnTransport,
)
from cti_app.application.investigation_tools import RegistryToolDispatcher
from cti_app.application.osint_open_policy import OsintOpenPolicyService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SampleState, SourceDocument
from cti_app.domain.entities import SampleOrigin as EntitySampleOrigin
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EnvelopeAuthorityKind,
    EvidenceKind,
    EvidenceObservation,
    SampleAssociation,
    SampleOrigin,
    TLPLevel,
    ToolInvocationStatus,
)
from cti_app.domain.osint_open import OsintCapabilities, OsintScopeKind
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.core import BlobRow, SampleRow
from cti_app.infrastructure.database.models.external_effects import (
    ExternalEffectAcquisitionDispatchRow,
    ExternalEffectClaimRow,
)
from cti_app.infrastructure.database.models.investigation import (
    SampleAssociationRow,
    SampleAssociationVersionRow,
)
from cti_app.infrastructure.database.models.production import SampleAcquisitionDispatchAttemptRow
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.investigation_runtime_ports import build_investigation_runtime_ports
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter
from tests.workflow_emulator.investigation_support import (
    build_investigation_app,
    composition_factory,
    decision,
    investigation_settings,
    run_http_job,
    seed_verified_article,
)

pytestmark = pytest.mark.integration

_VT_KEY = "synthetic-vt-credential-must-not-persist"
_VT_BASE = "https://www.virustotal.com/api/v3"
_LEGACY_BASE = "https://www.virustotal.com/vtapi/v2"


def _synthetic_hash() -> str:
    return hashlib.sha256(uuid4().bytes).hexdigest()


def _policy_capabilities(
    *, max_claims: int, acquire_hits_allowed: bool = False, external_llm_allowed: bool = True
) -> OsintCapabilities:
    return OsintCapabilities(
        allowed_providers=("vt",),
        budget_limits={
            BudgetDimension.VT_UNITS: 40,
            BudgetDimension.SHODAN_CREDITS: 0,
            BudgetDimension.DOWNLOADS: 2 if acquire_hits_allowed else 0,
            BudgetDimension.BRIDGE_TURNS: 20,
            BudgetDimension.TOOL_SECONDS: 3600,
            BudgetDimension.CONTEXT_BYTES: 1_000_000,
            BudgetDimension.CONTEXT_TOKENS: 100_000,
        },
        acquire_hits_allowed=acquire_hits_allowed,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=external_llm_allowed,
        ),
        max_effect_claims_per_investigation=max_claims,
    )


async def _activate_policy(
    uow_factory,
    subject_id,
    *,
    max_claims: int,
    acquire_hits_allowed: bool = False,
    external_llm_allowed: bool = True,
):
    service = OsintOpenPolicyService(uow_factory, Settings(_env_file=None))
    policy = await service.activate(
        scope_kind=OsintScopeKind.SUBJECT_SET,
        scope_ref=str(subject_id),
        capabilities=_policy_capabilities(
            max_claims=max_claims,
            acquire_hits_allowed=acquire_hits_allowed,
            external_llm_allowed=external_llm_allowed,
        ),
        actor_id="human:vt-read-integration",
        reason="Authorize bounded fake-transport VT reads for PostgreSQL integration.",
        correlation_id=f"vt-read-policy:{subject_id}:{uuid4()}",
    )
    return service, policy


async def _seed_unrelated_unassociated_sample(uow_factory, artifact_store) -> None:
    """Guard against source checks considering unrelated rows for ref-free VT reads."""
    unrelated = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=f"vt-read-unrelated-{uuid4().hex[:12]}",
        include_seed=False,
    )
    async with uow_factory() as uow:
        await uow.sample_associations.append_version(
            SampleAssociation(
                investigation_id=unrelated.investigation.id,
                sample_sha256=_synthetic_hash(),
                origin=SampleOrigin.MANUAL,
            ),
            expected_version=0,
        )
        await uow.commit()


def _make_vt_adapter(
    *,
    proxy_handler,
    direct_handler=None,
    allow_v2_fallback: bool = False,
    enable_behaviour_pcap: bool = False,
    enable_intelligence_search: bool = False,
    enable_file_download: bool = False,
    file_download_legacy_fallback_enabled: bool = False,
    download_allowed_hosts: frozenset[str] = frozenset(),
    download_client_factory=None,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
):
    proxy_client = httpx.AsyncClient(
        transport=proxy_transport or httpx.MockTransport(proxy_handler)
    )
    direct_client = (
        httpx.AsyncClient(
            transport=httpx.MockTransport(direct_handler),
            headers={"accept": "application/json", "x-apikey": _VT_KEY},
        )
        if direct_handler is not None
        else None
    )
    steps = [
        VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    ]
    if allow_v2_fallback:
        steps.append(
            VirusTotalRouteStep(
                transport=VirusTotalTransportKind.DIRECT,
                variant=VirusTotalEndpointVariant.LEGACY_V2,
            )
        )
    routes = {
        VirusTotalCapability.FILE_REPORT: VirusTotalOperationRoute(
            primary=steps[0], fallbacks=tuple(steps[1:])
        )
    }
    capabilities = VirusTotalCapabilities(file_report=True, file_download=enable_file_download)
    if enable_file_download:
        routes[VirusTotalCapability.FILE_DOWNLOAD] = VirusTotalOperationRoute(primary=steps[0])
    if enable_behaviour_pcap:
        routes[VirusTotalCapability.BEHAVIOUR_REPORTS] = VirusTotalOperationRoute(primary=steps[0])
        routes[VirusTotalCapability.BEHAVIOUR_PCAP] = VirusTotalOperationRoute(primary=steps[0])
        capabilities = VirusTotalCapabilities(
            file_report=True,
            file_download=enable_file_download,
            behaviour_reports=True,
            behaviour_pcap=True,
        )
    if enable_intelligence_search:
        routes[VirusTotalCapability.INTELLIGENCE_SEARCH] = VirusTotalOperationRoute(
            primary=steps[0]
        )
        capabilities = VirusTotalCapabilities(
            file_report=True,
            file_download=enable_file_download,
            intelligence_search=True,
            behaviour_reports=capabilities.behaviour_reports,
            behaviour_pcap=capabilities.behaviour_pcap,
        )
    adapter = VirusTotalHttpAdapter(
        client=proxy_client,
        direct_client=direct_client,
        api_key=_VT_KEY if direct_client is not None else None,
        base_url=_VT_BASE,
        legacy_base_url=_LEGACY_BASE if direct_client is not None else None,
        capabilities=capabilities,
        routing_policy=VirusTotalRoutingPolicy(routes=routes),
        behaviour_pcap_enabled=enable_behaviour_pcap,
        file_download_enabled=enable_file_download,
        file_download_legacy_fallback_enabled=file_download_legacy_fallback_enabled,
        download_allowed_hosts=download_allowed_hosts,
        download_client_factory=download_client_factory,
    )
    return adapter, proxy_client, direct_client


async def _run_inherited_case(
    *,
    database_url: str,
    tmp_path: Path,
    max_claims: int,
    hashes: tuple[str, ...],
    proxy_handler,
    direct_handler=None,
    allow_v2_fallback: bool = False,
    enable_behaviour_pcap: bool = False,
    sample_bytes: bytes | None = None,
    sample_tlp: TLP = TLP.CLEAR,
    sample_do_not_submit: bool = False,
    sample_external_llm_allowed: bool = True,
    bypass_legacy_guard: bool = False,
    enable_intelligence_search: bool = False,
    tool_name: str = "vt.file_report",
    tool_arguments: dict[str, object] | None = None,
    source_observation_id: UUID | None = None,
    source_tlp: TLP = TLP.CLEAR,
    source_do_not_submit: bool = False,
    source_license_restriction: str | None = None,
    associate_hashes: bool = True,
    sample_observation_hashes: tuple[str, ...] = (),
    enable_file_download: bool = False,
    file_download_legacy_fallback_enabled: bool = False,
    download_allowed_hosts: frozenset[str] = frozenset(),
    download_client_factory=None,
    proxy_transport: httpx.AsyncBaseTransport | None = None,
    acquire_hits_allowed: bool = False,
    policy_external_llm_allowed: bool = True,
    revoke_policy_before_download_consume: bool = False,
    repeat_tool_call_in_same_turn: bool = False,
):
    engine = create_postgres_engine(database_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    blob_store = FilesystemBlobStore(tmp_path / f"vt-read-blobs-{uuid4().hex}")
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    artifact_store = ProductionArtifactStore(blob_catalog)
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=f"vt-read-{uuid4().hex[:12]}",
        include_seed=False,
    )
    if sample_bytes is not None:
        sample_hash = hashlib.sha256(sample_bytes).hexdigest()
        if sample_hash not in hashes:
            raise ValueError("restricted sample bytes must match a requested VT hash")
        blob_id = await artifact_store.put_bytes(
            sample_bytes,
            bucket=SAMPLES_QUARANTINE_BUCKET,
            mime_type="application/octet-stream",
        )
        async with uow_factory() as uow:
            await uow.samples.add(
                Sample(
                    subject_id=seeded.subject.id,
                    blob_id=blob_id,
                    original_name="synthetic-vt-source.bin",
                    origin="synthetic VT source restriction fixture",
                    acquired_at=datetime.now(UTC),
                    license_restriction=None,
                    tlp=sample_tlp,
                    do_not_submit=sample_do_not_submit,
                    external_llm_allowed=sample_external_llm_allowed,
                    origin_kind=EntitySampleOrigin.MANUAL,
                    state=SampleState.QUARANTINED,
                    source_service="synthetic-vt-source-fixture",
                    source_object_id=sample_hash,
                    expected_hash=sample_hash,
                )
            )
            await uow.commit()
    if source_observation_id is not None:
        source_bytes = f"synthetic restricted VT source document {uuid4()}".encode()
        source_blob_id = await artifact_store.put_bytes(
            source_bytes,
            bucket="vt-read-source-documents",
            mime_type="text/plain",
        )
        source_document = SourceDocument(
            subject_id=seeded.subject.id,
            blob_id=source_blob_id,
            original_name="synthetic-vt-source.txt",
            origin="synthetic VT source restriction fixture",
            acquired_at=datetime.now(UTC),
            license_restriction=source_license_restriction,
            tlp=source_tlp,
            do_not_submit=source_do_not_submit,
            external_llm_allowed=True,
        )
        source_observation = EvidenceObservation(
            investigation_id=seeded.investigation.id,
            kind=EvidenceKind.OTHER,
            blob_id=source_blob_id,
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            source="synthetic-vt-source-document",
            source_version="source-document.v1",
            observed_at=datetime.now(UTC),
            object_type="source_document",
            object_id=str(source_document.id),
            limitations=("Synthetic source-document evidence only.",),
            id=source_observation_id,
        )
        async with uow_factory() as uow:
            await uow.source_documents.add(source_document)
            await uow.evidence_observations.append_if_absent(source_observation)
            await uow.commit()
    if sample_observation_hashes:
        async with uow_factory() as uow:
            for file_hash in sorted(set(sample_observation_hashes)):
                observation_bytes = f"synthetic hash observation {uuid4()}".encode()
                observation_blob_id = await artifact_store.put_bytes(
                    observation_bytes,
                    bucket="vt-read-hash-observations",
                    mime_type="application/octet-stream",
                )
                await uow.evidence_observations.append_if_absent(
                    EvidenceObservation(
                        investigation_id=seeded.investigation.id,
                        kind=EvidenceKind.OTHER,
                        blob_id=observation_blob_id,
                        sha256=hashlib.sha256(observation_bytes).hexdigest(),
                        source="synthetic-vt-hash-observation",
                        source_version="synthetic-v1",
                        observed_at=datetime.now(UTC),
                        object_type="sha256",
                        object_id=file_hash,
                        limitations=("Synthetic source observation only.",),
                    )
                )
            await uow.commit()
    if associate_hashes:
        async with uow_factory() as uow:
            for file_hash in sorted(set(hashes)):
                await uow.sample_associations.append_version(
                    SampleAssociation(
                        investigation_id=seeded.investigation.id,
                        sample_sha256=file_hash,
                        origin=SampleOrigin.MANUAL,
                    ),
                    expected_version=0,
                )
            await uow.commit()
    await _seed_unrelated_unassociated_sample(uow_factory, artifact_store)
    policy_service, policy = await _activate_policy(
        uow_factory,
        seeded.subject.id,
        max_claims=max_claims,
        acquire_hits_allowed=acquire_hits_allowed,
        external_llm_allowed=policy_external_llm_allowed,
    )

    adapter, proxy_client, direct_client = _make_vt_adapter(
        proxy_handler=proxy_handler,
        direct_handler=direct_handler,
        allow_v2_fallback=allow_v2_fallback,
        enable_behaviour_pcap=enable_behaviour_pcap,
        enable_intelligence_search=enable_intelligence_search,
        enable_file_download=enable_file_download,
        file_download_legacy_fallback_enabled=file_download_legacy_fallback_enabled,
        download_allowed_hosts=download_allowed_hosts,
        download_client_factory=download_client_factory,
        proxy_transport=proxy_transport,
    )
    settings = investigation_settings(providers=True)
    if enable_behaviour_pcap:
        settings = settings.model_copy(
            update={
                "virustotal_behaviour_reports_enabled": True,
                "virustotal_behaviour_pcap_enabled": True,
            }
        )
    runtime_ports = build_investigation_runtime_ports(
        settings,
        uow_factory=uow_factory,
        session_factory=session_factory,
        blob_store=blob_store,
        blob_catalog=blob_catalog,
        vt_adapter=adapter,
        vt_capabilities=adapter._capabilities,
        vt_observations=VirusTotalObservationService(blob_catalog, uow_factory),
        shodan_adapter=None,
        ghidra_adapter=None,
    )
    if tool_arguments is not None:
        responses = [
            decision(
                "Read the synthetic VT source evidence.",
                tool_call={
                    "tool_name": tool_name,
                    "schema_version": "1",
                    "arguments": tool_arguments,
                    "purpose": "Check source restrictions before a synthetic VT query.",
                },
            )
        ]
        if repeat_tool_call_in_same_turn:
            first_call = responses[0]["proposed_tool_calls"][0]
            responses[0]["proposed_tool_calls"] = [first_call, dict(first_call)]
    elif enable_behaviour_pcap:
        file_hash = hashes[0]
        responses = [
            decision(
                "Read the synthetic behaviour report.",
                tool_call={
                    "tool_name": "vt.behaviour",
                    "schema_version": "1",
                    "arguments": {"file_hash": file_hash},
                    "purpose": "Verify the persisted sandbox descriptor.",
                },
            ),
            decision(
                "Read the synthetic sandbox capture.",
                tool_call={
                    "tool_name": "vt.behaviour_pcap",
                    "schema_version": "1",
                    "arguments": {"file_hash": file_hash, "sandbox_id": "sandbox-1"},
                    "purpose": "Quarantine the bounded synthetic sandbox capture.",
                },
            ),
        ]
    else:
        responses = [
            decision(
                f"Read synthetic VT report {index}.",
                tool_call={
                    "tool_name": "vt.file_report",
                    "schema_version": "1",
                    "arguments": {"file_hash": file_hash},
                    "purpose": f"Read synthetic report {index}.",
                },
            )
            for index, file_hash in enumerate(hashes, start=1)
        ]
    responses.append(
        decision("Finish from the persisted synthetic VT evidence.", next_action="finish")
    )
    turn_transport = ScriptedTurnTransport(responses)
    consume_patcher = None
    if revoke_policy_before_download_consume:
        coordinator = getattr(adapter, "_external_effect_coordinator", None)
        assert coordinator is not None
        claim_store = coordinator._claim_store
        original_consume = claim_store.consume
        revocation_done = False

        async def revoke_before_consume(authority, prepared, **kwargs):
            nonlocal revocation_done
            if (
                not revocation_done
                and authority.investigation_id == seeded.investigation.id
                and prepared.family == "vt.download_sample"
            ):
                await policy_service.revoke(
                    policy_id=policy.policy_id,
                    expected_version=policy.version,
                    actor_id="human:vt-download-revocation-test",
                    reason="Revoke the synthetic download before coordinator consume.",
                    correlation_id=f"vt-download-revocation:{seeded.investigation.id}",
                )
                revocation_done = True
            return await original_consume(authority, prepared, **kwargs)

        consume_patcher = patch.object(claim_store, "consume", new=revoke_before_consume)
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=turn_transport,
        additional_ports=runtime_ports.optional_tool_ports,
    )
    app, registry, dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as api_client:
            started_response = await api_client.post(
                f"/api/subjects/{seeded.subject.id}/investigations",
                json={
                    "production_run_id": str(seeded.run.id),
                    "synthesis_artifact_id": str(seeded.synthesis.id),
                    "synthesis_sha256": seeded.synthesis.input_hash,
                },
            )
            assert started_response.status_code == 200, started_response.text
            started = started_response.json()
            with ExitStack() as stack:
                if consume_patcher is not None:
                    stack.enter_context(consume_patcher)
                if bypass_legacy_guard:
                    original_dispatch_once = RegistryToolDispatcher._dispatch_once

                    async def dispatch_once_without_legacy_guard(self, context, **kwargs):
                        kwargs["pre_dispatch_check"] = None
                        return await original_dispatch_once(self, context, **kwargs)

                    stack.enter_context(
                        patch.object(
                            RegistryToolDispatcher,
                            "_dispatch_once",
                            dispatch_once_without_legacy_guard,
                        )
                    )
                await run_http_job(
                    app,
                    uow_factory,
                    registry,
                    dispatcher,
                    UUID(started["job_id"]),
                )

        async with uow_factory() as uow:
            invocation_rows = await uow.tool_invocations.list_for_investigation(
                seeded.investigation.id
            )
            human_decisions = await uow.analyst_decisions.list_for_investigation(
                seeded.investigation.id
            )
            envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
            inheritance = await uow.osint_open_policies.get_inheritance(seeded.investigation.id)
            acquisition_errors = []
            for requested_hash in hashes:
                latest_error = await uow.sample_acquisition_attempts.find_latest_error(
                    seeded.investigation.id, requested_hash
                )
                if latest_error is not None:
                    acquisition_errors.append(latest_error)
            await uow.commit()
        async with session_factory() as session:
            claims = list(
                (
                    await session.scalars(
                        select(ExternalEffectClaimRow).where(
                            ExternalEffectClaimRow.investigation_id == seeded.investigation.id
                        )
                    )
                ).all()
            )
        return {
            "adapter": adapter,
            "acquisition_errors": acquisition_errors,
            "claims": claims,
            "direct_client": direct_client,
            "envelope": envelope,
            "hashes": hashes,
            "human_decisions": human_decisions,
            "inheritance": inheritance,
            "invocations": [row for row in invocation_rows if row.tool_name.startswith("vt.")],
            "policy": policy,
            "proxy_client": proxy_client,
            "session_factory": session_factory,
            "engine": engine,
        }
    except BaseException:
        if direct_client is not None:
            await direct_client.aclose()
        await proxy_client.aclose()
        await engine.dispose()
        raise


@pytest.mark.asyncio
async def test_osint_open_inherited_investigation_uses_vt_coordinator_and_hides_credentials(
    migrated_postgres_url: str, tmp_path: Path, caplog
) -> None:
    first_hash = _synthetic_hash()
    second_hash = _synthetic_hash()
    proxy_requests: list[httpx.Request] = []
    direct_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        proxy_requests.append(request)
        requested_hash = request.url.path.rsplit("/", 1)[-1]
        if requested_hash == first_hash:
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": requested_hash,
                    "type": "file",
                    "attributes": {"reputation": 1, "last_analysis_stats": {}},
                }
            },
            request=request,
        )

    def direct_handler(request: httpx.Request) -> httpx.Response:
        direct_requests.append(request)
        requested_hash = request.url.params["resource"]
        return httpx.Response(
            200,
            json={
                "response_code": 1,
                "md5": "c" * 32,
                "sha256": requested_hash,
                "positives": 3,
                "total": 70,
                "scans": {},
            },
            request=request,
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(first_hash, second_hash),
        proxy_handler=proxy_handler,
        direct_handler=direct_handler,
        allow_v2_fallback=True,
    )
    try:
        assert result["envelope"].authority_kind is EnvelopeAuthorityKind.OSINT_POLICY
        assert result["inheritance"].policy_event_id == result["policy"].event_id
        assert result["human_decisions"] == []
        assert [item.status for item in result["invocations"]] == [
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.SUCCEEDED,
        ]
        assert len(proxy_requests) == 2
        assert len(direct_requests) == 1
        assert direct_requests[0].url.params["apikey"] == _VT_KEY
        assert len(result["claims"]) == 3
        assert {claim.family for claim in result["claims"]} == {"vt.file_report"}
        assert {claim.canonical_request["route"] for claim in result["claims"]} == {
            "proxy:v3",
            "direct:legacy_v2",
        }
        durable_rows = json.dumps(
            [
                {
                    "canonical_request": claim.canonical_request,
                    "request_ref": claim.request_ref,
                    "canonical_request_sha256": claim.canonical_request_sha256,
                    "application_request_sha256": claim.application_request_sha256,
                    "evidence_refs": claim.evidence_refs,
                }
                for claim in result["claims"]
            ],
            sort_keys=True,
        )
        assert _VT_KEY not in durable_rows
        assert _VT_KEY not in caplog.text
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_osint_open_vt_read_accepts_investigation_observation_without_association(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    file_hash = _synthetic_hash()
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": file_hash,
                    "type": "file",
                    "attributes": {"reputation": 1, "last_analysis_stats": {}},
                }
            },
            request=request,
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(file_hash,),
        proxy_handler=proxy_handler,
        associate_hashes=False,
        sample_observation_hashes=(file_hash,),
    )
    try:
        assert len(physical_requests) == 1
        assert result["invocations"]
        assert result["invocations"][0].status is ToolInvocationStatus.SUCCEEDED
        assert len(result["claims"]) == 1
        assert result["claims"][0].family == "vt.file_report"
        source_refs = result["claims"][0].evidence_refs
        assert any(reference["kind"] == "sample_hash" for reference in source_refs)
        assert any(reference["kind"] == "evidence_observation" for reference in source_refs)
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_osint_open_candidate_download_claims_every_hop_before_send(
    migrated_postgres_url: str, tmp_path: Path, caplog
) -> None:
    sample_bytes = b"synthetic candidate download through bounded CDN redirect"
    requested_hash = hashlib.sha256(sample_bytes).hexdigest()
    signed_url = (
        "https://vtsamples.commondatastorage.googleapis.com/synthetic-object"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256"
        "&X-Goog-Credential=synthetic/20261026/storage/goog4_request"
        "&X-Goog-Signature=synthetic-signed-url-secret"
    )
    redirected_url = (
        "https://vtsamples.commondatastorage.googleapis.com/synthetic-object-final"
        "?X-Goog-Signature=synthetic-second-signature"
    )
    proxy_snapshots: list[tuple[int, int, str]] = []
    cdn_snapshots: list[tuple[int, int, str]] = []
    cdn_requests: list[httpx.Request] = []
    cdn_call_count = 0
    observer_engine = create_postgres_engine(migrated_postgres_url)
    observer_sessions = create_session_factory(observer_engine)

    async def observe_before_send(target: list[tuple[int, int, str]]) -> None:
        async with observer_sessions() as session:
            dispatches = list(
                (
                    await session.scalars(
                        select(SampleAcquisitionDispatchAttemptRow).where(
                            SampleAcquisitionDispatchAttemptRow.requested_hash == requested_hash
                        )
                    )
                ).all()
            )
            claims = list(
                (
                    await session.scalars(
                        select(ExternalEffectClaimRow).where(
                            ExternalEffectClaimRow.family == "vt.download_sample",
                            ExternalEffectClaimRow.canonical_request["sha256"].astext
                            == requested_hash,
                        )
                    )
                ).all()
            )
            links = list(
                (
                    await session.scalars(
                        select(ExternalEffectAcquisitionDispatchRow).where(
                            ExternalEffectAcquisitionDispatchRow.dispatch_attempt_id.in_(
                                [item.id for item in dispatches]
                            )
                        )
                    )
                ).all()
            )
            target.append(
                (
                    len(claims),
                    len(links),
                    dispatches[0].state if dispatches else "missing",
                )
            )

    class _RedirectTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await observe_before_send(proxy_snapshots)
            if request.url.path.endswith("/download_url"):
                return httpx.Response(200, json={"data": signed_url}, request=request)
            return httpx.Response(404, json={"error": "synthetic route missing"}, request=request)

    class _CdnTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal cdn_call_count
            cdn_call_count += 1
            cdn_requests.append(request)
            await observe_before_send(cdn_snapshots)
            if cdn_call_count == 1:
                return httpx.Response(302, headers={"location": redirected_url}, request=request)
            return httpx.Response(
                200,
                content=sample_bytes,
                headers={"content-type": "application/octet-stream"},
                request=request,
            )

    result = None
    try:
        result = await _run_inherited_case(
            database_url=migrated_postgres_url,
            tmp_path=tmp_path,
            max_claims=10,
            hashes=(requested_hash,),
            proxy_handler=lambda request: httpx.Response(500, request=request),
            proxy_transport=_RedirectTransport(),
            download_client_factory=lambda: httpx.AsyncClient(
                transport=_CdnTransport(), trust_env=False
            ),
            enable_file_download=True,
            download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
            acquire_hits_allowed=True,
            policy_external_llm_allowed=True,
            tool_name="vt.download_sample",
            tool_arguments={
                "sha256": requested_hash,
                "purpose": "Acquire the synthetic candidate for local review.",
                "evidence_refs": [],
            },
        )
        assert result["envelope"].authority_kind is EnvelopeAuthorityKind.OSINT_POLICY
        assert result["human_decisions"] == []
        assert len(result["invocations"]) == 1
        assert result["invocations"][0].status is ToolInvocationStatus.SUCCEEDED
        assert proxy_snapshots == [
            (1, 1, "SUBMISSION_IN_PROGRESS"),
            (2, 2, "SUBMISSION_IN_PROGRESS"),
        ]
        assert cdn_snapshots == [
            (3, 3, "SUBMISSION_IN_PROGRESS"),
            (4, 4, "SUBMISSION_IN_PROGRESS"),
        ]
        assert len(cdn_requests) == 2
        assert "x-apikey" not in cdn_requests[0].headers
        assert "authorization" not in cdn_requests[0].headers

        claims = result["claims"]
        assert len(claims) == 4
        assert {claim.family for claim in claims} == {"vt.download_sample"}
        assert {claim.state for claim in claims} == {"COMPLETED"}
        durable = json.dumps(
            [
                {
                    "canonical_request": claim.canonical_request,
                    "request_ref": claim.request_ref,
                    "canonical_request_sha256": claim.canonical_request_sha256,
                    "application_request_sha256": claim.application_request_sha256,
                    "evidence_refs": claim.evidence_refs,
                }
                for claim in claims
            ],
            sort_keys=True,
        )
        assert signed_url not in durable
        assert "synthetic-signed-url-secret" not in durable
        assert "synthetic-second-signature" not in durable
        assert _VT_KEY not in durable
        assert signed_url not in caplog.text
        assert "synthetic-signed-url-secret" not in caplog.text
        assert "synthetic-second-signature" not in caplog.text

        async with observer_sessions() as session:
            dispatch = await session.scalar(
                select(SampleAcquisitionDispatchAttemptRow).where(
                    SampleAcquisitionDispatchAttemptRow.requested_hash == requested_hash
                )
            )
            association = await session.scalar(
                select(SampleAssociationRow).where(
                    SampleAssociationRow.sample_sha256 == requested_hash
                )
            )
            sample = (
                await session.scalar(select(SampleRow).where(SampleRow.id == association.sample_id))
                if association is not None and association.sample_id is not None
                else None
            )
            association_version = (
                await session.scalar(
                    select(SampleAssociationVersionRow).where(
                        SampleAssociationVersionRow.association_id == association.id
                    )
                )
                if association is not None
                else None
            )
            linked_claim_ids = (
                set(
                    (
                        await session.scalars(
                            select(ExternalEffectAcquisitionDispatchRow.claim_id).where(
                                ExternalEffectAcquisitionDispatchRow.dispatch_attempt_id
                                == dispatch.id
                            )
                        )
                    ).all()
                )
                if dispatch is not None
                else set()
            )
        assert dispatch is not None
        assert dispatch.state == "COMPLETED"
        assert linked_claim_ids == {claim.id for claim in claims}
        assert association is not None
        assert association_version is not None
        assert association_version.qualification == "candidate"
        assert association_version.version == 1
        assert sample is not None
        assert sample.state == "review_candidate"
        assert sample.external_llm_allowed is True
    finally:
        if result is not None and result["direct_client"] is not None:
            await result["direct_client"].aclose()
        if result is not None:
            await result["proxy_client"].aclose()
            await result["engine"].dispose()
        await observer_engine.dispose()


@pytest.mark.asyncio
async def test_verified_local_candidate_reuse_skips_provider_and_s3_claims(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    sample_bytes = b"synthetic verified local quarantine reuse"
    requested_hash = hashlib.sha256(sample_bytes).hexdigest()
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(500, request=request)

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(requested_hash,),
        proxy_handler=proxy_handler,
        sample_bytes=sample_bytes,
        sample_external_llm_allowed=False,
        enable_file_download=True,
        download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
        acquire_hits_allowed=True,
        policy_external_llm_allowed=True,
        tool_name="vt.download_sample",
        tool_arguments={
            "sha256": requested_hash,
            "purpose": "Reuse synthetic bytes already verified in quarantine.",
            "evidence_refs": [],
        },
    )
    try:
        assert physical_requests == []
        assert result["claims"] == []
        assert [item.status for item in result["invocations"]] == [ToolInvocationStatus.SUCCEEDED]
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_download_sha256_mismatch_never_creates_quarantine_sample(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    requested_bytes = b"synthetic expected download bytes"
    returned_bytes = b"different synthetic response bytes"
    requested_hash = hashlib.sha256(requested_bytes).hexdigest()
    returned_hash = hashlib.sha256(returned_bytes).hexdigest()

    class _WrongBytesTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=returned_bytes,
                headers={"content-type": "application/octet-stream"},
                request=request,
            )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(requested_hash,),
        proxy_handler=lambda request: httpx.Response(500, request=request),
        proxy_transport=_WrongBytesTransport(),
        enable_file_download=True,
        acquire_hits_allowed=True,
        policy_external_llm_allowed=True,
        tool_name="vt.download_sample",
        tool_arguments={
            "sha256": requested_hash,
            "purpose": "Check that mismatched synthetic bytes are quarantined nowhere.",
            "evidence_refs": [],
        },
    )
    try:
        assert len(result["claims"]) == 1
        assert result["claims"][0].state == "COMPLETED"
        assert result["invocations"]
        assert result["invocations"][0].status is not ToolInvocationStatus.SUCCEEDED
        async with result["session_factory"]() as session:
            dispatch = await session.scalar(
                select(SampleAcquisitionDispatchAttemptRow).where(
                    SampleAcquisitionDispatchAttemptRow.requested_hash == requested_hash
                )
            )
            association = await session.scalar(
                select(SampleAssociationRow).where(
                    SampleAssociationRow.sample_sha256 == requested_hash
                )
            )
            quarantined_blob = await session.scalar(
                select(BlobRow).where(
                    BlobRow.logical_bucket == SAMPLES_QUARANTINE_BUCKET,
                    BlobRow.sha256 == returned_hash,
                )
            )
            sample = (
                await session.scalar(
                    select(SampleRow).where(SampleRow.source_object_id == requested_hash)
                )
                if dispatch is not None
                else None
            )
        assert dispatch is not None
        assert dispatch.state == "NEEDS_REVIEW"
        assert association is not None
        assert association.sample_id is None
        assert quarantined_blob is None
        assert sample is None
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_ambiguous_download_is_needs_review_and_replay_does_not_send_again(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    requested_hash = _synthetic_hash()
    physical_requests: list[httpx.Request] = []

    class _InterruptedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            physical_requests.append(request)
            raise httpx.ReadError("synthetic interruption after handoff", request=request)

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(requested_hash,),
        proxy_handler=lambda request: httpx.Response(500, request=request),
        proxy_transport=_InterruptedTransport(),
        enable_file_download=True,
        acquire_hits_allowed=True,
        policy_external_llm_allowed=True,
        repeat_tool_call_in_same_turn=True,
        tool_name="vt.download_sample",
        tool_arguments={
            "sha256": requested_hash,
            "purpose": "Resolve one synthetic download interruption without resending.",
            "evidence_refs": [],
        },
    )
    try:
        assert len(physical_requests) == 1
        assert len(result["claims"]) == 1
        assert result["claims"][0].family == "vt.download_sample"
        assert result["claims"][0].state == "NEEDS_REVIEW"
        assert result["invocations"]
        assert result["invocations"][0].status is ToolInvocationStatus.NEEDS_REVIEW
        async with result["session_factory"]() as session:
            dispatch = await session.scalar(
                select(SampleAcquisitionDispatchAttemptRow).where(
                    SampleAcquisitionDispatchAttemptRow.requested_hash == requested_hash
                )
            )
            links = (
                list(
                    (
                        await session.scalars(
                            select(ExternalEffectAcquisitionDispatchRow).where(
                                ExternalEffectAcquisitionDispatchRow.dispatch_attempt_id
                                == dispatch.id
                            )
                        )
                    ).all()
                )
                if dispatch is not None
                else []
            )
        assert dispatch is not None
        assert dispatch.state == "NEEDS_REVIEW"
        assert len(links) == 1
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.parametrize("fallback_enabled", [False, True], ids=("disabled", "enabled"))
@pytest.mark.asyncio
async def test_vt_download_v2_fallback_requires_explicit_opt_in_and_is_claimed(
    migrated_postgres_url: str,
    tmp_path: Path,
    fallback_enabled: bool,
    caplog,
) -> None:
    sample_bytes = b"synthetic opt-in fallback download bytes"
    requested_hash = hashlib.sha256(sample_bytes).hexdigest()
    proxy_requests: list[httpx.Request] = []
    direct_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        proxy_requests.append(request)
        return httpx.Response(
            404,
            json={"error": {"code": "route_missing", "message": "route missing"}},
            headers={"x-proxy-error": "route missing"},
            request=request,
        )

    def direct_handler(request: httpx.Request) -> httpx.Response:
        direct_requests.append(request)
        return httpx.Response(
            200,
            content=sample_bytes,
            headers={"content-type": "application/octet-stream"},
            request=request,
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(requested_hash,),
        proxy_handler=proxy_handler,
        direct_handler=direct_handler,
        enable_file_download=True,
        file_download_legacy_fallback_enabled=fallback_enabled,
        acquire_hits_allowed=True,
        policy_external_llm_allowed=True,
        tool_name="vt.download_sample",
        tool_arguments={
            "sha256": requested_hash,
            "purpose": "Acquire a synthetic sample through the configured fallback.",
            "evidence_refs": [],
        },
    )
    try:
        assert len(proxy_requests) == 2
        assert len(direct_requests) == (1 if fallback_enabled else 0)
        assert [item.status for item in result["invocations"]] == [
            ToolInvocationStatus.SUCCEEDED
            if fallback_enabled
            else ToolInvocationStatus.NEEDS_REVIEW
        ]
        claims = result["claims"]
        assert len(claims) == (3 if fallback_enabled else 2)
        assert {claim.family for claim in claims} == {"vt.download_sample"}
        if fallback_enabled:
            assert direct_requests[0].url.params["apikey"] == _VT_KEY
            assert direct_requests[0].url.params["hash"] == requested_hash
            direct_claims = [
                claim for claim in claims if claim.canonical_request["route"] == "direct:legacy_v2"
            ]
            assert len(direct_claims) == 1
            assert direct_claims[0].canonical_request["hop"] == 0
        durable = json.dumps(
            [
                {
                    "canonical_request": claim.canonical_request,
                    "canonical_request_sha256": claim.canonical_request_sha256,
                    "application_request_sha256": claim.application_request_sha256,
                    "evidence_refs": claim.evidence_refs,
                }
                for claim in claims
            ],
            sort_keys=True,
        )
        assert _VT_KEY not in durable
        assert _VT_KEY not in caplog.text
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_vt_download_rejects_non_allowlisted_redirect_before_target_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    sample_bytes = b"synthetic non-allowlisted redirect fixture"
    requested_hash = hashlib.sha256(sample_bytes).hexdigest()
    target_url = "https://unlisted.invalid/object?sig=synthetic-secret"
    cdn_sends: list[httpx.Request] = []

    class _RedirectTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": target_url}, request=request)

    class _UnexpectedCdnTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            cdn_sends.append(request)
            return httpx.Response(200, content=sample_bytes, request=request)

    def cdn_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_UnexpectedCdnTransport(), trust_env=False)

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(requested_hash,),
        proxy_handler=lambda request: httpx.Response(500, request=request),
        proxy_transport=_RedirectTransport(),
        download_client_factory=cdn_factory,
        enable_file_download=True,
        download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
        acquire_hits_allowed=True,
        policy_external_llm_allowed=True,
        tool_name="vt.download_sample",
        tool_arguments={
            "sha256": requested_hash,
            "purpose": "Reject a synthetic redirect outside the CDN allowlist.",
            "evidence_refs": [],
        },
    )
    try:
        assert cdn_sends == []
        assert len(result["claims"]) == 1
        claim = result["claims"][0]
        assert claim.family == "vt.download_sample"
        assert claim.state == "COMPLETED"
        assert claim.canonical_request["host"] != "unlisted.invalid"
        assert [item.status for item in result["invocations"]] == [
            ToolInvocationStatus.NEEDS_REVIEW
        ]
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_revoked_osint_download_authority_has_zero_send_and_no_claim(
    migrated_postgres_url: str, tmp_path: Path, caplog
) -> None:
    requested_hash = _synthetic_hash()
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(200, content=b"synthetic bytes", request=request)

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(requested_hash,),
        proxy_handler=proxy_handler,
        enable_file_download=True,
        download_allowed_hosts=frozenset({"vtsamples.commondatastorage.googleapis.com"}),
        acquire_hits_allowed=True,
        policy_external_llm_allowed=True,
        revoke_policy_before_download_consume=True,
        tool_name="vt.download_sample",
        tool_arguments={
            "sha256": requested_hash,
            "purpose": "Do not send after synthetic OSINT policy revocation.",
            "evidence_refs": [],
        },
    )
    try:
        assert physical_requests == []
        assert result["claims"] == []
        assert result["invocations"]
        assert result["invocations"][0].status is ToolInvocationStatus.NEEDS_REVIEW
        assert [attempt.error_code for attempt in result["acquisition_errors"]] == [
            "vt_refused_osint_policy_denied"
        ]
        warning = next(
            record
            for record in caplog.records
            if getattr(record, "event", None) == "investigation_tool_invocation_terminal_failure"
        )
        assert warning.error_code == "vt_refused_osint_policy_denied"
        assert requested_hash not in warning.getMessage()
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_osint_open_inherited_investigation_gates_behaviour_pcap(
    migrated_postgres_url: str, tmp_path: Path, caplog
) -> None:
    file_hash = _synthetic_hash()
    proxy_requests: list[httpx.Request] = []
    frame = b"\x00" * 14
    pcap = (
        struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65_535, 1)
        + struct.pack("<IIII", 1, 0, len(frame), len(frame))
        + frame
    )

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        proxy_requests.append(request)
        if request.url.path == f"/api/v3/files/{file_hash}/behaviours":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "sandbox-1",
                            "type": "file_behaviour",
                            "attributes": {
                                "has_pcap": True,
                                "sandbox_name": "synthetic-sandbox",
                            },
                        }
                    ]
                },
                request=request,
            )
        if request.url.path == "/api/v3/file_behaviours/sandbox-1/pcap":
            return httpx.Response(
                200,
                content=pcap,
                headers={"content-type": "application/vnd.tcpdump.pcap"},
                request=request,
            )
        pytest.fail(f"unexpected fake VT request: {request.url!s}")

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(file_hash,),
        proxy_handler=proxy_handler,
        enable_behaviour_pcap=True,
    )
    try:
        assert result["envelope"].authority_kind is EnvelopeAuthorityKind.OSINT_POLICY
        assert result["inheritance"].policy_event_id == result["policy"].event_id
        assert result["human_decisions"] == []
        assert [item.tool_name for item in result["invocations"]] == [
            "vt.behaviour",
            "vt.behaviour_pcap",
        ]
        assert [item.status for item in result["invocations"]] == [
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.SUCCEEDED,
        ]
        assert len(proxy_requests) == 2
        assert len(result["claims"]) == 2
        assert {claim.family for claim in result["claims"]} == {
            "vt.behaviour",
            "vt.behaviour_pcap",
        }
        durable_rows = json.dumps(
            [
                {
                    "canonical_request": claim.canonical_request,
                    "request_ref": claim.request_ref,
                    "canonical_request_sha256": claim.canonical_request_sha256,
                    "application_request_sha256": claim.application_request_sha256,
                    "evidence_refs": claim.evidence_refs,
                }
                for claim in result["claims"]
            ],
            sort_keys=True,
        )
        assert _VT_KEY not in durable_rows
        assert _VT_KEY not in caplog.text
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_osint_open_claim_ceiling_refuses_a_distinct_vt_effect_before_send(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        requested_hash = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": requested_hash,
                    "type": "file",
                    "attributes": {"reputation": 0, "last_analysis_stats": {}},
                }
            },
            request=request,
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=1,
        hashes=(_synthetic_hash(), _synthetic_hash()),
        proxy_handler=proxy_handler,
    )
    try:
        assert len(physical_requests) == 1
        assert len(result["claims"]) == 1
        assert [item.status for item in result["invocations"]] == [
            ToolInvocationStatus.SUCCEEDED,
            ToolInvocationStatus.NEEDS_REVIEW,
        ]
        assert result["human_decisions"] == []
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.parametrize(
    ("sample_tlp", "sample_do_not_submit"),
    [
        (TLP.RED, False),
        (TLP.CLEAR, True),
    ],
    ids=("tlp-red", "do-not-submit"),
)
@pytest.mark.parametrize("bypass_legacy_guard", [False, True], ids=("legacy", "coordinator"))
@pytest.mark.asyncio
async def test_restricted_sample_vt_read_is_refused_before_transport(
    migrated_postgres_url: str,
    tmp_path: Path,
    sample_tlp: TLP,
    sample_do_not_submit: bool,
    bypass_legacy_guard: bool,
) -> None:
    sample_bytes = f"synthetic restricted VT source {uuid4()}".encode()
    file_hash = hashlib.sha256(sample_bytes).hexdigest()
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": file_hash,
                    "type": "file",
                    "attributes": {"reputation": 0, "last_analysis_stats": {}},
                }
            },
            request=request,
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(file_hash,),
        proxy_handler=proxy_handler,
        sample_bytes=sample_bytes,
        sample_tlp=sample_tlp,
        sample_do_not_submit=sample_do_not_submit,
        sample_external_llm_allowed=True,
        bypass_legacy_guard=bypass_legacy_guard,
    )
    try:
        assert physical_requests == []
        assert result["human_decisions"] == []
        assert result["invocations"]
        assert result["invocations"][0].status in {
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.NEEDS_REVIEW,
        }
        if not bypass_legacy_guard:
            assert result["claims"] == []
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_restricted_observation_grounded_vt_read_is_refused_before_transport(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    sample_bytes = f"synthetic restricted observation-grounded source {uuid4()}".encode()
    file_hash = hashlib.sha256(sample_bytes).hexdigest()
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(
            200, json={"data": {"id": file_hash, "type": "file"}}, request=request
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(file_hash,),
        proxy_handler=proxy_handler,
        sample_bytes=sample_bytes,
        sample_do_not_submit=True,
        associate_hashes=False,
        sample_observation_hashes=(file_hash,),
        bypass_legacy_guard=True,
    )
    try:
        assert physical_requests == []
        assert result["invocations"]
        assert result["invocations"][0].status in {
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.NEEDS_REVIEW,
        }
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.asyncio
async def test_ungrounded_vt_hash_read_is_refused_before_transport(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    file_hash = _synthetic_hash()
    physical_requests: list[httpx.Request] = []

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(
            200, json={"data": {"id": file_hash, "type": "file"}}, request=request
        )

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(file_hash,),
        proxy_handler=proxy_handler,
        associate_hashes=False,
    )
    try:
        assert physical_requests == []
        assert result["invocations"]
        assert result["invocations"][0].status in {
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.NEEDS_REVIEW,
        }
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()


@pytest.mark.parametrize(
    ("source_tlp", "source_do_not_submit", "source_license_restriction"),
    [
        (TLP.RED, False, None),
        (TLP.CLEAR, True, None),
        (TLP.CLEAR, False, "no external submission"),
    ],
    ids=("tlp-red", "do-not-submit", "license-veto"),
)
@pytest.mark.parametrize("bypass_legacy_guard", [False, True], ids=("legacy", "coordinator"))
@pytest.mark.asyncio
async def test_restricted_source_document_vt_read_is_refused_before_transport(
    migrated_postgres_url: str,
    tmp_path: Path,
    source_tlp: TLP,
    source_do_not_submit: bool,
    source_license_restriction: str | None,
    bypass_legacy_guard: bool,
) -> None:
    physical_requests: list[httpx.Request] = []
    observation_id = uuid4()

    def proxy_handler(request: httpx.Request) -> httpx.Response:
        physical_requests.append(request)
        return httpx.Response(200, json={"data": []}, request=request)

    result = await _run_inherited_case(
        database_url=migrated_postgres_url,
        tmp_path=tmp_path,
        max_claims=10,
        hashes=(),
        proxy_handler=proxy_handler,
        enable_intelligence_search=True,
        tool_name="vt.intelligence_search",
        tool_arguments={
            "query": "tag:synthetic-source",
            "justification": "Investigate a synthetic source observation.",
            "evidence_refs": [str(observation_id)],
        },
        source_observation_id=observation_id,
        source_tlp=source_tlp,
        source_do_not_submit=source_do_not_submit,
        source_license_restriction=source_license_restriction,
        bypass_legacy_guard=bypass_legacy_guard,
    )
    try:
        assert physical_requests == []
        assert result["human_decisions"] == []
        assert result["invocations"]
        assert result["invocations"][0].status in {
            ToolInvocationStatus.POLICY_DENIED,
            ToolInvocationStatus.NEEDS_REVIEW,
        }
        if not bypass_legacy_guard:
            assert result["claims"] == []
    finally:
        if result["direct_client"] is not None:
            await result["direct_client"].aclose()
        await result["proxy_client"].aclose()
        await result["engine"].dispose()
