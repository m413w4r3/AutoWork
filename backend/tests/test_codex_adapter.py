"""M24 lot B: the Codex adapter maps facts, never CLI specifics (05B3/D10)."""

from __future__ import annotations

import pytest

from cti_app.application.agentic_host_sessions import (
    HostCapabilityUnsupportedError,
    HostResumeUnavailableError,
    HostSessionContractError,
)
from cti_app.application.agentic_tool_broker import AGENTIC_TOOL_NAMES
from cti_app.domain.agentic_operator import (
    OperatorAdapterKind,
    PreparedOperatorSessionV1,
    PrepareSessionRequest,
    ProbeCapabilitiesRequest,
    ResumeSessionRequest,
    StartSessionRequest,
)
from cti_app.infrastructure.agentic.codex_cli import CodexCliOperatorAdapter
from cti_app.infrastructure.agentic.host_operator import HostSessionPreparationRegistry
from tests.agentic_support import (
    HANDOFF_ID,
    INVESTIGATION_ID,
    NOW,
    SESSION_ID,
    TASK_ID,
    FakeHostSessionsPort,
    FixedContextProvider,
    probe_wire,
)


def _adapter(
    host: FakeHostSessionsPort,
    *,
    registry: HostSessionPreparationRegistry | None = None,
) -> tuple[CodexCliOperatorAdapter, HostSessionPreparationRegistry]:
    preparations = registry or HostSessionPreparationRegistry()
    adapter = CodexCliOperatorAdapter(
        host=host,
        preparations=preparations,
        context=FixedContextProvider(),
        clock=lambda: NOW,
    )
    return adapter, preparations


def _prepare_request(capability_sha256: str) -> PrepareSessionRequest:
    return PrepareSessionRequest(
        session_id=SESSION_ID,
        investigation_id=INVESTIGATION_ID,
        task_id=TASK_ID,
        adapter_kind=OperatorAdapterKind.CODEX_CLI,
        handoff_id=HANDOFF_ID,
        capability_sha256=capability_sha256,
    )


async def test_adapter_maps_the_next_run_instruction_flag() -> None:
    host = FakeHostSessionsPort(probe=probe_wire(flags={"next_run_instruction": True}))
    adapter, _ = _adapter(host)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )

    assert capabilities.supports_next_run_instruction is True
    assert capabilities.payload()["supports_next_run_instruction"] is True
    # The host flag is not a CLI mode: the adapter never adds an unprobed mode.
    assert "next_run_instruction" not in capabilities.modes


async def test_adapter_posts_the_closed_manifest_and_refuses_a_wrong_digest() -> None:
    host = FakeHostSessionsPort()
    adapter, _ = _adapter(host)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )

    await adapter.set_tool_manifest(
        SESSION_ID,
        host_session_ref=str(SESSION_ID),
        capability_sha256=capabilities.capability_sha256,
    )

    assert host.names() == ["probe_capabilities", "set_tool_manifest"]
    request = host.manifest_requests[0]
    assert request.manifest_sha256
    assert {tool.broker_name for tool in request.tools} == set(AGENTIC_TOOL_NAMES)

    # A supervisor answer that does not echo the posted digest is a contract
    # error: activation must fail closed rather than run without a manifest.
    host.manifest_sha256_override = "0" * 64
    with pytest.raises(HostSessionContractError):
        await adapter.set_tool_manifest(
            SESSION_ID,
            host_session_ref=str(SESSION_ID),
            capability_sha256=capabilities.capability_sha256,
        )


async def test_adapter_uses_probed_modes_only() -> None:
    host = FakeHostSessionsPort(
        probe=probe_wire(
            flags={
                "structured_output": False,
                "resume": False,
                "streaming_stdin": False,
                "os_sandbox": False,
            }
        )
    )
    adapter, _ = _adapter(host)

    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )

    # Only the flags the host probe marked true become modes; nothing is invented.
    assert capabilities.modes == ("non_interactive", "json_events")
    assert capabilities.supports_resume is False
    assert capabilities.supports_structured_output is False
    assert capabilities.supports_non_interactive is True
    assert capabilities.supports_stop is False
    assert capabilities.permission_modes == ()
    assert capabilities.executable == "codex-cli"
    assert capabilities.capability_sha256 == capabilities.expected_capability_sha256()
    assert host.names() == ["probe_capabilities"]

    resume = await adapter.resume_session(
        ResumeSessionRequest(
            session_id=SESSION_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=str(SESSION_ID),
            capability_sha256=capabilities.capability_sha256,
        )
    )
    assert resume.resumed is False
    assert resume.detail == "the probed adapter does not support resume"
    assert "resume_session" not in host.names()

    with pytest.raises(HostSessionContractError):
        await adapter.start_session(
            StartSessionRequest(
                session_id=SESSION_ID,
                adapter_kind=OperatorAdapterKind.CODEX_CLI,
                host_session_ref=str(SESSION_ID),
                capability_sha256="0" * 64,
            )
        )
    assert "start_session" not in host.names()


async def test_adapter_refuses_another_adapter_kind() -> None:
    adapter, _ = _adapter(FakeHostSessionsPort())
    with pytest.raises(HostSessionContractError):
        await adapter.probe_capabilities(
            ProbeCapabilitiesRequest(
                adapter_kind=OperatorAdapterKind.CLAUDE_CODE, executable="codex-cli"
            )
        )


async def test_prepare_is_deferred_until_the_secret_handoff() -> None:
    host = FakeHostSessionsPort()
    adapter, preparations = _adapter(host)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )

    prepared = await adapter.prepare_session(_prepare_request(capabilities.capability_sha256))

    assert isinstance(prepared, PreparedOperatorSessionV1)
    assert prepared.adapter_kind is OperatorAdapterKind.CODEX_CLI
    assert prepared.host_session_ref == str(SESSION_ID)
    assert prepared.capabilities == capabilities
    # The secret does not exist yet in 05B1, so the only wire prepare belongs to
    # the one-shot secret delivery.
    assert "prepare_session" not in host.names()
    registered = preparations.get(SESSION_ID)
    assert registered is not None
    assert registered.preparation.workspace_id.startswith("ws-")
    assert registered.preparation.expected_probe_sha256 == capabilities.probes[0].stdout_sha256


async def test_start_without_a_non_interactive_probe_is_refused_not_guessed() -> None:
    host = FakeHostSessionsPort(probe=probe_wire(flags={"non_interactive": False}))
    adapter, _ = _adapter(host)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )

    with pytest.raises(HostCapabilityUnsupportedError):
        await adapter.prepare_session(_prepare_request(capabilities.capability_sha256))

    status = await adapter.start_session(
        StartSessionRequest(
            session_id=SESSION_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=str(SESSION_ID),
            capability_sha256=capabilities.capability_sha256,
        )
    )
    assert status.alive is False
    assert "start_session" not in host.names()


async def test_resume_maps_a_host_refusal_to_the_typed_result() -> None:
    host = FakeHostSessionsPort(
        probe=probe_wire(flags={"resume": True}),
        resume_error=HostResumeUnavailableError("no resume token"),
    )
    adapter, _ = _adapter(host)
    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="codex-cli")
    )

    result = await adapter.resume_session(
        ResumeSessionRequest(
            session_id=SESSION_ID,
            adapter_kind=OperatorAdapterKind.CODEX_CLI,
            host_session_ref=str(SESSION_ID),
            capability_sha256=capabilities.capability_sha256,
        )
    )
    assert result.resumed is False
    assert result.detail == "resume_unavailable"
    assert host.names() == ["probe_capabilities", "resume_session"]
