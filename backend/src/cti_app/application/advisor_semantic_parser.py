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
from cti_app.application.advisor_contracts import parse_typed_advisor_response
from cti_app.application.advisor_dossier import (
    AdvisorDossier,
    build_advisor_dossier,
    evaluate_advisor_evidence_preflight,
)
from cti_app.application.advisor_wire import advisor_wire_aliases, parse_advisor_wire
from cti_app.application.publication_secret_guard import (
    contains_explicit_operational_secrets,
    redact_explicit_operational_secrets,
)

ADVISOR_RESPONSE_SCHEMA_V4 = "advisor-response.v4"
ADVISOR_SOURCE_PARSER_VERSION_V4 = "advisor-source-parser.v4"
_ALIAS = re.compile(r"(?i)^[ERT][1-9][0-9]*$")
_SUPPORTED_STATUSES = frozenset({"succeeded", "partial", "refused", "invalid", "ambiguous"})
_MAX_RESPONSE_ITEMS = 64
_MAX_SOURCE_ALIASES = 64
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
        "untrusted",
        "summary",
        "text",
        "answer",
        "response",
    }
)
_OPINION_KEYS = frozenset({"unit_id", "text", "source_aliases", "status"})


class _DuplicateJSONKey(ValueError):
    pass


def _looks_like_exploitable_text(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped.casefold() in {
        "json",
        "empty",
        "n/a",
        "none",
        "null",
    }:
        return False
    words = re.findall(r"[\w]+", stripped)
    if len(words) > 128 and len({word.casefold() for word in words}) < min(16, len(words) // 10):
        return False
    return True


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
    redacted_raw, redacted_secret_count = redact_explicit_operational_secrets(
        raw_text, operational_secret_values=operational_secret_values
    )
    safe_raw_text = str(redacted_raw)
    provenance: dict[str, Any] = {
        "raw_output": safe_raw_text,
        "raw_output_bytes": len(raw_bytes),
        "raw_output_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "raw_output_retained": True,
        "raw_output_redacted": bool(redacted_secret_count),
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

    secret_check: dict[str, Any] = {"prompt_data": dict(prompt_data)}
    if consultation_type != "review_text":
        secret_check["raw_output"] = raw_text
    if contains_explicit_operational_secrets(
        secret_check,
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

    parse_warnings: list[str] = []
    try:
        response = _decode_response(safe_raw_text, consultation_type)
    except _DuplicateJSONKey:
        result = terminal("ambiguous", reason="duplicate_response_key")
        result["unresolved_refs"] = [
            {"alias": None, "reason": "ambiguous_response", "unit_id": None}
        ]
        return result
    except (AttributeError, TypeError, ValueError):
        response = None
    if (
        response is not None
        and response.get("_content_missing") is True
        and response.get("_wire_unstructured") is True
    ):
        response = None
    heuristic_review: dict[str, Any] | None = None
    heuristic_review_warnings: list[str] = []
    if response is None and consultation_type == "review_text":
        legacy_review = parse_typed_advisor_response(
            safe_raw_text,
            consultation_type="review_text",
            prompt_data=prompt_data,
            safe_text=lambda value, _limit: value.strip(),
        )
        if legacy_review is not None and legacy_review.get("usable") is True:
            heuristic_review = legacy_review
            heuristic_review_warnings = list(legacy_review.get("warnings", ()))
            response = {
                "status": "succeeded",
                "opinions": [
                    {
                        "unit_id": item["unit_id"],
                        "text": "\n".join(item.get("remarks", ())),
                        "source_aliases": [],
                    }
                    for item in legacy_review.get("reviews", ())
                    if isinstance(item, Mapping)
                    and isinstance(item.get("unit_id"), str)
                    and isinstance(item.get("remarks"), list)
                    and item.get("remarks")
                ],
                "incomplete_units": [],
            }
        else:
            response = None
    if response is None:
        prose = safe_raw_text.strip()
        if not _looks_like_exploitable_text(prose):
            return terminal("invalid", reason="response_invalid")
        response = {
            "status": "succeeded",
            "opinions": [{"unit_id": "U1", "text": prose, "source_aliases": []}],
            "incomplete_units": [],
        }
        parse_warnings.append("unstructured_response")
    raw_parse_warnings = response.pop("_normalization_warnings", ())
    if isinstance(raw_parse_warnings, Sequence) and not isinstance(raw_parse_warnings, str | bytes):
        parse_warnings.extend(item for item in raw_parse_warnings if isinstance(item, str))
    if response.pop("_content_missing", False) is True:
        return terminal("invalid", reason="response_text_missing")

    status_value = response.get("status")
    status = status_value.strip().casefold() if isinstance(status_value, str) else status_value
    if status not in _SUPPORTED_STATUSES:
        status = "succeeded"
        parse_warnings.append("response_status_missing_or_unrecognized")
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

    response_opinions = response.get("opinions")
    if consultation_type == "review_text" and (
        not isinstance(response_opinions, list) or not response_opinions
    ):
        legacy_review = parse_typed_advisor_response(
            safe_raw_text,
            consultation_type="review_text",
            prompt_data=prompt_data,
            safe_text=lambda value, _limit: value.strip(),
        )
        if legacy_review is None or legacy_review.get("usable") is not True:
            return terminal("invalid", reason="opinions_missing")
        heuristic_review = legacy_review
        heuristic_review_warnings = list(legacy_review.get("warnings", ()))
        response = {
            "status": "succeeded",
            "opinions": [
                {
                    "unit_id": item["unit_id"],
                    "text": "\n".join(item.get("remarks", ())),
                    "source_aliases": [],
                }
                for item in legacy_review.get("reviews", ())
                if isinstance(item, Mapping)
                and isinstance(item.get("unit_id"), str)
                and isinstance(item.get("remarks"), list)
                and item.get("remarks")
            ],
            "incomplete_units": [],
        }
        status = "succeeded"

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
        opinions_raw = []
        parse_warnings.append("opinions_missing_or_invalid; preserved raw response")
    if len(opinions_raw) == 0 and status == "succeeded":
        if (
            consultation_type == "review_text"
            and heuristic_review is not None
            and heuristic_review.get("unit_assignment_status") == "unstructured_overall"
        ):
            status = "partial"
        else:
            prose = safe_raw_text.strip()
            if not prose:
                return terminal("invalid", reason="opinions_missing")
            opinions_raw = [{"unit_id": "U1", "text": prose, "source_aliases": []}]
            parse_warnings.append("unstructured_response")

    opinions: list[dict[str, Any]] = []
    raw_incomplete_units = response.get("incomplete_units", [])
    incomplete_units = (
        list(raw_incomplete_units[:_MAX_RESPONSE_ITEMS])
        if isinstance(raw_incomplete_units, list)
        else []
    )
    if isinstance(raw_incomplete_units, list) and len(raw_incomplete_units) > _MAX_RESPONSE_ITEMS:
        parse_warnings.append(f"array_truncated:incomplete_units:{_MAX_RESPONSE_ITEMS}")
    unresolved_refs: list[dict[str, Any]] = []
    seen_units: set[str] = set()
    inferred_incomplete: set[str] = set()
    any_ambiguous_alias = False

    if len(opinions_raw) > _MAX_RESPONSE_ITEMS:
        parse_warnings.append(f"array_truncated:opinions:{_MAX_RESPONSE_ITEMS}")
        opinions_raw = opinions_raw[:_MAX_RESPONSE_ITEMS]
    for index, item in enumerate(opinions_raw):
        if isinstance(item, str):
            item = {"unit_id": f"U{index + 1}", "text": item, "source_aliases": []}
            parse_warnings.append(f"opinion_string_normalized:opinions[{index}]")
        if not isinstance(item, Mapping):
            parse_warnings.append(f"opinion_ignored:opinions[{index}]")
            continue
        unknown_fields = set(item) - _OPINION_KEYS - {"statement", "opinion", "summary", "refs"}
        for unknown_field in sorted(unknown_fields, key=str):
            safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", str(unknown_field))[:64]
            parse_warnings.append(f"unknown_field_ignored:opinions[{index}].{safe_field}")
        unit_id = item.get("unit_id")
        text = item.get("text", item.get("statement", item.get("opinion", item.get("summary"))))
        unit_status = item.get("status", "complete")
        if not isinstance(unit_id, str) or not unit_id.strip():
            unit_id = f"U{index + 1}"
            parse_warnings.append(f"unit_id_missing:opinions[{index}]")
        if isinstance(unit_id, str):
            unit_id = unit_id.strip()
        if isinstance(unit_status, str):
            unit_status = unit_status.strip().casefold()
        if isinstance(unit_id, str) and unit_id in seen_units:
            unit_id = f"U{index + 1}"
            if unit_id in seen_units:
                parse_warnings.append(f"duplicate_opinion_ignored:opinions[{index}]")
                continue
            parse_warnings.append(f"duplicate_unit_id_renumbered:opinions[{index}]")
        if not isinstance(text, str) or not text.strip():
            parse_warnings.append(f"opinion_text_missing:opinions[{index}]")
            continue
        if unit_status not in {"complete", "partial"}:
            unit_status = "complete"
            parse_warnings.append(f"unit_status_normalized:opinions[{index}]")
        seen_units.add(unit_id)
        aliases_raw = item.get("source_aliases", item.get("refs", []))
        if isinstance(aliases_raw, str):
            aliases_raw = [
                part.strip() for part in re.split(r"[,;\s]+", aliases_raw) if part.strip()
            ]
            parse_warnings.append(f"source_aliases_split:opinions[{index}]")
        if not isinstance(aliases_raw, list):
            aliases_raw = []
            parse_warnings.append(f"source_aliases_ignored:opinions[{index}]")
        if len(aliases_raw) > _MAX_SOURCE_ALIASES:
            aliases_raw = aliases_raw[:_MAX_SOURCE_ALIASES]
            parse_warnings.append(
                f"array_truncated:opinions[{index}].source_aliases:{_MAX_SOURCE_ALIASES}"
            )
        if any(not isinstance(alias, str) for alias in aliases_raw):
            aliases_raw = [alias for alias in aliases_raw if isinstance(alias, str)]
            parse_warnings.append(f"source_aliases_filtered:opinions[{index}]")
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
        if (
            not source_refs
            and consultation_type != "review_text"
            and not any(
                warning in parse_warnings
                for warning in (
                    "unstructured_response",
                    "schema_missing_or_mismatched",
                    "consultation_type_missing_or_mismatched",
                    "untrusted_marker_missing_or_mismatched",
                )
            )
        ):
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
    if status == "partial" and not incomplete_units and consultation_type != "review_text":
        parse_warnings.append("partial_units_marker_missing")
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
    normalized_reviews: list[dict[str, Any]] = []
    if consultation_type == "review_text":
        if heuristic_review is not None:
            normalized_reviews = [
                dict(item)
                for item in heuristic_review.get("reviews", ())
                if isinstance(item, Mapping)
            ]
        else:
            normalized_reviews = [
                {
                    "unit_id": item["unit_id"],
                    "remarks": [item["text"]],
                    "basis_refs": [
                        {"kind": ref["kind"], "id": ref["id"]}
                        for ref in item["source_refs"]
                        if isinstance(ref.get("kind"), str) and isinstance(ref.get("id"), str)
                    ],
                    "warnings": [],
                    "usable": True,
                    "assignment": "explicit_alias",
                }
                for item in opinions
                if isinstance(item.get("unit_id"), str) and isinstance(item.get("text"), str)
            ]
        if heuristic_review is not None or redacted_secret_count:
            status = "partial"
        if redacted_secret_count and "secret_redacted" not in heuristic_review_warnings:
            heuristic_review_warnings.append("secret_redacted")
    warnings = list(dict.fromkeys((*heuristic_review_warnings, *parse_warnings)))
    overall_review = (
        heuristic_review.get("overall_review")
        if heuristic_review is not None and isinstance(heuristic_review.get("overall_review"), str)
        else None
    )
    result = {
        "schema": ADVISOR_RESPONSE_SCHEMA_V4,
        "parser_version": ADVISOR_SOURCE_PARSER_VERSION_V4,
        "consultation_type": consultation_type,
        "status": status,
        "untrusted": True,
        "usable": (
            (
                any(item.get("usable") is True for item in normalized_reviews)
                or bool(overall_review and overall_review.strip())
            )
            and status in {"succeeded", "partial"}
            if consultation_type == "review_text"
            else bool(candidates) and status in {"succeeded", "partial"}
        ),
        "opinions": opinions,
        "candidates": candidates,
        "incomplete_units": incomplete_units,
        "unresolved_refs": unresolved_refs,
        "validated_facts": [],
        "warnings": warnings,
        "evidence_preflight": completeness,
        **provenance,
    }
    if consultation_type == "review_text":
        result["reviews"] = normalized_reviews
        if overall_review is not None:
            result["overall_review"] = overall_review
        result["unit_assignment_status"] = (
            heuristic_review.get("unit_assignment_status", "heuristic")
            if heuristic_review is not None
            else "structured"
        )
        result["normalization_status"] = (
            "partial" if warnings or status == "partial" else "complete"
        )
        result["warning_classification"] = {
            "blocking": [],
            "informative": warnings,
        }
    return result


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
        warnings: list[str] = []
        schema = response.get("schema")
        if schema != ADVISOR_RESPONSE_SCHEMA_V4:
            warnings.append("schema_missing_or_mismatched")
        response_type = response.get("consultation_type")
        if response_type != consultation_type:
            warnings.append("consultation_type_missing_or_mismatched")
        if response.get("untrusted") is not True:
            warnings.append("untrusted_marker_missing_or_mismatched")
        for field_name in sorted(set(response) - _RESPONSE_KEYS):
            safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", str(field_name))[:64] or "<unknown>"
            warnings.append(f"unknown_field_ignored:$.{safe_field}")
        status = response.get("status")
        if not isinstance(status, str) or status.strip().casefold() not in _SUPPORTED_STATUSES:
            warnings.append("status_missing_or_unrecognized")
            status = "succeeded"
        else:
            status = status.strip().casefold()

        raw_opinions = response.get("opinions")
        if isinstance(raw_opinions, list):
            opinions: list[Any] = raw_opinions
        elif raw_opinions is not None:
            opinions = [raw_opinions]
            warnings.append("opinions_non_array_normalized")
        else:
            opinion = next(
                (
                    response[key]
                    for key in ("opinion", "text", "summary", "answer", "response")
                    if isinstance(response.get(key), str) and response[key].strip()
                ),
                None,
            )
            opinions = (
                [
                    {
                        "unit_id": "U1",
                        "text": opinion,
                        "source_aliases": response.get("source_aliases", []),
                    }
                ]
                if opinion is not None
                else []
            )
        if isinstance(raw_opinions, list) and len(raw_opinions) > _MAX_RESPONSE_ITEMS:
            warnings.append(f"array_truncated:opinions:{_MAX_RESPONSE_ITEMS}")
            opinions = raw_opinions[:_MAX_RESPONSE_ITEMS]

        incomplete = response.get("incomplete_units", [])
        if not isinstance(incomplete, list):
            incomplete = []
            warnings.append("incomplete_units_non_array_ignored")
        if len(incomplete) > _MAX_RESPONSE_ITEMS:
            incomplete = incomplete[:_MAX_RESPONSE_ITEMS]
            warnings.append(f"array_truncated:incomplete_units:{_MAX_RESPONSE_ITEMS}")
        clean_incomplete: list[str] = []
        for index, unit_id in enumerate(incomplete):
            if not isinstance(unit_id, str) or not unit_id.strip():
                warnings.append(f"incomplete_unit_ignored:incomplete_units[{index}]")
                continue
            if unit_id.strip() not in clean_incomplete:
                clean_incomplete.append(unit_id.strip())
        normalized_opinions: list[Any] = []
        for index, item in enumerate(opinions):
            if not isinstance(item, Mapping):
                normalized_opinions.append(item)
                continue
            allowed = _OPINION_KEYS | {"statement", "opinion", "summary", "refs"}
            clean_item = dict(item)
            for field_name in sorted(set(clean_item) - allowed):
                safe_field = re.sub(r"[^A-Za-z0-9_.-]", "_", str(field_name))[:64] or "<unknown>"
                warnings.append(f"unknown_field_ignored:opinions[{index}].{safe_field}")
                clean_item.pop(field_name, None)
            normalized_opinions.append(clean_item)
        content_missing = not any(
            (isinstance(item, str) and item.strip())
            or (
                isinstance(item, Mapping)
                and any(
                    isinstance(item.get(key), str) and item[key].strip()
                    for key in ("text", "statement", "opinion", "summary")
                )
            )
            for item in normalized_opinions
        )
        return {
            "schema": ADVISOR_RESPONSE_SCHEMA_V4,
            "consultation_type": consultation_type,
            "status": status,
            "opinions": normalized_opinions,
            "incomplete_units": clean_incomplete,
            "_content_missing": content_missing,
            "_normalization_warnings": warnings,
        }

    document = parse_advisor_wire(parse_text)
    fields = document.fields
    schema = _single_field(fields, "schema")
    response_type = _single_field(fields, "type")
    status = _single_field(fields, "status")
    warnings = []
    if schema != ADVISOR_RESPONSE_SCHEMA_V4:
        warnings.append("schema_missing_or_mismatched")
    if response_type != consultation_type:
        warnings.append("consultation_type_missing_or_mismatched")
    if status is None:
        warnings.append("status_missing_or_unrecognized")
        status = "succeeded"
    elif status.casefold() not in _SUPPORTED_STATUSES:
        warnings.append("status_missing_or_unrecognized")
        status = "succeeded"
    else:
        status = status.casefold()
    if document.ignored_prose:
        warnings.append("wire_prose_preserved")
    wire_opinions: list[dict[str, Any]] = []
    raw_wire_incomplete = list(fields.get("incomplete_unit", ()))
    if len(raw_wire_incomplete) > _MAX_RESPONSE_ITEMS:
        warnings.append(f"array_truncated:incomplete_units:{_MAX_RESPONSE_ITEMS}")
        raw_wire_incomplete = raw_wire_incomplete[:_MAX_RESPONSE_ITEMS]
    wire_incomplete: list[str] = []
    for index, unit_id in enumerate(raw_wire_incomplete):
        if not isinstance(unit_id, str) or not unit_id.strip():
            warnings.append(f"incomplete_unit_ignored:incomplete_units[{index}]")
            continue
        if unit_id.strip() not in wire_incomplete:
            wire_incomplete.append(unit_id.strip())
    active: dict[str, Any] | None = None
    top_text = _single_field(fields, "opinion")
    top_aliases = list(fields.get("ref", ()))
    for key, value in document.records:
        if key == "unit":
            if active is not None:
                wire_opinions.append(active)
            active = {"unit_id": value, "text": "", "source_aliases": [], "status": "complete"}
        elif key == "unit_text" and active is not None:
            active["text"] = value
        elif key == "unit_ref" and active is not None:
            active["source_aliases"].append(value)
        elif key == "unit_status" and active is not None:
            active["status"] = value.casefold()
    if active is not None:
        wire_opinions.append(active)
    elif top_text is not None:
        wire_opinions = [
            {
                "unit_id": "U1",
                "text": top_text,
                "source_aliases": top_aliases,
                "status": "complete",
            }
        ]
    elif fields.get("unit"):
        warnings.append("unit_text_missing")
    content_missing = not isinstance(top_text, str) or not top_text.strip()
    content_missing = content_missing and not any(
        isinstance(item, Mapping) and isinstance(item.get("text"), str) and item["text"].strip()
        for item in wire_opinions
    )
    if len(wire_opinions) > _MAX_RESPONSE_ITEMS:
        wire_opinions = wire_opinions[:_MAX_RESPONSE_ITEMS]
        warnings.append(f"array_truncated:opinions:{_MAX_RESPONSE_ITEMS}")
    return {
        "schema": ADVISOR_RESPONSE_SCHEMA_V4,
        "consultation_type": consultation_type,
        "status": status,
        "opinions": wire_opinions,
        "incomplete_units": wire_incomplete,
        "_content_missing": content_missing,
        "_wire_unstructured": not document.has_header and not fields and document.ignored_prose,
        "_normalization_warnings": warnings,
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
