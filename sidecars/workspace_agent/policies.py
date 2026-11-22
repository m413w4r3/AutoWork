"""Route-level file policies on top of the §5.3 policy-class table (design §5.3, §7).

The prefix -> ``policy_class`` table itself lives in exactly one place,
:func:`index.policy_class_for`, which the scan already uses; this module never
duplicates it. It adds what only a write route needs: which route may write
which class, the final mode of a published file, and the task-id binding of
``notes/agents/<task_id>/``. A route/prefix mismatch is ``policy_denied`` (403).
"""

from __future__ import annotations

from enum import StrEnum

from . import protocol, workspace
from .index import (
    POLICY_AGENT_OWNED,
    POLICY_GENERATED_IMMUTABLE,
    POLICY_HUMAN_EDITABLE,
    policy_class_for,
)
from .protocol import ErrorCode

EDITABLE_ROUTE_CLASSES: frozenset[str] = frozenset({POLICY_HUMAN_EDITABLE, POLICY_AGENT_OWNED})
GENERATED_ROUTE_CLASSES: frozenset[str] = frozenset({POLICY_GENERATED_IMMUTABLE})

EDITABLE_FILE_MODE = 0o640
GENERATED_FILE_MODE = 0o440
CONFLICT_FILE_MODE = 0o640

AGENTS_DIRECTORY = "agents"
NOTES_DIRECTORY = "notes"


class WriteRoute(StrEnum):
    """The two write surfaces of §4.1; reads are unconstrained by policy class."""

    FILES = "files"
    GENERATED = "generated"


class PolicyDeniedError(workspace.WorkspaceError):
    """The route, the policy class and the task binding do not agree."""

    code = ErrorCode.POLICY_DENIED
    status = 403

    def __init__(self, path: str, policy_class: str, route: str, *, reason: str) -> None:
        super().__init__(
            "the path policy forbids this operation",
            details={"path": path, "policy_class": policy_class, "route": route, "reason": reason},
        )


def policy_for(path: str) -> str:
    """Return the §5.3 class of a validated workspace-relative path."""
    return policy_class_for(path)


def require_route_access(route: WriteRoute, path: str) -> str:
    """Return the path's policy class, or refuse a route/prefix mismatch."""
    policy_class = policy_class_for(path)
    allowed = EDITABLE_ROUTE_CLASSES if route is WriteRoute.FILES else GENERATED_ROUTE_CLASSES
    if policy_class not in allowed:
        raise PolicyDeniedError(path, policy_class, str(route), reason="route_policy_mismatch")
    return policy_class


def agent_task_id_for(path: str) -> str | None:
    """Return the ``<task_id>`` component of ``notes/agents/<task_id>/…``."""
    components = path.split("/")
    if len(components) >= 3 and components[0] == NOTES_DIRECTORY:
        if components[1] == AGENTS_DIRECTORY:
            return components[2]
    return None


def require_task_binding(path: str, task_id: str | None) -> str | None:
    """Enforce ``X-Autowork-Task-Id == <task_id>`` for agent-owned paths (§7).

    A path outside ``notes/agents/<task_id>/`` has no component to compare and
    the header is optional there; when present it is recorded verbatim.
    """
    expected = agent_task_id_for(path)
    if expected is None:
        return task_id
    if task_id != expected:
        raise PolicyDeniedError(
            path, policy_class_for(path), str(WriteRoute.FILES), reason="task_id_mismatch"
        )
    return expected


def final_mode(route: WriteRoute) -> int:
    """The mode a successfully published file receives (§5.3)."""
    return EDITABLE_FILE_MODE if route is WriteRoute.FILES else GENERATED_FILE_MODE


def write_limit(route: WriteRoute) -> int:
    """The declared body limit of a write route (§4)."""
    if route is WriteRoute.FILES:
        return protocol.EDITABLE_FILE_LIMIT
    return protocol.GENERATED_FILE_LIMIT
