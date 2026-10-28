from __future__ import annotations

import json
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisWorkerAttestation,
)
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
from cti_app.domain.investigation import SampleAssociation, SampleOrigin
from cti_app.domain.local_analysis import LocalCommandId
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


def test_virustotal_capabilities_and_status_require_a_verified_v3_route() -> None:
    from cti_app.workers.tasks import virustotal_capabilities_from_settings

    settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_file_download_enabled=True,
        virustotal_behaviour_pcap_enabled=True,
        virustotal_download_allowed_hosts="vtsamples.commondatastorage.googleapis.com",
    )
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    worker_capabilities = virustotal_capabilities_from_settings(settings)
    assert worker_capabilities.file_download is True
    assert worker_capabilities.behaviour_pcap is True

    no_routes = VirusTotalRoutingPolicy()
    adapter = SimpleNamespace(_routing_policy=no_routes)
    assert runtime_ports._vt_proxy_route_status(
        settings, adapter, VirusTotalCapability.FILE_DOWNLOAD
    ) == (False, "virustotal_route_not_configured")

    configured = VirusTotalRoutingPolicy(
        routes={
            VirusTotalCapability.FILE_DOWNLOAD: route,
            VirusTotalCapability.BEHAVIOUR_PCAP: route,
        }
    )
    adapter = SimpleNamespace(_routing_policy=configured)
    assert runtime_ports._vt_proxy_route_status(
        settings, adapter, VirusTotalCapability.FILE_DOWNLOAD
    ) == (True, None)
    assert runtime_ports._vt_proxy_route_status(
        settings, adapter, VirusTotalCapability.BEHAVIOUR_PCAP
    ) == (True, None)

    inputs = _runtime_inputs(settings)
    inputs.update(
        {
            "vt_adapter": SimpleNamespace(
                _routing_policy=no_routes,
                configure_external_effects=lambda *_args: None,
            ),
            "vt_observations": object(),
            "session_factory": object(),
        }
    )
    inputs["vt_capabilities"] = VirusTotalCapabilities(file_download=True, behaviour_pcap=True)
    runtime = build_investigation_runtime_composition_factory(**inputs)
    statuses = _status_map(runtime)
    assert statuses["virustotal"] == {
        "status": "unavailable",
        "reason": "virustotal_route_not_configured",
    }
    assert statuses["virustotal_file_download"] == {
        "status": "unavailable",
        "reason": "virustotal_route_not_configured",
    }
    assert statuses["virustotal_behaviour_pcap"] == {
        "status": "unavailable",
        "reason": "virustotal_route_not_configured",
    }

    configured_inputs = _runtime_inputs(settings)
    configured_inputs.update(
        {
            "vt_adapter": SimpleNamespace(
                _routing_policy=configured,
                configure_external_effects=lambda *_args: None,
            ),
            "vt_observations": object(),
            "session_factory": object(),
        }
    )
    configured_inputs["vt_capabilities"] = VirusTotalCapabilities()
    configured_runtime = build_investigation_runtime_composition_factory(**configured_inputs)
    configured_statuses = _status_map(configured_runtime)
    assert configured_statuses["virustotal"] == {"status": "available", "reason": None}
    assert configured_statuses["virustotal_behaviour_pcap"] == {
        "status": "available",
        "reason": None,
    }


def test_virustotal_reads_are_unavailable_without_a_coordinator_session_factory() -> None:
    settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_file_report_enabled=True,
    )
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    adapter = SimpleNamespace(
        _routing_policy=VirusTotalRoutingPolicy(routes={VirusTotalCapability.FILE_REPORT: route}),
        configure_external_effects=lambda *_args: None,
    )
    inputs = _runtime_inputs(settings)
    inputs.update(
        {
            "vt_adapter": adapter,
            "vt_capabilities": VirusTotalCapabilities(file_report=True),
            "vt_observations": object(),
        }
    )

    runtime = build_investigation_runtime_composition_factory(**inputs)

    statuses = _status_map(runtime)
    assert statuses["virustotal_reads"] == {
        "status": "unavailable",
        "reason": "external_effect_coordinator_unavailable",
    }
    assert runtime.composition_factory.optional_tool_ports is not None
    assert runtime.composition_factory.optional_tool_ports.vt_capabilities is not None
    assert runtime.composition_factory.optional_tool_ports.vt_capabilities.file_report is False


