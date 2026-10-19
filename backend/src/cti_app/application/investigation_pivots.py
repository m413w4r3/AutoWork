"""Build bounded, exact-argument passive pivots from verified indicators."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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


def available_investigation_pivots(
    *,
    inventory: Sequence[ConfirmedIOCContextV1],
    discovered_inventory: Sequence[tuple[PivotableIndicatorType, str]] = (),
    tools: Sequence[ToolSummaryV1],
    tool_coverage: Sequence[ToolCoverageEntryV1],
    invocations: Sequence[ToolInvocation],
    observations: Sequence[EvidenceObservation],
) -> tuple[tuple[AvailablePivotV1, ...], bool]:
    """Return only valid read-tool calls whose exact normalized arguments are not covered."""
    read_tools = {
        item.name: item
        for item in tools
        if item.effect in {ToolEffect.LOCAL_READ, ToolEffect.EXTERNAL_READ}
    }
    visible_coverage = {item.invocation_id: item for item in tool_coverage}
    covered_arguments = {
        (item.tool_name, canonical_json_bytes(thaw_json(item.normalized_parameters)))
        for item in invocations
        if item.status in _COVERED_STATUSES
        and (
            item.id not in visible_coverage or visible_coverage[item.id].status in _COVERED_STATUSES
        )
    }
    observation_ids_by_indicator: dict[tuple[str, str], list[str]] = {}
    for observation in observations:
        key = (observation.object_type.casefold(), observation.object_id.casefold())
        observation_ids_by_indicator.setdefault(key, []).append(str(observation.id))

    candidates: dict[tuple[str, str], AvailablePivotV1] = {}

    def add(
        *,
        tool_name: str,
        indicator_type: PivotableIndicatorType,
        indicator_value: str,
        arguments: Mapping[str, Any],
    ) -> None:
        tool = read_tools.get(tool_name)
        if tool is None or not _arguments_fit_schema(arguments, tool.arguments_schema):
            return
        normalized = canonical_json_bytes(dict(arguments))
        identity = (tool_name, normalized)
        if identity in covered_arguments:
            return
        # The visible coverage index supplies statuses for displayed invocations;
        # the full persisted invocation record supplies the complete parameter map,
        # since parameter_key intentionally shows only one compact discriminator.
        pivot = AvailablePivotV1(
            tool_name=tool_name,
            schema_version=tool.schema_version,
            indicator_type=indicator_type,
            indicator_value=indicator_value,
            arguments=dict(arguments),
        )
        candidates[(tool_name, normalized.decode("utf-8"))] = pivot

    indicator_values = tuple((item.indicator_type, item.value) for item in inventory) + tuple(
        discovered_inventory
    )

    for kind, value in indicator_values:
        linked_evidence = _linked_observations(
            kind, value, observations, observation_ids_by_indicator
        )
        if kind in {"md5", "sha1", "sha256"}:
            add(
                tool_name="vt.file_report",
                indicator_type=kind,
                indicator_value=value,
                arguments={"file_hash": value},
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
            )
            add(
                tool_name="shodan.host",
                indicator_type=kind,
                indicator_value=value,
                arguments={"ip": value},
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
            )
            if kind in {"ipv4", "domain"}:
                add(
                    tool_name="shodan.count",
                    indicator_type=kind,
                    indicator_value=value,
                    arguments=exact_search_arguments,
                )

    ordered = tuple(
        candidates[key] for key in sorted(candidates, key=lambda item: (item[0], item[1]))
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
    ids = {
        observation_id
        for alias in aliases
        for observation_id in observation_ids_by_indicator.get((alias, value.casefold()), ())
    }
    ordered = [
        str(item.id)
        for item in sorted(observations, key=lambda item: (item.observed_at, str(item.id)))
        if str(item.id) in ids
    ]
    return tuple(ordered)
