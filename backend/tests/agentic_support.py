"""Shared fakes and builders for the M24 lot B host-session tests.

Everything here is in-memory and offline: the fake host port records the exact
wire DTOs an adapter or the secret delivery produced, so a test can assert the
mapping without a server.  The real-Supervisor contract test lives in
``test_agentic_host_sessions.py`` and uses the real sidecar instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from cti_app.application.agentic_host_sessions import (
    HostBudgetUsageV1,
    HostBudgetV1,
    HostCapabilityFlagsV1,
    HostCapabilityProbeV1,
    HostCapturedFileV1,
    HostCloseResultV1,
    HostCollectRequestV1,
    HostConfinementV1,
    HostHandoffRefV1,
    HostInstructionRequestV1,
    HostInstructionResultV1,
    HostInterruptResultV1,
    HostLogLineV1,
    HostLogRangeV1,
    HostLogsPageV1,
    HostPreparedSessionV1,
    HostPrepareSessionRequestV1,
    HostReasonRequestV1,
    HostReconcileEvidenceV1,
    HostReconcileRequestV1,
    HostReconcileResultV1,
    HostResumeRequestV1,
    HostSessionContextV1,
    HostSessionEventsPageV1,
    HostSessionEventV1,
    HostSessionResultV1,
    HostSessionStatusV1,
    HostStartedSessionV1,
    HostStartSessionRequestV1,
    HostToolManifestAcceptedV1,
    HostToolManifestRequestV1,
)
from cti_app.domain.agentic_operator import OperatorAdapterKind

SessionStateName = Literal[
    "provisioning",
    "ready",
    "running",
    "awaiting_instruction",
    "interrupt_requested",
    "stopped",
    "lost",
    "reconciling",
    "closed",
    "failed",
]

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SESSION_ID = UUID("5d3f0a6e-7c2b-4b8f-9a1d-2e4c6b8a0f13")
INVESTIGATION_ID = UUID("6e4f1b2c-8d3a-4c5e-9f60-1a2b3c4d5e6f")
TASK_ID = UUID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
HANDOFF_ID = UUID("0f1e2d3c-4b5a-4968-8776-655443322110")
WORKSPACE_ID = "ws-" + "a" * 32
PROBE_SHA256 = "1" * 64
BINARY_SHA256 = "2" * 64
HELP_SHA256 = "3" * 64
SECRET_HEX = "4" * 64
SECRET_BYTES = bytes.fromhex(SECRET_HEX)

DEFAULT_MODES: dict[str, bool] = {
    "non_interactive": True,
    "json_events": True,
    "structured_output": False,
    "resume": False,
    "streaming_stdin": False,
    "mcp_per_invocation": False,
    "os_sandbox": False,
}


def probe_wire(
    *,
    adapter_kind: str = "codex_cli",
    flags: dict[str, bool] | None = None,
    profile: str = "fake-direct",
    bwrap_ok: bool = False,
    probe_sha256: str = PROBE_SHA256,
) -> HostCapabilityProbeV1:
    merged = dict(DEFAULT_MODES)
    merged.update(flags or {})
    return HostCapabilityProbeV1(
        adapter_kind=adapter_kind,
        version="1.2.3",
        binary_sha256=BINARY_SHA256,
        help_sha256=HELP_SHA256,
        flags=HostCapabilityFlagsV1(**merged),
        confinement=HostConfinementV1(profile=profile, bwrap_ok=bwrap_ok, netns=False),
        probed_at=NOW,
        probe_sha256=probe_sha256,
    )


def context(
    *,
    workspace_id: str = WORKSPACE_ID,
    model: str | None = "fake-model",
    lease_fence: int = 0,
) -> HostSessionContextV1:
    return HostSessionContextV1(
        workspace_id=workspace_id,
        handoff=host_handoff(),
        lease_fence=lease_fence,
        allowed_workspace_paths=("notes/humaines", "results"),
        skill_names=(),
        budgets=host_budget(),
        model=model,
    )


def host_handoff() -> HostHandoffRefV1:
    return HostHandoffRefV1(handoff_id=HANDOFF_ID, version=1, sha256="b" * 64)


def host_budget() -> HostBudgetV1:
    return HostBudgetV1(wall_seconds=30, max_output_bytes=1_048_576, max_instructions=8)


class FixedContextProvider:
    """The test's explicit host-only coordinates for a prepare request."""

    def __init__(self, value: HostSessionContextV1 | None = None) -> None:
        self.value = value or context()
        self.requests: list[dict[str, object]] = []

    async def host_session_context(
        self,
        *,
        session_id: UUID,
        investigation_id: UUID,
        task_id: UUID,
        adapter_kind: OperatorAdapterKind,
    ) -> HostSessionContextV1:
        self.requests.append(
            {
                "session_id": session_id,
                "investigation_id": investigation_id,
                "task_id": task_id,
                "adapter_kind": adapter_kind,
            }
        )
        return self.value


