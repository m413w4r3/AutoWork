"""Codex CLI operator adapter (M24 lot B / 05B3).

The adapter exists to bind :class:`HostOperatorAdapter` to ``codex_cli``.  Every
Codex-specific fact — binary path, flags, sandbox arguments, resume support,
JSON event shape — belongs to the host profile built by lot H4; the backend sees
only the typed capability probe and the session wire.
"""

from __future__ import annotations

from cti_app.domain.agentic_operator import OperatorAdapterKind
from cti_app.infrastructure.agentic.host_operator import HostOperatorAdapter


class CodexCliOperatorAdapter(HostOperatorAdapter):
    _ADAPTER_KIND = OperatorAdapterKind.CODEX_CLI


__all__ = ["CodexCliOperatorAdapter"]
