"""Lecture/pilotage de l'interface ChatGPT (MOVE-ONLY depuis server.py)."""

import asyncio
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException

from bridge.config import UI_PROBE_TTL, UI_TIMEOUT
from bridge.contracts import (
    BridgeConversationTarget,
    ControlOutcome,
    Outcomes,
    RunControls,
    RunReport,
    UiState,
)
from bridge.transport import Bridge


class UiUnavailable(RuntimeError):
    pass


async def _ui_roundtrip(bridge: Bridge, payload: dict) -> dict:
    if not bridge.online:
        raise UiUnavailable("extension non connectée")
    try:
        packet = await bridge.request(payload, UI_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise UiUnavailable(f"aucune réponse de l'extension après {UI_TIMEOUT:.0f}s") from exc
    except Exception as exc:  # noqa: BLE001 - socket fermé, encodage refusé…
        raise UiUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if packet.get("type") == "error":  # injecté par `_fail_after_grace`
        raise UiUnavailable(str(packet.get("message") or "extension déconnectée"))
    if packet.get("error"):
        raise UiUnavailable(str(packet["error"]))
    return packet


def _ui_state_of(packet: dict) -> Optional[UiState]:
    state = packet.get("state")
    return UiState.model_validate(state) if isinstance(state, dict) else None


async def fetch_ui_state(
    bridge: Bridge,
    probe: bool = False,
    conversation: Optional[BridgeConversationTarget] = None,
) -> UiState:
    """Lit l'état de l'UI. `probe` ouvre les menus pour énumérer les choix."""
    state = _ui_state_of(
        await _ui_roundtrip(
            bridge,
            {
                "type": "ui_state",
                "probe": probe,
                "conversation": conversation.model_dump(mode="json") if conversation else None,
            },
        )
    )
    if state is None:
        raise UiUnavailable("l'extension n'a renvoyé aucun état")
    bridge.last_ui_state = state
    bridge.last_ui_at = time.time()
    return state


_probe_cache: Dict[str, Any] = {"at": 0.0, "state": None}


async def probed_ui_state(bridge: Bridge, fresh: bool = False) -> UiState:
    cached: Optional[UiState] = _probe_cache["state"]
    if not fresh and cached is not None and time.monotonic() - _probe_cache["at"] < UI_PROBE_TTL:
        return cached
    # La sonde manipule l'UI : elle ne doit jamais s'exécuter pendant une génération.
    async with bridge.slot:
        state = await fetch_ui_state(bridge, probe=True)
    _probe_cache.update(at=time.monotonic(), state=state)
    return state


async def apply_controls(
    bridge: Bridge,
    controls: RunControls,
    conversation: Optional[BridgeConversationTarget] = None,
) -> tuple[Outcomes, Optional[UiState]]:
    wanted = controls.wanted()
    if not wanted:
        return {}, await fetch_ui_state(bridge, conversation=conversation)
    packet = await _ui_roundtrip(
        bridge,
        {
            "type": "ui_control",
            "controls": wanted,
            "conversation": conversation.model_dump(mode="json") if conversation else None,
        },
    )
    outcomes = {
        name: ControlOutcome.model_validate(value)
        for name, value in (packet.get("applied") or {}).items()
    }
    return outcomes, _ui_state_of(packet)


def _web_search_mode(controls: RunControls, outcomes: Outcomes) -> str:
    """Comment la recherche web est réellement obtenue pour ce run."""
    outcome = outcomes.get("web_search")
    applied = outcome is not None and outcome.ok
    if controls.web_search is True:
        # L'instruction dans le prompt reste le repli : elle demande à ChatGPT
        # de chercher, sans garantie que l'outil soit actif.
        return "ui_tool" if applied else "prompt_instructed"
    if controls.web_search is False:
        return "off" if applied else "off_unverified"
    return "untouched"


async def prepare_run(
    bridge: Bridge,
    controls: RunControls,
    *,
    allow_unverified_model: bool,
    conversation: Optional[BridgeConversationTarget] = None,
) -> RunReport:
    """Applique les contrôles avant la génération, à l'intérieur du slot.

    Un contrôle explicitement demandé et non vérifié fait échouer le run : dans
    une chaîne CTI, un run attribué au mauvais modèle est pire qu'un run manquant.
    """
    try:
        outcomes, state = (
            await apply_controls(bridge, controls, conversation)
            if conversation is not None
            else await apply_controls(bridge, controls)
        )
    except UiUnavailable as exc:
        # Modèle et profil sont des exigences : sans pilotage de l'UI, le run
        # n'a pas lieu. La recherche web, elle, a un repli par le prompt, et
        # l'état seul n'est qu'un bonus d'observabilité.
        if controls.model or controls.profile:
            raise HTTPException(
                status_code=502, detail=f"Contrôles d'interface inapplicables : {exc}"
            ) from exc
        outcomes, state = {}, None

    for nom, demande in (("profile", controls.profile), ("model", controls.model)):
        outcome = outcomes.get(nom)
        if outcome is None or outcome.ok:
            continue
        if nom == "model" and allow_unverified_model:
            continue
        raise HTTPException(
            status_code=409,
            detail=f"Réglage « {nom} » = « {demande} » non appliqué dans l'UI : {outcome.reason}",
        )

    if any(o.changed for o in outcomes.values()):
        _probe_cache.update(at=0.0, state=None)  # la sonde en cache est périmée

    observed = state.model.selected if state and state.model.verified else None
    return RunReport(
        model_observed=observed,
        model_source="ui_observed" if observed else "unknown",
        web_search_mode=_web_search_mode(controls, outcomes),
        controls=outcomes,
    )


def cached_probe() -> Optional[UiState]:
    state: Optional[UiState] = _probe_cache["state"]
    if state is None or time.monotonic() - _probe_cache["at"] >= UI_PROBE_TTL:
        return None
    return state


def invalidate_probe_cache() -> None:
    _probe_cache.update(at=0.0, state=None)
