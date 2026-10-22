"""Compact operator trace records stored in the durable invocation purpose."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from cti_app.domain.investigation import BudgetDimension, ToolInvocationStatus, canonical_json_bytes

PURPOSE_MARKER = "[[advisor-trace.v1]]"
MAX_CHECKPOINT_ID_CHARS = 128
MAX_CHECKPOINT_REASON_CHARS = 256


def split_advisor_trace_purpose(
    purpose: str,
    *,
    tool_name: str | None = None,
    allow_server_denied: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Parse an optional trace prefix and return it with the human purpose."""
    if not purpose.startswith(PURPOSE_MARKER):
        return None, purpose
    encoded, separator, human_purpose = purpose[len(PURPOSE_MARKER) :].partition("\n")
    if not separator or not human_purpose.strip():
        raise ValueError("advisor_trace_invalid")
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError:
        raise ValueError("advisor_trace_invalid") from None
    if not isinstance(payload, Mapping) or set(payload) - {"checkpoint", "recommendation"}:
        raise ValueError("advisor_trace_invalid")
    if not payload:
        raise ValueError("advisor_trace_invalid")

    normalized: dict[str, Any] = {}
    checkpoint = payload.get("checkpoint")
    if checkpoint is not None:
        if not isinstance(checkpoint, Mapping) or set(checkpoint) - {
            "id",
            "status",
            "reason",
        }:
            raise ValueError("advisor_trace_invalid")
        checkpoint_id = checkpoint.get("id")
        status = checkpoint.get("status")
        reason = checkpoint.get("reason")
        if (
            not isinstance(checkpoint_id, str)
            or not checkpoint_id
            or len(checkpoint_id) > MAX_CHECKPOINT_ID_CHARS
            or status not in {"consulted", "skipped", "denied"}
            or (status == "denied" and not allow_server_denied)
        ):
            raise ValueError("advisor_trace_invalid")
        if status == "skipped" and not _bounded_reason(reason):
            raise ValueError("advisor_trace_invalid")
        if status == "denied" and not _bounded_reason(reason):
            raise ValueError("advisor_trace_invalid")
        if reason is not None and not _bounded_reason(reason):
            raise ValueError("advisor_trace_invalid")
        if tool_name is not None:
            if status == "consulted" and tool_name != "advisor.consult":
                raise ValueError("advisor_trace_invalid")
            if status == "skipped" and tool_name == "advisor.consult":
                raise ValueError("advisor_trace_invalid")
        normalized["checkpoint"] = {
            "id": checkpoint_id,
            "status": status,
            **({"reason": reason.strip()} if isinstance(reason, str) else {}),
        }

    recommendation = payload.get("recommendation")
    if recommendation is not None:
        if not isinstance(recommendation, Mapping) or set(recommendation) - {
            "invocation_id",
            "index",
            "decision",
            "reason",
        }:
            raise ValueError("advisor_trace_invalid")
        try:
            advisor_invocation_id = UUID(str(recommendation.get("invocation_id")))
        except (TypeError, ValueError):
            raise ValueError("advisor_trace_invalid") from None
        index = recommendation.get("index")
        decision = recommendation.get("decision")
        reason = recommendation.get("reason")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= 4
            or decision not in {"accepted", "refused"}
            or (decision == "refused" and not _bounded_reason(reason))
            or (reason is not None and not _bounded_reason(reason))
        ):
            raise ValueError("advisor_trace_invalid")
        normalized["recommendation"] = {
            "invocation_id": str(advisor_invocation_id),
            "index": index,
            "decision": decision,
            **({"reason": reason.strip()} if isinstance(reason, str) else {}),
        }

    return normalized, human_purpose


def format_advisor_trace_purpose(
    purpose: str,
    *,
    checkpoint: Mapping[str, Any] | None = None,
    recommendation: Mapping[str, Any] | None = None,
) -> str:
    """Prefix safe trace metadata to the already validated operator purpose."""
    if not purpose.strip():
        raise ValueError("operator_purpose_required")
    payload: dict[str, Any] = {}
    if checkpoint is not None:
        payload["checkpoint"] = dict(checkpoint)
    if recommendation is not None:
        payload["recommendation"] = dict(recommendation)
    encoded = canonical_json_bytes(payload).decode("utf-8")
    result = f"{PURPOSE_MARKER}{encoded}\n{purpose}"
    if len(result) > 4_096:
        raise ValueError("advisor_trace_invalid")
    return result


def advisor_checkpoint_summary(invocation: Any) -> str | None:
    """Render persisted checkpoint and recommendation links into tool coverage."""
    purpose = getattr(invocation, "purpose", None)
    if not isinstance(purpose, str):
        return None
    try:
        trace, _human_purpose = split_advisor_trace_purpose(purpose, allow_server_denied=True)
    except ValueError:
        return "Advisor trace is malformed; inspect the invocation journal."
    if trace is None:
        return None

    status = getattr(invocation.status, "value", invocation.status)
    checkpoint = trace.get("checkpoint")
    pieces: list[str] = []
    if isinstance(checkpoint, Mapping):
        decision = str(checkpoint["status"])
        reason = checkpoint.get("reason")
        if decision == "consulted" and status == ToolInvocationStatus.POLICY_DENIED.value:
            decision = "denied"
            reason = str(reason or "policy_denied")
        pieces.append(f"checkpoint {checkpoint['id']} {decision}")
        if isinstance(reason, str) and reason:
            pieces.append(f"reason={reason}")
        pieces.append(f"invocation={invocation.id}")
        budget = getattr(invocation, "consumed_budget", {})
        costs = (
            (BudgetDimension.BRIDGE_TURNS, "turns"),
            (BudgetDimension.CONTEXT_BYTES, "bytes"),
            (BudgetDimension.CONTEXT_TOKENS, "context_units_estimated"),
            (BudgetDimension.TOOL_SECONDS, "seconds"),
        )
        cost_text = ",".join(f"{label}:{budget.get(dimension, 0)}" for dimension, label in costs)
        pieces.append(f"cost={cost_text}")
        refs = tuple(getattr(invocation, "response_refs", ()))
        if refs:
            pieces.append("provenance=" + ",".join(str(item) for item in refs[:3]))
        else:
            pieces.append("provenance=none")

    recommendation = trace.get("recommendation")
    if isinstance(recommendation, Mapping):
        pieces.append(
            f"recommendation {recommendation['invocation_id']} {recommendation['decision']}"
        )
        if recommendation.get("reason"):
            pieces.append(f"reason={recommendation['reason']}")
        pieces.append(f"action={invocation.tool_name} outcome={status} invocation={invocation.id}")
        refs = tuple(getattr(invocation, "response_refs", ()))
        if refs:
            pieces.append("empirical_refs=" + ",".join(str(item) for item in refs[:3]))
        else:
            pieces.append("empirical_refs=pending")
    return "; ".join(pieces)[:480]


def _bounded_reason(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_CHECKPOINT_REASON_CHARS
    )


__all__ = [
    "PURPOSE_MARKER",
    "advisor_checkpoint_summary",
    "format_advisor_trace_purpose",
    "split_advisor_trace_purpose",
]
