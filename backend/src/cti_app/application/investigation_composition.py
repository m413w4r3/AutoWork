"""Production composition for the investigation loop and its extensible tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast
from uuid import UUID

from cti_app.application.analyst_handoff import (
    AnalystPostSynthesisService,
    loop_budget_from_settings,
)
from cti_app.application.analyst_opinion_service import (
    AnalystOpinionService,
    FindingServiceClaimKnowledgeProvider,
    OperatorProtocolFigureAttachmentBuilder,
    register_operator_opinion_tools,
)
from cti_app.application.article_novelty import ArticleNoveltyService
from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.bridge_investigator import (
    BridgeInvestigator,
    InvestigationTurnTransport,
    ModelConversationTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.confined_analysis import ConfinedAnalysisExecutionPort
from cti_app.application.detection_review_service import DetectionReviewService
from cti_app.application.evidence_recorder import EvidenceRecorder, ProductionEvidenceRecorder
from cti_app.application.goodware import GoodwareMeasurementService
from cti_app.application.invariants import InvariantRegistryService
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import (
    SAMPLE_ANALYSIS_CAPABILITY,
    LocalSampleAnalysisService,
    SampleAnalyzer,
    register_acquisition_tools,
)
from cti_app.application.investigation_advisor_tools import (
    InvestigationAdvisorTool,
    ReparsePersistedAdvisorOutputTool,
    register_advisor_reparse_tool,
    register_investigation_advisor_tool,
)
from cti_app.application.investigation_article_snapshot import ProductionArticleSnapshotAdapter
from cti_app.application.investigation_dotnet_tools import (
    DOTNET_TOOL_NAMES,
    dotnet_tool_capabilities,
    register_dotnet_tools,
)
from cti_app.application.investigation_experiments import register_operator_experiment_tools
from cti_app.application.investigation_extraction_tools import (
    extraction_tool_capabilities,
    register_extraction_tools,
)
from cti_app.application.investigation_findings import register_operator_finding_tools
from cti_app.application.investigation_governance import SampleQualificationService
from cti_app.application.investigation_local_tools import (
    SampleBytesReader,
    local_tool_capabilities,
    register_local_analysis_tools,
)
from cti_app.application.investigation_native_tools import (
    NATIVE_TOOL_NAME,
    native_tool_capabilities,
    register_native_tools,
)
from cti_app.application.investigation_network_tools import (
    network_tool_capabilities,
    register_network_tools,
)
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
    SampleAssociationRepository,
)
from cti_app.application.investigation_protocol_tools import (
    ProductionProtocolModelStore,
    RepositoryProtocolDocumentEvidenceResolver,
    register_protocol_tools,
)
from cti_app.application.investigation_retrohunt_tools import (
    register_retrohunt_tools,
    retrohunt_tool_capabilities,
)
from cti_app.application.investigation_rule_tools import (
    EvidencePayloadReader,
    register_rule_tools,
    rule_tool_capabilities,
)
from cti_app.application.investigation_service import (
    InvestigationService,
    ProductionArtifactContextSnapshotRepository,
    ProductionArtifactObservationWriter,
)
from cti_app.application.investigation_shodan_tools import (
    register_shodan_tools,
    shodan_tool_capabilities,
)
from cti_app.application.investigation_signature_bridge_tools import (
    RuleToolExecutionPort,
    register_signature_bridge_tools,
    signature_bridge_tool_capabilities,
)
from cti_app.application.investigation_similarity_tools import (
    SIMILARITY_TOOL_NAMES,
    register_similarity_tools,
    similarity_tool_capabilities,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolAdapter,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    project_tool_availability,
)
from cti_app.application.investigation_vt_tools import (
    register_virustotal_tools,
    virustotal_tool_capabilities,
)
from cti_app.application.local_analysis import (
    GhidraAnalysisPort,
    LocalCommandCatalog,
    LocalCommandService,
    SampleBytesForAnalysisPort,
    SampleBytesUnavailableError,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import BridgeExternalEffectBinding
from cti_app.application.native_analysis import NativeAnalysisService
from cti_app.application.network_capture_import import capture_max_bytes
from cti_app.application.network_protocol_model import NetworkProtocolWorkbench
from cti_app.application.network_workbench import (
    NetworkWorkbench,
    ProductionNetworkCaptureBlobStore,
    QuarantinedCaptureSource,
)
from cti_app.application.operator_tool_invocation import OperatorToolInvocationService
from cti_app.application.osint_open_policy import OsintOpenPolicyService
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import (
    ArticleSnapshotPort,
    CanonicalEvidenceSummaryPort,
    InvestigationGraphReadPort,
    RepositoryInvestigationGraphReadPort,
    RepositoryInvestigationReadPort,
    ResearchContextBuilder,
    RuleCorpusContextInventoryPort,
    SampleFeaturePackPort,
)
from cti_app.application.retrohunt import RETROHUNT_CAPABILITY, RetrohuntService
from cti_app.application.rule_workbench import (
    RuleCorpusBytesPort,
    RuleWorkbench,
    RuleWorkbenchPolicy,
    YaraEnginePort,
)
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.sample_extraction_ports import (
    ConfigurationExtractionPort,
    ObfuscatedStringExtractionPort,
)
from cti_app.application.sample_similarity import (
    CorpusSnapshotPort,
    GoodwarePrevalencePort,
    SampleSimilarityService,
    SimilarityArtifactStorePort,
)
from cti_app.application.shodan import ShodanBudgetEstimate, ShodanCapabilities, ShodanPort
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalPort,
    VirusTotalRetrohuntPort,
    VirusTotalRoutingPolicy,
)
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.config import VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST, Settings
from cti_app.domain.goodware import NonDiscriminantPatternRegistry
from cti_app.domain.investigation import (
    InvestigationBudget,
    SampleAssociation,
)
from cti_app.domain.network_protocol import NetworkExtractionLimitsV1
from cti_app.domain.sample_similarity import CorpusMember, CorpusSnapshot
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.legitimate_services import load_legitimate_services
from cti_app.infrastructure.rea_adapter import ReaReadOnlyRunner, build_native_analysis_port
from cti_app.infrastructure.tshark_adapter import TsharkAdapter
from cti_app.infrastructure.virustotal import VirusTotalRetrohuntProxyAdapter

LOCAL_READ_CAPABILITY = "investigation.local_read"
OPERATOR_WRITE_TOOL_NAMES = frozenset(
    {
        "finding.propose",
        "finding.revise",
        "opinion.compose",
        "opinion.revise",
        "opinion.note",
        "experiment.record",
        "hypothesis.declare",
        "advisor.reparse",
    }
)
OPTIONAL_CAPABILITIES = {
    "virustotal": "investigation.virustotal",
    "shodan": "investigation.shodan",
    "rule_workbench": "investigation.rule_workbench",
    "local_commands": "investigation.local_commands",
    "ghidra": "investigation.ghidra",
    "acquisition": "investigation.acquisition",
    "dotnet": "investigation.dotnet",
}
OPTIONAL_TOOL_NAMES = (
    "finding.propose",
    "finding.revise",
    "opinion.compose",
    "opinion.revise",
    "opinion.note",
    "experiment.record",
    "hypothesis.declare",
    "experiment.list",
    "advisor.consult",
    "vt.file_report",
    "vt.object_report",
    "vt.relationships",
    "vt.intelligence_search",
    "vt.behaviour",
    "vt.behaviour_pcap",
    "vt.download_sample",
    "shodan.host",
    "shodan.search",
    "shodan.domain",
    "shodan.count",
    "sample.analyze",
    "sample.set_qualification",
    "local.run_command",
    "ghidra.list_functions",
    "ghidra.list_imports",
    "ghidra.xrefs",
    "ghidra.decompile_function",
    "ghidra.search",
    "rule.propose",
    "rule.test",
    "rule.revise",
    "rule.compare",
    "rule.get",
    "samples.compare",
    "features.common",
    "signatures.candidates",
    "features.explain",
    "rule.propose_from_candidates",
    "sample.extract_obfuscated_strings",
    "sample.extract_configuration",
    "sandbox.report.import",
    "vt.retrohunt.submit",
    "vt.retrohunt.status",
    "vt.retrohunt.results",
    "network.capture.inspect",
    "network.suricata_replay",
    "network.suricata_validate_rules",
    "network.stream.reassemble",
    "network.messages.segment",
    "network.protocol.compare",
    "network.protocol.hypothesize",
    "network.protocol.verify",
    "network.protocol.render",
    "network.protocol.document",
    *DOTNET_TOOL_NAMES,
    NATIVE_TOOL_NAME,
)
FAMILY_CAPABILITY_KEYS = frozenset(
    {
        *SIMILARITY_TOOL_NAMES,
        "rule.propose_from_candidates",
        "sample.extract_obfuscated_strings",
        "sample.extract_configuration",
        "sandbox.report.import",
        RETROHUNT_CAPABILITY,
        "network.capture.inspect",
        "network.stream.reassemble",
        "network.messages.segment",
        "network.protocol.compare",
        "network.protocol.hypothesize",
        "network.protocol.verify",
        "network.protocol.render",
        "network.protocol.document",
        "investigation.dotnet",
        *DOTNET_TOOL_NAMES,
        NATIVE_TOOL_NAME,
    }
)


class SampleFeaturePackReader(SampleFeaturePackPort):
    """Read feature packs through the selected adapter when one is configured."""

    def __init__(self, reader: SampleFeaturePackPort | None = None) -> None:
        self._reader = reader

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None:
        if self._reader is None:
            return None
        return await self._reader.get_feature_pack(sample_sha256)


class QuarantineSampleBytesAdapter(SampleBytesForAnalysisPort):
    """Expose quarantined sample bytes through the local command service contract."""

    def __init__(self, source: RuleCorpusBytesPort, *, max_sample_bytes: int) -> None:
        self._source = source
        self._max_sample_bytes = max_sample_bytes

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        payload = await self._source.get_bytes_for_sample(sample_sha256)
        if len(payload) > self._max_sample_bytes:
            raise SampleBytesUnavailableError("Sample exceeds the configured local read limit")
        return payload

    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes:
        payload = await self._source.get_bytes_for_sample(sample_sha256)
        if len(payload) > min(max_bytes, self._max_sample_bytes):
            raise SampleBytesUnavailableError("Sample exceeds the configured local read limit")
        return payload


class RepositorySampleFeaturePackReader(SampleFeaturePackPort):
    """Resolve an indexed feature pack through sample and blob identities."""

    def __init__(self, uow: UnitOfWork, investigation_id: UUID) -> None:
        self._uow = uow
        self._investigation_id = investigation_id

    async def get_feature_pack(self, sample_sha256: str) -> Mapping[str, Any] | None:
        investigation = await self._uow.analyst_investigations.get(self._investigation_id)
        if investigation is None:
            return None
        sample_ids: list[UUID] = []
        for sample in await self._uow.samples.list_for_subject(investigation.subject_id):
            blob = await self._uow.blobs.get(sample.blob_id)
            if blob is not None and blob.descriptor.sha256 == sample_sha256:
                sample_ids.append(sample.id)
        if not sample_ids:
            return None
        feature_sets = await self._uow.sample_feature_sets.list_for_samples(sample_ids)
        matching = [
            item
            for item in feature_sets
            if item.get("blob_sha256") == sample_sha256 and isinstance(item.get("payload"), Mapping)
        ]
        if not matching:
            return None
        item = matching[-1]
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            return None
        return {
            "sample_sha256": sample_sha256,
            "extractor_version": item.get("extractor_version"),
            "parameters_sha256": item.get("parameters_sha256"),
            "features": payload,
        }


class RepositoryCorpusSnapshotReader(CorpusSnapshotPort):
    """Read the exact sample associations of one investigation as a corpus view."""

    def __init__(self, uow: UnitOfWork, investigation_id: UUID) -> None:
        self._uow = uow
        self._investigation_id = investigation_id

    async def get_snapshot(self, investigation_id: UUID) -> CorpusSnapshot:
        if investigation_id != self._investigation_id:
            raise ValueError("Corpus reader belongs to another investigation")
        associations = await self._uow.sample_associations.list_for_investigation(investigation_id)
        latest: dict[str, SampleAssociation] = {}
        for association in associations:
            current = latest.get(association.sample_sha256)
            if current is None or association.version > current.version:
                latest[association.sample_sha256] = association
        goodware_repository = getattr(self._uow, "investigation_goodware_baselines", None)
        baseline_id = (
            await goodware_repository.get(investigation_id)
            if goodware_repository is not None
            else None
        )
        return CorpusSnapshot(
            investigation_id=investigation_id,
            members=tuple(
                CorpusMember(
                    sample_sha256=association.sample_sha256,
                    qualification=association.qualification.value,
                    origin=association.origin.value,
                    association_version=association.version,
                )
                for association in latest.values()
            ),
            goodware_baseline_id=baseline_id,
        )


@dataclass(frozen=True, slots=True)
class InvestigationToolDependencies:
    """Dependencies visible to tool registrars; unavailable capabilities are ``None``."""

    investigation_id: UUID
    article_snapshots: ArticleSnapshotPort
    observations: EvidenceObservationRepository
    findings: FindingRepository
    sample_associations: SampleAssociationRepository
    feature_packs: SampleFeaturePackPort
    artifact_store: ProductionArtifactStore
    virustotal: VirusTotalPort | None = None
    shodan: ShodanPort | None = None
    rule_workbench: RuleWorkbench | None = None
    local_commands: LocalCommandService | None = None
    ghidra: GhidraAnalysisPort | None = None
    acquisition: VirusTotalSampleAcquisitionService | None = None
    virustotal_observations: VirusTotalObservationService | None = None
    vt_capabilities: VirusTotalCapabilities | None = None
    shodan_capabilities: ShodanCapabilities | None = None
    shodan_budget_estimate: ShodanBudgetEstimate | None = None
    rule_corpus_bytes: RuleCorpusBytesPort | None = None
    yara_engine: YaraEnginePort | None = None
    local_command_catalog: LocalCommandCatalog | None = None
    sample_static_analysis: Any | None = None
    sample_capabilities_analysis: Any | None = None
    sample_code_features: CodeFeatureService | None = None
    sample_goodware: GoodwareMeasurementService | None = None
    confined_analysis_execution: ConfinedAnalysisExecutionPort | None = None
    available_capabilities: frozenset[str] = frozenset()
    sample_similarity: SampleSimilarityService | None = None
    invariant_registry: InvariantRegistryService | None = None
    floss_extraction: ObfuscatedStringExtractionPort | None = None
    configuration_extraction: ConfigurationExtractionPort | None = None
    behavior_report_importer: BehaviorReportImporter | None = None
    retrohunt_provider: VirusTotalRetrohuntPort | None = None
    vt_routing_policy: VirusTotalRoutingPolicy | None = None
    network_quarantine: QuarantinedCaptureSource | None = None
    tshark: TsharkAdapter | None = None
    confined_rea_runner: ReaReadOnlyRunner | None = None


@dataclass(frozen=True, slots=True)
class OptionalInvestigationToolPorts:
    virustotal: VirusTotalPort | None = None
    shodan: ShodanPort | None = None
    rule_workbench: RuleWorkbench | None = None
    local_commands: LocalCommandService | None = None
    ghidra: GhidraAnalysisPort | None = None
    acquisition: VirusTotalSampleAcquisitionService | None = None
    virustotal_observations: VirusTotalObservationService | None = None
    vt_capabilities: VirusTotalCapabilities | None = None
    shodan_capabilities: ShodanCapabilities | None = None
    shodan_budget_estimate: ShodanBudgetEstimate | None = None
    rule_corpus_bytes: RuleCorpusBytesPort | None = None
    yara_engine: YaraEnginePort | None = None
    local_command_catalog: LocalCommandCatalog | None = None
    sample_static_analysis: Any | None = None
    sample_capabilities_analysis: Any | None = None
    sample_code_features: CodeFeatureService | None = None
    sample_goodware: GoodwareMeasurementService | None = None
    confined_analysis_execution: ConfinedAnalysisExecutionPort | None = None
    available_capabilities: frozenset[str] = frozenset()
    sample_similarity: SampleSimilarityService | None = None
    invariant_registry: InvariantRegistryService | None = None
    floss_extraction: ObfuscatedStringExtractionPort | None = None
    configuration_extraction: ConfigurationExtractionPort | None = None
    behavior_report_importer: BehaviorReportImporter | None = None
    retrohunt_provider: VirusTotalRetrohuntPort | None = None
    vt_routing_policy: VirusTotalRoutingPolicy | None = None
    network_quarantine: QuarantinedCaptureSource | None = None
    network_quarantine_factory: Callable[[UUID], QuarantinedCaptureSource] | None = None
    tshark: TsharkAdapter | None = None
    confined_rea_runner: ReaReadOnlyRunner | None = None
    # Process-level collaborators of SampleSimilarityService. The service itself is
    # assembled per composition so its feature-pack and corpus readers are bound to
    # the exact investigation and unit of work, never to a stale one.
    similarity_artifacts: SimilarityArtifactStorePort | None = None
    goodware_prevalence: GoodwarePrevalencePort | None = None


ToolRegistrar = Callable[[ToolRegistry, InvestigationToolDependencies], None]


def _register_similarity_tool_family(
    registry: ToolRegistry,
    *,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    enabled: set[str],
) -> None:
    register_similarity_tools(
        registry,
        service=dependencies.sample_similarity,
        sample_associations=dependencies.sample_associations,
        recorder=recorder,
    )
    enabled.update(
        similarity_tool_capabilities(
            service=dependencies.sample_similarity,
            sample_associations=dependencies.sample_associations,
            recorder=recorder,
        )
    )


def _register_signature_bridge_tool_family(
    registry: ToolRegistry,
    *,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    rule_tools: RuleToolExecutionPort | None,
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    enabled: set[str],
) -> None:
    invariant_registry = dependencies.invariant_registry
    if invariant_registry is None:
        invariant_registry = InvariantRegistryService(
            uow_factory,
            dependencies.sample_goodware,
            settings=settings,
        )
    register_signature_bridge_tools(
        registry,
        service=dependencies.sample_similarity,
        sample_associations=dependencies.sample_associations,
        recorder=recorder,
        uow_factory=uow_factory,
        invariant_registry=invariant_registry,
        rule_tools=rule_tools,
    )
    enabled.update(
        signature_bridge_tool_capabilities(
            service=dependencies.sample_similarity,
            sample_associations=dependencies.sample_associations,
            recorder=recorder,
            uow_factory=uow_factory,
            invariant_registry=invariant_registry,
            rule_tools=rule_tools,
        )
    )


def _register_extraction_tool_family(
    registry: ToolRegistry,
    *,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    enabled: set[str],
) -> None:
    floss = dependencies.floss_extraction
    configuration = dependencies.configuration_extraction
    importer = dependencies.behavior_report_importer
    register_extraction_tools(
        registry,
        sample_associations=dependencies.sample_associations,
        recorder=recorder,
        floss=floss,
        configuration=configuration,
        goodware=dependencies.sample_goodware,
        uow_factory=uow_factory,
        report_importer=importer,
    )
    enabled.update(
        extraction_tool_capabilities(
            floss=floss,
            configuration=configuration,
            report_import_enabled=importer is not None,
        )
    )


def _register_retrohunt_tool_family(
    registry: ToolRegistry,
    *,
    uow: UnitOfWork,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    enabled: set[str],
) -> None:
    provider = dependencies.retrohunt_provider
    if provider is None or getattr(uow, "retrohunt_reservations", None) is None:
        return
    capabilities = dependencies.vt_capabilities or VirusTotalCapabilities()
    routing_policy = dependencies.vt_routing_policy or VirusTotalRoutingPolicy()
    proxy_provider = VirusTotalRetrohuntProxyAdapter(
        capabilities=capabilities,
        routing_policy=routing_policy,
        proxy_client=provider,
    )
    service = RetrohuntService(
        uow_factory=uow_factory,
        review_service=DetectionReviewService(uow_factory, artifact_store),
        provider=proxy_provider,
        evidence_recorder=recorder,
    )
    register_retrohunt_tools(registry, service=service)
    enabled.update(retrohunt_tool_capabilities(capabilities, routing_policy, proxy_provider))


def _register_network_tool_family(
    registry: ToolRegistry,
    *,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    enabled: set[str],
) -> NetworkWorkbench | None:
    quarantine = dependencies.network_quarantine
    if quarantine is None or not callable(getattr(quarantine, "read_quarantined_capture", None)):
        return None

    limits = NetworkExtractionLimitsV1()
    tshark = dependencies.tshark or TsharkAdapter(limits=limits)

    workbench = NetworkWorkbench(
        quarantine=quarantine,
        blobs=ProductionNetworkCaptureBlobStore(dependencies.artifact_store),
        tshark=tshark,
        recorder=recorder,
        limits=limits,
    )
    register_network_tools(registry, workbench=workbench, recorder=recorder)
    enabled.update(network_tool_capabilities(tshark))
    return workbench


def _register_protocol_tool_family(
    registry: ToolRegistry,
    *,
    uow: UnitOfWork,
    workbench: NetworkWorkbench | None,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    enabled: set[str],
) -> None:
    protocol_workbench = NetworkProtocolWorkbench(workbench) if workbench is not None else None
    document_evidence = RepositoryProtocolDocumentEvidenceResolver(
        investigations=uow.analyst_investigations,
        observations=uow.evidence_observations,
        findings=uow.investigation_findings,
        article_snapshots=dependencies.article_snapshots,
    )
    register_protocol_tools(
        registry,
        service=protocol_workbench,
        recorder=recorder,
        model_store=ProductionProtocolModelStore(dependencies.artifact_store),
        document_evidence=document_evidence,
    )
    enabled.update({"network.protocol.render", "network.protocol.document"})
    if protocol_workbench is not None:
        enabled.update(
            {
                "network.protocol.compare",
                "network.protocol.hypothesize",
                "network.protocol.verify",
            }
        )


def _register_native_tool_family(
    registry: ToolRegistry,
    *,
    dependencies: InvestigationToolDependencies,
    recorder: EvidenceRecorder,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    enabled: set[str],
) -> None:
    port = build_native_analysis_port(settings, runner=dependencies.confined_rea_runner)
    service = NativeAnalysisService(
        port,
        timeout_seconds=settings.native_analysis_timeout_seconds,
        max_output_bytes=settings.native_analysis_max_output_bytes,
    )
    register_native_tools(
        registry,
        service=service,
        port=port,
        sample_associations=dependencies.sample_associations,
        recorder=recorder,
        enabled=settings.native_analysis_enabled,
        provider=settings.native_analysis_provider,
    )
    enabled.update(
        native_tool_capabilities(
            enabled=settings.native_analysis_enabled,
            provider=settings.native_analysis_provider,
            port=port,
            recorder=recorder,
            sample_associations=dependencies.sample_associations,
        )
    )
    register_dotnet_tools(
        registry,
        execution=dependencies.confined_analysis_execution,
        sample_associations=dependencies.sample_associations,
        recorder=recorder,
        uow_factory=uow_factory,
        settings=settings,
    )
    enabled.update(
        dotnet_tool_capabilities(
            execution=dependencies.confined_analysis_execution,
            sample_associations=dependencies.sample_associations,
            recorder=recorder,
        )
    )


class _PermissionAwareInvestigationTransport:
    """Project a denied model submission to the loop's durable review state."""

    def __init__(self, transport: InvestigationTurnTransport) -> None:
        self._transport = transport
        self.requires_external_effect_authority = bool(
            getattr(transport, "requires_external_effect_authority", False)
        )

    async def build_external_effect_binding(
        self, *, family: str, **attempt: Any
    ) -> BridgeExternalEffectBinding | None:
        build = getattr(self._transport, "build_external_effect_binding", None)
        if not callable(build):
            return None
        binding = await build(family=family, **attempt)
        if binding is None or isinstance(binding, BridgeExternalEffectBinding):
            return binding
        return None

    async def reconcile_orphaned_investigation_turn(
        self,
        *,
        investigation_id: UUID,
        turn_index: int,
        context_sha256: str,
        attempt: int,
        owner_job_terminal: bool,
    ) -> str | None:
        reconcile = getattr(self._transport, "reconcile_orphaned_investigation_turn", None)
        if not callable(reconcile):
            return None
        disposition = await reconcile(
            investigation_id=investigation_id,
            turn_index=turn_index,
            context_sha256=context_sha256,
            attempt=attempt,
            owner_job_terminal=owner_job_terminal,
        )
        return disposition if isinstance(disposition, str) else None

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
        bridge_external_effect_binding: BridgeExternalEffectBinding | None = None,
    ) -> TurnSubmission:
        submission_kwargs: dict[str, Any] = {
            "investigation_id": investigation_id,
            "subject_id": subject_id,
            "turn_index": turn_index,
            "context_sha256": context_sha256,
            "prompt": prompt,
            "external_llm_allowed": external_llm_allowed,
            "timeout_seconds": timeout_seconds,
        }
        if bridge_external_effect_binding is not None:
            submission_kwargs["bridge_external_effect_binding"] = bridge_external_effect_binding
        result = await self._transport.submit(**submission_kwargs)
        if result.status is not TurnSubmissionStatus.BLOCKED:
            return result
        return TurnSubmission(
            status=TurnSubmissionStatus.NEEDS_REVIEW,
            idempotency_key=result.idempotency_key,
            conversation_id=result.conversation_id,
            detail=result.detail or "Model submission requires an approval decision.",
        )

    async def submit_with_attempt(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        turn_index: int,
        context_sha256: str,
        prompt: str,
        external_llm_allowed: bool,
        attempt: int,
        timeout_seconds: float | None = None,
        bridge_external_effect_binding: BridgeExternalEffectBinding | None = None,
    ) -> TurnSubmission:
        submit_attempt = getattr(self._transport, "submit_with_attempt", None)
        if not callable(submit_attempt):
            return TurnSubmission(
                status=TurnSubmissionStatus.UNAVAILABLE,
                detail="The configured transport cannot create a distinct retry attempt.",
            )
        result = cast(
            TurnSubmission,
            await submit_attempt(
                investigation_id=investigation_id,
                subject_id=subject_id,
                turn_index=turn_index,
                context_sha256=context_sha256,
                prompt=prompt,
                external_llm_allowed=external_llm_allowed,
                attempt=attempt,
                timeout_seconds=timeout_seconds,
                **(
                    {"bridge_external_effect_binding": bridge_external_effect_binding}
                    if bridge_external_effect_binding is not None
                    else {}
                ),
            ),
        )
        if result.status is not TurnSubmissionStatus.BLOCKED:
            return result
        return TurnSubmission(
            status=TurnSubmissionStatus.NEEDS_REVIEW,
            idempotency_key=result.idempotency_key,
            conversation_id=result.conversation_id,
            detail=result.detail or "Model submission requires an approval decision.",
        )


