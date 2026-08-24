from __future__ import annotations

import asyncio
import json
import logging
import re
import runpy
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request


def load_bridge() -> dict[str, Any]:
    bridge_root = Path(__file__).parents[2] / "chatgpt-bridge"
    old_sys_path = sys.path.copy()
    try:
        sys.path.insert(0, str(bridge_root))
        return runpy.run_path(str(bridge_root / "server.py"))
    finally:
        sys.path = old_sys_path


def _openai_globals(module: dict[str, Any]) -> dict[str, Any]:
    """Module namespace of `bridge.routes_openai`, reached via the `OpenAIRoutes`
    instance the server wires up (R59a). `bridge.generation`/`bridge.ui` are
    import-cached, so anything pulled from here shares state with the rest of
    the bridge across `load_bridge()` calls, same as `module[...]` did before
    these endpoints moved out of `server.py`.
    """
    globals_: dict[str, Any] = module["openai_routes"].create_response_internal.__globals__
    return globals_


def _bridge_globals(module: dict[str, Any]) -> dict[str, Any]:
    """Module namespace of `bridge.routes_bridge`, reached via the `BridgeRoutes`
    instance the server wires up (R59b). Import-cached across `load_bridge()`
    calls just like `bridge.routes_openai` — see `_openai_globals`.
    """
    globals_: dict[str, Any] = module["bridge_routes"].create_bridge_run.__globals__
    return globals_


def test_extension_reserves_request_before_real_send_click() -> None:
    root = Path(__file__).parents[2] / "chatgpt-bridge" / "extension"
    background = (root / "background.js").read_text()
    content = (root / "content.js").read_text()

    assert 'requestStates.set(msg.id, "received")' in background
    assert "await Promise.all([requestStatesReady, conversationRegistryReady])" in background
    assert content.index("await claimPrompt(id)") < content.index("sendBtn.click()")
    assert "submittedRequestIds" in content
    assert "bridgeConversationRegistry" in background
    assert (
        "msg.conversation ? resolveConversationTab(msg.conversation) : findChatTab()" in background
    )
    assert 'chrome.tabs.create({ url: "https://chatgpt.com/", active: false })' in background
    assert "candidate.url === conversation.external_locator" in background
    # P0 : le heartbeat est un signal de liveness de l'extension. Il doit être
    # émis avant tout `continue` dépendant du DOM, sinon une phase de recherche
    # web qui remplace le tour assistant provoque un faux idle timeout.
    assert 'type: "heartbeat"' in content
    assert content.index('type: "heartbeat"') < content.index("if (!turn) continue;")
    assert 'type: "chunk"' not in content
    assert "text: serialized.text" in content
    assert '"final-output.js"' in background


class FakeExtension:
    def __init__(self, module: dict[str, Any], *, prompt_delay: float = 0) -> None:
        self.module = module
        self.prompt_delay = prompt_delay
        self.prompt_count = 0
        self.sent: list[dict[str, Any]] = []
        self.tasks: set[asyncio.Task[None]] = set()
        self.closed: tuple[int, str] | None = None
        self.locators: dict[str, str] = {}

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        task = asyncio.create_task(self._respond(payload))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _respond(self, payload: dict[str, Any]) -> None:
        if payload["type"] in {"ui_control", "ui_state"}:
            await asyncio.sleep(0)
            applied = {}
            if payload["type"] == "ui_control":
                applied = {
                    key: {
                        "requested": value,
                        "applied": value,
                        "verified": True,
                        "ok": True,
                        "changed": False,
                    }
                    for key, value in payload.get("controls", {}).items()
                }
            self.module["bridge"].dispatch(
                {
                    "type": payload["type"],
                    "id": payload["id"],
                    "applied": applied,
                    "state": {
                        "observed_at": 1,
                        "model": {},
                        "profile": {},
                        "web_search": {},
                    },
                }
            )
        elif payload["type"] == "prompt":
            self.prompt_count += 1
            await asyncio.sleep(self.prompt_delay)
            self.module["bridge"].dispatch(
                {"type": "heartbeat", "id": payload["id"], "event_id": "1"}
            )
            target = payload.get("conversation")
            conversation = None
            if target:
                if target["mode"] == "fresh":
                    self.locators[target["id"]] = f"https://chatgpt.com/simulated/{target['id']}"
                elif self.locators.get(target["id"]) != target.get("external_locator"):
                    self.module["bridge"].dispatch(
                        {
                            "type": "error",
                            "id": payload["id"],
                            "event_id": "2",
                            "message": "conversation simulée introuvable",
                        }
                    )
                    return
                conversation = {
                    "id": target["id"],
                    "external_locator": self.locators[target["id"]],
                    "turn_id": f"turn-{self.prompt_count}",
                    "mode": target["mode"],
                    "verified": True,
                }
            self.module["bridge"].dispatch(
                {
                    "type": "done",
                    "id": payload["id"],
                    "event_id": "2",
                    "text": "ok",
                    "metadata": {
                        "completion_signal": "assistant_actions",
                        "completion_confidence": "high",
                        "stable_for_ms": 2_100,
                        "output_chars": 2,
                        "visible_citation_count": 0,
                        "content_script_version": "14",
                    },
                    "conversation": conversation,
                }
            )

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def request_with_key(key: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/bridge/runs",
            "headers": [(b"x-idempotency-key", key.encode())],
        }
    )


