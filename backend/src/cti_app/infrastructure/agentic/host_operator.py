"""Shared thin mapping from the 05B1 operator port onto the host session wire.

Both provider adapters are the same code with a different ``adapter_kind``:
every provider difference (binary, flags, sandbox, resume, permission model)
lives in the host profile and reaches the backend only through the typed
:class:`~cti_app.domain.agentic_operator.OperatorCapabilitiesV1` produced by a
real probe.  A call whose capability the probe did not establish returns the
typed negative result instead of guessing a flag.

Host preparation is deliberately two-phase because the session secret exists
only after 05B1 has derived it (ADR-0011 D7): :meth:`prepare_session` records a
secret-free :class:`HostSessionPreparationV1` and the secret delivery performs
the single ``POST /v1/sessions`` that carries the secret.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Literal
from uuid import UUID

from cti_app.application.agentic_host_sessions import (
    HOST_MAX_INSTRUCTION_BYTES,
    HOST_MAX_REASON_CHARS,
    HostBudgetExceededError,
    HostCapabilityProbeV1,
    HostCapabilityUnsupportedError,
    HostCollectRequestV1,
    HostIdempotencyConflictError,
    HostInstructionRequestV1,
    HostReasonRequestV1,
    HostReconcileRequestV1,
    HostResumeRequestV1,
    HostResumeUnavailableError,
    HostSessionBusyError,
    HostSessionContextProvider,
    HostSessionContextV1,
    HostSessionContractError,
    HostSessionPreparationV1,
    HostSessionsPort,
    HostSessionStateConflictError,
    HostStartedSessionV1,
    HostStartSessionRequestV1,
    HostToolManifestEntryV1,
    HostToolManifestRequestV1,
)
from cti_app.application.agentic_tool_broker import AGENTIC_TOOL_NAMES
from cti_app.domain.agentic_operator import (
    CollectResultsRequest,
    HandoffRequestReceiptV1,
    InstructionDeliveryOutcome,
    InstructionReceiptV1,
    InterruptSessionRequest,
    InterruptSessionResultV1,
    OperatorAdapterKind,
    OperatorCapabilitiesV1,
    OperatorConfinementV1,
    OperatorProbeCommand,
    OperatorResultItemV1,
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
    SessionReconcileOutcome,
    SessionReconciliationV1,
    SessionStatusRequest,
    StartSessionRequest,
)

_HOST_ACTIVE_PROCESS_STATES = frozenset({"running", "awaiting_instruction", "interrupt_requested"})
_REFUSABLE_INSTRUCTION_ERRORS = (
    HostSessionStateConflictError,
    HostSessionBusyError,
    HostBudgetExceededError,
    HostIdempotencyConflictError,
)
_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,15}$")


def host_idempotency_key(operation: str, session_id: UUID, supplied: str | None = None) -> str:
    """Derive a wire-valid key: the same operation and input always replay."""
    if _OPERATION_PATTERN.fullmatch(operation) is None:
        raise ValueError("operation must be a short lowercase token")
    material = f"{session_id}\x00{operation}\x00{supplied or ''}".encode()
    return f"m24h-{operation}-{hashlib.sha256(material).hexdigest()[:48]}"


@dataclass(frozen=True, slots=True)
class ProbedHostFacts:
    capabilities: OperatorCapabilitiesV1
    probe_sha256: str


@dataclass(frozen=True, slots=True)
class RegisteredHostPreparation:
    preparation: HostSessionPreparationV1
    capability_sha256: str
    executable: str


@dataclass(frozen=True, slots=True)
class DeliveredHostSecret:
    """A one-shot delivery marker; the secret itself is never retained."""

    secret_sha256: str
    idempotency_key: str


class HostSessionPreparationRegistry:
    """Per-process bridge between an adapter's prepare and the secret hand-off.

    It holds no secret: only the secret-free preparation, the recorded
    capability hash and, after delivery, the SHA-256 of the delivered secret so
    an idempotent 05B1 replay is recognised without re-sending anything.
    """

    def __init__(self) -> None:
        self._preparations: dict[UUID, RegisteredHostPreparation] = {}
        self._deliveries: dict[UUID, DeliveredHostSecret] = {}

    def register(
        self,
        preparation: HostSessionPreparationV1,
        *,
        capability_sha256: str,
        executable: str,
    ) -> RegisteredHostPreparation:
        existing = self._preparations.get(preparation.session_id)
        if existing is not None:
            if existing.preparation != preparation or existing.executable != executable:
                raise HostSessionContractError("another host preparation already owns the session")
            return existing
        registered = RegisteredHostPreparation(
            preparation=preparation,
            capability_sha256=capability_sha256,
            executable=executable,
        )
        self._preparations[preparation.session_id] = registered
        return registered

    def get(self, session_id: UUID) -> RegisteredHostPreparation | None:
        return self._preparations.get(session_id)

    def delivery(self, session_id: UUID) -> DeliveredHostSecret | None:
        return self._deliveries.get(session_id)

    def record_delivery(
        self, session_id: UUID, *, secret_sha256: str, idempotency_key: str
    ) -> None:
        existing = self._deliveries.get(session_id)
        if existing is not None and existing != DeliveredHostSecret(
            secret_sha256=secret_sha256, idempotency_key=idempotency_key
        ):
            raise HostSessionContractError("the session secret was already delivered")
        self._deliveries[session_id] = DeliveredHostSecret(
            secret_sha256=secret_sha256, idempotency_key=idempotency_key
        )


#: The closed tool manifest the backend posts to a session's supervisor.
#: ``(mcp_name, broker_name, schema_version, effect, cost)`` rows mirror the
#: shim catalogue (``sidecars/workspace_agent/sessions/catalog.py``) and the
#: broker's accepted names (``agentic_tool_broker.AGENTIC_TOOL_NAMES``); a
#: backend test asserts both equalities, so this table can never silently
#: widen the operator surface.  ``effect``/``cost`` are descriptive bounds of
#: the wire contract today: the relay authorizes on ``broker_name`` only and
#: the broker revalidates every argument.  No row claims a capability, so a G2
#: grant cannot be inferred from its mere presence here.
_AGENT_SESSION_TOOL_ROWS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "autowork_bridge_consult",
        "advisor.consult",
        "advisor-consult.v1",
        "external_read",
        "provider",
    ),
    ("autowork_task_heartbeat", "task.heartbeat", "1", "write", "none"),
    ("autowork_task_submit_result", "task.submit_result", "1", "write", "none"),
    ("autowork_handoff_submit", "handoff.submit", "investigation-handoff.v1", "write", "none"),
    ("autowork_vt_file_report", "vt.file_report", "1", "external_read", "provider"),
    ("autowork_vt_object_report", "vt.object_report", "1", "external_read", "provider"),
    ("autowork_vt_relationships", "vt.relationships", "1", "external_read", "provider"),
    ("autowork_vt_intelligence_search", "vt.intelligence_search", "1", "external_read", "provider"),
    ("autowork_vt_behaviour", "vt.behaviour", "1", "external_read", "provider"),
    ("autowork_vt_behaviour_pcap", "vt.behaviour_pcap", "1", "external_read", "provider"),
    ("autowork_vt_download_sample", "vt.download_sample", "1", "acquire", "provider"),
    ("autowork_sample_analyze", "sample.analyze", "1", "local_compute", "worker"),
    ("autowork_archive_extract_member", "archive.extract_member", "1", "local_compute", "worker"),
    ("autowork_shodan_host", "shodan.host", "1", "external_read", "provider"),
    ("autowork_shodan_domain", "shodan.domain", "1", "external_read", "provider"),
    ("autowork_shodan_count", "shodan.count", "1", "external_read", "provider"),
    ("autowork_shodan_search", "shodan.search", "1", "external_read", "provider"),
    ("autowork_analysis_run_script", "analysis.run_script", "1", "local_compute", "worker"),
    ("autowork_note_capture", "note.capture", "1", "write", "none"),
    ("autowork_contribution_import", "contribution.import", "1", "write", "none"),
)


def agent_session_tool_manifest() -> HostToolManifestRequestV1:
    """The exact manifest, digest included, a supervisor must accept before start.

    The digest is the supervisor's canonical form (``json.dumps`` sorted keys,
    compact separators) over ``{"tools": [...]}``; a different digest is a
    contract error, never silently stored.
    """
    declared = {row[1] for row in _AGENT_SESSION_TOOL_ROWS}
    if declared != set(AGENTIC_TOOL_NAMES):
        # Fail closed: a manifest that does not cover the broker's accepted
        # names would refuse legitimate tools, and one that names others would
        # drift beyond the broker's authority.
        raise HostSessionContractError(
            "the session tool manifest does not match the agentic broker catalogue"
        )
    tools = tuple(
        HostToolManifestEntryV1(
            mcp_name=mcp_name,
            broker_name=broker_name,
            schema_version=schema_version,
            input_schema={"type": "object"},
            effect=effect,
            cost=cost,
            capability=None,
        )
        for mcp_name, broker_name, schema_version, effect, cost in _AGENT_SESSION_TOOL_ROWS
    )
    payload = {"tools": [tool.model_dump(mode="json") for tool in tools]}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return HostToolManifestRequestV1(manifest_sha256=digest, tools=tools)


class HostOperatorAdapter:
    """Base for the two provider adapters; subclasses set only their kind."""

    _ADAPTER_KIND: ClassVar[OperatorAdapterKind]

    def __init__(
        self,
        *,
        host: HostSessionsPort,
        preparations: HostSessionPreparationRegistry,
        context: HostSessionContextProvider | None = None,
        network_mode: str = "isolated_egress",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if network_mode != "isolated_egress":
            raise ValueError("network_mode must be isolated_egress")
        self._host = host
        self._preparations = preparations
        self._context = context
        self._network_mode: Literal["isolated_egress"] = "isolated_egress"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._facts: ProbedHostFacts | None = None

    @property
    def adapter_kind(self) -> OperatorAdapterKind:
        return self._ADAPTER_KIND

    # -- probe -------------------------------------------------------------

    async def probe_capabilities(self, request: ProbeCapabilitiesRequest) -> OperatorCapabilitiesV1:
        if request.adapter_kind is not self._ADAPTER_KIND:
            raise HostSessionContractError("the probe names another adapter kind")
        wire = await self._host.probe_capabilities(self._ADAPTER_KIND.value)
        if wire.adapter_kind != self._ADAPTER_KIND.value:
            raise HostSessionContractError("the host probed another adapter kind")
        capabilities = capabilities_from_probe(
            wire, adapter_kind=self._ADAPTER_KIND, executable=request.executable
        )
        self._facts = ProbedHostFacts(capabilities=capabilities, probe_sha256=wire.probe_sha256)
        return capabilities

    def _require_facts(self, capability_sha256: str | None = None) -> ProbedHostFacts:
        facts = self._facts
        if facts is None:
            raise HostSessionContractError("no probed host facts are established in this process")
        if (
            capability_sha256 is not None
            and facts.capabilities.capability_sha256 != capability_sha256
        ):
            raise HostSessionContractError("the recorded capability probe does not match the host")
        return facts

    # -- prepare -----------------------------------------------------------

    async def prepare_session(self, request: PrepareSessionRequest) -> PreparedOperatorSessionV1:
        if request.adapter_kind is not self._ADAPTER_KIND:
            raise HostSessionContractError("prepare_session names another adapter kind")
        facts = self._require_facts(request.capability_sha256)
        if not facts.capabilities.supports_non_interactive:
            raise HostCapabilityUnsupportedError(
                "the probe did not establish a non-interactive operator session"
            )
        context = await self._require_context(request)
        preparation = HostSessionPreparationV1(
            session_id=request.session_id,
            adapter_kind=self._ADAPTER_KIND.value,
            workspace_id=context.workspace_id,
            investigation_id=request.investigation_id,
            task_id=request.task_id,
            handoff=context.handoff,
            lease_fence=context.lease_fence,
            allowed_workspace_paths=context.allowed_workspace_paths,
            skill_names=context.skill_names,
            budgets=context.budgets,
            model=context.model,
            network_mode=self._network_mode,
            expected_probe_sha256=facts.probe_sha256,
        )
        self._preparations.register(
            preparation,
            capability_sha256=facts.capabilities.capability_sha256,
            executable=facts.capabilities.executable,
        )
        return PreparedOperatorSessionV1(
            session_id=request.session_id,
            investigation_id=request.investigation_id,
            task_id=request.task_id,
            adapter_kind=self._ADAPTER_KIND,
            host_session_ref=str(request.session_id),
            capabilities=facts.capabilities,
            prepared_at=self._clock(),
        )

    async def _require_context(self, request: PrepareSessionRequest) -> HostSessionContextV1:
        if self._context is None:
            raise HostSessionContractError(
                "no host session context provider is wired for this adapter"
            )
        return await self._context.host_session_context(
            session_id=request.session_id,
            investigation_id=request.investigation_id,
            task_id=request.task_id,
            adapter_kind=request.adapter_kind,
            handoff_id=request.handoff_id,
            requested_model=request.model,
        )

    # -- lifecycle ---------------------------------------------------------

    async def set_tool_manifest(
        self, session_id: UUID, *, host_session_ref: str, capability_sha256: str
    ) -> None:
        """Post the session's closed tool manifest; start stays refuse-without-it.

        Idempotent by derivation: the same session always sends the same
        manifest and key, so a resume replays the accepted response instead of
        storing a second one.  A refusal or a digest mismatch is a contract
        error, so an activation can never proceed to ``start`` without it.
        """
        self._require_ref(session_id, host_session_ref)
        self._require_facts(capability_sha256)
        request = agent_session_tool_manifest()
        accepted = await self._host.set_tool_manifest(
            session_id,
            request,
            idempotency_key=host_idempotency_key("manifest", session_id),
        )
        if not accepted.accepted or accepted.manifest_sha256 != request.manifest_sha256:
            raise HostSessionContractError(
                "the supervisor did not accept the session tool manifest"
            )

    async def start_session(self, request: StartSessionRequest) -> SessionProcessStatusV1:
        self._require_ref(request.session_id, request.host_session_ref)
        facts = self._require_facts(request.capability_sha256)
        observed_at = self._clock()
        if not facts.capabilities.supports_non_interactive:
            return SessionProcessStatusV1(
                session_id=request.session_id,
                host_session_ref=request.host_session_ref,
                alive=False,
                observed_at=observed_at,
            )
        started = await self._host.start_session(
            request.session_id,
            HostStartSessionRequestV1(),
            idempotency_key=host_idempotency_key(
                "start", request.session_id, request.capability_sha256
            ),
        )
        if started.session_id != request.session_id:
            raise HostSessionContractError("start_session answered another session")
        return _process_status(started, request.host_session_ref, observed_at)

    async def get_session_status(self, request: SessionStatusRequest) -> SessionProcessStatusV1:
        self._require_ref(request.session_id, request.host_session_ref)
        status = await self._host.get_session_status(request.session_id)
        if status.adapter_kind != self._ADAPTER_KIND.value:
            raise HostSessionContractError("the host session belongs to another adapter kind")
        return SessionProcessStatusV1(
            session_id=request.session_id,
            host_session_ref=request.host_session_ref,
            alive=status.state in _HOST_ACTIVE_PROCESS_STATES,
            exit_code=None if status.exit is None else status.exit.code,
            last_activity_at=status.last_activity_at,
            observed_at=self._clock(),
        )

    async def send_instruction(self, request: SendInstructionRequest) -> InstructionReceiptV1:
        self._require_ref(request.session_id, request.host_session_ref)
        observed_at = self._clock()
        text_bytes = request.text.encode("utf-8")
        if len(text_bytes) > HOST_MAX_INSTRUCTION_BYTES or (
            hashlib.sha256(text_bytes).hexdigest() != request.instruction_sha256
        ):
            return InstructionReceiptV1(
                session_id=request.session_id,
                instruction_id=request.instruction_id,
                outcome=InstructionDeliveryOutcome.REFUSED,
                observed_at=observed_at,
            )
        try:
            result = await self._host.send_instruction(
                request.session_id,
                HostInstructionRequestV1(
                    instruction_id=request.instruction_id,
                    text=request.text,
                    sha256=request.instruction_sha256,
                ),
                idempotency_key=host_idempotency_key(
                    "instruction", request.session_id, request.idempotency_key
                ),
            )
        except _REFUSABLE_INSTRUCTION_ERRORS:
            return InstructionReceiptV1(
                session_id=request.session_id,
                instruction_id=request.instruction_id,
                outcome=InstructionDeliveryOutcome.REFUSED,
                observed_at=observed_at,
            )
        return InstructionReceiptV1(
            session_id=request.session_id,
            instruction_id=request.instruction_id,
            outcome=(
                InstructionDeliveryOutcome.ACCEPTED
                if result.accepted
                else InstructionDeliveryOutcome.REFUSED
            ),
            observed_at=observed_at,
        )

    async def request_handoff(self, request: RequestHandoffRequest) -> HandoffRequestReceiptV1:
        # §9: a handoff request is backend-side (05A) and becomes an
        # instruction plus a new run; it is not a host operation.
        raise HostSessionContractError(
            "request_handoff is a backend-side operation, not a host operation"
        )

    async def interrupt_session(self, request: InterruptSessionRequest) -> InterruptSessionResultV1:
        self._require_ref(request.session_id, request.host_session_ref)
        observed_at = self._clock()
        if len(request.reason) > HOST_MAX_REASON_CHARS:
            return InterruptSessionResultV1(
                session_id=request.session_id,
                stopped=False,
                detail="the reason exceeds the host wire bound",
                observed_at=observed_at,
            )
        result = await self._host.interrupt_session(
            request.session_id,
            HostReasonRequestV1(reason=request.reason),
            idempotency_key=host_idempotency_key("interrupt", request.session_id, request.reason),
        )
        return InterruptSessionResultV1(
            session_id=request.session_id,
            stopped=result.signal_sent,
            detail="signal sent" if result.signal_sent else "the host sent no signal",
            observed_at=observed_at,
        )

    async def resume_session(self, request: ResumeSessionRequest) -> ResumeSessionResultV1:
        self._require_ref(request.session_id, request.host_session_ref)
        observed_at = self._clock()
        facts = self._require_facts(request.capability_sha256)
        if not facts.capabilities.supports_resume:
            return ResumeSessionResultV1(
                session_id=request.session_id,
                resumed=False,
                detail="the probed adapter does not support resume",
                observed_at=observed_at,
            )
        try:
            started = await self._host.resume_session(
                request.session_id,
                HostResumeRequestV1(reason="human resume"),
                idempotency_key=host_idempotency_key(
                    "resume", request.session_id, request.capability_sha256
                ),
            )
        except HostResumeUnavailableError:
            return ResumeSessionResultV1(
                session_id=request.session_id,
                resumed=False,
                detail="resume_unavailable",
                observed_at=observed_at,
            )
        if started.session_id != request.session_id:
            raise HostSessionContractError("resume_session answered another session")
        return ResumeSessionResultV1(
            session_id=request.session_id,
            resumed=True,
            host_session_ref=str(request.session_id),
            observed_at=observed_at,
        )

    # -- results -----------------------------------------------------------

    async def collect_results(self, request: CollectResultsRequest) -> OperatorResultsV1:
        self._require_ref(request.session_id, request.host_session_ref)
        result = await self._host.collect_results(
            request.session_id,
            HostCollectRequestV1(capture=True),
            idempotency_key=host_idempotency_key("collect", request.session_id, request.cursor),
        )
        items = tuple(
            OperatorResultItemV1(
                result_id=f"{_result_kind(captured.path)}:"
                f"{hashlib.sha256(captured.path.encode('utf-8')).hexdigest()[:40]}",
                kind=_result_kind(captured.path),
                summary=captured.path,
                content_sha256=captured.sha256,
            )
            for captured in result.files
        )
        return OperatorResultsV1(
            session_id=request.session_id,
            items=items,
            observed_at=result.collected_at,
        )

    async def reconcile_session(self, request: ReconcileSessionRequest) -> SessionReconciliationV1:
        host_ref = request.host_session_ref
        if host_ref is not None:
            self._require_ref(request.session_id, host_ref)
        observed_at = self._clock()
        result = await self._host.reconcile_session(
            request.session_id,
            HostReconcileRequestV1(),
            idempotency_key=host_idempotency_key("reconcile", request.session_id),
        )
        outcome = _RECONCILE_OUTCOMES[result.outcome]
        checkpoint_ref: str | None = None
        checkpoint_sha256: str | None = None
        detail: str | None = f"host outcome {result.outcome}"
        if outcome is SessionReconcileOutcome.LOST_WITH_HANDOFF:
            status = await self._host.get_session_status(request.session_id)
            if status.checkpoint_sha256 is None:
                # Never fabricate a checkpoint: a loss without one is unreconciled.
                outcome = SessionReconcileOutcome.LOST_UNRECONCILED
                detail = "the host reported a loss without a recorded checkpoint"
            else:
                checkpoint_ref = f"host-session:{request.session_id}"
                checkpoint_sha256 = status.checkpoint_sha256
        return SessionReconciliationV1(
            session_id=request.session_id,
            outcome=outcome,
            host_session_ref=(
                str(request.session_id)
                if outcome is SessionReconcileOutcome.RESUME_AVAILABLE
                else None
            ),
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha256=checkpoint_sha256,
            detail=detail,
            observed_at=observed_at,
        )

    def _require_ref(self, session_id: UUID, host_session_ref: str) -> None:
        if host_session_ref != str(session_id):
            raise HostSessionContractError("the host session reference names another session")


def capabilities_from_probe(
    wire: HostCapabilityProbeV1, *, adapter_kind: OperatorAdapterKind, executable: str
) -> OperatorCapabilitiesV1:
    """Translate the host's real probe facts; every mode came from the probe."""
    modes = wire.flags.proven_modes()
    confinement_mode = f"confinement:{wire.confinement.profile}"
    advertised = modes
    if len(confinement_mode) <= 64:
        advertised = (*modes, confinement_mode)
    probe = OperatorProbeCommand(
        argv=("sessions.capabilities", wire.adapter_kind),
        exit_code=0,
        stdout_sha256=wire.probe_sha256,
        advertised_modes=advertised,
        resolved_version=wire.version,
    )
    return OperatorCapabilitiesV1(
        adapter_kind=adapter_kind,
        executable=executable.strip(),
        version=wire.version,
        modes=modes,
        supports_resume=wire.flags.resume,
        supports_structured_output=wire.flags.structured_output,
        supports_non_interactive=wire.flags.non_interactive,
        # F3-D §3.4: the flag the instruction drain requires before any host
        # effect; an older host that does not send it keeps the queue intact.
        supports_next_run_instruction=wire.flags.next_run_instruction,
        # The host wire has no CLI stop flag; interrupt is a supervisor signal.
        supports_stop=False,
        permission_modes=(),
        confinement=OperatorConfinementV1(
            profile=wire.confinement.profile,
            bwrap_ok=wire.confinement.bwrap_ok,
            netns=wire.confinement.netns,
            network_mode=wire.confinement.network_mode,
            loopback_isolated=wire.confinement.loopback_isolated,
            reason=(
                wire.confinement.network_reason
                or wire.confinement.reason
                or wire.confinement.bwrap_reason
            ),
        ),
        probed_at=wire.probed_at,
        probes=(probe,),
    )


