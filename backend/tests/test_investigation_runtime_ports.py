from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.investigation_composition import (
    InvestigationToolDependencies,
    OptionalInvestigationToolPorts,
    build_investigation_composition,
)
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_state import InMemoryInvestigationStateRepository
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.behavior_evidence import BehaviorEvidenceSource
from cti_app.domain.investigation import SampleAssociation, SampleOrigin
from cti_app.domain.local_analysis import LocalCommandId, LocalCommandResult, LocalCommandStatus
from cti_app.domain.native_analysis import NativeAnalysisOperation, NativeAnalysisRequestV1
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure import investigation_runtime_ports as runtime_ports
from cti_app.infrastructure.investigation_runtime_ports import (
    InvestigationRuntimeComposition,
    build_investigation_runtime_composition_factory,
    describe_tool_inventory,
)
from cti_app.infrastructure.rea_adapter import (
    ReaReadOnlyRequestV1,
    build_native_analysis_port,
)
from cti_app.infrastructure.virustotal_retrohunt_client import VirusTotalRetrohuntHttpClient

_SAMPLE_SHA256 = "a" * 64


def _root_builders() -> tuple[Any, Any]:
    from cti_app.api import main as api_main
    from cti_app.workers import tasks as worker_tasks

    return (
        api_main.build_investigation_runtime_composition_factory,
        worker_tasks.build_investigation_runtime_composition_factory,
    )


def _runtime_inputs(settings: Settings) -> dict[str, object]:
    uow_factory = cast(UnitOfWorkFactory, lambda: None)
    blob_store = cast(BlobStore, object())
    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    return {
        "settings": settings,
        "uow_factory": uow_factory,
        "blob_store": blob_store,
        "blob_catalog": blob_catalog,
        "artifact_store": cast(ProductionArtifactStore, object()),
        "model_conversations": cast(ModelConversationService, object()),
        "vt_adapter": None,
        "vt_capabilities": VirusTotalCapabilities(),
        "vt_observations": None,
        "shodan_adapter": None,
        "ghidra_adapter": None,
    }


def _status_map(composition: InvestigationRuntimeComposition) -> dict[str, Any]:
    return describe_tool_inventory(composition)["families"]  # type: ignore[return-value]


@pytest.mark.parametrize("root_builder", _root_builders())
def test_api_and_worker_roots_share_real_runtime_assembly_without_infrastructure(
    root_builder: Any,
) -> None:
    runtime = root_builder(**_runtime_inputs(Settings.model_construct()))
    statuses = _status_map(runtime)

    assert statuses["sample_similarity"] == {"status": "available", "reason": None}
    assert statuses["behavior_report_import"] == {"status": "available", "reason": None}
    assert statuses["retrohunt"] == {
        "status": "unavailable",
        "reason": "retrohunt_disabled_by_configuration",
    }
    assert statuses["network_capture"] == {
        "status": "unavailable",
        "reason": "no_operator_capture_source",
    }
    assert statuses["native_analysis"] == {
        "status": "unavailable",
        "reason": "provider_unavailable:none",
    }
    assert runtime.composition_factory.optional_tool_ports is runtime.ports.optional_tool_ports

    serialized = json.dumps(describe_tool_inventory(runtime), sort_keys=True)
    assert _SAMPLE_SHA256 not in serialized
    assert "api_key" not in serialized
    json.loads(serialized)


class _SyntheticCommandService:
    def __init__(self, *, confined: bool) -> None:
        self.filesystem_confined = confined

    async def run(
        self,
        command_id: LocalCommandId,
        sample_sha256: str,
        _options: Mapping[str, object] | None = None,
    ) -> LocalCommandResult:
        confined = self.filesystem_confined
        return LocalCommandResult(
            command_id=command_id,
            status=LocalCommandStatus.EMPTY if confined else LocalCommandStatus.UNAVAILABLE,
            sample_sha256=sample_sha256,
            output="{}" if confined else "",
            executable_version="synthetic-1.0" if confined else None,
        )


