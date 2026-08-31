"""Tests for `bridge/generation.py`: idle vs. total timeout semantics.

`run_generation` distinguishes two deadlines that must never be confused: the
idle timeout (the extension stopped sending anything) and the total timeout
(the extension is alive but the generation ran too long). Confusing the two
has already caused a live extension sending heartbeats to be misdiagnosed as
disconnected.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import request_with_key

from bridge.app import BridgeApplication
from bridge.contracts import ChatRequest
from bridge.generation import UpstreamError, run_generation


class SilentExtension:
    """Extension muette : reçoit le prompt mais ne redispatche jamais rien."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)


class TypedErrorExtension:
    """Extension-side typed failure used to verify the submission boundary."""

    def __init__(self, runtime: BridgeApplication) -> None:
        self.runtime = runtime

    async def send_json(self, payload: dict[str, Any]) -> None:
        if payload.get("type") == "prompt":
            self.runtime.bridge.dispatch(
                {
                    "type": "error",
                    "id": payload["id"],
                    "event_id": "typed-error",
                    "code": "bridge_ui_timeout",
                    "message": "confirmation impossible",
                    "retryable": True,
                    "phase": "submission_confirmation",
                    "submission_state": "submission_attempted",
                    "diagnostics": {
                        "composer_text": "secret prompt must not escape",
                        "user_turns_before": 2,
                    },
                }
            )

    async def close(self, code: int, reason: str) -> None:
        del code, reason


async def test_idle_timeout_does_not_send_abort_to_extension(
    runtime: BridgeApplication,
) -> None:
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
    silent = SilentExtension()
    runtime.bridge.ws = silent

    chat_request = ChatRequest(messages=[{"role": "user", "content": "test"}])

    async def never_disconnects() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    http_req = request_with_key("timeout-test")
    http_req._receive = never_disconnects

    old_idle = run_generation.__globals__["IDLE_TIMEOUT"]
    run_generation.__globals__["IDLE_TIMEOUT"] = 0.1
    try:
        with pytest.raises(Exception) as caught:
            async for _ in run_generation(
                runtime.bridge, runtime.registry, "timeout-test", chat_request, http_req
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
        runtime: BridgeApplication,
        *,
        interval: float,
        done_after: float | None = None,
    ) -> None:
        self.runtime = runtime
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
                self.runtime.bridge.dispatch(
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
            self.runtime.bridge.dispatch(
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
    runtime: BridgeApplication,
    extension: Any,
    request_id: str,
    *,
    total_timeout: float,
    idle_timeout: float,
) -> tuple[list[str], Exception | None, float]:
    """Exécute une génération complète avec des échéances accélérées."""
    runtime.bridge.ws = extension
    chat_request = ChatRequest(messages=[{"role": "user", "content": "recherche"}])

    async def never_disconnects() -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    http_req = request_with_key(request_id)
    http_req._receive = never_disconnects

    globals_ = run_generation.__globals__
    previous = (globals_["TOTAL_TIMEOUT"], globals_["IDLE_TIMEOUT"])
    globals_["TOTAL_TIMEOUT"] = total_timeout
    globals_["IDLE_TIMEOUT"] = idle_timeout
    chunks: list[str] = []
    failure: Exception | None = None
    started = asyncio.get_running_loop().time()
    try:
        async for text in run_generation(
            runtime.bridge, runtime.registry, request_id, chat_request, http_req
        ):
            chunks.append(text)
    except Exception as exc:  # noqa: BLE001 - le test inspecte lui-même le type et le code
        failure = exc
    finally:
        elapsed = asyncio.get_running_loop().time() - started
        globals_["TOTAL_TIMEOUT"], globals_["IDLE_TIMEOUT"] = previous
        if hasattr(extension, "stop"):
            extension.stop()
    return chunks, failure, elapsed


async def test_typed_extension_error_preserves_submission_state_and_safe_diagnostics(
    runtime: BridgeApplication,
) -> None:
    _, failure, _ = await _generate(
        runtime,
        TypedErrorExtension(runtime),
        "typed-error",
        total_timeout=1.0,
        idle_timeout=0.2,
    )

    assert isinstance(failure, UpstreamError)
    assert failure.code == "bridge_ui_timeout"
    assert failure.retryable is True
    assert failure.phase == "submission_confirmation"
    assert failure.submission_state == "submission_attempted"
    assert "composer_text" not in failure.details
    assert failure.details["user_turns_before"] == 2


async def test_live_generation_reaching_the_total_deadline_is_never_called_idle(
    runtime: BridgeApplication,
) -> None:
    """Le bug du 17/08 : 900 s pile, heartbeats reçus, message « aucune donnée ».

    `asyncio.wait_for` expirait sur la borne totale, mais la branche d'erreur
    accusait systématiquement l'extension d'être muette.
    """
    extension = HeartbeatingExtension(runtime, interval=0.05)

    _, failure, elapsed = await _generate(
        runtime, extension, "total-timeout", total_timeout=0.5, idle_timeout=0.15
    )

    assert isinstance(failure, UpstreamError)
    assert failure.code == "bridge_total_timeout"
    assert "génération non terminée après" in str(failure)
    assert "aucune donnée de l'extension" not in str(failure)
    # La borne totale, et elle seule, a mis fin au run : les heartbeats
    # réarmaient l'attente d'inactivité à chaque paquet.
    assert elapsed >= 0.5
    assert extension.beats >= 3


async def test_silent_extension_is_reported_as_idle_well_before_the_total_deadline(
    runtime: BridgeApplication,
) -> None:
    _, failure, elapsed = await _generate(
        runtime, SilentExtension(), "idle-timeout", total_timeout=2.0, idle_timeout=0.2
    )

    assert isinstance(failure, UpstreamError)
    assert failure.code == "bridge_idle_timeout"
    assert "aucune donnée de l'extension depuis" in str(failure)
    assert elapsed < 1.0


async def test_long_but_live_generation_completes_before_the_total_deadline(
    runtime: BridgeApplication,
) -> None:
    extension = HeartbeatingExtension(runtime, interval=0.05, done_after=0.6)

    chunks, failure, _ = await _generate(
        runtime, extension, "live-completion", total_timeout=1.5, idle_timeout=0.15
    )

    assert failure is None
    assert "".join(chunks) == "rapport final"
    # Le run a duré bien plus longtemps que l'idle timeout sans jamais expirer.
    assert extension.beats >= 3