class FakeHostSessionsPort:
    """Records every wire call and answers with contract-valid canned facts."""

    def __init__(
        self,
        *,
        probe: HostCapabilityProbeV1 | None = None,
        statuses: list[HostSessionStatusV1] | None = None,
        reconcile_outcome: str = "terminated_clean",
        checkpoint_sha256: str | None = None,
        captured_files: tuple[HostCapturedFileV1, ...] = (),
        resume_error: Exception | None = None,
    ) -> None:
        self.probe = probe or probe_wire()
        self.statuses = list(statuses or [])
        self.reconcile_outcome = reconcile_outcome
        self.checkpoint_sha256 = checkpoint_sha256
        self.captured_files = captured_files
        self.resume_error = resume_error
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.prepare_requests: list[HostPrepareSessionRequestV1] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def probe_capabilities(self, adapter_kind: str) -> HostCapabilityProbeV1:
        self.calls.append(("probe_capabilities", {"adapter_kind": adapter_kind}))
        return self.probe

    async def prepare_session(
        self, request: HostPrepareSessionRequestV1, *, idempotency_key: str
    ) -> HostPreparedSessionV1:
        self.calls.append(("prepare_session", {"idempotency_key": idempotency_key}))
        self.prepare_requests.append(request)
        return HostPreparedSessionV1(
            session_id=request.session_id,
            state="ready",
            state_version=1,
            capability_sha256=self.probe.probe_sha256,
            confinement=self.probe.confinement,
            prompt_sha256="9" * 64,
        )

    async def start_session(
        self,
        session_id: UUID,
        request: HostStartSessionRequestV1,
        *,
        idempotency_key: str,
    ) -> HostStartedSessionV1:
        self.calls.append(("start_session", {"session_id": session_id}))
        return HostStartedSessionV1(
            session_id=session_id,
            state="running",
            state_version=2,
            run=1,
            log_cursor=3,
        )

    async def get_session_status(self, session_id: UUID) -> HostSessionStatusV1:
        self.calls.append(("get_session_status", {"session_id": session_id}))
        if self.statuses:
            return self.statuses.pop(0)
        return status_wire(checkpoint_sha256=self.checkpoint_sha256)

    async def get_events(
        self,
        session_id: UUID,
        *,
        after: int = 0,
        generation: int | None = None,
        limit: int = 500,
    ) -> HostSessionEventsPageV1:
        self.calls.append(("get_events", {"session_id": session_id}))
        return HostSessionEventsPageV1(
            generation=1,
            events=(HostSessionEventV1(seq=1, kind="process_started", at=NOW, payload={"run": 1}),),
            next_cursor=1,
            oldest_retained=0,
            resync_required=False,
        )

    async def get_logs(
        self, session_id: UUID, *, after: int = 0, limit: int = 500
    ) -> HostLogsPageV1:
        self.calls.append(("get_logs", {"session_id": session_id}))
        return HostLogsPageV1(
            lines=(HostLogLineV1(seq=1, stream="stdout", text="started", truncated=False),),
            next_cursor=1,
        )

    async def send_instruction(
        self,
        session_id: UUID,
        request: HostInstructionRequestV1,
        *,
        idempotency_key: str,
    ) -> HostInstructionResultV1:
        self.calls.append(("send_instruction", {"session_id": session_id}))
        return HostInstructionResultV1(accepted=True, delivered="live", cursor=4)

    async def interrupt_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostInterruptResultV1:
        self.calls.append(("interrupt_session", {"session_id": session_id}))
        return HostInterruptResultV1(state="interrupt_requested", signal_sent=True)

    async def resume_session(
        self, session_id: UUID, request: HostResumeRequestV1, *, idempotency_key: str
    ) -> HostStartedSessionV1:
        self.calls.append(("resume_session", {"session_id": session_id}))
        if self.resume_error is not None:
            raise self.resume_error
        return HostStartedSessionV1(
            session_id=session_id,
            state="running",
            state_version=3,
            run=2,
            log_cursor=9,
        )

    async def reconcile_session(
        self, session_id: UUID, request: HostReconcileRequestV1, *, idempotency_key: str
    ) -> HostReconcileResultV1:
        self.calls.append(("reconcile_session", {"session_id": session_id}))
        return HostReconcileResultV1(
            outcome=self.reconcile_outcome,
            evidence=HostReconcileEvidenceV1(last_event_seq=7),
        )

    async def collect_results(
        self, session_id: UUID, request: HostCollectRequestV1, *, idempotency_key: str
    ) -> HostSessionResultV1:
        self.calls.append(("collect_results", {"session_id": session_id}))
        return HostSessionResultV1(
            outcome="success",
            exit=None,
            final_message_sha256="8" * 64,
            files=self.captured_files,
            log_range=HostLogRangeV1(cursor_from=0, cursor_to=7),
            captured_notes=(),
            collected_at=NOW,
        )

    async def close_session(
        self, session_id: UUID, request: HostReasonRequestV1, *, idempotency_key: str
    ) -> HostCloseResultV1:
        self.calls.append(("close_session", {"session_id": session_id}))
        return HostCloseResultV1(state="closed")

    async def set_tool_manifest(
        self,
        session_id: UUID,
        request: HostToolManifestRequestV1,
        *,
        idempotency_key: str,
    ) -> HostToolManifestAcceptedV1:
        self.calls.append(("set_tool_manifest", {"session_id": session_id}))
        return HostToolManifestAcceptedV1(accepted=True, manifest_sha256=request.manifest_sha256)


def status_wire(
    *,
    state: SessionStateName = "running",
    checkpoint_sha256: str | None = None,
    adapter_kind: str = "codex_cli",
) -> HostSessionStatusV1:
    return HostSessionStatusV1(
        state=state,
        state_version=2,
        adapter_kind=adapter_kind,
        runs=(),
        budgets_used=HostBudgetUsageV1(wall_seconds=0.5, output_bytes=10, instructions=1),
        last_activity_at=NOW,
        exit=None,
        log_cursor=3,
        checkpoint_sha256=checkpoint_sha256,
    )


__all__ = [
    "BINARY_SHA256",
    "DEFAULT_MODES",
    "HANDOFF_ID",
    "HELP_SHA256",
    "INVESTIGATION_ID",
    "NOW",
    "PROBE_SHA256",
    "SECRET_BYTES",
    "SECRET_HEX",
    "SESSION_ID",
    "TASK_ID",
    "WORKSPACE_ID",
    "FakeHostSessionsPort",
    "FixedContextProvider",
    "SessionStateName",
    "context",
    "host_budget",
    "host_handoff",
    "probe_wire",
    "status_wire",
]