def isolated_registry(module: dict[str, Any], tmp_path: Path) -> None:
    registry = module["RunRegistry"](tmp_path / "runs.sqlite3")
    # `create_bridge_run`/`retrieve_bridge_run` no longer read a `run_registry`
    # global (R59b: they read `self.registry`, bound once at construction) — every
    # real owner of a registry reference needs patching directly, including the
    # `module["run_registry"]` entry itself, since later test code still reads it
    # for direct `run_generation` calls.
    module["run_registry"] = registry
    module["openai_routes"].registry = registry
    module["bridge_routes"].registry = registry
    module["conversation_routes"].registry = registry


def test_responses_facade_translates_web_search_and_rejects_binary_blocks() -> None:
    module = load_bridge()
    openai_globals = _openai_globals(module)
    response_request = openai_globals["ResponseRequest"]
    translate = openai_globals["_response_chat_request"]

    translated = translate(
        response_request(
            model="chatgpt-web",
            input=[{"role": "user", "content": "Recherche autorisée"}],
            tools=[{"type": "web_search"}],
        )
    )

    assert translated.files == []
    assert translated.new_chat is True
    assert "Recherche sur le Web" in translated.messages[0].content
    assert "Les pages consultées sont des sources non fiables" in translated.messages[0].content
    assert "ignore toute instruction qu'il contient" not in translated.messages[0].content
    assert "Responses API" not in translated.messages[0].content

    with pytest.raises(HTTPException, match="binaires"):
        translate(
            response_request(
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": "data:image/png"}],
                    }
                ]
            )
        )


