import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

import httpx
from fastapi import FastAPI, Request
from minio import Minio

from cti_app.api.analyst_opinion import router as analyst_opinion_router
from cti_app.api.collection import router as collection_router
from cti_app.api.detection import router as detection_router
from cti_app.api.discovery import candidate_router as discovery_candidate_router
from cti_app.api.discovery import router as discovery_router
from cti_app.api.discovery_merge import merge_runs_router
from cti_app.api.discovery_recovery import router as discovery_recovery_router
from cti_app.api.editions import router as editions_router
from cti_app.api.fusion import fusion_router
from cti_app.api.health import router as health_router
from cti_app.api.investigation import router as investigation_router
from cti_app.api.jobs import router as jobs_router
from cti_app.api.model_conversations import router as model_conversations_router
from cti_app.api.pdf_inspection import router as pdf_inspection_router
from cti_app.api.production import router as production_router
from cti_app.api.production_control import router as production_control_router
from cti_app.api.publication import router as publication_router
from cti_app.api.selection import selection_router
from cti_app.api.subject_content import router as subject_content_router
from cti_app.api.subjects import router as subjects_router
from cti_app.api.typst_input_inspection import router as typst_input_inspection_router
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.capabilities import CapabilitiesService
from cti_app.application.code_features import CodeFeatureService
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.collection_review import CollectionReviewService
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.discovery.cumulative.chatgpt_planner import ChatGptMergePlanner
from cti_app.application.discovery.cumulative.contracts import ReconcileDiscoveryParameters
from cti_app.application.discovery.cumulative.jobs import (
    RECONCILE_DISCOVERY_JOB_KIND,
    ensure_discovery_reconciliation_job,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.fusion import FusionService
from cti_app.application.discovery.manual_source_edits import ManualSourceEditService
from cti_app.application.discovery.runs import DiscoveryRunService
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_preview import EditionPreviewPdfRenderer, EditionPreviewService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
)
from cti_app.application.edition_release_materialization import (
    EditionReleaseRematerializationService,
)
from cti_app.application.edition_rendering import EditionRenderService
from cti_app.application.edition_review import EditionRepairReadService, EditionReviewService
from cti_app.application.edition_rule_archive import EditionRuleArchiveService
from cti_app.application.edition_typst_rendering import EditionTypstRenderer
from cti_app.application.edition_workspace import (
    EditionProductionCheckpointService,
    EditionWorkspaceMaterializer,
)
from cti_app.application.editions import EditionService
from cti_app.application.goodware import GoodwareMeasurementService
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.investigation_composition import (
    InvestigationCompositionFactory,
    OptionalInvestigationToolPorts,
    QuarantineSampleBytesAdapter,
)
from cti_app.application.investigation_jobs import make_investigation_runner
from cti_app.application.investigation_rule_tools import QuarantineRuleCorpusBytes
from cti_app.application.jobs import DuplicateJobError, JobService, create_job_registry
from cti_app.application.local_analysis import LocalCommandCatalog, LocalCommandService
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.pdf_inspection import PdfInspectionService
from cti_app.application.persistence import UnitOfWork
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_control_view import ProductionControlViewService
from cti_app.application.production_jobs import ProductionStageChain
from cti_app.application.production_pacing import ProductionPacingPolicy
from cti_app.application.production_repair_payloads import ProductionRepairPayloadResolver
from cti_app.application.production_repairs import (
    ProductionReferenceRepairService,
    ProductionRepairAdjudicationService,
    ProductionRepairDecisionService,
    ProductionRepairIssueService,
    ProductionRepairMaterializationService,
    ProductionRepairProjectionService,
)
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.selection import SelectionService
from cti_app.application.static_analysis import StaticAnalysisService
from cti_app.application.subject_content import SubjectContentService
from cti_app.application.subjects import SubjectService
from cti_app.application.typst_input_inspection import RenderInputInspectionService
from cti_app.application.typst_paths import typst_bundle_paths
from cti_app.application.typst_rendering import TypstRenderer
from cti_app.application.virustotal import VirusTotalCapabilities, VirusTotalRoutingPolicy
from cti_app.application.virustotal_persistence import VirusTotalObservationService
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.config import get_settings
from cti_app.infrastructure.analytic_chart_renderer import AnalyticChartRenderer
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.infrastructure.d2_diagram_compiler import D2DiagramCompiler
from cti_app.infrastructure.database.repositories.pdf_inspection import (
    SqlAlchemyPdfInspectionSourceRepository,
)
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.ghidra_mcp import create_ghidra_mcp_http_adapter
from cti_app.infrastructure.health import InfrastructureReadinessChecker
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport
from cti_app.infrastructure.jobs import DramatiqJobDispatcher
from cti_app.infrastructure.local_command_runner import LocalCommandRunner
from cti_app.infrastructure.pdf_inspection import PdfInspectionCache, PopplerSubprocess
from cti_app.infrastructure.shodan import create_shodan_http_adapter, create_shodan_http_client
from cti_app.infrastructure.smda import SmdaAdapter
from cti_app.infrastructure.typst_compiler import TypstSubprocessCompiler
from cti_app.infrastructure.virustotal import (
    VirusTotalHttpAdapter,
    create_virustotal_direct_http_client,
    create_virustotal_http_client,
)
from cti_app.infrastructure.yara_runner import IsolatedYaraRunner
from cti_app.integrations.model_factory import (
    create_bridge_capabilities_provider,
    create_model_gateway,
)
from cti_app.logging import (
    CorrelationIdMiddleware,
    configure_logging,
    get_correlation_id,
    request_query_parameter_names,
)