def _process_status(
    started: HostStartedSessionV1, host_session_ref: str, observed_at: datetime
) -> SessionProcessStatusV1:
    return SessionProcessStatusV1(
        session_id=started.session_id,
        host_session_ref=host_session_ref,
        alive=started.state == "running",
        observed_at=observed_at,
    )


def _result_kind(path: str) -> Literal["note", "report", "artifact"]:
    if path.startswith("notes/"):
        return "note"
    if path.startswith("results/"):
        return "report"
    return "artifact"


_RECONCILE_OUTCOMES = {
    "session_alive": SessionReconcileOutcome.SESSION_ALIVE,
    "terminated_clean": SessionReconcileOutcome.TERMINATED_CLEAN,
    "lost_with_handoff": SessionReconcileOutcome.LOST_WITH_HANDOFF,
    "lost_unreconciled": SessionReconcileOutcome.LOST_UNRECONCILED,
    "resume_available": SessionReconcileOutcome.RESUME_AVAILABLE,
    "resume_unavailable": SessionReconcileOutcome.RESUME_UNAVAILABLE,
}

__all__ = [
    "DeliveredHostSecret",
    "HostOperatorAdapter",
    "HostSessionPreparationRegistry",
    "ProbedHostFacts",
    "RegisteredHostPreparation",
    "capabilities_from_probe",
    "host_idempotency_key",
]
