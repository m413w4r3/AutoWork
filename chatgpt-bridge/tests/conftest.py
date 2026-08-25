"""Shared fixtures and helpers for the bridge test suite.

Every test here imports `bridge.*` directly (this file puts the package root
on `sys.path`), then builds its own `BridgeApplication()` — the cheap
composition root `server.py` also uses — for per-test isolation. There is no
need to reload `server.py` through `runpy`: `bridge.app`, `bridge.ui`, and
`bridge.generation` are import-cached across tests like any other module, and
`BridgeApplication()` already builds fresh `Bridge`/`RunRegistry`/route
instances each call.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parent.parent))

from bridge.app import BridgeApplication
from bridge.registry import RunRegistry


@pytest.fixture
def runtime() -> BridgeApplication:
    return BridgeApplication()


class FakeExtension:
    """Simule l'extension côté WebSocket : répond aux `ui_control`/`ui_state`
    et `prompt` avec un `done` immédiat (ou après `prompt_delay`)."""

    def __init__(self, runtime: BridgeApplication, *, prompt_delay: float = 0) -> None:
        self.runtime = runtime
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
            self.runtime.bridge.dispatch(
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
            self.runtime.bridge.dispatch(
                {"type": "heartbeat", "id": payload["id"], "event_id": "1"}
            )
            target = payload.get("conversation")
            conversation = None
            if target:
                if target["mode"] == "fresh":
                    self.locators[target["id"]] = f"https://chatgpt.com/simulated/{target['id']}"
                elif self.locators.get(target["id"]) != target.get("external_locator"):
                    self.runtime.bridge.dispatch(
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
            self.runtime.bridge.dispatch(
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


def isolated_registry(runtime: BridgeApplication, tmp_path: Path) -> None:
    """`create_bridge_run`/`retrieve_bridge_run` read `self.registry`, bound
    once at construction: every real owner of a registry reference needs
    patching directly."""
    registry = RunRegistry(tmp_path / "runs.sqlite3")
    runtime.registry = registry
    runtime.openai_routes.registry = registry
    runtime.bridge_routes.registry = registry
    runtime.conversation_routes.registry = registry
