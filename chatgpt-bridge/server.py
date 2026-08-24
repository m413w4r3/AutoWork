"""
Mini-Bridge : serveur local exposant une API compatible OpenAI, servie par un
onglet chatgpt.com piloté via une extension Chrome.

    [client HTTP] --POST /v1/chat/completions--> [server.py] <--WebSocket--> [extension]

Lancement :  python server.py   (ou  uvicorn server:app --port 8000)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from bridge.config import (
    API_KEY,
    HOST,
    KEEPALIVE_INTERVAL,
    PORT,
    RUN_CLEANUP_LIMIT,
    RUN_DB_PATH,
    RUN_RETENTION_SECONDS,
    SHUTDOWN_GRACE_SECONDS,
    UI_SNAPSHOT_STALE,
    UI_TIMEOUT,
    WS_TOKEN,
)
from bridge.contracts import (
    MODELES_NEUTRES,
    BridgeRunRequest,
    ResponseRequest,
    RunControls,
    UiState,
)
from bridge.generation import (
    NeedsReviewError,
    _BackgroundRequest,
    generation_progress,
)
from bridge.lifecycle import (
    CleanupWorker,
    ConversationSweeper,
)
from bridge.registry import RunRegistry
from bridge.routes_conversations import ConversationRoutes
from bridge.routes_openai import OpenAIRoutes
from bridge.transport import Bridge
from bridge.ui import (
    UiUnavailable,
    apply_controls,
    fetch_ui_state,
    invalidate_probe_cache,
    probed_ui_state,
)

logger = logging.getLogger("chatgpt_bridge")


def _bridge_controls(req: BridgeRunRequest) -> RunControls:
    modele = (req.ui_model or "").strip()
    return RunControls(
        model=None if modele.lower() in MODELES_NEUTRES else modele,
        profile=req.profile,
        # `False` est volontaire : sans lui, une recherche web laissée active
        # dans l'UI polluerait tous les runs suivants à l'insu de l'appelant.
        web_search=req.web_search,
    )


def _bridge_response_request(req: BridgeRunRequest) -> ResponseRequest:
    # `reasoning_effort` est conservé dans le contrat pour l'observabilité, mais
    # l'interface web ne permet pas encore de sélectionner ce réglage de façon fiable.
    return ResponseRequest(
        model=req.requested_model,
        input=req.input,
        tools=[{"type": "web_search"}] if req.web_search else [],
        text={"format": req.response_format} if req.response_format else None,
        background=req.background,
        conversation=req.conversation,
        bridge_recovery=req.recovery,
    )


# --------------------------------------------------------------------------- #
# Cleanup Automation — Incrément 3
# --------------------------------------------------------------------------- #

bridge = Bridge()
run_registry = RunRegistry(RUN_DB_PATH)
idempotent_tasks: Dict[str, asyncio.Task] = {}
bridge_metrics: Dict[str, int] = {
    "runs_started": 0,
    "runs_completed": 0,
    "runs_failed": 0,
    "deduplication_hits": 0,
    "payload_conflicts": 0,
    "ui_timeouts": 0,
}

accepting_runs = True
cleanup_worker: Optional[CleanupWorker] = None
conversation_sweeper: Optional[ConversationSweeper] = None


def _consume_task_exception(task: asyncio.Task) -> None:
    """Observe detached failures; the typed result already lives in SQLite."""
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def keepalive_loop() -> None:
    """Ping périodique : réveille le service worker MV3 et détecte les sockets morts."""
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        if bridge.ws is not None:
            try:
                await bridge.send({"type": "ping", "t": time.time()})
            except Exception:
                pass


def _configuration_state() -> dict[str, Any]:
    local_only = HOST in {"127.0.0.1", "localhost", "::1"}
    http_configured = bool(API_KEY)
    websocket_configured = bool(WS_TOKEN)
    return {
        "complete": websocket_configured and (http_configured or local_only),
        "http_auth": "configured" if http_configured else "absent",
        "http_auth_required": not local_only,
        "websocket_token": "configured" if websocket_configured else "absent",
    }


def _shutdown_error() -> dict[str, Any]:
    return {
        "status_code": 503,
        "body": {
            "error": {
                "code": "bridge_server_error",
                "message": "Le bridge a interrompu cette exécution pendant son arrêt.",
                "retryable": True,
            }
        },
    }


def _ensure_accepting_runs() -> None:
    if not accepting_runs:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "bridge_server_error",
                "message": "Le bridge est en cours d'arrêt et n'accepte plus de nouveaux runs.",
                "retryable": True,
            },
        )


async def shutdown_bridge(grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> None:
    """Draine les runs natifs, puis annule prudemment ce qui reste."""
    global accepting_runs
    accepting_runs = False
    tracked = set(idempotent_tasks.values()) | set(openai_routes.background_tasks.values())
    logger.info(
        "bridge_shutdown_started grace_seconds=%s active_runs=%s extension=%s",
        grace_seconds,
        len(tracked),
        "connected" if bridge.online else "disconnected",
    )
    pending = tracked
    if tracked and grace_seconds > 0:
        _, pending = await asyncio.wait(tracked, timeout=grace_seconds)
    await bridge.close()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    run_registry.checkpoint_and_close()
    logger.info("bridge_shutdown_completed cancelled_runs=%s", len(pending))


@asynccontextmanager
async def lifespan(app: FastAPI):
    global accepting_runs, cleanup_worker, conversation_sweeper
    accepting_runs = True
    bridge.closing = False
    task = asyncio.create_task(keepalive_loop())
    run_registry.recover_interrupted()
    run_registry.cleanup()

    # Initialiser le cleanup worker et le sweeper
    cleanup_worker = CleanupWorker(run_registry, bridge)
    conversation_sweeper = ConversationSweeper(run_registry, cleanup_worker)

    # Lancer le sweep initial (reprendre après restart)
    try:
        await conversation_sweeper.sweep()
    except Exception as e:
        logger.error(f"Initial cleanup sweep failed: {e}", exc_info=True)

    # Lancer la tâche de retry périodique
    sweep_task = asyncio.create_task(_periodic_cleanup_retry(conversation_sweeper))

    configuration = _configuration_state()
    registry_state = "accessible" if run_registry.accessible() else "unavailable"
    logger.info(
        "bridge_started host=%s port=%s http_auth=%s websocket_token=%s "
        "sqlite_registry=%s extension=disconnected cleanup_worker=active",
        HOST,
        PORT,
        configuration["http_auth"],
        configuration["websocket_token"],
        registry_state,
    )
    try:
        yield
    finally:
        task.cancel()
        sweep_task.cancel()
        await asyncio.gather(task, sweep_task, return_exceptions=True)
        await shutdown_bridge()


async def _periodic_cleanup_retry(sweeper: ConversationSweeper):
    """Retry périodique des CLEANUP_FAILED."""
    while True:
        try:
            await asyncio.sleep(300)  # Chaque 5 minutes
            await sweeper.retry_failed()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Periodic cleanup retry error: {e}", exc_info=True)


app = FastAPI(title="ChatGPT Mini-Bridge", version="1.0.0", lifespan=lifespan)

_bearer = HTTPBearer(auto_error=False)


async def require_key(cred: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> None:
    if not API_KEY and HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "bridge_auth_failed",
                "message": "BRIDGE_API_KEY est obligatoire sur une écoute non locale.",
                "retryable": False,
            },
        )
    if API_KEY and (cred is None or cred.credentials != API_KEY):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "bridge_auth_failed",
                "message": "Clé API invalide.",
                "retryable": False,
            },
        )


conversation_routes = ConversationRoutes(
    bridge=bridge,
    registry=run_registry,
    auth_dependency=require_key,
)
app.include_router(conversation_routes.router)

openai_routes = OpenAIRoutes(
    bridge=bridge,
    registry=run_registry,
    auth_dependency=require_key,
    ensure_accepting_runs=_ensure_accepting_runs,
)
app.include_router(openai_routes.router)


# --------------------------------------------------------------------------- #
# WebSocket extension
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    if not accepting_runs or bridge.closing:
        await ws.close(code=1013, reason="server shutdown")
        return
    supplied = ws.query_params.get("token")
    if not WS_TOKEN or not supplied or not hmac.compare_digest(supplied, WS_TOKEN):
        # Fermeture avant acceptation : l'extension ne peut envoyer aucun paquet.
        await ws.close(code=4401, reason="authentication required")
        logger.warning("websocket_auth_failed")
        return
    await ws.accept()
    await bridge.attach(ws)
    logger.info("extension_connected reconnections=%s", bridge.reconnections)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                packet = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = packet.get("type")
            if kind == "pong":
                continue
            if kind == "hello":
                bridge.client_name = str(packet.get("client", "inconnu"))
                logger.info("extension_identified client=%s", bridge.client_name[:64])
                continue
            bridge.dispatch(packet)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - on ne veut jamais tuer le serveur
        logger.exception("websocket_failure")
    finally:
        bridge.detach(ws)


@app.post("/v1/bridge/runs", dependencies=[Depends(require_key)])
async def create_bridge_run(req: BridgeRunRequest, http_req: Request):
    _ensure_accepting_runs()
    header_key = http_req.headers.get("X-Idempotency-Key")
    if header_key and req.request_id and header_key != req.request_id:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "bridge_payload_conflict",
                "message": "L'en-tête et request_id ne concordent pas.",
                "retryable": False,
            },
        )
    key = header_key or req.request_id or f"non_retryable_{uuid.uuid4().hex}"
    canonical = req.model_dump(mode="json", exclude={"request_id"})
    request_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    record, created = run_registry.claim(key, request_hash)
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:12]
    correlation_id = http_req.headers.get("X-Correlation-ID", "-")[:128]
    run_id = str(record["bridge_run_id"])
    if record["request_hash"] != request_hash:
        bridge_metrics["payload_conflicts"] += 1
        logger.warning(
            "bridge_payload_conflict bridge_run_id=%s idempotency_fingerprint=%s",
            run_id,
            fingerprint,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "bridge_payload_conflict",
                "message": "Cette clé d'idempotence désigne un autre payload.",
                "retryable": False,
            },
        )

    if not created:
        bridge_metrics["deduplication_hits"] += 1
        logger.info(
            "bridge_run_deduplicated bridge_run_id=%s idempotency_fingerprint=%s "
            "state=%s deduplication_hit=true",
            run_id,
            fingerprint,
            record["state"],
        )
        if record["state"] == "completed" and record["response_json"]:
            return json.loads(record["response_json"])
        if record["state"] == "needs_review" and record["error_json"]:
            return json.loads(record["error_json"])
        if record["state"] == "failed" and record["error_json"]:
            stored = json.loads(record["error_json"])
            return JSONResponse(status_code=stored["status_code"], content=stored["body"])

    async def execute_once() -> dict:
        started = time.monotonic()
        bridge_metrics["runs_started"] += 1
        run_registry.set_state(key, "running")
        logger.info(
            "bridge_run_started bridge_run_id=%s correlation_id=%s idempotency_fingerprint=%s phase=waiting_extension",
            run_id,
            correlation_id,
            fingerprint,
        )
        try:
            response = await openai_routes.create_response_internal(
                # Le mode background du contrat natif est géré par ce registre
                # SQLite. La façade Responses interne doit donc exécuter une
                # seule génération synchrone dans cette tâche détachée.
                _bridge_response_request(req).model_copy(update={"background": False}),
                _BackgroundRequest(),
                _bridge_controls(req),
                allow_unverified_model=req.allow_unverified_model,
                response_id=run_id,
            )
            run_registry.set_state(key, "completed", response)
            bridge_metrics["runs_completed"] += 1
            logger.info(
                "bridge_run_completed bridge_run_id=%s correlation_id=%s idempotency_fingerprint=%s "
                "duration_ms=%s phase=completed",
                run_id,
                correlation_id,
                fingerprint,
                int((time.monotonic() - started) * 1000),
            )
            return response
        except NeedsReviewError as exc:
            body = {
                "id": run_id,
                "object": "response",
                "status": "needs_review",
                "error": {
                    "code": exc.reason,
                    "message": "ChatGPT s'est arrêté sans réponse finale.",
                },
                "metadata": exc.details,
            }
            run_registry.set_state(key, "needs_review", body)
            logger.warning(
                "bridge_run_needs_review bridge_run_id=%s correlation_id=%s reason=%s",
                run_id,
                correlation_id,
                exc.reason,
            )
            return body
        except asyncio.CancelledError:
            stored = _shutdown_error()
            run_registry.set_state(key, "failed", stored)
            bridge_metrics["runs_failed"] += 1
            logger.warning(
                "bridge_run_interrupted bridge_run_id=%s correlation_id=%s "
                "idempotency_fingerprint=%s phase=shutdown",
                run_id,
                correlation_id,
                fingerprint,
            )
            raise
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {
                "code": "bridge_server_error",
                "message": str(exc.detail),
                "retryable": exc.status_code in {408, 429, 502, 503, 504},
            }
            stored = {"status_code": exc.status_code, "body": {"error": detail}}
            run_registry.set_state(key, "failed", stored)
            bridge_metrics["runs_failed"] += 1
            raise
        except Exception as exc:
            logger.exception("bridge_run_unexpected_failure bridge_run_id=%s", run_id)
            stored = {
                "status_code": 500,
                "body": {
                    "error": {
                        "code": "bridge_server_error",
                        "message": "La génération via le bridge a échoué.",
                        "retryable": True,
                    }
                },
            }
            run_registry.set_state(key, "failed", stored)
            bridge_metrics["runs_failed"] += 1
            raise HTTPException(status_code=500, detail=stored["body"]["error"]) from exc
        finally:
            idempotent_tasks.pop(run_id, None)

    task = idempotent_tasks.get(run_id)
    if task is None:
        # Une déconnexion HTTP ne doit ni annuler ni resoumettre un clic coûteux.
        task = asyncio.create_task(execute_once())
        idempotent_tasks[run_id] = task
        task.add_done_callback(_consume_task_exception)
    if req.background:
        # Le client reprend exclusivement par GET. Le résultat final sera écrit
        # par execute_once dans SQLite, même si cette requête HTTP disparaît.
        current = run_registry.get_by_run_id(run_id) or record
        return {
            "id": run_id,
            "object": "response",
            "status": current["state"],
        }
    return await asyncio.shield(task)


@app.get("/v1/bridge/runs/{response_id}", dependencies=[Depends(require_key)])
async def retrieve_bridge_run(response_id: str):
    record = run_registry.get_by_run_id(response_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run bridge inconnu ou expiré")
    if record["state"] == "completed" and record["response_json"]:
        return json.loads(record["response_json"])
    if record["state"] == "needs_review" and record["error_json"]:
        return json.loads(record["error_json"])
    if record["state"] == "failed" and record["error_json"]:
        stored = json.loads(record["error_json"])
        return JSONResponse(status_code=stored["status_code"], content=stored["body"])
    return {
        "id": response_id,
        "object": "response",
        "status": record["state"],
        "metadata": {
            "bridge_progress": generation_progress(response_id),
        },
    }


@app.post(
    "/v1/bridge/runs/{response_id}/recovery/visible",
    dependencies=[Depends(require_key)],
)
async def preview_visible_recovery(response_id: str):
    record = run_registry.get_by_run_id(response_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run bridge inconnu")
    if (
        record["state"] not in {
            "running",
            "needs_review",
            "completed",
            "failed",
        }
        or not record.get("conversation_json")
    ):
        raise HTTPException(status_code=409, detail="Run non récupérable")

    conversation = json.loads(record["conversation_json"])
    packet = await bridge.request(
        {
            "type": "recovery_capture",
            "conversation": conversation,
        },
        timeout=UI_TIMEOUT,
    )
    if packet.get("error"):
        raise HTTPException(
            status_code=404,
            detail={
                "code": "recovery_answer_unavailable",
                "message": str(packet["error"]),
            },
        )
    if (
        packet.get("conversation_id") != conversation.get("id")
        or packet.get("external_locator") != conversation.get("external_locator")
    ):
        raise HTTPException(status_code=409, detail="Conversation de récupération incohérente")
    text = packet.get("text")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=404, detail="Aucune réponse finale récupérable")
    preview = {
        "bridge_run_id": response_id,
        "conversation_id": conversation["id"],
        "external_locator": conversation["external_locator"],
        "turn_id": packet.get("turn_id"),
        "text": text,
        "metadata": packet.get("metadata") if isinstance(packet.get("metadata"), dict) else {},
    }
    run_registry.store_preview(response_id, preview)
    return preview


@app.get("/v1/bridge/ui", dependencies=[Depends(require_key)])
async def bridge_ui_state(probe: bool = False, fresh: bool = False):
    """État pilotable de l'onglet ChatGPT, tel que le content script le relit.

    `probe=true` ouvre les menus pour énumérer modèles et profils : c'est visible
    à l'écran, et la génération en cours est attendue avant de le faire.
    """
    try:
        state = await (probed_ui_state(bridge, fresh) if probe else fetch_ui_state(bridge))
    except UiUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return state.model_dump()


@app.post("/v1/bridge/ui/controls", dependencies=[Depends(require_key)])
async def bridge_ui_controls(controls: RunControls):
    """Applique des réglages hors run (ex. fixer le profil une fois pour toutes)."""
    if not controls.wanted():
        raise HTTPException(status_code=422, detail="Aucun contrôle demandé")
    async with bridge.slot:
        try:
            outcomes, state = await apply_controls(bridge, controls)
        except UiUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    # La sonde en cache décrit un état désormais périmé.
    invalidate_probe_cache()
    return {
        "ok": all(o.ok for o in outcomes.values()),
        "applied": {name: o.model_dump() for name, o in outcomes.items()},
        "state": state.model_dump() if state else None,
    }


@app.get("/v1/bridge/capabilities", dependencies=[Depends(require_key)])
async def bridge_capabilities(probe: bool = False, fresh: bool = False):
    """Capacités réelles, y compris l'état vérifié des contrôles d'interface.

    Sans `probe`, l'état est lu sans toucher à l'UI : le modèle sélectionné est
    connu, mais pas la liste des modèles disponibles.
    """
    if probe:
        try:
            state = await probed_ui_state(bridge, fresh)
        except UiUnavailable as exc:
            code = "bridge_ui_timeout" if "après" in str(exc) else "bridge_extension_disconnected"
            if code == "bridge_ui_timeout":
                bridge_metrics["ui_timeouts"] += 1
            raise HTTPException(
                status_code=504 if code == "bridge_ui_timeout" else 503,
                detail={"code": code, "message": str(exc), "retryable": True},
            ) from exc
    else:
        # Chemin critique : strictement aucun aller-retour WebSocket/DOM.
        state = bridge.last_ui_state

    observed_at = bridge.last_ui_at or (state.observed_at if state else None)
    age = max(0.0, time.time() - observed_at) if observed_at else None
    stale = age is None or age > UI_SNAPSHOT_STALE

    model_ok = bool(state and state.model.supported and state.model.verified)
    search_ok = bool(state and state.web_search.supported and state.web_search.verified)
    return {
        "transport": "chatgpt_web_ui",
        "extension_connected": bridge.online,
        "serialization": "single_request",
        "text": True,
        "new_chat": True,
        "web_search": "ui_toggle" if search_ok else "prompt_instructed",
        "structured_output": "prompt_and_client_validation",
        "background": "asynchronous_durable_result",
        "streaming": "final_delta_only",
        # Vrai seulement quand le libellé du sélecteur a pu être relu : c'est le
        # modèle *affiché* par l'UI, pas le snapshot exact servi par OpenAI.
        "actual_model_version": model_ok,
        "native_usage": False,
        "binary_allowed_for_cti_gateway": False,
        "controls": {
            "model_selection": "verified" if model_ok else "unavailable",
            "profile_selection": (
                "verified"
                if state and state.profile.supported and state.profile.verified
                else "unavailable"
            ),
            "web_search_toggle": "verified" if search_ok else "unavailable",
            "reasoning_effort": "unavailable",
            "verification": "dom_readback",
        },
        "ui": {
            "available": state is not None,
            "state": state.model_dump() if state else None,
            "observed_at": observed_at,
            "age_seconds": age,
            "stale": stale,
            "reason": None if state else "snapshot indisponible",
        },
    }


@app.get("/v1/bridge/metrics", dependencies=[Depends(require_key)])
async def bridge_operational_metrics():
    """Compteurs bornés, sans labels issus des prompts ou des secrets."""
    return {
        **bridge_metrics,
        "websocket_reconnections": bridge.reconnections,
        "active_runs": len(idempotent_tasks),
        "extension_connected": bridge.online,
        "busy": bridge.slot.locked(),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "extension_connected": bridge.online,
        "client": bridge.client_name if bridge.online else None,
        "busy": bridge.slot.locked(),
        "connected_since": bridge.connected_at,
    }


@app.get("/ready")
async def ready():
    """Disponibilité fonctionnelle, distincte de la liveness `/health`."""
    configuration = _configuration_state()
    registry_accessible = run_registry.accessible()
    if not registry_accessible:
        status = "server_unavailable"
    elif not configuration["complete"]:
        status = "configuration_incomplete"
    elif not bridge.online:
        status = "extension_absent"
    else:
        status = "extension_available"
    body = {
        "status": status,
        "server_operational": registry_accessible and accepting_runs,
        "accepting_runs": accepting_runs,
        "configuration": configuration,
        "sqlite_registry": "accessible" if registry_accessible else "unavailable",
        "extension": "connected" if bridge.online else "disconnected",
    }
    return JSONResponse(
        status_code=200 if status == "extension_available" else 503,
        content=body,
    )


@app.exception_handler(HTTPException)
async def openai_error(_: Request, exc: HTTPException):
    """Erreurs au format OpenAI, pour que les SDK clients les comprennent."""
    if isinstance(exc.detail, dict):
        error = exc.detail
    else:
        error = {
            "message": str(exc.detail),
            "type": "bridge_error",
            "code": "bridge_server_error",
            "retryable": exc.status_code in {408, 429, 502, 503, 504},
        }
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
    )


if __name__ == "__main__":
    import uvicorn

    # Les logs protocolaires INFO d'Uvicorn incluent l'URL du handshake et donc
    # le token WebSocket en query string. Les événements applicatifs sûrs
    # conservent leur propre handler INFO ; Uvicorn reste visible à WARNING.
    bridge_handler = logging.StreamHandler()
    bridge_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logger.addHandler(bridge_handler)
    logger.setLevel(
        getattr(logging, os.getenv("BRIDGE_LOG_LEVEL", "INFO").upper(), logging.INFO)
    )
    logger.propagate = False
    # Uvicorn libère rapidement les handlers HTTP ; les tâches idempotentes,
    # protégées par shield, sont drainées par `shutdown_bridge` pendant le délai
    # applicatif ci-dessus.
    # Le token WebSocket est transporté dans la query string par l'extension :
    # désactiver l'access log évite que Uvicorn ne l'imprime lors du handshake.
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=1,
    )
