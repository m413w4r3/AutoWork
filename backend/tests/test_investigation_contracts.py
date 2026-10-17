from __future__ import annotations

import json
from uuid import uuid4

import pytest

from cti_app.application.investigation_acquisition_tools import VT_DOWNLOAD_SAMPLE_SCHEMA
from cti_app.application.investigation_contracts import (
    AgentTurnDecisionV1,
    ArticleClaimV1,
    ArticleContextV1,
    ContextChangeV1,
    ContextInvestigationV1,
    DecisionValidationCode,
    InvestigationContextV1,
    InvestigationDeltaV1,
    InvestigationSourceV1,
    parse_agent_turn_decision,
    parse_investigation_context,
    validate_decision_hypothesis_references,
)
from cti_app.application.investigation_rule_tools import RULE_PROPOSE_SCHEMA, RULE_REVISE_SCHEMA
from cti_app.application.investigation_tools import ToolEffect, validate_json_schema
from cti_app.domain.investigation import AgentNextAction
from cti_app.domain.investigation_state import (
    DecisionValidationCode as DomainDecisionValidationCode,
)


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


def test_compact_decision_parses_explicit_operator_request_acknowledgements() -> None:
    request_id = uuid4()
    decision = parse_agent_turn_decision(
        "@@DECISION@@\nGOAL: Address the operator request.\n"
        "SUMMARY: The requested check is complete.\nNEXT: finish\n"
        f"ADDRESSED_REQUESTS: {request_id}\n@@END@@"
    )

    assert decision.addressed_request_ids == (request_id,)


