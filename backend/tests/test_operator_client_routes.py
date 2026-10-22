from __future__ import annotations

import pytest

from cti_app.api.main import _operator_client_route_allowed

INVESTIGATION_ID = "a71a2aa0-2f8c-4a90-80f2-8c6c9a331e08"
OBSERVATION_ID = "56a906d5-943b-4dae-a3c8-867d730e2229"


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", f"/api/investigations/{INVESTIGATION_ID}"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/operator-status"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/operator/context"),
        (
            "GET",
            f"/api/investigations/{INVESTIGATION_ID}/operator/observations/{OBSERVATION_ID}",
        ),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/operator/tools/finding.propose"),
        (
            "GET",
            f"/api/investigations/{INVESTIGATION_ID}/operator/context/section/budget_remaining",
        ),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/pivots"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/experiments"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/envelope"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/samples"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/opinion"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/detections"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/network-captures"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/rule-corpus/imports"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/rule-corpus/imports"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/rule-corpus/roles"),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION_ID}/rule-corpus/holdout-designations",
        ),
        ("GET", "/api/v1/network-captures/capabilities"),
        ("GET", f"/api/v1/investigations/{INVESTIGATION_ID}/network-captures"),
    ),
)
def test_operator_client_route_allowlist_includes_read_and_tool_routes(method, path) -> None:
    assert _operator_client_route_allowed(method, path)


def test_operator_client_route_allowlist_normalizes_outer_slashes_only() -> None:
    assert _operator_client_route_allowed(
        "GET", f"//api/investigations/{INVESTIGATION_ID}/pivots//"
    )
    assert not _operator_client_route_allowed(
        "GET", f"/api//investigations/{INVESTIGATION_ID}/pivots"
    )


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/envelope/approve"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/envelope/revoke"),
        (
            "POST",
            f"/api/investigations/{INVESTIGATION_ID}/operator/tools/sample.set_qualification",
        ),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/findings"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/opinions/approve"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/opinion/compose"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/rule-corpus/roles/extra"),
        ("PUT", f"/api/investigations/{INVESTIGATION_ID}/rule-corpus/roles"),
        ("POST", f"/api/v1/investigations/{INVESTIGATION_ID}/network-captures"),
        ("POST", "/api/detections/56a906d5-943b-4dae-a3c8-867d730e2229/approve"),
        ("POST", f"/api/investigations/{INVESTIGATION_ID}/pivots"),
        ("PUT", f"/api/investigations/{INVESTIGATION_ID}/experiments"),
        (
            "PATCH",
            f"/api/investigations/{INVESTIGATION_ID}/operator/context/section/budget_remaining",
        ),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/pivots/extra"),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/experiments/extra"),
        (
            "GET",
            f"/api/investigations/{INVESTIGATION_ID}/operator/context/section/budget_remaining/extra",
        ),
        ("GET", f"/api/investigations/{INVESTIGATION_ID}/operator/context/section"),
        ("GET", "/api/investigations/not-a-uuid/pivots"),
        ("GET", "/api/investigations/not-a-uuid/experiments"),
        (
            "GET",
            "/api/investigations/not-a-uuid/operator/context/section/budget_remaining",
        ),
        ("GET", "/api/investigations/not-a-uuid"),
        ("GET", "/api/v1/investigations/not-a-uuid/network-captures"),
    ),
)
def test_operator_client_route_allowlist_excludes_governance_and_review_routes(
    method, path
) -> None:
    assert not _operator_client_route_allowed(method, path)
