"""Wiring helper for the M24 agentic operator ports.

The orchestrator mounts the returned objects on ``app.state`` (this module never
touches ``api/main.py``)::

    ports = build_agentic_operator_ports(settings, context_provider=...)
    app.state.agentic_operator_port = ports.operator
    app.state.agent_session_secret_delivery = ports.secret_delivery

With no configured workspace-agent socket or token the backend is fail-closed:
both members are ``None``, so ``SessionService`` refuses with its typed
"unavailable" errors instead of inventing a local operator.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

import httpx

from cti_app.application.agentic_host_sessions import (
    HostSessionContextProvider,
    HostSessionContractError,
)
from cti_app.application.agentic_sessions import AgentSessionSecretDeliveryPort
from cti_app.application.host_session_context import CanonicalHostSessionContextProvider
from cti_app.config import Settings
from cti_app.domain.agentic_operator import (
    AgenticOperatorPort,
    CollectResultsRequest,
    HandoffRequestReceiptV1,
    InstructionReceiptV1,
    InterruptSessionRequest,
    InterruptSessionResultV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorResultsV1,
    PreparedOperatorSessionV1,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    ReconcileSessionRequest,
    RequestHandoffRequest,
    ResumeSessionRequest,
    ResumeSessionResultV1,
    SendInstructionRequest,
    SessionProcessStatusV1,
    SessionReconciliationV1,
    SessionStatusRequest,
    StartSessionRequest,
)
from cti_app.infrastructure.agentic.claude_code import ClaudeCodeOperatorAdapter
from cti_app.infrastructure.agentic.codex_cli import CodexCliOperatorAdapter
from cti_app.infrastructure.agentic.host_operator import (
    HostOperatorAdapter,
    HostSessionPreparationRegistry,
)
from cti_app.infrastructure.agentic.host_sessions import HttpHostSessionsClient
from cti_app.infrastructure.agentic.secret_delivery import HostSessionSecretDelivery

SessionAdapterKindResolver = Callable[[UUID], Awaitable[OperatorAdapterKind | None]]
"""Resolve a session's persisted adapter kind after a process restart (F2-D)."""


