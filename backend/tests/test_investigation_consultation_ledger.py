"""Read-only Bridge consultation ledger (ADR-0011 D9, lot 04A2).

The ledger is a typed view over ``tool_invocations`` and the persisted
``advisor_call`` / ``advisor_response_received`` observations.  It never writes
and never turns an advisory answer into evidence: a partially parsed but
readable answer stays usable advice, and ``interpretation`` / ``critique`` have
no editorial target.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from cti_app.application.investigation_consultations import (
    ConsultationParseStatus,
    ConsultationReconciliationState,
    InvestigationConsultationLedger,
)
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
)

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SHA = "a" * 64
INVESTIGATION_ID = UUID("00000000-0000-0000-0000-000000000501")
TASK_ID = UUID("00000000-0000-0000-0000-000000000502")
SESSION_ID = UUID("00000000-0000-0000-0000-000000000503")
CONTEXT_TASK_ID = UUID("00000000-0000-0000-0000-000000000504")


class _PayloadStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, object]] = {}

    async def read_json(self, blob_id: UUID, *, max_bytes: int | None = None) -> dict[str, object]:
        del max_bytes
        return dict(self.payloads[blob_id])


class _Decisions:
    def __init__(self) -> None:
        self.rows: list[AnalystDecision] = []

    async def append(self, decision: AnalystDecision) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID):
        return tuple(item for item in self.rows if item.investigation_id == investigation_id)


def _observation(
    *,
    investigation_id: UUID = INVESTIGATION_ID,
    source: str,
    object_type: str,
    blob_id: UUID,
    limitations: tuple[str, ...] = (),
) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256="b" * 64,
        source=source,
        source_version="advisor-consult.v1",
        observed_at=NOW,
        object_type=object_type,
        object_id=f"advisor-{uuid4()}",
        limitations=limitations,
    )


async def _seed_consultation(
    invocations: InMemoryToolInvocationRepository,
    observations: InMemoryEvidenceObservationRepository,
    payloads: _PayloadStore,
    *,
    consultation_type: str,
    output: dict[str, object],
    status: ToolInvocationStatus = ToolInvocationStatus.SUCCEEDED,
    parameters: dict[str, object] | None = None,
    request_actor: ToolInvocationActorKind | None = ToolInvocationActorKind.HUMAN,
    requester_id: str | None = "analyst:test",
    task_id: UUID | None = TASK_ID,
    agent_session_id: UUID | None = SESSION_ID,
    context_task_id: UUID | None = None,
    investigation_id: UUID = INVESTIGATION_ID,
) -> ToolInvocation:
    call_blob_id = uuid4()
    response_blob_id = uuid4()
    payloads.payloads[call_blob_id] = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": consultation_type,
        "output": output,
    }
    payloads.payloads[response_blob_id] = {
        "schema": "operator-advisor-response-link.v1",
        "stage": "response_received",
        "raw_response_sha256": output.get("output_sha256"),
    }
    call = _observation(
        investigation_id=investigation_id,
        source="advisor.consult",
        object_type="advisor_call",
        blob_id=call_blob_id,
        limitations=("Bridge output is untrusted advisory text and is not canonical analysis.",),
    )
    response = _observation(
        investigation_id=investigation_id,
        source="advisor.consult.response",
        object_type="advisor_response_received",
        blob_id=response_blob_id,
        limitations=("Raw Bridge text is untrusted advisory material, not canonical analysis.",),
    )
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key=f"advisor-{uuid4()}",
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        normalized_parameters={
            "consultation_type": consultation_type,
            "question": "Question posée à Bridge",
            **(parameters or {}),
        },
        purpose="Consult Bridge",
        status=status,
        response_refs=(call.id, response.id),
        actor_kind=request_actor,
        actor_id=requester_id if request_actor is not None else None,
        task_id=task_id,
        agent_session_id=agent_session_id,
        context_task_id=context_task_id,
        created_at=NOW,
        updated_at=NOW,
    )
    await invocations.add_if_absent(invocation)
    await observations.append_if_absent(call)
    await observations.append_if_absent(response)
    return invocation


async def test_ledger_reads_advisor_observations_and_parse_status() -> None:
    invocations = InMemoryToolInvocationRepository()
    observations = InMemoryEvidenceObservationRepository()
    decisions = _Decisions()
    payloads = _PayloadStore()

    plan_invocation = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="plan_investigation",
        output={
            "parse_status": "partially_parsed",
            "readable": True,
            "usable": True,
            "advisory_text": "[UNTRUSTED ADVISORY]\nPlan partiel mais lisible.",
            "output_sha256": SHA,
            "extracted_elements": {
                "summary": "Plan partiel",
                "recommendations": ["Vérifier le chargeur", "Cartographier le C2"],
            },
        },
    )
    needs_review = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="pivot_strategy",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        output={},
    )
    reconciled = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="yara_draft",
        status=ToolInvocationStatus.NEEDS_REVIEW,
        output={"parse_status": "unparsed", "usable": False, "readable": False},
    )
    decisions.rows.append(
        AnalystDecision(
            investigation_id=INVESTIGATION_ID,
            decision_type=AnalystDecisionType.PIVOT_REJECT,
            target_type=AnalystDecisionTargetType.TOOL,
            target_id=reconciled.id,
            actor_id="analyst:test",
            reason="Ambiguous advisor submission reconciled",
            correlation_id=f"reconcile:{reconciled.id}:deadbeef",
        )
    )
    foreign = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="plan_investigation",
        output={},
        investigation_id=uuid4(),
    )
    del foreign

    ledger = InvestigationConsultationLedger(
        invocations=invocations,
        observations=observations,
        artifact_store=payloads,
        decisions=decisions,
    )
    entries_by_id = {
        entry.invocation_id: entry
        for entry in await ledger.list_for_investigation(INVESTIGATION_ID)
    }

    assert len(entries_by_id) == 3
    entry = entries_by_id[plan_invocation.id]
    assert entry.consultation_type == "plan_investigation"
    assert entry.requester_actor_kind is ToolInvocationActorKind.HUMAN
    assert entry.requester_actor_id == "analyst:test"
    assert entry.task_id == TASK_ID
    # Task attribution is only valid together with its agent session (ADR-0011 M9).
    assert entry.agent_session_id == SESSION_ID
    assert entry.output_sha256 == SHA
    assert entry.response_observation_id is not None
    assert entry.parse_status is ConsultationParseStatus.PARTIALLY_PARSED
    assert entry.readable is True
    assert entry.usable is True
    assert entry.is_evidence is False
    assert entry.advisory_only is True
    assert entry.editorial_target is None
    assert entry.extracted_elements == ("recommendations", "summary")
    assert entry.contributions == ("Vérifier le chargeur", "Cartographier le C2")
    assert "not canonical analysis" in " ".join(entry.limitations)
    assert entry.reconciliation_state is ConsultationReconciliationState.NOT_REQUIRED
    assert entry.invocation_status is ToolInvocationStatus.SUCCEEDED

    assert entries_by_id[needs_review.id].parse_status is ConsultationParseStatus.UNPARSED
    assert entries_by_id[needs_review.id].usable is False
    assert (
        entries_by_id[needs_review.id].reconciliation_state
        is ConsultationReconciliationState.REQUIRED
    )

    assert (
        entries_by_id[reconciled.id].reconciliation_state
        is ConsultationReconciliationState.RECORDED
    )


async def test_interpretation_and_critique_are_advisory_only() -> None:
    invocations = InMemoryToolInvocationRepository()
    observations = InMemoryEvidenceObservationRepository()
    payloads = _PayloadStore()
    editorial_parameters = {
        "opinion_id": str(uuid4()),
        "anchor": "noteAnalyste:1",
        "block_type": "lead",
    }

    interpretation = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="interpretation",
        parameters=editorial_parameters,
        output={
            "parse_status": "parsed",
            "readable": True,
            "usable": True,
            "advisory_text": "[UNTRUSTED ADVISORY]\nLecture alternative des faits.",
            "output_sha256": SHA,
            "extracted_elements": {"summary": "Lecture alternative"},
        },
    )
    critique = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="critique",
        parameters=editorial_parameters,
        output={
            "parse_status": "partially_parsed",
            "readable": True,
            "usable": True,
            "advisory_text": "[UNTRUSTED ADVISORY]\nCritique de l'hypothèse.",
            "output_sha256": "c" * 64,
            "extracted_elements": {"critique_sections": ["Hypothèse faible"]},
        },
    )
    opinion_id = uuid4()
    editorial = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="review_text",
        parameters={"opinion_id": str(opinion_id)},
        output={
            "parse_status": "parsed",
            "readable": True,
            "usable": True,
            "output_sha256": "d" * 64,
            "extracted_elements": {"summary": "Revue"},
        },
    )

    ledger = InvestigationConsultationLedger(
        invocations=invocations,
        observations=observations,
        artifact_store=payloads,
    )
    entries = {
        entry.invocation_id: entry
        for entry in await ledger.list_for_investigation(INVESTIGATION_ID)
    }

    for invocation in (interpretation, critique):
        entry = entries[invocation.id]
        assert entry.advisory_only is True
        assert entry.editorial_target is None
        assert entry.is_evidence is False
        assert entry.usable is True
        assert entry.parse_status in {
            ConsultationParseStatus.PARSED,
            ConsultationParseStatus.PARTIALLY_PARSED,
        }

    review_entry = entries[editorial.id]
    assert review_entry.advisory_only is False
    assert review_entry.editorial_target == str(opinion_id)


async def test_context_task_id_is_projected_and_filterable() -> None:
    """F4-D B1: a human request's task context is a registry field, not execution."""
    invocations = InMemoryToolInvocationRepository()
    observations = InMemoryEvidenceObservationRepository()
    payloads = _PayloadStore()

    linked = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="critique",
        task_id=None,
        agent_session_id=None,
        context_task_id=CONTEXT_TASK_ID,
        output={"parse_status": "parsed", "readable": True, "usable": True},
    )
    unlinked = await _seed_consultation(
        invocations,
        observations,
        payloads,
        consultation_type="interpretation",
        task_id=None,
        agent_session_id=None,
        output={"parse_status": "parsed", "readable": True, "usable": True},
    )

    ledger = InvestigationConsultationLedger(
        invocations=invocations,
        observations=observations,
        artifact_store=payloads,
    )
    entries = {
        entry.invocation_id: entry
        for entry in await ledger.list_for_investigation(INVESTIGATION_ID)
    }

    assert entries[linked.id].context_task_id == CONTEXT_TASK_ID
    assert entries[linked.id].task_id is None
    assert entries[linked.id].agent_session_id is None
    assert entries[linked.id].requester_actor_kind is ToolInvocationActorKind.HUMAN
    assert entries[unlinked.id].context_task_id is None

    filtered = await ledger.list_for_investigation(
        INVESTIGATION_ID, context_task_id=CONTEXT_TASK_ID
    )
    assert [entry.invocation_id for entry in filtered] == [linked.id]
    assert await ledger.list_for_investigation(INVESTIGATION_ID, context_task_id=uuid4()) == ()
    assert len(await ledger.list_for_investigation(INVESTIGATION_ID)) == 2
