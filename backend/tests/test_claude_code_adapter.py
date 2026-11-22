"""M24 lot B: the Claude Code adapter maps facts, never CLI specifics (06)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cti_app.application.agentic_host_sessions import HostSessionContractError
from cti_app.domain.agentic_operator import (
    OperatorAdapterKind,
    ProbeCapabilitiesRequest,
    ResumeSessionRequest,
)
from cti_app.infrastructure.agentic.claude_code import ClaudeCodeOperatorAdapter
from cti_app.infrastructure.agentic.host_operator import HostSessionPreparationRegistry
from tests.agentic_support import (
    NOW,
    SESSION_ID,
    FakeHostSessionsPort,
    FixedContextProvider,
    probe_wire,
)

BACKEND_SRC = Path(__file__).resolve().parents[1] / "src" / "cti_app"

# A backend module never names a host CLI flag, a home configuration directory
# or an absolute host path: those facts live in the host profiles only.
_FORBIDDEN_SOURCE = (
    re.compile(r"/home/"),
    re.compile(r"/root/"),
    re.compile(r'"~/'),
    re.compile(r"/\.codex\b"),
    re.compile(r"/\.claude\b"),
    re.compile(r"codex exec"),
    re.compile(r"claude -p"),
    re.compile(r"""["']--?[A-Za-z]"""),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"\bPopen\b"),
    re.compile(r"\bshlex\b"),
    re.compile(r"\bos\.exec"),
)


def test_backend_never_mentions_host_paths() -> None:
    paths = [BACKEND_SRC / "application" / "agentic_host_sessions.py"]
    paths.extend(sorted((BACKEND_SRC / "infrastructure" / "agentic").glob("*.py")))
    assert len(paths) >= 6
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN_SOURCE:
            match = pattern.search(source)
            assert match is None, f"{path.name} names a host-only fact: {match.group(0)!r}"


async def test_adapter_uses_probed_modes_only() -> None:
    host = FakeHostSessionsPort(
        probe=probe_wire(
            adapter_kind="claude_code",
            flags={
                "structured_output": True,
                "resume": True,
                "streaming_stdin": True,
                "os_sandbox": True,
            },
            profile="fake-direct",
        )
    )
    adapter = ClaudeCodeOperatorAdapter(
        host=host,
        preparations=HostSessionPreparationRegistry(),
        context=FixedContextProvider(),
        clock=lambda: NOW,
    )

    capabilities = await adapter.probe_capabilities(
        ProbeCapabilitiesRequest(
            adapter_kind=OperatorAdapterKind.CLAUDE_CODE, executable="claude-code"
        )
    )

    assert capabilities.adapter_kind is OperatorAdapterKind.CLAUDE_CODE
    assert capabilities.modes == (
        "non_interactive",
        "json_events",
        "structured_output",
        "resume",
        "streaming_stdin",
        "os_sandbox",
    )
    assert capabilities.supports_resume is True
    assert capabilities.supports_structured_output is True
    assert capabilities.supports_non_interactive is True
    assert capabilities.supports_stop is False
    assert capabilities.executable == "claude-code"
    # The probe command records the real wire probe digest, not a CLI claim.
    expected_probe = probe_wire(adapter_kind="claude_code").probe_sha256
    assert capabilities.probes[0].stdout_sha256 == expected_probe
    assert "confinement:fake-direct" in capabilities.probes[0].advertised_modes
    assert host.names() == ["probe_capabilities"]

    resumed = await adapter.resume_session(
        ResumeSessionRequest(
            session_id=SESSION_ID,
            adapter_kind=OperatorAdapterKind.CLAUDE_CODE,
            host_session_ref=str(SESSION_ID),
            capability_sha256=capabilities.capability_sha256,
        )
    )
    assert resumed.resumed is True
    assert resumed.host_session_ref == str(SESSION_ID)
    assert host.names() == ["probe_capabilities", "resume_session"]

    with pytest.raises(HostSessionContractError):
        await adapter.probe_capabilities(
            ProbeCapabilitiesRequest(
                adapter_kind=OperatorAdapterKind.CODEX_CLI, executable="claude-code"
            )
        )
