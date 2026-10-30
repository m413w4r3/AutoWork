"""Compact, read-only projection for the investigation activity cockpit."""

# ruff: noqa: RUF001 - French interface text uses typographic apostrophes.

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from cti_app.domain.investigation import (
    BudgetDimension,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.jobs import JobStatus

_OBSERVATIONS_PER_INVOCATION = 8
_ADVISOR_ACTIVITY_LABELS = {
    "plan_investigation": "Conseil Bridge · Plan d’enquête",
    "pivot_strategy": "Conseil Bridge · Stratégie de pivot",
    "yara_draft": "Conseil Bridge · Règle YARA",
    "review_text": "Conseil Bridge · Revue de texte",
    "draft_text": "Conseil Bridge · Rédaction",
    "suricata_draft": "Conseil Bridge · Règle Suricata",
    "figure_plan": "Conseil Bridge · Plan de figure",
}
_TOOL_ACTIVITY_LABELS = {
    "vt.file_report": "Rapport VT · vt.file_report",
    "vt.relationships": "Relations VT · vt.relationships",
    "vt.intelligence_search": "Recherche VT · vt.intelligence_search",
    "vt.object_report": "Rapport VT · vt.object_report",
    "vt.download_sample": "Acquisition VT · vt.download_sample",
    "sample.analyze": "Analyse locale · sample.analyze",
    "sample.set_qualification": "Qualification · sample.set_qualification",
    "archive.extract_member": "Extraction locale · archive.extract_member",
    "opinion.compose": "Avis analyste · opinion.compose",
    "opinion.note": "Note analyste · opinion.note",
    "opinion.revise": "Révision analyste · opinion.revise",
    "rule.propose_bridge": "Proposition de règle · rule.propose_bridge",
}


def build_investigation_activity_projection(
    *,
    investigation: Any,
    state: Any | None,
    envelope: Any | None,
    invocations: tuple[Any, ...],
    observations: tuple[Any, ...],
    jobs: tuple[Any, ...],
    findings: tuple[Any, ...],
    synthesis_sha256: str | None,
    next_cursor: str | None,
    has_more: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project persisted metadata only; never include parameters, blobs, or prompts."""
    current_time = now or datetime.now(UTC)
    observations_by_id = {item.id: item for item in observations}
    fingerprints = Counter(_parameter_fingerprint(item) for item in invocations)

    timeline: list[dict[str, Any]] = []
    for item in invocations:
        fingerprint = _parameter_fingerprint(item)
        linked_observations = [
            observations_by_id[identity]
            for identity in item.response_refs[:_OBSERVATIONS_PER_INVOCATION]
            if identity in observations_by_id
        ]
        timeline.append(
            {
                "id": item.id,
                "version": item.version,
                "tool_name": item.tool_name[:128],
                "schema_version": item.schema_version[:128],
                "activity_label": _activity_label(item.tool_name, item.normalized_parameters),
                "status": _value(item.status),
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "duration_seconds": None,
                "duration_quality": "unknown",
                "reserved_budget": _budget_amounts(item.reserved_budget),
                "consumed_budget": _budget_amounts(item.consumed_budget),
                "repeated_input_count_in_page": fingerprints[fingerprint],
                "observation_refs": list(item.response_refs[:_OBSERVATIONS_PER_INVOCATION]),
                "observations": [
                    {
                        "id": observation.id,
                        "kind": _value(observation.kind),
                        "sha256": observation.sha256,
                        "source": observation.source[:96],
                        "source_version": observation.source_version[:96],
                        "observed_at": observation.observed_at,
                    }
                    for observation in linked_observations
                ],
            }
        )

    activity_times: list[tuple[datetime, str]] = []
    investigation_updated_at = getattr(investigation, "updated_at", None)
    if investigation_updated_at is not None:
        activity_times.append((investigation_updated_at, "investigation_record"))
    activity_times.extend((item.updated_at, "invocation") for item in invocations)
    activity_times.extend((item.observed_at, "observation") for item in observations)
    activity_times.extend(
        (item.heartbeat_at, "backend_job_heartbeat")
        for item in jobs
        if item.heartbeat_at is not None
    )
    latest = max(activity_times, key=lambda item: item[0], default=None)

    active_job_count = sum(
        _value(item.status) in {JobStatus.QUEUED.value, JobStatus.RUNNING.value} for item in jobs
    )
    superseded_ids = {
        item.supersedes for item in findings if getattr(item, "supersedes", None) is not None
    }
    current_findings = tuple(item for item in findings if item.id not in superseded_ids)
    inconclusive_count = sum(
        _value(getattr(item, "assessment", None)) == "inconclusive" for item in current_findings
    )
    stop_reason = _value(
        getattr(state, "stop_reason", None)
        if state is not None
        else getattr(investigation, "stop_reason", None)
    )
    blockers = _project_blockers(
        stop_reason=stop_reason,
        invocations=invocations,
        inconclusive_count=inconclusive_count,
    )

    state_payload = None
    if state is not None:
        open_hypotheses = sum(
            _value(hypothesis.status) == "open" for hypothesis in state.hypotheses
        )
        state_payload = {
            "version": state.version,
            "turn_index": state.turn_index,
            "stop_reason": _value(state.stop_reason),
            "hypothesis_count": len(state.hypotheses),
            "open_hypothesis_count": open_hypotheses,
        }

    return {
        "schema_version": "investigation-activity.v1",
        "investigation": {
            "id": investigation.id,
            "subject_id": investigation.subject_id,
            "production_run_id": investigation.production_run_id,
            "synthesis_artifact_id": investigation.synthesis_artifact_id,
            "synthesis_sha256": synthesis_sha256,
            "status": _value(investigation.status),
            "version": investigation.version,
            "stop_reason": stop_reason,
            "execution_mode": _value(getattr(state, "execution_mode", None)),
        },
        "operator": {
            "principal": None,
            "model": None,
            "effort": None,
            "active_subagent_count": None,
            "metadata_source": "unavailable_in_autowork",
        },
        "working_state": state_payload,
        "last_activity_at": latest[0] if latest else None,
        "last_activity_source": latest[1] if latest else None,
        "active_backend_job_count": active_job_count,
        "budget": _project_budget(envelope),
        "timeline": timeline,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "blockers": blockers,
        "counts": {"inconclusive_findings": inconclusive_count},
        "generated_at": current_time,
    }


def _project_budget(envelope: Any | None) -> dict[str, dict[str, int | None]] | None:
    if envelope is None:
        return None
    budget = envelope.budget
    return {
        dimension.value: {
            "limit": budget.limits.get(dimension),
            "consumed": budget.consumed[dimension],
            "reserved": budget.reserved[dimension],
            "remaining": budget.remaining[dimension],
        }
        for dimension in BudgetDimension
    }


def _project_blockers(
    *, stop_reason: str | None, invocations: tuple[Any, ...], inconclusive_count: int
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if stop_reason == "permission_required":
        blockers.append(
            {
                "kind": "permission_missing",
                "code": stop_reason,
                "invocation_id": None,
                "message": "Une autorisation est requise pour poursuivre.",
                "next_action": "Examiner l’enveloppe G2 et la décision humaine dans le journal.",
            }
        )
    elif stop_reason == "model_unavailable":
        blockers.append(
            {
                "kind": "service_unavailable",
                "code": stop_reason,
                "invocation_id": None,
                "message": "Le modèle est indiqué comme indisponible.",
                "next_action": "Vérifier la disponibilité du fournisseur configuré.",
            }
        )
    elif stop_reason == "submission_needs_review" and not any(
        _value(item.status) == ToolInvocationStatus.NEEDS_REVIEW.value for item in invocations
    ):
        blockers.append(
            {
                "kind": "external_effect_ambiguous",
                "code": stop_reason,
                "invocation_id": None,
                "external_effect_state": "unknown",
                "message": (
                    "Un effet externe attend une réconciliation; son invocation n’apparaît "
                    "pas dans cette page."
                ),
                "next_action": (
                    "Charger les opérations précédentes pour retrouver son identifiant, puis "
                    "vérifier l’état réel avant réconciliation."
                ),
            }
        )
    elif stop_reason == "budget_exhausted":
        blockers.append(
            {
                "kind": "budget_limit",
                "code": stop_reason,
                "invocation_id": None,
                "message": "La limite de budget enregistrée est atteinte.",
                "next_action": "Examiner le budget et décider si une extension est justifiée.",
            }
        )
    elif stop_reason == "no_new_information":
        blockers.append(
            {
                "kind": "analytical_limitation",
                "code": stop_reason,
                "invocation_id": None,
                "message": "La boucle n’a pas enregistré de nouvelle information.",
                "next_action": "Revoir les hypothèses et les pistes restantes.",
            }
        )

    latest_by_tool: dict[str, Any] = {}
    for invocation in sorted(invocations, key=lambda item: item.updated_at, reverse=True):
        latest_by_tool.setdefault(invocation.tool_name, invocation)

    # Ambiguous effects stay open until their explicit reconciliation, even if a
    # later invocation of the same tool has a different operation key.
    for invocation in invocations:
        status = _value(invocation.status)
        if status == ToolInvocationStatus.NEEDS_REVIEW.value:
            blockers.append(
                {
                    "kind": "external_effect_ambiguous",
                    "code": status,
                    "invocation_id": invocation.id,
                    "external_effect_state": "unknown",
                    "message": (
                        "L’effet externe de cette invocation reste ambigu; son état réel "
                        "n’est pas confirmé."
                    ),
                    "next_action": (
                        "Vérifier l’état réel de cet identifiant puis réconcilier l’invocation."
                    ),
                }
            )

    for invocation in latest_by_tool.values():
        status = _value(invocation.status)
        if status == ToolInvocationStatus.POLICY_DENIED.value:
            blockers.append(
                {
                    "kind": "policy_block",
                    "code": status,
                    "invocation_id": invocation.id,
                    "message": "L’opération a été refusée par une règle de politique.",
                    "next_action": "Examiner le périmètre d’autorisation et la raison canonique.",
                }
            )
        elif status == ToolInvocationStatus.UNAVAILABLE.value:
            is_external = invocation.tool_name.startswith(("vt.", "advisor."))
            blockers.append(
                {
                    "kind": "service_unavailable" if is_external else "tool_not_executable",
                    "code": status,
                    "invocation_id": invocation.id,
                    "message": (
                        "L’outil a été enregistré comme indisponible; le détail du motif "
                        "n’est pas exposé ici."
                    ),
                    "next_action": (
                        "Vérifier la disponibilité de l’outil ou du service avant une "
                        "nouvelle tentative."
                    ),
                }
            )
        elif status == ToolInvocationStatus.FAILED.value:
            blockers.append(
                {
                    "kind": "technical_block",
                    "code": status,
                    "invocation_id": invocation.id,
                    "message": "L’invocation est enregistrée en échec technique.",
                    "next_action": (
                        "Examiner le détail canonique de l’invocation avant de relancer."
                    ),
                }
            )

    if inconclusive_count:
        blockers.append(
            {
                "kind": "evidence_insufficient",
                "code": "inconclusive_findings",
                "invocation_id": None,
                "message": (
                    f"{inconclusive_count} constat(s) reste(nt) sans preuve suffisante; "
                    "un résultat inconclusif n’est pas un bug."
                ),
                "next_action": (
                    "Clarifier l’hypothèse ou rechercher une preuve admissible supplémentaire."
                ),
            }
        )
    return blockers


def _parameter_fingerprint(invocation: Any) -> str:
    fingerprint_payload = {
        "tool_name": invocation.tool_name,
        "schema_version": invocation.schema_version,
        "parameters": invocation.normalized_parameters,
    }
    return hashlib.sha256(canonical_json_bytes(fingerprint_payload)).hexdigest()


def _activity_label(tool_name: str, parameters: Any) -> str:
    if tool_name == "advisor.consult":
        consultation_type = (
            parameters.get("consultation_type") if isinstance(parameters, Mapping) else None
        )
        if isinstance(consultation_type, str):
            return _ADVISOR_ACTIVITY_LABELS.get(
                consultation_type, "Conseil Bridge · advisor.consult"
            )
        return "Conseil Bridge · advisor.consult"
    return _TOOL_ACTIVITY_LABELS.get(tool_name, "Autre opération d’investigation")


def _budget_amounts(values: Any) -> dict[str, int]:
    return {str(_value(dimension)): amount for dimension, amount in values.items()}


def _value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


__all__ = ["build_investigation_activity_projection"]
