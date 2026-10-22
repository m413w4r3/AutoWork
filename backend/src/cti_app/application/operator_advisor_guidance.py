"""Non-blocking suggestions for operator use of the Bridge advisor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from cti_app.domain.investigation import BudgetDimension

ADVISOR_TOOL_NAME = "advisor.consult"
ADVISOR_CONSULTATION_TYPES = frozenset(
    {
        "plan_investigation",
        "pivot_strategy",
        "yara_draft",
        "review_text",
        "draft_text",
        "suricata_draft",
        "figure_plan",
    }
)
ADVISOR_GUIDANCE_REMINDER = (
    "Suggestion facultative; Bridge non fiable. Hypothèse → pivot → succès/échec → coût; "
    "absence ≠ négatif; pas d'historique."
)
MAX_ADVISOR_GUIDANCE_POINTS = 5
MAX_ADVISOR_GUIDANCE_TEXT = 600
DEFAULT_CHECKPOINT_TOOL_THRESHOLD = 12


class EvidenceSummaryPort(Protocol):
    async def summary(self, observation: Any) -> Mapping[str, Any] | str | None: ...


async def build_advisor_guidance(
    *,
    invocations: Sequence[Any],
    observations: Sequence[Any],
    evidence_summaries: EvidenceSummaryPort | None,
    available_tools: Sequence[str],
    checkpoint_tool_threshold: int = DEFAULT_CHECKPOINT_TOOL_THRESHOLD,
    g2_bridge_turns_remaining: int | None = None,
    pivot_observation_ids: Sequence[Any] = (),
    enabled: bool = True,
) -> dict[str, Any]:
    ordered = tuple(
        sorted(
            invocations,
            key=lambda item: (
                getattr(item, "created_at", None),
                str(getattr(item, "id", "")),
            ),
        )
    )
    advisor_invocations = tuple(item for item in ordered if item.tool_name == ADVISOR_TOOL_NAME)
    used = sum(
        int(item.consumed_budget.get(BudgetDimension.BRIDGE_TURNS, 0))
        for item in advisor_invocations
    )
    g2_turns_remaining = (
        max(0, g2_bridge_turns_remaining) if g2_bridge_turns_remaining is not None else None
    )
    latest_advice = await _latest_advice(
        advisor_invocations, observations, evidence_summaries, available_tools
    )
    reason, consultation_type = (
        _checkpoint(
            ordered,
            observations=observations,
            pivot_observation_ids=pivot_observation_ids,
            checkpoint_tool_threshold=max(1, checkpoint_tool_threshold),
        )
        if enabled
        else (None, None)
    )
    exhausted_reason = (
        "g2_bridge_budget_exhausted"
        if g2_turns_remaining is not None and g2_turns_remaining < 1
        else None
    )
    if exhausted_reason is not None:
        reason, consultation_type = exhausted_reason, None

    guidance: dict[str, Any] = {
        "checkpoint_due": reason is not None,
        "reason": reason,
        # The strict context DTO currently accepts only the legacy four-value enum.
        # Keep the checkpoint reason visible without making context construction fail
        # until the DTO is extended in its separately scoped module.
        "suggested_consultation_type": consultation_type
        if consultation_type
        in {"plan_investigation", "pivot_strategy", "yara_draft", "review_text"}
        else None,
        "last_advice": latest_advice,
        "reminder": _checkpoint_reminder(reason, consultation_type),
        "advisor_turns_used": used,
        "advisor_turn_limit": None,
        "advisor_turns_remaining": None,
        "g2_bridge_turns_remaining": g2_turns_remaining,
        "truncated": False,
    }
    return guidance


def _checkpoint_reminder(reason: str | None, consultation_type: str | None) -> str:
    if reason is None:
        return ADVISOR_GUIDANCE_REMINDER
    if consultation_type is None:
        return "Suggestion Bridge indisponible: budget G2 épuisé; aucun appel requis."
    targeted = {
        "new_pivot_discovered": (
            "Hypothèse changée: hypothèse → pivot → succès/échec → coût; mesurer avant de "
            "conclure; absence ≠ négatif."
        ),
        "pivot_without_measurement": (
            "Pivot sans mesure: pas de conclusion; hypothèse → pivot → succès/échec → coût; "
            "absence ≠ négatif."
        ),
        "repeated_tool_block": (
            "Blocage répété: hypothèse → pivot → succès/échec → coût; autre mesure; "
            "absence ≠ négatif."
        ),
        "editorial_draft_due": (
            "Brouillon modifié: texte courant seul; avis obsolète. Hypothèse → pivot → "
            "succès/échec → coût; pas d'historique."
        ),
        "opinion_review_due": (
            "Revue obsolète: texte/version courants; hypothèse → pivot → succès/échec → coût; "
            "absence ≠ négatif."
        ),
    }
    if reason in targeted:
        return targeted[reason]
    return ADVISOR_GUIDANCE_REMINDER


async def _latest_advice(
    advisor_invocations: Sequence[Any],
    observations: Sequence[Any],
    evidence_summaries: EvidenceSummaryPort | None,
    available_tools: Sequence[str],
) -> dict[str, Any] | None:
    if evidence_summaries is None:
        return None
    observations_by_id = {getattr(item, "id", None): item for item in observations}
    for invocation in reversed(advisor_invocations):
        if getattr(invocation.status, "value", invocation.status) not in {"succeeded", "partial"}:
            continue
        normalized = getattr(invocation, "normalized_parameters", {})
        consultation_type = (
            normalized.get("consultation_type") if isinstance(normalized, Mapping) else None
        )
        for observation_id in reversed(tuple(getattr(invocation, "response_refs", ()))):
            observation = observations_by_id.get(observation_id)
            if observation is None or getattr(observation, "source", None) != ADVISOR_TOOL_NAME:
                continue
            try:
                payload = await evidence_summaries.summary(observation)
            except Exception:
                continue
            if not isinstance(payload, Mapping):
                continue
            if payload.get("schema") != "operator-advisor-call.v1":
                continue
            output = payload.get("output")
            if not isinstance(output, Mapping) or output.get("untrusted") is not True:
                continue
            if output.get("usable", True) is False or output.get("advisor_status") == "refused":
                continue
            persisted_type = payload.get("consultation_type", consultation_type)
            if persisted_type not in {
                "plan_investigation",
                "pivot_strategy",
                "yara_draft",
                "review_text",
            }:
                continue
            points = _key_points(output, available_tools)
            return {
                "consultation_type": consultation_type
                if consultation_type
                in {"plan_investigation", "pivot_strategy", "yara_draft", "review_text"}
                else persisted_type,
                "date": invocation.updated_at,
                "key_points": points,
                "invocation_id": invocation.id,
            }
    return None


def _key_points(output: Mapping[str, Any], available_tools: Sequence[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    summary = output.get("summary")
    if isinstance(summary, str) and summary.strip():
        candidates.append(summary.strip())
    for block in output.get("blocks", ()):
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("block_type")
        sentences = block.get("sentences", ())
        if isinstance(sentences, (list, tuple)):
            first = next(
                (
                    item.get("text")
                    for item in sentences
                    if isinstance(item, Mapping) and isinstance(item.get("text"), str)
                ),
                None,
            )
            if isinstance(first, str) and first.strip():
                candidates.append(f"{block_type}: {first.strip()}")
    figure = output.get("figure_plan")
    if isinstance(figure, Mapping) and isinstance(figure.get("title"), str):
        candidates.append(f"figure_plan: {figure['title']}")
    refusal = output.get("refusal_reason")
    if isinstance(refusal, str) and refusal.strip():
        candidates.append(f"Refus: {refusal.strip()}")
    for recommendation in output.get("recommended_next_tools", ()):
        if not isinstance(recommendation, Mapping):
            continue
        tool = recommendation.get("tool")
        why = recommendation.get("why")
        if isinstance(tool, str) and tool in available_tools and isinstance(why, str):
            candidates.append(f"{tool}: {why}")
    for group, prefix in (("risks", "Risque"), ("questions", "Question")):
        values = output.get(group, ())
        if isinstance(values, (list, tuple)):
            candidates.extend(
                f"{prefix}: {value.strip()}"
                for value in values
                if isinstance(value, str) and value.strip()
            )
    points: list[str] = []
    remaining = MAX_ADVISOR_GUIDANCE_TEXT
    seen: set[str] = set()
    for candidate in candidates:
        if len(points) >= MAX_ADVISOR_GUIDANCE_POINTS:
            break
        text = " ".join(candidate.split())
        normalized = text.casefold()
        if not text or normalized in seen:
            continue
        seen.add(normalized)
        if not remaining:
            break
        text = text[:remaining]
        if text:
            points.append(text)
            remaining -= len(text)
    return tuple(points)


def _checkpoint(
    invocations: Sequence[Any],
    *,
    observations: Sequence[Any],
    pivot_observation_ids: Sequence[Any],
    checkpoint_tool_threshold: int,
) -> tuple[str | None, str | None]:
    advisor_positions = [
        index for index, item in enumerate(invocations) if item.tool_name == ADVISOR_TOOL_NAME
    ]
    last_advisor_position = advisor_positions[-1] if advisor_positions else -1
    subsequent = tuple(invocations[last_advisor_position + 1 :])
    post_consult = tuple(item for item in subsequent if item.tool_name != ADVISOR_TOOL_NAME)

    failed = {"failed", "unavailable", "partial", "needs_review"}
    if len(post_consult) >= 2:
        left, right = post_consult[-2:]
        if (
            left.tool_name == right.tool_name
            and getattr(left.status, "value", left.status) in failed
            and getattr(right.status, "value", right.status) in failed
        ):
            return "repeated_tool_block", "pivot_strategy"

    if advisor_positions:
        latest_advisor = invocations[advisor_positions[-1]]
        latest_advisor_at = getattr(latest_advisor, "updated_at", None) or getattr(
            latest_advisor, "created_at", None
        )
        if _consultation_type(latest_advisor) == "pivot_strategy" and post_consult:
            response_statuses: dict[str, set[str]] = {}
            for invocation in post_consult:
                status = getattr(invocation.status, "value", invocation.status)
                for reference in getattr(invocation, "response_refs", ()):
                    response_statuses.setdefault(str(reference), set()).add(str(status))
            measured_after_pivot = any(
                str(getattr(observation, "id", "")) in response_statuses
                and getattr(observation, "observed_at", None) is not None
                and latest_advisor_at is not None
                and observation.observed_at > latest_advisor_at
                and (
                    "succeeded" in response_statuses[str(getattr(observation, "id", ""))]
                    or "partial" in response_statuses[str(getattr(observation, "id", ""))]
                    or getattr(getattr(observation, "kind", None), "value", None)
                    == "negative_result"
                )
                for observation in observations
            )
            if not measured_after_pivot:
                return "pivot_without_measurement", "pivot_strategy"
        pivot_ids = {str(item) for item in pivot_observation_ids}
        if any(
            str(getattr(item, "id", "")) in pivot_ids
            and getattr(item, "observed_at", None) is not None
            and latest_advisor_at is not None
            and item.observed_at > latest_advisor_at
            for item in observations
        ):
            return "new_pivot_discovered", "pivot_strategy"

    analyzed_positions = [
        index
        for index, item in enumerate(invocations)
        if _is_significant_decompilation(item.tool_name)
        and getattr(item.status, "value", item.status) in {"succeeded", "partial"}
    ]
    if analyzed_positions:
        first_analysis_position = analyzed_positions[0]
        later_consults = tuple(
            (index, item)
            for index, item in enumerate(
                invocations[first_analysis_position + 1 :], start=first_analysis_position + 1
            )
            if item.tool_name == ADVISOR_TOOL_NAME
        )
        if not any(
            _consultation_type(item) in {"plan_investigation", "pivot_strategy"}
            for _, item in later_consults
        ):
            return "first_significant_decompilation", "plan_investigation"

        latest_analysis_position = analyzed_positions[-1]
        later_after_analysis = invocations[latest_analysis_position + 1 :]
        later_analysis_advice = tuple(
            item for item in later_after_analysis if item.tool_name == ADVISOR_TOOL_NAME
        )
        rule_proposed_after_analysis = any(
            item.tool_name in {"rule.propose", "rule.revise"} for item in later_after_analysis
        )
        if (
            not any(_consultation_type(item) == "yara_draft" for item in later_analysis_advice)
            and not rule_proposed_after_analysis
        ):
            return "before_yara_proposal", "yara_draft"

    yara_positions = [
        index
        for index, item in enumerate(invocations)
        if item.tool_name in {"rule.test", "rule.revise"}
        and getattr(item.status, "value", item.status) in {"succeeded", "partial"}
    ]
    if yara_positions:
        latest_yara_position = yara_positions[-1]
        later_yara_advice = tuple(
            item
            for item in invocations[latest_yara_position + 1 :]
            if item.tool_name == ADVISOR_TOOL_NAME
        )
        if not any(_consultation_type(item) == "yara_draft" for item in later_yara_advice):
            return "yara_revision_review", "yara_draft"

    if analyzed_positions:
        latest_analysis_position = analyzed_positions[-1]
        later_analysis_advice = tuple(
            item
            for item in invocations[latest_analysis_position + 1 :]
            if item.tool_name == ADVISOR_TOOL_NAME
        )
        if not any(_consultation_type(item) == "draft_text" for item in later_analysis_advice):
            return "editorial_draft_due", "draft_text"

    opinion_positions = [
        index
        for index, item in enumerate(invocations)
        if item.tool_name in {"opinion.compose", "opinion.revise"}
        and getattr(item.status, "value", item.status) in {"succeeded", "partial"}
    ]
    if opinion_positions:
        latest_opinion_position = opinion_positions[-1]
        if not any(
            item.tool_name == ADVISOR_TOOL_NAME and _consultation_type(item) == "review_text"
            for item in invocations[latest_opinion_position + 1 :]
        ):
            return "opinion_review_due", "review_text"

    if advisor_positions:
        if len(post_consult) >= checkpoint_tool_threshold:
            return "tool_threshold_reached", "pivot_strategy"
        return None, None
    return "no_advisor_consultation", "plan_investigation"


def _is_significant_decompilation(tool_name: str) -> bool:
    normalized = tool_name.casefold()
    return normalized == "sample.analyze" or any(
        marker in normalized for marker in ("decompil", "ghidra", "ilspy")
    )


def _consultation_type(invocation: Any) -> str | None:
    normalized = getattr(invocation, "normalized_parameters", {})
    value = normalized.get("consultation_type") if isinstance(normalized, Mapping) else None
    return value if value in ADVISOR_CONSULTATION_TYPES else None


def bound_advisor_guidance(value: Any, *, max_bytes: int) -> tuple[Any, bool]:
    """Trim the optional prior-advice recap through context budget stages."""
    from cti_app.application.investigation_contracts import AdvisorGuidanceV1
    from cti_app.domain.investigation import canonical_json_bytes

    guidance = (
        value if isinstance(value, AdvisorGuidanceV1) else AdvisorGuidanceV1.model_validate(value)
    )
    if len(canonical_json_bytes(guidance.model_dump(mode="json"))) <= max_bytes:
        return guidance, False
    advice = guidance.last_advice
    if advice is not None:
        for limit in (240, 120, 48, 0):
            points = []
            remaining = limit
            for point in advice.key_points:
                if remaining <= 0:
                    break
                clipped = point[:remaining]
                if clipped:
                    points.append(clipped)
                    remaining -= len(clipped)
            candidate = guidance.model_copy(
                update={
                    "last_advice": advice.model_copy(update={"key_points": tuple(points)}),
                    "truncated": True,
                }
            )
            if len(canonical_json_bytes(candidate.model_dump(mode="json"))) <= max_bytes:
                return candidate, True
        candidate = guidance.model_copy(update={"last_advice": None, "truncated": True})
        if len(canonical_json_bytes(candidate.model_dump(mode="json"))) <= max_bytes:
            return candidate, True
    return guidance.model_copy(update={"truncated": True}), True


__all__ = [
    "ADVISOR_GUIDANCE_REMINDER",
    "DEFAULT_CHECKPOINT_TOOL_THRESHOLD",
    "bound_advisor_guidance",
    "build_advisor_guidance",
]