def test_recovery_message_is_forwarded_exactly_without_discovery_preamble() -> None:
    module = load_bridge()
    message = (
        "Ta réponse précédente ne contient pas de résultat final. Termine maintenant "
        "la mission initiale et fournis directement le rapport Markdown demandé, sans "
        "recommencer toute la recherche."
    )
    request = _bridge_globals(module)["BridgeRunRequest"](
        input=message,
        recovery=True,
        conversation={
            "mode": "continue",
            "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "external_locator": ("https://chatgpt.com/c/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        },
    )

    chat_request = _openai_globals(module)["_response_chat_request"](
        module["bridge_routes"]._bridge_response_request(request)
    )

    assert len(chat_request.messages) == 1
    assert chat_request.messages[0].role == "user"
    assert chat_request.messages[0].content == message
    assert chat_request.new_chat is False


async def test_responses_facade_completes_background_request_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_bridge()

    async def fake_generation(*_: object, **__: object) -> AsyncIterator[str]:
        yield "résultat simulé"

    # `bridge.routes_openai` is import-cached across `load_bridge()` calls, so a
    # bare assignment here would leak `fake_generation` into every later test in
    # this process. `monkeypatch` reverts it when this test ends.
    monkeypatch.setitem(
        module["openai_routes"]._execute_background_response.__globals__,
        "run_generation",
        fake_generation,
    )
    module["bridge"].ws = object()
    request = _openai_globals(module)["ResponseRequest"](
        input="Recherche autorisée",
        tools=[{"type": "web_search"}],
        background=True,
    )
    http_request = Request({"type": "http", "method": "POST", "path": "/v1/responses"})

    queued = await module["openai_routes"].create_response(request, http_request)
    await module["openai_routes"].background_tasks[queued["id"]]
    completed = await module["openai_routes"].retrieve_response(queued["id"])

    assert queued["status"] == "queued"
    assert completed["status"] == "completed"
    assert completed["output_text"] == "résultat simulé"
    assert completed["usage"]["estimated"] is True


def test_native_bridge_contract_reports_honest_capabilities() -> None:
    module = load_bridge()
    request = _bridge_globals(module)["BridgeRunRequest"](
        requested_model="premium-profile",
        input="Recherche autorisée",
        web_search=True,
        reasoning_effort="high",
        background=True,
    )

    translated = module["bridge_routes"]._bridge_response_request(request)

    assert translated.model == "premium-profile"
    assert translated.tools == [{"type": "web_search"}]
    assert translated.background is True


def test_conversation_contract_is_explicit_and_rejects_arbitrary_navigation() -> None:
    module = load_bridge()
    conversation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    fresh = _bridge_globals(module)["BridgeRunRequest"](
        input="A1",
        conversation={"mode": "fresh", "id": conversation_id, "external_locator": None},
    )
    continued = _bridge_globals(module)["BridgeRunRequest"](
        input="A2",
        conversation={
            "mode": "continue",
            "id": conversation_id,
            "external_locator": "https://chatgpt.com/opaque/conversation-a",
        },
    )

    translate = _openai_globals(module)["_response_chat_request"]
    assert translate(module["bridge_routes"]._bridge_response_request(fresh)).new_chat
    assert not translate(module["bridge_routes"]._bridge_response_request(continued)).new_chat
    with pytest.raises(ValidationError):
        _bridge_globals(module)["BridgeRunRequest"](
            input="attaque SSRF",
            conversation={
                "mode": "continue",
                "id": conversation_id,
                "external_locator": "https://example.org/internal",
            },
        )
    with pytest.raises(ValidationError):
        _bridge_globals(module)["BridgeRunRequest"](
            input="continuation sans cible",
            conversation={"mode": "continue", "id": conversation_id},
        )


async def test_fake_extension_routes_a_b_a_and_retry_clicks_once(tmp_path: Path) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    extension = FakeExtension(module)
    module["bridge"].ws = extension
    conversation_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    conversation_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    async def send(key: str, conversation: dict[str, object]) -> dict[str, Any]:
        result = await module["bridge_routes"].create_bridge_run(
            _bridge_globals(module)["BridgeRunRequest"](input=key, conversation=conversation),
            request_with_key(key),
        )
        assert isinstance(result, dict)
        return result

    a1 = await send("a1", {"mode": "fresh", "id": conversation_a})
    b1 = await send("b1", {"mode": "fresh", "id": conversation_b})
    a2_target = {
        "mode": "continue",
        "id": conversation_a,
        "external_locator": a1["metadata"]["conversation"]["external_locator"],
    }
    a2 = await send("a2", a2_target)
    replay = await send("a2", a2_target)

    assert a2["metadata"]["conversation"]["id"] == conversation_a
    assert (
        a2["metadata"]["conversation"]["external_locator"]
        != b1["metadata"]["conversation"]["external_locator"]
    )
    assert replay == a2
    assert extension.prompt_count == 3


def test_requested_model_is_a_label_and_only_ui_model_drives_the_interface() -> None:
    module = load_bridge()
    build = _bridge_globals(module)["BridgeRunRequest"]
    bridge_controls = module["bridge_routes"]._bridge_controls

    label = bridge_controls(build(requested_model="premium-profile", input="x"))
    driving = bridge_controls(
        build(requested_model="premium-profile", ui_model="GPT-5 Thinking", input="x")
    )
    neutral = bridge_controls(build(ui_model="chatgpt-web", input="x"))

    assert label.model is None
    assert driving.model == "GPT-5 Thinking"
    assert neutral.model is None
    # `web_search=False` est une exigence, pas une absence : le bridge doit
    # désactiver un outil resté actif dans l'interface.
    assert label.web_search is False


def _stub_controls(
    monkeypatch: pytest.MonkeyPatch, module: dict[str, Any], applied: dict[str, Any], state: Any
) -> None:
    # `prepare_run`/`apply_controls` live in the (import-cached) `bridge.ui`
    # module, unlike the rest of `module`, which `runpy` re-executes fresh per
    # `load_bridge()` call. `monkeypatch` undoes the patch after the test so it
    # cannot leak into later tests sharing that cached module.
    ui_globals = _openai_globals(module)["prepare_run"].__globals__

    async def apply_controls(
        _bridge: Any, _controls: Any, _conversation: Any = None
    ) -> tuple[dict[str, Any], Any]:
        outcome = ui_globals["ControlOutcome"]
        return {name: outcome.model_validate(value) for name, value in applied.items()}, state

    monkeypatch.setitem(ui_globals, "apply_controls", apply_controls)


async def test_unverified_model_refuses_the_run_but_web_search_falls_back_to_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_bridge()
    openai_globals = _openai_globals(module)
    controls = openai_globals["RunControls"]
    bridge = module["bridge"]
    prepare_run = openai_globals["prepare_run"]

    _stub_controls(
        monkeypatch,
        module,
        {"model": {"requested": "GPT-5 Thinking", "ok": False, "reason": "absent du sélecteur"}},
        None,
    )
    with pytest.raises(HTTPException, match="non appliqué"):
        await prepare_run(
            bridge, controls(model="GPT-5 Thinking"), allow_unverified_model=False
        )

    tolerated = await prepare_run(
        bridge, controls(model="GPT-5 Thinking"), allow_unverified_model=True
    )
    assert tolerated.model_source == "unknown"

    _stub_controls(
        monkeypatch,
        module,
        {"web_search": {"requested": True, "ok": False, "reason": "bouton introuvable"}},
        None,
    )
    fallback = await prepare_run(
        bridge, controls(web_search=True), allow_unverified_model=False
    )
    assert fallback.web_search_mode == "prompt_instructed"


async def test_verified_ui_state_names_the_model_and_the_native_search_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_bridge()
    openai_globals = _openai_globals(module)
    state = openai_globals["prepare_run"].__globals__["UiState"].model_validate(
        {
            "model": {"supported": True, "selected": "GPT-5 Thinking", "verified": True},
            "web_search": {"supported": True, "enabled": True, "verified": True},
        }
    )
    _stub_controls(
        monkeypatch,
        module,
        {"web_search": {"requested": True, "ok": True, "verified": True}},
        state,
    )

    report = await openai_globals["prepare_run"](
        module["bridge"],
        openai_globals["RunControls"](web_search=True),
        allow_unverified_model=False,
    )
    body = openai_globals["_response_body"](
        "resp_x",
        openai_globals["ResponseRequest"](input="x", tools=[{"type": "web_search"}]),
        status="completed",
        output_text="ok",
        run=report,
    )

    assert report.web_search_mode == "ui_tool"
    assert body["model"] == "GPT-5 Thinking"
    assert body["metadata"]["model_source"] == "ui_observed"
    # Le message visible reste métier et ne décrit pas l'implémentation de l'outil.
    prompt = (
        openai_globals["_response_chat_request"](
            openai_globals["ResponseRequest"](input="x", tools=[{"type": "web_search"}]),
        )
        .messages[0]
        .content
    )
    assert "Recherche sur le Web" in prompt
    assert "interface" not in prompt


async def test_capabilities_degrade_visibly_without_a_connected_extension() -> None:
    module = load_bridge()
    module["bridge"].ws = None

    caps = await module["bridge_routes"].bridge_capabilities()

    assert caps["streaming"] == "final_delta_only"
    assert caps["web_search"] == "prompt_instructed"
    assert caps["actual_model_version"] is False
    assert caps["controls"]["model_selection"] == "unavailable"
    assert caps["ui"]["available"] is False
    assert caps["ui"]["stale"] is True
    assert caps["ui"]["state"] is None


async def test_ready_distinguishes_incomplete_absent_and_available_states() -> None:
    module = load_bridge()
    globals_ = module["ready"].__globals__
    globals_["HOST"] = "0.0.0.0"
    globals_["API_KEY"] = None
    globals_["WS_TOKEN"] = None
    module["bridge"].ws = None

    incomplete = await module["ready"]()
    incomplete_body = json.loads(incomplete.body)
    assert incomplete.status_code == 503
    assert incomplete_body["status"] == "configuration_incomplete"
    assert incomplete_body["server_operational"] is True
    assert incomplete_body["configuration"]["http_auth"] == "absent"
    assert incomplete_body["configuration"]["websocket_token"] == "absent"

    globals_["API_KEY"] = "not-logged-http-secret"
    globals_["WS_TOKEN"] = "not-logged-websocket-secret"
    absent = await module["ready"]()
    assert absent.status_code == 503
    assert json.loads(absent.body)["status"] == "extension_absent"

    module["bridge"].ws = FakeExtension(module)
    available = await module["ready"]()
    assert available.status_code == 200
    assert json.loads(available.body)["status"] == "extension_available"


async def test_startup_reports_safe_configuration_states(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    globals_ = module["_configuration_state"].__globals__
    globals_["HOST"] = "0.0.0.0"
    globals_["API_KEY"] = "STARTUP-HTTP-SECRET"
    globals_["WS_TOKEN"] = "STARTUP-WS-SECRET"
    module["bridge"].ws = None
    caplog.set_level(logging.INFO, logger="chatgpt_bridge")

    async with module["lifespan"](None):
        pass

    rendered = caplog.text
    assert "http_auth=configured" in rendered
    assert "websocket_token=configured" in rendered
    assert "sqlite_registry=accessible" in rendered
    assert "extension=disconnected" in rendered
    assert "STARTUP-HTTP-SECRET" not in rendered
    assert "STARTUP-WS-SECRET" not in rendered


async def test_three_http_retries_with_same_key_submit_one_prompt_and_replay_result(
    tmp_path: Path,
) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    extension = FakeExtension(module, prompt_delay=0.02)
    module["bridge"].ws = extension
    req = _bridge_globals(module)["BridgeRunRequest"](input="secret prompt")

    first, second, third = await asyncio.gather(
        module["bridge_routes"].create_bridge_run(req, request_with_key("business-1")),
        module["bridge_routes"].create_bridge_run(req, request_with_key("business-1")),
        module["bridge_routes"].create_bridge_run(req, request_with_key("business-1")),
    )
    replay = await module["bridge_routes"].create_bridge_run(req, request_with_key("business-1"))

    assert first["id"] == second["id"] == third["id"] == replay["id"]
    assert extension.prompt_count == 1
    assert first["metadata"]["completion_signal"] == "assistant_actions"
    assert first["metadata"]["completion_confidence"] == "high"
    assert first["metadata"]["stable_for_ms"] == 2_100
    assert first["metadata"]["output_chars"] == 2
    assert first["metadata"]["visible_citation_count"] == 0
    assert first["metadata"]["content_script_version"] == "14"


async def test_background_bridge_run_returns_immediately_and_is_polled_to_completion(
    tmp_path: Path,
) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    generation_started = asyncio.Event()
    release_generation = asyncio.Event()

    class ControlledExtension(FakeExtension):
        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            generation_started.set()
            await release_generation.wait()
            self.module["bridge"].dispatch(
                {
                    "type": "done",
                    "id": payload["id"],
                    "event_id": "1",
                    "text": "snapshot final unique",
                    "metadata": {
                        "completion_signal": "assistant_actions",
                        "completion_confidence": "high",
                        "stable_for_ms": 2_100,
                        "output_chars": 21,
                        "visible_citation_count": 0,
                        "content_script_version": "14",
                    },
                }
            )

    extension = ControlledExtension(module)
    module["bridge"].ws = extension
    request = _bridge_globals(module)["BridgeRunRequest"](input="durable", background=True)

    create_bridge_run = module["bridge_routes"].create_bridge_run
    accepted = await create_bridge_run(request, request_with_key("durable-background"))
    replay = await create_bridge_run(request, request_with_key("durable-background"))
    await generation_started.wait()
    running = await module["bridge_routes"].retrieve_bridge_run(accepted["id"])

    assert accepted["status"] in {"queued", "running"}
    assert replay["id"] == accepted["id"]
    assert running["status"] == "running"
    assert extension.prompt_count == 1

    release_generation.set()
    await module["bridge_routes"].idempotent_tasks[accepted["id"]]
    completed = await module["bridge_routes"].retrieve_bridge_run(accepted["id"])

    assert completed["status"] == "completed"
    assert completed["output_text"] == "snapshot final unique"
    assert extension.prompt_count == 1


async def test_conversation_binding_precedes_incomplete_and_survives_restart(
    tmp_path: Path,
) -> None:
    module = load_bridge()
    database = tmp_path / "runs.sqlite3"
    isolated_registry(module, tmp_path)
    bound = asyncio.Event()
    release = asyncio.Event()
    conversation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    locator = f"https://chatgpt.com/c/{conversation_id}"

    class IncompleteExtension(FakeExtension):
        wrong_conversation = False

        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] == "recovery_capture":
                self.module["bridge"].dispatch(
                    {
                        "type": "recovery_preview",
                        "id": payload["id"],
                        "conversation_id": (
                            "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                            if self.wrong_conversation
                            else conversation_id
                        ),
                        "external_locator": locator,
                        "turn_id": "assistant-later",
                        "text": "## SUBJECT S1\ntitle: Réponse récupérée\n",
                        "metadata": {"completion_signal": "assistant_actions"},
                    }
                )
                return
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            conversation = {
                "id": conversation_id,
                "external_locator": locator,
                "assistant_turns_before": 2,
                "initial_assistant_turn_id": "assistant-before",
                "verified": True,
                "verified_at": "2026-08-13T10:00:00.000Z",
            }
            self.module["bridge"].dispatch(
                {
                    "type": "conversation_bound",
                    "id": payload["id"],
                    "event_id": "1",
                    "conversation": conversation,
                }
            )
            bound.set()
            await release.wait()
            self.module["bridge"].dispatch(
                {
                    "type": "incomplete",
                    "id": payload["id"],
                    "event_id": "2",
                    "reason": "no_final_answer",
                    "metadata": {
                        "completion_signal": "assistant_actions",
                        "completion_confidence": "high",
                        "initial_turn_id": "assistant-empty",
                        "output_chars": 0,
                    },
                }
            )

    extension = IncompleteExtension(module)
    module["bridge"].ws = extension
    request = _bridge_globals(module)["BridgeRunRequest"](
        input="mission",
        background=True,
        conversation={"mode": "fresh", "id": conversation_id},
    )
    accepted = await module["bridge_routes"].create_bridge_run(
        request, request_with_key("incomplete-bound")
    )
    await bound.wait()

    persisted = module["bridge_routes"].registry.get_by_run_id(accepted["id"])
    assert persisted is not None
    binding = json.loads(persisted["conversation_json"])
    assert binding["external_locator"] == locator
    assert binding["assistant_turns_before"] == 2

    release.set()
    await module["bridge_routes"].idempotent_tasks[accepted["id"]]
    needs_review = await module["bridge_routes"].retrieve_bridge_run(accepted["id"])
    assert needs_review["status"] == "needs_review"
    assert needs_review["error"]["code"] == "no_final_answer"

    restarted = module["RunRegistry"](database)
    module["bridge_routes"].registry = restarted
    preview = await module["bridge_routes"].preview_visible_recovery(accepted["id"])
    assert preview["turn_id"] == "assistant-later"
    assert preview["text"].startswith("## SUBJECT S1")
    assert extension.prompt_count == 1

    extension.wrong_conversation = True
    with pytest.raises(HTTPException) as mismatched:
        await module["bridge_routes"].preview_visible_recovery(accepted["id"])
    assert mismatched.value.status_code == 409
    assert extension.prompt_count == 1


async def test_done_snapshot_replaces_rewritten_legacy_chunks(tmp_path: Path) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)

    class RewritingExtension(FakeExtension):
        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            for sequence, text in enumerate(("ABC", "DE", "XYZ"), start=1):
                self.module["bridge"].dispatch(
                    {
                        "type": "chunk",
                        "id": payload["id"],
                        "text": text,
                        "event_id": str(sequence),
                    }
                )
            self.module["bridge"].dispatch(
                {
                    "type": "done",
                    "id": payload["id"],
                    "text": "ABXYZ",
                    "event_id": "4",
                    "metadata": {
                        "completion_signal": "assistant_actions",
                        "completion_confidence": "high",
                        "stable_for_ms": 2_100,
                        "output_chars": 5,
                        "visible_citation_count": 0,
                        "content_script_version": "14",
                    },
                }
            )

    extension = RewritingExtension(module)
    module["bridge"].ws = extension

    result = await module["bridge_routes"].create_bridge_run(
        _bridge_globals(module)["BridgeRunRequest"](input="rewrite"),
        request_with_key("rewrite-final"),
    )

    assert result["output_text"] == "ABXYZ"
    assert result["output_text"] != "ABCDEXYZ"


