"""Tests for `bridge/routes_bridge.py`: native runs, idempotency, conversation
binding, model/UI controls, capabilities, and recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeExtension, isolated_registry, request_with_key
from fastapi import HTTPException
from pydantic import ValidationError

from bridge.app import BridgeApplication
from bridge.contracts import BridgeRunRequest
from bridge.generation import _response_chat_request
from bridge.registry import RunRegistry


def test_native_bridge_contract_reports_honest_capabilities(runtime: BridgeApplication) -> None:
    request = BridgeRunRequest(
        requested_model="premium-profile",
        input="Recherche autorisée",
        web_search=True,
        reasoning_effort="high",
        background=True,
    )

    translated = runtime.bridge_routes._bridge_response_request(request)

    assert translated.model == "premium-profile"
    assert translated.tools == [{"type": "web_search"}]
    assert translated.background is True


def test_conversation_contract_is_explicit_and_rejects_arbitrary_navigation(
    runtime: BridgeApplication,
) -> None:
    conversation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    fresh = BridgeRunRequest(
        input="A1",
        conversation={"mode": "fresh", "id": conversation_id},
    )
    continued = BridgeRunRequest(
        input="A2",
        conversation={
            "mode": "continue",
            "id": conversation_id,
            "expected_turn_id": "external-turn-1",
        },
    )

    assert not _response_chat_request(
        runtime.bridge_routes._bridge_response_request(fresh)
    ).new_chat
    assert not _response_chat_request(
        runtime.bridge_routes._bridge_response_request(continued)
    ).new_chat
    # fresh forbids a pre-existing expected_turn_id.
    with pytest.raises(ValidationError):
        BridgeRunRequest(
            input="fresh avec cible",
            conversation={
                "mode": "fresh",
                "id": conversation_id,
                "expected_turn_id": "external-turn-1",
            },
        )
    with pytest.raises(ValidationError):
        BridgeRunRequest(
            input="continuation sans cible",
            conversation={"mode": "continue", "id": conversation_id},
        )
    # external_locator is not a routing field at all: any input under that key
    # is rejected as an unexpected extra field, not silently accepted.
    with pytest.raises(ValidationError):
        BridgeRunRequest(
            input="locator arbitraire",
            conversation={
                "mode": "continue",
                "id": conversation_id,
                "expected_turn_id": "external-turn-1",
                "external_locator": "https://example.org/internal",
            },
        )


def test_requested_model_is_a_label_and_only_ui_model_drives_the_interface(
    runtime: BridgeApplication,
) -> None:
    bridge_controls = runtime.bridge_routes._bridge_controls

    label = bridge_controls(BridgeRunRequest(requested_model="premium-profile", input="x"))
    driving = bridge_controls(
        BridgeRunRequest(requested_model="premium-profile", ui_model="GPT-5 Thinking", input="x")
    )
    neutral = bridge_controls(BridgeRunRequest(ui_model="chatgpt-web", input="x"))

    assert label.model is None
    assert driving.model == "GPT-5 Thinking"
    assert neutral.model is None
    # `web_search=False` est une exigence, pas une absence : le bridge doit
    # désactiver un outil resté actif dans l'interface.
    assert label.web_search is False


async def test_fake_extension_routes_a_b_a_and_retry_clicks_once(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)
    extension = FakeExtension(runtime)
    runtime.bridge.ws = extension
    conversation_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    conversation_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    async def send(key: str, conversation: dict[str, object]) -> dict[str, Any]:
        result = await runtime.bridge_routes.create_bridge_run(
            BridgeRunRequest(input=key, conversation=conversation),
            request_with_key(key),
        )
        assert isinstance(result, dict)
        return result

    a1 = await send("a1", {"mode": "fresh", "id": conversation_a})
    b1 = await send("b1", {"mode": "fresh", "id": conversation_b})
    a2_target = {
        "mode": "continue",
        "id": conversation_a,
        "expected_turn_id": a1["metadata"]["conversation"]["turn_id"],
    }
    a2 = await send("a2", a2_target)
    replay = await send("a2", a2_target)

    assert a2["metadata"]["conversation"]["id"] == conversation_a
    # A/B/A routing is by id + expected_turn_id, never by a locator: both
    # simulated conversations may legitimately share the same diagnostic URL.
    assert (
        a2["metadata"]["conversation"]["external_locator"]
        == b1["metadata"]["conversation"]["external_locator"]
    )
    assert a1["metadata"]["conversation"]["turn_id"] != b1["metadata"]["conversation"]["turn_id"]
    assert replay == a2
    assert extension.prompt_count == 3


async def test_three_http_retries_with_same_key_submit_one_prompt_and_replay_result(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)
    extension = FakeExtension(runtime, prompt_delay=0.02)
    runtime.bridge.ws = extension
    req = BridgeRunRequest(input="secret prompt")

    first, second, third = await asyncio.gather(
        runtime.bridge_routes.create_bridge_run(req, request_with_key("business-1")),
        runtime.bridge_routes.create_bridge_run(req, request_with_key("business-1")),
        runtime.bridge_routes.create_bridge_run(req, request_with_key("business-1")),
    )
    replay = await runtime.bridge_routes.create_bridge_run(req, request_with_key("business-1"))

    assert first["id"] == second["id"] == third["id"] == replay["id"]
    assert extension.prompt_count == 1
    assert first["metadata"]["completion_signal"] == "assistant_actions"
    assert first["metadata"]["completion_confidence"] == "high"
    assert first["metadata"]["stable_for_ms"] == 2_100
    assert first["metadata"]["output_chars"] == 2
    assert first["metadata"]["visible_citation_count"] == 0
    assert first["metadata"]["content_script_version"] == "14"


async def test_background_bridge_run_returns_immediately_and_is_polled_to_completion(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)
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
            browser_target = payload.get("browser_target")
            self.runtime.bridge.dispatch(
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
                    "target_id": browser_target["id"],
                    "tab_id": 1,
                }
            )

    extension = ControlledExtension(runtime)
    runtime.bridge.ws = extension
    request = BridgeRunRequest(input="durable", background=True)

    create_bridge_run = runtime.bridge_routes.create_bridge_run
    accepted = await create_bridge_run(request, request_with_key("durable-background"))
    replay = await create_bridge_run(request, request_with_key("durable-background"))
    await generation_started.wait()
    running = await runtime.bridge_routes.retrieve_bridge_run(accepted["id"])

    assert accepted["status"] in {"queued", "running"}
    assert replay["id"] == accepted["id"]
    assert running["status"] == "running"
    assert extension.prompt_count == 1

    release_generation.set()
    await runtime.bridge_routes.idempotent_tasks[accepted["id"]]
    completed = await runtime.bridge_routes.retrieve_bridge_run(accepted["id"])

    assert completed["status"] == "completed"
    assert completed["output_text"] == "snapshot final unique"
    assert extension.prompt_count == 1


async def test_active_signal_stalled_incomplete_is_native_needs_review(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)

    class StalledExtension(FakeExtension):
        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            browser_target = payload.get("browser_target")
            route = (
                {"target_id": browser_target["id"], "tab_id": 1}
                if isinstance(browser_target, dict)
                else {}
            )
            self.runtime.bridge.dispatch(
                {
                    "type": "incomplete",
                    "id": payload["id"],
                    "event_id": "stalled",
                    "reason": "active_signal_stalled",
                    "metadata": {"completion_signal": "streaming", "output_chars": 0},
                    **route,
                }
            )

    extension = StalledExtension(runtime)
    runtime.bridge.ws = extension

    result = await runtime.bridge_routes.create_bridge_run(
        BridgeRunRequest(input="mission"), request_with_key("active-signal-stalled")
    )

    assert result["status"] == "needs_review"
    assert result["error"]["code"] == "active_signal_stalled"
    assert result["metadata"]["reason"] == "active_signal_stalled"
    assert extension.prompt_count == 1


async def test_empty_done_is_never_reported_as_completed(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)

    class EmptyDoneExtension(FakeExtension):
        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            browser_target = payload.get("browser_target")
            route = (
                {"target_id": browser_target["id"], "tab_id": 1}
                if isinstance(browser_target, dict)
                else {}
            )
            self.runtime.bridge.dispatch(
                {
                    "type": "done",
                    "id": payload["id"],
                    "event_id": "empty",
                    "text": "",
                    "metadata": {"output_chars": 0},
                    **route,
                }
            )

    extension = EmptyDoneExtension(runtime)
    runtime.bridge.ws = extension

    result = await runtime.bridge_routes.create_bridge_run(
        BridgeRunRequest(input="empty final"), request_with_key("empty-done")
    )

    assert result["status"] != "completed"
    assert result["status"] == "needs_review"
    assert result["error"]["code"] == "no_final_answer"
    assert extension.prompt_count == 1


async def test_conversation_binding_precedes_incomplete_and_survives_restart(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    database = tmp_path / "runs.sqlite3"
    isolated_registry(runtime, tmp_path)
    bound = asyncio.Event()
    release = asyncio.Event()
    conversation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    locator = f"https://chatgpt.com/c/{conversation_id}"

    class IncompleteExtension(FakeExtension):
        wrong_conversation = False

        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] == "recovery_capture":
                self.runtime.bridge.dispatch(
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
                "expected_turn_id": None,
                "external_locator": locator,
                "assistant_turns_before": 2,
                "initial_assistant_turn_id": "assistant-before",
                "verified": True,
                "ephemeral": True,
                "verified_at": "2026-08-13T10:00:00.000Z",
            }
            self.runtime.bridge.dispatch(
                {
                    "type": "conversation_bound",
                    "id": payload["id"],
                    "event_id": "1",
                    "conversation": conversation,
                }
            )
            bound.set()
            await release.wait()
            self.runtime.bridge.dispatch(
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

    extension = IncompleteExtension(runtime)
    runtime.bridge.ws = extension
    request = BridgeRunRequest(
        input="mission",
        background=True,
        conversation={"mode": "fresh", "id": conversation_id},
    )
    accepted = await runtime.bridge_routes.create_bridge_run(
        request, request_with_key("incomplete-bound")
    )
    await bound.wait()

    persisted = runtime.bridge_routes.registry.get_by_run_id(accepted["id"])
    assert persisted is not None
    binding = json.loads(persisted["conversation_json"])
    assert binding["external_locator"] == locator
    assert binding["assistant_turns_before"] == 2

    release.set()
    await runtime.bridge_routes.idempotent_tasks[accepted["id"]]
    needs_review = await runtime.bridge_routes.retrieve_bridge_run(accepted["id"])
    assert needs_review["status"] == "needs_review"
    assert needs_review["error"]["code"] == "no_final_answer"

    restarted = RunRegistry(database)
    runtime.bridge_routes.registry = restarted
    preview = await runtime.bridge_routes.preview_visible_recovery(accepted["id"])
    assert preview["turn_id"] == "assistant-later"
    assert preview["text"].startswith("## SUBJECT S1")
    assert extension.prompt_count == 1

    extension.wrong_conversation = True
    with pytest.raises(HTTPException) as mismatched:
        await runtime.bridge_routes.preview_visible_recovery(accepted["id"])
    assert mismatched.value.status_code == 409
    assert extension.prompt_count == 1


async def test_done_rejects_incoherent_output_chars(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)

    class InvalidLengthExtension(FakeExtension):
        async def _respond(self, payload: dict[str, Any]) -> None:
            if payload["type"] != "prompt":
                await super()._respond(payload)
                return
            self.prompt_count += 1
            browser_target = payload.get("browser_target")
            self.runtime.bridge.dispatch(
                {
                    "type": "done",
                    "id": payload["id"],
                    "text": "final",
                    "event_id": "1",
                    "metadata": {"output_chars": 99},
                    "target_id": browser_target["id"],
                    "tab_id": 1,
                }
            )

    runtime.bridge.ws = InvalidLengthExtension(runtime)

    with pytest.raises(HTTPException) as caught:
        await runtime.bridge_routes.create_bridge_run(
            BridgeRunRequest(input="bad length"),
            request_with_key("bad-length"),
        )
    assert caught.value.status_code == 502
    assert "longueur du snapshot final incohérente" in str(caught.value.detail)


async def test_cancelled_http_wait_then_retry_joins_original_run(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)
    extension = FakeExtension(runtime, prompt_delay=0.05)
    runtime.bridge.ws = extension
    req = BridgeRunRequest(input="expensive prompt")
    create_bridge_run = runtime.bridge_routes.create_bridge_run

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


async def test_same_key_different_payload_conflicts(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    isolated_registry(runtime, tmp_path)
    extension = FakeExtension(runtime)
    runtime.bridge.ws = extension
    await runtime.bridge_routes.create_bridge_run(
        BridgeRunRequest(input="one"), request_with_key("conflict")
    )

    with pytest.raises(HTTPException) as caught:
        await runtime.bridge_routes.create_bridge_run(
            BridgeRunRequest(input="two"), request_with_key("conflict")
        )
    assert caught.value.status_code == 409
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail["code"] == "bridge_payload_conflict"


async def test_completed_run_survives_registry_restart_without_ui(
    runtime: BridgeApplication, tmp_path: Path
) -> None:
    database = tmp_path / "runs.sqlite3"
    isolated_registry(runtime, tmp_path)
    extension = FakeExtension(runtime)
    runtime.bridge.ws = extension
    req = BridgeRunRequest(input="once")
    first = await runtime.bridge_routes.create_bridge_run(req, request_with_key("restart"))

    runtime.bridge_routes.registry = RunRegistry(database)
    runtime.bridge.ws = None
    replay = await runtime.bridge_routes.create_bridge_run(req, request_with_key("restart"))

    assert replay["id"] == first["id"]
    assert extension.prompt_count == 1


async def test_capabilities_degrade_visibly_without_a_connected_extension(
    runtime: BridgeApplication,
) -> None:
    runtime.bridge.ws = None

    caps = await runtime.bridge_routes.bridge_capabilities()

    assert caps["streaming"] == "final_delta_only"
    assert caps["web_search"] == "prompt_instructed"
    assert caps["actual_model_version"] is False
    assert caps["controls"]["model_selection"] == "unavailable"
    assert caps["ui"]["available"] is False
    assert caps["ui"]["stale"] is True
    assert caps["ui"]["state"] is None


async def test_capabilities_never_waits_for_a_blocked_extension(
    runtime: BridgeApplication,
) -> None:
    class Blocked:
        async def send_json(self, _: dict[str, Any]) -> None:
            await asyncio.Event().wait()

    runtime.bridge.ws = Blocked()
    started = asyncio.get_running_loop().time()
    caps = await runtime.bridge_routes.bridge_capabilities()
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.25
    assert caps["extension_connected"] is True


async def test_ui_probe_timeout_is_typed(
    runtime: BridgeApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Silent:
        async def send_json(self, _: dict[str, Any]) -> None:
            return None

    runtime.bridge.ws = Silent()
    # `UI_TIMEOUT` is a module-level name inside `bridge.routes_bridge`
    # (import-cached across tests): a bare assignment here would leak
    # UI_TIMEOUT=0.01 into every later test in this process.
    monkeypatch.setitem(
        runtime.bridge_routes.bridge_capabilities.__globals__, "UI_TIMEOUT", 0.01
    )
    with pytest.raises(HTTPException) as caught:
        await runtime.bridge_routes.bridge_capabilities(probe=True, fresh=True)

    assert caught.value.status_code == 504
    assert isinstance(caught.value.detail, dict)
    assert caught.value.detail["code"] == "bridge_ui_timeout"


async def test_bridge_logs_neither_prompt_nor_idempotency_secret(
    runtime: BridgeApplication, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    isolated_registry(runtime, tmp_path)
    runtime.bridge.ws = FakeExtension(runtime)
    caplog.set_level(logging.INFO, logger="chatgpt_bridge")

    await runtime.bridge_routes.create_bridge_run(
        BridgeRunRequest(input="TOP-SECRET-PROMPT"),
        request_with_key("TOP-SECRET-IDEMPOTENCY-KEY"),
    )

    rendered = caplog.text
    assert "TOP-SECRET-PROMPT" not in rendered
    assert "TOP-SECRET-IDEMPOTENCY-KEY" not in rendered
    assert "idempotency_fingerprint=" in rendered