class _SyntheticCommandCatalog:
    def __init__(self, *, available: bool) -> None:
        self._capabilities = {
            command_id: SimpleNamespace(
                spec=SimpleNamespace(id=command_id),
                executable_path="synthetic" if available else None,
                version="synthetic-1.0" if available else None,
                available=available,
            )
            for command_id in (LocalCommandId.FLOSS, LocalCommandId.MWCP)
        }

    def get(self, command_id: LocalCommandId) -> object | None:
        return self._capabilities.get(command_id)

    def all_commands(self) -> tuple[object, ...]:
        return tuple(self._capabilities.values())

    def available_commands(self) -> tuple[object, ...]:
        return tuple(
            capability for capability in self._capabilities.values() if capability.available
        )


@pytest.mark.asyncio
async def test_configured_extraction_ports_run_synthetic_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _SyntheticCommandCatalog(available=True)
    service = _SyntheticCommandService(confined=True)
    monkeypatch.setattr(runtime_ports, "LocalCommandCatalog", lambda **_kwargs: catalog)
    monkeypatch.setattr(runtime_ports, "LocalCommandService", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(runtime_ports, "LocalCommandRunner", lambda **_kwargs: object())
    settings = Settings.model_construct(
        local_analysis_enabled=True,
        mwcp_parser_versions={"test.parser": "1.0"},
    )
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(settings)  # type: ignore[arg-type]
    )
    ports = runtime.ports.optional_tool_ports
    binaries = cast(dict[str, object], describe_tool_inventory(runtime)["binaries"])

    assert ports.floss_extraction is not None and ports.floss_extraction.available
    assert ports.configuration_extraction is not None
    assert ports.configuration_extraction.available
    assert binaries["floss"] == {"available": True, "version": "synthetic-1.0"}
    floss_result = await ports.floss_extraction.extract(_SAMPLE_SHA256, max_strings=8)
    configuration_result = await ports.configuration_extraction.extract(
        _SAMPLE_SHA256,
        parser_id="test.parser",
    )
    assert floss_result.sample_sha256 == _SAMPLE_SHA256
    assert configuration_result.sample_sha256 == _SAMPLE_SHA256

    report = json.dumps(
        {
            "target": {"file": {"sha256": _SAMPLE_SHA256}},
            "behavior": {"events": [{"category": "process", "description": "synthetic"}]},
        }
    )
    imported = BehaviorReportImporter().parse(
        report,
        source=BehaviorEvidenceSource.CAPE,
        expected_sample_sha256=_SAMPLE_SHA256,
    )
    assert imported.sample_sha256 == _SAMPLE_SHA256


@pytest.mark.asyncio
async def test_missing_confinement_and_parser_allowlist_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _SyntheticCommandCatalog(available=True)
    service = _SyntheticCommandService(confined=False)
    monkeypatch.setattr(runtime_ports, "LocalCommandCatalog", lambda **_kwargs: catalog)
    monkeypatch.setattr(runtime_ports, "LocalCommandService", lambda *_args, **_kwargs: service)
    monkeypatch.setattr(runtime_ports, "LocalCommandRunner", lambda **_kwargs: object())
    settings = Settings.model_construct(local_analysis_enabled=True, mwcp_parser_versions={})
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(settings)  # type: ignore[arg-type]
    )
    statuses = _status_map(runtime)
    ports = runtime.ports.optional_tool_ports

    assert statuses["floss_extraction"]["reason"] == "filesystem_confinement_required"
    assert statuses["configuration_extraction"]["reason"] == "parser_allowlist_empty"
    assert ports.floss_extraction is not None
    assert ports.configuration_extraction is not None
    floss_result = await ports.floss_extraction.extract(_SAMPLE_SHA256, max_strings=8)
    configuration_result = await ports.configuration_extraction.extract(
        _SAMPLE_SHA256,
        parser_id="not-allowlisted",
    )
    assert floss_result.status.value == "unavailable"
    assert configuration_result.status.value == "policy_denied"