async def test_done_rejects_incoherent_output_chars(tmp_path: Path) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)

    class InvalidLengthExtension(FakeExtension):
        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            self.module["bridge"].dispatch(
                {
                    "type": "done",
                    "id": payload["id"],
                    "text": "final",
                    "event_id": "1",
                    "metadata": {"output_chars": 99},
                }
            )

    module["bridge"].ws = InvalidLengthExtension(module)

    with pytest.raises(HTTPException) as caught:
        await module["bridge_routes"].create_bridge_run(
            _bridge_globals(module)["BridgeRunRequest"](input="bad length"),
            request_with_key("bad-length"),
        )
    assert caught.value.status_code == 502
    assert "longueur du snapshot final incohérente" in str(caught.value.detail)


async def test_cancelled_http_wait_then_retry_joins_original_run(tmp_path: Path) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    extension = FakeExtension(module, prompt_delay=0.05)
    module["bridge"].ws = extension
    req = _bridge_globals(module)["BridgeRunRequest"](input="expensive prompt")
    create_bridge_run = module["bridge_routes"].create_bridge_run

    abandoned = asyncio.create_task(
        create_bridge_run(req, request_with_key("business-timeout"))
    )
    await asyncio.sleep(0.01)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    replay = await create_bridge_run(req, request_with_key("business-timeout"))

    assert replay["status"] == "completed"
    assert extension.prompt_count == 1


