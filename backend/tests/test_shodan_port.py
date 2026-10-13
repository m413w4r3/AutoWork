from __future__ import annotations

from datetime import datetime

import pytest

from cti_app.application.shodan import (
    MAX_SEARCH_QUERY_LENGTH,
    ShodanBudgetEstimate,
    ShodanCapabilities,
    ShodanInvalidInputError,
    validate_shodan_domain,
    validate_shodan_ip,
    validate_shodan_page,
    validate_shodan_search_query,
)
from cti_app.config import Settings
from cti_app.domain.shodan import ShodanCapability, ShodanRoute, ShodanService
from tests.shodan_support import (
    DOMAIN_RESPONSE,
    HOST_JSON,
    HOST_RESPONSE,
    SEARCH_JSON,
    SEARCH_RESPONSE,
    SHARED_CERT_SHA256,
    FakeShodanPort,
)


def test_capabilities_and_budget_are_deny_by_default() -> None:
    capabilities = ShodanCapabilities()
    assert all(not capabilities.is_enabled(capability) for capability in ShodanCapability)
    estimate = ShodanBudgetEstimate()
    assert [estimate.credits_for(capability) for capability in ShodanCapability] == [1, 1, 1, 1]
    assert ShodanBudgetEstimate(host=0, search=2).credits_for(ShodanCapability.HOST) == 0


def test_ip_and_domain_values_are_normalized_and_validated() -> None:
    assert validate_shodan_ip(" 2001:0db8::1 ") == "2001:db8::1"
    assert validate_shodan_domain("WWW.Example.Test.") == "www.example.test"
    assert validate_shodan_domain("münich.example") == "xn--mnich-kva.example"

    for invalid_ip in ("", "example.test", "999.1.2.3"):
        with pytest.raises(ShodanInvalidInputError):
            validate_shodan_ip(invalid_ip)
    for invalid_domain in ("", "localhost", "127.0.0.1", "-bad.example", "bad_.example"):
        with pytest.raises(ShodanInvalidInputError):
            validate_shodan_domain(invalid_domain)


def test_search_query_is_bounded_and_refuses_control_or_active_scan_filters() -> None:
    assert validate_shodan_search_query("  port:443 org:Example  ") == "port:443 org:Example"
    with pytest.raises(ShodanInvalidInputError):
        validate_shodan_search_query("\nport:443")
    with pytest.raises(ShodanInvalidInputError):
        validate_shodan_search_query("x" * (MAX_SEARCH_QUERY_LENGTH + 1))
    for query in ("scan:203.0.113.1", "scan_time:1", "active_scan:true", "on_demand_scan:true"):
        with pytest.raises(ShodanInvalidInputError):
            validate_shodan_search_query(query)


def test_page_must_stay_within_the_configured_limit() -> None:
    assert validate_shodan_page(4, max_pages=4) == 4
    for invalid_page in (0, -1, 5, True, "1"):
        with pytest.raises(ShodanInvalidInputError):
            validate_shodan_page(invalid_page, max_pages=4)  # type: ignore[arg-type]


def test_domain_service_and_settings_invariants() -> None:
    with pytest.raises(ValueError, match="port"):
        ShodanService(port=65536)
    with pytest.raises(ValueError, match="timezone-aware"):
        ShodanService(port=443, timestamp=datetime(2026, 1, 1))

    settings = Settings(_env_file=None)
    assert not settings.shodan_host_enabled
    assert not settings.shodan_search_enabled
    assert not settings.shodan_domain_enabled
    assert not settings.shodan_count_enabled
    assert settings.shodan_api_key is None
    assert settings.shodan_base_url == "https://api.shodan.io"
    assert settings.investigation_max_shodan_credits == 0


def test_shodan_api_key_is_secret_and_does_not_enable_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHODAN_API_KEY", "config-only-test-secret")
    settings = Settings(_env_file=None)
    assert settings.shodan_api_key is not None
    assert settings.shodan_api_key.get_secret_value() == "config-only-test-secret"
    assert "config-only-test-secret" not in repr(settings)
    assert "config-only-test-secret" not in str(settings)
    assert not settings.shodan_host_enabled
    assert not settings.shodan_search_enabled


@pytest.mark.asyncio
async def test_fake_port_returns_synthetic_host_search_and_domain_fixtures() -> None:
    port = FakeShodanPort()
    host = await port.host("203.0.113.10")
    search = await port.search("port:443", page=2)
    domain = await port.domain("example.test")
    count = await port.count("port:443")

    assert len(host.services) == 2
    assert host.services[0].certificate is not None
    assert host.services[0].certificate.fingerprint_sha256 == SHARED_CERT_SHA256
    assert host.services[1].certificate == host.services[0].certificate
    assert host.meta.route is ShodanRoute.HOST
    assert len(search.matches) == 3 and search.total == 3 and search.page == 2
    assert domain.subdomains == ("www", "api", "vpn")
    assert len(domain.dns_records) == 2
    assert count.total == 3
    assert port.calls == [
        ("host", ("203.0.113.10",)),
        ("search", ("port:443", 2)),
        ("domain", ("example.test",)),
        ("count", ("port:443",)),
    ]

    # The fixtures are JSON wire shapes and can be decoded independently of the fake port.
    assert HOST_RESPONSE and SEARCH_RESPONSE and DOMAIN_RESPONSE
    assert len(HOST_JSON["data"]) == 2
    assert len(SEARCH_JSON["matches"]) == 3