def test_download_requires_cdn_allowlist_even_when_read_coordinator_is_absent() -> None:
    wrong_host_settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_file_download_enabled=True,
        virustotal_download_allowed_hosts="cdn.example.test",
    )
    no_routes = VirusTotalRoutingPolicy()
    wrong_host_inputs = _runtime_inputs(wrong_host_settings)
    wrong_host_inputs["vt_adapter"] = SimpleNamespace(_routing_policy=no_routes)
    wrong_host_inputs["vt_capabilities"] = VirusTotalCapabilities(file_download=True)
    wrong_host_runtime = build_investigation_runtime_composition_factory(**wrong_host_inputs)
    assert _status_map(wrong_host_runtime)["virustotal_file_download"] == {
        "status": "unavailable",
        "reason": "virustotal_download_cdn_host_not_allowlisted",
    }


def test_advisor_transport_requires_nonempty_bridge_config_and_bridge_route() -> None:
    default = Settings.model_construct(
        openai_bridge_base_url="http://127.0.0.1:8001/v1",
        openai_bridge_api_key=None,
        model_force_adapter="auto",
        model_route_web_research="chatgpt_bridge",
    )
    assert runtime_ports._bridge_turn_transport_configured(default) is False

    empty_key = Settings.model_construct(
        openai_bridge_base_url="http://127.0.0.1:8001/v1",
        openai_bridge_api_key=SecretStr(""),
        model_force_adapter="auto",
        model_route_web_research="chatgpt_bridge",
    )
    assert runtime_ports._bridge_turn_transport_configured(empty_key) is False

    configured_key = Settings.model_construct(
        openai_bridge_base_url="http://127.0.0.1:8001/v1",
        openai_bridge_api_key=SecretStr("configured-for-test"),
        model_force_adapter="auto",
        model_route_web_research="chatgpt_bridge",
    )
    assert runtime_ports._bridge_turn_transport_configured(configured_key) is True

    alternate_endpoint = Settings.model_construct(
        openai_bridge_base_url="http://bridge.internal/v1",
        openai_bridge_api_key=None,
        model_force_adapter="chatgpt_bridge",
        model_route_web_research="fake",
    )
    assert runtime_ports._bridge_turn_transport_configured(alternate_endpoint) is True

    non_bridge_route = Settings.model_construct(
        openai_bridge_base_url="http://bridge.internal/v1",
        openai_bridge_api_key=SecretStr("configured-for-test"),
        model_force_adapter="auto",
        model_route_web_research="qwen",
    )
    assert runtime_ports._bridge_turn_transport_configured(non_bridge_route) is False


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


def test_behavior_report_import_status_tracks_the_parser_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_ports, "BehaviorReportImporter", lambda: None)

    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(Settings.model_construct())
    )

    assert _status_map(runtime)["behavior_report_import"] == {
        "status": "unavailable",
        "reason": "behavior_report_importer_unavailable",
    }
    assert runtime.ports.optional_tool_ports.behavior_report_importer is None


def test_runtime_does_not_fall_back_to_local_yara_without_measured_confinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_ports,
        "LocalCommandRunner",
        lambda **_kwargs: SimpleNamespace(filesystem_confined=False),
    )
    monkeypatch.setattr(
        runtime_ports,
        "IsolatedYaraRunner",
        lambda: pytest.fail("local YARA runner must not start without measured confinement"),
    )
    settings = Settings.model_construct(rule_workbench_enabled=True, analysis_worker_enabled=False)

    runtime = build_investigation_runtime_composition_factory(**_runtime_inputs(settings))

    status = _status_map(runtime)["rule_workbench"]
    assert status == {
        "status": "unavailable",
        "reason": "analysis_worker_disabled_by_configuration",
        "available_reason": "analysis_worker_disabled_by_configuration",
        "execution_mode": "unavailable",
        "version": None,
    }


def test_runtime_allows_local_yara_only_with_measured_confinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AvailableYaraRunner:
        available = True
        unavailable_reason = None

    engine = _AvailableYaraRunner()
    monkeypatch.setattr(
        runtime_ports,
        "LocalCommandRunner",
        lambda **_kwargs: SimpleNamespace(filesystem_confined=True),
    )
    monkeypatch.setattr(runtime_ports, "IsolatedYaraRunner", lambda *_args, **_kwargs: engine)
    settings = Settings.model_construct(rule_workbench_enabled=True, analysis_worker_enabled=False)

    runtime = build_investigation_runtime_composition_factory(**_runtime_inputs(settings))

    status = _status_map(runtime)["rule_workbench"]
    assert runtime.ports.optional_tool_ports.yara_engine is engine
    assert status["status"] == "available"
    assert status["execution_mode"] == "local-bwrap-measured"


