from typing import cast

from minio import Minio

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.model_gateway import (
    ModelGateway,
    ModelRouter,
    ModelRoutingHint,
    ModelRunUnitOfWorkFactory,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.model_runs import ModelBackend, ModelProvider
from cti_app.infrastructure.blob_storage.minio import MinioBlobStore
from cti_app.integrations.models import (
    BlobModelOutputStore,
    ChatGPTBridgeClient,
    FakeModelAdapter,
    HttpChatCompletionsTransport,
    OpenAICompatibleChatAdapter,
    OpenAIResearchAdapter,
    OpenAIStructuredAdapter,
    QwenAdapter,
)


def create_model_gateway(settings: Settings, uow_factory: UnitOfWorkFactory) -> ModelGateway:
    if settings.model_force_adapter != "auto" and settings.app_env != "development":
        raise ValueError("A forced model adapter is allowed only in development")
    bridge_transport = ChatGPTBridgeClient(
        settings.openai_bridge_base_url,
        api_key=_secret_value(settings.openai_bridge_api_key),
        timeout_seconds=settings.model_request_timeout_seconds,
        connect_timeout_seconds=settings.openai_bridge_connect_timeout_seconds,
        capabilities_timeout_seconds=settings.openai_bridge_capabilities_timeout_seconds,
        max_attempts=settings.openai_bridge_max_attempts,
    )
    qwen_transport = HttpChatCompletionsTransport(
        settings.qwen_base_url,
        api_key=_secret_value(settings.qwen_api_key),
        timeout_seconds=settings.model_request_timeout_seconds,
        provider="qwen",
    )
    webai_transport = HttpChatCompletionsTransport(
        settings.webai_base_url,
        api_key=_secret_value(settings.webai_api_key),
        timeout_seconds=settings.model_request_timeout_seconds,
        provider="gemini",
    )
    openai_research = OpenAIResearchAdapter(bridge_transport, model=settings.openai_research_model)
    openai_structured = OpenAIStructuredAdapter(
        bridge_transport, model=settings.openai_structured_model
    )
    openai_drafting = OpenAIResearchAdapter(bridge_transport, model=settings.openai_drafting_model)
    openai_critic = OpenAIResearchAdapter(bridge_transport, model=settings.openai_critic_model)
    qwen = QwenAdapter(
        qwen_transport,
        model=settings.qwen_model,
        is_external=settings.qwen_is_external,
    )
    gemini = OpenAICompatibleChatAdapter(
        webai_transport,
        provider=ModelProvider.GEMINI,
        backend=ModelBackend.GEMINI_WEBAI,
        model=settings.webai_model,
        is_external=settings.webai_is_external,
    )
    fake = FakeModelAdapter()
    force_aliases = {"openai": "chatgpt_bridge", "gemini": "gemini_webai"}
    forced = (
        ModelBackend(force_aliases.get(settings.model_force_adapter, settings.model_force_adapter))
        if settings.model_force_adapter != "auto"
        else None
    )
    routing = {
        hint: ModelBackend(value)
        for hint, value in {
            ModelRoutingHint.WEB_RESEARCH: settings.model_route_web_research,
            ModelRoutingHint.BULK_EXTRACTION: settings.model_route_bulk_extraction,
            ModelRoutingHint.AMBIGUOUS_CLUSTERING: settings.model_route_ambiguous_clustering,
            ModelRoutingHint.STANDARD_DRAFT: settings.model_route_standard_draft,
            ModelRoutingHint.PREMIUM_SYNTHESIS: settings.model_route_premium_synthesis,
            ModelRoutingHint.CRITIQUE: settings.model_route_critique,
            ModelRoutingHint.DISCOVERY_MERGE: settings.model_route_discovery_merge,
        }.items()
    }
    router = ModelRouter(
        openai_research=openai_research,
        openai_structured=openai_structured,
        openai_drafting=openai_drafting,
        openai_critic=openai_critic,
        qwen=qwen,
        gemini=gemini,
        fake=fake,
        forced_backend=forced,
        routing=routing,
    )
    minio_client = Minio(
        settings.s3_endpoint,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        secure=settings.s3_secure,
    )
    blob_store = MinioBlobStore(minio_client, physical_bucket=settings.s3_bucket)
    output_store = BlobModelOutputStore(BlobCatalogService(blob_store, uow_factory))
    return ModelGateway(
        router,
        cast(ModelRunUnitOfWorkFactory, uow_factory),
        output_store,
        diagnostics=DiagnosticsLog.from_env(settings.diagnostics_log_root),
    )


def create_bridge_capabilities_provider(settings: Settings) -> ChatGPTBridgeClient:
    # Cette instance sert deux usages aux budgets opposés : la sonde
    # `/bridge/capabilities`, qui doit rester quasi instantanée et sans rejeu,
    # et la fermeture de session, qui pilote le navigateur. Chaque appel porte
    # désormais son propre budget ; `timeout_seconds` n'est plus qu'un
    # défaut de sécurité.
    return ChatGPTBridgeClient(
        settings.openai_bridge_base_url,
        api_key=_secret_value(settings.openai_bridge_api_key),
        timeout_seconds=settings.openai_bridge_archive_timeout_seconds,
        connect_timeout_seconds=settings.openai_bridge_connect_timeout_seconds,
        capabilities_timeout_seconds=settings.openai_bridge_capabilities_timeout_seconds,
        archive_timeout_seconds=settings.openai_bridge_archive_timeout_seconds,
        max_attempts=settings.openai_bridge_max_attempts,
    )


def _secret_value(secret: object | None) -> str | None:
    if secret is None:
        return None
    getter = getattr(secret, "get_secret_value", None)
    if not callable(getter):
        return None
    value = getter()
    return value or None
