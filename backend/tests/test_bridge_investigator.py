import hashlib
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
    assert "AgentTurnDecisionV1" in transport.prompts[0]
    assert "BEGIN_CANONICAL_INVESTIGATION_CONTEXT" in transport.prompts[0]


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
async def test_invalid_json_gets_a_corrective_turn_with_a_new_identity() -> None:
    investigation_id = uuid4()
    context = _context(investigation_id)
    valid = {
        "reasoning_summary": "Continue after the corrected response.",
        "goal": "Finish the investigation.",
        "next_action": "finish",
    }
    transport = ScriptedTurnTransport(["not JSON", valid])
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
    assert "valid JSON" in transport.prompts[1]


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


class _PersistedConversationService:
    def __init__(self, *, status, output_text=None):
        self.conversation_id = uuid4()
        self.contents = []
        self.status = status
        self.output_text = output_text
        self.add_turn_count = 0

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
