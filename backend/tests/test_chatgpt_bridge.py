from __future__ import annotations

import runpy
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request


def load_bridge() -> dict[str, Any]:
    path = Path(__file__).parents[2] / "chatgpt-bridge" / "server.py"
    return runpy.run_path(str(path))


def test_responses_facade_translates_web_search_and_rejects_binary_blocks() -> None:
    module = load_bridge()
    response_request = module["ResponseRequest"]
    translate = module["_response_chat_request"]

    translated = translate(
        response_request(
            model="chatgpt-web",
            input=[{"role": "user", "content": "Recherche autorisée"}],
            tools=[{"type": "web_search"}],
        )
    )

    assert translated.files == []
    assert translated.new_chat is True
    assert "recherche web" in translated.messages[0].content

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


async def test_responses_facade_completes_background_request_without_network() -> None:
    module = load_bridge()

    async def fake_generation(*_: object, **__: object):
        yield "résultat simulé"

    module["_execute_background_response"].__globals__["run_generation"] = fake_generation
    module["bridge"].ws = object()
    request = module["ResponseRequest"](
        input="Recherche autorisée",
        tools=[{"type": "web_search"}],
        background=True,
    )
    http_request = Request({"type": "http", "method": "POST", "path": "/v1/responses"})

    queued = await module["create_response"](request, http_request)
    await module["background_tasks"][queued["id"]]
    completed = await module["retrieve_response"](queued["id"])

    assert queued["status"] == "queued"
    assert completed["status"] == "completed"
    assert completed["output_text"] == "résultat simulé"
    assert completed["usage"]["estimated"] is True


def test_native_bridge_contract_reports_honest_capabilities() -> None:
    module = load_bridge()
    request = module["BridgeRunRequest"](
        requested_model="premium-profile",
        input="Recherche autorisée",
        web_search=True,
        reasoning_effort="high",
        background=True,
    )

    translated = module["_bridge_response_request"](request)

    assert translated.model == "premium-profile"
    assert translated.tools == [{"type": "web_search"}]
    assert translated.background is True


def test_requested_model_is_a_label_and_only_ui_model_drives_the_interface() -> None:
    module = load_bridge()
    build = module["BridgeRunRequest"]

    label = module["_bridge_controls"](build(requested_model="premium-profile", input="x"))
    driving = module["_bridge_controls"](
        build(requested_model="premium-profile", ui_model="GPT-5 Thinking", input="x")
    )
    neutral = module["_bridge_controls"](build(ui_model="chatgpt-web", input="x"))

    assert label.model is None
    assert driving.model == "GPT-5 Thinking"
    assert neutral.model is None
    # `web_search=False` est une exigence, pas une absence : le bridge doit
    # désactiver un outil resté actif dans l'interface.
    assert label.web_search is False


def _stub_controls(module: dict[str, Any], applied: dict[str, Any], state: Any) -> None:
    async def apply_controls(_: Any) -> tuple[dict[str, Any], Any]:
        outcome = module["ControlOutcome"]
        return {name: outcome.model_validate(value) for name, value in applied.items()}, state

    module["prepare_run"].__globals__["apply_controls"] = apply_controls


async def test_unverified_model_refuses_the_run_but_web_search_falls_back_to_the_prompt() -> None:
    module = load_bridge()
    controls = module["RunControls"]

    _stub_controls(
        module,
        {"model": {"requested": "GPT-5 Thinking", "ok": False, "reason": "absent du sélecteur"}},
        None,
    )
    with pytest.raises(HTTPException, match="non appliqué"):
        await module["prepare_run"](controls(model="GPT-5 Thinking"), allow_unverified_model=False)

    tolerated = await module["prepare_run"](
        controls(model="GPT-5 Thinking"), allow_unverified_model=True
    )
    assert tolerated.model_source == "unknown"

    _stub_controls(
        module,
        {"web_search": {"requested": True, "ok": False, "reason": "bouton introuvable"}},
        None,
    )
    fallback = await module["prepare_run"](controls(web_search=True), allow_unverified_model=False)
    assert fallback.web_search_mode == "prompt_instructed"


async def test_verified_ui_state_names_the_model_and_the_native_search_tool() -> None:
    module = load_bridge()
    state = module["UiState"].model_validate(
        {
            "model": {"supported": True, "selected": "GPT-5 Thinking", "verified": True},
            "web_search": {"supported": True, "enabled": True, "verified": True},
        }
    )
    _stub_controls(module, {"web_search": {"requested": True, "ok": True, "verified": True}}, state)

    report = await module["prepare_run"](
        module["RunControls"](web_search=True), allow_unverified_model=False
    )
    body = module["_response_body"](
        "resp_x",
        module["ResponseRequest"](input="x", tools=[{"type": "web_search"}]),
        status="completed",
        output_text="ok",
        run=report,
    )

    assert report.web_search_mode == "ui_tool"
    assert body["model"] == "GPT-5 Thinking"
    assert body["metadata"]["model_source"] == "ui_observed"
    # L'instruction de repli ne doit pas prétendre demander ce que l'outil fait déjà.
    prompt = (
        module["_response_chat_request"](
            module["ResponseRequest"](input="x", tools=[{"type": "web_search"}]),
            web_search_native=True,
        )
        .messages[0]
        .content
    )
    assert "activée dans l'interface" in prompt


async def test_capabilities_degrade_visibly_without_a_connected_extension() -> None:
    module = load_bridge()
    module["bridge"].ws = None

    caps = await module["bridge_capabilities"]()

    assert caps["web_search"] == "prompt_instructed"
    assert caps["actual_model_version"] is False
    assert caps["controls"]["model_selection"] == "unavailable"
    assert caps["ui"] == {"available": False, "reason": "extension non connectée"}