def test_compact_tool_arguments_follow_registered_json_schema_types() -> None:
    schemas = {
        ("shodan.count", "1"): {
            "type": "object",
            "properties": {
                "justification": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
        },
        ("vt.download_sample", "1"): VT_DOWNLOAD_SAMPLE_SCHEMA,
        ("rule.propose", "1"): RULE_PROPOSE_SCHEMA,
    }
    wire = """@@DECISION@@
GOAL: Preserve typed arguments.
SUMMARY: Strings keep punctuation while structured arguments stay typed.
NEXT: continue
@@END@@
@@TOOL@@
NAME: shodan.count
VERSION: 1
PURPOSE: Count the matching host.
ARG justification: Verify one pass, then compare the result with the article.
ARG evidence_refs: []
@@END@@
@@TOOL@@
NAME: vt.download_sample
VERSION: 1
PURPOSE: Download the documented sample.
ARG sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
ARG purpose: Review the sample; then preserve its source context.
ARG evidence_refs: "synthetic, reference", second
@@END@@
@@TOOL@@
NAME: rule.propose
VERSION: 1
PURPOSE: Propose a bounded rule.
ARG meta: {"classification":"trojan","description":"synthetic metadata"}
@@END@@"""

    decision = parse_agent_turn_decision(wire, tool_schemas=schemas)

    calls = {item.tool_name: item.arguments for item in decision.proposed_tool_calls}
    assert calls["shodan.count"]["justification"] == (
        "Verify one pass, then compare the result with the article."
    )
    assert calls["shodan.count"]["evidence_refs"] == []
    assert calls["vt.download_sample"]["purpose"] == (
        "Review the sample; then preserve its source context."
    )
    assert calls["vt.download_sample"]["evidence_refs"] == ["synthetic, reference", "second"]
    assert calls["rule.propose"]["meta"] == {
        "classification": "trojan",
        "description": "synthetic metadata",
    }


def test_compact_rule_motifs_accept_multiline_json_for_real_rule_schemas() -> None:
    candidate_id = str(uuid4())
    evidence_id = str(uuid4())
    motif_schema = RULE_REVISE_SCHEMA["properties"]["motifs"]["items"]["properties"]
    motif = {
        "kind": motif_schema["kind"]["enum"][0],
        "value": 'synthetic ] } "quoted" pattern',
        "provenance_kind": motif_schema["provenance_kind"]["enum"][0],
        "provenance_id": "synthetic-source",
        "observation_refs": [evidence_id],
    }
    motifs = json.dumps([motif], indent=2)
    condition = RULE_PROPOSE_SCHEMA["properties"]["condition"]["enum"][0]
    wire = f"""@@DECISION@@
GOAL: Preserve multiline rule arguments.
SUMMARY: Both real rule schemas accept a structured motif array.
NEXT: continue
@@END@@
@@TOOL@@
NAME: rule.propose
VERSION: 1
PURPOSE: Propose a synthetic detection condition.
ARG condition: {condition}
ARG motifs: {motifs}
@@END@@
@@TOOL@@
NAME: rule.revise
VERSION: 1
PURPOSE: Revise the synthetic candidate.
ARG candidate_id: {candidate_id}
ARG parent_version: 1
ARG condition: {condition}
ARG motifs: {motifs}
@@END@@"""

    decision = parse_agent_turn_decision(
        wire,
        tool_schemas={
            ("rule.propose", "1"): RULE_PROPOSE_SCHEMA,
            ("rule.revise", "1"): RULE_REVISE_SCHEMA,
        },
    )

    calls = {item.tool_name: item.arguments for item in decision.proposed_tool_calls}
    assert calls["rule.propose"]["motifs"] == [motif]
    assert calls["rule.revise"]["motifs"] == [motif]
    assert calls["rule.revise"]["candidate_id"] == candidate_id
    validate_json_schema(calls["rule.propose"], RULE_PROPOSE_SCHEMA)
    validate_json_schema(calls["rule.revise"], RULE_REVISE_SCHEMA)


def test_compact_rejects_unclosed_json_argument_before_next_arg_or_end() -> None:
    for boundary in ("ARG condition: all", "@@END@@", "@@FINDING@@"):
        wire = (
            "@@DECISION@@\nGOAL: Reject the incomplete argument.\n"
            "SUMMARY: An incomplete JSON value must not be partially accepted.\n"
            "NEXT: finish\n@@END@@\n@@TOOL@@\nNAME: rule.revise\nVERSION: 1\n"
            "PURPOSE: Revise a synthetic candidate.\nARG motifs: [\n  {\n"
            f"{boundary}"
        )
        with pytest.raises(ValueError, match=r"TOOL .*ARG motifs.*not closed"):
            parse_agent_turn_decision(
                wire,
                tool_schemas={("rule.revise", "1"): RULE_REVISE_SCHEMA},
            )


def test_compact_multiline_json_argument_has_a_line_bound() -> None:
    motifs = (
        "[\n"
        + ",\n".join(
            json.dumps({"kind": "literal", "value": f"synthetic-{index}"}, indent=2)
            for index in range(32)
        )
        + "\n]"
    )
    wire = (
        "@@DECISION@@\nGOAL: Keep structured arguments bounded.\n"
        "SUMMARY: This oversized line sequence must be rejected.\nNEXT: finish\n@@END@@\n"
        "@@TOOL@@\nNAME: rule.revise\nVERSION: 1\nPURPOSE: Revise a candidate.\n"
        f"ARG motifs: {motifs}\n@@END@@"
    )

    with pytest.raises(
        ValueError, match=r"TOOL rule\.revise\.ARG motifs JSON value exceeds 128 lines"
    ):
        parse_agent_turn_decision(wire)


def test_compact_json_argument_has_a_byte_bound() -> None:
    oversized = '{"value":"' + ("x" * (32 * 1024)) + '"}'
    wire = (
        "@@DECISION@@\nGOAL: Keep structured arguments byte bounded.\n"
        "SUMMARY: This JSON value exceeds the per-argument limit.\nNEXT: finish\n@@END@@\n"
        "@@TOOL@@\nNAME: rule.revise\nVERSION: 1\nPURPOSE: Revise a candidate.\n"
        f"ARG motifs: {oversized}\n@@END@@"
    )

    with pytest.raises(
        ValueError, match=r"TOOL rule\.revise\.ARG motifs JSON value exceeds 32768 bytes"
    ):
        parse_agent_turn_decision(wire)


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (
            "@@TOOL@@\nNAME: article.get_claims\nPURPOSE: Read claims.\nARG claim_ids: C1\n@@END@@",
            "TOOL",
        ),
        (
            "@@FINDING@@\nKIND: capability\nASSESSMENT: qualified\n@@END@@",
            "FINDING",
        ),
        (
            "@@NOTE@@\nANCHOR: lead:0001\nSTATE: observed\nRELATION: confirms\n"
            "TEXT: A synthetic observation.\nFINDINGS: invalid-uuid\n@@END@@",
            "NOTE",
        ),
    ],
)
def test_rejected_tool_finding_and_note_blocks_reject_the_whole_decision(block, message) -> None:
    wire = (
        "@@DECISION@@\nGOAL: Keep the entire response atomic.\n"
        "SUMMARY: No decision with a rejected block may be partially accepted.\n"
        f"NEXT: finish\n@@END@@\n{block}"
    )

    with pytest.raises(ValueError, match=message):
        parse_agent_turn_decision(wire)