settings = get_settings()
configure_logging(settings.log_level)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    readiness = InfrastructureReadinessChecker(settings)
    production_pacing = ProductionPacingPolicy.from_settings(settings)
    job_engine = create_postgres_engine(settings.postgres_dsn)
    session_factory = create_session_factory(job_engine)

    def uow_factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    model_gateway = create_model_gateway(settings, uow_factory)
    vt_proxy_client: httpx.AsyncClient | None = None
    vt_direct_client: httpx.AsyncClient | None = None
    shodan_client: httpx.AsyncClient | None = None
    ghidra_adapter = None
    vt_capabilities = VirusTotalCapabilities(
        file_report=settings.virustotal_file_report_enabled,
        file_relationships=settings.virustotal_object_relationships_enabled,
        intelligence_search=settings.virustotal_intelligence_search_enabled,
        file_download=settings.virustotal_file_download_enabled,
        behaviour_reports=settings.virustotal_behaviour_reports_enabled,
    )
    vt_enabled = any(
        (
            settings.virustotal_file_report_enabled,
            settings.virustotal_object_relationships_enabled,
            settings.virustotal_intelligence_search_enabled,
            settings.virustotal_behaviour_reports_enabled,
            settings.virustotal_file_download_enabled,
        )
    )
    vt_adapter = None
    if vt_enabled:
        vt_proxy_client = (
            create_virustotal_http_client(settings)
            if settings.virustotal_proxy_url
            else httpx.AsyncClient()
        )
        if settings.virustotal_api_key and settings.virustotal_file_report_legacy_fallback_enabled:
            vt_direct_client = create_virustotal_direct_http_client(settings)
        vt_adapter = VirusTotalHttpAdapter(
            client=vt_proxy_client,
            base_url=settings.virustotal_base_url,
            fallback_base_url=settings.virustotal_fallback_base_url,
            legacy_base_url=settings.virustotal_legacy_base_url,
            direct_client=vt_direct_client,
            api_key=(
                settings.virustotal_api_key.get_secret_value()
                if vt_direct_client is not None and settings.virustotal_api_key is not None
                else None
            ),
            capabilities=vt_capabilities,
            file_report_proxy_fallback_enabled=settings.virustotal_file_report_proxy_fallback_enabled,
            file_report_legacy_fallback_enabled=settings.virustotal_file_report_legacy_fallback_enabled,
            routing_policy=(
                None if settings.virustotal_proxy_url else VirusTotalRoutingPolicy(routes={})
            ),
            max_response_bytes=settings.virustotal_max_response_bytes,
            default_page_size=settings.virustotal_default_page_size,
            max_page_size=settings.virustotal_max_page_size,
            max_pages=settings.virustotal_max_pages,
            max_results=settings.virustotal_max_results,
            file_download_enabled=settings.virustotal_file_download_enabled,
        )
    shodan_enabled = settings.shodan_api_key is not None and any(
        (
            settings.shodan_host_enabled,
            settings.shodan_search_enabled,
            settings.shodan_domain_enabled,
            settings.shodan_count_enabled,
        )
    )
    shodan_adapter = None
    if shodan_enabled:
        shodan_client = create_shodan_http_client(settings)
        shodan_adapter = create_shodan_http_adapter(client=shodan_client, settings=settings)
    if settings.ghidra_enabled:
        ghidra_adapter = create_ghidra_mcp_http_adapter(settings)
    blob_store = MinioBlobStore(
        Minio(
            settings.s3_endpoint,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=settings.s3_secure,
        ),
        physical_bucket=settings.s3_bucket,
    )
    selection_service = SelectionService(
        uow_factory,
        materializer=SubjectWorkspaceMaterializer(blob_store),
        workspace_root=settings.subject_workspace_root,
    )
    production_diagnostics = DiagnosticsLog.from_env(settings.diagnostics_log_root)
    # Exactly one bridge capabilities provider for this process: it also
    # doubles as the ConversationSessionCloser (archive_conversation) that
    # closes the exact live Temporary Chat browser session — construct it
    # once and inject the same instance everywhere it's needed.
    bridge_provider = create_bridge_capabilities_provider(settings)
    cumulative_discovery_service = CumulativeDiscoveryService(
        uow_factory,
        planner=ChatGptMergePlanner(
            model_gateway,
            bridge_capabilities_provider=bridge_provider,
        ),
        diagnostics=production_diagnostics,
    )
    job_service: JobService
    job_dispatcher: DramatiqJobDispatcher

    async def enqueue_discovery_reconciliation(
        batch: object, input_mode: object, actor_id: str
    ) -> object:
        from cti_app.domain.discovery import DiscoveryBatch
        from cti_app.domain.discovery_cumulative import DiscoveryInputMode

        if not isinstance(batch, DiscoveryBatch) or not isinstance(input_mode, DiscoveryInputMode):
            raise TypeError("Invalid discovery reconciliation request")
        return await ensure_discovery_reconciliation_job(
            batch,
            input_mode=input_mode,
            actor_id=actor_id,
            cumulative_discovery_service=cumulative_discovery_service,
            job_service=job_service,
            job_dispatcher=job_dispatcher,
            correlation_id=get_correlation_id(),
        )

    async def replan_discovery_intake(parameters: ReconcileDiscoveryParameters) -> object:
        # Parent snapshot is part of the key: the bare key was already claimed by this
        # intake's first reconciliation, so a retry against a moved-on state needs a new one.
        parent = parameters.expected_parent_snapshot_id
        key = f"reconcile-discovery:{parameters.intake_id}:{parent or 'root'}"
        try:
            job = await job_service.submit(
                kind=RECONCILE_DISCOVERY_JOB_KIND,
                aggregate_type="edition",
                aggregate_id=parameters.edition_id,
                idempotency_key=key,
                correlation_id=get_correlation_id(),
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=3,
                actor_id=parameters.actor_id,
            )
            await job_dispatcher.dispatch(job.id)
            return job
        except DuplicateJobError as exc:
            return await job_service.get(exc.existing_job_id)

    cumulative_discovery_service.set_replan_intake(replan_discovery_intake)
    fusion_service = FusionService(
        uow_factory,
        replan_intake=replan_discovery_intake,
    )

    discovery_service = DiscoveryService(
        uow_factory,
        model_gateway,
        archive=model_gateway,
        bridge_capabilities_provider=bridge_provider,
        after_persisted_batch=enqueue_discovery_reconciliation,
        background_poll_interval_seconds=settings.discovery_bridge_poll_interval_seconds,
    )
    manual_source_edit_service = ManualSourceEditService(
        uow_factory, model_gateway, cumulative_discovery_service
    )
    collection_service = SubjectCollectionService(
        uow_factory,
        SafeHttpCollector(
            AsyncioPinnedHttpTransport(),
            SystemDnsResolver(),
            CollectionPolicy(
                max_redirects=settings.collection_max_redirects,
                timeout_seconds=settings.collection_timeout_seconds,
                max_download_bytes=settings.collection_max_download_bytes,
                max_expanded_bytes=settings.collection_max_expanded_bytes,
                max_decompression_ratio=settings.collection_max_decompression_ratio,
                allowed_domains=parse_domain_policy(settings.collection_allowed_domains),
                blocked_domains=parse_domain_policy(settings.collection_blocked_domains),
            ),
        ),
        blob_store,
        fetch_lease_seconds=settings.collection_fetch_lease_seconds,
        workspace_materializer=SubjectWorkspaceMaterializer(blob_store),
        workspace_root=settings.subject_workspace_root,
    )
    collection_review_service = CollectionReviewService(
        uow_factory,
        blob_store,
    )
    model_conversation_service = ModelConversationService(
        uow_factory,
        model_gateway,
        blob_store,
        retention_days=settings.model_conversation_retention_days,
        conversation_session_closer=bridge_provider,
    )

    blob_catalog = BlobCatalogService(blob_store, uow_factory)
    production_artifact_store = ProductionArtifactStore(blob_catalog)
    vt_observations = (
        VirusTotalObservationService(blob_catalog, uow_factory) if vt_adapter is not None else None
    )
    quarantine_bytes = None
    local_command_catalog = None
    local_command_service = None
    static_analysis = None
    capabilities_analysis = None
    code_features = None
    goodware_measurement = None
    yara_engine = None
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
        local_command_catalog = LocalCommandCatalog(
            timeout_seconds=settings.local_analysis_timeout_seconds,
            max_output_bytes=settings.local_analysis_max_output_bytes,
            cpu_limit_seconds=settings.local_analysis_cpu_limit_seconds,
            memory_limit_bytes=settings.local_analysis_memory_limit_bytes,
            file_size_limit_bytes=settings.local_analysis_file_size_limit_bytes,
        )
        assert quarantine_bytes is not None
        local_command_service = LocalCommandService(
            QuarantineSampleBytesAdapter(
                quarantine_bytes,
                max_sample_bytes=settings.local_analysis_max_sample_bytes,
            ),
            LocalCommandRunner(max_sample_bytes=settings.local_analysis_max_sample_bytes),
            local_command_catalog,
            enabled=settings.local_analysis_enabled,
            max_sample_bytes=settings.local_analysis_max_sample_bytes,
        )
    if settings.local_analysis_enabled:
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
        goodware_measurement = GoodwareMeasurementService(
            blob_store,
            uow_factory,
            cache_root=settings.goodware_cache_root,
        )
    if settings.rule_workbench_enabled:
        yara_engine = IsolatedYaraRunner()
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
    investigation_composition_factory = InvestigationCompositionFactory(
        uow_factory=uow_factory,
        artifact_store=production_artifact_store,
        model_conversations=model_conversation_service,
        settings=settings,
        optional_tool_ports=OptionalInvestigationToolPorts(
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
        ),
    )
    investigation_runner = make_investigation_runner(
        uow_factory,
        investigation_composition_factory,
    )
    pdf_inspection_cache = PdfInspectionCache(settings.pdf_inspection_cache_root)
    pdf_inspection_source_repository = SqlAlchemyPdfInspectionSourceRepository(session_factory)
    pdf_inspection_service = PdfInspectionService(
        pdf_inspection_source_repository,
        production_artifact_store,
        PopplerSubprocess(),
        pdf_inspection_cache,
    )
    render_input_inspection_service = RenderInputInspectionService(
        pdf_inspection_source_repository,
        production_artifact_store,
        pdf_inspection_service,
        pdf_inspection_cache,
    )
    production_checkpoint = EditionProductionCheckpointService(
        uow_factory,
        production_artifact_store,
        settings.edition_workspace_root,
        diagnostics=production_diagnostics,
    )
    edition_workspace_materializer = EditionWorkspaceMaterializer(settings.edition_workspace_root)
    edition_release_rematerializer = EditionReleaseRematerializationService(
        uow_factory,
        production_artifact_store,
        edition_workspace_materializer,
    )
    publication_assembly = EditionAssemblyService(
        uow_factory,
        production_artifact_store,
    )
    chp_typst_root, font_bundle_root, typst_fonts_lock_path = typst_bundle_paths()
    typst_compiler = TypstSubprocessCompiler()
    media_asset_store = MediaAssetStore(production_artifact_store, uow_factory)
    edition_typst_renderer = EditionTypstRenderer()
    edition_render_service = EditionRenderService(
        uow_factory=uow_factory,
        artifact_store=production_artifact_store,
        media_asset_store=media_asset_store,
        renderer=edition_typst_renderer,
        compiler=typst_compiler,
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
        mapping_cache=pdf_inspection_cache,
    )
    edition_preview_pdf_renderer = EditionPreviewPdfRenderer(
        renderer=edition_typst_renderer,
        media_asset_store=media_asset_store,
        compiler=typst_compiler,
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
        mapping_cache=pdf_inspection_cache,
    )
    publication_render_service = PublicationRenderService(
        uow_factory=uow_factory,
        artifact_store=production_artifact_store,
        media_asset_store=media_asset_store,
        renderer=TypstRenderer(),
        compiler=typst_compiler,
        chp_typst_root=chp_typst_root,
        font_bundle_root=font_bundle_root,
        typst_fonts_lock_path=typst_fonts_lock_path,
        mapping_cache=pdf_inspection_cache,
    )
    subject_content_service = SubjectContentService(uow_factory, production_artifact_store)
    production_chain = ProductionStageChain(production_pacing)
    job_dispatcher = DramatiqJobDispatcher()
    registry = create_job_registry(
        model_gateway,
        discovery_service,
        collection_service,
        uow_factory,
        production_chain=production_chain,
        production_artifact_store=production_artifact_store,
        production_diagnostics=production_diagnostics,
        cumulative_discovery_service=cumulative_discovery_service,
        production_checkpoint=production_checkpoint,
        publication_assembly=publication_assembly,
        edition_render_service=edition_render_service,
        job_dispatcher=job_dispatcher,
        bridge_transport=bridge_provider,
        production_diagram_compiler=D2DiagramCompiler(),
        production_chart_compiler=AnalyticChartRenderer(),
        edition_release_rematerializer=edition_release_rematerializer,
        investigation_runner=investigation_runner,
    )
    app.state.readiness = readiness
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = production_artifact_store
    app.state.pdf_inspection_service = pdf_inspection_service
    app.state.render_input_inspection_service = render_input_inspection_service
    app.state.media_asset_store = media_asset_store
    app.state.subject_content_service = subject_content_service
    app.state.publication_render_service = publication_render_service
    app.state.production_diagnostics = production_diagnostics
    app.state.production_pacing = production_pacing
    job_service = JobService(uow_factory, registry)
    discovery_run_service = DiscoveryRunService(uow_factory, job_service, job_dispatcher)
    # Registry must exist before the service consuming it, so bind only once both are ready.
    production_chain.bind(job_service, job_dispatcher)
    app.state.job_service = job_service
    app.state.job_dispatcher = job_dispatcher
    app.state.edition_service = EditionService(uow_factory)
    app.state.subject_service = SubjectService(uow_factory)
    app.state.identity_provider = LocalIdentityProvider()
    app.state.model_gateway = model_gateway
    app.state.bridge_capabilities_provider = bridge_provider
    app.state.model_conversation_service = model_conversation_service
    app.state.investigation_composition_factory = investigation_composition_factory
    app.state.discovery_service = discovery_service
    app.state.discovery_run_service = discovery_run_service
    app.state.cumulative_discovery_service = cumulative_discovery_service
    app.state.fusion_service = fusion_service
    app.state.selection_service = selection_service
    app.state.manual_source_edit_service = manual_source_edit_service
    app.state.collection_service = collection_service
    app.state.collection_review_service = collection_review_service
    # One resolver for the detail, the adjudication and the projection: an
    # analyst can only include a value the projection will find again.
    repair_payload_resolver = ProductionRepairPayloadResolver(model_gateway)
    app.state.production_repair_payload_resolver = repair_payload_resolver
    production_repair_issue_service = ProductionRepairIssueService(
        uow_factory, production_artifact_store, repair_payload_resolver
    )
    app.state.production_repair_issue_service = production_repair_issue_service
    app.state.edition_composition_service = EditionCompositionService(
        uow_factory, repair_issue_reader=production_repair_issue_service
    )
    app.state.edition_review_service = EditionReviewService(
        uow_factory, production_repair_issue_service
    )
    app.state.edition_repair_read_service = EditionRepairReadService(
        uow_factory, production_repair_issue_service
    )
    app.state.production_repair_decision_service = ProductionRepairDecisionService(uow_factory)
    app.state.production_reference_repair_service = ProductionReferenceRepairService(
        uow_factory, production_artifact_store
    )
    app.state.production_repair_projection_service = ProductionRepairProjectionService(
        uow_factory, production_artifact_store, payload_resolver=repair_payload_resolver
    )
    app.state.production_checkpoint = production_checkpoint
    app.state.production_repair_materialization_service = ProductionRepairMaterializationService(
        uow_factory,
        projection_service=app.state.production_repair_projection_service,
        checkpoint_service=production_checkpoint,
        artifact_store=production_artifact_store,
        diagnostics=production_diagnostics,
    )
    app.state.production_repair_adjudication_service = ProductionRepairAdjudicationService(
        uow_factory,
        production_repair_issue_service,
        app.state.production_repair_decision_service,
        production_artifact_store,
    )
    app.state.edition_publication_service = EditionPublicationService(
        uow_factory,
        production_artifact_store,
        job_service=job_service,
        job_dispatcher=job_dispatcher,
        repair_issue_reader=production_repair_issue_service,
    )
    app.state.production_control_view_service = ProductionControlViewService(
        uow_factory,
        repair_issue_service=production_repair_issue_service,
        review_service=app.state.edition_review_service,
        publication_service=app.state.edition_publication_service,
    )
    app.state.edition_preview_service = EditionPreviewService(
        uow_factory,
        production_artifact_store,
        repair_issue_reader=production_repair_issue_service,
        pdf_renderer=edition_preview_pdf_renderer,
    )
    app.state.edition_rule_archive_service = EditionRuleArchiveService(
        uow_factory,
        production_artifact_store,
    )
    app.state.edition_release_rematerializer = edition_release_rematerializer
    yield
    await readiness.close()
    if vt_direct_client is not None:
        await vt_direct_client.aclose()
    if vt_proxy_client is not None:
        await vt_proxy_client.aclose()
    if shodan_client is not None:
        await shodan_client.aclose()
    if ghidra_adapter is not None:
        await ghidra_adapter.aclose()
    await job_engine.dispose()


