from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ScriptedTurnTransport
from cti_app.application.osint_open_policy import OsintOpenPolicyService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EnvelopeAuthorityKind,
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
from cti_app.infrastructure.database.models.external_effects import ExternalEffectClaimRow
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


def _policy_capabilities(*, max_claims: int) -> OsintCapabilities:
    return OsintCapabilities(
        allowed_providers=("vt",),
        budget_limits={
            BudgetDimension.VT_UNITS: 40,
            BudgetDimension.SHODAN_CREDITS: 0,
            BudgetDimension.DOWNLOADS: 0,
            BudgetDimension.BRIDGE_TURNS: 20,
            BudgetDimension.TOOL_SECONDS: 3600,
            BudgetDimension.CONTEXT_BYTES: 1_000_000,
            BudgetDimension.CONTEXT_TOKENS: 100_000,
        },
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.CLEAR,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        max_effect_claims_per_investigation=max_claims,
    )


async def _activate_policy(uow_factory, subject_id, *, max_claims: int):
    service = OsintOpenPolicyService(uow_factory, Settings(_env_file=None))
    policy = await service.activate(
        scope_kind=OsintScopeKind.SUBJECT_SET,
        scope_ref=str(subject_id),
        capabilities=_policy_capabilities(max_claims=max_claims),
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
):
    proxy_client = httpx.AsyncClient(transport=httpx.MockTransport(proxy_handler))
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
    routing = VirusTotalRoutingPolicy(
        routes={
            VirusTotalCapability.FILE_REPORT: VirusTotalOperationRoute(
                primary=steps[0], fallbacks=tuple(steps[1:])
            )
        }
    )
    adapter = VirusTotalHttpAdapter(
        client=proxy_client,
        direct_client=direct_client,
        api_key=_VT_KEY if direct_client is not None else None,
        base_url=_VT_BASE,
        legacy_base_url=_LEGACY_BASE if direct_client is not None else None,
        capabilities=VirusTotalCapabilities(file_report=True),
        routing_policy=routing,
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
    await _seed_unrelated_unassociated_sample(uow_factory, artifact_store)
    policy_service, policy = await _activate_policy(
        uow_factory, seeded.subject.id, max_claims=max_claims
    )
    del policy_service

    adapter, proxy_client, direct_client = _make_vt_adapter(
        proxy_handler=proxy_handler,
        direct_handler=direct_handler,
        allow_v2_fallback=allow_v2_fallback,
    )
    settings = investigation_settings(providers=True)
    runtime_ports = build_investigation_runtime_ports(
        settings,
        uow_factory=uow_factory,
        session_factory=session_factory,
        blob_store=blob_store,
        blob_catalog=blob_catalog,
        vt_adapter=adapter,
        vt_capabilities=VirusTotalCapabilities(file_report=True),
        vt_observations=VirusTotalObservationService(blob_catalog, uow_factory),
        shodan_adapter=None,
        ghidra_adapter=None,
    )
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
            "claims": claims,
            "direct_client": direct_client,
            "envelope": envelope,
            "hashes": hashes,
            "human_decisions": human_decisions,
            "inheritance": inheritance,
            "invocations": [row for row in invocation_rows if row.tool_name == "vt.file_report"],
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
