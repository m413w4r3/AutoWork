"""Non-blocking suggestions for operator use of the Bridge advisor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from cti_app.application.advisor_checkpoint import split_advisor_trace_purpose
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
PLAN_GUIDANCE_TOOL_THRESHOLD = 3
VT_PIVOT_GUIDANCE_THRESHOLD = 5


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
        "suggested_consultation_type": consultation_type
        if consultation_type in ADVISOR_CONSULTATION_TYPES
        else None,
        "last_advice": latest_advice,
        "bridge_usage": _bridge_usage(ordered),
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
        "before_vt_pivot_campaign": (
            "Avant VT: demander une stratégie de pivots discriminants; hypothèse → pivot → "
            "succès/échec/coût; absence ≠ négatif."
        ),
        "plan_investigation_due": (
            "Analyses en cours: demander un plan Bridge pour prioriser la suite."
        ),
        "vt_pivot_campaign_due": (
            "Pivots VT répétés: demander une stratégie Bridge avant de poursuivre."
        ),
        "before_yara_proposal": (
            "Règle YARA opérateur: demander un brouillon Bridge, puis l’importer."
        ),
        "suricata_draft_due": (
            "Modèle réseau exploitable: demander une règle Suricata à Bridge."
        ),
        "interpretation_after_result": (
            "Résultat important: demander une interprétation Bridge avant de conclure."
        ),
        "editorial_draft_due": (
            "Chaque bloc rédigé: demander un draft_text Bridge avant la version finale."
        ),
        "opinion_review_due": (
            "Revue obsolète: texte/version courants; hypothèse → pivot → succès/échec → coût; "
            "absence ≠ négatif."
        ),
    }
    if reason in targeted:
        return targeted[reason]
    return ADVISOR_GUIDANCE_REMINDER


def _bridge_usage(invocations: Sequence[Any]) -> dict[str, Any]:
    consultations = tuple(item for item in invocations if item.tool_name == ADVISOR_TOOL_NAME)
    counts: dict[str, int] = {}
    for item in consultations:
        consultation_type = _consultation_type(item)
        if consultation_type is not None:
            counts[consultation_type] = counts.get(consultation_type, 0) + 1

    last_consult = None
    last_position = max(
        (index for index, item in enumerate(invocations) if item.tool_name == ADVISOR_TOOL_NAME),
        default=-1,
    )
    if consultations:
        last = consultations[-1]
        timestamp = getattr(last, "updated_at", None) or getattr(last, "created_at", None)
        if timestamp is not None:
            last_consult = {
                "consultation_type": _consultation_type(last),
                "date": timestamp,
            }
    return {
        "consultations_by_type": counts,
        "total_consultations": len(consultations),
        "last_consult": last_consult,
        "tools_since_last_consult": sum(
            1
            for item in invocations[last_position + 1 :]
            if item.tool_name != ADVISOR_TOOL_NAME
        ),
    }


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


def _parameters(invocation: Any) -> Mapping[str, Any]:
    value = getattr(invocation, "normalized_parameters", {})
    return value if isinstance(value, Mapping) else {}


def _successful_consultation(invocation: Any, consultation_type: str) -> bool:
    return (
        invocation.tool_name == ADVISOR_TOOL_NAME
        and _consultation_type(invocation) == consultation_type
        and getattr(invocation.status, "value", invocation.status) in {"succeeded", "partial"}
    )


def _latest_successful_consultation_position(
    invocations: Sequence[Any], consultation_type: str
) -> int:
    return max(
        (
            index
            for index, item in enumerate(invocations)
            if _successful_consultation(item, consultation_type)
        ),
        default=-1,
    )


def _is_analysis_tool(tool_name: str) -> bool:
    return tool_name.startswith(
        ("sample.", "archive.", "dotnet.", "apk.", "native.", "network.protocol.")
    )


def _analysis_positions(invocations: Sequence[Any]) -> tuple[int, ...]:
    return tuple(
        index
        for index, item in enumerate(invocations)
        if item.tool_name != ADVISOR_TOOL_NAME and _is_analysis_tool(item.tool_name)
    )


def _plan_checkpoint_position(invocations: Sequence[Any]) -> int | None:
    positions = _analysis_positions(invocations)
    latest_plan = _latest_successful_consultation_position(invocations, "plan_investigation")
    after_plan = tuple(index for index in positions if index > latest_plan)
    if len(after_plan) < PLAN_GUIDANCE_TOOL_THRESHOLD:
        return None
    count_at_checkpoint = (len(after_plan) // PLAN_GUIDANCE_TOOL_THRESHOLD) * (
        PLAN_GUIDANCE_TOOL_THRESHOLD
    )
    return after_plan[count_at_checkpoint - 1]


def _vt_pivot_checkpoint_position(invocations: Sequence[Any]) -> int | None:
    latest_pivot = _latest_successful_consultation_position(invocations, "pivot_strategy")
    known_hashes = {
        value.casefold()
        for item in invocations[: latest_pivot + 1]
        if item.tool_name == "vt.file_report"
        if isinstance((value := _parameters(item).get("file_hash")), str)
        and value
    }
    pivot_positions: list[int] = []
    for index, item in enumerate(invocations[latest_pivot + 1 :], start=latest_pivot + 1):
        if item.tool_name in {
            "vt.relationships",
            "vt.intelligence_search",
            "vt.object_report",
            "vt.behaviour",
        }:
            pivot_positions.append(index)
        elif item.tool_name == "vt.file_report":
            value = _parameters(item).get("file_hash")
            if isinstance(value, str) and value and value.casefold() not in known_hashes:
                known_hashes.add(value.casefold())
                pivot_positions.append(index)
    if len(pivot_positions) < VT_PIVOT_GUIDANCE_THRESHOLD:
        return None
    count_at_checkpoint = (len(pivot_positions) // VT_PIVOT_GUIDANCE_THRESHOLD) * (
        VT_PIVOT_GUIDANCE_THRESHOLD
    )
    return pivot_positions[count_at_checkpoint - 1]


def _latest_corpus_activity_position(invocations: Sequence[Any], end: int) -> int:
    return max(
        (
            index
            for index, item in enumerate(invocations[:end])
            if item.tool_name.startswith("vt.") or _is_analysis_tool(item.tool_name)
        ),
        default=-1,
    )


def _operator_yara_checkpoint_position(invocations: Sequence[Any]) -> int | None:
    source_positions = [
        index
        for index, item in enumerate(invocations)
        if item.tool_name in {"rule.propose", "rule.operator_syntax_fix", "rule.revise"}
    ]
    latest_bridge_import = max(
        (
            index
            for index, item in enumerate(invocations)
            if item.tool_name == "rule.propose_bridge"
        ),
        default=-1,
    )
    source_positions = [index for index in source_positions if index > latest_bridge_import]
    for index, item in enumerate(invocations):
        if item.tool_name != "rule.test":
            continue
        prior_writers = tuple(
            position
            for position, candidate in enumerate(invocations[:index])
            if candidate.tool_name
            in {"rule.propose", "rule.propose_bridge", "rule.operator_syntax_fix", "rule.revise"}
        )
        if not prior_writers or invocations[prior_writers[-1]].tool_name != "rule.propose_bridge":
            source_positions.append(index)
    return max(source_positions, default=None)


def _network_checkpoint_position(
    invocations: Sequence[Any], observations: Sequence[Any]
) -> int | None:
    positions = [
        index
        for index, item in enumerate(invocations)
        if item.tool_name == "network.protocol.document"
        or any(
            field in _parameters(item)
            for field in (
                "url",
                "uri",
                "user_agent",
                "user-agent",
                "port",
                "destination_port",
                "remote_port",
                "host",
                "domain",
                "network_strings",
                "http_header",
            )
        )
    ]
    network_object_types = {
        "protocol_model",
        "protocol_evidence_pack",
        "network_stream",
        "network_capture_report",
        "network_string",
        "network_indicator",
        "url",
        "uri",
        "user_agent",
        "user-agent",
        "http_header",
        "domain",
        "dns_name",
        "host",
        "ip",
        "port",
    }
    for observation in observations:
        source = str(getattr(observation, "source", "")).casefold()
        object_type = str(getattr(observation, "object_type", "")).casefold()
        if not (
            source.startswith("network.")
            or object_type in network_object_types
            or any(marker in object_type for marker in ("network", "protocol", "url", "user_agent"))
        ):
            continue
        position = _observation_invocation_position(invocations, observation)
        positions.append(position if position is not None else max(0, len(invocations) - 1))
    return max(positions, default=None)


def _unattributed_opinion_position(invocations: Sequence[Any]) -> int | None:
    positions: list[int] = []
    for index, item in enumerate(invocations):
        if item.tool_name not in {"opinion.compose", "opinion.revise", "opinion.note"}:
            continue
        if getattr(item.status, "value", item.status) not in {"succeeded", "partial"}:
            continue
        params = _parameters(item)
        raw_blocks = params.get("blocks")
        if item.tool_name == "opinion.note":
            unattributed = params.get("bridge_authored") is not True
        elif isinstance(raw_blocks, (list, tuple)):
            unattributed = any(
                not isinstance(block, Mapping) or block.get("bridge_authored") is not True
                for block in raw_blocks
            )
        else:
            unattributed = True
        if unattributed:
            positions.append(index)
    return max(positions, default=None)


def _important_result_position(
    invocations: Sequence[Any], observations: Sequence[Any]
) -> int | None:
    positions: list[int] = []
    for observation in observations:
        raw_kind = getattr(observation, "kind", None)
        kind = getattr(raw_kind, "value", raw_kind)
        object_type = str(getattr(observation, "object_type", "")).casefold()
        is_component_result = object_type == "apk_analysis" or "component" in object_type
        if kind != "negative_result" and not is_component_result:
            continue
        position = _observation_invocation_position(invocations, observation)
        if position is not None:
            positions.append(position)
    return max(positions, default=None)


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
    post_consult_positions = tuple(
        index
        for index in range(last_advisor_position + 1, len(invocations))
        if invocations[index].tool_name != ADVISOR_TOOL_NAME
    )
    post_consult = tuple(invocations[index] for index in post_consult_positions)

    failed = {"failed", "unavailable", "needs_review"}
    if len(post_consult) >= 2:
        left, right = post_consult[-2:]
        left_position = post_consult_positions[-2]
        if (
            left.tool_name == right.tool_name
            and getattr(left.status, "value", left.status) in failed
            and getattr(right.status, "value", right.status) in failed
            and not _checkpoint_disposition_after(invocations, "repeated_tool_block", left_position)
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
                if not _checkpoint_disposition_after(
                    invocations, "pivot_without_measurement", advisor_positions[-1]
                ):
                    return "pivot_without_measurement", "pivot_strategy"
        pivot_ids = {str(item) for item in pivot_observation_ids}
        for item in observations:
            observed_at = getattr(item, "observed_at", None)
            if (
                str(getattr(item, "id", "")) not in pivot_ids
                or observed_at is None
                or latest_advisor_at is None
                or observed_at <= latest_advisor_at
            ):
                continue
            trigger_position = _observation_invocation_position(invocations, item)
            acknowledged = (
                _checkpoint_disposition_after(invocations, "new_pivot_discovered", trigger_position)
                if trigger_position is not None
                else _checkpoint_disposition_after_time(
                    invocations, "new_pivot_discovered", observed_at
                )
            )
            if not acknowledged:
                return "new_pivot_discovered", "pivot_strategy"

    yara_trigger_position = _operator_yara_checkpoint_position(invocations)
    if yara_trigger_position is not None:
        latest_yara = _latest_successful_consultation_position(invocations, "yara_draft")
        latest_corpus_activity = _latest_corpus_activity_position(
            invocations, yara_trigger_position + 1
        )
        if (
            (latest_yara < latest_corpus_activity or latest_yara < 0)
            and not _checkpoint_disposition_after(
                invocations, "before_yara_proposal", yara_trigger_position
            )
        ):
            return "before_yara_proposal", "yara_draft"

    network_trigger_position = _network_checkpoint_position(invocations, observations)
    if network_trigger_position is not None:
        latest_suricata = _latest_successful_consultation_position(
            invocations, "suricata_draft"
        )
        if (
            latest_suricata < network_trigger_position
            and not _checkpoint_disposition_after(
                invocations, "suricata_draft_due", network_trigger_position
            )
        ):
            return "suricata_draft_due", "suricata_draft"

    opinion_trigger_position = _unattributed_opinion_position(invocations)
    if opinion_trigger_position is not None:
        latest_draft = _latest_successful_consultation_position(invocations, "draft_text")
        if (
            latest_draft < opinion_trigger_position
            and not _checkpoint_disposition_after(
                invocations, "editorial_draft_due", opinion_trigger_position
            )
        ):
            return "editorial_draft_due", "draft_text"

    important_result_position = _important_result_position(invocations, observations)
    if important_result_position is not None:
        interpreted_after = any(
            index > important_result_position
            and (
                _successful_consultation(item, "plan_investigation")
                or _successful_consultation(item, "pivot_strategy")
            )
            for index, item in enumerate(invocations)
        )
        if (
            not interpreted_after
            and not _checkpoint_disposition_after(
                invocations, "interpretation_after_result", important_result_position
            )
        ):
            return "interpretation_after_result", "pivot_strategy"

    vt_trigger_position = _vt_pivot_checkpoint_position(invocations)
    if vt_trigger_position is not None and not _checkpoint_disposition_after(
        invocations, "vt_pivot_campaign_due", vt_trigger_position
    ):
        return "vt_pivot_campaign_due", "pivot_strategy"

    plan_trigger_position = _plan_checkpoint_position(invocations)
    if plan_trigger_position is not None and not _checkpoint_disposition_after(
        invocations, "plan_investigation_due", plan_trigger_position
    ):
        return "plan_investigation_due", "plan_investigation"

    analyzed_positions = [
        index
        for index, item in enumerate(invocations)
        if _is_significant_decompilation(item.tool_name)
        and getattr(item.status, "value", item.status) in {"succeeded", "partial"}
    ]
    if analyzed_positions:
        latest_analysis_position = analyzed_positions[-1]
        later_consults = tuple(
            (index, item)
            for index, item in enumerate(
                invocations[latest_analysis_position + 1 :], start=latest_analysis_position + 1
            )
            if item.tool_name == ADVISOR_TOOL_NAME
        )
        # A qualifying consultation or same-reason disposition after the latest analysis
        # acknowledges this trigger; a later analysis starts a fresh trigger.
        if not any(
            _consultation_type(item) in {"plan_investigation", "pivot_strategy", "yara_draft"}
            for _, item in later_consults
        ) and not _checkpoint_disposition_after(
            invocations, "first_significant_decompilation", latest_analysis_position
        ):
            return "first_significant_decompilation", "plan_investigation"

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
            and not _checkpoint_disposition_after(
                invocations, "before_yara_proposal", latest_analysis_position
            )
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
        if not any(
            _consultation_type(item) == "yara_draft" for item in later_yara_advice
        ) and not _checkpoint_disposition_after(
            invocations, "yara_revision_review", latest_yara_position
        ):
            return "yara_revision_review", "yara_draft"
        # A measured rule gives invariants worth pivoting on; ask Bridge for a discriminating
        # search strategy before spending VT searches on them.
        if not any(
            _consultation_type(item) == "pivot_strategy" for item in later_yara_advice
        ) and not _checkpoint_disposition_after(
            invocations, "before_vt_pivot_campaign", latest_yara_position
        ):
            return "before_vt_pivot_campaign", "pivot_strategy"

    if analyzed_positions:
        latest_analysis_position = analyzed_positions[-1]
        later_analysis_advice = tuple(
            item
            for item in invocations[latest_analysis_position + 1 :]
            if item.tool_name == ADVISOR_TOOL_NAME
        )
        if not any(
            _consultation_type(item) == "draft_text" for item in later_analysis_advice
        ) and not _checkpoint_disposition_after(
            invocations, "editorial_draft_due", latest_analysis_position
        ):
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
        ) and not _checkpoint_disposition_after(
            invocations, "opinion_review_due", latest_opinion_position
        ):
            return "opinion_review_due", "review_text"

    if advisor_positions:
        if len(post_consult) >= checkpoint_tool_threshold:
            threshold_index = (len(post_consult) // checkpoint_tool_threshold) * (
                checkpoint_tool_threshold
            ) - 1
            threshold_position = post_consult_positions[threshold_index]
            if _checkpoint_disposition_after(
                invocations, "tool_threshold_reached", threshold_position
            ):
                return None, None
            return "tool_threshold_reached", "pivot_strategy"
        return None, None
    if _checkpoint_disposition_after(invocations, "no_advisor_consultation", -1):
        return None, None
    return "no_advisor_consultation", "plan_investigation"


def _checkpoint_disposition_after(
    invocations: Sequence[Any], reason: str, trigger_position: int
) -> bool:
    """A matching trace acknowledges the source invocation that triggered the checkpoint.

    The stable motif is the checkpoint ID prefix before ``@version``. A later source
    invocation starts a new trigger; a state-version change alone does not.
    """
    for item in invocations[trigger_position + 1 :]:
        purpose = getattr(item, "purpose", None)
        if not isinstance(purpose, str):
            continue
        try:
            trace, _human_purpose = split_advisor_trace_purpose(purpose, allow_server_denied=True)
        except ValueError:
            continue
        checkpoint = trace.get("checkpoint") if isinstance(trace, Mapping) else None
        if not isinstance(checkpoint, Mapping):
            continue
        checkpoint_id = checkpoint.get("id")
        if (
            isinstance(checkpoint_id, str)
            and checkpoint_id.partition("@")[0] == reason
            and checkpoint.get("status") in {"consulted", "skipped", "denied"}
        ):
            return True
    return False


def _checkpoint_disposition_after_time(
    invocations: Sequence[Any], reason: str, trigger_time: Any
) -> bool:
    for item in invocations:
        created_at = getattr(item, "created_at", None)
        if created_at is None or created_at <= trigger_time:
            continue
        if _checkpoint_disposition_after((item,), reason, -1):
            return True
    return False


def _observation_invocation_position(invocations: Sequence[Any], observation: Any) -> int | None:
    observation_id = str(getattr(observation, "id", ""))
    if not observation_id:
        return None
    for index in range(len(invocations) - 1, -1, -1):
        if any(
            str(reference) == observation_id
            for reference in getattr(invocations[index], "response_refs", ())
        ) or str(getattr(invocations[index], "id", "")) == str(
            getattr(observation, "invocation_ref", "")
        ):
            return index
    return None


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