def test_opinion_finding_refs_accept_whitespace_separated_uuids_and_reject_bad_refs() -> None:
    finding_refs = (uuid4(), uuid4())
    prefix = (
        "@@DECISION@@\nGOAL: Preserve all opinion citations.\n"
        "SUMMARY: The references are separated by whitespace.\nNEXT: finish\n@@END@@\n"
        "@@OPINION@@\nTYPE: key_findings\nTEXT: A synthetic inference.\n"
        f"FINDINGS: {finding_refs[0]} {finding_refs[1]}\n"
        "QUALIFICATION: inference\nCERTAINTY: 0.6\n@@END@@"
    )

    parsed = parse_agent_turn_decision(prefix)

    assert parsed.opinion_delta is not None
    assert parsed.opinion_delta.blocks[0].finding_refs == finding_refs
    with pytest.raises(ValueError, match=r"OPINION\.FINDING_REFS.*invalid UUID"):
        parse_agent_turn_decision(prefix.replace(f"{finding_refs[1]}", "not-a-uuid"))


def test_compact_decision_parses_structured_anchored_notes() -> None:
    finding_id = uuid4()
    evidence_id = uuid4()
    decision = parse_agent_turn_decision(
        "@@DECISION@@\nGOAL: Record a note at a verified paragraph.\n"
        "SUMMARY: The cited observation supports this local statement.\nNEXT: continue\n"
        "@@END@@\n@@NOTE@@\nANCHOR: lead:0001\nSTATE: observed\n"
        "RELATION: confirms\nTEXT: The paragraph describes the same loader stage.\n"
        f"FINDINGS: {finding_id}\nEVIDENCE: {evidence_id}\n@@END@@"
    )

    assert len(decision.opinion_delta.notes) == 1
    note = decision.opinion_delta.notes[0]
    assert note.anchor == "lead:0001"
    assert note.finding_refs == (finding_id,)
    assert note.evidence_refs == (evidence_id,)


def test_compact_opinion_certainty_accepts_numeric_prefix_and_rejects_verbal_only() -> None:
    finding_id = uuid4()
    evidence_id = uuid4()
    prefix = (
        "@@DECISION@@\nGOAL: Draft a qualified opinion.\nSUMMARY: Keep uncertainty explicit.\n"
        "NEXT: finish\n@@END@@\n@@OPINION@@\nTYPE: key_findings\n"
        "TEXT: The synthetic finding remains qualified.\n"
        f"FINDINGS: {finding_id}\nEVIDENCE: {evidence_id}\n"
        "QUALIFICATION: inference\nCERTAINTY: 0.6 (moderate confidence)\n@@END@@"
    )
    verbal = prefix.replace("0.6 (moderate confidence)", "moderate confidence")

    parsed = parse_agent_turn_decision(prefix)

    assert parsed.opinion_delta is not None
    assert parsed.opinion_delta.blocks[0].certainty == 0.6
    with pytest.raises(ValueError, match=r"line \d+: OPINION\.CERTAINTY must be a number"):
        parse_agent_turn_decision(verbal)