def test_local_command_worker_stays_unavailable_when_worker_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnconfinedRunner:
        filesystem_confined = False
        namespace_reason = "bwrap_probe_filesystem_not_confined"

    monkeypatch.setattr(runtime_ports, "LocalCommandRunner", lambda **_kwargs: _UnconfinedRunner())
    settings = Settings.model_construct(
        local_analysis_enabled=True,
        local_analysis_require_filesystem_confinement=True,
    )

    runtime = build_investigation_runtime_composition_factory(**_runtime_inputs(settings))

    status = _status_map(runtime)["local_command_execution"]
    assert status["status"] == "unavailable"
    assert status["reason"] == "analysis_worker_disabled_by_configuration"
    assert status["execution_mode"] == "analysis-worker"
    assert _status_map(runtime)["local_filesystem_confinement"]["reason"] == (
        "bwrap_probe_filesystem_not_confined"
    )


def test_inventory_separates_unmeasured_local_confinement_from_worker_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    families = {
        "sample_analysis": ConfinedAnalysisFamilyStatus(
            "sample_analysis", True, "sample-analysis.v1"
        ),
        "static": ConfinedAnalysisFamilyStatus("static", True, "static-v1"),
        "capa": ConfinedAnalysisFamilyStatus("capa", True, "9.4.0"),
        "smda": ConfinedAnalysisFamilyStatus("smda", True, "4.5.0"),
        "local_command": ConfinedAnalysisFamilyStatus(
            "local_command", True, "local-command-catalog-v1"
        ),
        "floss": ConfinedAnalysisFamilyStatus(
            "floss", False, None, "analysis_family_not_supported_by_worker"
        ),
        "mwcp": ConfinedAnalysisFamilyStatus("mwcp", True, "3.16.1", "no_authorized_parser"),
        "yara": ConfinedAnalysisFamilyStatus("yara", True, "4.5.0"),
    }
    attestation = ConfinedAnalysisWorkerAttestation(
        reachable=True,
        confined=True,
        isolation_profile="bookworm-internal-v1",
        versions={
            "capa": "9.4.0",
            "smda": "4.5.0",
            "mwcp": "3.16.1",
            "yara": "4.5.0",
            "local_file": "5.44",
            "local_strings": "2.40",
            "local_readelf": "2.40",
            "local_objdump": "2.40",
            "local_xxd": "2022-01-14",
            "local_sha256sum": "9.1",
        },
        families=families,
        dependencies_ready=True,
    )
    worker_port = SimpleNamespace(
        available=True,
        available_reason=None,
        family_statuses=families,
        isolation_profile="bookworm-internal-v1",
        attestation=attestation,
    )

    class _UnconfinedRunner:
        filesystem_confined = False
        namespace_reason = "bwrap_userns_refused_by_kernel"

    monkeypatch.setattr(runtime_ports, "LocalCommandRunner", lambda **_kwargs: _UnconfinedRunner())
    monkeypatch.setattr(
        runtime_ports,
        "build_confined_analysis_execution_port",
        lambda *_args, **_kwargs: worker_port,
    )
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(Settings.model_construct(local_analysis_enabled=True))
    )

    inventory = describe_tool_inventory(runtime)
    inventory_families = cast(dict[str, dict[str, object]], inventory["families"])
    worker = cast(dict[str, object], inventory["analysis_worker"])
    worker_families = cast(dict[str, dict[str, object]], worker["families"])

    assert inventory_families["local_filesystem_confinement"]["status"] == "unavailable"
    assert inventory_families["local_filesystem_confinement"]["reason"] == (
        "bwrap_userns_refused_by_kernel"
    )
    assert inventory_families["local_command_execution"]["status"] == "available"
    assert inventory_families["local_command_execution"]["execution_mode"] == "analysis-worker"
    assert runtime.ports.optional_tool_ports.local_command_catalog is not None
    assert {
        item.spec.id.value
        for item in runtime.ports.optional_tool_ports.local_command_catalog.available_commands()
    } >= {"file", "strings", "readelf", "objdump"}
    assert inventory_families["sample_analysis"] == {
        "status": "available",
        "reason": None,
        "available_reason": None,
        "execution_mode": "analysis-worker",
        "version": "sample-analysis.v1",
    }
    assert worker["status"] == "available"
    assert worker_families["floss"]["available"] is False
    assert worker_families["floss"]["available_reason"] == (
        "analysis_family_not_supported_by_worker"
    )
    assert worker_families["mwcp"] == {
        "available": True,
        "version": "3.16.1",
        "available_reason": "no_authorized_parser",
    }
    assert inventory_families["analysis_worker_mwcp"]["execution_mode"] == "analysis-worker"
    assert inventory_families["analysis_worker_mwcp"]["version"] == "3.16.1"
    assert inventory_families["analysis_worker_mwcp"]["available_reason"] == (
        "no_authorized_parser"
    )


