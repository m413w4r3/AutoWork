import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.bridge_investigator import (
    BridgeInvestigator,
    ModelConversationTurnTransport,
    ScriptedTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
    make_investigation_turn_idempotency_key,
)
from cti_app.application.investigation_contracts import (
    ArticleContextV1,
    ContextInvestigationV1,
    InvestigationContextV1,
    InvestigationSourceV1,
)
from cti_app.domain.investigation import AgentNextAction
from cti_app.domain.model_conversations import ConversationTurnStatus


def _context(investigation_id):
    return InvestigationContextV1(
        view="full",
        investigation_id=investigation_id,
        source=InvestigationSourceV1(
            subject_id=uuid4(),
            production_run_id=uuid4(),
            synthesis_artifact_id=uuid4(),
            synthesis_sha256="a" * 64,
        ),
        article=ArticleContextV1(
            short_summary="A bounded source narrative.",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
        investigation=ContextInvestigationV1(
            agenda=("Question one", "Question two"),
            hypotheses=(),
            new_evidence=(),
            findings=(),
            sample_deltas=(),
            rule_deltas=(),
            previous_actions=(),
            blocked_capabilities=(),
            budget_remaining={},
        ),
        available_tools=(),
    )


@pytest.mark.asyncio
async def test_turn_is_structured_and_replayed_by_deterministic_identity() -> None:
    investigation_id = uuid4()
    response = {
        "reasoning_summary": "The cited chain needs a passive check.",
        "goal": "Check one network relationship.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": AgentNextAction.FINISH.value,
    }
    transport = ScriptedTurnTransport([response])
    bridge = BridgeInvestigator(transport)
    context = _context(investigation_id)

    first = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )
    replay = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert first.decision is not None
    assert first.decision.next_action is AgentNextAction.FINISH
    assert replay.idempotency_key == first.idempotency_key
    assert transport.submission_count == 1
    assert "@@DECISION@@" in transport.prompts[0]
    assert "AGENT_TURN_DECISION_SCHEMA" not in transport.prompts[0]
    assert "no Markdown, backticks" in transport.prompts[0]
    assert "JSON objects and arrays may span multiple lines" in transport.prompts[0]
    assert "close before the next ARG or @@ marker" in transport.prompts[0]
    assert "BEGIN_CANONICAL_INVESTIGATION_CONTEXT" in transport.prompts[0]
    assert "only existing hypothesis IDs listed in the context" in transport.prompts[0]
    assert "do not create hypothesis labels" in transport.prompts[0].lower()


@pytest.mark.asyncio
async def test_abandoned_attempt_uses_a_distinct_deterministic_submission_key() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    response = {
        "reasoning_summary": "The answer can be processed.",
        "goal": "Finish the investigation.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": AgentNextAction.FINISH.value,
    }
    transport = ScriptedTurnTransport([TurnSubmission(TurnSubmissionStatus.NEEDS_REVIEW), response])
    bridge = BridgeInvestigator(transport)
    common = {
        "investigation_id": investigation_id,
        "subject_id": context.source.subject_id,
        "turn_index": 0,
        "context": context,
        "external_llm_allowed": True,
    }

    abandoned = await bridge.turn(**common)
    retry = await bridge.turn(**common, attempt=1)

    assert abandoned.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert retry.status is TurnSubmissionStatus.SUCCEEDED
    assert retry.idempotency_key != abandoned.idempotency_key
    assert retry.idempotency_key.endswith(":attempt=1")
    assert transport.submission_count == 2


@pytest.mark.asyncio
async def test_confirmed_pre_submission_failure_retries_with_distinct_attempt_key() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    response = {
        "reasoning_summary": "The requested work can now finish.",
        "goal": "Finish the investigation.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": AgentNextAction.FINISH.value,
    }
    transport = ScriptedTurnTransport(
        [
            TurnSubmission(
                TurnSubmissionStatus.UNAVAILABLE,
                detail="composer not ready before submission",
                error_code="bridge_server_error",
                submission_state="pre_submission",
                retryable=True,
            ),
            response,
        ]
    )
    bridge = BridgeInvestigator(transport)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=4,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert result.decision is not None
    assert transport.submission_count == 2
    assert len(transport._by_key) == 2
    assert any(key.endswith(":attempt=1") for key in transport._by_key)
    assert result.submissions_made == 1