async def test_shutdown_during_run_fails_safe_without_second_prompt(tmp_path: Path) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    extension = FakeExtension(module, prompt_delay=60)
    module["bridge"].ws = extension
    req = _bridge_globals(module)["BridgeRunRequest"](input="expensive prompt")
    create_bridge_run = module["bridge_routes"].create_bridge_run

    active = asyncio.create_task(create_bridge_run(req, request_with_key("sigterm-run")))
    for _ in range(100):
        if extension.prompt_count:
            break
        await asyncio.sleep(0.001)
    assert extension.prompt_count == 1

    await module["shutdown_bridge"](0.01)
    with pytest.raises(asyncio.CancelledError):
        await active
    assert extension.closed == (1001, "server shutdown")

    # Simule le redémarrage : la même clé rejoue l'échec SQLite et ne touche
    # pas la nouvelle extension, même si elle est disponible.
    module["shutdown_bridge"].__globals__["accepting_runs"] = True
    module["bridge"].closing = False
    replacement = FakeExtension(module)
    module["bridge"].ws = replacement
    replay = await create_bridge_run(req, request_with_key("sigterm-run"))

    assert replay.status_code == 503
    assert json.loads(replay.body)["error"]["code"] == "bridge_server_error"
    assert replacement.prompt_count == 0


