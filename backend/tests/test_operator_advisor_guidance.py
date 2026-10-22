from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.investigation_contracts import AdvisorGuidanceV1, AdvisorLastAdviceV1
from cti_app.application.operator_advisor_guidance import (
    DEFAULT_CHECKPOINT_TOOL_THRESHOLD,
    bound_advisor_guidance,
    build_advisor_guidance,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    ToolInvocationStatus,
    canonical_json_bytes,
)


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
    )

    assert result["checkpoint_due"] is True
    assert result["reason"] == "no_advisor_consultation"
    assert result["suggested_consultation_type"] == "plan_investigation"
    assert result["advisor_turn_limit"] is None
    assert result["advisor_turns_remaining"] is None
    assert "non fiable" in result["reminder"]
    assert "facultative" in result["reminder"]
    assert "hypothèse → pivot → succès/échec → coût" in result["reminder"].lower()
    assert "absence ≠ négatif" in result["reminder"]
    assert "historique" in result["reminder"]
    assert len(result["reminder"]) <= 120


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
    assert threshold["reason"] == "first_significant_decompilation"
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
    assert yara["reason"] == "before_yara_proposal"
    assert yara["suggested_consultation_type"] == "yara_draft"


@pytest.mark.asyncio
async def test_event_checkpoint_fixture_fits_eight_consultations_and_keeps_twelve_tool_fallback():
    base = datetime.now(UTC)
    pivot_id = uuid4()
    pivot_observation = SimpleNamespace(id=pivot_id, observed_at=base + timedelta(seconds=1))
    advisor = invocation(
        "advisor.consult",
        created_at=base,
        parameters={"consultation_type": "plan_investigation"},
        consumed=1,
    )
    fixtures = (
        ((), (), (), "no_advisor_consultation"),
        (
            (
                advisor,
                invocation("dotnet.decompile", created_at=base + timedelta(seconds=1)),
            ),
            (),
            (),
            "first_significant_decompilation",
        ),
        ((advisor,), (pivot_observation,), (pivot_id,), "new_pivot_discovered"),
        (
            (
                advisor,
                invocation("vt.file_report", status=ToolInvocationStatus.FAILED),
                invocation("vt.file_report", status=ToolInvocationStatus.FAILED),
            ),
            (),
            (),
            "repeated_tool_block",
        ),
        (
            (
                invocation("sample.analyze", created_at=base),
                invocation(
                    "advisor.consult",
                    created_at=base + timedelta(seconds=1),
                    parameters={"consultation_type": "plan_investigation"},
                ),
            ),
            (),
            (),
            "before_yara_proposal",
        ),
        (
            (
                invocation("sample.analyze", created_at=base),
                invocation(
                    "advisor.consult",
                    created_at=base + timedelta(seconds=1),
                    parameters={"consultation_type": "plan_investigation"},
                ),
                invocation("rule.propose"),
                invocation("rule.test"),
            ),
            (),
            (),
            "yara_revision_review",
        ),
        (
            (advisor, invocation("opinion.compose", created_at=base + timedelta(seconds=1))),
            (),
            (),
            "opinion_review_due",
        ),
    )

    measured = []
    for calls, observations, pivot_ids, expected_reason in fixtures:
        result = await build_advisor_guidance(
            invocations=calls,
            observations=observations,
            evidence_summaries=None,
            available_tools=(),
            pivot_observation_ids=pivot_ids,
        )
        assert result["checkpoint_due"] is True
        assert result["reason"] == expected_reason
        measured.append(expected_reason)

    assert len(measured) == 7
    assert DEFAULT_CHECKPOINT_TOOL_THRESHOLD == 12

    fallback_calls = (
        advisor,
        *(
            invocation(f"local.read.{index}", created_at=base + timedelta(seconds=index + 1))
            for index in range(12)
        ),
    )
    fallback = await build_advisor_guidance(
        invocations=fallback_calls,
        observations=(),
        evidence_summaries=None,
        available_tools=(),
    )
    assert fallback["reason"] == "tool_threshold_reached"


