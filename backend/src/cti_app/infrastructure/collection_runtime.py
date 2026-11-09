"""Shared construction for the source collection service used by API and workers."""

from __future__ import annotations

from cti_app.application.blob_storage import BlobStore
from cti_app.application.collection import SubjectCollectionService
from cti_app.application.http_collection import (
    CollectionPolicy,
    SafeHttpCollector,
    SystemDnsResolver,
    parse_domain_policy,
)
from cti_app.application.model_gateway import ModelGateway
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.workspace import SubjectWorkspaceMaterializer
from cti_app.config import Settings
from cti_app.infrastructure.http import AsyncioPinnedHttpTransport


def create_subject_collection_service(
    *,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    blob_store: BlobStore,
    model_gateway: ModelGateway | None,
    source_media_max_images_per_subject: int | None = None,
    stop_on_source_media_policy_block: bool = False,
) -> SubjectCollectionService:
    """Build the shared collection service from runtime settings.

    API and worker wiring use the same collector policy and collection options.
    The media limits are opt-in for the bounded operator archive script.
    """
    return SubjectCollectionService(
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
        model_gateway=model_gateway,
        fallback_variants_enabled=settings.collection_fallback_variants_enabled,
        fallback_archive_enabled=settings.collection_fallback_archive_enabled,
        fallback_bridge_enabled=settings.collection_fallback_bridge_enabled,
        fallback_max_variants=settings.collection_fallback_max_variants,
        fallback_strategy_flags=settings.collection_acquisition_strategy_enabled,
        fallback_strategy_timeouts=settings.collection_acquisition_strategy_timeout_seconds,
        fallback_min_text_chars=settings.collection_fallback_min_text_chars,
        fallback_playwright_sidecar_url=settings.collection_playwright_sidecar_url or None,
        acquisition_deadline_seconds=settings.collection_acquisition_deadline_seconds,
        acquisition_max_attempts=settings.collection_acquisition_max_attempts,
        acquisition_per_domain_concurrency=settings.collection_acquisition_per_domain_concurrency,
        fallback_endpoint_enabled=settings.collection_fallback_endpoint_enabled,
        browser_navigation_enabled=settings.collection_browser_navigation_enabled,
        browser_control_socket=settings.collection_browser_control_socket,
        syndication_domains=settings.collection_syndication_domains,
        exact_title_search_endpoint=settings.collection_exact_title_search_endpoint,
        third_party_provider_url=settings.collection_third_party_provider_url,
        third_party_provider_token=settings.collection_third_party_provider_token,
        third_party_provider_enabled=settings.collection_third_party_provider_enabled,
        ocr_command=settings.collection_ocr_command,
        newsletter_archive_endpoint=settings.collection_official_newsletter_archive_endpoint,
        source_media_max_images_per_subject=source_media_max_images_per_subject,
        stop_on_source_media_policy_block=stop_on_source_media_policy_block,
    )