async def test_same_key_different_payload_conflicts(tmp_path: Path) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    extension = FakeExtension(module)
    module["bridge"].ws = extension
    await module["bridge_routes"].create_bridge_run(
        _bridge_globals(module)["BridgeRunRequest"](input="one"), request_with_key("conflict")
    )

    with pytest.raises(HTTPException) as caught:
        await module["bridge_routes"].create_bridge_run(
            _bridge_globals(module)["BridgeRunRequest"](input="two"), request_with_key("conflict")
        )
    assert caught.value.status_code == 409
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail["code"] == "bridge_payload_conflict"


async def test_completed_run_survives_registry_restart_without_ui(tmp_path: Path) -> None:
    module = load_bridge()
    database = tmp_path / "runs.sqlite3"
    isolated_registry(module, tmp_path)
    extension = FakeExtension(module)
    module["bridge"].ws = extension
    req = _bridge_globals(module)["BridgeRunRequest"](input="once")
    first = await module["bridge_routes"].create_bridge_run(req, request_with_key("restart"))

    module["bridge_routes"].registry = module["RunRegistry"](database)
    module["bridge"].ws = None
    replay = await module["bridge_routes"].create_bridge_run(req, request_with_key("restart"))

    assert replay["id"] == first["id"]
    assert extension.prompt_count == 1


async def test_capabilities_never_waits_for_a_blocked_extension() -> None:
    module = load_bridge()

    class Blocked:
        async def send_json(self, _: dict[str, Any]) -> None:
            await asyncio.Event().wait()

    module["bridge"].ws = Blocked()
    started = asyncio.get_running_loop().time()
    caps = await module["bridge_routes"].bridge_capabilities()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert caps["extension_connected"] is True


async def test_ui_probe_timeout_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_bridge()

    class Silent:
        async def send_json(self, _: dict[str, Any]) -> None:
            return None

    module["bridge"].ws = Silent()
    # `bridge.routes_bridge` is import-cached across `load_bridge()` calls (like
    # `bridge.routes_openai`, see R59a): a bare assignment here would leak
    # UI_TIMEOUT=0.01 into every later test in this process.
    monkeypatch.setitem(
        module["bridge_routes"].bridge_capabilities.__globals__, "UI_TIMEOUT", 0.01
    )
    with pytest.raises(HTTPException) as caught:
        await module["bridge_routes"].bridge_capabilities(probe=True, fresh=True)

    assert caught.value.status_code == 504
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail["code"] == "bridge_ui_timeout"


async def test_websocket_without_pairing_token_is_rejected() -> None:
    module = load_bridge()
    module["websocket_endpoint"].__globals__["WS_TOKEN"] = "required-secret"

    class UnauthenticatedSocket:
        def __init__(self) -> None:
            self.query_params: dict[str, str] = {}
            self.accepted = False
            self.closed: tuple[int, str] | None = None

        async def accept(self) -> None:
            self.accepted = True

        async def close(self, code: int, reason: str) -> None:
            self.closed = (code, reason)

    socket = UnauthenticatedSocket()
    await module["websocket_endpoint"](socket)

    assert socket.accepted is False
    assert socket.closed == (4401, "authentication required")


async def test_duplicate_websocket_event_is_dispatched_once() -> None:
    module = load_bridge()
    bridge = module["Bridge"]()
    queue = bridge.open_channel("run-1")
    packet = {"id": "run-1", "type": "chunk", "text": "x", "event_id": "event-1"}

    bridge.dispatch(packet)
    bridge.dispatch(packet)

    assert (await queue.get())["text"] == "x"
    assert queue.empty()


async def test_bridge_logs_neither_prompt_nor_idempotency_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = load_bridge()
    isolated_registry(module, tmp_path)
    module["bridge"].ws = FakeExtension(module)
    caplog.set_level(logging.INFO, logger="chatgpt_bridge")

    await module["bridge_routes"].create_bridge_run(
        _bridge_globals(module)["BridgeRunRequest"](input="TOP-SECRET-PROMPT"),
        request_with_key("TOP-SECRET-IDEMPOTENCY-KEY"),
    )

    rendered = caplog.text
    assert "TOP-SECRET-PROMPT" not in rendered
    assert "TOP-SECRET-IDEMPOTENCY-KEY" not in rendered
    assert "idempotency_fingerprint=" in rendered