def test_archive_commands_are_projected_only_from_matching_worker_attestations() -> None:
    settings = Settings(_env_file=None, local_analysis_enabled=True)
    families = {
        "zip_members": ConfinedAnalysisFamilyStatus("zip_members", True, "autowork-zip-members-v1"),
        "archive_tools": ConfinedAnalysisFamilyStatus(
            "archive_tools", False, None, "analysis_tool_version_unattested"
        ),
    }
    catalog = runtime_ports._local_command_catalog(settings, family_statuses=families)

    assert catalog.get(LocalCommandId.ZIP_MEMBERS).available is True  # type: ignore[union-attr]
    assert catalog.get(LocalCommandId.ARCHIVE_LIST).available is False  # type: ignore[union-attr]
    assert catalog.get(LocalCommandId.ARCHIVE_EXTRACT_HASH).available is False  # type: ignore[union-attr]

    families["archive_tools"] = ConfinedAnalysisFamilyStatus("archive_tools", True, "16.02")
    catalog = runtime_ports._local_command_catalog(settings, family_statuses=families)
    assert catalog.get(LocalCommandId.ARCHIVE_LIST).available is True  # type: ignore[union-attr]
    assert catalog.get(LocalCommandId.ARCHIVE_EXTRACT_HASH).available is True  # type: ignore[union-attr]


def test_configured_extraction_ports_require_worker_attestations() -> None:
    families = {
        "floss": ConfinedAnalysisFamilyStatus("floss", True, "3.1.1"),
        "mwcp": ConfinedAnalysisFamilyStatus("mwcp", True, "3.16.1"),
        "mwcp_parser:test.parser": ConfinedAnalysisFamilyStatus(
            "mwcp_parser:test.parser", True, "1.0"
        ),
    }
    attestation = ConfinedAnalysisWorkerAttestation(
        reachable=True,
        confined=True,
        isolation_profile="bookworm-internal-v1",
        versions={"floss": "3.1.1", "mwcp": "3.16.1"},
        families=families,
        dependencies_ready=True,
    )
    worker = SimpleNamespace(
        available=True,
        available_reason=None,
        family_statuses=families,
        attestation=attestation,
    )
    settings = Settings.model_construct(
        analysis_worker_enabled=True,
        local_analysis_enabled=False,
        mwcp_parser_versions={"test.parser": "1.0"},
    )
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(settings),  # type: ignore[arg-type]
        confined_analysis_execution=worker,
    )
    ports = runtime.ports.optional_tool_ports
    inventory = describe_tool_inventory(runtime)
    inventory_families = cast(dict[str, dict[str, object]], inventory["families"])
    worker_details = cast(dict[str, object], inventory["analysis_worker"])
    worker_families = cast(dict[str, dict[str, object]], worker_details["families"])

    assert ports.floss_extraction is not None and ports.floss_extraction.available
    assert ports.configuration_extraction is not None
    assert ports.configuration_extraction.available
    assert inventory_families["floss_extraction"]["execution_mode"] == "analysis-worker"
    assert inventory_families["floss_extraction"]["version"] == "3.1.1"
    assert worker_families["mwcp_parser:test.parser"] == {
        "available": True,
        "version": "1.0",
        "available_reason": None,
    }


def test_local_command_confinement_does_not_enable_floss_or_mwcp_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_ports,
        "LocalCommandRunner",
        lambda **_kwargs: SimpleNamespace(filesystem_confined=True, namespace_reason=None),
    )
    settings = Settings.model_construct(
        local_analysis_enabled=True,
        analysis_worker_enabled=False,
        mwcp_parser_versions={"test.parser": "1.0"},
    )
    runtime = build_investigation_runtime_composition_factory(
        **_runtime_inputs(settings)  # type: ignore[arg-type]
    )
    ports = runtime.ports.optional_tool_ports
    families = _status_map(runtime)

    assert ports.local_commands is not None
    assert ports.floss_extraction is None
    assert ports.configuration_extraction is None
    assert families["floss_extraction"]["reason"] == ("analysis_worker_disabled_by_configuration")
    assert families["configuration_extraction"]["reason"] == (
        "analysis_worker_disabled_by_configuration"
    )


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

    present_status = _status_map(present)["network_capture"]
    assert present_status["status"] == "available"
    assert present_status["available_reason"] is None
    assert present_status["execution_mode"] == "local-bwrap-measured"
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
    assert set(composition.available_tools).intersection(
        {
            "network.protocol.document",
            "network.protocol.render",
            "network.protocol.compare",
            "network.protocol.hypothesize",
            "network.protocol.verify",
        }
    ) == {"network.protocol.document", "network.protocol.render"}
    assert not any(
        name.startswith("network.") and not name.startswith("network.protocol.")
        for name in composition.available_tools
    )


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
        "virustotal_api_key": SecretStr("test-vt-key"),
        "virustotal_base_url": "https://www.virustotal.com/api/v3",
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
            "https://www.virustotal.com/api/v3/intelligence/retrohunt_jobs/job-1?x=1"
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
