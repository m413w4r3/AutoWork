"""Single assembly of the investigation tool ports shared by the API and the worker.

Both composition roots delegate here so a family can never be wired in one process
and silently missing in the other. A port is only constructed when its settings
opt-in is on; whether its executor actually exists is decided later by the
adapters themselves (``unavailable_reason``) and by the tool registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, cast
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ModelConversationTurnTransport
from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.confined_analysis import (
    ConfinedAnalysisExecutionPort,
    ConfinedAnalysisFamilyStatus,
)
from cti_app.application.external_effects import ExternalEffectCoordinator
from cti_app.application.goodware import GoodwareMeasurementService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_advisor_tools import ADVISOR_SCHEMA_VERSION
from cti_app.application.investigation_composition import (
    InvestigationCompositionFactory,
    OptionalInvestigationToolPorts,
)
from cti_app.application.investigation_rule_tools import QuarantineRuleCorpusBytes
from cti_app.application.local_analysis import (
    GhidraAnalysisPort,
    LocalCommandCatalog,
    LocalCommandService,
    default_local_command_specs,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    BRIDGE_SOURCE_PROVENANCE_KEY,
    BridgeExternalEffectBinding,
)
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
from cti_app.config import VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST, Settings
from cti_app.domain.external_effects import EvidenceReference, ExternalEffectAuthority
from cti_app.domain.network_protocol import NetworkExtractionLimitsV1
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.confined_analysis_dispatch import (
    build_confined_analysis_execution_port,
)
from cti_app.infrastructure.confined_local_commands import ConfinedLocalCommands
from cti_app.infrastructure.confined_tshark import ConfinedTsharkAdapter
from cti_app.infrastructure.database.repositories.external_effects import (
    BRIDGE_RESOLVER_FAMILY_KEY,
    BRIDGE_RESOLVER_RUN_HASH_KEY,
    BRIDGE_RESOLVER_TURN_HASH_KEY,
    ExternalEffectFamilyBinding,
    SqlAlchemyExternalEffectClaimStore,
)
from cti_app.infrastructure.database.repositories.virustotal_external_effects import (
    VT_EXTERNAL_EFFECT_FAMILIES,
    SqlAlchemyVirusTotalPolicyGuard,
    VirusTotalReadReusePolicy,
    virustotal_external_effect_family_bindings,
)
from cti_app.infrastructure.floss_adapter import FlossAdapter
from cti_app.infrastructure.ghidra_mcp import GhidraMcpHttpAdapter
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
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner, YaraRunnerLimits


class RuntimePortState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RuntimePortStatus:
    """Construction-time state of one port family, with an operator-readable reason."""

    family: str
    state: RuntimePortState
    reason: str | None = None
    execution_mode: str | None = None

    @property
    def available_reason(self) -> str | None:
        return self.reason


@dataclass(frozen=True, slots=True)
class InvestigationRuntimePorts:
    optional_tool_ports: OptionalInvestigationToolPorts
    statuses: tuple[RuntimePortStatus, ...]
    external_effect_coordinator: ExternalEffectCoordinator | None = None


@dataclass(frozen=True, slots=True)
class InvestigationRuntimeComposition:
    """The production investigation factory and its operator-facing port states."""

    composition_factory: InvestigationCompositionFactory
    ports: InvestigationRuntimePorts


_BRIDGE_PROVIDER_SCOPE = "bridge.responses"
_BRIDGE_TURN_FAMILY = "bridge.investigation_turn.v1"
_ADVISOR_CONSULTATION_FAMILY = "advisor.consultation.v1"


def _bridge_source_reference_resolver(
    family: str,
) -> Callable[[Mapping[str, Any]], tuple[EvidenceReference, ...]]:
    def resolve(parameters: Mapping[str, Any]) -> tuple[EvidenceReference, ...]:
        provenance = parameters.get(BRIDGE_SOURCE_PROVENANCE_KEY)
        raw_refs = provenance.get("evidence_refs") if isinstance(provenance, Mapping) else None
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("family") != family
            or provenance.get("authorized_input_hash")
            != parameters.get(BRIDGE_RESOLVER_RUN_HASH_KEY)
            or provenance.get("turn_input_sha256") != parameters.get(BRIDGE_RESOLVER_TURN_HASH_KEY)
            or parameters.get(BRIDGE_RESOLVER_FAMILY_KEY) != family
            or not isinstance(raw_refs, (list, tuple))
            or not raw_refs
        ):
            return ()
        references: list[EvidenceReference] = []
        for item in raw_refs:
            if not isinstance(item, Mapping) or set(item) != {
                "kind",
                "id",
                "version",
                "sha256",
            }:
                return ()
            try:
                references.append(
                    EvidenceReference(
                        kind=item["kind"],
                        id=item["id"],
                        version=item["version"],
                        sha256=item["sha256"],
                    )
                )
            except (TypeError, ValueError):
                return ()
        return tuple(references)

    return resolve


def create_external_effect_coordinator(
    session_factory: async_sessionmaker[AsyncSession],
) -> ExternalEffectCoordinator:
    """Create the one runtime coordinator shared by Bridge and VT transports."""
    family_bindings = virustotal_external_effect_family_bindings()
    family_bindings.update(
        {
            _BRIDGE_TURN_FAMILY: ExternalEffectFamilyBinding(
                provider="bridge",
                provider_scopes=frozenset({_BRIDGE_PROVIDER_SCOPE}),
                normalizer_versions=frozenset({"bridge.turn.v1"}),
                invocation_tool_binding=None,
                model_submission=True,
                requires_model_lineage=True,
                model_run_provider="openai",
                requires_source_refs=True,
                source_reference_resolver=_bridge_source_reference_resolver(_BRIDGE_TURN_FAMILY),
            ),
            _ADVISOR_CONSULTATION_FAMILY: ExternalEffectFamilyBinding(
                provider="bridge",
                provider_scopes=frozenset({_BRIDGE_PROVIDER_SCOPE}),
                normalizer_versions=frozenset({_ADVISOR_CONSULTATION_FAMILY}),
                invocation_tool_binding=("advisor.consult", ADVISOR_SCHEMA_VERSION),
                model_submission=True,
                requires_model_lineage=True,
                model_run_provider="openai",
                requires_source_refs=True,
                source_reference_resolver=_bridge_source_reference_resolver(
                    _ADVISOR_CONSULTATION_FAMILY
                ),
            ),
        }
    )
    reuse_policy = VirusTotalReadReusePolicy()
    return ExternalEffectCoordinator(
        SqlAlchemyExternalEffectClaimStore(
            session_factory,
            policy_guard=SqlAlchemyVirusTotalPolicyGuard(),
            family_bindings=family_bindings,
        ),
        reuse_policies={
            family: reuse_policy
            for family in VT_EXTERNAL_EFFECT_FAMILIES
            if family != "vt.download_sample"
        },
    )


async def _build_bridge_external_effect_binding(
    **attempt: object,
) -> BridgeExternalEffectBinding | None:
    authority = attempt.get("authority_context")
    family = attempt.get("family")
    if not isinstance(authority, ExternalEffectAuthority) or not isinstance(family, str):
        return None
    normalizer_version = {
        _BRIDGE_TURN_FAMILY: "bridge.turn.v1",
        _ADVISOR_CONSULTATION_FAMILY: _ADVISOR_CONSULTATION_FAMILY,
    }.get(family)
    if normalizer_version is None:
        return None
    raw_evidence_refs = attempt.get("evidence_refs", ())
    if not isinstance(raw_evidence_refs, tuple) or any(
        not isinstance(reference, EvidenceReference) for reference in raw_evidence_refs
    ):
        return None
    return BridgeExternalEffectBinding(
        authority=authority,
        family=family,
        provider_scope=_BRIDGE_PROVIDER_SCOPE,
        normalizer_version=normalizer_version,
        evidence_refs=raw_evidence_refs,
    )


def _unavailable_binary(_name: str) -> None:
    return None


def _local_command_catalog(
    settings: Settings,
    *,
    family_statuses: Mapping[str, ConfinedAnalysisFamilyStatus],
) -> LocalCommandCatalog:
    """Expose only commands attested in the confined analysis worker."""
    specs = tuple(
        replace(spec, requires_filesystem_confinement=True)
        for spec in default_local_command_specs()
    )
    local_commands = family_statuses.get("local_command")
    yara = family_statuses.get("yara")

    def worker_binary(name: str) -> str | None:
        if name == "yara":
            return "/usr/bin/yara" if yara is not None and yara.available else None
        if local_commands is not None and local_commands.available:
            return f"/usr/bin/{name}"
        return None

    return LocalCommandCatalog(
        specs=specs,
        which=worker_binary,
        timeout_seconds=settings.local_analysis_timeout_seconds,
        max_output_bytes=settings.local_analysis_max_output_bytes,
        cpu_limit_seconds=settings.local_analysis_cpu_limit_seconds,
        memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
        file_size_limit_bytes=settings.local_analysis_file_size_limit_bytes,
    )


def _allow_unconfined_analysis_processes(settings: Settings) -> bool:
    """Permit the legacy local YARA runner only after measuring bwrap confinement."""
    probe = LocalCommandRunner(
        max_sample_bytes=settings.local_analysis_max_sample_bytes,
        require_filesystem_confinement=True,
    )
    return bool(probe.filesystem_confined)


def build_investigation_runtime_ports(
    settings: Settings,
    *,
    uow_factory: UnitOfWorkFactory,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    external_effect_coordinator: ExternalEffectCoordinator | None = None,
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
    confined_analysis_execution: ConfinedAnalysisExecutionPort | None = None,
) -> InvestigationRuntimePorts:
    statuses: list[RuntimePortStatus] = []
    coordinator = external_effect_coordinator
    if coordinator is None and session_factory is not None:
        coordinator = create_external_effect_coordinator(session_factory)

    configure_vt_effects = getattr(vt_adapter, "configure_external_effects", None)
    vt_effects_ready = bool(
        session_factory is not None
        and vt_adapter is not None
        and vt_observations is not None
        and callable(configure_vt_effects)
    )
    if (
        coordinator is not None
        and session_factory is not None
        and vt_adapter is not None
        and vt_observations is not None
        and callable(configure_vt_effects)
    ):
        configure_vt_effects(coordinator, vt_observations)

    vt_route_status = {
        capability: _vt_proxy_route_status(settings, vt_adapter, capability)
        for capability in (
            VirusTotalCapability.FILE_REPORT,
            VirusTotalCapability.FILE_RELATIONSHIPS,
            VirusTotalCapability.INTELLIGENCE_SEARCH,
            VirusTotalCapability.FILE_DOWNLOAD,
            VirusTotalCapability.BEHAVIOUR_REPORTS,
            VirusTotalCapability.BEHAVIOUR_PCAP,
        )
    }
    download_route_ready, download_route_reason = vt_route_status[
        VirusTotalCapability.FILE_DOWNLOAD
    ]
    pcap_route_ready, pcap_route_reason = vt_route_status[VirusTotalCapability.BEHAVIOUR_PCAP]
    download_hosts = {
        host.strip().lower()
        for host in settings.virustotal_download_allowed_hosts.split(",")
        if host.strip()
    }
    download_ports = {
        port.strip()
        for port in settings.virustotal_download_allowed_ports.split(",")
        if port.strip()
    }
    download_storage_ready = bool(blob_store is not None and blob_catalog is not None)
    acquisition_budget_ready = settings.investigation_max_hits_acquired > 0
    effective_download = bool(
        vt_capabilities.file_download
        and settings.virustotal_file_download_enabled
        and vt_adapter is not None
        and download_route_ready
        and VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST in download_hosts
        and download_ports
        and download_storage_ready
        and acquisition_budget_ready
    )
    effective_pcap = bool(
        settings.virustotal_behaviour_pcap_enabled
        and vt_adapter is not None
        and vt_effects_ready
        and pcap_route_ready
    )
    vt_capabilities = replace(
        vt_capabilities,
        file_report=(
            vt_capabilities.file_report
            and settings.virustotal_file_report_enabled
            and vt_effects_ready
            and vt_route_status[VirusTotalCapability.FILE_REPORT][0]
        ),
        file_relationships=(
            vt_capabilities.file_relationships
            and settings.virustotal_object_relationships_enabled
            and vt_effects_ready
            and vt_route_status[VirusTotalCapability.FILE_RELATIONSHIPS][0]
        ),
        intelligence_search=(
            vt_capabilities.intelligence_search
            and settings.virustotal_intelligence_search_enabled
            and vt_effects_ready
            and vt_route_status[VirusTotalCapability.INTELLIGENCE_SEARCH][0]
        ),
        file_download=effective_download,
        behaviour_reports=(
            vt_capabilities.behaviour_reports
            and settings.virustotal_behaviour_reports_enabled
            and vt_effects_ready
            and vt_route_status[VirusTotalCapability.BEHAVIOUR_REPORTS][0]
        ),
        behaviour_pcap=effective_pcap,
    )

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

    def record(
        family: str,
        wired: bool,
        reason: str | None = None,
        *,
        execution_mode: str | None = None,
    ) -> None:
        statuses.append(
            RuntimePortStatus(
                family,
                RuntimePortState.AVAILABLE if wired else RuntimePortState.UNAVAILABLE,
                None if wired else reason,
                execution_mode,
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
    if confined_analysis_execution is None:
        confined_analysis_execution = build_confined_analysis_execution_port(
            settings,
            uow_factory=uow_factory,
            blob_catalog=blob_catalog,
        )
    assert confined_analysis_execution is not None
    worker_statuses = confined_analysis_execution.family_statuses
    worker_extraction_available = any(
        status is not None and status.available
        for status in (worker_statuses.get("floss"), worker_statuses.get("mwcp"))
    )

    if (
        settings.local_analysis_enabled
        or settings.ghidra_enabled
        or settings.rule_workbench_enabled
        or worker_extraction_available
    ):
        quarantine_bytes = QuarantineRuleCorpusBytes(
            uow_factory,
            blob_store,
            maximum_sample_bytes=max(
                settings.local_analysis_max_sample_bytes,
                settings.ghidra_max_sample_bytes if settings.ghidra_enabled else 1,
                64 * 1024 * 1024
                if settings.rule_workbench_enabled or worker_extraction_available
                else 1,
            ),
        )
    if settings.ghidra_enabled and isinstance(ghidra_adapter, GhidraMcpHttpAdapter):
        assert quarantine_bytes is not None
        ghidra_adapter.set_sample_bytes_loader(quarantine_bytes.get_bytes_for_sample)
    if settings.local_analysis_enabled or settings.ghidra_enabled:
        assert quarantine_bytes is not None
        local_command_runner = LocalCommandRunner(
            max_sample_bytes=settings.local_analysis_max_sample_bytes,
            require_filesystem_confinement=True,
        )
        local_command_catalog = _local_command_catalog(
            settings,
            family_statuses=worker_statuses,
        )
        local_command_service = cast(
            LocalCommandService,
            ConfinedLocalCommands(
                execution=confined_analysis_execution,
                catalog=local_command_catalog,
                uow_factory=uow_factory,
                settings=settings,
                enabled=settings.local_analysis_enabled,
                max_sample_bytes=settings.local_analysis_max_sample_bytes,
            ),
        )
    local_filesystem_confined = bool(
        local_command_runner is not None
        and getattr(local_command_runner, "filesystem_confined", False)
    )
    if settings.local_analysis_enabled and local_filesystem_confined:
        assert local_command_service is not None and local_command_catalog is not None
        static_analysis = StaticAnalysisService(
            blob_catalog,
            uow_factory,
            max_sample_bytes=settings.analysis_max_sample_bytes,
            string_min_length=settings.analysis_string_min_length,
            max_strings=settings.analysis_max_strings,
        )
        capabilities_analysis = CapabilitiesService(
            blob_catalog,
            uow_factory,
            rules_path=settings.analysis_capa_rules_path,
            timeout_seconds=settings.analysis_capa_timeout_seconds,
            max_output_bytes=settings.analysis_capa_max_output_bytes,
            max_memory_bytes=settings.analysis_capa_max_memory_bytes,
        )
        code_features = CodeFeatureService(blob_catalog, uow_factory, SmdaAdapter())
    if settings.local_analysis_enabled:
        goodware_measurement = GoodwareMeasurementService(
            blob_store,
            uow_factory,
            cache_root=settings.goodware_cache_root,
        )
    if worker_extraction_available and quarantine_bytes is not None:
        floss = FlossAdapter(
            enabled=True,
            confined_analysis=confined_analysis_execution,
            uow_factory=uow_factory,
            max_sample_bytes=settings.analysis_max_sample_bytes,
        )
        mwcp = MwcpAdapter(
            parser_versions=settings.mwcp_parser_versions,
            enabled=True,
            confined_analysis=confined_analysis_execution,
            uow_factory=uow_factory,
            max_sample_bytes=settings.analysis_max_sample_bytes,
        )
    if settings.rule_workbench_enabled and _allow_unconfined_analysis_processes(settings):
        candidate_yara_engine = IsolatedYaraRunner(
            YaraRunnerLimits(maximum_source_bytes=settings.model_output_max_bytes)
        )
        if candidate_yara_engine.available:
            yara_engine = candidate_yara_engine
        else:
            yara_probe_reason = candidate_yara_engine.unavailable_reason or "yara_probe_failed"

    network_source_configured = (
        network_quarantine is not None or network_quarantine_factory is not None
    )
    if network_source_configured and tshark is None:
        tshark = ConfinedTsharkAdapter(
            confined_analysis_execution,
            limits=NetworkExtractionLimitsV1(),
            timeout_seconds=settings.analysis_worker_job_timeout_seconds,
            max_output_bytes=settings.analysis_worker_result_max_bytes,
        )

    acquisition_service = (
        VirusTotalSampleAcquisitionService(
            vt_adapter,
            vt_capabilities,
            blob_catalog,
            uow_factory,
            LocalIdentityProvider("system:investigation"),
        )
        if vt_adapter is not None and vt_capabilities.file_download
        else None
    )

    floss_available = bool(floss is not None and floss.available)
    floss_worker_status = worker_statuses.get("floss")
    floss_reason = (
        getattr(floss, "unavailable_reason", None)
        if floss is not None
        else floss_worker_status.available_reason
        if floss_worker_status is not None
        else confined_analysis_execution.available_reason or "analysis_worker_unavailable"
    )
    mwcp_available = bool(mwcp is not None and mwcp.available)
    mwcp_worker_status = worker_statuses.get("mwcp")
    mwcp_reason = (
        getattr(mwcp, "unavailable_reason", None)
        if mwcp is not None
        else mwcp_worker_status.available_reason
        if mwcp_worker_status is not None
        else confined_analysis_execution.available_reason or "analysis_worker_unavailable"
    )
    network_available = bool(network_source_configured and tshark is not None and tshark.available)
    network_reason = (
        None
        if network_available
        else "no_operator_capture_source"
        if not network_source_configured
        else getattr(tshark, "available_reason", None)
        or (
            "tshark_or_filesystem_confinement_unavailable"
            if not isinstance(tshark, ConfinedTsharkAdapter)
            else "analysis_worker_tshark_unavailable"
        )
    )
    if not retrohunt_enabled:
        retrohunt_available, retrohunt_reason = False, "retrohunt_disabled_by_configuration"
    else:
        retrohunt_available, retrohunt_reason = _retrohunt_availability(
            vt_capabilities=vt_capabilities,
            provider=retrohunt_provider,
            routing_policy=vt_routing_policy,
        )
    native_runner = confined_rea_runner
    if settings.native_analysis_provider == "ghidra_mcp" and isinstance(
        ghidra_adapter, GhidraMcpHttpAdapter
    ):
        # The composition factory already forwards this process-level collaborator
        # through its runner dependency. Keep the same attested client and byte loader.
        native_runner = cast(ReaReadOnlyRunner, ghidra_adapter)
    native_port = build_native_analysis_port(settings, runner=native_runner)

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
    if vt_adapter is None:
        vt_reason = "virustotal_adapter_not_configured"
    elif not vt_effects_ready and any(
        (
            settings.virustotal_file_report_enabled,
            settings.virustotal_object_relationships_enabled,
            settings.virustotal_intelligence_search_enabled,
            settings.virustotal_behaviour_reports_enabled,
            settings.virustotal_behaviour_pcap_enabled,
        )
    ):
        vt_reason = "external_effect_coordinator_unavailable"
    elif vt_enabled:
        vt_reason = None
    else:
        configured_capabilities = (
            (settings.virustotal_file_report_enabled, VirusTotalCapability.FILE_REPORT),
            (
                settings.virustotal_object_relationships_enabled,
                VirusTotalCapability.FILE_RELATIONSHIPS,
            ),
            (
                settings.virustotal_intelligence_search_enabled,
                VirusTotalCapability.INTELLIGENCE_SEARCH,
            ),
            (settings.virustotal_behaviour_reports_enabled, VirusTotalCapability.BEHAVIOUR_REPORTS),
            (settings.virustotal_file_download_enabled, VirusTotalCapability.FILE_DOWNLOAD),
            (settings.virustotal_behaviour_pcap_enabled, VirusTotalCapability.BEHAVIOUR_PCAP),
        )
        vt_reason = next(
            (
                vt_route_status[capability][1]
                for enabled, capability in configured_capabilities
                if enabled and vt_route_status[capability][1] is not None
            ),
            "virustotal_capabilities_disabled",
        )
    record(
        "virustotal",
        vt_adapter is not None and vt_enabled,
        vt_reason,
    )
    vt_reads_available = any(
        (
            vt_capabilities.file_report,
            vt_capabilities.file_relationships,
            vt_capabilities.intelligence_search,
            vt_capabilities.behaviour_reports,
            vt_capabilities.behaviour_pcap,
        )
    )
    record(
        "virustotal_reads",
        vt_reads_available,
        None if vt_reads_available else vt_reason,
    )
    if not settings.virustotal_file_download_enabled:
        download_reason = "virustotal_file_download_disabled"
    elif vt_adapter is None:
        download_reason = "virustotal_adapter_not_configured"
    elif not download_hosts:
        download_reason = "virustotal_download_allowed_hosts_empty"
    elif VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST not in download_hosts:
        download_reason = "virustotal_download_cdn_host_not_allowlisted"
    elif not download_route_ready:
        download_reason = download_route_reason or "virustotal_download_route_unavailable"
    elif not download_ports:
        download_reason = "virustotal_download_allowed_ports_empty"
    elif not download_storage_ready:
        download_reason = "quarantine_storage_unavailable"
    elif not acquisition_budget_ready:
        download_reason = "acquisition_budget_disabled"
    elif not vt_capabilities.file_download:
        download_reason = "virustotal_file_download_capability_disabled"
    else:
        download_reason = None
    record(
        "virustotal_file_download",
        acquisition_service is not None,
        download_reason,
    )
    if not settings.virustotal_behaviour_pcap_enabled:
        pcap_reason = "virustotal_behaviour_pcap_disabled"
    elif vt_adapter is None:
        pcap_reason = "virustotal_adapter_not_configured"
    elif not pcap_route_ready:
        pcap_reason = pcap_route_reason or "virustotal_behaviour_pcap_route_unavailable"
    elif not vt_capabilities.behaviour_pcap:
        pcap_reason = "virustotal_behaviour_pcap_capability_disabled"
    else:
        pcap_reason = None
    record("virustotal_behaviour_pcap", vt_capabilities.behaviour_pcap, pcap_reason)
    record("shodan", shodan_adapter is not None, "shodan_not_configured")
    ghidra_available = bool(getattr(ghidra_adapter, "available", False))
    ghidra_reason = (
        None
        if ghidra_available
        else "ghidra_disabled_by_configuration"
        if not settings.ghidra_enabled
        else getattr(ghidra_adapter, "available_reason", None) or "ghidra_service_unavailable"
    )
    record("ghidra", ghidra_available, ghidra_reason)
    record(
        "local_filesystem_confinement",
        local_filesystem_confined,
        None
        if local_filesystem_confined
        else getattr(local_command_runner, "namespace_reason", None)
        or "local_analysis_disabled_or_unconfined",
        execution_mode="local-bwrap-measured",
    )
    sample_worker_status = worker_statuses.get("sample_analysis")
    sample_worker_available = bool(sample_worker_status and sample_worker_status.available)
    record(
        "confined_analysis_worker",
        confined_analysis_execution.available,
        confined_analysis_execution.available_reason or "analysis_worker_unavailable",
        execution_mode="analysis-worker",
    )
    record(
        "local_analysis",
        sample_worker_available,
        None
        if sample_worker_available
        else sample_worker_status.available_reason
        if sample_worker_status is not None
        else confined_analysis_execution.available_reason or "sample_analysis_unavailable",
        execution_mode="analysis-worker",
    )
    record(
        "sample_analysis",
        sample_worker_available,
        None
        if sample_worker_available
        else sample_worker_status.available_reason
        if sample_worker_status is not None
        else confined_analysis_execution.available_reason or "sample_analysis_unavailable",
        execution_mode="analysis-worker",
    )
    if not settings.local_analysis_enabled:
        local_command_reason = "local_analysis_disabled"
    else:
        local_command_status = worker_statuses.get("local_command")
        if not confined_analysis_execution.available:
            local_command_reason = (
                confined_analysis_execution.available_reason or "analysis_worker_unavailable"
            )
        elif local_command_status is not None and not local_command_status.available:
            local_command_reason = (
                local_command_status.available_reason or "local_command_worker_unavailable"
            )
        elif local_command_service is None or not local_command_catalog:
            local_command_reason = "no_local_command_available"
        else:
            local_command_reason = None
    record(
        "local_command_execution",
        bool(
            settings.local_analysis_enabled
            and local_command_service is not None
            and local_command_catalog is not None
            and local_command_service.filesystem_confined
            and local_command_catalog.available_commands()
        ),
        local_command_reason,
        execution_mode="analysis-worker",
    )
    yara_worker_status = worker_statuses.get("yara")
    yara_worker_available = bool(yara_worker_status and yara_worker_status.available)
    record(
        "rule_workbench",
        settings.rule_workbench_enabled and (yara_engine is not None or yara_worker_available),
        "rule_workbench_disabled"
        if not settings.rule_workbench_enabled
        else yara_worker_status.available_reason
        if not yara_worker_available and yara_worker_status is not None
        else "yara_engine_or_worker_unavailable"
        if not yara_worker_available and yara_engine is None
        else yara_probe_reason,
        execution_mode=(
            "analysis-worker"
            if yara_worker_available
            else "local-bwrap-measured"
            if yara_engine is not None
            else "unavailable"
        ),
    )
    for worker_family, family_name in (
        ("static", "static_analysis"),
        ("capa", "capa_analysis"),
        ("smda", "smda_analysis"),
        ("tshark", "analysis_worker_tshark"),
        ("capinfos", "analysis_worker_capinfos"),
        ("floss", "analysis_worker_floss"),
        ("mwcp", "analysis_worker_mwcp"),
        ("yara", "analysis_worker_yara"),
    ):
        worker_status = worker_statuses.get(worker_family)
        worker_available = bool(worker_status and worker_status.available)
        record(
            family_name,
            worker_available,
            None
            if worker_available
            else worker_status.available_reason
            if worker_status is not None
            else confined_analysis_execution.available_reason or "analysis_worker_unavailable",
            execution_mode="analysis-worker",
        )
    record(
        "sample_similarity",
        blob_catalog is not None,
        "similarity_artifact_store_not_configured",
    )
    record(
        "floss_extraction",
        floss_available,
        floss_reason,
        execution_mode="analysis-worker",
    )
    record(
        "configuration_extraction",
        mwcp_available,
        mwcp_reason,
        execution_mode="analysis-worker",
    )
    behavior_report_import_available = callable(getattr(behavior_report_importer, "parse", None))
    record(
        "behavior_report_import",
        behavior_report_import_available,
        None if behavior_report_import_available else "behavior_report_importer_unavailable",
    )
    record("retrohunt", retrohunt_available, retrohunt_reason)
    network_execution_mode = (
        getattr(tshark, "execution_mode", "local-bwrap-measured") if network_available else None
    )
    record(
        "network_capture",
        network_available,
        network_reason,
        execution_mode=network_execution_mode,
    )
    record(
        "network_capture_inspect",
        network_available,
        network_reason,
        execution_mode=network_execution_mode,
    )
    record(
        "network_stream_reassemble",
        network_source_configured,
        None if network_source_configured else "no_operator_capture_source",
    )
    record(
        "network_messages_segment",
        network_source_configured,
        None if network_source_configured else "no_operator_capture_source",
    )
    record(
        "network_protocol",
        network_source_configured,
        None if network_source_configured else "no_operator_capture_source",
    )
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
        confined_rea_runner=native_runner,
        confined_analysis_execution=confined_analysis_execution,
    )
    return InvestigationRuntimePorts(
        optional_tool_ports=ports,
        statuses=tuple(statuses),
        external_effect_coordinator=coordinator,
    )


def build_investigation_runtime_composition_factory(
    settings: Settings,
    *,
    uow_factory: UnitOfWorkFactory,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    external_effect_coordinator: ExternalEffectCoordinator | None = None,
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
    confined_analysis_execution: ConfinedAnalysisExecutionPort | None = None,
) -> InvestigationRuntimeComposition:
    """Build the same production composition used by both API and worker roots."""
    ports = build_investigation_runtime_ports(
        settings,
        uow_factory=uow_factory,
        session_factory=session_factory,
        external_effect_coordinator=external_effect_coordinator,
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
        confined_analysis_execution=confined_analysis_execution,
    )
    return InvestigationRuntimeComposition(
        composition_factory=InvestigationCompositionFactory(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            model_conversations=model_conversations,
            settings=settings,
            optional_tool_ports=ports.optional_tool_ports,
            turn_transport=(
                ModelConversationTurnTransport(
                    model_conversations,
                    bridge_external_effect_binding_factory=(
                        _build_bridge_external_effect_binding
                        if ports.external_effect_coordinator is not None
                        else None
                    ),
                    require_external_effect_authority=(
                        ports.external_effect_coordinator is not None
                    ),
                )
                if _bridge_turn_transport_configured(settings)
                else None
            ),
        ),
        ports=ports,
    )


def _bridge_turn_transport_configured(settings: Settings) -> bool:
    """Fail closed unless Bridge routing and endpoint configuration are explicit."""
    base_url = getattr(settings, "openai_bridge_base_url", "")
    api_key = getattr(settings, "openai_bridge_api_key", None)
    forced_adapter = getattr(settings, "model_force_adapter", "auto")
    research_route = getattr(settings, "model_route_web_research", "chatgpt_bridge")
    if not isinstance(base_url, str) or not base_url.strip():
        return False
    # The loopback URL is only a default. Require either credentials or a
    # non-default endpoint so a constructed ModelConversationService is not
    # mistaken for a configured Bridge transport.
    endpoint_configured = base_url.rstrip("/") != "http://127.0.0.1:8001/v1"
    api_key_value = getattr(api_key, "get_secret_value", lambda: api_key)()
    transport_configured = bool(api_key_value) or endpoint_configured
    route_uses_bridge = forced_adapter in {"chatgpt_bridge", "openai"} or (
        forced_adapter == "auto" and research_route == "chatgpt_bridge"
    )
    return transport_configured and route_uses_bridge


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

    families = {
        item.family: {
            "status": item.state.value,
            "reason": item.reason,
            **(
                {
                    "available_reason": item.available_reason,
                    "execution_mode": item.execution_mode,
                }
                if item.execution_mode is not None
                else {}
            ),
        }
        for item in ports.statuses
    }
    worker = ports.optional_tool_ports.confined_analysis_execution
    if worker is not None:
        inventory_families = {
            "static_analysis": "static",
            "capa_analysis": "capa",
            "smda_analysis": "smda",
            "sample_analysis": "sample_analysis",
            "floss_extraction": "floss",
            "configuration_extraction": "mwcp",
            "rule_workbench": "yara",
            "analysis_worker_tshark": "tshark",
            "analysis_worker_capinfos": "capinfos",
            "analysis_worker_floss": "floss",
            "analysis_worker_mwcp": "mwcp",
            "analysis_worker_yara": "yara",
        }
        for inventory_name, worker_name in inventory_families.items():
            item = families.get(inventory_name)
            worker_family = worker.family_statuses.get(worker_name)
            if item is None or worker_family is None:
                continue
            item["version"] = worker_family.version
            if worker_family.available_reason is not None:
                item["available_reason"] = worker_family.available_reason
    worker_details: dict[str, object] = {
        "status": "unavailable",
        "available_reason": "analysis_worker_unavailable",
        "reachable": False,
        "confined": False,
        "dependencies_ready": False,
        "isolation_profile": None,
        "versions": {},
        "families": {},
    }
    if worker is not None:
        attestation = worker.attestation
        worker_details = {
            "status": "available" if worker.available else "unavailable",
            "available_reason": worker.available_reason,
            "reachable": attestation.reachable,
            "confined": attestation.confined,
            "dependencies_ready": attestation.dependencies_ready,
            "isolation_profile": attestation.isolation_profile,
            "versions": dict(attestation.versions),
            "families": {
                name: {
                    "available": item.available,
                    "version": item.version,
                    "available_reason": item.available_reason,
                }
                for name, item in worker.family_statuses.items()
            },
        }
    return {
        "families": families,
        "analysis_worker": worker_details,
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


def _vt_proxy_route_status(
    settings: Settings,
    adapter: VirusTotalPort | None,
    capability: VirusTotalCapability,
) -> tuple[bool, str | None]:
    if adapter is None:
        return False, "virustotal_adapter_not_configured"
    if not settings.virustotal_proxy_url or not settings.virustotal_base_url:
        return False, "virustotal_proxy_route_not_configured"
    routing_policy = getattr(adapter, "_routing_policy", None)
    if routing_policy is None:
        return False, "virustotal_route_not_verified"
    route = routing_policy.route_for(capability)
    if route is None:
        return False, "virustotal_route_not_configured"
    if route.primary.transport is not VirusTotalTransportKind.PROXY:
        return False, "virustotal_proxy_route_required"
    if route.primary.variant is not VirusTotalEndpointVariant.V3:
        return False, "virustotal_v3_route_required"
    return True, None


def _native_port_unavailable_reason(port: object) -> str | None:
    if getattr(port, "available", False):
        return None
    if isinstance(port, UnavailableNativeAnalysisPort):
        error = port.unavailable_error()
        return error.code if error.code != "native_analysis_unavailable" else str(error)
    provider = getattr(port, "provider", None)
    provider_name = getattr(provider, "value", "unknown")
    return f"provider_unavailable:{provider_name}"