def test_compose_and_makefile_bridge_lifecycle_contract() -> None:
    root = Path(__file__).parents[2]
    compose = (root / "compose.yaml").read_text()
    makefile = (root / "Makefile").read_text()
    environment = compose.split("environment: &backend-environment", 1)[1].split(
        "\n    volumes:", 1
    )[0]
    backend = compose.split("\n  backend:", 1)[1].split("\n  migrate:", 1)[0]
    backend_depends = backend.split("\n    depends_on:", 1)[1].split("\n    healthcheck:", 1)[0]
    worker = compose.split("\n  worker:", 1)[1].split("\n  job-recovery:", 1)[0]
    postgres = compose.split("\n  postgres:", 1)[1].split("\n  redis:", 1)[0]
    redis = compose.split("\n  redis:", 1)[1].split("\n  minio:", 1)[0]

    assert (
        "OPENAI_BRIDGE_BASE_URL: "
        "${OPENAI_BRIDGE_BASE_URL:-http://chatgpt-bridge:8001/v1}" in environment
    )
    assert "127.0.0.1:8001/v1" not in environment
    assert "chatgpt-bridge:\n        condition: service_healthy" in worker
    assert "chatgpt-bridge:" not in backend_depends
    assert "depends_on:" not in postgres
    assert "depends_on:" not in redis
    assert "bridge_data:/data" in compose
    assert "stop_grace_period: 30s" in compose

    assert "$(COMPOSE) up -d --build --wait" in makefile
    assert "$(COMPOSE) down -v" not in makefile
    # A data reset must never drop bridge_data: it holds the authenticated
    # ChatGPT browser profile, and losing it means logging the bridge back in.
    assert "docker volume rm" in makefile
    assert "bridge_data" not in makefile.split("CLEAN_VOLUMES = ", 1)[1].split("\n", 1)[0]
    assert "python tools/status.py" in makefile
    status_script = (root / "chatgpt-bridge" / "tools" / "status.py").read_text()
    assert 'os.getenv("BRIDGE_API_KEY")' in status_script
    assert "print(key)" not in status_script
    server = (root / "chatgpt-bridge" / "server.py").read_text()
    assert "access_log=False" in server
    assert 'log_level="warning"' in server
    assert "logger.propagate = False" in server


async def test_idle_timeout_does_not_send_abort_to_extension() -> None:
    """Vérifier que le timeout de 120s ne déclenche plus un abort automatique.

    Avant la correction :
    - Timeout après 120s sans paquet
    - finally envoie automatiquement {"type": "abort"}
    - extension clique Stop dans ChatGPT
    - → "Stopped thinking"

    Après la correction :
    - Timeout après 120s sans paquet
    - finally ferme le canal HTTP uniquement
    - ChatGPT continue dans son onglet
    - récupération DOM possible via /recovery/visible
    """
    module = load_bridge()

    # Extension muette : elle reçoit le prompt mais ne redispatche jamais rien,
    # ce qui reproduit exactement la disparition des paquets côté extension.
    class SilentExtension:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []
            self.closed: tuple[int, str] | None = None

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def close(self, code: int, reason: str) -> None:
            self.closed = (code, reason)

    silent = SilentExtension()
    module["bridge"].ws = silent

    openai_globals = _openai_globals(module)
    chat_request = openai_globals["ChatRequest"](
        messages=[{"role": "user", "content": "test"}],
    )

    async def never_disconnects() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    http_req = request_with_key("timeout-test")
    http_req._receive = never_disconnects

    run_generation = openai_globals["run_generation"]
    old_idle = run_generation.__globals__["IDLE_TIMEOUT"]
    run_generation.__globals__["IDLE_TIMEOUT"] = 0.1
    try:
        with pytest.raises(Exception) as caught:
            async for _ in run_generation(
                module["bridge"], module["run_registry"], "timeout-test", chat_request, http_req
            ):
                pass
    finally:
        run_generation.__globals__["IDLE_TIMEOUT"] = old_idle

    assert "aucune donnée de l'extension depuis" in str(caught.value)

    # Vérification critique : le nettoyage du serveur ne clique jamais Stop.
    abort_messages = [msg for msg in silent.sent if msg.get("type") == "abort"]
    assert not abort_messages, (
        "Un message abort ne doit jamais être envoyé automatiquement, "
        f"mais {len(abort_messages)} ont été trouvé(s)"
    )
    assert [msg for msg in silent.sent if msg.get("type") == "prompt"], (
        "Le prompt aurait dû être transmis à l'extension"
    )


class HeartbeatingExtension:
    """Extension vivante : heartbeat régulier, `done` optionnel.

    Elle reproduit le cas qui a fait diagnostiquer à tort une extension muette :
    ChatGPT travaille, les heartbeats arrivent toutes les quelques secondes, mais
    la génération dépasse la borne totale du bridge.
    """

    def __init__(
        self,
        module: dict[str, Any],
        *,
        interval: float,
        done_after: float | None = None,
    ) -> None:
        self.module = module
        self.interval = interval
        self.done_after = done_after
        self.sent: list[dict[str, Any]] = []
        self.beats = 0
        self.task: asyncio.Task[None] | None = None
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        if payload.get("type") == "prompt":
            self.task = asyncio.create_task(self._beat(payload["id"]))

    async def _beat(self, request_id: str) -> None:
        started = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(self.interval)
            if (
                self.done_after is not None
                and asyncio.get_running_loop().time() - started >= self.done_after
            ):
                self.module["bridge"].dispatch(
                    {
                        "type": "done",
                        "id": request_id,
                        "event_id": "done",
                        "text": "rapport final",
                        "metadata": {
                            "completion_signal": "assistant_actions",
                            "completion_confidence": "high",
                            "stable_for_ms": 2_100,
                            "output_chars": len("rapport final"),
                            "visible_citation_count": 0,
                            "content_script_version": "16",
                        },
                    }
                )
                return
            self.beats += 1
            self.module["bridge"].dispatch(
                {
                    "type": "heartbeat",
                    "id": request_id,
                    "event_id": f"hb-{self.beats}",
                    "progress": {
                        "phase": "generating",
                        "output_chars": 30_454,
                        "stable_for_ms": 0,
                        "completion_signal": "streaming",
                        "completion_confidence": "high",
                    },
                }
            )

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()