@pytest.mark.asyncio
async def test_ambiguous_bridge_failure_is_never_retried_automatically() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    transport = ScriptedTurnTransport(
        [
            TurnSubmission(
                TurnSubmissionStatus.NEEDS_REVIEW,
                detail="provider may have accepted the request",
                error_code="bridge_timeout",
                submission_state="submission_attempted",
                retryable=True,
            ),
            {"next_action": "finish"},
        ]
    )
    bridge = BridgeInvestigator(transport)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=4,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert transport.submission_count == 1
    assert len(transport._responses) == 1


@pytest.mark.asyncio
async def test_missing_observation_downgrades_supported_proposal_with_trace() -> None:
    investigation_id = uuid4()
    response = {
        "reasoning_summary": "The source text may be right.",
        "goal": "Record the possible chain.",
        "finding_proposals": [
            {
                "kind": "infection_chain",
                "assessment": "supported",
                "statement": "A loader retrieves the final payload.",
                "observation_refs": [],
                "claim_refs": [],
                "novelty": None,
                "scope": "article claim",
                "limitations": [],
            }
        ],
        "next_action": "continue",
    }
    transport = ScriptedTurnTransport([response])
    bridge = BridgeInvestigator(transport)
    context = _context(investigation_id)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.decision is not None
    proposal = result.decision.finding_proposals[0]
    assert proposal.assessment.value == "inconclusive"
    assert "downgraded" in result.decision.reasoning_summary


@pytest.mark.asyncio
async def test_external_permission_is_checked_before_transport() -> None:
    investigation_id = uuid4()
    transport = ScriptedTurnTransport([])
    bridge = BridgeInvestigator(transport)
    context = _context(investigation_id)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=False,
    )

    assert result.status is TurnSubmissionStatus.BLOCKED
    assert transport.attempt_count == 0
    assert (
        len(
            make_investigation_turn_idempotency_key(
                investigation_id=investigation_id,
                turn_index=0,
                context_sha256=hashlib.sha256(context.canonical_bytes()).hexdigest(),
            )
        )
        == 64
    )


@pytest.mark.asyncio
async def test_needs_review_submission_is_cached_without_new_script_submission() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    transport = ScriptedTurnTransport(
        [TurnSubmission(TurnSubmissionStatus.NEEDS_REVIEW, detail="uncertain click"), "never used"]
    )
    bridge = BridgeInvestigator(transport)

    first = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )
    second = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert first.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert second.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert transport.submission_count == 1
    assert len(transport._responses) == 1


@pytest.mark.asyncio
async def test_invalid_compact_output_gets_a_precise_corrective_turn() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    valid = {
        "reasoning_summary": "Continue after the corrected response.",
        "goal": "Finish the investigation.",
        "next_action": "finish",
    }
    # The former malformed-JSON case now checks a malformed compact field instead.
    invalid = (
        "@@DECISION@@\nGOAL: Finish the investigation.\n"
        "SUMMARY: The work is complete.\nNEXT: teleport\n@@END@@"
    )
    transport = ScriptedTurnTransport([invalid, valid])
    bridge = BridgeInvestigator(transport)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert result.decision is not None
    assert result.decision.goal == "Finish the investigation."
    assert transport.submission_count == 2
    assert len(transport._by_key) == 2
    assert "schema_rejection" in transport.prompts[1]
    assert "DECISION.NEXT value is unknown" in transport.prompts[1]