class _CaptureAdapter:
    available = True

    def versions(self) -> tuple[str, str]:
        return "tshark synthetic", "capinfos synthetic"

    async def inspect(self, capture_bytes: bytes, sha256: str) -> dict[str, object]:
        return {"capture_bytes": len(capture_bytes), "sha256": sha256}


class _SyntheticRetrohuntProvider:
    available = True

    async def submit_retrohunt(self, request: object) -> dict[str, object]:
        return {"accepted": True, "request": request}


class _ConfinedReaRunner:
    available = True
    worker_confined = True
    dedicated_user = True
    network_disabled = True
    filesystem_confined = True
    no_host_privileges = True

    async def inspect(
        self,
        contract: ReaReadOnlyRequestV1,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Mapping[str, object]:
        del timeout_seconds, max_response_bytes
        request = contract.request
        return {
            "schema_version": "autowork-rea-read-response.v1",
            "provider_version": "rea-test",
            "catalog_version": contract.catalog_version,
            "sample_sha256": request.sample_sha256,
            "operation": request.operation.value,
            "request_fingerprint": request.fingerprint,
            "data": {"functions": []},
        }


@pytest.mark.asyncio
async def test_optional_future_adapters_are_passed_and_reflected_in_statuses() -> None:
    from cti_app.api import main as api_main
    from cti_app.workers import tasks as worker_tasks

    retrohunt_provider = _SyntheticRetrohuntProvider()
    quarantine = SimpleNamespace()
    tshark = _CaptureAdapter()
    runner = _ConfinedReaRunner()
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    settings = Settings.model_construct(
        virustotal_retrohunt_enabled=True,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_api_key="test-vt-key",
        native_analysis_enabled=True,
        native_analysis_provider="rea",
        native_analysis_rea_expected_version="rea-test",
    )
    base = _runtime_inputs(settings)
    base.update(
        {
            "vt_capabilities": VirusTotalCapabilities(retrohunt=True),
            "retrohunt_provider": retrohunt_provider,
            "vt_routing_policy": VirusTotalRoutingPolicy(
                routes={VirusTotalCapability.RETROHUNT: route}
            ),
            "network_quarantine": quarantine,
            "tshark": tshark,
            "confined_rea_runner": runner,
        }
    )

    api_runtime = api_main.build_investigation_runtime_composition_factory(**base)
    worker_runtime = worker_tasks.build_investigation_runtime_composition_factory(**base)

    assert describe_tool_inventory(api_runtime) == describe_tool_inventory(worker_runtime)
    assert _status_map(api_runtime)["retrohunt"]["status"] == "available"
    assert _status_map(api_runtime)["network_capture"]["status"] == "available"
    assert _status_map(api_runtime)["native_analysis"]["status"] == "available"
    ports = api_runtime.composition_factory.optional_tool_ports
    assert ports is not None
    assert ports.retrohunt_provider is retrohunt_provider
    assert ports.network_quarantine is quarantine
    assert ports.tshark is tshark
    assert ports.confined_rea_runner is runner

    retrohunt_result = await retrohunt_provider.submit_retrohunt({"query": "synthetic"})
    capture_result = await tshark.inspect(b"synthetic-capture", _SAMPLE_SHA256)
    rea_port = build_native_analysis_port(settings, runner=runner)
    rea_result = await rea_port.inspect(
        NativeAnalysisRequestV1(
            sample_sha256=_SAMPLE_SHA256,
            operation=NativeAnalysisOperation.LIST_FUNCTIONS,
        )
    )
    assert retrohunt_result["accepted"] is True
    assert capture_result == {"capture_bytes": len(b"synthetic-capture"), "sha256": _SAMPLE_SHA256}
    assert rea_result.data == {"functions": []}


class _UnavailableCaptureAdapter:
    available = False

    def versions(self) -> tuple[None, None]:
        return None, None


def test_network_capture_status_uses_the_supplied_source_and_real_tshark_state() -> None:
    settings = Settings.model_construct()
    base = _runtime_inputs(settings)
    source = SimpleNamespace()

    present = build_investigation_runtime_composition_factory(
        **base,
        network_quarantine=source,
        tshark=_CaptureAdapter(),  # type: ignore[arg-type]
    )
    unavailable = build_investigation_runtime_composition_factory(
        **base,
        network_quarantine=source,
        tshark=_UnavailableCaptureAdapter(),  # type: ignore[arg-type]
    )

    assert _status_map(present)["network_capture"] == {"status": "available", "reason": None}
    assert _status_map(unavailable)["network_capture"] == {
        "status": "unavailable",
        "reason": "tshark_or_filesystem_confinement_unavailable",
    }


def test_network_quarantine_factory_is_bound_to_each_composition_investigation() -> None:
    settings = Settings.model_construct()
    calls: list[UUID] = []
    source = SimpleNamespace()

    def source_for(investigation_id: UUID):
        calls.append(investigation_id)
        return source

    uow = _unit_of_work(_SAMPLE_SHA256)
    composition = build_investigation_composition(
        uow,  # type: ignore[arg-type]
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=cast(ProductionArtifactStore, object()),
        model_conversations=cast(ModelConversationService, object()),
        settings=settings,
        investigation_id=UUID("0189a6c4-3b10-7d11-bf73-81293c592c77"),
        optional_tool_ports=OptionalInvestigationToolPorts(
            network_quarantine_factory=source_for,  # type: ignore[arg-type]
            tshark=_UnavailableCaptureAdapter(),  # type: ignore[arg-type]
        ),
    )

    assert calls == [UUID("0189a6c4-3b10-7d11-bf73-81293c592c77")]
    assert isinstance(composition.tool_dependencies, InvestigationToolDependencies)
    assert composition.tool_dependencies.network_quarantine is source
    assert not any(name.startswith("network.") for name in composition.available_tools)


def test_rea_without_a_confined_runner_reports_the_adapter_reason() -> None:
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(
            Settings.model_construct(
                native_analysis_enabled=True,
                native_analysis_provider="rea",
                native_analysis_rea_expected_version="rea-test",
            )
        )  # type: ignore[arg-type]
    )

    assert runtime.ports.optional_tool_ports.confined_rea_runner is None
    assert _status_map(runtime)["native_analysis"] == {
        "status": "unavailable",
        "reason": "rea_runner_not_configured",
    }