def test_compact_decision_rejects_malformed_anchored_note_fields() -> None:
    with pytest.raises(ValueError, match="Invalid agent turn decision"):
        parse_agent_turn_decision(
            "@@DECISION@@\nGOAL: Record a note.\nSUMMARY: Verify the fields.\n"
            "NEXT: continue\n@@END@@\n@@NOTE@@\nANCHOR: lead:0001\n"
            "STATE: observed\nRELATION: confirms\nTEXT: Missing required evidence.\n"
            "FINDINGS: not-a-uuid\n@@END@@"
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


def test_legacy_decision_json_without_hypothesis_refs_is_readable_and_replayable() -> None:
    legacy_json = (
        '{"schema":"agent-turn-decision.v1","reasoning_summary":"Keep investigating.",'
        '"goal":"Check one claim.","proposed_tool_calls":[],"finding_proposals":['
        '{"kind":"claim_verification","assessment":"inconclusive",'
        '"statement":"The available evidence is incomplete.","subject_ref":null,'
        '"observation_refs":[],"claim_refs":[],"novelty":null,"scope":"",'
        '"limitations":[]}],"opinion_delta":null,"next_action":"continue"}'
    )

    first = parse_agent_turn_decision(legacy_json)
    replayed = parse_agent_turn_decision(legacy_json)

    assert first.hypothesis_refs == ()
    assert first.finding_proposals[0].hypothesis_refs == ()
    assert first.canonical_bytes() == replayed.canonical_bytes()
    assert validate_decision_hypothesis_references(first, known_hypothesis_ids=set()) is None


def test_compact_agent_turn_wire_parses_all_decision_blocks() -> None:
    observation_id = uuid4()
    wire = f"""Preamble ignored by the parser.
@@DECISION@@
GOAL: Check claim C1 against the observation.
SUMMARY: The observation confirms one part of the claim.
NEXT: continue
HYPOTHESES: H1, H2
@@END@@
@@TOOL@@
NAME: article.get_claims
VERSION: 1
PURPOSE: Compare the cited claims.
ARG claim_ids: ["C1", "C2"]
ARG query: loader family
@@END@@
@@FINDING@@
KIND: claim_verification
ASSESSMENT: supported
STATEMENT: The observation supports the claim.
OBSERVATIONS: {observation_id}
CLAIMS: C1; C2
HYPOTHESES: H1
NOVELTY: new_to_article
SCOPE: Current sample only.
LIMITATIONS: One observation, small sample
@@END@@
@@OPINION@@
TYPE: key_findings
TEXT: The observation supports claim C1.
FINDINGS: {observation_id}
EVIDENCE: {observation_id}
QUALIFICATION: inference
CERTAINTY: 0.8
UNRESOLVED: Attribution remains open.
@@END@@
Postface ignored by the parser.
"""

    decision = parse_agent_turn_decision(wire)

    assert decision.goal == "Check claim C1 against the observation."
    assert decision.reasoning_summary == "The observation confirms one part of the claim."
    assert decision.hypothesis_refs == ("H1", "H2")
    assert decision.proposed_tool_calls[0].arguments == {
        "claim_ids": ["C1", "C2"],
        "query": "loader family",
    }
    finding = decision.finding_proposals[0]
    assert finding.observation_refs == (observation_id,)
    assert finding.claim_refs == ("C1", "C2")
    assert finding.hypothesis_refs == ("H1",)
    assert decision.opinion_delta is not None
    assert decision.opinion_delta.blocks[0].block_type.value == "key_findings"
    assert decision.opinion_delta.unresolved_points == ("Attribution remains open.",)


def test_compact_agent_turn_wire_accepts_minimal_and_tolerant_variants() -> None:
    wire = """Bridge preamble.
```text
@@decision@@
goal: Verify a bounded claim.
summary: First summary line
second summary line continues the value.
next: CONTINUE
hypotheses: H1, H2; H3
H4
```"""

    decision = parse_agent_turn_decision(wire)
    minimal = parse_agent_turn_decision(
        "@@DECISION@@\nGOAL: Finish\nSUMMARY: No further evidence is needed.\nNEXT: finish"
    )

    assert decision.reasoning_summary == (
        "First summary line\nsecond summary line continues the value."
    )
    assert decision.hypothesis_refs == ("H1", "H2", "H3", "H4")
    assert decision.next_action is AgentNextAction.CONTINUE
    assert minimal.proposed_tool_calls == ()
    assert minimal.finding_proposals == ()
    assert minimal.opinion_delta is None


def test_adopted_turn_zero_json_and_fenced_legacy_json_remain_readable() -> None:
    adopted_turn_zero = (
        '{"schema":"agent-turn-decision.v1","reasoning_summary":"…","goal":"…",'
        '"hypothesis_refs":[],"proposed_tool_calls":[{"tool_name":"article.get_claims",'
        '"schema_version":"1","arguments":{"claim_ids":["S3P4.1","S4P4.1"]},'
        '"purpose":"…"}],"finding_proposals":[],"opinion_delta":null,'
        '"next_action":"continue"}'
    )

    direct = parse_agent_turn_decision(adopted_turn_zero)
    fenced = parse_agent_turn_decision(f"```json\n{adopted_turn_zero}\n```")

    assert direct.canonical_bytes() == fenced.canonical_bytes()
    assert direct.proposed_tool_calls[0].arguments["claim_ids"] == ["S3P4.1", "S4P4.1"]


@pytest.mark.parametrize(
    ("wire", "message"),
    [
        (
            "@@DECISION@@\nGOAL: Check\nSUMMARY: Continue\nNEXT: teleport",
            "NEXT",
        ),
        (
            "@@DECISION@@\nGOAL: Check\nSUMMARY: Continue\nNEXT: continue\n"
            "@@DECISION@@\nGOAL: Again\nSUMMARY: Again\nNEXT: finish",
            "duplicate @@DECISION@@",
        ),
        (
            "@@DECISION@@\nGOAL: Check\nSUMMARY: Continue\nNEXT: continue\n"
            "@@TOOL@@\nPURPOSE: Missing a name\n@@END@@",
            "TOOL.NAME",
        ),
    ],
)
def test_compact_agent_turn_wire_rejects_unusable_required_values(wire: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_agent_turn_decision(wire)


@pytest.mark.parametrize(
    "wire",
    [
        "@@DECISION@@\nGOAL: Check\nSUMMARY: Report\nNEXT: finish\n"
        "@@TOOL@@\nNAME: x\nVERSION: 1\nPURPOSE: Read\nARG access_token: private\n@@END@@",
        "@@DECISION@@\nGOAL: Check\nSUMMARY: Report\nNEXT: finish\n"
        "@@TOOL@@\nNAME: x\nVERSION: 1\nPURPOSE: Read\nARG api_key: private\n@@END@@",
        "@@DECISION@@\nGOAL: Check\nSUMMARY: https://x.invalid/a?X-Amz-Signature="
        "secret\nNEXT: finish",
    ],
)
def test_compact_agent_turn_wire_rejects_secret_fields_and_signed_urls(wire: str) -> None:
    with pytest.raises(ValueError, match=r"secret|signed URL"):
        parse_agent_turn_decision(wire)


def test_malformed_tool_rejects_the_whole_decision_with_a_targeted_error() -> None:
    wire = """@@DECISION@@
GOAL: Finish safely.
SUMMARY: The malformed proposal must not be partially retained.
NEXT: finish
@@END@@
@@TOOL@@
NAME: article.get_claims
VERSION: 1
PURPOSE: Read claims.
ARG claim_ids: [not valid JSON
@@END@@"""

    with pytest.raises(ValueError, match=r"TOOL article\.get_claims\.ARG claim_ids.*not closed"):
        parse_agent_turn_decision(wire)


def test_malformed_finding_field_rejects_the_whole_decision_with_a_targeted_error() -> None:
    wire = """@@DECISION@@
GOAL: Finish.
SUMMARY: Keep the valid decision.
NEXT: finish
@@END@@
@@FINDING@@
KIND: claim_verification
ASSESSMENT: inconclusive
STATEMENT: The source is incomplete.
NOVELTY: invented_value
@@END@@"""

    with pytest.raises(ValueError, match=r"FINDING block rejected: FINDING\.NOVELTY"):
        parse_agent_turn_decision(wire)


def test_hypothesis_references_have_stable_typed_rejection_codes() -> None:
    assert DecisionValidationCode is DomainDecisionValidationCode
    unknown = AgentTurnDecisionV1(
        reasoning_summary="Check an unresolved attribution.",
        goal="Compare the new observation with the attribution hypothesis.",
        hypothesis_refs=("missing",),
        proposed_tool_calls=(),
        finding_proposals=(),
        next_action=AgentNextAction.CONTINUE,
    )
    unknown_issue = validate_decision_hypothesis_references(unknown, known_hypothesis_ids={"known"})
    assert unknown_issue is not None
    assert unknown_issue.code is DecisionValidationCode.UNKNOWN_HYPOTHESIS_REFERENCE

    multi_target_finding = parse_agent_turn_decision(
        {
            "reasoning_summary": "One finding cannot settle two hypotheses.",
            "goal": "Keep the two attributions separate.",
            "hypothesis_refs": ["a", "b"],
            "proposed_tool_calls": [],
            "finding_proposals": [
                {
                    "kind": "claim_verification",
                    "assessment": "supported",
                    "statement": "The cited observation supports both claims.",
                    "observation_refs": [str(uuid4())],
                    "claim_refs": [],
                    "hypothesis_refs": ["a", "b"],
                }
            ],
            "next_action": "continue",
        }
    )
    multi_issue = validate_decision_hypothesis_references(
        multi_target_finding, known_hypothesis_ids={"a", "b"}
    )
    assert multi_issue is not None
    assert multi_issue.code is DecisionValidationCode.FINDING_LINKS_MULTIPLE_HYPOTHESES


def test_duplicate_finding_identity_with_conflicting_links_has_stable_rejection_code() -> None:
    proposal = {
        "kind": "claim_verification",
        "assessment": "inconclusive",
        "statement": "The evidence remains incomplete.",
        "observation_refs": [],
        "claim_refs": [],
    }
    linked_a = {**proposal, "hypothesis_refs": ["a"]}
    linked_b = {**proposal, "hypothesis_refs": ["b"]}
    decision = parse_agent_turn_decision(
        {
            "reasoning_summary": "Keep two hypotheses distinct.",
            "goal": "Avoid assigning one finding to both hypotheses.",
            "hypothesis_refs": ["a", "b"],
            "finding_proposals": [linked_a, linked_b],
            "next_action": "continue",
        }
    )

    assert (
        decision.finding_proposals[0].persistent_identity_bytes()
        == decision.finding_proposals[1].persistent_identity_bytes()
    )
    issue = validate_decision_hypothesis_references(decision, known_hypothesis_ids={"a", "b"})

    assert issue is not None
    assert issue.code is DecisionValidationCode.DUPLICATE_FINDING_IDENTITY_CONFLICTING_LINKS


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
