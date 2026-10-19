from __future__ import annotations

import pytest

from cti_app.api.main import _operator_client_route_allowed


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/investigations/abc"),
        ("GET", "/api/investigations/abc/operator-status"),
        ("GET", "/api/investigations/abc/operator/context"),
        ("GET", "/api/investigations/abc/operator/observations/def"),
        ("POST", "/api/investigations/abc/operator/tools/finding.propose"),
    ),
)
def test_operator_client_route_allowlist_includes_read_and_tool_routes(method, path) -> None:
    assert _operator_client_route_allowed(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/investigations/abc/envelope"),
        ("POST", "/api/investigations/abc/envelope/approve"),
        ("POST", "/api/investigations/abc/envelope/revoke"),
        ("POST", "/api/investigations/abc/operator/tools/sample.set_qualification"),
        ("POST", "/api/investigations/abc/findings"),
        ("POST", "/api/investigations/abc/opinions/approve"),
    ),
)
def test_operator_client_route_allowlist_excludes_governance_and_review_routes(
    method, path
) -> None:
    assert not _operator_client_route_allowed(method, path)