@pytest.mark.asyncio
async def test_retrohunt_runtime_requires_opt_in_capability_and_proxy_route() -> None:
    from cti_app.api import main as api_main
    from cti_app.workers import tasks as worker_tasks

    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "job-1",
                    "type": "retrohunt_job",
                    "attributes": {"status": "queued"},
                }
            },
            request=request,
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
    )
    configured = {
        "virustotal_proxy_url": "http://proxy.test:8080",
        "virustotal_api_key": "test-vt-key",
    }
    configured_runtimes: list[InvestigationRuntimeComposition] = []

    def build(root_builder: Any, settings: Settings, capabilities: VirusTotalCapabilities):
        inputs = _runtime_inputs(settings)
        inputs["vt_capabilities"] = capabilities
        return root_builder(**inputs, retrohunt_http_client=http_client)

    try:
        for root_builder in (
            api_main.build_investigation_runtime_composition_factory,
            worker_tasks.build_investigation_runtime_composition_factory,
        ):
            disabled = build(
                root_builder,
                Settings.model_construct(**configured),
                VirusTotalCapabilities(retrohunt=True),
            )
            assert _status_map(disabled)["retrohunt"]["reason"] == (
                "retrohunt_disabled_by_configuration"
            )

            enabled_without_route = build(
                root_builder,
                Settings.model_construct(virustotal_retrohunt_enabled=True),
                VirusTotalCapabilities(retrohunt=True),
            )
            assert _status_map(enabled_without_route)["retrohunt"]["reason"] == (
                "retrohunt_route_not_configured"
            )

            enabled_without_capability = build(
                root_builder,
                Settings.model_construct(
                    virustotal_retrohunt_enabled=True,
                    **configured,
                ),
                VirusTotalCapabilities(retrohunt=False),
            )
            assert _status_map(enabled_without_capability)["retrohunt"]["reason"] == (
                "retrohunt_capability_disabled"
            )

            enabled = build(
                root_builder,
                Settings.model_construct(
                    virustotal_retrohunt_enabled=True,
                    **configured,
                ),
                VirusTotalCapabilities(retrohunt=True),
            )
            assert _status_map(enabled)["retrohunt"] == {"status": "available", "reason": None}
            assert isinstance(
                enabled.ports.optional_tool_ports.retrohunt_provider,
                VirusTotalRetrohuntHttpClient,
            )
            configured_runtimes.append(enabled)
            assert observed == []

        assert describe_tool_inventory(configured_runtimes[0]) == describe_tool_inventory(
            configured_runtimes[1]
        )
        provider = configured_runtimes[0].ports.optional_tool_ports.retrohunt_provider
        assert provider is not None
        job = await provider.retrohunt_status("job-1")
        assert job.job_id == "job-1"

        assert len(observed) == 1
        assert str(observed[0].url) == (
            "https://www.virustotal.com/api/v3/intelligence/retrohunt_jobs/job-1"
        )
        assert observed[0].headers["x-apikey"] == "test-vt-key"
    finally:
        await http_client.aclose()


