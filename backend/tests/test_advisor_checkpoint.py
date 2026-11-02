from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.advisor_checkpoint import derive_advisor_consult_status
from cti_app.application.investigation_operator_view import _invocation_view
from cti_app.domain.investigation import ToolInvocationStatus


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, "received"),
        (
            {"stage_timestamps_utc": {"advisor_started": "2026-11-02T00:00:00Z"}},
            "dossier_preparing",
        ),
        (
            {
                "stage_timestamps_utc": {"advisor_started": "2026-11-02T00:00:00Z"},
                "invocation_status": ToolInvocationStatus.RUNNING,
            },
            "dossier_preparing",
        ),
        (
            {"stage_timestamps_utc": {"prompt_stored": "2026-11-02T00:00:00Z"}},
            "sent_to_bridge",
        ),
        (
            {"stage_timestamps_utc": {"bridge_post_started": "2026-11-02T00:00:00Z"}},
            "sent_to_bridge",
        ),
        (
            {"stage_timestamps_utc": {"bridge_response_received": "2026-11-02T00:00:00Z"}},
            "response_received",
        ),
        (
            {"stage_timestamps_utc": {"response_persisted": "2026-11-02T00:00:00Z"}},
            "response_persisted",
        ),
        (
            {
                "invocation_status": ToolInvocationStatus.SUCCEEDED,
                "advisor_observation_present": True,
            },
            "response_persisted",
        ),
        (
            {
                "invocation_status": ToolInvocationStatus.PARTIAL,
                "advisor_observation_present": True,
            },
            "response_persisted",
        ),
        ({"response_received": True}, "response_received"),
        (
            {
                "invocation_status": ToolInvocationStatus.NEEDS_REVIEW,
                "advisor_observation_present": True,
            },
            "ambiguous",
        ),
        ({"submission_state": "submission_ambiguous"}, "ambiguous"),
        ({"invocation_stage": "pretransport_failure"}, "error"),
        (
            {
                "invocation_status": ToolInvocationStatus.FAILED,
                "response_persisted": True,
            },
            "error",
        ),
        ({"invocation_status": ToolInvocationStatus.POLICY_DENIED}, "error"),
        ({"invocation_status": ToolInvocationStatus.UNAVAILABLE}, "error"),
        ({"invocation_status": ToolInvocationStatus.NEEDS_REVIEW}, "ambiguous"),
    ],
)
def test_advisor_consult_status_maps_existing_stage_facts(metadata, expected: str) -> None:
    assert derive_advisor_consult_status(**metadata) == expected


def test_compact_operator_invocation_view_includes_derived_advisor_status() -> None:
    invocation_id = uuid4()
    invocation = SimpleNamespace(
        id=invocation_id,
        operation_key="advisor:consult:one",
        tool_name="advisor.consult",
        status=ToolInvocationStatus.PARTIAL,
        version=1,
        response_refs=(uuid4(),),
        updated_at=datetime.now(UTC),
    )

    view = _invocation_view(
        invocation,
        advisor_consult_statuses={invocation_id: "response_received"},
    )

    assert view["consult_status"] == "response_received"