@dataclass(frozen=True, slots=True)
class InvestigationComposition:
    service: InvestigationService
    operator_tool_invocations: OperatorToolInvocationService
    osint_open_policy_service: OsintOpenPolicyService | None
    registry: ToolRegistry
    tool_dependencies: InvestigationToolDependencies
    available_capabilities: frozenset[str]
    available_tools: tuple[str, ...]
    unavailable_tools: tuple[str, ...]
    unavailable_tool_reasons: Mapping[str, str]
    advisor_tool: InvestigationAdvisorTool | None = None


class _TrackingToolRegistry(InMemoryToolRegistry):
    """Keep the small in-memory registry while exposing its declared catalogue."""

    def __init__(self) -> None:
        super().__init__()
        self._definitions: dict[tuple[str, str], ToolDefinition] = {}

    def register(self, definition: ToolDefinition, adapter: ToolAdapter) -> None:
        super().register(definition, adapter)
        self._definitions[(definition.name, definition.schema_version)] = definition

    def unregister(self, name: str, schema_version: str | None = None) -> bool:
        removed = super().unregister(name, schema_version)
        if removed:
            self._definitions = {
                key: definition
                for key, definition in self._definitions.items()
                if key[0] != name or (schema_version is not None and key[1] != schema_version)
            }
        return removed

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._definitions.values())


