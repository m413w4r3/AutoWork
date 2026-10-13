from __future__ import annotations

import json
from uuid import uuid4

import pytest

from cti_app.application.investigation_contracts import (
    AgentTurnDecisionV1,
    ArticleClaimV1,
    ArticleContextV1,
    ContextChangeV1,
    ContextInvestigationV1,
    InvestigationContextV1,
    InvestigationDeltaV1,
    InvestigationSourceV1,
    parse_agent_turn_decision,
    parse_investigation_context,
)
from cti_app.application.investigation_tools import ToolEffect
from cti_app.domain.investigation import AgentNextAction


def _source() -> InvestigationSourceV1:
    return InvestigationSourceV1(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
    )


def _investigation(*, evidence=()):
    return ContextInvestigationV1(
        agenda=("Verify the loader claim", "Check the network attribution"),
        hypotheses=(),
        new_evidence=evidence,
        findings=(),
        sample_deltas=(),
        rule_deltas=(),
        previous_actions=(),
        blocked_capabilities=(),
        budget_remaining={"vt_units": 4, "shodan_credits": None},
    )


def test_full_context_has_versioned_schema_and_canonical_serialization() -> None:
    context = InvestigationContextV1(
        view="full",
        investigation_id=uuid4(),
        source=_source(),
        article=ArticleContextV1(
            short_summary="Article summary.",
            claims=(ArticleClaimV1(id="C1", statement="The domain is C2.", source_refs=("S1",)),),
            documented_samples=("b" * 64,),
            known_chain=(),
            open_questions=("Is the attribution supported?",),
        ),
        investigation=_investigation(),
        available_tools=(
            {"name": "vt.file_report", "schema_version": "1", "effect": ToolEffect.EXTERNAL_READ},
        ),
    )
    serialized = context.canonical_bytes()
    payload = json.loads(serialized)
    assert payload["schema"] == "investigation-context.v1"
    assert payload["view"] == "full"
    assert InvestigationContextV1.json_schema()["properties"]["schema"]
    assert parse_investigation_context(serialized).canonical_bytes() == serialized


def test_delta_context_cannot_reembed_article_content() -> None:
    context = InvestigationContextV1(
        view="delta",
        investigation_id=uuid4(),
        source=_source(),
        article=None,
        investigation=_investigation(),
        delta=InvestigationDeltaV1(
            changes=(
                ContextChangeV1(
                    kind="evidence", identity=str(uuid4()), summary="One short update."
                ),
            ),
            stable_hypotheses=(),
        ),
        available_tools=(),
    )
    assert b"full article payload" not in context.canonical_bytes()
    assert json.loads(context.canonical_bytes())["article"] is None

    with pytest.raises(ValueError, match="cannot re-embed article"):
        InvestigationContextV1(
            view="delta",
            investigation_id=uuid4(),
            source=_source(),
            article=ArticleContextV1(
                short_summary="This article body must not be repeated.",
                claims=(),
                documented_samples=(),
                known_chain=(),
                open_questions=(),
            ),
            investigation=_investigation(),
            delta=InvestigationDeltaV1(changes=(), stable_hypotheses=()),
            available_tools=(),
        )


def test_agent_decision_supports_all_five_actions_and_free_arguments() -> None:
    for action in AgentNextAction:
        decision = AgentTurnDecisionV1(
            reasoning_summary="A concise decision summary.",
            goal="Check one observation against claim C1.",
            hypothesis_refs=("H1",),
            proposed_tool_calls=(
                {
                    "tool_name": "vt.search",
                    "schema_version": "2",
                    "arguments": {
                        "provider_specific_expression": "content:loader",
                        "future_option": 3,
                    },
                    "purpose": "Find related reports.",
                },
            ),
            finding_proposals=(),
            opinion_delta=None,
            next_action=action,
        )
        assert parse_agent_turn_decision(decision.canonical_bytes()).next_action is action
    assert {action.value for action in AgentNextAction} == {
        "continue",
        "switch_hypothesis",
        "draft_opinion",
        "finish",
        "request_permission",
    }


def test_bridge_parser_ignores_unknown_fields_but_validates_known_fields_strictly() -> None:
    payload = {
        "schema": "agent-turn-decision.v1",
        "reasoning_summary": "The next query is useful.",
        "goal": "Check the hash.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": "continue",
        "future_optional_field": {"display_hint": "ignore safely"},
    }
    assert parse_agent_turn_decision(payload).next_action is AgentNextAction.CONTINUE
    payload["next_action"] = "do_everything"
    with pytest.raises(ValueError, match="Invalid agent turn decision"):
        parse_agent_turn_decision(payload)


def test_context_rejects_signed_urls_and_secret_fields() -> None:
    with pytest.raises(ValueError, match="secret"):
        InvestigationContextV1(
            view="full",
            investigation_id=uuid4(),
            source=_source(),
            article=ArticleContextV1(
                short_summary="Summary",
                claims=(),
                documented_samples=(),
                known_chain=({"api_key": "redacted"},),
                open_questions=(),
            ),
            investigation=_investigation(),
            available_tools=(),
        )


def test_context_rejects_signed_url_values() -> None:
    with pytest.raises(ValueError, match="signed URL"):
        InvestigationContextV1(
            view="full",
            investigation_id=uuid4(),
            source=_source(),
            article=ArticleContextV1(
                short_summary="Summary",
                claims=(),
                documented_samples=(),
                known_chain=(
                    {"source": "https://example.invalid/report?X-Goog-Signature=private"},
                ),
                open_questions=(),
            ),
            investigation=_investigation(),
            available_tools=(),
        )