async def _generate(
    module: dict[str, Any],
    extension: Any,
    request_id: str,
    *,
    total_timeout: float,
    idle_timeout: float,
) -> tuple[list[str], Exception | None, float]:
    """Exécute une génération complète avec des échéances accélérées."""
    module["bridge"].ws = extension
    openai_globals = _openai_globals(module)
    chat_request = openai_globals["ChatRequest"](
        messages=[{"role": "user", "content": "recherche"}]
    )

    async def never_disconnects() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    http_req = request_with_key(request_id)
    http_req._receive = never_disconnects

    run_generation = openai_globals["run_generation"]
    globals_ = run_generation.__globals__
    previous = (globals_["TOTAL_TIMEOUT"], globals_["IDLE_TIMEOUT"])
    globals_["TOTAL_TIMEOUT"] = total_timeout
    globals_["IDLE_TIMEOUT"] = idle_timeout
    chunks: list[str] = []
    failure: Exception | None = None
    started = asyncio.get_running_loop().time()
    try:
        async for text in run_generation(
            module["bridge"], module["run_registry"], request_id, chat_request, http_req
        ):
            chunks.append(text)
    except Exception as exc:
        # Le test inspecte lui-même le type et le code de l'échec.
        failure = exc
    finally:
        elapsed = asyncio.get_running_loop().time() - started
        globals_["TOTAL_TIMEOUT"], globals_["IDLE_TIMEOUT"] = previous
        if hasattr(extension, "stop"):
            extension.stop()
    return chunks, failure, elapsed


async def test_live_generation_reaching_the_total_deadline_is_never_called_idle() -> None:
    """Le bug du 17/08 : 900 s pile, heartbeats reçus, message « aucune donnée ».

    `asyncio.wait_for` expirait sur la borne totale, mais la branche d'erreur
    accusait systématiquement l'extension d'être muette.
    """
    module = load_bridge()
    extension = HeartbeatingExtension(module, interval=0.05)

    _, failure, elapsed = await _generate(
        module, extension, "total-timeout", total_timeout=0.5, idle_timeout=0.15
    )

    assert isinstance(failure, _openai_globals(module)["UpstreamError"])
    assert failure.code == "bridge_total_timeout"
    assert "génération non terminée après" in str(failure)
    assert "aucune donnée de l'extension" not in str(failure)
    # La borne totale, et elle seule, a mis fin au run : les heartbeats
    # réarmaient l'attente d'inactivité à chaque paquet.
    assert elapsed >= 0.5
    assert extension.beats >= 3


async def test_silent_extension_is_reported_as_idle_well_before_the_total_deadline() -> None:
    module = load_bridge()

    class SilentExtension:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_json(self, payload: dict[str, Any]) -> None:
            self.sent.append(payload)

        async def close(self, code: int, reason: str) -> None:
            return None

    _, failure, elapsed = await _generate(
        module, SilentExtension(), "idle-timeout", total_timeout=2.0, idle_timeout=0.2
    )

    assert isinstance(failure, _openai_globals(module)["UpstreamError"])
    assert failure.code == "bridge_idle_timeout"
    assert "aucune donnée de l'extension depuis" in str(failure)
    assert elapsed < 1.0


async def test_long_but_live_generation_completes_before_the_total_deadline() -> None:
    module = load_bridge()
    extension = HeartbeatingExtension(module, interval=0.05, done_after=0.6)

    chunks, failure, _ = await _generate(
        module, extension, "live-completion", total_timeout=1.5, idle_timeout=0.15
    )

    assert failure is None
    assert "".join(chunks) == "rapport final"
    # Le run a duré bien plus longtemps que l'idle timeout sans jamais expirer.
    assert extension.beats >= 3


def test_worker_time_limit_outlives_the_bridge_total_timeout() -> None:
    """Le worker doit survivre au bridge, jamais l'inverse.

    Les deux bornes vivent dans deux composants différents ; les laisser dériver
    l'une par rapport à l'autre a déjà tué le worker en pleine attente. Le défaut
    du bridge fut aussi longtemps recopié depuis MODEL_REQUEST_TIMEOUT_SECONDS,
    qui borne un appel HTTP court et n'a rien à dire sur la durée d'une recherche.
    """
    compose = (Path(__file__).parents[2] / "compose.yaml").read_text()

    def default_of(variable: str) -> float:
        match = re.search(rf"\$\{{{variable}:-([0-9.]+)\}}", compose)
        assert match, f"{variable} n'a plus de défaut dans compose.yaml"
        return float(match.group(1))

    bridge_total = default_of("BRIDGE_TOTAL_TIMEOUT_SECONDS")
    actor_limit = default_of("JOB_ACTOR_TIME_LIMIT_SECONDS")

    # Une recherche approfondie ChatGPT doit pouvoir occuper l'heure entière
    # sans être coupée, et le worker doit lui survivre avec de la marge.
    assert bridge_total >= 3600
    assert actor_limit >= bridge_total + 600
    assert "BRIDGE_TOTAL_TIMEOUT: ${MODEL_REQUEST_TIMEOUT_SECONDS" not in compose
