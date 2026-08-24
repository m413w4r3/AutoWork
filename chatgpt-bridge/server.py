"""
Mini-Bridge : serveur local exposant une API compatible OpenAI, servie par un
onglet chatgpt.com piloté via une extension Chrome.

    [client HTTP] --POST /v1/chat/completions--> [server.py] <--WebSocket--> [extension]

Lancement :  python server.py   (ou  uvicorn server:app --port 8000)
"""

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

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
    WS_TOKEN,
)
from bridge.lifecycle import (
    CleanupWorker,
    ConversationSweeper,
)
from bridge.registry import RunRegistry
from bridge.routes_bridge import BridgeRoutes
from bridge.routes_conversations import ConversationRoutes
from bridge.routes_openai import OpenAIRoutes
from bridge.transport import Bridge

logger = logging.getLogger("chatgpt_bridge")


# --------------------------------------------------------------------------- #
# Cleanup Automation — Incrément 3
# --------------------------------------------------------------------------- #

bridge = Bridge()
run_registry = RunRegistry(RUN_DB_PATH)

accepting_runs = True
cleanup_worker: Optional[CleanupWorker] = None
conversation_sweeper: Optional[ConversationSweeper] = None


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
    tracked = (
        set(bridge_routes.idempotent_tasks.values())
        | set(openai_routes.background_tasks.values())
    )
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

bridge_routes = BridgeRoutes(
    bridge=bridge,
    registry=run_registry,
    openai_routes=openai_routes,
    auth_dependency=require_key,
    ensure_accepting_runs=_ensure_accepting_runs,
)
app.include_router(bridge_routes.router)


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