class _AssociationReader:
    def __init__(self, sample_sha256: str) -> None:
        self._sample_sha256 = sample_sha256

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return (
            SampleAssociation(
                investigation_id=investigation_id,
                sample_sha256=self._sample_sha256,
                origin=SampleOrigin.SEED,
            ),
        )


def _unit_of_work(sample_sha256: str) -> SimpleNamespace:
    return SimpleNamespace(
        analyst_investigations=SimpleNamespace(),
        investigation_states=InMemoryInvestigationStateRepository(),
        investigation_envelopes=InMemoryInvestigationEnvelopeRepository(),
        tool_invocations=InMemoryToolInvocationRepository(),
        evidence_observations=InMemoryEvidenceObservationRepository(),
        investigation_findings=InMemoryFindingRepository(),
        sample_associations=_AssociationReader(sample_sha256),
        analyst_opinions=InMemoryAnalystOpinionRepository(),
    )


@pytest.mark.asyncio
async def test_sample_similarity_reader_is_bound_to_each_composition_uow() -> None:
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(Settings.model_construct())  # type: ignore[arg-type]
    )
    investigation_a, investigation_b = uuid4(), uuid4()
    uow_a, uow_b = _unit_of_work("a" * 64), _unit_of_work("b" * 64)

    composition_a = runtime.composition_factory.build(uow_a, investigation_a)
    composition_b = runtime.composition_factory.build(uow_b, investigation_b)
    similarity_a = composition_a.tool_dependencies.sample_similarity
    similarity_b = composition_b.tool_dependencies.sample_similarity

    assert similarity_a is not None and similarity_b is not None
    assert similarity_a is not similarity_b
    snapshot_a = await similarity_a._corpus_snapshots.get_snapshot(investigation_a)
    snapshot_b = await similarity_b._corpus_snapshots.get_snapshot(investigation_b)
    assert snapshot_a.sample_sha256s == ("a" * 64,)
    assert snapshot_b.sample_sha256s == ("b" * 64,)
    assert similarity_a._corpus_snapshots._uow is uow_a
    assert similarity_b._corpus_snapshots._uow is uow_b
