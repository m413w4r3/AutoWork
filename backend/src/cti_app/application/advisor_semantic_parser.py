"""Versioned, local semantic parsing for non-authoritative adviser output.

The v4 envelope may be JSON or the existing tolerant line-oriented wire. JSON
strings inside an opinion remain content; they are never required to be a
second serialization layer.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from cti_app.application.advisor_consultation import (
    SourceRef,
    build_source_ref_catalog,
    find_source_ref_conflicts,
    restricted_source_identities,
)
from cti_app.application.advisor_dossier import (
    AdvisorDossier,
    build_advisor_dossier,
    evaluate_advisor_evidence_preflight,
)
from cti_app.application.advisor_wire import advisor_wire_aliases, parse_advisor_wire
from cti_app.application.publication_secret_guard import contains_explicit_operational_secrets

ADVISOR_RESPONSE_SCHEMA_V4 = "advisor-response.v4"
ADVISOR_SOURCE_PARSER_VERSION_V4 = "advisor-source-parser.v4"
_ALIAS = re.compile(r"(?i)^[ERT][1-9][0-9]*$")
_SUPPORTED_STATUSES = frozenset({"succeeded", "partial", "refused", "invalid", "ambiguous"})
_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "consultation_type",
        "status",
        "opinion",
        "opinions",
        "source_aliases",
        "incomplete_units",
        "refusal_reason",
        "limitations",
    }
)
_OPINION_KEYS = frozenset({"unit_id", "text", "source_aliases", "status"})


class _DuplicateJSONKey(ValueError):
    pass


def parse_advisor_semantic_response(
    raw: str,
    *,
    consultation_type: str,
    prompt_data: Mapping[str, Any],
    operational_secret_values: Sequence[str] = (),
) -> dict[str, Any]:
    """Parse one v4 adviser response without I/O or canonical fact promotion."""
    raw_text = raw if isinstance(raw, str) else ""
    raw_bytes = raw_text.encode("utf-8")
    provenance: dict[str, Any] = {
        "raw_output": raw_text,
        "raw_output_bytes": len(raw_bytes),
        "raw_output_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_output_retained": True,
    }

    def terminal(
        status: str, *, reason: str | None = None, retain_raw: bool = True
    ) -> dict[str, Any]:
        raw_provenance = dict(provenance)
        if not retain_raw:
            raw_provenance["raw_output"] = None
            raw_provenance["raw_output_retained"] = False
        result = {
            "schema": ADVISOR_RESPONSE_SCHEMA_V4,
            "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
            "consultation_type": consultation_type,
            "status": status,
            "untrusted": True,
            "usable": False,
            "opinions": [],
            "candidates": [],
            "incomplete_units": [],
            "unresolved_refs": [],
            "validated_facts": [],
            "warnings": [],
            **raw_provenance,
        }
        if reason is not None:
            result["refusal_reason" if status == "refused" else "reason"] = reason
        return result

    if not isinstance(raw, str) or not isinstance(prompt_data, Mapping):
        return terminal("invalid", reason="input_invalid")
    if prompt_data.get("consultation_type") not in {None, consultation_type}:
        return terminal("invalid", reason="consultation_type_mismatch")

    if contains_explicit_operational_secrets(
        {"raw_output": raw_text, "prompt_data": dict(prompt_data)},
        operational_secret_values=operational_secret_values,
    ):
        return terminal("refused", reason="operational_secret_detected", retain_raw=False)

    try:
        dossier = build_advisor_dossier(prompt_data)
        completeness = evaluate_advisor_evidence_preflight(
            prompt_data, dossier, dossier.text, submitted=False
        )
    except (AttributeError, KeyError, RecursionError, TypeError, UnicodeError, ValueError):
        return terminal("invalid", reason="evidence_context_invalid")

    try:
        response = _decode_response(raw_text, consultation_type)
    except _DuplicateJSONKey:
        result = terminal("ambiguous", reason="duplicate_response_key")
        result["unresolved_refs"] = [
            {"alias": None, "reason": "ambiguous_response", "unit_id": None}
        ]
        return result
    except (AttributeError, TypeError, ValueError):
        return terminal("invalid", reason="response_invalid")
    if response is None:
        return terminal("invalid", reason="response_invalid")

    status_value = response.get("status")
    status = status_value.strip().casefold() if isinstance(status_value, str) else status_value
    if status not in _SUPPORTED_STATUSES:
        return terminal("invalid", reason="response_status_invalid")
    if status == "refused":
        return terminal("refused", reason="advisor_refused")
    if status == "invalid":
        return terminal("invalid", reason="advisor_marked_invalid")
    if status == "ambiguous":
        result = terminal("ambiguous", reason="advisor_marked_ambiguous")
        result["unresolved_refs"] = [
            {"alias": None, "reason": "advisor_marked_ambiguous", "unit_id": None}
        ]
        return result

    raw_manifest = prompt_data.get("dossier_manifest")
    conflicts = find_source_ref_conflicts(prompt_data, dossier, raw_manifest=raw_manifest)
    if conflicts:
        result = terminal("refused", reason="source_identity_conflict")
        result["source_conflicts"] = [
            {"kind": item.kind, "id": item.id, "sha256s": list(item.sha256s)} for item in conflicts
        ]
        result["evidence_preflight"] = completeness
        return result
    if _manifest_alias_conflicts(raw_manifest, dossier):
        result = terminal("ambiguous", reason="source_alias_conflict")
        result["unresolved_refs"] = [
            {"alias": alias, "reason": "ambiguous_alias", "unit_id": None}
            for alias in _manifest_alias_conflicts(raw_manifest, dossier)
        ]
        result["evidence_preflight"] = completeness
        return result

    canonical_context = dict(prompt_data)
    canonical_context["dossier_manifest"] = [dict(item) for item in dossier.manifest]
    aliases = advisor_wire_aliases(canonical_context)
    catalog = build_source_ref_catalog(
        canonical_context,
        dossier,
        raw_manifest=raw_manifest,
    )
    restricted = restricted_source_identities(prompt_data)
    raw_aliases = _raw_manifest_aliases(raw_manifest)

    opinions_raw = response.get("opinions")
    if not isinstance(opinions_raw, list):
        return terminal("invalid", reason="opinions_invalid")
    if len(opinions_raw) == 0 and status == "succeeded":
        return terminal("invalid", reason="opinions_missing")

    opinions: list[dict[str, Any]] = []
    incomplete_units: list[str] = list(response.get("incomplete_units", []))
    unresolved_refs: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    inferred_incomplete: set[str] = set()
    any_ambiguous_alias = False

    for item in opinions_raw:
        if not isinstance(item, Mapping) or set(item) - _OPINION_KEYS:
            return terminal("invalid", reason="opinion_shape_invalid")
        unit_id = item.get("unit_id")
        text = item.get("text")
        unit_status = item.get("status", "complete")
        if isinstance(unit_id, str):
            unit_id = unit_id.strip()
        if isinstance(unit_status, str):
            unit_status = unit_status.strip().casefold()
        if (
            not isinstance(unit_id, str)
            or not unit_id.strip()
            or unit_id in seen_units
            or not isinstance(text, str)
            or not text.strip()
            or unit_status not in {"complete", "partial"}
        ):
            return terminal(
                "ambiguous" if isinstance(unit_id, str) and unit_id in seen_units else "invalid",
                reason="opinion_unit_invalid",
            )
        seen_units.add(unit_id)
        aliases_raw = item.get("source_aliases", [])
        if not isinstance(aliases_raw, list) or not all(
            isinstance(alias, str) for alias in aliases_raw
        ):
            return terminal("invalid", reason="source_aliases_invalid")
        if unit_status == "partial":
            inferred_incomplete.add(unit_id)
        source_refs: list[dict[str, Any]] = []
        seen_aliases: set[str] = set()
        for raw_alias in aliases_raw:
            alias = raw_alias.strip().upper()
            if not _ALIAS.fullmatch(alias):
                unresolved_refs.append(
                    {"alias": raw_alias, "reason": "missing_alias", "unit_id": unit_id}
                )
                inferred_incomplete.add(unit_id)
                continue
            if alias in seen_aliases:
                continue
            seen_aliases.add(alias)
            source_ref, reason = _resolve_alias(
                alias,
                aliases=aliases,
                catalog=catalog,
                restricted=restricted,
                raw_aliases=raw_aliases,
            )
            if source_ref is None:
                unresolved_refs.append(
                    {"alias": alias, "reason": reason or "missing_alias", "unit_id": unit_id}
                )
                inferred_incomplete.add(unit_id)
                any_ambiguous_alias = any_ambiguous_alias or reason == "ambiguous_alias"
                continue
            source_refs.append(
                {
                    "alias": alias,
                    "kind": source_ref.kind,
                    "id": source_ref.id,
                    "sha256": source_ref.sha256,
                    **({"source_ref": source_ref.id} if source_ref.kind == "opinion" else {}),
                    "validated": False,
                }
            )
        if not source_refs:
            inferred_incomplete.add(unit_id)
        opinion = {
            "unit_id": unit_id,
            "text": text,
            "source_refs": source_refs,
            "untrusted": True,
            "validation_status": "unresolved" if unit_id in inferred_incomplete else "referenced",
        }
        opinions.append(opinion)

    incomplete_units.extend(sorted(inferred_incomplete))
    incomplete_units = list(dict.fromkeys(incomplete_units))
    if status == "partial" and not incomplete_units:
        return terminal("invalid", reason="partial_units_missing")
    if any_ambiguous_alias:
        status = "ambiguous"
        opinions = []
    elif incomplete_units or unresolved_refs:
        status = "partial"

    candidates = [
        {
            "unit_id": item["unit_id"],
            "text": item["text"],
            "source_refs": list(item["source_refs"]),
            "untrusted": True,
            "validated": False,
        }
        for item in opinions
    ]
    return {
        "schema": ADVISOR_RESPONSE_SCHEMA_V4,
        "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        "consultation_type": consultation_type,
        "status": status,
        "untrusted": True,
        "usable": bool(candidates) and status == "succeeded",
        "opinions": opinions,
        "candidates": candidates,
        "incomplete_units": incomplete_units,
        "unresolved_refs": unresolved_refs,
        "validated_facts": [],
        "warnings": [],
        "evidence_preflight": completeness,
        **provenance,
    }


def _decode_response(raw: str, consultation_type: str) -> dict[str, Any] | None:
    parse_text = raw.lstrip("\ufeff")
    stripped = parse_text.lstrip()
    if stripped.startswith("{"):
        try:
            response = json.loads(stripped, object_pairs_hook=_unique_object)
        except json.JSONDecodeError:
            return None
        if not isinstance(response, dict):
            return None
        if response.get("schema") != ADVISOR_RESPONSE_SCHEMA_V4:
            return None
        if response.get("consultation_type") != consultation_type:
            return None
        if set(response) - _RESPONSE_KEYS:
            return None
        if "opinions" in response:
            if not isinstance(response["opinions"], list):
                return None
        elif isinstance(response.get("opinion"), str):
            response["opinions"] = [
                {
                    "unit_id": "U1",
                    "text": response["opinion"],
                    "source_aliases": response.get("source_aliases", []),
                }
            ]
        else:
            response["opinions"] = []
        incomplete = response.get("incomplete_units", [])
        if not isinstance(incomplete, list) or not all(
            isinstance(unit_id, str) and unit_id.strip() for unit_id in incomplete
        ):
            return None
        response["incomplete_units"] = list(
            dict.fromkeys(unit_id.strip() for unit_id in incomplete)
        )
        return response

    document = parse_advisor_wire(parse_text)
    fields = document.fields
    schema = _single_field(fields, "schema")
    response_type = _single_field(fields, "type")
    status = _single_field(fields, "status")
    if schema != ADVISOR_RESPONSE_SCHEMA_V4 or response_type != consultation_type or status is None:
        return None
    opinions: list[dict[str, Any]] = []
    wire_incomplete: list[str] = list(fields.get("incomplete_unit", ()))
    if not all(isinstance(unit_id, str) and unit_id.strip() for unit_id in wire_incomplete):
        return None
    wire_incomplete = list(dict.fromkeys(unit_id.strip() for unit_id in wire_incomplete))
    active: dict[str, Any] | None = None
    top_text = _single_field(fields, "opinion")
    top_aliases = list(fields.get("ref", ()))
    for key, value in document.records:
        if key == "unit":
            if active is not None:
                opinions.append(active)
            active = {"unit_id": value, "text": "", "source_aliases": [], "status": "complete"}
        elif key == "unit_text" and active is not None:
            active["text"] = value
        elif key == "unit_ref" and active is not None:
            active["source_aliases"].append(value)
        elif key == "unit_status" and active is not None:
            active["status"] = value.casefold()
    if active is not None:
        opinions.append(active)
    elif top_text is not None:
        opinions = [
            {
                "unit_id": "U1",
                "text": top_text,
                "source_aliases": top_aliases,
                "status": "complete",
            }
        ]
    elif fields.get("unit"):
        return None
    return {
        "schema": schema,
        "consultation_type": response_type,
        "status": status.casefold(),
        "opinions": opinions,
        "incomplete_units": wire_incomplete,
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(key)
        result[key] = value
    return result


def _single_field(fields: Mapping[str, tuple[str, ...]], key: str) -> str | None:
    values = fields.get(key, ())
    return values[0] if len(values) == 1 else None


def _resolve_alias(
    alias: str,
    *,
    aliases: Mapping[str, Mapping[str, Any]],
    catalog: Any,
    restricted: AbstractSet[tuple[str, str]],
    raw_aliases: Mapping[str, tuple[Mapping[str, Any], ...]],
) -> tuple[SourceRef | None, str | None]:
    if alias.startswith(("E", "T")):
        item = aliases.get("evidence", {}).get(alias)
        if item is None:
            return None, "stale_alias" if alias in raw_aliases else "missing_alias"
        kind = item.get("kind")
        identifier = item.get("id")
        digest = item.get("sha256")
        if not all(isinstance(value, str) and value for value in (kind, identifier, digest)):
            return None, "stale_alias"
        if (kind, _unprefix(kind, identifier)) in restricted:
            return None, "restricted"
        if kind == "opinion":
            source_ref = item.get("source_ref")
            if not isinstance(source_ref, str) or source_ref != identifier:
                return None, "stale_alias"
        try:
            candidate = SourceRef(kind, identifier, digest)
        except ValueError:
            return None, "stale_alias"
        if any(
            _manifest_identity(item) != (candidate.kind, candidate.id, candidate.sha256)
            for item in raw_aliases.get(alias, ())
        ):
            return None, "stale_alias"
        resolved = catalog.resolve_alias(alias)
        if resolved != candidate:
            return None, "stale_alias"
        return candidate, None

    item = aliases.get("refs", {}).get(alias)
    if item is None:
        return None, "stale_alias" if alias in raw_aliases else "missing_alias"
    kind = item.get("kind")
    identifier = item.get("id")
    digest = item.get("sha256")
    if not all(isinstance(value, str) and value for value in (kind, identifier, digest)):
        return None, "stale_alias"
    if (kind, _unprefix(kind, identifier)) in restricted:
        return None, "restricted"
    try:
        candidate = SourceRef(kind, identifier, digest)
    except ValueError:
        return None, "stale_alias"
    if any(
        _manifest_identity(row) != (candidate.kind, candidate.id, candidate.sha256)
        for row in raw_aliases.get(alias, ())
    ):
        return None, "stale_alias"
    if candidate not in catalog.transmitted_refs:
        return None, "stale_alias"
    return candidate, None


def _manifest_alias_conflicts(raw_manifest: object, dossier: AdvisorDossier) -> tuple[str, ...]:
    raw_aliases = _manifest_aliases_from_rows(_manifest_rows(raw_manifest))
    conflicts = {
        alias
        for alias, items in raw_aliases.items()
        if len({_manifest_identity(item) for item in items}) > 1
    }
    current: dict[str, set[tuple[object, object, object]]] = {}
    for item in dossier.manifest:
        alias = item.get("alias")
        if isinstance(alias, str):
            current.setdefault(alias.upper(), set()).add(_manifest_identity(item))
    conflicts.update(alias for alias, values in current.items() if len(values) > 1)
    return tuple(sorted(conflicts))


def _raw_manifest_aliases(raw_manifest: object) -> dict[str, tuple[Mapping[str, Any], ...]]:
    return _manifest_aliases_from_rows(_manifest_rows(raw_manifest))


def _manifest_aliases_from_rows(
    manifest_rows: tuple[Mapping[str, Any], ...],
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in manifest_rows:
        alias = item.get("alias")
        if isinstance(alias, str):
            grouped.setdefault(alias.upper(), []).append(item)
    return {alias: tuple(items) for alias, items in grouped.items()}


def _manifest_identity(item: Mapping[str, Any]) -> tuple[object, object, object]:
    kind = item.get("kind")
    identifier = item.get("id")
    digest = item.get("sha256")
    if kind == "opinion" and isinstance(item.get("source_ref"), str):
        identifier = item["source_ref"]
    elif (
        kind == "article" and isinstance(identifier, str) and not identifier.startswith("article:")
    ):
        identifier = f"article:{identifier}"
    elif kind in {"measurement", "fact"}:
        kind = "observation"
        identifier = item.get("evidence_id")
        digest = item.get("evidence_sha256")
    return kind, identifier, digest


def _manifest_rows(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _unprefix(kind: str, identifier: str) -> str:
    prefix = f"{kind}:"
    return identifier.removeprefix(prefix)


__all__ = [
    "ADVISOR_RESPONSE_SCHEMA_V4",
    "ADVISOR_SOURCE_PARSER_VERSION_V4",
    "parse_advisor_semantic_response",
]