def investigation_tool_dependencies_from_settings(
    settings: Settings,
    *,
    investigation_id: UUID,
    article_snapshots: ArticleSnapshotPort,
    observations: EvidenceObservationRepository,
    findings: FindingRepository,
    sample_associations: SampleAssociationRepository,
    artifact_store: ProductionArtifactStore,
    feature_packs: SampleFeaturePackPort | None = None,
    virustotal: VirusTotalPort | None = None,
    shodan: ShodanPort | None = None,
    rule_workbench: RuleWorkbench | None = None,
    local_commands: LocalCommandService | None = None,
    ghidra: GhidraAnalysisPort | None = None,
    acquisition: VirusTotalSampleAcquisitionService | None = None,
    virustotal_observations: VirusTotalObservationService | None = None,
    vt_capabilities: VirusTotalCapabilities | None = None,
    shodan_capabilities: ShodanCapabilities | None = None,
    shodan_budget_estimate: ShodanBudgetEstimate | None = None,
    rule_corpus_bytes: RuleCorpusBytesPort | None = None,
    yara_engine: YaraEnginePort | None = None,
    local_command_catalog: LocalCommandCatalog | None = None,
    sample_static_analysis: Any | None = None,
    sample_capabilities_analysis: Any | None = None,
    sample_code_features: CodeFeatureService | None = None,
    sample_goodware: GoodwareMeasurementService | None = None,
    confined_analysis_execution: ConfinedAnalysisExecutionPort | None = None,
    sample_similarity: SampleSimilarityService | None = None,
    invariant_registry: InvariantRegistryService | None = None,
    floss_extraction: ObfuscatedStringExtractionPort | None = None,
    configuration_extraction: ConfigurationExtractionPort | None = None,
    behavior_report_importer: BehaviorReportImporter | None = None,
    retrohunt_provider: VirusTotalRetrohuntPort | None = None,
    vt_routing_policy: VirusTotalRoutingPolicy | None = None,
    network_quarantine: QuarantinedCaptureSource | None = None,
    tshark: TsharkAdapter | None = None,
    confined_rea_runner: ReaReadOnlyRunner | None = None,
    available_capabilities: frozenset[str] = frozenset(),
) -> InvestigationToolDependencies:
    """Apply Settings opt-ins before making optional ports visible to registrars."""

    vt_routing_policy = getattr(virustotal, "_routing_policy", None) or vt_routing_policy
    vt_proxy_configured = bool(settings.virustotal_proxy_url and settings.virustotal_base_url)

    def vt_route_configured(capability: VirusTotalCapability) -> bool:
        if virustotal is None or not vt_proxy_configured:
            return False
        if vt_routing_policy is None:
            return False
        route = vt_routing_policy.route_for(capability)
        return bool(
            route is not None
            and route.primary.transport is VirusTotalTransportKind.PROXY
            and route.primary.variant is VirusTotalEndpointVariant.V3
        )

    configured_vt_capabilities = vt_capabilities or VirusTotalCapabilities()
    download_hosts_configured = (
        VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST
        in settings.virustotal_download_allowed_hosts.split(",")
    )
    download_ports_configured = any(
        port.strip() for port in settings.virustotal_download_allowed_ports.split(",")
    )
    vt_capabilities = replace(
        configured_vt_capabilities,
        file_report=(
            configured_vt_capabilities.file_report
            and settings.virustotal_file_report_enabled
            and vt_route_configured(VirusTotalCapability.FILE_REPORT)
        ),
        file_relationships=(
            configured_vt_capabilities.file_relationships
            and settings.virustotal_object_relationships_enabled
            and vt_route_configured(VirusTotalCapability.FILE_RELATIONSHIPS)
        ),
        intelligence_search=(
            configured_vt_capabilities.intelligence_search
            and settings.virustotal_intelligence_search_enabled
            and vt_route_configured(VirusTotalCapability.INTELLIGENCE_SEARCH)
        ),
        file_download=(
            configured_vt_capabilities.file_download
            and settings.virustotal_file_download_enabled
            and download_hosts_configured
            and download_ports_configured
            and settings.investigation_max_hits_acquired > 0
            and acquisition is not None
            and vt_route_configured(VirusTotalCapability.FILE_DOWNLOAD)
        ),
        behaviour_reports=(
            configured_vt_capabilities.behaviour_reports
            and settings.virustotal_behaviour_reports_enabled
            and vt_route_configured(VirusTotalCapability.BEHAVIOUR_REPORTS)
        ),
        behaviour_pcap=(
            configured_vt_capabilities.behaviour_pcap
            and settings.virustotal_behaviour_pcap_enabled
            and vt_route_configured(VirusTotalCapability.BEHAVIOUR_PCAP)
        ),
    )
    vt_enabled = any(
        (
            vt_capabilities.file_report,
            vt_capabilities.file_relationships,
            vt_capabilities.intelligence_search,
            vt_capabilities.behaviour_reports,
            vt_capabilities.file_download,
            vt_capabilities.behaviour_pcap,
        )
    )
    shodan_enabled = settings.shodan_api_key is not None and any(
        (
            settings.shodan_host_enabled,
            settings.shodan_search_enabled,
            settings.shodan_domain_enabled,
            settings.shodan_count_enabled,
        )
    )
    acquisition_enabled = vt_capabilities.file_download
    enabled_capabilities = set(available_capabilities)
    if not vt_enabled:
        virustotal = None
    if not shodan_enabled:
        shodan = None
    if not settings.rule_workbench_enabled:
        rule_workbench = None
    if not settings.ghidra_enabled:
        ghidra = None
    if not acquisition_enabled:
        acquisition = None

    shodan_capabilities = shodan_capabilities or ShodanCapabilities(
        host=settings.shodan_host_enabled,
        search=settings.shodan_search_enabled,
        domain=settings.shodan_domain_enabled,
        count=settings.shodan_count_enabled,
    )

    return InvestigationToolDependencies(
        investigation_id=investigation_id,
        article_snapshots=article_snapshots,
        observations=observations,
        findings=findings,
        sample_associations=sample_associations,
        feature_packs=SampleFeaturePackReader(feature_packs),
        artifact_store=artifact_store,
        virustotal=virustotal,
        shodan=shodan,
        rule_workbench=rule_workbench,
        local_commands=local_commands,
        ghidra=ghidra,
        acquisition=acquisition,
        virustotal_observations=virustotal_observations if virustotal is not None else None,
        vt_capabilities=(
            vt_capabilities if virustotal is not None or retrohunt_provider is not None else None
        ),
        shodan_capabilities=shodan_capabilities if shodan is not None else None,
        shodan_budget_estimate=shodan_budget_estimate if shodan is not None else None,
        rule_corpus_bytes=rule_corpus_bytes,
        yara_engine=yara_engine if settings.rule_workbench_enabled else None,
        local_command_catalog=local_command_catalog,
        sample_static_analysis=(
            sample_static_analysis if settings.local_analysis_enabled else None
        ),
        sample_capabilities_analysis=(
            sample_capabilities_analysis if settings.local_analysis_enabled else None
        ),
        sample_code_features=(sample_code_features if settings.local_analysis_enabled else None),
        sample_goodware=(sample_goodware if settings.local_analysis_enabled else None),
        confined_analysis_execution=(
            confined_analysis_execution if settings.analysis_worker_enabled else None
        ),
        sample_similarity=sample_similarity,
        invariant_registry=invariant_registry,
        floss_extraction=floss_extraction,
        configuration_extraction=configuration_extraction,
        behavior_report_importer=behavior_report_importer,
        retrohunt_provider=retrohunt_provider,
        vt_routing_policy=vt_routing_policy,
        network_quarantine=network_quarantine,
        tshark=tshark,
        confined_rea_runner=confined_rea_runner,
        available_capabilities=frozenset(enabled_capabilities),
    )