def create_app() -> FastAPI:
    application = FastAPI(title="CTI Bulletin API", version="0.1.0", lifespan=lifespan)
    settings = get_settings()
    request_failures = DiagnosticsLog.from_env(settings.diagnostics_log_root)

    def record_request_failure(request: Request, error: BaseException) -> None:
        # Keyed by correlation id, not a domain id: at this level that's all we know, and
        # it's what the browser traces back through. The endpoint's own event carries the rest.
        request_failures.record_failure(
            event="http.request_failed",
            run_id=uuid5(NAMESPACE_URL, f"http-request:{get_correlation_id()}"),
            stage="http",
            correlation_id=get_correlation_id(),
            error=error,
            http_method=request.method,
            http_path=request.url.path,
            http_query=request_query_parameter_names(request),
        )

    application.add_middleware(CorrelationIdMiddleware, on_failure=record_request_failure)
    application.include_router(health_router)
    application.include_router(editions_router)
    application.include_router(discovery_router)
    application.include_router(discovery_candidate_router)
    application.include_router(discovery_recovery_router)
    application.include_router(merge_runs_router)
    application.include_router(fusion_router)
    application.include_router(jobs_router)
    application.include_router(collection_router)
    application.include_router(model_conversations_router)
    application.include_router(investigation_router)
    application.include_router(production_router)
    application.include_router(production_control_router)
    application.include_router(publication_router)
    application.include_router(pdf_inspection_router)
    application.include_router(typst_input_inspection_router)
    application.include_router(selection_router)
    application.include_router(subjects_router)
    application.include_router(subject_content_router)
    application.include_router(detection_router)
    application.include_router(analyst_opinion_router)
    return application


app = create_app()
