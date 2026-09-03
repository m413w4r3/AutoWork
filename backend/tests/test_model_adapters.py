from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel

from cti_app.application.model_gateway import (
    AdapterResultStatus,
    ModelGatewayError,
    ModelRoutingHint,
    SafeModelRequest,
    StructuredOutputError,
)
from cti_app.domain.model_runs import ModelProvider, ModelRole
from cti_app.integrations.models import (
    BridgeTransportError,
    ChatGPTBridgeTransport,
    FakeModelAdapter,
    OpenAIResearchAdapter,
    OpenAIStructuredAdapter,
    QwenAdapter,
    _bridge_http_error,
)


class Extraction(BaseModel):
    title: str
    score: int


class FakeResponsesTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.created_payloads: list[dict[str, Any]] = []
        self.retrieved: list[str] = []

    async def create(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        del idempotency_key
        self.created_payloads.append(payload)
        return self.response

    async def retrieve(self, response_id: str) -> dict[str, Any]:
        self.retrieved.append(response_id)
        return self.response


class FakeChatTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.payloads: list[dict[str, Any]] = []

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return self.response


def safe_request(*, background: bool = False) -> SafeModelRequest:
    return SafeModelRequest(
        text="Texte autorisé",
        prompt_template_id="contract-test",
        prompt_template_version="1",
        evidence_pack_hash="a" * 64,
        routing_hint=ModelRoutingHint.BULK_EXTRACTION,
        sensitivity="internal",
        metadata={},
        parameters={},
        web_search=False,
        background=background,
        authorized_input_hash="b" * 64,
    )


AdapterFactory = Callable[[], tuple[object, ModelRole, type[BaseModel] | None]]


def research_case() -> tuple[object, ModelRole, type[BaseModel] | None]:
    transport = FakeResponsesTransport(
        {
            "id": "resp_research",
            "status": "completed",
            "model": "chatgpt-web",
            "output_text": "Résultat sourcé",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }
    )
    return OpenAIResearchAdapter(transport, model="chatgpt-web"), ModelRole.RESEARCH, None


def structured_case() -> tuple[object, ModelRole, type[BaseModel] | None]:
    transport = FakeResponsesTransport(
        {
            "id": "resp_structured",
            "status": "completed",
            "model": "chatgpt-web",
            "output_text": '{"title":"Iran","score":2}',
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        }
    )
    return (
        OpenAIStructuredAdapter(transport, model="chatgpt-web"),
        ModelRole.STRUCTURED_EXTRACTION,
        Extraction,
    )


def qwen_case() -> tuple[object, ModelRole, type[BaseModel] | None]:
    transport = FakeChatTransport(
        {
            "id": "qwen_1",
            "model": "Qwen3-32B-build-42",
            "choices": [{"message": {"content": '{"title":"Iran","score":2}'}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
    )
    return (
        QwenAdapter(transport, model="Qwen3-32B", is_external=False),
        ModelRole.STRUCTURED_EXTRACTION,
        Extraction,
    )


def fake_case() -> tuple[object, ModelRole, type[BaseModel] | None]:
    return FakeModelAdapter(), ModelRole.DRAFTING, None


@pytest.mark.parametrize("factory", [research_case, structured_case, qwen_case, fake_case])
async def test_all_adapters_obey_common_contract(factory: AdapterFactory) -> None:
    adapter, role, schema = factory()
    result = await adapter.invoke(safe_request(), role=role, output_schema=schema)  # type: ignore[attr-defined]

    assert result.status is AdapterResultStatus.COMPLETED
    assert result.requested_model
    assert result.actual_model_version
    assert result.usage.total_tokens >= result.usage.input_tokens + result.usage.output_tokens
    assert result.output_text is not None or result.structured_output is not None


async def test_openai_research_uses_responses_web_search_and_background() -> None:
    transport = FakeResponsesTransport(
        {
            "id": "resp_background",
            "status": "queued",
            "model": "chatgpt-web",
            "usage": None,
        }
    )
    adapter = OpenAIResearchAdapter(transport, model="chatgpt-web")

    unsafe_overrides = replace(
        replace(safe_request(background=True), web_search=True),
        parameters={
            "model": "policy-bypass",
            "tools": [],
            "reasoning": {"effort": "high"},
        },
    )
    result = await adapter.invoke(unsafe_overrides, role=ModelRole.RESEARCH)

    assert result.status is AdapterResultStatus.WAITING_BACKGROUND
    payload = transport.created_payloads[0]
    assert payload["model"] == "chatgpt-web"
    assert payload["tools"] == [{"type": "web_search"}]
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["background"] is True
    assert payload["include"] == ["web_search_call.action.sources"]
    assert payload["input"] == [{"role": "user", "content": "Texte autorisé"}]


async def test_openai_structured_rejects_invalid_output() -> None:
    transport = FakeResponsesTransport(
        {
            "id": "resp_invalid",
            "status": "completed",
            "model": "chatgpt-web",
            "output_text": '{"title":12}',
        }
    )
    adapter = OpenAIStructuredAdapter(transport, model="chatgpt-web")

    with pytest.raises(StructuredOutputError):
        await adapter.invoke(
            safe_request(),
            role=ModelRole.STRUCTURED_EXTRACTION,
            output_schema=Extraction,
        )
    output_format = transport.created_payloads[0]["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["additionalProperties"] is False
    assert output_format["schema"]["required"] == ["title", "score"]


async def test_openai_needs_review_preserves_bridge_reason() -> None:
    transport = FakeResponsesTransport(
        {
            "id": "resp_review",
            "status": "needs_review",
            "model": "chatgpt-web",
            "error": {
                "code": "active_signal_stalled",
                "message": "ChatGPT s'est arrêté sans réponse finale.",
            },
            "metadata": {"completion_signal": "streaming"},
        }
    )
    adapter = OpenAIResearchAdapter(transport, model="chatgpt-web")

    result = await adapter.invoke(safe_request(), role=ModelRole.RESEARCH)

    assert result.status is AdapterResultStatus.NEEDS_REVIEW
    assert result.output_text is None
    assert result.metadata["reason"] == "active_signal_stalled"


async def test_needs_review_keeps_the_visible_candidate_facts() -> None:
    """A stalled run whose answer is on screen must not be reported as empty."""
    transport = FakeResponsesTransport(
        {
            "id": "resp_65a707c50a5549a582b2fc3f",
            "status": "needs_review",
            "model": "chatgpt-web",
            "error": {"code": "active_signal_stalled", "message": "stalled"},
            "metadata": {
                "completion_signal": "streaming",
                "completion_confidence": "high",
                "output_chars": 4211,
                "candidate_output_present": True,
                "recovery_preview_available": True,
                "external_turn_id_verified": False,
                "candidate_output_sha256": "d" * 64,
                "streaming_signal_sources": [
                    {"source": ".result-streaming", "visible": True, "aria_hidden": None}
                ],
            },
        }
    )
    adapter = OpenAIResearchAdapter(transport, model="chatgpt-web")

    result = await adapter.invoke(safe_request(), role=ModelRole.RESEARCH)

    assert result.metadata["output_chars"] == 4211
    assert result.metadata["candidate_output_present"] is True
    assert result.metadata["recovery_preview_available"] is True
    assert result.metadata["external_turn_id_verified"] is False
    assert result.metadata["candidate_output_sha256"] == "d" * 64
    assert result.metadata["streaming_signal_sources"] == [
        {"source": ".result-streaming", "visible": True, "aria_hidden": None}
    ]


async def test_openai_completed_empty_output_is_a_contract_error() -> None:
    transport = FakeResponsesTransport(
        {
            "id": "resp_empty",
            "status": "completed",
            "model": "chatgpt-web",
            "output_text": "",
        }
    )
    adapter = OpenAIResearchAdapter(transport, model="chatgpt-web")

    with pytest.raises(ModelGatewayError, match="empty output text"):
        await adapter.invoke(safe_request(), role=ModelRole.RESEARCH)


async def test_qwen_protocol_is_confined_to_its_adapter() -> None:
    transport = FakeChatTransport(
        {
            "id": "qwen_2",
            "model": "Qwen3-32B-build-42",
            "choices": [{"message": {"content": "Brouillon local"}}],
        }
    )
    adapter = QwenAdapter(transport, model="Qwen3-32B", is_external=False)

    result = await adapter.invoke(safe_request(), role=ModelRole.DRAFTING)

    assert result.provider is ModelProvider.QWEN
    assert transport.payloads[0]["model"] == "Qwen3-32B"
    assert transport.payloads[0]["messages"][1]["content"] == "Texte autorisé"


async def test_qwen_uses_compact_contract_and_defers_discovery_validation() -> None:
    transport = FakeChatTransport(
        {
            "id": "qwen_compact",
            "model": "Qwen3-32B",
            "choices": [{"message": {"content": '{"title":"Iran","score":2}'}}],
        }
    )
    adapter = QwenAdapter(transport, model="Qwen3-32B", is_external=False)
    request = replace(
        safe_request(),
        metadata={
            "compact_contract": {
                "version": "compact-v1",
                "required": ["title", "score"],
            },
            "defer_validation": True,
        },
    )

    result = await adapter.invoke(
        request, role=ModelRole.STRUCTURED_EXTRACTION, output_schema=Extraction
    )

    system = transport.payloads[0]["messages"][0]["content"]
    assert "compact-v1" in system
    assert "$defs" not in system
    assert '"title":' not in system
    assert transport.payloads[0]["response_format"] == {"type": "json_object"}
    assert result.output_text == '{"title":"Iran","score":2}'
    assert result.structured_output is None


async def test_bridge_visible_citations_are_exposed_as_adapter_metadata() -> None:
    transport = FakeResponsesTransport(
        {
            "id": "resp_citations",
            "status": "completed",
            "model": "chatgpt-web",
            "output_text": "Texte propre",
            "metadata": {
                "serializer_version": "chatgpt-dom-v2",
                "completion_signal": "assistant_actions",
                "completion_confidence": "high",
                "stable_for_ms": 2100,
                "output_chars": 12,
                "visible_citation_count": 1,
                "content_script_version": "13",
                "visible_citations": [
                    {
                        "label": "Publisher",
                        "url": "https://publisher.example/report?utm_source=chatgpt",
                        "canonical_url": "https://publisher.example/report",
                        "position": None,
                    }
                ],
            },
        }
    )
    adapter = OpenAIResearchAdapter(transport, model="chatgpt-web")

    result = await adapter.invoke(safe_request(), role=ModelRole.RESEARCH)

    assert result.output_text == "Texte propre"
    assert result.metadata["serializer_version"] == "chatgpt-dom-v2"
    assert result.metadata["visible_citations"][0]["label"] == "Publisher"
    assert result.metadata["completion_signal"] == "assistant_actions"
    assert result.metadata["completion_confidence"] == "high"
    assert result.metadata["stable_for_ms"] == 2100
    assert result.metadata["output_chars"] == 12
    assert result.metadata["visible_citation_count"] == 1
    assert result.metadata["content_script_version"] == "13"


async def test_chatgpt_bridge_transport_uses_native_capabilities() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/capabilities"):
            return httpx.Response(200, json={"transport": "chatgpt_web_ui"})
        return httpx.Response(
            200,
            json={
                "id": "resp_bridge",
                "status": "queued",
                "model": "chatgpt-web",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeTransport("http://bridge.test/v1", client=client)
        response = await transport.create(
            {
                "model": "premium-profile",
                "input": [{"role": "user", "content": "Texte"}],
                "tools": [{"type": "web_search"}],
                "text": {"format": {"type": "json_schema", "schema": {"type": "object"}}},
                "background": True,
                "reasoning": {"effort": "high"},
            }
        )
        capabilities = await transport.capabilities()

    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/bridge/runs"
    assert body == {
        "requested_model": "premium-profile",
        "input": [{"role": "user", "content": "Texte"}],
        "web_search": True,
        "response_format": {"type": "json_schema", "schema": {"type": "object"}},
        "background": True,
        "reasoning_effort": "high",
    }
    assert response["status"] == "queued"
    assert capabilities == {"transport": "chatgpt_web_ui"}


@pytest.mark.parametrize(
    ("status", "code", "attempts"),
    [(401, "bridge_auth_failed", 1), (500, "bridge_server_error", 1)],
)
async def test_bridge_classifies_http_errors_and_never_retries_auth(
    status: int, code: str, attempts: int
) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": {"message": "unsafe upstream detail"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeTransport("http://bridge.test/v1", client=client)
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create({"input": "secret"}, idempotency_key="stable")

    assert caught.value.code == code
    assert caught.value.retryable is (status >= 500)
    assert calls == attempts
    assert "unsafe" not in str(caught.value)


async def test_bridge_http_error_preserves_submission_boundary_and_safe_diagnostics() -> None:
    request = httpx.Request("POST", "https://bridge.test/v1/responses")
    response = httpx.Response(
        502,
        request=request,
        json={
            "detail": {
                "error": {
                    "code": "bridge_ui_timeout",
                    "message": "safe message",
                    "retryable": True,
                    "phase": "submission_confirmation",
                    "submission_state": "submission_attempted",
                    "details": {
                        "user_turns_before": 1,
                        "composer_text": "must not persist",
                    },
                }
            }
        },
    )

    error = _bridge_http_error(response, attempts=1)

    assert error.code == "bridge_ui_timeout"
    assert error.retryable is True
    assert error.phase == "submission_confirmation"
    assert error.submission_state == "submission_attempted"
    assert error.diagnostics == {"user_turns_before": 1}


async def test_bridge_archive_requires_archived_true_on_http_2xx() -> None:
    conversation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "archived": False,
                "conversation_id": conversation_id,
                "code": "conversation_window_close_failed",
                "message": "fenêtre exacte encore ouverte",
                "retryable": True,
                "phase": "conversation_archive",
                "details": {"tab_id": 3, "window_id": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeTransport("http://bridge.test/v1", client=client)
        with pytest.raises(BridgeTransportError) as caught:
            await transport.archive_conversation(UUID(conversation_id))

    assert caught.value.code == "conversation_window_close_failed"
    assert caught.value.retryable is True
    assert caught.value.phase == "conversation_archive"
    assert caught.value.conversation_id == conversation_id
    assert caught.value.diagnostics == {
        "conversation_id": conversation_id,
        "tab_id": 3,
        "window_id": 4,
    }
    assert len(requests) == 1


async def test_bridge_archive_accepts_only_explicit_archived_true() -> None:
    conversation_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "archived": True,
                "conversation_id": str(conversation_id),
                "close_state": "closed",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeTransport("http://bridge.test/v1", client=client)
        await transport.archive_conversation(conversation_id)


async def test_bridge_connect_error_is_typed_and_post_without_key_is_not_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeTransport("http://bridge.test/v1", client=client)
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create({"input": "secret"})

    assert caught.value.code == "bridge_unreachable"
    assert caught.value.retryable is True
    assert calls == 1


async def test_bridge_429_honours_retry_after_and_reuses_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {}})
        return httpx.Response(200, json={"id": "resp_1", "status": "completed"})

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("cti_app.integrations.models.asyncio.sleep", fake_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeTransport("http://bridge.test/v1", client=client)
        await transport.create(
            {"input": "secret"}, idempotency_key="00000000-0000-4000-8000-000000000001:a1"
        )

    assert delays == [2]
    assert [request.headers["X-Idempotency-Key"] for request in requests] == [
        "00000000-0000-4000-8000-000000000001:a1",
        "00000000-0000-4000-8000-000000000001:a1",
    ]
    assert [json.loads(request.content)["request_id"] for request in requests] == [
        "00000000-0000-4000-8000-000000000001:a1",
        "00000000-0000-4000-8000-000000000001:a1",
    ]
    assert all(":a2" not in request.headers["X-Idempotency-Key"] for request in requests)
