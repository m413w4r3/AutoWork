"""Claude Code operator adapter (M24 lot B / 06).

The adapter exists to bind :class:`HostOperatorAdapter` to ``claude_code``.
Every Claude-specific fact — binary path, flags, stream-json input, resume
support, restricted tool set — belongs to the host profile built by lot H5; the
backend sees only the typed capability probe and the session wire.
"""

from __future__ import annotations

from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.infrastructure.agentic.host_operator import HostOperatorAdapter


class ClaudeCodeOperatorAdapter(HostOperatorAdapter):
    _ADAPTER_KIND = OperatorAdapterKind.CLAUDE_CODE


__all__ = ["ClaudeCodeOperatorAdapter"]
