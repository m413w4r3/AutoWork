"""Single assembly of the investigation tool ports shared by the API and the worker.

Both composition roots delegate here so a family can never be wired in one process
and silently missing in the other. A port is only constructed when its settings
opt-in is on; whether its executor actually exists is decided later by the
adapters themselves (``unavailable_reason``) and by the tool registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import cast
from uuid import UUID

import httpx

from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.goodware import GoodwareMeasurementService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_composition import (
    InvestigationCompositionFactory,
    OptionalInvestigationToolPorts,
    QuarantineSampleBytesAdapter,
)
from cti_app.application.investigation_rule_tools import QuarantineRuleCorpusBytes
from cti_app.application.local_analysis import (
    GhidraAnalysisPort,
    LocalCommandCatalog,
    LocalCommandService,
    default_local_command_specs,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.native_analysis import UnavailableNativeAnalysisPort
from cti_app.application.network_workbench import QuarantinedCaptureSource
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.sample_similarity import GoodwarePrevalencePort
from cti_app.application.shodan import ShodanPort
from cti_app.application.static_analysis import StaticAnalysisService
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalPort,
    VirusTotalRetrohuntPort,
    VirusTotalRouteStep,
    VirusTotalRouteUnavailableError,
    VirusTotalRoutingPolicy,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.config import Settings
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.floss_adapter import FlossAdapter
from cti_app.infrastructure.local_command_runner import LocalCommandRunner
from cti_app.infrastructure.mwcp_adapter import MwcpAdapter
from cti_app.infrastructure.rea_adapter import (
    ReaReadOnlyRunner,
    build_native_analysis_port,
)
from cti_app.infrastructure.smda import SmdaAdapter
from cti_app.infrastructure.tshark_adapter import TsharkAdapter
from cti_app.infrastructure.virustotal import configure_virustotal_proxy_http_client
from cti_app.infrastructure.virustotal_retrohunt_client import VirusTotalRetrohuntHttpClient
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner


class RuntimePortState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RuntimePortStatus:
    """Construction-time state of one port family, with an operator-readable reason."""

    family: str
    state: RuntimePortState
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class InvestigationRuntimePorts:
    optional_tool_ports: OptionalInvestigationToolPorts
    statuses: tuple[RuntimePortStatus, ...]


@dataclass(frozen=True, slots=True)
class InvestigationRuntimeComposition:
    """The production investigation factory and its operator-facing port states."""

    composition_factory: InvestigationCompositionFactory
    ports: InvestigationRuntimePorts


def _unavailable_binary(_name: str) -> None:
    return None


def _local_command_catalog(
    settings: Settings,
    *,
    filesystem_confined: bool,
) -> LocalCommandCatalog:
    """Hide executable commands when strict filesystem isolation is unproven."""
    specs = (
        tuple(
            replace(spec, requires_filesystem_confinement=True)
            for spec in default_local_command_specs()
        )
        if settings.local_analysis_require_filesystem_confinement
        else None
    )
    if settings.local_analysis_require_filesystem_confinement and not filesystem_confined:
        # The tool schema is built from this catalogue. Returning no executables
        # removes local.run_command from the registry instead of exposing a tool
        # that only fails after an operator or Bridge asks to execute it.
        return LocalCommandCatalog(
            specs=specs,
            which=_unavailable_binary,
            timeout_seconds=settings.local_analysis_timeout_seconds,
            max_output_bytes=settings.local_analysis_max_output_bytes,
            cpu_limit_seconds=settings.local_analysis_cpu_limit_seconds,
            memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
            file_size_limit_bytes=settings.local_analysis_file_size_limit_bytes,
        )
    return LocalCommandCatalog(
        specs=specs,
        timeout_seconds=settings.local_analysis_timeout_seconds,
        max_output_bytes=settings.local_analysis_max_output_bytes,
        cpu_limit_seconds=settings.local_analysis_cpu_limit_seconds,
        memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
        file_size_limit_bytes=settings.local_analysis_file_size_limit_bytes,
    )


def _allow_unconfined_analysis_processes(settings: Settings) -> bool:
    """capa, SMDA, and the isolated YARA worker do not use the bwrap runner."""
    return not settings.local_analysis_require_filesystem_confinement


def build_investigation_runtime_ports(
    settings: Settings,
    *,
    uow_factory: UnitOfWorkFactory,
    blob_store: BlobStore,
    blob_catalog: BlobCatalogService,
    vt_adapter: VirusTotalPort | None,
    vt_capabilities: VirusTotalCapabilities,
    vt_observations: VirusTotalObservationService | None,
    shodan_adapter: ShodanPort | None,
    ghidra_adapter: GhidraAnalysisPort | None,
    retrohunt_provider: VirusTotalRetrohuntPort | None = None,
    vt_routing_policy: VirusTotalRoutingPolicy | None = None,
    network_quarantine: QuarantinedCaptureSource | None = None,
    network_quarantine_factory: Callable[[UUID], QuarantinedCaptureSource] | None = None,
    tshark: TsharkAdapter | None = None,
    retrohunt_http_client: httpx.AsyncClient | None = None,
    confined_rea_runner: ReaReadOnlyRunner | None = None,
) -> InvestigationRuntimePorts:
    statuses: list[RuntimePortStatus] = []

    retrohunt_enabled = bool(settings.virustotal_retrohunt_enabled)
    retrohunt_available: bool
    retrohunt_reason: str | None
    if not retrohunt_enabled:
        retrohunt_provider = None
        vt_capabilities = replace(vt_capabilities, retrohunt=False)
        vt_routing_policy = VirusTotalRoutingPolicy()
    else:
        route_configuration_available = bool(
            settings.virustotal_proxy_url
            and settings.virustotal_api_key is not None
            and settings.virustotal_base_url
        )
        if not route_configuration_available:
            retrohunt_provider = None
            vt_routing_policy = VirusTotalRoutingPolicy()
        elif vt_routing_policy is None:
            vt_routing_policy = _configured_retrohunt_routing_policy(settings)
        route = vt_routing_policy.route_for(VirusTotalCapability.RETROHUNT)
        if (
            retrohunt_provider is None
            and retrohunt_http_client is not None
            and vt_capabilities.retrohunt
            and route is not None
        ):
            try:
                configure_virustotal_proxy_http_client(retrohunt_http_client, settings)
                retrohunt_provider = VirusTotalRetrohuntHttpClient(
                    client=retrohunt_http_client,
                    base_url=settings.virustotal_base_url,
                    api_key=settings.virustotal_api_key,
                    proxy_url=settings.virustotal_proxy_url,
                    capabilities=vt_capabilities,
                    routing_policy=vt_routing_policy,
                    max_response_bytes=settings.virustotal_max_response_bytes,
                    max_result_pages=settings.virustotal_retrohunt_max_result_pages,
                    max_results=settings.virustotal_retrohunt_max_results,
                )
            except (ValueError, VirusTotalRouteUnavailableError):
                # Invalid local VirusTotal configuration stays unavailable and does not
                # make API/worker startup fail or guess a different endpoint.
                retrohunt_provider = None

    def record(family: str, wired: bool, reason: str | None = None) -> None:
        statuses.append(
            RuntimePortStatus(
                family,
                RuntimePortState.AVAILABLE if wired else RuntimePortState.UNAVAILABLE,
                None if wired else reason,
            )
        )

    quarantine_bytes: QuarantineRuleCorpusBytes | None = None
    local_command_catalog: LocalCommandCatalog | None = None
    local_command_service: LocalCommandService | None = None
    static_analysis: StaticAnalysisService | None = None
    capabilities_analysis: CapabilitiesService | None = None
    code_features: CodeFeatureService | None = None
    goodware_measurement: GoodwareMeasurementService | None = None
    yara_engine: IsolatedYaraRunner | None = None
    yara_probe_reason: str | None = None
    local_command_runner: LocalCommandRunner | None = None
    floss: FlossAdapter | None = None
    mwcp: MwcpAdapter | None = None
    behavior_report_importer: BehaviorReportImporter | None = BehaviorReportImporter()

    if (
        settings.local_analysis_enabled
        or settings.ghidra_enabled
        or settings.rule_workbench_enabled
    ):
        quarantine_bytes = QuarantineRuleCorpusBytes(
            uow_factory,
            blob_store,
            maximum_sample_bytes=max(
                settings.local_analysis_max_sample_bytes,
                64 * 1024 * 1024 if settings.rule_workbench_enabled else 1,
            ),
        )
    if settings.local_analysis_enabled or settings.ghidra_enabled:
        assert quarantine_bytes is not None
        local_command_runner = LocalCommandRunner(
            max_sample_bytes=settings.local_analysis_max_sample_bytes,
            require_filesystem_confinement=(settings.local_analysis_require_filesystem_confinement),
        )
        local_command_catalog = _local_command_catalog(
            settings,
            filesystem_confined=bool(getattr(local_command_runner, "filesystem_confined", False)),
        )
        local_command_service = LocalCommandService(
            QuarantineSampleBytesAdapter(
                quarantine_bytes,
                max_sample_bytes=settings.local_analysis_max_sample_bytes,
            ),
            local_command_runner,
            local_command_catalog,
            enabled=settings.local_analysis_enabled,
            max_sample_bytes=settings.local_analysis_max_sample_bytes,
        )
    if settings.local_analysis_enabled:
        assert local_command_service is not None and local_command_catalog is not None
        static_analysis = StaticAnalysisService(
            blob_catalog,
            uow_factory,
            max_sample_bytes=settings.analysis_max_sample_bytes,
            string_min_length=settings.analysis_string_min_length,
            max_strings=settings.analysis_max_strings,
        )
        if _allow_unconfined_analysis_processes(settings):
            capabilities_analysis = CapabilitiesService(
                blob_catalog,
                uow_factory,
                rules_path=settings.analysis_capa_rules_path,
                timeout_seconds=settings.analysis_capa_timeout_seconds,
                max_output_bytes=settings.analysis_capa_max_output_bytes,
                max_memory_bytes=settings.analysis_capa_max_memory_bytes,
            )
            code_features = CodeFeatureService(blob_catalog, uow_factory, SmdaAdapter())
        goodware_measurement = GoodwareMeasurementService(
            blob_store,
            uow_factory,
            cache_root=settings.goodware_cache_root,
        )
        # Both adapters re-evaluate their own confinement/binary/allowlist state on
        # every call, so a degraded executor never presents itself as available.
        floss = FlossAdapter(local_command_service, local_command_catalog, enabled=True)
        mwcp = MwcpAdapter(
            local_command_service,
            local_command_catalog,
            parser_versions=settings.mwcp_parser_versions,
            enabled=True,
        )
    if settings.rule_workbench_enabled and _allow_unconfined_analysis_processes(settings):
        candidate_yara_engine = IsolatedYaraRunner()
        if candidate_yara_engine.available:
            yara_engine = candidate_yara_engine
        else:
            yara_probe_reason = candidate_yara_engine.unavailable_reason or "yara_probe_failed"

    network_source_configured = (
        network_quarantine is not None or network_quarantine_factory is not None
    )
    if network_source_configured and tshark is None:
        tshark = TsharkAdapter()

    acquisition_service = (
        VirusTotalSampleAcquisitionService(
            vt_adapter,
            vt_capabilities,
            blob_catalog,
            uow_factory,
            LocalIdentityProvider("system:investigation"),
        )
        if vt_adapter is not None
        else None
    )

    floss_available = bool(floss is not None and floss.available)
    floss_reason = (
        getattr(floss, "unavailable_reason", None)
        if floss is not None
        else ("local_analysis_disabled")
    )
    mwcp_available = bool(mwcp is not None and mwcp.available)
    mwcp_reason = (
        getattr(mwcp, "unavailable_reason", None) if mwcp is not None else "local_analysis_disabled"
    )
    network_available = bool(network_source_configured and tshark is not None and tshark.available)
    network_reason = (
        None
        if network_available
        else "no_operator_capture_source"
        if not network_source_configured
        else "tshark_or_filesystem_confinement_unavailable"
    )
    if not retrohunt_enabled:
        retrohunt_available, retrohunt_reason = False, "retrohunt_disabled_by_configuration"
    else:
        retrohunt_available, retrohunt_reason = _retrohunt_availability(
            vt_capabilities=vt_capabilities,
            provider=retrohunt_provider,
            routing_policy=vt_routing_policy,
        )
    native_port = build_native_analysis_port(settings, runner=confined_rea_runner)

    vt_enabled = any(
        (
            vt_capabilities.file_report,
            vt_capabilities.file_relationships,
            vt_capabilities.intelligence_search,
            vt_capabilities.file_download,
            vt_capabilities.behaviour_reports,
            vt_capabilities.behaviour_pcap,
        )
    )
    record(
        "virustotal",
        vt_adapter is not None and vt_enabled,
        "virustotal_not_configured" if vt_adapter is None else "virustotal_capabilities_disabled",
    )
    record("shodan", shodan_adapter is not None, "shodan_not_configured")
    record("ghidra", ghidra_adapter is not None, "ghidra_disabled")
    record(
        "local_analysis",
        bool(settings.local_analysis_enabled and static_analysis is not None),
        "local_analysis_disabled"
        if not settings.local_analysis_enabled
        else "sample_analysis_services_unavailable",
    )
    if not settings.local_analysis_enabled:
        local_command_reason = "local_analysis_disabled"
    elif (
        settings.local_analysis_require_filesystem_confinement
        and local_command_service is not None
        and not local_command_service.filesystem_confined
    ):
        local_command_reason = (
            getattr(local_command_runner, "namespace_reason", None)
            or "filesystem_confinement_unavailable"
        )
    else:
        local_command_reason = "no_local_command_available"
    record(
        "local_command_execution",
        bool(
            settings.local_analysis_enabled
            and local_command_service is not None
            and local_command_catalog is not None
            and (
                not settings.local_analysis_require_filesystem_confinement
                or local_command_service.filesystem_confined
            )
            and local_command_catalog.available_commands()
        ),
        local_command_reason,
    )
    record(
        "rule_workbench",
        yara_engine is not None,
        "rule_workbench_disabled"
        if not settings.rule_workbench_enabled
        else "filesystem_confinement_unavailable"
        if not _allow_unconfined_analysis_processes(settings)
        else yara_probe_reason or "yara_engine_or_worker_unavailable",
    )
    if settings.local_analysis_require_filesystem_confinement:
        record(
            "capa_analysis",
            capabilities_analysis is not None,
            None if capabilities_analysis is not None else "filesystem_confinement_unavailable",
        )
        record(
            "smda_analysis",
            code_features is not None,
            None if code_features is not None else "filesystem_confinement_unavailable",
        )
    record(
        "sample_similarity",
        blob_catalog is not None,
        "similarity_artifact_store_not_configured",
    )
    record("floss_extraction", floss_available, floss_reason)
    record("configuration_extraction", mwcp_available, mwcp_reason)
    behavior_report_import_available = callable(getattr(behavior_report_importer, "parse", None))
    record(
        "behavior_report_import",
        behavior_report_import_available,
        None if behavior_report_import_available else "behavior_report_importer_unavailable",
    )
    record("retrohunt", retrohunt_available, retrohunt_reason)
    record("network_capture", network_available, network_reason)
    record(
        "native_analysis",
        native_port.available,
        _native_port_unavailable_reason(native_port),
    )

    ports = OptionalInvestigationToolPorts(
        virustotal=vt_adapter,
        shodan=shodan_adapter,
        ghidra=ghidra_adapter,
        acquisition=acquisition_service,
        virustotal_observations=vt_observations,
        vt_capabilities=vt_capabilities,
        rule_corpus_bytes=quarantine_bytes,
        yara_engine=yara_engine,
        local_commands=local_command_service,
        local_command_catalog=local_command_catalog,
        sample_static_analysis=static_analysis,
        sample_capabilities_analysis=capabilities_analysis,
        sample_code_features=code_features,
        sample_goodware=goodware_measurement,
        similarity_artifacts=blob_catalog,
        goodware_prevalence=cast(GoodwarePrevalencePort | None, goodware_measurement),
        floss_extraction=floss,
        configuration_extraction=mwcp,
        behavior_report_importer=(
            behavior_report_importer if behavior_report_import_available else None
        ),
        retrohunt_provider=retrohunt_provider,
        vt_routing_policy=vt_routing_policy,
        network_quarantine=network_quarantine,
        network_quarantine_factory=network_quarantine_factory,
        tshark=tshark,
        confined_rea_runner=confined_rea_runner,
    )
    return InvestigationRuntimePorts(optional_tool_ports=ports, statuses=tuple(statuses))


def build_investigation_runtime_composition_factory(
    settings: Settings,
    *,
    uow_factory: UnitOfWorkFactory,
    blob_store: BlobStore,
    blob_catalog: BlobCatalogService,
    artifact_store: ProductionArtifactStore,
    model_conversations: ModelConversationService,
    vt_adapter: VirusTotalPort | None,
    vt_capabilities: VirusTotalCapabilities,
    vt_observations: VirusTotalObservationService | None,
    shodan_adapter: ShodanPort | None,
    ghidra_adapter: GhidraAnalysisPort | None,
    retrohunt_provider: VirusTotalRetrohuntPort | None = None,
    vt_routing_policy: VirusTotalRoutingPolicy | None = None,
    network_quarantine: QuarantinedCaptureSource | None = None,
    network_quarantine_factory: Callable[[UUID], QuarantinedCaptureSource] | None = None,
    tshark: TsharkAdapter | None = None,
    retrohunt_http_client: httpx.AsyncClient | None = None,
    confined_rea_runner: ReaReadOnlyRunner | None = None,
) -> InvestigationRuntimeComposition:
    """Build the same production composition used by both API and worker roots."""
    ports = build_investigation_runtime_ports(
        settings,
        uow_factory=uow_factory,
        blob_store=blob_store,
        blob_catalog=blob_catalog,
        vt_adapter=vt_adapter,
        vt_capabilities=vt_capabilities,
        vt_observations=vt_observations,
        shodan_adapter=shodan_adapter,
        ghidra_adapter=ghidra_adapter,
        retrohunt_provider=retrohunt_provider,
        vt_routing_policy=vt_routing_policy,
        network_quarantine=network_quarantine,
        network_quarantine_factory=network_quarantine_factory,
        tshark=tshark,
        retrohunt_http_client=retrohunt_http_client,
        confined_rea_runner=confined_rea_runner,
    )
    return InvestigationRuntimeComposition(
        composition_factory=InvestigationCompositionFactory(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            model_conversations=model_conversations,
            settings=settings,
            optional_tool_ports=ports.optional_tool_ports,
        ),
        ports=ports,
    )


def describe_tool_inventory(
    composition: InvestigationRuntimeComposition | InvestigationRuntimePorts,
) -> dict[str, object]:
    """Return a safe, JSON-serializable runtime inventory without sample data or secrets."""
    ports = (
        composition.ports
        if isinstance(composition, InvestigationRuntimeComposition)
        else composition
    )
    binaries: dict[str, object] = {}
    command_catalog = ports.optional_tool_ports.local_command_catalog
    if command_catalog is not None:
        for capability in command_catalog.all_commands():
            binaries[capability.spec.id.value] = {
                "available": capability.available,
                "version": capability.version if capability.available else None,
            }
    tshark = ports.optional_tool_ports.tshark
    if tshark is not None:
        tshark_version, capinfos_version = tshark.versions()
        for name, version in (("tshark", tshark_version), ("capinfos", capinfos_version)):
            binaries[name] = {"available": version is not None, "version": version}

    return {
        "families": {
            item.family: {
                "status": item.state.value,
                "reason": item.reason,
            }
            for item in ports.statuses
        },
        "binaries": binaries,
    }


def _retrohunt_availability(
    *,
    vt_capabilities: VirusTotalCapabilities,
    provider: VirusTotalRetrohuntPort | None,
    routing_policy: VirusTotalRoutingPolicy | None,
) -> tuple[bool, str | None]:
    if not vt_capabilities.retrohunt:
        return False, "retrohunt_capability_disabled"
    route = (
        routing_policy.route_for(VirusTotalCapability.RETROHUNT)
        if routing_policy is not None
        else None
    )
    if route is None:
        return False, "retrohunt_route_not_configured"
    if any(step.transport is not VirusTotalTransportKind.PROXY for step in route.steps()):
        return False, "retrohunt_proxy_route_required"
    if any(
        step.variant
        not in {
            VirusTotalEndpointVariant.V3,
            VirusTotalEndpointVariant.V3_FALLBACK,
        }
        for step in route.steps()
    ):
        return False, "retrohunt_proxy_route_variant_unsupported"
    if provider is None:
        return False, "retrohunt_provider_not_configured"
    if not provider.available:
        reason = getattr(provider, "available_reason", None)
        return False, reason or "retrohunt_provider_unavailable"
    return True, None


def _configured_retrohunt_routing_policy(settings: Settings) -> VirusTotalRoutingPolicy:
    if not settings.virustotal_proxy_url or settings.virustotal_api_key is None:
        return VirusTotalRoutingPolicy()
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    return VirusTotalRoutingPolicy(routes={VirusTotalCapability.RETROHUNT: route})


def _native_port_unavailable_reason(port: object) -> str | None:
    if getattr(port, "available", False):
        return None
    if isinstance(port, UnavailableNativeAnalysisPort):
        error = port.unavailable_error()
        return error.code if error.code != "native_analysis_unavailable" else str(error)
    provider = getattr(port, "provider", None)
    provider_name = getattr(provider, "value", "unknown")
    return f"provider_unavailable:{provider_name}"
