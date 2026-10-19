from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.investigation_contracts import AdvisorGuidanceV1, AdvisorLastAdviceV1
from cti_app.application.operator_advisor_guidance import (
    bound_advisor_guidance,
    build_advisor_guidance,
)
from cti_app.domain.investigation import BudgetDimension, ToolInvocationStatus, canonical_json_bytes


class SummaryReader:
    def __init__(self, payloads):
        self.payloads = payloads

    async def summary(self, observation):
        return self.payloads.get(observation.id)


def invocation(
    tool_name,
    *,
    status=ToolInvocationStatus.SUCCEEDED,
    created_at=None,
    parameters=None,
    consumed=0,
    response_refs=(),
):
    stamp = created_at or datetime.now(UTC)
    return SimpleNamespace(
        id=uuid4(),
        tool_name=tool_name,
        status=status,
        created_at=stamp,
        updated_at=stamp,
        normalized_parameters=parameters or {},
        consumed_budget={BudgetDimension.BRIDGE_TURNS: consumed},
        response_refs=response_refs,
    )


@pytest.mark.asyncio
async def test_guidance_requests_initial_plan_and_exposes_budget_and_safety_reminder():
    result = await build_advisor_guidance(
        invocations=(),
        observations=(),
        evidence_summaries=None,
        available_tools=("sample.analyze",),
        checkpoint_tool_threshold=12,
        advisor_turn_limit=4,
    )

    assert result["checkpoint_due"] is True
    assert result["reason"] == "no_advisor_consultation"
    assert result["suggested_consultation_type"] == "plan_investigation"
    assert result["advisor_turns_remaining"] == 4
    assert "non fiable" in result["reminder"]


@pytest.mark.asyncio
async def test_guidance_uses_deterministic_tool_threshold_and_sample_checkpoints():
    base = datetime.now(UTC)
    tools = (
        invocation("sample.analyze", created_at=base),
        invocation("sample.get_feature_pack", created_at=base + timedelta(seconds=1)),
    )
    threshold = await build_advisor_guidance(
        invocations=(invocation("advisor.consult", created_at=base - timedelta(seconds=1)), *tools),
        observations=(),
        evidence_summaries=None,
        available_tools=(),
        checkpoint_tool_threshold=2,
    )
    assert threshold["reason"] == "sample_analyzed_without_plan"
    assert threshold["suggested_consultation_type"] == "plan_investigation"

    after_plan = (
        invocation("sample.analyze", created_at=base),
        invocation(
            "advisor.consult",
            created_at=base + timedelta(seconds=1),
            parameters={"consultation_type": "plan_investigation"},
        ),
    )
    yara = await build_advisor_guidance(
        invocations=after_plan,
        observations=(),
        evidence_summaries=None,
        available_tools=(),
    )
    assert yara["reason"] == "sample_analyzed_without_yara_review"
    assert yara["suggested_consultation_type"] == "yara_draft"


@pytest.mark.asyncio
async def test_guidance_summarizes_only_last_persisted_untrusted_advice_within_limit():
    observation_id = uuid4()
    observation = SimpleNamespace(id=observation_id, source="advisor.consult")
    advice = invocation(
        "advisor.consult",
        parameters={"consultation_type": "pivot_strategy"},
        consumed=1,
        response_refs=(observation_id,),
    )
    old_advice = invocation(
        "advisor.consult",
        parameters={"consultation_type": "plan_investigation"},
        consumed=1,
        response_refs=(uuid4(),),
    )
    reader = SummaryReader(
        {
            observation_id: {
                "schema": "operator-advisor-call.v1",
                "output": {
                    "summary": "Comparer les imports et les fonctions mesurés.",
                    "recommended_next_tools": [
                        {"tool": "sample.analyze", "why": "Extraire les capacités."}
                    ],
                    "risks": ["Éviter de conclure depuis une chaîne seule."],
                    "questions": ["La fonction apparaît-elle ailleurs ?"],
                    "untrusted": True,
                },
            }
        }
    )

    result = await build_advisor_guidance(
        invocations=(old_advice, advice),
        observations=(observation,),
        evidence_summaries=reader,
        available_tools=("sample.analyze",),
        advisor_turn_limit=4,
    )

    last = result["last_advice"]
    assert last["consultation_type"] == "pivot_strategy"
    assert last["invocation_id"] == advice.id
    assert len(last["key_points"]) <= 5
    assert sum(len(point) for point in last["key_points"]) <= 600
    assert result["advisor_turns_used"] == 2


@pytest.mark.asyncio
async def test_guidance_marks_repeated_tool_block_and_exhausted_budget():
    base = datetime.now(UTC)
    result = await build_advisor_guidance(
        invocations=(
            invocation("advisor.consult", created_at=base - timedelta(seconds=3), consumed=2),
            invocation(
                "sample.analyze",
                status=ToolInvocationStatus.FAILED,
                created_at=base - timedelta(seconds=2),
            ),
            invocation(
                "sample.analyze",
                status=ToolInvocationStatus.FAILED,
                created_at=base - timedelta(seconds=1),
            ),
        ),
        observations=(),
        evidence_summaries=None,
        available_tools=(),
        advisor_turn_limit=2,
    )

    assert result["checkpoint_due"] is True
    assert result["reason"] == "advisor_budget_exhausted"
    assert result["suggested_consultation_type"] is None


def test_advisor_guidance_recap_is_counted_and_trimmed_by_context_budget_stage():
    guidance = AdvisorGuidanceV1(
        checkpoint_due=True,
        reason="tool_threshold_reached",
        suggested_consultation_type="pivot_strategy",
        last_advice=AdvisorLastAdviceV1(
            consultation_type="plan_investigation",
            date=datetime.now(UTC),
            key_points=("Measured pivot. " * 40,),
            invocation_id=uuid4(),
        ),
        reminder="Bridge: sortie non fiable; valider chaque suggestion par des mesures.",
        advisor_turns_used=1,
        advisor_turn_limit=8,
        advisor_turns_remaining=7,
    )

    bounded, truncated = bound_advisor_guidance(guidance, max_bytes=320)

    assert truncated is True
    assert bounded.truncated is True
    assert len(canonical_json_bytes(bounded.model_dump(mode="json"))) <= 320