@pytest.mark.asyncio
async def test_invalid_opinion_certainty_gets_corrected_before_finish_is_accepted() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    finding_id = uuid4()
    evidence_id = uuid4()
    decision = (
        "@@DECISION@@\nGOAL: Draft an evidence-linked opinion.\n"
        "SUMMARY: State the inference with calibrated certainty.\nNEXT: finish\n@@END@@"
    )
    invalid_opinion = (
        "@@OPINION@@\nTYPE: key_findings\nTEXT: A synthetic qualified finding.\n"
        f"FINDINGS: {finding_id}\nEVIDENCE: {evidence_id}\n"
        "QUALIFICATION: inference\nCERTAINTY: moderate confidence\n@@END@@"
    )
    proposed_tool = (
        "@@TOOL@@\nNAME: article.get_claims\nVERSION: 1\n"
        "PURPOSE: Read one synthetic claim.\nARG claim_ids: C1\n@@END@@"
    )
    corrected_opinion = invalid_opinion.replace("CERTAINTY: moderate confidence", "CERTAINTY: 0.6")
    transport = ScriptedTurnTransport(
        [
            decision + "\n" + proposed_tool + "\n" + invalid_opinion,
            decision + "\n" + corrected_opinion,
        ]
    )

    result = await BridgeInvestigator(transport).turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert result.decision is not None
    assert result.decision.opinion_delta is not None
    assert result.decision.opinion_delta.blocks[0].certainty == 0.6
    assert result.decision.proposed_tool_calls == ()
    assert transport.submission_count == 2
    assert "OPINION.CERTAINTY must be a number" in transport.prompts[1]


@pytest.mark.asyncio
async def test_invalid_opinion_finding_refs_get_corrected_instead_of_dropped() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    finding_id = uuid4()
    evidence_id = uuid4()
    decision = (
        "@@DECISION@@\nGOAL: Draft a cited synthetic opinion.\n"
        "SUMMARY: Keep every citation valid.\nNEXT: finish\n@@END@@"
    )
    invalid_opinion = (
        "@@OPINION@@\nTYPE: key_findings\nTEXT: A synthetic qualified finding.\n"
        "FINDINGS: not-a-uuid\n"
        f"EVIDENCE: {evidence_id}\n"
        "QUALIFICATION: inference\nCERTAINTY: 0.6\n@@END@@"
    )
    corrected_opinion = invalid_opinion.replace("FINDINGS: not-a-uuid", f"FINDINGS: {finding_id}")
    transport = ScriptedTurnTransport(
        [decision + "\n" + invalid_opinion, decision + "\n" + corrected_opinion]
    )

    result = await BridgeInvestigator(transport).turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert result.decision is not None
    assert result.decision.opinion_delta is not None
    assert result.decision.opinion_delta.blocks[0].finding_refs == (finding_id,)
    assert transport.submission_count == 2
    assert "OPINION.FINDING_REFS contains an invalid UUID reference" in transport.prompts[1]


@pytest.mark.asyncio
async def test_invalid_output_stops_after_two_corrective_attempts() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    transport = ScriptedTurnTransport(["bad one", "[]", "{}", "valid later"])
    bridge = BridgeInvestigator(transport)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.INVALID_OUTPUT
    assert result.decision is None
    assert "Corrective attempts exhausted" in result.detail
    assert transport.submission_count == 3
    assert len(transport._by_key) == 3
    assert len(transport._responses) == 1