def build_investigation_composition(
    uow: UnitOfWork,
    *,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    model_conversations: ModelConversationService,
    settings: Settings,
    investigation_id: UUID,
    tool_dependencies: InvestigationToolDependencies | None = None,
    optional_tool_ports: OptionalInvestigationToolPorts | None = None,
    tool_registrar: ToolRegistrar | None = None,
    turn_transport: InvestigationTurnTransport | None = None,
    advisor_turn_transport: InvestigationTurnTransport | None = None,
    operator_mode: bool = False,
) -> InvestigationComposition:
    article_snapshots = ProductionArticleSnapshotAdapter(uow_factory, artifact_store)
    source_dependencies = tool_dependencies
    ports = optional_tool_ports or OptionalInvestigationToolPorts()

    def supplied(name: str) -> Any | None:
        value = getattr(ports, name, None)
        return value if value is not None else getattr(source_dependencies, name, None)

    network_quarantine = supplied("network_quarantine")
    if network_quarantine is None:
        network_quarantine_factory = supplied("network_quarantine_factory")
        if network_quarantine_factory is not None:
            network_quarantine = network_quarantine_factory(investigation_id)

    base_dependencies = investigation_tool_dependencies_from_settings(
        settings,
        investigation_id=investigation_id,
        article_snapshots=(
            source_dependencies.article_snapshots if source_dependencies else article_snapshots
        ),
        observations=uow.evidence_observations,
        findings=uow.investigation_findings,
        sample_associations=uow.sample_associations,
        artifact_store=artifact_store,
        feature_packs=(
            source_dependencies.feature_packs
            if source_dependencies
            else RepositorySampleFeaturePackReader(uow, investigation_id)
        ),
        virustotal=(
            ports.virustotal
            if ports.virustotal is not None
            else source_dependencies.virustotal
            if source_dependencies
            else None
        ),
        shodan=(
            ports.shodan
            if ports.shodan is not None
            else source_dependencies.shodan
            if source_dependencies
            else None
        ),
        rule_workbench=(
            ports.rule_workbench
            if ports.rule_workbench is not None
            else source_dependencies.rule_workbench
            if source_dependencies
            else None
        ),
        local_commands=(
            ports.local_commands
            if ports.local_commands is not None
            else source_dependencies.local_commands
            if source_dependencies
            else None
        ),
        ghidra=(
            ports.ghidra
            if ports.ghidra is not None
            else source_dependencies.ghidra
            if source_dependencies
            else None
        ),
        acquisition=(
            ports.acquisition
            if ports.acquisition is not None
            else source_dependencies.acquisition
            if source_dependencies
            else None
        ),
        virustotal_observations=supplied("virustotal_observations"),
        vt_capabilities=supplied("vt_capabilities"),
        shodan_capabilities=supplied("shodan_capabilities"),
        shodan_budget_estimate=supplied("shodan_budget_estimate"),
        rule_corpus_bytes=supplied("rule_corpus_bytes"),
        yara_engine=supplied("yara_engine"),
        local_command_catalog=supplied("local_command_catalog"),
        sample_static_analysis=supplied("sample_static_analysis"),
        sample_capabilities_analysis=supplied("sample_capabilities_analysis"),
        sample_code_features=supplied("sample_code_features"),
        sample_goodware=supplied("sample_goodware"),
        confined_analysis_execution=supplied("confined_analysis_execution"),
        sample_similarity=supplied("sample_similarity"),
        invariant_registry=supplied("invariant_registry"),
        floss_extraction=supplied("floss_extraction"),
        configuration_extraction=supplied("configuration_extraction"),
        behavior_report_importer=supplied("behavior_report_importer"),
        retrohunt_provider=supplied("retrohunt_provider"),
        vt_routing_policy=supplied("vt_routing_policy"),
        network_quarantine=network_quarantine,
        tshark=supplied("tshark"),
        confined_rea_runner=supplied("confined_rea_runner"),
        available_capabilities=ports.available_capabilities
        | (source_dependencies.available_capabilities if source_dependencies else frozenset()),
    )
    if base_dependencies.investigation_id != investigation_id:
        raise ValueError("Tool dependencies belong to another investigation")
    operator_led = operator_mode

    if base_dependencies.sample_similarity is None and ports.similarity_artifacts is not None:
        base_dependencies = replace(
            base_dependencies,
            sample_similarity=SampleSimilarityService(
                feature_packs=base_dependencies.feature_packs,
                corpus_snapshots=RepositoryCorpusSnapshotReader(uow, investigation_id),
                artifacts=ports.similarity_artifacts,
                goodware=ports.goodware_prevalence,
            ),
        )

    confined_analysis = base_dependencies.confined_analysis_execution
    confined_yara = (
        confined_analysis.family_statuses.get("yara") if confined_analysis is not None else None
    )
    yara_execution_available = bool(
        (
            base_dependencies.yara_engine is not None
            and getattr(base_dependencies.yara_engine, "available", True)
        )
        or (confined_yara is not None and confined_yara.available)
    )
    if (
        settings.rule_workbench_enabled
        and base_dependencies.rule_workbench is None
        and base_dependencies.rule_corpus_bytes is not None
        and yara_execution_available
        and getattr(uow, "detection_candidates", None) is not None
    ):
        base_dependencies = replace(
            base_dependencies,
            rule_workbench=RuleWorkbench(
                corpus_bytes=base_dependencies.rule_corpus_bytes,
                yara_engine=base_dependencies.yara_engine,
                confined_analysis=confined_analysis,
                detection_candidates=uow.detection_candidates,
                non_discriminant_patterns=NonDiscriminantPatternRegistry(()),
                legitimate_services=load_legitimate_services(),
                policy=RuleWorkbenchPolicy(maximum_source_bytes=settings.model_output_max_bytes),
            ),
        )

    registry = _TrackingToolRegistry()
    evidence_summaries = CanonicalEvidenceSummaryPort(
        artifact_store, max_payload_bytes=settings.model_output_max_bytes
    )
    context_builder = ResearchContextBuilder(
        article_snapshots=base_dependencies.article_snapshots,
        observations=uow.evidence_observations,
        findings=uow.investigation_findings,
        registry=registry,
        evidence_summaries=evidence_summaries,
        invocations=uow.tool_invocations,
        opinions=uow.analyst_opinions,
        rule_corpus_inventory=(
            cast(RuleCorpusContextInventoryPort, base_dependencies.rule_corpus_bytes)
            if callable(
                getattr(base_dependencies.rule_corpus_bytes, "manual_rule_corpus_imports", None)
            )
            else None
        ),
    )
    graph: InvestigationGraphReadPort = RepositoryInvestigationGraphReadPort(
        investigation_id=investigation_id,
        findings=uow.investigation_findings,
        associations=uow.sample_associations,
    )
    read_port = RepositoryInvestigationReadPort(
        investigation_id=investigation_id,
        observations=uow.evidence_observations,
        article=base_dependencies.article_snapshots,
        feature_packs=base_dependencies.feature_packs,
        graph=graph,
        evidence_summary=evidence_summaries,
    )
    context_builder.register_local_tools(registry, read_port)
    dependencies = base_dependencies

    recorder: EvidenceRecorder = ProductionEvidenceRecorder(
        artifact_store,
        uow.evidence_observations,
        max_payload_bytes=settings.model_output_max_bytes,
    )
    enabled = set(dependencies.available_capabilities)
    enabled.difference_update(FAMILY_CAPABILITY_KEYS)
    enabled.add(LOCAL_READ_CAPABILITY)
    rule_tools: RuleToolExecutionPort | None = None

    if dependencies.virustotal is not None and dependencies.virustotal_observations is not None:
        vt_capabilities = dependencies.vt_capabilities or VirusTotalCapabilities()

        async def resolve_vt_article(investigation_key: UUID) -> Any:
            linked = await uow.analyst_investigations.get(investigation_key)
            if linked is None:
                raise ValueError("Investigation does not exist")
            return await dependencies.article_snapshots.resolve(linked.synthesis_artifact_id)

        register_virustotal_tools(
            registry,
            virustotal=dependencies.virustotal,
            observations=dependencies.virustotal_observations,
            recorder=recorder,
            evidence_observations=uow.evidence_observations,
            findings=uow.investigation_findings,
            article_snapshot_resolver=resolve_vt_article,
            capabilities=vt_capabilities,
            evidence_payloads=artifact_store,
            max_capture_bytes=capture_max_bytes(settings),
            max_pages=settings.virustotal_max_pages,
            page_size=settings.virustotal_default_page_size,
            max_results=min(settings.virustotal_max_results, 50),
        )
        enabled.update(virustotal_tool_capabilities(vt_capabilities))

    if dependencies.shodan is not None and dependencies.shodan_capabilities is not None:
        shodan_capabilities = dependencies.shodan_capabilities
        if shodan_tool_capabilities(shodan_capabilities):

            async def resolve_article(investigation_key: UUID) -> Any:
                linked = await uow.analyst_investigations.get(investigation_key)
                if linked is None:
                    raise ValueError("Investigation does not exist")
                return await dependencies.article_snapshots.resolve(linked.synthesis_artifact_id)

            register_shodan_tools(
                registry,
                shodan=dependencies.shodan,
                recorder=recorder,
                article_snapshots=dependencies.article_snapshots,
                observations=uow.evidence_observations,
                findings=uow.investigation_findings,
                capabilities=shodan_capabilities,
                budget_estimate=(dependencies.shodan_budget_estimate or ShodanBudgetEstimate()),
                article_snapshot_resolver=resolve_article,
                raw_archive=artifact_store,
                max_response_bytes=settings.shodan_max_response_bytes,
            )
            enabled.update(shodan_tool_capabilities(shodan_capabilities))

    if dependencies.acquisition is not None or dependencies.sample_associations is not None:
        hits = HitAcquisitionService(
            uow_factory,
            acquisition=dependencies.acquisition,
            recorder=recorder,
            novelty=ArticleNoveltyService(
                observations=uow.evidence_observations,
                investigations=uow.analyst_investigations,
                article_snapshots=dependencies.article_snapshots,
            ),
        )
        analyzer: SampleAnalyzer | None = None
        worker_analysis_available = bool(
            dependencies.confined_analysis_execution is not None
            and (
                family := dependencies.confined_analysis_execution.family_statuses.get(
                    "sample_analysis"
                )
            )
            is not None
            and family.available
        )
        if worker_analysis_available:
            analyzer = LocalSampleAnalysisService(
                hits,
                uow_factory,
                recorder,
                static_analysis=dependencies.sample_static_analysis,
                capabilities=dependencies.sample_capabilities_analysis,
                code_features=dependencies.sample_code_features,
                goodware=dependencies.sample_goodware,
                confined_analysis=dependencies.confined_analysis_execution,
                analysis_timeout_seconds=settings.analysis_worker_job_timeout_seconds,
                max_output_bytes=settings.analysis_worker_result_max_bytes,
            )
        register_acquisition_tools(
            registry,
            hits=hits,
            qualifications=SampleQualificationService(uow_factory),
            analyzer=analyzer,
        )
        if settings.virustotal_file_download_enabled and dependencies.acquisition is not None:
            enabled.add("vt.file_download")
        if analyzer is not None:
            enabled.add(SAMPLE_ANALYSIS_CAPABILITY)

    detection_candidates = getattr(uow, "detection_candidates", None)
    rule_get_ports_available = all(
        port is not None
        for port in (
            detection_candidates,
            getattr(uow, "evidence_observations", None),
            getattr(artifact_store, "read_json", None),
        )
    )
    rule_workbench_available = (
        settings.rule_workbench_enabled
        and dependencies.rule_workbench is not None
        and dependencies.rule_corpus_bytes is not None
        and detection_candidates is not None
    )
    if rule_workbench_available or rule_get_ports_available:
        rule_tools = register_rule_tools(
            registry,
            tool_names=None if rule_workbench_available else ("rule.get",),
            workbench=dependencies.rule_workbench if rule_workbench_available else None,
            candidates=detection_candidates,
            associations=uow.sample_associations if rule_workbench_available else None,
            corpus_bytes=dependencies.rule_corpus_bytes if rule_workbench_available else None,
            recorder=recorder if rule_workbench_available else None,
            artifact_store=artifact_store if rule_workbench_available else None,
            evidence_observations=(uow.evidence_observations if rule_get_ports_available else None),
            evidence_payloads=(
                cast(EvidencePayloadReader, artifact_store) if rule_get_ports_available else None
            ),
            corpus_inventory=(
                cast(Any, dependencies.rule_corpus_bytes) if rule_workbench_available else None
            ),
            article_claims=(
                cast(Any, dependencies.article_snapshots) if rule_workbench_available else None
            ),
        )
        enabled.update(
            rule_tool_capabilities(
                workbench=dependencies.rule_workbench if rule_workbench_available else None,
                candidates=detection_candidates,
                associations=uow.sample_associations if rule_workbench_available else None,
                corpus_bytes=dependencies.rule_corpus_bytes if rule_workbench_available else None,
                recorder=recorder if rule_workbench_available else None,
                evidence_observations=(
                    uow.evidence_observations if rule_get_ports_available else None
                ),
                evidence_payloads=(
                    cast(EvidencePayloadReader, artifact_store)
                    if rule_get_ports_available
                    else None
                ),
            )
        )

    if (
        dependencies.local_commands is not None
        and dependencies.local_command_catalog is not None
        and dependencies.rule_corpus_bytes is not None
    ):
        ghidra_enabled = settings.ghidra_enabled and dependencies.ghidra is not None
        register_local_analysis_tools(
            registry,
            command_service=dependencies.local_commands,
            command_catalog=dependencies.local_command_catalog,
            ghidra=dependencies.ghidra,
            sample_bytes=cast(
                SampleBytesReader,
                QuarantineSampleBytesAdapter(
                    dependencies.rule_corpus_bytes,
                    max_sample_bytes=settings.local_analysis_max_sample_bytes,
                ),
            ),
            sample_associations=uow.sample_associations,
            recorder=recorder,
            ghidra_enabled=ghidra_enabled,
            max_sample_bytes=settings.local_analysis_max_sample_bytes,
        )
        local_caps = set(
            local_tool_capabilities(
                dependencies.local_command_catalog,
                ghidra_enabled=ghidra_enabled,
            )
        )
        if not settings.local_analysis_enabled:
            local_caps.discard("local.run_command")
        enabled.update(local_caps)

    _register_similarity_tool_family(
        registry,
        dependencies=dependencies,
        recorder=recorder,
        enabled=enabled,
    )
    _register_signature_bridge_tool_family(
        registry,
        dependencies=dependencies,
        recorder=recorder,
        rule_tools=rule_tools,
        uow_factory=uow_factory,
        settings=settings,
        enabled=enabled,
    )
    _register_extraction_tool_family(
        registry,
        dependencies=dependencies,
        recorder=recorder,
        settings=settings,
        uow_factory=uow_factory,
        enabled=enabled,
    )
    _register_retrohunt_tool_family(
        registry,
        uow=uow,
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        dependencies=dependencies,
        recorder=recorder,
        enabled=enabled,
    )
    network_workbench = _register_network_tool_family(
        registry,
        dependencies=dependencies,
        recorder=recorder,
        enabled=enabled,
    )
    _register_protocol_tool_family(
        registry,
        uow=uow,
        workbench=network_workbench,
        dependencies=dependencies,
        recorder=recorder,
        enabled=enabled,
    )
    _register_native_tool_family(
        registry,
        dependencies=dependencies,
        recorder=recorder,
        settings=settings,
        uow_factory=uow_factory,
        enabled=enabled,
    )

    advisor_tool: InvestigationAdvisorTool | None = None
    if operator_led:
        registry.unregister("sample.set_qualification")
        register_operator_finding_tools(
            registry,
            findings=uow.investigation_findings,
            observations=uow.evidence_observations,
            investigations=uow.analyst_investigations,
            article_snapshots=dependencies.article_snapshots,
        )
        experiment_repository = getattr(uow, "investigation_experiments", None)
        if experiment_repository is not None:
            register_operator_experiment_tools(
                registry,
                experiments=experiment_repository,
                observations=uow.evidence_observations,
                invocations=uow.tool_invocations,
                states=uow.investigation_states,
                envelopes=uow.investigation_envelopes,
                article_snapshots=dependencies.article_snapshots,
            )
        opinion_service = AnalystOpinionService(
            uow_factory=uow_factory,
            artifact_store=artifact_store,
            claim_knowledge_provider=FindingServiceClaimKnowledgeProvider(
                uow_factory=uow_factory,
                article_snapshots=dependencies.article_snapshots,
            ),
            author_id="operator:claude-client",
        )
        register_operator_opinion_tools(
            registry,
            service=opinion_service,
            investigations=uow.analyst_investigations,
            article_snapshots=dependencies.article_snapshots,
            protocol_figure_builder=OperatorProtocolFigureAttachmentBuilder(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
            ),
        )
        register_advisor_reparse_tool(
            registry,
            adapter=ReparsePersistedAdvisorOutputTool(
                observations=uow.evidence_observations,
                blobs=getattr(uow, "blobs", None),
                artifact_store=artifact_store,
                recorder=recorder,
                settings=settings,
            ),
        )

    # Operator-led mode can reuse its turn transport. A Bridge-led composition
    # must inject a separate authority-bound advisor transport explicitly so an
    # ordinary investigator turn transport is never treated as authorization.
    advisor_transport = advisor_turn_transport
    if advisor_transport is None and operator_led:
        advisor_transport = turn_transport
    advisor_unavailable_reason = "bridge_transport_unavailable"
    if advisor_transport is not None and not operator_led and not getattr(
        advisor_transport, "requires_external_effect_authority", False
    ):
        advisor_transport = None
        advisor_unavailable_reason = "external_effect_authority_required"
    if advisor_transport is not None:
        advisor_tool = InvestigationAdvisorTool(
            transport=advisor_transport,
            settings=settings,
            investigations=uow.analyst_investigations,
            observations=uow.evidence_observations,
            findings=uow.investigation_findings,
            investigation_states=uow.investigation_states,
            experiments=getattr(uow, "investigation_experiments", None),
            source_documents=getattr(uow, "source_documents", None),
            opinions=uow.analyst_opinions,
            sample_associations=uow.sample_associations,
            samples=getattr(uow, "samples", None),
            blobs=getattr(uow, "blobs", None),
            article_snapshots=dependencies.article_snapshots,
            artifact_store=artifact_store,
            registry=registry,
        )
        register_investigation_advisor_tool(registry, adapter=advisor_tool)

    if tool_registrar is not None:
        tool_registrar(registry, dependencies)

    registered_operator_capabilities = {
        definition.capability_key
        for definition in getattr(registry, "definitions", ())
        if definition.name in OPERATOR_WRITE_TOOL_NAMES | {"advisor.consult"}
    }
    available_capabilities = frozenset(enabled | registered_operator_capabilities)
    unavailable_tool_reasons = dict(
        _unavailable_tool_reasons(
            OPTIONAL_TOOL_NAMES,
            dependencies=dependencies,
            settings=settings,
        )
    )
    if not operator_led:
        for name in (
            "finding.propose",
            "finding.revise",
            "opinion.compose",
            "opinion.revise",
            "opinion.note",
        ):
            unavailable_tool_reasons[name] = "operator_mode_required"
    else:
        unavailable_tool_reasons["sample.set_qualification"] = "human_qualification_required"
    if advisor_tool is None:
        unavailable_tool_reasons["advisor.consult"] = advisor_unavailable_reason
    registry.set_availability_catalog(OPTIONAL_TOOL_NAMES, unavailable_tool_reasons)
    availability_context = ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="composition-tool-availability",
        available_capabilities=available_capabilities,
        unavailable_tool_reasons=unavailable_tool_reasons,
    )
    tool_availability = project_tool_availability(
        registry,
        availability_context,
        known_tool_names=OPTIONAL_TOOL_NAMES,
    )
    available_tools = tool_availability.available_names
    unavailable_tools = tuple(name for name, _reason in tool_availability.unavailable)
    bridge_transport = _PermissionAwareInvestigationTransport(
        turn_transport or ModelConversationTurnTransport(model_conversations)
    )
    commit_uow = getattr(uow, "commit", None)
    rollback_uow = getattr(uow, "rollback", None)
    osint_open_policy_service = OsintOpenPolicyService(uow_factory, settings)
    service = InvestigationService(
        post_synthesis=AnalystPostSynthesisService(
            uow_factory,
            artifact_store,
            loop_budget_factory=lambda: loop_budget_from_settings(settings),
        ),
        investigations=uow.analyst_investigations,
        states=uow.investigation_states,
        context_snapshots=ProductionArtifactContextSnapshotRepository(artifact_store),
        envelopes=uow.investigation_envelopes,
        context_builder=context_builder,
        bridge=BridgeInvestigator(bridge_transport),
        registry=registry,
        dispatcher=RegistryToolDispatcher(registry),
        invocations=uow.tool_invocations,
        observations=uow.evidence_observations,
        findings=uow.investigation_findings,
        opinions=uow.analyst_opinions,
        sample_associations=uow.sample_associations,
        samples=getattr(uow, "samples", None),
        source_documents=getattr(uow, "source_documents", None),
        budget_snapshots=getattr(uow, "investigation_budgets", None),
        observation_writer=ProductionArtifactObservationWriter(
            artifact_store, uow.evidence_observations
        ),
        artifact_store=artifact_store,
        evidence_summaries=evidence_summaries,
        available_capabilities=available_capabilities,
        no_progress_limit=settings.investigation_no_progress_limit,
        commit_uow=commit_uow if callable(commit_uow) else None,
        rollback_uow=rollback_uow if callable(rollback_uow) else None,
        effective_policy_for=osint_open_policy_service.effective_for_investigation,
    )
    return InvestigationComposition(
        service=service,
        operator_tool_invocations=OperatorToolInvocationService(
            investigation_id=investigation_id,
            investigations=uow.analyst_investigations,
            states=uow.investigation_states,
            envelopes=uow.investigation_envelopes,
            invocations=uow.tool_invocations,
            context_builder=context_builder,
            execution=service,
            registry=registry,
            available_capabilities=available_capabilities,
            unavailable_tool_reasons=unavailable_tool_reasons,
            observations=uow.evidence_observations,
            evidence_summaries=evidence_summaries,
            effective_policy_for=osint_open_policy_service.effective_for_investigation,
        ),
        osint_open_policy_service=osint_open_policy_service,
        registry=registry,
        tool_dependencies=dependencies,
        available_capabilities=available_capabilities,
        available_tools=available_tools,
        unavailable_tools=unavailable_tools,
        unavailable_tool_reasons=unavailable_tool_reasons,
        advisor_tool=advisor_tool,
    )


