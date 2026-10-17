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
        virustotal_base_url="https://www.virustotal.com/api/v3",
        virustotal_file_download_enabled=True,
        virustotal_download_allowed_hosts=" WWW.VIRUSTOTAL.COM, ,cdn.example.test ",
        virustotal_download_max_bytes=321_000,
        virustotal_download_timeout_seconds=17.5,
    )
    client = httpx.AsyncClient()
    try:
        adapter = create_virustotal_http_adapter(
            client=client,
            settings=settings,
            capabilities=VirusTotalCapabilities(file_download=True),
        )
        assert adapter._download_allowed_hosts == frozenset(
            {"www.virustotal.com", "cdn.example.test"}
        )
        assert adapter._download_max_bytes == 321_000
        assert adapter._download_timeout_seconds == 17.5
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