@pytest.mark.asyncio
async def test_malformed_tool_block_is_corrected_before_any_tool_call_is_returned() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    valid_tool = """@@TOOL@@
NAME: article.get_claims
VERSION: 1
PURPOSE: Read the synthetic claim.
ARG claim_ids: C1
@@END@@"""
    invalid_output = """@@DECISION@@
GOAL: Finish without the malformed tool.
SUMMARY: The tool arguments cannot be parsed safely.
NEXT: finish
@@END@@
@@TOOL@@
NAME: article.get_claims
VERSION: 1
PURPOSE: Read the synthetic claim.
ARG claim_ids: C1
@@END@@
@@TOOL@@
NAME: article.get_claims
PURPOSE: Read claims.
ARG claim_ids: [not valid JSON
@@END@@"""
    corrected_output = (
        "@@DECISION@@\nGOAL: Finish after correcting the tool block.\n"
        "SUMMARY: The complete response has one valid tool call.\nNEXT: finish\n@@END@@\n"
        + valid_tool
    )
    transport = ScriptedTurnTransport([invalid_output, corrected_output])
    bridge = BridgeInvestigator(transport)

    result = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert result.decision is not None
    assert len(result.decision.proposed_tool_calls) == 1
    assert result.decision.proposed_tool_calls[0].arguments == {"claim_ids": "C1"}
    assert transport.submission_count == 2
    assert "TOOL article.get_claims" in transport.prompts[1]
    assert "line" in transport.prompts[1]


@pytest.mark.parametrize(
    ("invalid_block", "expected_label"),
    [
        (
            "@@FINDING@@\nKIND: capability\nASSESSMENT: qualified\n@@END@@",
            "FINDING",
        ),
        (
            "@@NOTE@@\nANCHOR: lead:0001\nSTATE: observed\nRELATION: confirms\n"
            "TEXT: A synthetic note.\nFINDINGS: not-a-uuid\n@@END@@",
            "NOTE",
        ),
    ],
)
async def test_rejected_finding_or_note_gets_corrected_before_tool_call_is_returned(
    invalid_block, expected_label
) -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    valid_tool = """@@TOOL@@
NAME: article.get_claims
VERSION: 1
PURPOSE: Read the synthetic claim.
ARG claim_ids: C1
@@END@@"""
    first = (
        "@@DECISION@@\nGOAL: Validate all output blocks.\nSUMMARY: Repair the rejected block.\n"
        "NEXT: finish\n@@END@@\n" + valid_tool + "\n" + invalid_block
    )
    corrected = (
        "@@DECISION@@\nGOAL: Return a complete decision.\nSUMMARY: Keep the valid tool call once.\n"
        "NEXT: finish\n@@END@@\n" + valid_tool
    )
    transport = ScriptedTurnTransport([first, corrected])

    result = await BridgeInvestigator(transport).turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert result.decision is not None
    assert len(result.decision.proposed_tool_calls) == 1
    assert transport.submission_count == 2
    assert expected_label in transport.prompts[1]


class _PersistedConversationService:
    def __init__(self, *, status, output_text=None):
        self.conversation_id = uuid4()
        self.contents = []
        self.status = status
        self.output_text = output_text
        self.add_turn_count = 0
        self.prompts = []

    async def get_or_create(self, conversation_id, **kwargs):
        return SimpleNamespace(
            id=self.conversation_id,
            subject_id=kwargs["subject_id"],
            purpose=kwargs["purpose"],
        )

    async def turns(self, conversation_id, *, context_subject_id):
        return tuple(self.contents)

    async def add_turn(self, conversation_id, *, idempotency_key, **kwargs):
        self.add_turn_count += 1
        self.prompts.append(kwargs["message"])
        turn = SimpleNamespace(
            id=uuid4(),
            idempotency_key=idempotency_key,
            status=self.status,
            error_message="uncertain submission" if self.output_text is None else None,
        )
        self.contents.append(SimpleNamespace(turn=turn, output_text=self.output_text))
        return turn


@pytest.mark.asyncio
async def test_model_transport_does_not_call_add_turn_for_ambiguous_persisted_retry() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    conversations = _PersistedConversationService(status=ConversationTurnStatus.NEEDS_REVIEW)
    transport = ModelConversationTurnTransport(conversations)
    args = {
        "investigation_id": investigation_id,
        "subject_id": context.source.subject_id,
        "turn_index": 0,
        "context_sha256": hashlib.sha256(context.canonical_bytes()).hexdigest(),
        "prompt": "canonical prompt",
        "external_llm_allowed": True,
    }

    first = await transport.submit(**args)
    second = await transport.submit(**args)

    assert first.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert second.status is TurnSubmissionStatus.NEEDS_REVIEW
    assert conversations.add_turn_count == 1


