from dataclasses import replace
from uuid import uuid4

import pytest

from cti_app.application.model_gateway import (
    ConversationContext,
    ConversationLifecycleSpec,
    ModelRequest,
    ModelRoutingHint,
    sanitize_model_request,
)
from cti_app.domain.model_conversations import (
    ConversationMode,
    ConversationPolicy,
    ConversationPurpose,
    ConversationStatus,
    ConversationTransport,
    ModelConversation,
)
from cti_app.domain.model_runs import ModelProvider


def _request(context: ConversationContext) -> ModelRequest:
    return ModelRequest(
        text="Quel pivot faut-il vérifier ?",
        prompt_template_id="analyst-conversation",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
        conversation=context,
    )


def test_authorized_hash_binds_conversation_parent_head_message_and_prompt() -> None:
    conversation_id = uuid4()
    parent_id = uuid4()
    first = ConversationContext(
        mode="continue",
        id=conversation_id,
        external_locator="https://chatgpt.com/opaque/a",
        parent_turn_id=parent_id,
        previous_head_hash="b" * 64,
    )
    other_head = ConversationContext(
        mode="continue",
        id=conversation_id,
        external_locator="https://chatgpt.com/opaque/a",
        parent_turn_id=parent_id,
        previous_head_hash="c" * 64,
    )

    assert (
        sanitize_model_request(_request(first)).authorized_input_hash
        != sanitize_model_request(_request(other_head)).authorized_input_hash
    )
    assert sanitize_model_request(_request(first)).conversation == first


def test_authorized_hash_binds_the_explicit_web_search_choice() -> None:
    context = ConversationContext(
        mode="continue",
        id=uuid4(),
        external_locator="https://chatgpt.com/opaque/a",
    )
    request = _request(context)

    assert sanitize_model_request(request).web_search is False
    assert (
        sanitize_model_request(request).authorized_input_hash
        != sanitize_model_request(replace(request, web_search=True)).authorized_input_hash
    )


def test_only_analyst_assistance_and_pivot_research_can_continue() -> None:
    analyst = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.ANALYST_ASSISTANCE,
        title="Analyse",
        external_locator="https://chatgpt.com/opaque/a",
        head_turn_id=uuid4(),
        status=ConversationStatus.READY,
    )
    analyst.start_turn(mode=ConversationMode.CONTINUE)
    assert analyst.status is ConversationStatus.BUSY

    discovery = ModelConversation(
        provider=ModelProvider.OPENAI,
        transport=ConversationTransport.CHATGPT_BRIDGE,
        purpose=ConversationPurpose.DISCOVERY,
        title="Découverte",
        external_locator="https://chatgpt.com/opaque/d",
        head_turn_id=uuid4(),
        status=ConversationStatus.READY,
    )
    with pytest.raises(ValueError, match="requires fresh"):
        discovery.start_turn(mode=ConversationMode.CONTINUE)


def test_model_request_lifecycle_contract() -> None:
    """Fresh conversations without explicit lifecycle policy raise ValueError."""
    conversation_id = uuid4()

    request = ModelRequest(
        text="Extract data",
        prompt_template_id="test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
    )
    assert request.conversation is None
    assert request.conversation_lifecycle is None

    fresh_context = ConversationContext(mode="fresh", id=conversation_id)
    with pytest.raises(ValueError, match="A fresh conversation requires an explicit"):
        ModelRequest(
            text="Analyze data",
            prompt_template_id="test",
            prompt_template_version="1",
            evidence_pack_hash="a" * 64,
            external_llm_allowed=True,
            routing_hint=ModelRoutingHint.WEB_RESEARCH,
            conversation=fresh_context,
        )

    fresh_with_lifecycle = ModelRequest(
        text="Analyze data",
        prompt_template_id="test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
        conversation=fresh_context,
        conversation_lifecycle=ConversationLifecycleSpec(policy=ConversationPolicy.KEEP),
    )
    assert fresh_with_lifecycle.conversation == fresh_context
    assert fresh_with_lifecycle.conversation_lifecycle is not None

    continue_context = ConversationContext(
        mode="continue",
        id=conversation_id,
        external_locator="https://chatgpt.com/opaque/a",
    )
    continue_request = ModelRequest(
        text="Continue analysis",
        prompt_template_id="test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.WEB_RESEARCH,
        conversation=continue_context,
    )
    assert continue_request.conversation == continue_context
    assert continue_request.conversation_lifecycle is None