def _unavailable_tool_reasons(
    names: tuple[str, ...],
    *,
    dependencies: InvestigationToolDependencies,
    settings: Settings,
) -> Mapping[str, str]:
    """Provide bounded reason codes for optional tools absent from the registry."""
    execution = dependencies.confined_analysis_execution
    family_statuses = execution.family_statuses if execution is not None else {}

    def reason_code(value: str | None, fallback: str) -> str:
        if not isinstance(value, str):
            return fallback
        normalized = "_".join(value.strip().lower().split())
        if (
            len(normalized) <= 96
            and normalized
            and all(char in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for char in normalized)
        ):
            return normalized
        return fallback

    def worker_reason(family: str, fallback: str) -> str:
        status = family_statuses.get(family)
        if status is not None and not status.available and status.available_reason:
            return reason_code(status.available_reason, fallback)
        if execution is not None and not execution.available:
            return reason_code(execution.available_reason, fallback)
        return fallback

    reasons: dict[str, str] = {}
    for name in names:
        if name == "sample.analyze":
            reason = worker_reason("sample_analysis", "sample_analysis_unavailable")
        elif name == "sample.extract_obfuscated_strings":
            reason = worker_reason("floss", "floss_unavailable")
        elif name == "sample.extract_configuration":
            reason = worker_reason("mwcp", "configuration_extraction_unavailable")
        elif name == "local.run_command":
            reason = (
                "local_analysis_disabled"
                if not settings.local_analysis_enabled
                else worker_reason("local_command", "local_command_unavailable")
            )
        elif name.startswith("rule."):
            if not settings.rule_workbench_enabled:
                reason = "rule_workbench_disabled"
            elif dependencies.rule_corpus_bytes is None:
                reason = "rule_corpus_unavailable"
            else:
                reason = worker_reason("yara", "rule_workbench_unavailable")
        elif name == "network.capture.inspect":
            reason = (
                "network_capture_source_unavailable"
                if dependencies.network_quarantine is None
                else worker_reason("tshark", "tshark_unavailable")
            )
        elif name == "network.suricata_replay":
            reason = worker_reason("suricata", "suricata_replay_unavailable")
        elif name == "network.suricata_validate_rules":
            reason = worker_reason("suricata", "suricata_validate_rules_unavailable")
        elif name.startswith("network."):
            reason = "tshark_unavailable" if dependencies.tshark is None else "network_unavailable"
        elif name.startswith("ghidra."):
            reason = "ghidra_disabled" if not settings.ghidra_enabled else "ghidra_unavailable"
        elif name.startswith("vt.retrohunt."):
            reason = "retrohunt_unavailable"
        elif name.startswith("vt."):
            reason = "virustotal_unavailable"
        elif name.startswith("shodan."):
            reason = "shodan_unavailable"
        elif name == NATIVE_TOOL_NAME:
            reason = "native_analysis_unavailable"
        elif name in DOTNET_TOOL_NAMES:
            reason = worker_reason("dotnet", "dotnet_ilspy_unavailable")
        elif name == "sandbox.report.import":
            reason = "behavior_report_import_unavailable"
        elif name.startswith(("features.", "samples.")) or name == "signatures.candidates":
            reason = "sample_similarity_unavailable"
        elif name == "sample.set_qualification":
            reason = "sample_association_unavailable"
        else:
            reason = "dependency_unavailable"
        reasons[name] = reason_code(reason, "dependency_unavailable")
    return reasons


