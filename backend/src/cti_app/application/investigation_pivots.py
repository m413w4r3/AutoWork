"""Build bounded, exact-argument passive pivots from verified indicators."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from cti_app.application.investigation_contracts import (
    AvailablePivotV1,
    ConfirmedIOCContextV1,
    ToolCoverageEntryV1,
    ToolSummaryV1,
)
from cti_app.application.investigation_pivot_frontier import PivotableIndicatorType
from cti_app.application.investigation_tools import ToolEffect
from cti_app.domain.investigation import (
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    thaw_json,
)

MAX_AVAILABLE_PIVOTS = 128
MAX_AVAILABLE_PIVOTS_BYTES = 24 * 1024
_COVERED_STATUSES = frozenset(
    {
        ToolInvocationStatus.PENDING,
        ToolInvocationStatus.RUNNING,
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.EMPTY,
        ToolInvocationStatus.UNAVAILABLE,
        ToolInvocationStatus.NEEDS_REVIEW,
    }
)
_RETRYABLE_VT_PASSIVE_READS = frozenset(
    {
        "vt.file_report",
        "vt.object_report",
        "vt.relationships",
        "vt.intelligence_search",
        "vt.behaviour",
        "vt.behaviour_pcap",
    }
)
_VT_PASSIVE_RETRY_TRUSTED_PARAMETER = "_vt_passive_retry_adapter_trusted"


def available_investigation_pivots(
    *,
    inventory: Sequence[ConfirmedIOCContextV1],
    discovered_inventory: Sequence[tuple[PivotableIndicatorType, str]] = (),
    tools: Sequence[ToolSummaryV1],
    tool_coverage: Sequence[ToolCoverageEntryV1],
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
    blocked_tool_reasons: Mapping[tuple[str, str], str] | None = None,
    retryable_vt_tool_versions: frozenset[tuple[str, str]] | None = None,
) -> tuple[tuple[AvailablePivotV1, ...], bool]:
    """Return only valid read-tool calls whose exact normalized arguments are not covered."""
    read_tools = {
        (item.name, item.schema_version): item
        for item in tools
        if item.effect in {ToolEffect.LOCAL_READ, ToolEffect.EXTERNAL_READ}
    }
    blocked_tool_reasons = blocked_tool_reasons or {}
    from cti_app.application.investigation_vt_tools import VT_PASSIVE_RETRY_TOOL_NAMES

    blocked_retry_arguments: set[tuple[str, bytes]] = set()
    for invocation in invocations:
        if (
            invocation.status is not ToolInvocationStatus.NEEDS_REVIEW
            or invocation.tool_name not in VT_PASSIVE_RETRY_TOOL_NAMES
        ):
            continue
        parameters = thaw_json(invocation.normalized_parameters)
        if not isinstance(parameters, Mapping):
            continue
        persisted_trust = invocation.passive_read_retry_trusted
        if persisted_trust is None:
            persisted_trust = parameters.get(_VT_PASSIVE_RETRY_TRUSTED_PARAMETER)
        legacy_builtin = (
            invocation.passive_read_retry_trusted is None
            and _VT_PASSIVE_RETRY_TRUSTED_PARAMETER not in parameters
            and invocation.schema_version == "1"
        )
        if persisted_trust is True or legacy_builtin:
            continue
        blocked_retry_arguments.add(
            (
                invocation.tool_name,
                canonical_json_bytes(_tool_arguments(parameters)),
            )
        )
    visible_coverage = {item.invocation_id: item for item in tool_coverage}
    covered_tools_by_indicator: dict[tuple[str, str], tuple[str, ...]] = {}
    latest_observation_by_indicator: dict[tuple[str, str], EvidenceObservation] = {}
    indicator_order: dict[tuple[str, str], int] = {}
    retryable_tools = retryable_vt_tool_versions
    if retryable_tools is None:
        retryable_tools = frozenset(
            key for key in read_tools if key[0] in _RETRYABLE_VT_PASSIVE_READS
        )
    covered_arguments = {
        (
            item.tool_name,
            item.schema_version,
            canonical_json_bytes(_tool_arguments(item.normalized_parameters)),
        )
        for item in invocations
        if item.status in _COVERED_STATUSES
        and not (
            item.status is ToolInvocationStatus.NEEDS_REVIEW
            and item.tool_name in _RETRYABLE_VT_PASSIVE_READS
            and (item.tool_name, item.schema_version) in read_tools
        )
        and (
            item.id not in visible_coverage or visible_coverage[item.id].status in _COVERED_STATUSES
        )
    }
    observation_ids_by_indicator: dict[tuple[str, str], list[str]] = {}
    observations_by_id = {str(item.id): item for item in observations}
    for observation in observations:
        object_type = observation.object_type.casefold()
        object_value = (
            observation.object_id
            if object_type in {"url", "urls"}
            else observation.object_id.casefold()
        )
        key = (object_type, object_value)
        observation_ids_by_indicator.setdefault(key, []).append(str(observation.id))

    candidates: dict[tuple[str, str, str], AvailablePivotV1] = {}
    document_backed_indicators = {
        (item.indicator_type, _indicator_value_key(item.indicator_type, item.value))
        for item in inventory
        if item.source_document_ids
    }

    def add(
        *,
        tool_name: str,
        indicator_type: PivotableIndicatorType,
        indicator_value: str,
        arguments: Mapping[str, Any],
        schema_version: str | None = None,
        blocked_reason: str | None = None,
        source_observation_ids: tuple[UUID, ...] = (),
        covered_by_tools: tuple[str, ...] = (),
    ) -> None:
        normalized = canonical_json_bytes(dict(arguments))
        matching_tools = (
            (read_tools[(tool_name, schema_version)],)
            if schema_version is not None and (tool_name, schema_version) in read_tools
            else ()
            if schema_version is not None
            else tuple(tool for (name, _version), tool in read_tools.items() if name == tool_name)
        )
        for tool in matching_tools:
            identity = (tool_name, tool.schema_version, normalized)
            if (
                not _arguments_fit_schema(arguments, tool.arguments_schema)
                or identity in covered_arguments
            ):
                continue
            # The visible coverage index supplies statuses for displayed invocations;
            # the full persisted invocation record supplies the complete parameter map,
            # since parameter_key intentionally shows only one compact discriminator.
            tool_key = (tool_name, tool.schema_version)
            effective_block = (
                blocked_tool_reasons.get(tool_key)
                or blocked_reason
                or (
                    "ambiguous_tool_invocation_requires_reconciliation"
                    if (tool_name, normalized) in blocked_retry_arguments
                    else None
                )
            )
            pivot = AvailablePivotV1(
                tool_name=tool_name,
                schema_version=tool.schema_version,
                indicator_type=indicator_type,
                indicator_value=indicator_value,
                arguments=dict(arguments),
                execution_state="blocked" if effective_block else "available",
                blocked_reason=effective_block,
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_by_tools,
            )
            candidates[(tool_name, tool.schema_version, normalized.decode("utf-8"))] = pivot

    indicator_values = tuple((item.indicator_type, item.value) for item in inventory) + tuple(
        discovered_inventory
    )

    for kind, value in indicator_values:
        indicator_key = (kind, _indicator_value_key(kind, value))
        indicator_order.setdefault(indicator_key, len(indicator_order))
        linked_evidence = _linked_observations(
            kind, value, observations, observation_ids_by_indicator
        )
        source_observation_ids = tuple(UUID(item) for item in linked_evidence[:16])
        if linked_evidence:
            latest_observation_by_indicator[indicator_key] = observations_by_id[linked_evidence[-1]]
        covered_tools_by_indicator[indicator_key] = _covered_tools_for_indicator(
            kind,
            value,
            invocations,
            visible_coverage,
        )
        source_available = (
            bool(linked_evidence)
            or (
                kind,
                _indicator_value_key(kind, value),
            )
            in document_backed_indicators
        )
        if kind in {"md5", "sha1", "sha256"}:
            blocked_reason = "source_references_required" if not source_available else None
            add(
                tool_name="vt.file_report",
                indicator_type=kind,
                indicator_value=value,
                arguments={"file_hash": value},
                blocked_reason=blocked_reason,
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )
            for relation in ("contacted_ips", "contacted_domains"):
                add(
                    tool_name="vt.relationships",
                    indicator_type=kind,
                    indicator_value=value,
                    arguments={
                        "object_type": "file",
                        "object_id": value,
                        "relation": relation,
                    },
                    blocked_reason=blocked_reason,
                    source_observation_ids=source_observation_ids,
                    covered_by_tools=covered_tools_by_indicator[indicator_key],
                )
        elif kind == "ipv4":
            add(
                tool_name="vt.relationships",
                indicator_type=kind,
                indicator_value=value,
                arguments={
                    "object_type": "ip_address",
                    "object_id": value,
                    "relation": "resolutions",
                },
                blocked_reason=(None if source_available else "source_references_required"),
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )
            add(
                tool_name="shodan.host",
                indicator_type=kind,
                indicator_value=value,
                arguments={"ip": value},
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )
        elif kind == "domain":
            add(
                tool_name="vt.relationships",
                indicator_type=kind,
                indicator_value=value,
                arguments={
                    "object_type": "domain",
                    "object_id": value,
                    "relation": "resolutions",
                },
                blocked_reason=(None if source_available else "source_references_required"),
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )
        elif kind == "url":
            add(
                tool_name="vt.relationships",
                indicator_type=kind,
                indicator_value=value,
                arguments={
                    "object_type": "url",
                    "object_id": value,
                    "relation": "communicating_files",
                },
                blocked_reason=(None if source_available else "source_references_required"),
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )

        if linked_evidence and kind in {"md5", "sha1", "sha256", "ipv4", "domain"}:
            exact_search_arguments = {
                "query": value,
                "justification": "Pivot from this exact confirmed indicator.",
                "evidence_refs": list(linked_evidence[:16]),
            }
            add(
                tool_name="vt.intelligence_search",
                indicator_type=kind,
                indicator_value=value,
                arguments=exact_search_arguments,
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )
            if kind in {"ipv4", "domain"}:
                add(
                    tool_name="shodan.count",
                    indicator_type=kind,
                    indicator_value=value,
                    arguments=exact_search_arguments,
                    source_observation_ids=source_observation_ids,
                    covered_by_tools=covered_tools_by_indicator[indicator_key],
                )

    # A reviewed passive behaviour read can be offered again as a distinct
    # invocation. Only rebuild the canonical hash argument; never carry model
    # supplied reference metadata or a provider cursor into the new occurrence.
    for invocation in invocations:
        if (
            invocation.status is not ToolInvocationStatus.NEEDS_REVIEW
            or invocation.tool_name != "vt.behaviour"
            or (invocation.tool_name, invocation.schema_version) not in read_tools
        ):
            continue
        previous_arguments = _tool_arguments(invocation.normalized_parameters)
        if not isinstance(previous_arguments, Mapping):
            continue
        previous_hash = previous_arguments.get("file_hash")
        if not isinstance(previous_hash, str):
            continue
        for kind, value in indicator_values:
            if (
                kind not in {"md5", "sha1", "sha256"}
                or value.casefold() != previous_hash.casefold()
            ):
                continue
            linked_evidence = _linked_observations(
                kind, value, observations, observation_ids_by_indicator
            )
            indicator_key = (kind, _indicator_value_key(kind, value))
            indicator_order.setdefault(indicator_key, len(indicator_order))
            source_observation_ids = tuple(UUID(item) for item in linked_evidence[:16])
            covered_tools_by_indicator.setdefault(
                indicator_key,
                _covered_tools_for_indicator(kind, value, invocations, visible_coverage),
            )
            source_available = (
                bool(linked_evidence)
                or (
                    kind,
                    _indicator_value_key(kind, value),
                )
                in document_backed_indicators
            )
            retry_blocker = None
            if not source_available:
                retry_blocker = "source_references_required"
            elif (invocation.tool_name, invocation.schema_version) not in retryable_tools:
                retry_blocker = blocked_tool_reasons.get(
                    (invocation.tool_name, invocation.schema_version),
                    "ambiguous_tool_invocation_requires_reconciliation",
                )
            add(
                tool_name="vt.behaviour",
                indicator_type=kind,
                indicator_value=value,
                arguments={"file_hash": value},
                schema_version=invocation.schema_version,
                blocked_reason=retry_blocker,
                source_observation_ids=source_observation_ids,
                covered_by_tools=covered_tools_by_indicator[indicator_key],
            )

    ordered = _order_candidates_fairly(
        candidates.values(),
        latest_observation_by_indicator=latest_observation_by_indicator,
        indicator_order=indicator_order,
    )
    result: list[AvailablePivotV1] = []
    used_bytes = 2
    truncated = len(ordered) > MAX_AVAILABLE_PIVOTS
    for pivot in ordered:
        item_bytes = len(canonical_json_bytes(pivot.model_dump(mode="json"))) + 1
        if (
            len(result) >= MAX_AVAILABLE_PIVOTS
            or used_bytes + item_bytes > MAX_AVAILABLE_PIVOTS_BYTES
        ):
            truncated = True
            continue
        result.append(pivot)
        used_bytes += item_bytes
    return tuple(result), truncated


def _arguments_fit_schema(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> bool:
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        return False
    if not set(arguments).issubset(properties) or not set(required).issubset(arguments):
        return False
    if schema.get("additionalProperties") is False and set(arguments).difference(properties):
        return False
    for key, value in arguments.items():
        property_schema = properties.get(key)
        if not isinstance(property_schema, Mapping):
            return False
        if property_schema.get("type") == "string" and not isinstance(value, str):
            return False
        if property_schema.get("type") == "array" and not isinstance(value, list):
            return False
        enum = property_schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            return False
    return True


def _tool_arguments(value: Any) -> Any:
    parameters = thaw_json(value)
    if isinstance(parameters, Mapping):
        return {key: item for key, item in parameters.items() if not str(key).startswith("_")}
    return parameters


def _linked_observations(
    indicator_type: str,
    value: str,
    observations: Sequence[EvidenceObservation],
    observation_ids_by_indicator: Mapping[tuple[str, str], list[str]],
) -> tuple[str, ...]:
    object_types = {
        "md5": "file",
        "sha1": "file",
        "sha256": "file",
        "ipv4": "ip",
        "domain": "domain",
        "url": "url",
    }
    object_type = object_types[indicator_type]
    aliases = {
        "file": ("file", "files"),
        "ip": ("ip", "ip_address", "ip_addresses", "ipv4"),
        "domain": ("domain", "domains"),
        "url": ("url", "urls"),
    }[object_type]
    value_key = _indicator_value_key(indicator_type, value)
    ids = {
        observation_id
        for alias in aliases
        for observation_id in observation_ids_by_indicator.get((alias, value_key), ())
    }
    ordered = [
        str(item.id)
        for item in sorted(observations, key=lambda item: (item.observed_at, str(item.id)))
        if str(item.id) in ids
    ]
    return tuple(ordered)


def _covered_tools_for_indicator(
    indicator_type: str,
    value: str,
    invocations: Sequence[ToolInvocation],
    visible_coverage: Mapping[UUID, ToolCoverageEntryV1],
) -> tuple[str, ...]:
    successful_statuses = {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.EMPTY}
    covered: set[str] = set()
    for invocation in invocations:
        coverage = visible_coverage.get(invocation.id)
        status = coverage.status if coverage is not None else invocation.status
        if status in successful_statuses and _invocation_matches_indicator(
            invocation, indicator_type, value
        ):
            covered.add(invocation.tool_name)
    return tuple(sorted(covered)[:256])


def _invocation_matches_indicator(
    invocation: ToolInvocation,
    indicator_type: str,
    value: str,
) -> bool:
    parameters = _tool_arguments(invocation.normalized_parameters)
    if not isinstance(parameters, Mapping):
        return False

    expected = _indicator_value_key(indicator_type, value)

    def matches(candidate: Any) -> bool:
        return (
            isinstance(candidate, str)
            and _indicator_value_key(indicator_type, candidate) == expected
        )

    direct_keys = {
        "md5": ("file_hash", "query"),
        "sha1": ("file_hash", "query"),
        "sha256": ("file_hash", "query"),
        "ipv4": ("ip", "ip_address", "query"),
        "domain": ("domain", "query"),
        "url": ("url", "query"),
    }[indicator_type]
    if any(matches(parameters.get(key)) for key in direct_keys):
        return True

    object_type = parameters.get("object_type")
    object_id = parameters.get("object_id")
    aliases = {
        "md5": {"file", "files"},
        "sha1": {"file", "files"},
        "sha256": {"file", "files"},
        "ipv4": {"ip", "ip_address", "ip_addresses", "ipv4"},
        "domain": {"domain", "domains"},
        "url": {"url", "urls"},
    }[indicator_type]
    return isinstance(object_type, str) and object_type.casefold() in aliases and matches(object_id)


def _order_candidates_fairly(
    candidates: Iterable[AvailablePivotV1],
    *,
    latest_observation_by_indicator: Mapping[tuple[str, str], EvidenceObservation],
    indicator_order: Mapping[tuple[str, str], int],
) -> tuple[AvailablePivotV1, ...]:
    groups: dict[tuple[str, str], list[AvailablePivotV1]] = {}
    for pivot in candidates:
        indicator_key = (
            pivot.indicator_type,
            _indicator_value_key(pivot.indicator_type, pivot.indicator_value),
        )
        groups.setdefault(indicator_key, []).append(pivot)

    ordered: list[AvailablePivotV1] = []
    for state in ("available", "blocked"):
        for has_source in (True, False):
            group_keys = [
                key
                for key, pivots in groups.items()
                if bool(pivots[0].source_observation_ids) is has_source
                and any(pivot.execution_state == state for pivot in pivots)
            ]
            group_keys.sort(key=lambda key: indicator_order.get(key, 0))
            if has_source:

                def observation_recency(key: tuple[str, str]) -> tuple[datetime, str]:
                    observation = latest_observation_by_indicator[key]
                    return observation.observed_at, str(observation.id)

                group_keys.sort(key=observation_recency, reverse=True)

            queues: dict[tuple[str, str], list[AvailablePivotV1]] = {}
            for key in group_keys:
                queues[key] = sorted(
                    (pivot for pivot in groups[key] if pivot.execution_state == state),
                    key=lambda pivot: (
                        pivot.tool_name,
                        pivot.schema_version,
                        canonical_json_bytes(pivot.arguments),
                    ),
                )
            while any(queues.values()):
                for key in group_keys:
                    if queues[key]:
                        ordered.append(queues[key].pop(0))
    return tuple(ordered)


def _indicator_value_key(indicator_type: str, value: str) -> str:
    return value if indicator_type == "url" else value.casefold()