class DispatchingOperatorPort:
    """One mounted port routing each call to the adapter for its kind.

    The per-process ``_sessions`` map is only a cache: after a backend restart
    it is empty, so a call that names only the session id (interrupt, status,
    instruction, collect, reconcile) asks ``resolve_adapter_kind`` for the
    persisted ``agent_sessions.adapter_kind`` instead of failing with "no
    prepared adapter in this process" (F2-D).
    """

    def __init__(
        self,
        adapters: Mapping[OperatorAdapterKind, HostOperatorAdapter],
        *,
        resolve_adapter_kind: SessionAdapterKindResolver | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._sessions: dict[UUID, OperatorAdapterKind] = {}
        self._resolve_adapter_kind = resolve_adapter_kind

    @property
    def adapter_kind(self) -> OperatorAdapterKind:
        # ADR-0011 D7 declares one kind per adapter; a dispatcher serves both,
        # so it must never present itself as one of them.
        raise HostSessionContractError("a dispatching port has no single adapter kind")

    async def probe_capabilities(self, request: ProbeCapabilitiesRequest) -> OperatorCapabilitiesV1:
        return await self._adapter(request.adapter_kind).probe_capabilities(request)

    async def prepare_session(self, request: PrepareSessionRequest) -> PreparedOperatorSessionV1:
        prepared = await self._adapter(request.adapter_kind).prepare_session(request)
        self._sessions[request.session_id] = request.adapter_kind
        return prepared

    async def start_session(self, request: StartSessionRequest) -> SessionProcessStatusV1:
        self._sessions[request.session_id] = request.adapter_kind
        return await self._adapter(request.adapter_kind).start_session(request)

    async def get_session_status(self, request: SessionStatusRequest) -> SessionProcessStatusV1:
        return await (await self._adapter_for(request.session_id)).get_session_status(request)

    async def send_instruction(self, request: SendInstructionRequest) -> InstructionReceiptV1:
        return await (await self._adapter_for(request.session_id)).send_instruction(request)

    async def request_handoff(self, request: RequestHandoffRequest) -> HandoffRequestReceiptV1:
        return await (await self._adapter_for(request.session_id)).request_handoff(request)

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        return await (await self._adapter_for(request.session_id)).interrupt_session(request)

    async def resume_session(self, request: ResumeSessionRequest) -> ResumeSessionResultV1:
        self._sessions[request.session_id] = request.adapter_kind
        return await self._adapter(request.adapter_kind).resume_session(request)

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1:
        return await (await self._adapter_for(request.session_id)).collect_results(request)

    async def reconcile_session(self, request: ReconcileSessionRequest) -> SessionReconciliationV1:
        return await (await self._adapter_for(request.session_id)).reconcile_session(request)

    def _adapter(self, kind: OperatorAdapterKind) -> HostOperatorAdapter:
        adapter = self._adapters.get(kind)
        if adapter is None:
            raise HostSessionContractError(f"no operator adapter is wired for {kind.value}")
        return adapter

    async def _adapter_for(self, session_id: UUID) -> HostOperatorAdapter:
        kind = self._sessions.get(session_id)
        if kind is None and self._resolve_adapter_kind is not None:
            # Restart recovery: the adapter kind is canonical, so read it from
            # the persisted session rather than failing the operation.
            kind = await self._resolve_adapter_kind(session_id)
            if kind is not None:
                self._sessions[session_id] = kind
        if kind is None:
            raise HostSessionContractError("this process has no prepared adapter for the session")
        return self._adapter(kind)


@dataclass(frozen=True, slots=True)
class AgenticOperatorPorts:
    """What the orchestrator mounts; every member is ``None`` when unavailable."""

    operator: AgenticOperatorPort | None
    secret_delivery: AgentSessionSecretDeliveryPort | None
    host_sessions: HttpHostSessionsClient | None
    adapters: Mapping[OperatorAdapterKind, HostOperatorAdapter] = field(default_factory=dict)
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.operator is not None and self.secret_delivery is not None

    async def aclose(self) -> None:
        if self.host_sessions is not None:
            await self.host_sessions.aclose()


def build_session_runtime(
    app_state: object,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
) -> AgenticOperatorPorts:
    """Build the mountable session runtime, or a fail-closed empty set.

    The orchestrator calls this from the lifespan and mounts the three members
    on ``app.state`` (``agentic_operator_port``, ``agent_session_secret_delivery``
    and ``host_sessions``); it also mounts ``agent_session_runtime`` itself so a
    route can report the typed unavailable reason.  The canonical context
    provider needs the lifespan's ``uow_factory``; without it, or without a
    configured host workspace agent, no adapter is invented (D7 fail-closed).
    """
    socket_path = settings.workspace_agent_socket.strip()
    token_file = settings.workspace_agent_token_file.strip()
    if not socket_path or not token_file:
        return AgenticOperatorPorts(
            operator=None,
            secret_delivery=None,
            host_sessions=None,
            unavailable_reason="workspace_agent_unconfigured",
        )
    uow_factory = getattr(app_state, "uow_factory", None)
    if uow_factory is None:
        return AgenticOperatorPorts(
            operator=None,
            secret_delivery=None,
            host_sessions=None,
            unavailable_reason="canonical_uow_unavailable",
        )
    context_provider = CanonicalHostSessionContextProvider(
        uow_factory=uow_factory,
        settings=settings,
        clock=clock,
    )

    async def resolve_adapter_kind(session_id: UUID) -> OperatorAdapterKind | None:
        """Read the canonical adapter kind so a restarted process can dispatch."""
        async with uow_factory() as uow:
            session = await uow.agent_sessions.get(session_id)
        return None if session is None else session.adapter_kind

    return build_agentic_operator_ports(
        settings,
        context_provider=context_provider,
        transport=transport,
        clock=clock,
        resolve_adapter_kind=resolve_adapter_kind,
    )


def build_agentic_operator_ports(
    settings: Settings,
    *,
    context_provider: HostSessionContextProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Callable[[], datetime] | None = None,
    resolve_adapter_kind: SessionAdapterKindResolver | None = None,
) -> AgenticOperatorPorts:
    """Build both adapters and the secret delivery, or a fail-closed empty set."""
    socket_path = settings.workspace_agent_socket.strip()
    token_file = settings.workspace_agent_token_file.strip()
    if not socket_path or not token_file:
        return AgenticOperatorPorts(operator=None, secret_delivery=None, host_sessions=None)
    host = HttpHostSessionsClient(
        socket_path=socket_path,
        token_file=token_file,
        transport=transport,
    )
    preparations = HostSessionPreparationRegistry()
    network_mode = settings.agent_sessions_cli_network_mode
    adapters: dict[OperatorAdapterKind, HostOperatorAdapter] = {
        OperatorAdapterKind.CODEX_CLI: CodexCliOperatorAdapter(
            host=host,
            preparations=preparations,
            context=context_provider,
            network_mode=network_mode,
            clock=clock,
        ),
        OperatorAdapterKind.CLAUDE_CODE: ClaudeCodeOperatorAdapter(
            host=host,
            preparations=preparations,
            context=context_provider,
            network_mode=network_mode,
            clock=clock,
        ),
    }
    return AgenticOperatorPorts(
        operator=DispatchingOperatorPort(adapters, resolve_adapter_kind=resolve_adapter_kind),
        secret_delivery=HostSessionSecretDelivery(host=host, preparations=preparations),
        host_sessions=host,
        adapters=MappingProxyType(adapters),
    )


__all__ = [
    "AgenticOperatorPorts",
    "DispatchingOperatorPort",
    "SessionAdapterKindResolver",
    "build_agentic_operator_ports",
    "build_session_runtime",
]