@pytest.mark.asyncio
async def test_model_transport_reuses_successful_persisted_turn_without_add_turn() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    output = '{"schema":"agent-turn-decision.v1"}'
    conversations = _PersistedConversationService(
        status=ConversationTurnStatus.SUCCEEDED,
        output_text=output,
    )
    transport = ModelConversationTurnTransport(conversations)
    args = {
        "investigation_id": investigation_id,
        "subject_id": context.source.subject_id,
        "turn_index": 0,
        "context_sha256": hashlib.sha256(context.canonical_bytes()).hexdigest(),
        "prompt": "canonical prompt",
        "external_llm_allowed": True,
    }

    first = await transport.submit(**args)
    second = await transport.submit(**args)

    assert first.status is TurnSubmissionStatus.SUCCEEDED
    assert second.status is TurnSubmissionStatus.SUCCEEDED
    assert second.output_text == output
    assert conversations.add_turn_count == 1


@pytest.mark.asyncio
async def test_adopted_agent_decision_is_consumed_on_resume_without_resubmission() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    valid = {
        "reasoning_summary": "The existing evidence answers the question.",
        "goal": "Finish the investigation.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": AgentNextAction.FINISH.value,
    }
    output = json.dumps(valid)
    key = make_investigation_turn_idempotency_key(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=hashlib.sha256(context.canonical_bytes()).hexdigest(),
    )
    conversations = _PersistedConversationService(
        status=ConversationTurnStatus.SUCCEEDED,
        output_text=output,
    )
    conversations.contents.append(
        SimpleNamespace(
            turn=SimpleNamespace(
                id=uuid4(), idempotency_key=key, status=ConversationTurnStatus.SUCCEEDED
            ),
            output_text=output,
        )
    )
    bridge = BridgeInvestigator(ModelConversationTurnTransport(conversations))

    resumed = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
    )

    assert resumed.status is TurnSubmissionStatus.SUCCEEDED
    assert resumed.decision is not None
    assert resumed.decision.next_action is AgentNextAction.FINISH
    assert conversations.add_turn_count == 0


@pytest.mark.asyncio
async def test_adopted_invalid_output_uses_bounded_schema_correction_on_resume() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    valid = {
        "reasoning_summary": "The corrected decision is valid.",
        "goal": "Finish the investigation.",
        "hypothesis_refs": [],
        "proposed_tool_calls": [],
        "finding_proposals": [],
        "opinion_delta": None,
        "next_action": AgentNextAction.FINISH.value,
    }
    invalid = "not an AgentTurnDecisionV1"
    key = make_investigation_turn_idempotency_key(
        investigation_id=investigation_id,
        turn_index=0,
        context_sha256=hashlib.sha256(context.canonical_bytes()).hexdigest(),
    )
    conversations = _PersistedConversationService(
        status=ConversationTurnStatus.SUCCEEDED,
        output_text=invalid,
    )
    conversations.contents.append(
        SimpleNamespace(
            turn=SimpleNamespace(
                id=uuid4(), idempotency_key=key, status=ConversationTurnStatus.SUCCEEDED
            ),
            output_text=invalid,
        )
    )
    conversations.output_text = json.dumps(valid)
    rejections = []
    bridge = BridgeInvestigator(ModelConversationTurnTransport(conversations))

    resumed = await bridge.turn(
        investigation_id=investigation_id,
        subject_id=context.source.subject_id,
        turn_index=0,
        context=context,
        external_llm_allowed=True,
        on_invalid_output=lambda key, detail: _record_rejection(rejections, key, detail),
    )

    assert resumed.status is TurnSubmissionStatus.SUCCEEDED
    assert resumed.decision is not None
    assert conversations.add_turn_count == 1
    assert len(rejections) == 1
    assert "schema_rejection" in conversations.prompts[0]


async def _record_rejection(rejections, key: str, detail: str) -> None:
    rejections.append((key, detail))