@pytest.mark.asyncio
async def test_complete_editorial_checkpoint_path_uses_six_bridge_turns_with_new_pivot():
    base = datetime.now(UTC)
    calls = [invocation("sample.analyze", created_at=base)]

    async def checkpoint(**kwargs):
        return await build_advisor_guidance(
            invocations=tuple(calls),
            observations=kwargs.get("observations", ()),
            evidence_summaries=None,
            available_tools=(),
            pivot_observation_ids=kwargs.get("pivot_observation_ids", ()),
        )

    initial = await checkpoint()
    assert initial["reason"] == "first_significant_decompilation"

    def add_advice(consultation_type: str, seconds: int) -> None:
        calls.append(
            invocation(
                "advisor.consult",
                created_at=base + timedelta(seconds=seconds),
                parameters={"consultation_type": consultation_type},
                consumed=1,
            )
        )

    add_advice("plan_investigation", 1)
    assert (await checkpoint())["reason"] == "before_yara_proposal"

    add_advice("yara_draft", 2)
    calls.extend(
        (
            invocation("rule.propose", created_at=base + timedelta(seconds=3)),
            invocation("rule.test", created_at=base + timedelta(seconds=4)),
        )
    )
    assert (await checkpoint())["reason"] == "yara_revision_review"

    add_advice("yara_draft", 5)
    pivot_id = uuid4()
    pivot = SimpleNamespace(id=pivot_id, observed_at=base + timedelta(seconds=6))
    assert (await checkpoint(observations=(pivot,), pivot_observation_ids=(pivot_id,)))[
        "reason"
    ] == "new_pivot_discovered"

    add_advice("pivot_strategy", 7)
    editorial_checkpoint = await checkpoint()
    assert editorial_checkpoint["reason"] == "editorial_draft_due"
    assert "Brouillon modifié" in editorial_checkpoint["reminder"]
    assert "texte courant" in editorial_checkpoint["reminder"]
    assert "pas d'historique" in editorial_checkpoint["reminder"]
    assert "hypothèse → pivot → succès/échec → coût" in editorial_checkpoint["reminder"].lower()

    add_advice("draft_text", 8)
    calls.append(
        invocation(
            "opinion.compose",
            created_at=base + timedelta(seconds=9),
        )
    )
    assert (await checkpoint())["reason"] == "opinion_review_due"

    add_advice("review_text", 10)
    completed = await checkpoint()
    assert completed["checkpoint_due"] is False
    assert completed["advisor_turns_used"] == 6
    assert completed["advisor_turns_remaining"] is None


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
    )

    assert result["checkpoint_due"] is True
    assert result["reason"] == "repeated_tool_block"
    assert result["suggested_consultation_type"] == "pivot_strategy"
    assert "autre mesure" in result["reminder"]
    assert "absence ≠ négatif" in result["reminder"]


@pytest.mark.asyncio
async def test_pivot_without_observation_requests_measurement_before_conclusion():
    base = datetime.now(UTC)
    result = await build_advisor_guidance(
        invocations=(
            invocation(
                "advisor.consult",
                created_at=base,
                parameters={"consultation_type": "pivot_strategy"},
            ),
            invocation("sample.get_feature_pack", created_at=base + timedelta(seconds=1)),
        ),
        observations=(),
        evidence_summaries=None,
        available_tools=(),
    )

    assert result["reason"] == "pivot_without_measurement"
    assert result["suggested_consultation_type"] == "pivot_strategy"
    assert "pas de conclusion" in result["reminder"]
    assert "absence ≠ négatif" in result["reminder"]


@pytest.mark.asyncio
async def test_pivot_with_observation_is_not_marked_unmeasured():
    base = datetime.now(UTC)
    observation_id = uuid4()
    observation = SimpleNamespace(id=observation_id, observed_at=base + timedelta(seconds=2))
    result = await build_advisor_guidance(
        invocations=(
            invocation(
                "advisor.consult",
                created_at=base,
                parameters={"consultation_type": "pivot_strategy"},
            ),
            invocation(
                "sample.get_feature_pack",
                created_at=base + timedelta(seconds=1),
                response_refs=(observation_id,),
            ),
        ),
        observations=(observation,),
        evidence_summaries=None,
        available_tools=(),
    )

    assert result["reason"] is None


@pytest.mark.asyncio
async def test_explicit_negative_result_is_measured_not_missing_data():
    base = datetime.now(UTC)
    observation_id = uuid4()
    observation = SimpleNamespace(
        id=observation_id,
        observed_at=base + timedelta(seconds=2),
        kind=EvidenceKind.NEGATIVE_RESULT,
    )
    result = await build_advisor_guidance(
        invocations=(
            invocation(
                "advisor.consult",
                created_at=base,
                parameters={"consultation_type": "pivot_strategy"},
            ),
            invocation(
                "sample.get_feature_pack",
                status=ToolInvocationStatus.EMPTY,
                created_at=base + timedelta(seconds=1),
                response_refs=(observation_id,),
            ),
        ),
        observations=(observation,),
        evidence_summaries=None,
        available_tools=(),
    )

    assert result["reason"] is None


@pytest.mark.asyncio
async def test_guidance_reports_usage_and_g2_remaining_without_local_cap():
    result = await build_advisor_guidance(
        invocations=(invocation("advisor.consult", consumed=69),),
        observations=(),
        evidence_summaries=None,
        available_tools=(),
        g2_bridge_turns_remaining=68,
    )

    assert result["advisor_turns_used"] == 69
    assert result["advisor_turn_limit"] is None
    assert result["advisor_turns_remaining"] is None
    assert result["g2_bridge_turns_remaining"] == 68
    assert AdvisorGuidanceV1.model_validate(result).g2_bridge_turns_remaining == 68


@pytest.mark.asyncio
async def test_guidance_reports_g2_exhaustion_without_local_cap():
    result = await build_advisor_guidance(
        invocations=(),
        observations=(),
        evidence_summaries=None,
        available_tools=(),
        g2_bridge_turns_remaining=0,
    )

    assert result["reason"] == "g2_bridge_budget_exhausted"
    assert result["advisor_turns_used"] == 0
    assert result["advisor_turn_limit"] is None
    assert result["advisor_turns_remaining"] is None
    assert result["g2_bridge_turns_remaining"] == 0


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
        advisor_turn_limit=None,
        advisor_turns_remaining=None,
    )

    bounded, truncated = bound_advisor_guidance(guidance, max_bytes=320)

    assert truncated is True
    assert bounded.truncated is True
    assert len(canonical_json_bytes(bounded.model_dump(mode="json"))) <= 320
