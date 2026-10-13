"""Tool-registry adapters for the optional, subject-bounded VT Retrohunt flow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.retrohunt import (
    RETROHUNT_CAPABILITY,
    RetrohuntError,
    RetrohuntOutcome,
    RetrohuntService,
    RetrohuntSlotLimitError,
)
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalRetrohuntPort,
    VirusTotalRoutingPolicy,
)
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.retrohunt import RetrohuntReservationState
from cti_app.domain.virustotal import VirusTotalCapability, VirusTotalTransportKind

_UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_SHA256_PATTERN = "^[0-9a-f]{64}$"
_SUBMIT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "version_id": {"type": "string", "pattern": _UUID_PATTERN},
        "source_sha256": {"type": "string", "pattern": _SHA256_PATTERN},
        "corpus_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "corpus_sha256": {"type": "string", "pattern": _SHA256_PATTERN},
        "parameters": {"type": "object", "maxProperties": 32, "additionalProperties": True},
    },
    "required": ["version_id", "source_sha256", "corpus_id", "corpus_sha256", "parameters"],
}
_RESERVATION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {"reservation_id": {"type": "string", "pattern": _UUID_PATTERN}},
    "required": ["reservation_id"],
}
_TOOL_SCHEMAS = {
    "vt.retrohunt.submit": _SUBMIT_SCHEMA,
    "vt.retrohunt.status": _RESERVATION_SCHEMA,
    "vt.retrohunt.results": _RESERVATION_SCHEMA,
}


def retrohunt_tool_capabilities(
    capabilities: VirusTotalCapabilities,
    routing_policy: VirusTotalRoutingPolicy,
    provider: VirusTotalRetrohuntPort,
) -> frozenset[str]:
    """Expose Retrohunt only when config, effective route, and proxy adapter agree."""
    route = routing_policy.route_for(VirusTotalCapability.RETROHUNT)
    if (
        not capabilities.retrohunt
        or route is None
        or route.primary.transport is not VirusTotalTransportKind.PROXY
        or any(step.transport is not VirusTotalTransportKind.PROXY for step in route.steps())
        or not provider.available
    ):
        return frozenset()
    return frozenset({RETROHUNT_CAPABILITY})


def register_retrohunt_tools(registry: ToolRegistry, *, service: RetrohuntService) -> None:
    """Register the optional tools; the caller supplies effective capabilities separately."""
    for name, schema in _TOOL_SCHEMAS.items():
        effect = (
            ToolEffect.EXTERNAL_ACTIVE if name.endswith(".submit") else ToolEffect.EXTERNAL_READ
        )
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=effect,
                cost_model={},
                confidentiality=ToolConfidentiality.INTERNAL,
                limitations=(
                    "La soumission consomme un emplacement durable sur deux pour le sujet.",
                    "Chaque hit est enregistré comme candidat; aucun hit ne devient un membre "
                    "confirmé.",
                    "La règle exacte doit être testée, mesurée et explicitement autorisée par "
                    "revue humaine.",
                ),
                capability_key=RETROHUNT_CAPABILITY,
            ),
            _RetrohuntToolAdapter(service, name),
        )


class _RetrohuntToolAdapter(ToolAdapter):
    def __init__(self, service: RetrohuntService, name: str) -> None:
        self._service = service
        self._name = name

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        if RETROHUNT_CAPABILITY not in context.available_capabilities:
            return ToolResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                output={"status": "unavailable", "reason": "capability_unavailable"},
                message="No effective VirusTotal proxy route exposes Retrohunt.",
            )
        try:
            if self._name == "vt.retrohunt.submit":
                outcome = await self._service.submit(
                    investigation_id=context.investigation_id,
                    version_id=UUID(str(arguments["version_id"])),
                    source_sha256=str(arguments["source_sha256"]),
                    corpus_id=str(arguments["corpus_id"]),
                    corpus_sha256=str(arguments["corpus_sha256"]),
                    parameters=arguments["parameters"],
                    envelope=context.envelope,
                )
            elif self._name == "vt.retrohunt.status":
                outcome = await self._service.status(
                    investigation_id=context.investigation_id,
                    reservation_id=UUID(str(arguments["reservation_id"])),
                )
            else:
                outcome = await self._service.results(
                    investigation_id=context.investigation_id,
                    reservation_id=UUID(str(arguments["reservation_id"])),
                )
        except RetrohuntError as exc:
            status = (
                ToolInvocationStatus.UNAVAILABLE
                if exc.code == "capability_unavailable"
                else ToolInvocationStatus.POLICY_DENIED
                if exc.code
                in {
                    "policy_denied",
                    "rule_not_prevalidated",
                    "retrohunt_human_authorization_required",
                    "rule_test_quality_insufficient",
                }
                else ToolInvocationStatus.FAILED
            )
            counters = await _failure_slot_counters(self._service, context.investigation_id, exc)
            return ToolResult(
                status=status,
                output={
                    "status": status.value,
                    "reason": exc.code,
                    **counters,
                },
                message=str(exc),
            )
        except (KeyError, TypeError, ValueError):
            counters = await _failure_slot_counters(self._service, context.investigation_id)
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                output={"status": "failed", "reason": "invalid_input", **counters},
                message="Retrohunt arguments are invalid.",
            )
        except Exception:
            counters = await _failure_slot_counters(self._service, context.investigation_id)
            return ToolResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                output={"status": "unavailable", "reason": "provider_unavailable", **counters},
                message="The Retrohunt provider is unavailable for this operation.",
            )
        return _tool_result(outcome)


def _tool_result(outcome: RetrohuntOutcome) -> ToolResult:
    reservation = outcome.reservation
    state = reservation.state.value
    status = (
        ToolInvocationStatus.UNAVAILABLE
        if outcome.provider_error is not None
        or reservation.state is RetrohuntReservationState.RECONCILIATION_REQUIRED
        else ToolInvocationStatus.SUCCEEDED
    )
    return ToolResult(
        status=status,
        observation_refs=outcome.observation_refs,
        output={
            "status": status.value,
            "reservation_id": str(reservation.id),
            "slot_number": reservation.slot_number,
            "slots_consumed": outcome.slots_consumed,
            "slots_remaining": outcome.slots_remaining,
            "job_state": outcome.provider_job_state or state,
            "provider_error": (
                outcome.provider_error.value if outcome.provider_error is not None else None
            ),
            "provider_job_id": reservation.provider_job_id,
            "candidate_hits": [
                {"sha256": hit, "qualification": "candidate", "confirmed_member": False}
                for hit in outcome.candidate_hits
            ],
            "observation_refs": [str(reference) for reference in outcome.observation_refs],
        },
        message=(
            "The Retrohunt provider is temporarily unavailable."
            if outcome.provider_error is not None
            else "Submission outcome needs manual reconciliation."
            if reservation.state is RetrohuntReservationState.RECONCILIATION_REQUIRED
            else "Retrohunt result recorded; hits remain candidates."
            if outcome.candidate_hits
            else "Retrohunt operation completed."
        ),
    )


async def _failure_slot_counters(
    service: RetrohuntService,
    investigation_id: UUID,
    error: RetrohuntError | None = None,
) -> dict[str, int]:
    if isinstance(error, RetrohuntSlotLimitError):
        return {
            "slots_consumed": error.slots_consumed,
            "slots_remaining": error.slots_remaining,
        }
    try:
        consumed, remaining = await service.slot_usage(investigation_id)
    except Exception:
        return {}
    return {"slots_consumed": consumed, "slots_remaining": remaining}


__all__ = ["register_retrohunt_tools", "retrohunt_tool_capabilities"]
