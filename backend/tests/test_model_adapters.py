from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import BaseModel

from cti_app.application.model_gateway import (
    AdapterResultStatus,
    ModelCapabilities,
    ModelCapabilityError,
    ModelGatewayError,
    ModelRoutingHint,
    SafeModelRequest,
    StructuredOutputError,
)
from cti_app.domain.model_runs import ModelBackend, ModelProvider, ModelRole, ModelTransport
from cti_app.integrations.models import (
    BridgeTransportError,
    ChatCompletionsTransportError,
    ChatGPTBridgeClient,
    FakeModelAdapter,
    HttpChatCompletionsTransport,
    OpenAICompatibleChatAdapter,
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
            "model": "GPT-5 Thinking",
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
            "model": "GPT-5 Thinking",
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


async def test_chatgpt_bridge_client_uses_standard_responses_endpoints() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "resp_1", "status": "queued"})
        return httpx.Response(
            200, json={"id": "resp_1", "status": "completed", "output_text": "ok"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
        await transport.create({"model": "chatgpt-web"}, idempotency_key="run:a1")
        await transport.retrieve("resp_1")

    assert [request.url.path for request in requests] == ["/v1/responses", "/v1/responses/resp_1"]
    assert requests[0].headers["X-Idempotency-Key"] == "run:a1"


async def test_failed_bridge_response_preserves_typed_diagnostics_without_resubmission() -> None:
    transport = FakeResponsesTransport(
        {
            "id": "resp_failed",
            "status": "failed",
            "error": {
                "code": "bridge_ui_timeout",
                "message": "La génération a expiré.",
                "retryable": True,
                "phase": "submission_confirmation",
                "submission_state": "post_submission",
                "conversation_id": "conv_123",
                "details": {"attempt": 2, "composer_text": "secret à exclure"},
            },
        }
    )
    adapter = OpenAIResearchAdapter(transport, model="chatgpt-web")

    with pytest.raises(BridgeTransportError) as caught:
        await adapter.invoke(safe_request(), role=ModelRole.RESEARCH)

    error = caught.value
    assert error.bridge_run_id == "resp_failed"
    assert error.bridge_status == "failed"
    assert error.code == "bridge_ui_timeout"
    assert error.retryable is True
    assert error.phase == "submission_confirmation"
    assert error.submission_state == "post_submission"
    assert error.conversation_id == "conv_123"
    assert error.diagnostics == {"attempt": 2}
    assert len(transport.created_payloads) == 1


async def test_gemini_webai_uses_generic_textual_chat_completions_contract() -> None:
    transport = FakeChatTransport(
        {
            "id": "gemini-1",
            "model": "gemini-3-flash-actual",
            "choices": [{"message": {"content": "Brouillon Gemini"}}],
        }
    )
    adapter = OpenAICompatibleChatAdapter(
        transport,
        provider=ModelProvider.GEMINI,
        backend=ModelBackend.GEMINI_WEBAI,
        model="gemini-3-flash",
        is_external=True,
    )

    result = await adapter.invoke(safe_request(), role=ModelRole.DRAFTING)

    assert result.provider is ModelProvider.GEMINI
    assert adapter.backend is ModelBackend.GEMINI_WEBAI
    assert adapter.transport is ModelTransport.OPENAI_CHAT_COMPLETIONS
    assert adapter.capabilities.structured_output is False
    assert result.output_text == "Brouillon Gemini"
    assert result.actual_model_version == "gemini-3-flash-actual"
    payload = transport.payloads[0]
    assert payload["model"] == "gemini-3-flash"
    assert not {"response_format", "text", "json_schema"} & payload.keys()


@pytest.mark.parametrize(
    "capabilities",
    [None, ModelCapabilities(structured_output=True)],
    ids=["default", "misdeclared"],
)
async def test_gemini_webai_refuses_structured_output_before_transport(
    capabilities: ModelCapabilities | None,
) -> None:
    transport = FakeChatTransport({})
    adapter = OpenAICompatibleChatAdapter(
        transport,
        provider=ModelProvider.GEMINI,
        backend=ModelBackend.GEMINI_WEBAI,
        model="gemini-3-flash",
        is_external=True,
        capabilities=capabilities,
    )

    with pytest.raises(ModelCapabilityError) as caught:
        await adapter.invoke(
            safe_request(), role=ModelRole.STRUCTURED_EXTRACTION, output_schema=Extraction
        )

    assert caught.value.backend is ModelBackend.GEMINI_WEBAI
    assert caught.value.capability == "structured_output"
    assert transport.payloads == []


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
    payload = transport.created_payloads[0]
    assert "text" not in payload
    assert "response_format" not in payload
    assert '"title"' in payload["input"][0]["content"]


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


async def test_qwen_sends_its_json_contract_and_defers_discovery_validation() -> None:
    transport = FakeChatTransport(
        {
            "id": "qwen_contract",
            "model": "Qwen3-32B",
            "choices": [{"message": {"content": '{"title":"Iran","score":2}'}}],
        }
    )
    adapter = QwenAdapter(transport, model="Qwen3-32B", is_external=False)
    request = replace(safe_request(), metadata={"defer_validation": True})

    result = await adapter.invoke(
        request, role=ModelRole.STRUCTURED_EXTRACTION, output_schema=Extraction
    )

    system = transport.payloads[0]["messages"][0]["content"]
    assert '"title"' in system
    assert '"score"' in system
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


async def test_bridge_capabilities_and_archive_use_separate_timeouts() -> None:
    class _TimeoutClient:
        def __init__(self) -> None:
            self.timeouts: list[httpx.Timeout] = []

        async def request(
            self,
            method: str,
            url: str,
            **kwargs: Any,
        ) -> httpx.Response:
            self.timeouts.append(kwargs["timeout"])
            if url.endswith("/bridge/capabilities"):
                return httpx.Response(200, json={"transport": "chatgpt_web_ui"})
            return httpx.Response(200, json={"archived": True})

    client = _TimeoutClient()
    transport = ChatGPTBridgeClient(
        "http://bridge.test/v1",
        capabilities_timeout_seconds=2,
        archive_timeout_seconds=60,
        client=client,  # type: ignore[arg-type]
    )

    await transport.capabilities()
    await transport.archive_conversation(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))

    assert len(client.timeouts) == 2
    assert client.timeouts[0].read == 2
    assert client.timeouts[1].read == 60


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
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
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
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
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
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
        await transport.archive_conversation(conversation_id)


async def test_bridge_archive_read_timeout_is_not_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("read timeout", request=request)

    conversation_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
        with pytest.raises(BridgeTransportError) as caught:
            await transport.archive_conversation(conversation_id)

    assert caught.value.code == "bridge_timeout"
    assert caught.value.attempts == 1
    assert calls == 1


async def test_bridge_connect_error_is_typed_and_post_without_key_is_not_retried() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create({"input": "secret"})

    assert caught.value.code == "bridge_unreachable"
    assert caught.value.retryable is True
    assert calls == 1


async def test_bridge_unobserved_ui_model_is_not_recorded_as_a_model_version() -> None:
    def completed(model: str) -> FakeResponsesTransport:
        return FakeResponsesTransport(
            {"id": "resp_model", "status": "completed", "model": model, "output_text": "ok"}
        )

    observed = await OpenAIResearchAdapter(completed("GPT-5 Thinking"), model="chatgpt-web").invoke(
        safe_request(), role=ModelRole.RESEARCH
    )
    unobserved = await OpenAIResearchAdapter(completed("chatgpt-web"), model="chatgpt-web").invoke(
        safe_request(), role=ModelRole.RESEARCH
    )

    assert observed.actual_model_version == "GPT-5 Thinking"
    assert unobserved.actual_model_version is None


async def test_bridge_fastapi_detail_keeps_submission_state_without_replay() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            json={
                "detail": {
                    "code": "bridge_extension_disconnected",
                    "message": "Extension Chrome non connectée : ouvre un onglet chatgpt.com.",
                    "retryable": True,
                    "phase": "pre_submission",
                    "submission_state": "pre_submission",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = ChatGPTBridgeClient("http://bridge.test/v1", client=client)
        with pytest.raises(BridgeTransportError) as caught:
            await transport.create({"input": "secret"}, idempotency_key="run:a1")

    assert caught.value.code == "bridge_extension_disconnected"
    assert caught.value.retryable is True
    assert caught.value.phase == "pre_submission"
    assert caught.value.submission_state == "pre_submission"
    assert caught.value.status_code == 503
    assert calls == 1


def test_bridge_detail_submission_state_accepts_only_known_values() -> None:
    def parsed(value: str) -> str | None:
        response = httpx.Response(
            503,
            request=httpx.Request("POST", "https://bridge.test/v1/responses"),
            json={"detail": {"code": "bridge_extension_disconnected", "submission_state": value}},
        )
        return _bridge_http_error(response, attempts=1).submission_state

    assert parsed("pre_submission") == "pre_submission"
    assert parsed("submission_attempted") == "submission_attempted"
    assert parsed("post_submission") == "post_submission"
    assert parsed("probably_not_sent") is None


def _chat_payload(prompt: str = "Texte autorisé") -> dict[str, Any]:
    return {"model": "gemini-3-flash", "messages": [{"role": "user", "content": prompt}]}


async def _webai_error(
    respond: Callable[[httpx.Request], httpx.Response],
    payload: dict[str, Any] | None = None,
) -> tuple[ChatCompletionsTransportError, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpChatCompletionsTransport(
            "http://web_ai.test/v1", api_key="webai-secret-key", provider="gemini", client=client
        )
        with pytest.raises(ChatCompletionsTransportError) as caught:
            await transport.create(payload or _chat_payload())
    return caught.value, requests


@pytest.mark.parametrize(
    ("status", "code", "retryable", "submission_state"),
    [
        (400, "provider_bad_request", False, None),
        (401, "provider_auth_failed", False, "pre_submission"),
        (403, "provider_auth_failed", False, "pre_submission"),
        (404, "provider_not_found", False, "pre_submission"),
        (422, "provider_validation_failed", False, None),
        (429, "provider_rate_limited", True, None),
        (502, "provider_server_error", True, None),
        (503, "provider_server_error", True, None),
        (504, "provider_timeout", True, None),
    ],
)
async def test_webai_http_errors_keep_a_typed_bounded_contract(
    status: int, code: str, retryable: bool, submission_state: str | None
) -> None:
    error, requests = await _webai_error(
        lambda _: httpx.Response(status, json={"detail": "Gemini WebAPI provider request failed."})
    )

    assert error.code == code
    assert error.code != "model_transport_unavailable"
    assert error.provider == "gemini"
    assert error.status_code == status
    assert error.retryable is retryable
    assert error.submission_state == submission_state
    assert error.diagnostics["http_status"] == status
    assert error.diagnostics["provider_message"] == "Gemini WebAPI provider request failed."
    assert len(requests) == 1


async def test_webai_only_an_unopened_connection_is_a_transport_pre_submission() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    refused, _ = await _webai_error(refuse)
    stalled, _ = await _webai_error(stall)
    garbled, _ = await _webai_error(lambda _: httpx.Response(200, content=b"<html>"))

    assert (refused.code, refused.retryable, refused.submission_state) == (
        "provider_unreachable",
        True,
        "pre_submission",
    )
    assert (stalled.code, stalled.retryable, stalled.submission_state) == (
        "provider_timeout",
        True,
        None,
    )
    assert (garbled.code, garbled.retryable, garbled.submission_state) == (
        "provider_protocol_error",
        False,
        "post_submission",
    )


async def test_webai_explicit_submission_contract_is_honoured_only_when_valid() -> None:
    def contract(state: str) -> Callable[[httpx.Request], httpx.Response]:
        return lambda _: httpx.Response(
            503,
            json={"detail": {"code": "gemini_not_ready", "submission_state": state}},
        )

    proven, _ = await _webai_error(contract("pre_submission"))
    invented, _ = await _webai_error(contract("surely_not_sent"))

    assert proven.submission_state == "pre_submission"
    assert proven.diagnostics["provider_code"] == "gemini_not_ready"
    assert invented.submission_state is None


async def test_webai_error_never_keeps_prompt_echoes_or_secrets() -> None:
    prompt = "Rapport confidentiel sur l'acteur APT-Example et ses infrastructures"
    echoed, requests = await _webai_error(
        lambda _: httpx.Response(
            400,
            json={
                "error": {
                    "message": f"Invalid content: {prompt}",
                    "code": "invalid_value",
                    "type": "invalid_request_error",
                    "param": "messages[1].content",
                }
            },
        ),
        _chat_payload(prompt),
    )
    leaked, _ = await _webai_error(
        lambda _: httpx.Response(
            401,
            json={"detail": "Rejected Authorization: Bearer sk-live-abcdef123456 cookie=1PSID"},
        )
    )
    validation, _ = await _webai_error(
        lambda _: httpx.Response(
            422, json={"detail": [{"loc": ["body", "messages"], "msg": "bad", "input": prompt}]}
        ),
        _chat_payload(prompt),
    )

    assert "provider_message" not in echoed.diagnostics
    assert echoed.diagnostics["provider_code"] == "invalid_value"
    assert echoed.diagnostics["provider_param"] == "messages[1].content"
    assert "provider_message" not in validation.diagnostics
    for error in (echoed, leaked, validation):
        rendered = f"{error} {error.diagnostics}"
        assert "APT-Example" not in rendered
        assert "sk-live" not in rendered
        assert "1PSID" not in rendered
        assert "webai-secret-key" not in rendered
    assert requests[0].headers["Authorization"] == "Bearer webai-secret-key"
