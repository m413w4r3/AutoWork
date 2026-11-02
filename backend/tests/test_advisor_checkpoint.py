from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.advisor_checkpoint import (
    derive_advisor_consult_status,
    format_advisor_trace_purpose,
    split_advisor_trace_purpose,
)
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
            "response_received",
        ),
        (
            {"stage_timestamps_utc": {"manifest_persisted": "2026-11-02T00:00:00Z"}},
            "response_persisted",
        ),
        (
            {
                "response_observation_present": True,
            },
            "response_persisted",
        ),
        (
            {
                "response_received": True,
            },
            "response_received",
        ),
        ({"output_blob_present": True}, "response_received"),
        (
            {
                "invocation_status": ToolInvocationStatus.SUCCEEDED,
            },
            "received",
        ),
        ({"response_received": True}, "response_received"),
        (
            {
                "invocation_status": ToolInvocationStatus.NEEDS_REVIEW,
            },
            "ambiguous",
        ),
        ({"submission_state": "submission_ambiguous"}, "ambiguous"),
        ({"invocation_stage": "pretransport_failure"}, "error"),
        (
            {
                "invocation_status": ToolInvocationStatus.FAILED,
                "response_observation_present": True,
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


@pytest.mark.asyncio
async def test_api_consult_status_requires_response_link_or_manifest_persisted() -> None:
    from cti_app.api.investigation import _advisor_consult_statuses

    invocation_id = uuid4()
    advisor_call_id = uuid4()
    response_link_id = uuid4()
    advisor_call = SimpleNamespace(
        id=advisor_call_id,
        source="advisor.consult",
        object_type="advisor_call",
    )
    response_link = SimpleNamespace(
        id=response_link_id,
        source="advisor.consult.response",
        object_type="advisor_response_received",
    )

    class Summary:
        def __init__(self, metadata):
            self.metadata = metadata

        async def summary(self, _observation):
            return self.metadata

    def invocation(*response_refs):
        return SimpleNamespace(
            id=invocation_id,
            tool_name="advisor.consult",
            status=ToolInvocationStatus.SUCCEEDED,
            response_refs=response_refs,
        )

    linked_status = await _advisor_consult_statuses(
        invocations=(invocation(advisor_call_id, response_link_id),),
        observations=(advisor_call, response_link),
        evidence_summaries=Summary(
            {
                "schema": "operator-advisor-call.v1",
                "response_observation_ref": str(response_link_id),
                "output": {"output_blob_id": str(uuid4())},
            }
        ),
    )
    blob_only_status = await _advisor_consult_statuses(
        invocations=(invocation(advisor_call_id),),
        observations=(advisor_call,),
        evidence_summaries=Summary(
            {
                "schema": "operator-advisor-call.v1",
                "output_blob_id": str(uuid4()),
                "output": {"output_blob_id": str(uuid4())},
            }
        ),
    )
    manifest_status = await _advisor_consult_statuses(
        invocations=(invocation(advisor_call_id),),
        observations=(advisor_call,),
        evidence_summaries=Summary(
            {
                "schema": "operator-advisor-call.v1",
                "stage_timestamps_utc": {"manifest_persisted": "2026-11-02T00:00:00Z"},
            }
        ),
    )
    unlinked_status = await _advisor_consult_statuses(
        invocations=(invocation(advisor_call_id),),
        observations=(advisor_call,),
        evidence_summaries=Summary(
            {
                "schema": "operator-advisor-call.v1",
                "response_observation_ref": str(uuid4()),
            }
        ),
    )

    assert linked_status[invocation_id] == "response_persisted"
    assert blob_only_status[invocation_id] == "response_received"
    assert manifest_status[invocation_id] == "response_persisted"
    assert unlinked_status[invocation_id] == "received"


def test_prose_disposition_of_a_consultation_needs_no_recommendation_index() -> None:
    advisor = str(uuid4())
    purpose = format_advisor_trace_purpose(
        "Mesurer la fonction avant le pivot.",
        recommendation={"invocation_id": advisor, "decision": "deferred", "reason": "Pas encore"},
    )
    trace, human = split_advisor_trace_purpose(purpose)
    assert human == "Mesurer la fonction avant le pivot."
    assert trace == {
        "recommendation": {"invocation_id": advisor, "decision": "deferred", "reason": "Pas encore"}
    }


@pytest.mark.parametrize(
    "recommendation",
    [
        {"decision": "adapted"},
        {"decision": "refused", "reason": " "},
        {"decision": "maybe", "reason": "x"},
        {"index": 1, "decision": "adapted", "reason": "x"},
        {"index": 5, "decision": "accepted"},
        {"index": True, "decision": "accepted"},
    ],
)
def test_recommendation_trace_keeps_its_bounded_contract(recommendation: dict[str, object]) -> None:
    purpose = format_advisor_trace_purpose(
        "Action choisie.", recommendation={"invocation_id": str(uuid4()), **recommendation}
    )
    with pytest.raises(ValueError, match="advisor_trace_invalid"):
        split_advisor_trace_purpose(purpose)
