"""Routes OpenAI: responses, chat completions, models.

Encapsule les endpoints OpenAI sous un propriétaire explicite.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from bridge.contracts import ChatRequest, ResponseRequest, RunControls
from bridge.generation import (
    NeedsReviewError,
    UpstreamError,
    _BackgroundRequest,
    _response_body,
    _response_chat_request,
    _tokens,
    completion_body,
    parse_messages,
    run_generation,
    sse_chunk,
)
from bridge.registry import RunRegistry
from bridge.transport import Bridge
from bridge.ui import (
    UiUnavailable,
    cached_probe,
    fetch_ui_state,
    prepare_run,
    probed_ui_state,
)

logger = logging.getLogger("chatgpt_bridge")


class OpenAIRoutes:
    """Propriétaire des quatre endpoints compatibles OpenAI.

    Ne détient aucun état métier propre au delà du cache local des réponses de
    fond : `bridge` et `registry` sont des instances injectées par
    BridgeApplication, `router` est l'APIRouter à monter sur l'application
    FastAPI.
    """

    def __init__(
        self,
        *,
        bridge: Bridge,
        registry: RunRegistry,
        auth_dependency: Callable[..., Any],
        ensure_accepting_runs: Callable[[], None],
    ) -> None:
        self.bridge = bridge
        self.registry = registry
        self.ensure_accepting_runs = ensure_accepting_runs
        # Les réponses de fond sont un cache de transport local, pas un état
        # canonique. PostgreSQL côté application conserve l'identité et le
        # statut du ModelRun.
        self.background_responses: Dict[str, dict] = {}
        self.background_tasks: Dict[str, asyncio.Task] = {}
        self.router = APIRouter(dependencies=[Depends(auth_dependency)])

        self.router.add_api_route(
            "/v1/responses",
            self.create_response,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/v1/responses/{response_id}",
            self.retrieve_response,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/v1/chat/completions",
            self.chat_completions,
            methods=["POST"],
        )
        self.router.add_api_route(
            "/v1/models",
            self.list_models,
            methods=["GET"],
        )

    async def _execute_background_response(
        self,
        response_id: str,
        req: ResponseRequest,
        controls: RunControls,
        allow_unverified_model: bool,
    ) -> None:
        self.background_responses[response_id] = _response_body(
            response_id, req, status="in_progress"
        )
        try:
            async with self.bridge.slot:
                report = await prepare_run(
                    self.bridge,
                    controls,
                    allow_unverified_model=allow_unverified_model,
                    conversation=req.conversation,
                )
                chat_request = _response_chat_request(req)
                conversation_result: dict = {}
                extension_metadata: dict = {}
                parts = [
                    text
                    async for text in run_generation(
                        self.bridge,
                        self.registry,
                        response_id,
                        chat_request,
                        _BackgroundRequest(),
                        conversation=req.conversation,
                        conversation_result=conversation_result,
                        extension_metadata=extension_metadata,
                    )
                ]
            self.background_responses[response_id] = _response_body(
                response_id,
                req,
                status="completed",
                output_text="".join(parts),
                run=report,
                conversation_result=conversation_result or None,
                extension_metadata=extension_metadata or None,
            )
        except HTTPException as exc:
            # Un contrôle d'interface refusé est un diagnostic actionnable, pas
            # une fuite : on le rend tel quel, contrairement aux erreurs de
            # génération.
            self.background_responses[response_id] = _response_body(
                response_id, req, status="failed", error=str(exc.detail)
            )
            print(f"⚠️  Réponse de fond {response_id} refusée : {exc.detail}")
        except Exception as exc:  # noqa: BLE001 - erreur publique nettoyée ci-dessous
            self.background_responses[response_id] = _response_body(
                response_id,
                req,
                status="failed",
                error="La génération via le bridge a échoué.",
            )
            print(f"⚠️  Réponse de fond {response_id} en échec : {type(exc).__name__}")
        finally:
            self.background_tasks.pop(response_id, None)

    async def create_response_internal(
        self,
        req: ResponseRequest,
        http_req: Request,
        controls: Optional[RunControls] = None,
        *,
        allow_unverified_model: bool = False,
        response_id: Optional[str] = None,
    ) -> dict:
        if req.stream:
            raise HTTPException(status_code=422, detail="Responses streaming non supporté")
        if not self.bridge.online:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "bridge_extension_disconnected",
                    "message": "Extension Chrome non connectée : ouvre un onglet chatgpt.com.",
                    "retryable": True,
                },
            )
        controls = controls or RunControls()
        response_id = response_id or f"resp_{uuid.uuid4().hex[:24]}"
        # Valide immédiatement outils, entrées et schéma, avant de mettre en file.
        _response_chat_request(req)
        if req.background:
            self.background_responses[response_id] = _response_body(
                response_id, req, status="queued"
            )
            self.background_tasks[response_id] = asyncio.create_task(
                self._execute_background_response(
                    response_id, req, controls, allow_unverified_model
                )
            )
            return self.background_responses[response_id]
        # Les contrôles sont appliqués *dans* le slot : entre leur vérification
        # et la génération, aucune autre requête ne doit pouvoir rebasculer
        # l'interface.
        queued_at = time.monotonic()
        async with self.bridge.slot:
            logger.info(
                "bridge_run_phase bridge_run_id=%s phase=ui_controls queue_wait_ms=%s",
                response_id,
                int((time.monotonic() - queued_at) * 1000),
            )
            report = await prepare_run(
                self.bridge,
                controls,
                allow_unverified_model=allow_unverified_model,
                conversation=req.conversation,
            )
            chat_request = _response_chat_request(req)
            try:
                conversation_result: dict = {}
                extension_metadata: dict = {}
                parts = [
                    text
                    async for text in run_generation(
                        self.bridge,
                        self.registry,
                        response_id,
                        chat_request,
                        http_req,
                        conversation=req.conversation,
                        conversation_result=conversation_result,
                        extension_metadata=extension_metadata,
                    )
                ]
            except NeedsReviewError:
                raise
            except UpstreamError as exc:
                raise HTTPException(
                    status_code=502,
                    detail={"code": exc.code, "message": str(exc), "retryable": exc.retryable},
                ) from exc
        return _response_body(
            response_id,
            req,
            status="completed",
            output_text="".join(parts),
            run=report,
            conversation_result=conversation_result or None,
            extension_metadata=extension_metadata or None,
        )

    async def create_response(self, req: ResponseRequest, http_req: Request):
        """Façade de compatibilité ; préférer le contrat `/v1/bridge/*` en interne.

        Le champ `model` d'une requête Responses nomme un modèle de l'API
        OpenAI, pas une entrée du sélecteur de l'UI : il ne pilote donc rien
        ici. Seul l'outil `web_search` est traduit en réglage d'interface.
        """
        self.ensure_accepting_runs()
        web_search = any(str(tool.get("type", "")) == "web_search" for tool in req.tools)
        return await self.create_response_internal(
            req, http_req, RunControls(web_search=True if web_search else None)
        )

    async def retrieve_response(self, response_id: str):
        response = self.background_responses.get(response_id)
        if response is None:
            raise HTTPException(status_code=404, detail="Réponse de fond inconnue ou expirée")
        return response

    async def chat_completions(self, req: ChatRequest, http_req: Request):
        self.ensure_accepting_runs()
        if not self.bridge.online:
            raise HTTPException(
                status_code=503,
                detail="Extension Chrome non connectée : ouvre un onglet chatgpt.com.",
            )

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        prompt_tokens = _tokens(parse_messages(req.messages)[0])

        if req.stream:
            async def event_stream() -> AsyncIterator[str]:
                # Le verrou est pris ici (et pas dans le handler) : le
                # générateur s'exécute après le retour de l'endpoint.
                async with self.bridge.slot:
                    yield sse_chunk(
                        cid, req.model, created, {"role": "assistant", "content": ""}, None
                    )
                    try:
                        async for text in run_generation(
                            self.bridge, self.registry, cid, req, http_req
                        ):
                            yield sse_chunk(cid, req.model, created, {"content": text}, None)
                    except UpstreamError as exc:
                        err = {"error": {"message": str(exc), "type": "bridge_error"}}
                        yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    yield sse_chunk(cid, req.model, created, {}, "stop")
                    yield "data: [DONE]\n\n"

            return StreamingResponse(
                event_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        async with self.bridge.slot:
            try:
                parts = [
                    text
                    async for text in run_generation(self.bridge, self.registry, cid, req, http_req)
                ]
            except UpstreamError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc

        return completion_body(cid, req.model, created, "".join(parts), prompt_tokens)

    async def list_models(self, probe: bool = False):
        """Modèles du sélecteur ChatGPT quand ils sont connus, liste factice sinon.

        Énumérer les modèles impose d'ouvrir le menu de l'UI : ce n'est fait
        que sur `probe=true`, sinon on se contente d'une sonde récente déjà en
        cache.
        """
        now = int(time.time())
        state = None
        if probe:
            try:
                state = await probed_ui_state(self.bridge)
            except UiUnavailable:
                state = None
        else:
            state = cached_probe()

        disponibles = (state.model.available if state else None) or []
        # La liste des modèles bouge rarement, la sélection change à chaque
        # run : on relit celle-ci, sans toucher aux menus.
        selection = state.model.selected_id if state else None
        if disponibles:
            try:
                selection = (await fetch_ui_state(self.bridge)).model.selected_id or selection
            except UiUnavailable:
                pass

        entrees = [
            {
                "id": m["id"],
                "object": "model",
                "created": now,
                "owned_by": "chatgpt-web-ui",
                "label": m.get("label"),
                "selected": m["id"] == selection,
            }
            for m in disponibles
            if m.get("id")
        ]
        if entrees:
            return {"object": "list", "data": entrees, "source": "chatgpt_ui"}
        return {
            "object": "list",
            "data": [
                {"id": name, "object": "model", "created": now, "owned_by": "chatgpt-web"}
                for name in ("chatgpt-web", "gpt-4o", "gpt-5")
            ],
            "source": "static_fallback",
        }
