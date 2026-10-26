from __future__ import annotations

import inspect

import httpx
import pytest
from pydantic import ValidationError

from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalOperationRoute,
    VirusTotalRouteStep,
    VirusTotalRoutingPolicy,
)
from cti_app.config import Settings
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.virustotal import (
    VirusTotalHttpAdapter,
    create_virustotal_http_adapter,
)


def test_adapter_factory_passes_normalized_download_settings() -> None:
    settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_base_url="https://www.virustotal.com/api/v3",
        virustotal_file_download_enabled=True,
        virustotal_download_allowed_hosts=(
            " WWW.VIRUSTOTAL.COM, ,vtsamples.commondatastorage.googleapis.com,cdn.example.test "
        ),
        virustotal_download_max_bytes=321_000,
        virustotal_download_timeout_seconds=17.5,
        virustotal_download_allowed_ports="443,8443",
        virustotal_download_max_redirects=4,
        virustotal_file_download_legacy_fallback_enabled=True,
    )
    client = httpx.AsyncClient()
    try:
        adapter = create_virustotal_http_adapter(
            client=client,
            settings=settings,
            capabilities=VirusTotalCapabilities(file_download=True),
        )
        assert adapter._download_allowed_hosts == frozenset(
            {
                "www.virustotal.com",
                "vtsamples.commondatastorage.googleapis.com",
                "cdn.example.test",
            }
        )
        assert adapter._download_max_bytes == 321_000
        assert adapter._download_timeout_seconds == 17.5
        assert adapter._download_allowed_ports == frozenset({443, 8443})
        assert adapter._download_max_redirects == 4
        assert adapter._file_download_legacy_fallback_enabled is True
        assert adapter._proxy_query_sentinel_enabled is True
        assert adapter._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD) is not None
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_adapter_factory_tracks_all_settings_driven_constructor_parameters() -> None:
    from cti_app.infrastructure.virustotal import (
        VIRUSTOTAL_ADAPTER_SETTINGS_BY_ARGUMENT,
    )

    injected = {
        "client",
        "capabilities",
        "direct_client",
        "api_key",
        "routing_policy",
        "download_client_factory",
        "offline_probe_mode",
    }
    constructor_parameters = set(inspect.signature(VirusTotalHttpAdapter.__init__).parameters)
    constructor_parameters.discard("self")
    assert set(VIRUSTOTAL_ADAPTER_SETTINGS_BY_ARGUMENT) == constructor_parameters - injected
    assert all(
        field_name in Settings.model_fields
        for field_name in VIRUSTOTAL_ADAPTER_SETTINGS_BY_ARGUMENT.values()
    )


@pytest.mark.parametrize(
    "invalid_hosts",
    [
        "https://www.virustotal.com",
        "www.virustotal.com/path",
        "www.virustotal.com:443",
        "*.virustotal.com",
        "user@virustotal.com",
    ],
)
def test_invalid_download_allowlist_fails_settings_validation(invalid_hosts: str) -> None:
    with pytest.raises(ValidationError, match="virustotal_download_allowed_hosts"):
        Settings(_env_file=None, virustotal_download_allowed_hosts=invalid_hosts)


def test_download_route_stays_absent_when_flag_is_disabled() -> None:
    settings = Settings(_env_file=None, virustotal_file_download_enabled=False)
    client = httpx.AsyncClient()
    try:
        adapter = create_virustotal_http_adapter(
            client=client,
            settings=settings,
            capabilities=VirusTotalCapabilities(file_download=True),
        )
        assert adapter._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD) is None
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_download_route_stays_absent_without_plausible_signed_cdn_host() -> None:
    settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_file_download_enabled=True,
        virustotal_download_allowed_hosts="cdn.example.test",
        virustotal_download_allowed_ports="443",
    )
    client = httpx.AsyncClient()
    try:
        adapter = create_virustotal_http_adapter(
            client=client,
            settings=settings,
            capabilities=VirusTotalCapabilities(file_download=True),
        )
        assert adapter._capabilities.file_download is False
        assert adapter._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD) is None
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_proxy_query_sentinel_and_download_cdn_have_safe_settings_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.virustotal_proxy_query_sentinel_enabled is True
    assert settings.virustotal_download_allowed_hosts == (
        "vtsamples.commondatastorage.googleapis.com"
    )


def test_behaviour_pcap_setting_is_authoritative_for_api_and_worker_composition() -> None:
    settings = Settings(
        _env_file=None,
        virustotal_proxy_url="http://proxy.test:8080",
        virustotal_behaviour_pcap_enabled=True,
    )
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    client = httpx.AsyncClient()
    try:
        adapter = create_virustotal_http_adapter(
            client=client,
            settings=settings,
            capabilities=VirusTotalCapabilities(),
            routing_policy=VirusTotalRoutingPolicy(
                routes={VirusTotalCapability.BEHAVIOUR_PCAP: route}
            ),
        )
        assert adapter._capabilities.behaviour_pcap is True
        assert adapter._routing_policy.route_for(VirusTotalCapability.BEHAVIOUR_PCAP) == route
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_explicit_route_cannot_bypass_disabled_download_flag() -> None:
    settings = Settings(_env_file=None, virustotal_file_download_enabled=False)
    route = VirusTotalOperationRoute(
        primary=VirusTotalRouteStep(
            transport=VirusTotalTransportKind.PROXY,
            variant=VirusTotalEndpointVariant.V3,
        )
    )
    client = httpx.AsyncClient()
    try:
        adapter = create_virustotal_http_adapter(
            client=client,
            settings=settings,
            capabilities=VirusTotalCapabilities(file_download=True),
            routing_policy=VirusTotalRoutingPolicy(
                routes={VirusTotalCapability.FILE_DOWNLOAD: route}
            ),
        )
        assert adapter._routing_policy.route_for(VirusTotalCapability.FILE_DOWNLOAD) is None
    finally:
        import asyncio

        asyncio.run(client.aclose())