def build_investigation_service(
    uow: UnitOfWork,
    **kwargs: Any,
) -> InvestigationService:
    """Build the service with all repositories and production ports from the UoW."""

    return build_investigation_composition(uow, **kwargs).service


@dataclass(frozen=True, slots=True)
class InvestigationCompositionFactory:
    uow_factory: UnitOfWorkFactory
    artifact_store: ProductionArtifactStore
    model_conversations: ModelConversationService
    settings: Settings
    optional_tool_ports: OptionalInvestigationToolPorts | None = None
    tool_registrar: ToolRegistrar | None = None
    turn_transport: InvestigationTurnTransport | None = None
    advisor_turn_transport: InvestigationTurnTransport | None = None
    tool_dependencies: InvestigationToolDependencies | None = None

    @property
    def article_snapshots(self) -> ProductionArticleSnapshotAdapter:
        return ProductionArticleSnapshotAdapter(self.uow_factory, self.artifact_store)

    @property
    def osint_open_policy_service(self) -> OsintOpenPolicyService:
        return OsintOpenPolicyService(self.uow_factory, self.settings)

    def build(
        self,
        uow: UnitOfWork,
        investigation_id: UUID,
        *,
        operator_mode: bool = False,
    ) -> InvestigationComposition:
        return build_investigation_composition(
            uow,
            uow_factory=self.uow_factory,
            artifact_store=self.artifact_store,
            model_conversations=self.model_conversations,
            settings=self.settings,
            investigation_id=investigation_id,
            tool_dependencies=self.tool_dependencies,
            optional_tool_ports=self.optional_tool_ports,
            tool_registrar=self.tool_registrar,
            turn_transport=self.turn_transport,
            advisor_turn_transport=self.advisor_turn_transport,
            operator_mode=operator_mode,
        )
