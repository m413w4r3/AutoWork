"""Strict parsers for typed, non-authoritative Bridge artifacts."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from cti_app.application.advisor_wire import (
    advisor_wire_aliases,
    parse_advisor_wire,
)
from cti_app.application.publication_secret_guard import redact_explicit_operational_secrets
from cti_app.domain.advisor_figure_plan import AdvisorFigurePlanV1

ADVISOR_RESPONSE_SCHEMA_V3 = "advisor-response.v3"
ADVISOR_SOURCE_PARSER_VERSION = "advisor-source-parser.v3"
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ANCHOR = re.compile(r"^[a-z][a-z0-9:_-]{0,127}$")
_REFERENCE_ALIAS_IN_PROSE = re.compile(r"(?<![A-Za-z0-9_])[ER][1-9][0-9]*(?![A-Za-z0-9_])", re.I)
_INFORMATIVE_ADVISOR_WARNINGS = {
    "block_target_defaulted",
    "bridge_reported_partial",
    "invariants_missing",
    "limitations_missing",
    "source_alias_in_prose",
    "heuristic_unit_assignment",
    "secret_redacted",
    "response_bounded",
    "type_missing",
    "unstructured_overall",
    "unstructured_draft",
    "wire_header_missing",
    "wire_prose_ignored",
}
_REVIEW_STATUS_NEUTRAL_WARNINGS = frozenset(
    {
        "reference_alias_invalid",
        "reference_alias_unknown",
        "reference_list_partial",
        "source_alias_in_prose",
    }
)
_GLOBAL_BLOCKING_WARNING_MARKERS = ("byte", "secret", "truncat", "output_size", "oversized")
_BLOCK_TYPES = {
    "key_findings",
    "additional_samples",
    "infection_chain_and_capabilities",
    "network_and_infrastructure",
    "claim_verification",
    "proposed_detections",
    "uncertainties_and_next_leads",
}
_YARA_RULE_START = re.compile(r"(?m)^\s*(?:(?:private|global)\s+)*rule\s+[A-Za-z_]")


def parse_typed_advisor_response(
    raw: str,
    *,
    consultation_type: str,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
    measurement_sha256_overrides: Mapping[str, str] | None = None,
    operational_secret_values: Sequence[str] = (),
) -> dict[str, Any] | None:
    """Parse a legacy v3 object or the current tolerant text wire without clipping."""
    raw_text = raw if isinstance(raw, str) else ""
    parsed = extract_json_object(raw_text)
    if parsed is None and consultation_type == "review_text":
        parsed = _extract_jsonish_object(raw_text)
    redacted_raw, raw_secret_count = redact_explicit_operational_secrets(
        raw_text, operational_secret_values=operational_secret_values
    )
    raw_safe = str(redacted_raw)
    if parsed is None:
        wire_result = _parse_typed_advisor_wire(
            raw_safe,
            consultation_type=consultation_type,
            prompt_data=prompt_data,
            safe_text=safe_text,
            measurement_sha256_overrides=measurement_sha256_overrides,
        )
        if consultation_type == "review_text":
            if wire_result is not None and wire_result.get("usable") is True:
                return _classify_advisor_warnings(
                    _finish_review_normalization(
                        wire_result,
                        raw_text=raw_text,
                        raw_safe=raw_safe,
                        raw_secret_count=raw_secret_count,
                        warnings=(),
                    )
                )
            heuristic = _normalize_heuristic_review(
                raw_safe,
                json_payload=None,
                prompt_data=prompt_data,
                safe_text=safe_text,
            )
            heuristic = heuristic or _unusable_review_result(raw_safe)
            return _classify_advisor_warnings(
                _finish_review_normalization(
                    heuristic,
                    raw_text=raw_text,
                    raw_safe=raw_safe,
                    raw_secret_count=raw_secret_count,
                    warnings=(),
                )
            )
        if consultation_type == "draft_text":
            if wire_result is not None and not any(
                warning in wire_result.get("warnings", ())
                for warning in ("wire_header_missing", "type_missing", "status_missing")
            ):
                return _classify_advisor_warnings(wire_result)
            heuristic = _normalize_heuristic_draft(
                raw_safe,
                json_payload=None,
                prompt_data=prompt_data,
                safe_text=safe_text,
            )
            heuristic = heuristic or _unusable_draft_result()
            if raw_secret_count:
                _append_warning(heuristic, "secret_redacted")
            return _classify_advisor_warnings(heuristic)
        return _classify_advisor_warnings(wire_result)
    parsed, secret_count = redact_explicit_operational_secrets(
        parsed, operational_secret_values=operational_secret_values
    )
    raw_secret_count = max(raw_secret_count, secret_count)
    if not isinstance(parsed, dict):
        return None
    if (
        parsed.get("schema") != ADVISOR_RESPONSE_SCHEMA_V3
        or parsed.get("consultation_type") != consultation_type
        or parsed.get("untrusted") is not True
    ):
        if consultation_type != "review_text":
            return None
        if parsed.get("consultation_type") not in {None, consultation_type}:
            return None
        if parsed.get("status") in {"refused", "invalid", "ambiguous"}:
            return None
        heuristic = _normalize_heuristic_review(
            raw_safe,
            json_payload=parsed,
            prompt_data=prompt_data,
            safe_text=safe_text,
        )
        heuristic = heuristic or _unusable_review_result(raw_safe)
        return _classify_advisor_warnings(
            _finish_review_normalization(
                heuristic,
                raw_text=raw_text,
                raw_safe=raw_safe,
                raw_secret_count=raw_secret_count,
                warnings=(),
            )
        )
    parsed, alias_warnings = _resolve_typed_json_aliases(parsed, prompt_data)
    result: dict[str, Any] | None = None
    try:
        if consultation_type == "draft_text":
            result = _parse_draft_text(parsed, prompt_data=prompt_data, safe_text=safe_text)
        elif consultation_type == "review_text":
            result = _parse_review_text(parsed, prompt_data=prompt_data, safe_text=safe_text)
        elif consultation_type == "yara_draft":
            result = _parse_yara_draft(
                parsed,
                prompt_data=prompt_data,
                safe_text=safe_text,
                measurement_sha256_overrides=measurement_sha256_overrides,
            )
        elif consultation_type == "suricata_draft":
            result = _parse_suricata_draft(parsed, prompt_data=prompt_data, safe_text=safe_text)
        elif consultation_type == "figure_plan":
            result = _parse_figure_plan(parsed, prompt_data=prompt_data, safe_text=safe_text)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None
    if result is None and alias_warnings:
        return _partial_typed_wire(
            {
                "schema": ADVISOR_RESPONSE_SCHEMA_V3,
                "consultation_type": consultation_type,
                "untrusted": True,
            },
            alias_warnings,
            limitations=[],
        )
    if result is None and consultation_type == "review_text":
        heuristic = _normalize_heuristic_review(
            raw_safe,
            json_payload=parsed,
            prompt_data=prompt_data,
            safe_text=safe_text,
        )
        result = heuristic or _unusable_review_result(raw_safe)
    if result is not None and alias_warnings:
        result = _mark_typed_partial(result, alias_warnings)
    if consultation_type == "review_text" and result is not None:
        result = _finish_review_normalization(
            result,
            raw_text=raw_text,
            raw_safe=raw_safe,
            raw_secret_count=raw_secret_count,
            warnings=(),
        )
    return _classify_advisor_warnings(result)


_MAX_HEURISTIC_REVIEW_TEXT_CHARS = 4000
_REVIEW_TEXT_KEYS = frozenset(
    {
        "analysis",
        "comment",
        "comments",
        "concerns",
        "feedback",
        "issues",
        "issue",
        "recommendations",
        "recommendation",
        "remarks",
        "review",
        "summary",
        "suggestion",
        "text",
        "verdict",
        "why",
    }
)
_REVIEW_PLACEHOLDERS = frozenset(
    {"", "...", "{}", "[]", "json", "null", "none", "n/a", "no response"}
)
_REVIEW_ALIAS_HEADING = re.compile(r"(?i)^(?:unit\s*)?([ut]?\s*\d+)$")


def _extract_jsonish_object(raw: str) -> dict[str, Any] | None:
    """Recover one JSON or Python-literal object from a Bridge-like answer."""
    candidate = _json_object_candidate(raw)
    if candidate is None:
        return None
    try:
        value = json.loads(
            candidate,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        try:
            tree = ast.parse(candidate, mode="eval")
            if _ast_has_duplicate_string_keys(tree):
                return None
            value = ast.literal_eval(tree)
        except (SyntaxError, TypeError, ValueError, MemoryError, RecursionError):
            return None
    return dict(value) if isinstance(value, Mapping) else None


def _json_object_candidate(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    fences = list(re.finditer(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL))
    if fences:
        if len(fences) != 1 or text.count("```") != 2:
            return None
        candidate = fences[0].group(1).strip()
        return candidate if candidate.startswith("{") and candidate.endswith("}") else None
    starts: list[int] = []
    depth = 0
    quote: str | None = None
    escaped = False
    end_positions: list[int] = []
    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            if depth == 0:
                starts.append(index)
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                end_positions.append(index + 1)
    if len(starts) != 1 or len(end_positions) != 1:
        return None
    return text[starts[0] : end_positions[0]]


def _ast_has_duplicate_string_keys(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value
            for key in node.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        ]
        if len(keys) != len(set(keys)):
            return True
    return False


def _normalize_heuristic_review(
    raw: str,
    *,
    json_payload: Mapping[str, Any] | None,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
) -> dict[str, Any] | None:
    units = _requested_review_units(prompt_data)
    if not units:
        return _unusable_review_result(raw)

    grouped: dict[str, list[str]] = {}
    assignments: dict[str, str] = {}
    overall_text: str | None = None
    if json_payload is not None:
        grouped, assignments, overall_text = _reviews_from_json(json_payload, units)
        if not grouped and overall_text is None:
            return _unusable_review_result(raw)
        assignment_status = "unstructured_overall" if overall_text and not grouped else "heuristic"
        if overall_text and not grouped:
            grouped = {unit_id: [overall_text] for unit_id, _unit in units}
            assignments = {unit_id: "unstructured_overall" for unit_id, _unit in units}
            warning_codes = ["unstructured_overall"]
        else:
            warning_codes = ["heuristic_unit_assignment"]
    else:
        grouped, assignments, overall_text = _reviews_from_prose(raw, units)
        if not grouped and overall_text is not None:
            grouped = {unit_id: [overall_text] for unit_id, _unit in units}
            assignments = {unit_id: "unstructured_overall" for unit_id, _unit in units}
        if not grouped:
            return _unusable_review_result(raw)
        assignment_status = (
            "unstructured_overall"
            if all(value == "unstructured_overall" for value in assignments.values())
            else "heuristic"
        )
        warning_codes = (
            ["unstructured_overall"]
            if assignment_status == "unstructured_overall"
            else ["heuristic_unit_assignment"]
        )

    review_items = []
    for unit_id, remarks in grouped.items():
        bounded = [_bound_review_remark(item) for item in remarks if _review_text_is_useful(item)]
        if not bounded:
            continue
        review_items.append(
            {
                "unit_id": unit_id,
                "remarks": bounded,
                "basis_refs": [],
                "assignment": assignments.get(unit_id, "heuristic"),
            }
        )
    if not review_items:
        return _unusable_review_result(raw)
    normalized = _parse_review_text(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V3,
            "consultation_type": "review_text",
            "status": "partial",
            "language": "fr",
            "reviews": review_items,
            "limitations": [],
            "warnings": warning_codes,
            "normalization_status": "partial",
            "untrusted": True,
        },
        prompt_data=prompt_data,
        safe_text=safe_text,
    )
    if normalized is None:
        return _unusable_review_result(raw)
    normalized["unit_assignment_status"] = assignment_status
    if overall_text is not None:
        normalized["overall_review"] = _bound_review_remark(overall_text)
    return normalized


def _normalize_heuristic_draft(
    raw: str,
    *,
    json_payload: Mapping[str, Any] | None,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
) -> dict[str, Any] | None:
    units = _requested_review_units(prompt_data)
    grouped: dict[str, list[str]] = {}
    assignments: dict[str, str] = {}
    overall_text: str | None = None
    if json_payload is not None:
        grouped, assignments, overall_text = _reviews_from_json(json_payload, units)
    else:
        grouped, assignments, overall_text = _reviews_from_prose(raw, units)
    blocks: list[dict[str, Any]] = []
    units_by_id = {unit_id: unit for unit_id, unit in units}
    for unit_id, text_values in grouped.items():
        unit = units_by_id.get(unit_id)
        if unit is None:
            continue
        sentences: list[dict[str, Any]] = []
        for value in text_values:
            if not _review_text_is_useful(value):
                continue
            cleaned = safe_text(value, _MAX_HEURISTIC_REVIEW_TEXT_CHARS + 1)
            if _review_text_is_useful(cleaned):
                sentences.append({"text": _bound_review_remark(cleaned), "assertion_ids": []})
        if not sentences:
            continue
        blocks.append(
            {
                "unit_id": unit_id,
                "block_type": unit.get("block_type"),
                "form": unit.get("form", "body"),
                "anchor": unit.get("anchor"),
                "sentences": sentences,
                "warnings": ["unstructured_draft"],
                "usable": True,
                "assignment": assignments.get(unit_id, "heuristic"),
            }
        )
    if blocks:
        warning_codes = ["heuristic_unit_assignment", "unstructured_draft"]
        return {
            "schema": ADVISOR_RESPONSE_SCHEMA_V3,
            "consultation_type": "draft_text",
            "status": "partial",
            "untrusted": True,
            "blocks": blocks,
            "summary": " ".join(
                sentence["text"] for block in blocks for sentence in block["sentences"]
            ),
            "warnings": warning_codes,
            "normalization_status": "partial",
            "unit_assignment_status": "heuristic",
            "usable": True,
        }
    if overall_text is None or not _review_text_is_useful(overall_text):
        return None
    overall_text = safe_text(overall_text, _MAX_HEURISTIC_REVIEW_TEXT_CHARS + 1)
    if not _review_text_is_useful(overall_text):
        return None
    return {
        "schema": ADVISOR_RESPONSE_SCHEMA_V3,
        "consultation_type": "draft_text",
        "status": "partial",
        "untrusted": True,
        "summary": _bound_review_remark(overall_text),
        "warnings": ["unstructured_overall"],
        "normalization_status": "partial",
        "unit_assignment_status": "unstructured_overall",
        "usable": True,
    }


def _requested_review_units(prompt_data: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    raw_units = prompt_data.get("units")
    if isinstance(raw_units, list):
        return [
            (str(item.get("unit_id", item.get("id"))), item)
            for item in raw_units
            if isinstance(item, Mapping) and item.get("unit_id", item.get("id")) is not None
        ]
    legacy = prompt_data.get("review_text")
    return [("U1", legacy)] if isinstance(legacy, Mapping) else []


def _reviews_from_json(
    payload: Mapping[str, Any], units: list[tuple[str, Mapping[str, Any]]]
) -> tuple[dict[str, list[str]], dict[str, str], str | None]:
    unit_rows: list[tuple[object, object]] = []
    overall_values: list[str] = []
    unit_container: object = payload.get("units", payload.get("reviews"))
    if isinstance(unit_container, list):
        unit_rows.extend((None, item) for item in unit_container)
    elif isinstance(unit_container, Mapping):
        unit_rows.extend(unit_container.items())
    elif unit_container is not None:
        unit_rows.append((None, unit_container))

    grouped: dict[str, list[str]] = {}
    assignments: dict[str, str] = {}
    for overall_key in ("verdict", "issues", "recommendations", "summary"):
        if overall_key not in payload:
            continue
        value = payload[overall_key]
        if isinstance(value, Mapping):
            entry_label = _review_entry_label(value)
            normalized_keys = {str(key).casefold().replace("-", "_") for key in value}
            if entry_label is not None:
                unit_rows.append((None, value))
            elif normalized_keys & _REVIEW_TEXT_KEYS:
                overall_values.extend(_review_text_values(value))
            else:
                unit_rows.extend(value.items())
        elif isinstance(value, list):
            unit_rows.extend((None, item) for item in value)
        else:
            overall_values.extend(_review_text_values(value))

    for order_index, (key, value) in enumerate(unit_rows):
        label: object = key
        entry: object = value
        if isinstance(value, Mapping):
            label = _review_entry_label(value) or key
        unit_id, assignment = _resolve_review_unit(label, units, order_index)
        content = _review_text_values(entry)
        if content and unit_id is not None:
            grouped.setdefault(unit_id, []).extend(content)
            assignments.setdefault(unit_id, assignment)
        elif content:
            overall_values.extend(content)
    overall = "\n".join(item for item in overall_values if _review_text_is_useful(item)).strip()
    return grouped, assignments, overall or None


def _review_entry_label(value: Mapping[str, Any]) -> object | None:
    for key in (
        "unit_id",
        "unit",
        "alias",
        "target",
        "id",
        "index",
        "unit_index",
        "block_type",
        "anchor",
    ):
        if key in value:
            return cast(object, value[key])
    return None


def _review_text_values(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if _review_text_is_useful(text) else []
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _review_text_values(item)]
    if isinstance(value, Mapping):
        texts: list[str] = []
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _REVIEW_TEXT_KEYS:
                texts.extend(_review_text_values(child))
            elif isinstance(child, Mapping):
                texts.extend(_review_text_values(child))
        return texts
    return []


def _resolve_review_unit(
    label: object, units: list[tuple[str, Mapping[str, Any]]], order_index: int
) -> tuple[str | None, str]:
    if isinstance(label, int) and not isinstance(label, bool):
        index = label - 1 if label > 0 else label
        return (units[index][0], "index") if 0 <= index < len(units) else (None, "index")
    if isinstance(label, str):
        candidate = label.strip().strip("*_`[](){}<>: ")
        for unit_id, _unit in units:
            if candidate.casefold() == unit_id.casefold():
                return unit_id, "alias"
        alias_match = _REVIEW_ALIAS_HEADING.fullmatch(candidate)
        if alias_match:
            digits = re.search(r"\d+", alias_match.group(1))
            index = int(digits.group()) - 1 if digits else -1
            if 0 <= index < len(units):
                return units[index][0], "alias"
        for unit_id, unit in units:
            if candidate.casefold() in {
                str(unit.get("block_type", "")).casefold(),
                str(unit.get("anchor", "")).casefold(),
            }:
                return unit_id, "block_type_or_anchor"
    if 0 <= order_index < len(units):
        return units[order_index][0], "order"
    return None, "order"


def _reviews_from_prose(
    raw: str, units: list[tuple[str, Mapping[str, Any]]]
) -> tuple[dict[str, list[str]], dict[str, str], str | None]:
    grouped: dict[str, list[str]] = {}
    assignments: dict[str, str] = {}
    overall_lines: list[str] = []
    active_id: str | None = None
    active_assignment = "alias"
    active_lines: list[str] = []
    saw_heading = False

    def flush() -> None:
        nonlocal active_lines
        text = "\n".join(active_lines).strip()
        if active_id is not None and _review_text_is_useful(text):
            grouped.setdefault(active_id, []).append(text)
            assignments.setdefault(active_id, active_assignment)
        active_lines = []

    for line in raw.splitlines():
        heading, body = _review_heading(line, units)
        if heading == "overall":
            flush()
            active_id = None
            overall_lines.append(body)
            continue
        if isinstance(heading, tuple):
            flush()
            active_id, active_assignment = heading
            saw_heading = True
            if body:
                active_lines.append(body)
            continue
        if active_id is None:
            overall_lines.append(line)
        else:
            active_lines.append(line)
    flush()
    overall = "\n".join(overall_lines).strip()
    if saw_heading and grouped:
        return grouped, assignments, overall if _review_text_is_useful(overall) else None
    whole = raw.strip()
    return {}, {}, whole if _review_text_is_useful(whole) else None


def _review_heading(
    line: str, units: list[tuple[str, Mapping[str, Any]]]
) -> tuple[tuple[str, str] | str | None, str]:
    cleaned = re.sub(r"^\s*(?:[-*+]\s+)?(?:#{1,6}\s*)?", "", line).strip()
    cleaned = cleaned.strip("*_` ")
    if re.match(r"(?i)^overall\s*:", cleaned):
        return "overall", re.sub(r"(?i)^overall\s*:\s*", "", cleaned).strip()
    separator = re.search(r"\s*(?::|[\u2014\u2013])\s*", cleaned)
    if separator is None:
        separator = re.search(r"\s+-\s+", cleaned)
    if separator is None:
        resolved = _resolve_review_unit(cleaned, units, -1)
        if resolved[0] is not None:
            return (resolved[0], resolved[1]), ""
        return None, ""
    label = cleaned[: separator.start()].strip("*_` ")
    body = cleaned[separator.end() :].strip().strip("*_` ")
    resolved = _resolve_review_unit(label, units, -1)
    if resolved[0] is None:
        return None, ""
    return (resolved[0], resolved[1]), body


def _review_text_is_useful(value: str) -> bool:
    normalized = value.strip().strip("`*_# ").casefold()
    if normalized in _REVIEW_PLACEHOLDERS:
        return False
    return re.search(r"[\wÀ-ÖØ-öø-ÿ]{2,}", normalized) is not None


def _bound_review_remark(value: str) -> str:
    text = value.strip()
    if len(text) <= _MAX_HEURISTIC_REVIEW_TEXT_CHARS:
        return text
    return text[: _MAX_HEURISTIC_REVIEW_TEXT_CHARS - 1].rstrip() + "…"


def _unusable_review_result(_raw: str) -> dict[str, Any]:
    return {
        "schema": ADVISOR_RESPONSE_SCHEMA_V3,
        "consultation_type": "review_text",
        "status": "partial",
        "untrusted": True,
        "language": "fr",
        "reviews": [],
        "limitations": [],
        "warnings": ["review_response_unusable"],
        "normalization_status": "partial",
        "unit_assignment_status": "unusable",
        "usable": False,
    }


def _unusable_draft_result() -> dict[str, Any]:
    return {
        "schema": ADVISOR_RESPONSE_SCHEMA_V3,
        "consultation_type": "draft_text",
        "status": "partial",
        "untrusted": True,
        "blocks": [],
        "warnings": ["draft_response_unusable"],
        "normalization_status": "partial",
        "usable": False,
    }


def _append_warning(result: dict[str, Any], warning: str) -> None:
    warnings = result.setdefault("warnings", [])
    if isinstance(warnings, list) and warning not in warnings:
        warnings.append(warning)
    result["status"] = "partial"
    result["normalization_status"] = "partial"


def _finish_review_normalization(
    result: dict[str, Any],
    *,
    raw_text: str,
    raw_safe: str,
    raw_secret_count: int,
    warnings: tuple[str, ...],
) -> dict[str, Any]:
    for warning in warnings:
        _append_warning(result, warning)
    if raw_secret_count:
        _append_warning(result, "secret_redacted")
    encoded = raw_text.encode("utf-8")
    result["raw_output"] = raw_safe
    result["raw_output_bytes"] = len(encoded)
    result["raw_output_sha256"] = hashlib.sha256(encoded).hexdigest()
    result["raw_output_retained"] = True
    result["raw_output_truncated"] = False
    return result


def _warning_is_blocking(code: object) -> bool:
    if not isinstance(code, str):
        return True
    return code not in _INFORMATIVE_ADVISOR_WARNINGS


def _warning_classification(warnings: object) -> dict[str, list[str]]:
    codes = (
        list(dict.fromkeys(item for item in warnings if isinstance(item, str)))
        if isinstance(warnings, (list, tuple))
        else []
    )
    return {
        "blocking": [item for item in codes if _warning_is_blocking(item)],
        "informative": [item for item in codes if not _warning_is_blocking(item)],
    }


def _review_warnings_require_partial(warnings: object) -> bool:
    return isinstance(warnings, (list, tuple)) and any(
        isinstance(item, str) and item not in _REVIEW_STATUS_NEUTRAL_WARNINGS for item in warnings
    )


def _has_global_blocking_warning(warnings: object) -> bool:
    if not isinstance(warnings, (list, tuple)):
        return False
    return any(
        isinstance(item, str)
        and item != "secret_redacted"
        and any(marker in item.casefold() for marker in _GLOBAL_BLOCKING_WARNING_MARKERS)
        for item in warnings
    )


def _classify_advisor_warnings(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    classified = dict(result)
    warnings = classified.get("warnings", [])
    classification = _warning_classification(warnings)
    if classified.get("consultation_type") == "review_text":
        reference_warnings = [
            item for item in classification["blocking"] if item in _REVIEW_STATUS_NEUTRAL_WARNINGS
        ]
        classification["blocking"] = [
            item for item in classification["blocking"] if item not in reference_warnings
        ]
        classification["informative"].extend(
            item for item in reference_warnings if item not in classification["informative"]
        )
    classified["warning_classification"] = classification
    for collection in ("blocks", "reviews"):
        items = classified.get(collection)
        if not isinstance(items, list):
            continue
        normalized: list[Any] = []
        for item in items:
            if not isinstance(item, Mapping):
                normalized.append(item)
                continue
            unit_item = dict(item)
            unit_warnings = unit_item.get("warnings", [])
            unit_classification = _warning_classification(unit_warnings)
            unit_item["warning_classification"] = unit_classification
            unit_item["usable"] = (
                unit_item.get("usable") is True
                and not unit_classification["blocking"]
                and not _has_global_blocking_warning(warnings)
            )
            normalized.append(unit_item)
        classified[collection] = normalized
        classified["usable"] = any(
            isinstance(item, Mapping) and item.get("usable") is True for item in normalized
        )
    return classified


def _resolve_typed_json_aliases(
    parsed: dict[str, Any], prompt_data: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Accept alias citations in legacy JSON replies while resolving locally."""
    aliases = advisor_wire_aliases(prompt_data)
    warnings: list[str] = []
    actual_refs = {
        (str(item.get("kind")), str(item.get("id"))): item for item in aliases["refs"].values()
    }
    actual_measurements = {
        str(item.get("measurement_id")): item for item in aliases["measurements"].values()
    }
    actual_facts = {str(item.get("fact_id")): item for item in aliases["facts"].values()}
    actual_assertions = {str(item.get("id")): item for item in aliases["assertions"].values()}

    def warn() -> None:
        if "reference_alias_unknown" not in warnings:
            warnings.append("reference_alias_unknown")

    def resolve_ref(alias: object, prefix: str) -> dict[str, str] | None:
        if not isinstance(alias, str):
            return None
        key = alias.upper()
        collection = (
            aliases["refs"]
            if prefix == "R"
            else aliases["measurements"]
            if prefix == "M"
            else aliases["facts"]
        )
        item = collection.get(key)
        if item is None and prefix == "R" and re.fullmatch(r"E[1-9][0-9]*", key):
            item = aliases["evidence"].get(key)
        if item is None and prefix == "R" and alias.startswith("article:"):
            article_sha = alias.partition(":")[2]
            item = actual_refs.get(("article", article_sha))
        if item is None and prefix == "R":
            item = next(
                (row for (kind, identifier), row in actual_refs.items() if identifier == alias),
                None,
            )
        elif item is None and prefix == "M":
            item = actual_measurements.get(alias)
        elif item is None and prefix == "F":
            item = actual_facts.get(alias)
        if item is None:
            warn()
            return None
        if prefix == "R":
            return {"kind": str(item["kind"]), "id": str(item["id"])}
        if prefix == "M":
            measurement = dict(item)
            digest = hashlib.sha256(
                json.dumps(
                    measurement, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            return {"measurement_id": str(measurement.get("measurement_id")), "sha256": digest}
        return {"fact_id": str(item.get("fact_id"))}

    def resolve_ref_list(values: object, prefix: str, key: str) -> list[Any]:
        if not isinstance(values, list):
            return values if isinstance(values, list) else []
        resolved: list[Any] = []
        for item in values:
            if prefix == "M" and isinstance(item, Mapping):
                raw_id = item.get("measurement_id")
                if isinstance(raw_id, str):
                    mapped = resolve_ref(raw_id, prefix)
                    if mapped is not None:
                        resolved.append(mapped)
                    continue
            elif prefix == "F" and isinstance(item, str):
                mapped = resolve_ref(item, prefix)
                if mapped is not None:
                    resolved.append(mapped["fact_id"])
                continue
            elif prefix == "R" and isinstance(item, Mapping):
                raw_id = item.get("id")
                if isinstance(raw_id, str):
                    mapped = resolve_ref(raw_id, prefix)
                    if mapped is not None and item.get("kind") in {None, mapped["kind"]}:
                        resolved.append(mapped)
                    elif mapped is not None:
                        warn()
                    continue
            elif prefix == "A" and isinstance(item, str):
                mapped = aliases["assertions"].get(item.upper()) or actual_assertions.get(item)
                if mapped is None:
                    warn()
                else:
                    resolved.append(str(mapped.get("id")))
                continue
            else:
                warn()
        return resolved

    result = dict(parsed)
    consultation_type = result.get("consultation_type")
    if consultation_type == "draft_text" and isinstance(result.get("blocks"), list):
        blocks = []
        for block in result["blocks"]:
            if isinstance(block, Mapping) and isinstance(block.get("sentences"), list):
                block_copy = dict(block)
                block_copy.pop("_parser_warnings", None)
                block_warnings: list[str] = []
                for sentence in block["sentences"]:
                    if not isinstance(sentence, Mapping):
                        continue
                    raw_assertion_ids = sentence.get("assertion_ids")
                    if not isinstance(raw_assertion_ids, list):
                        continue
                    if any(
                        not isinstance(item, str)
                        or (
                            item.upper() not in aliases["assertions"]
                            and item not in actual_assertions
                        )
                        for item in raw_assertion_ids
                    ):
                        if "assertion_alias_unknown" not in block_warnings:
                            block_warnings.append("assertion_alias_unknown")
                block_copy["sentences"] = [
                    {
                        **dict(sentence),
                        "assertion_ids": resolve_ref_list(
                            sentence.get("assertion_ids"), "A", "assertion_ids"
                        ),
                    }
                    if isinstance(sentence, Mapping)
                    else sentence
                    for sentence in block["sentences"]
                ]
                if block_warnings:
                    block_copy["_parser_warnings"] = block_warnings
                blocks.append(block_copy)
            else:
                blocks.append(block)
        result["blocks"] = blocks
    elif consultation_type == "yara_draft" and isinstance(result.get("motifs"), list):
        result["motifs"] = [
            {
                **dict(motif),
                "measurement_refs": resolve_ref_list(
                    motif.get("measurement_refs"), "M", "measurement_refs"
                ),
            }
            if isinstance(motif, Mapping)
            else motif
            for motif in result["motifs"]
        ]
    elif consultation_type == "suricata_draft":
        if "fact_refs" in result:
            result["fact_refs"] = resolve_ref_list(result["fact_refs"], "F", "fact_refs")
        if isinstance(result.get("invariants"), list):
            result["invariants"] = [
                {
                    **dict(item),
                    "fact_refs": resolve_ref_list(item.get("fact_refs"), "F", "fact_refs"),
                }
                if isinstance(item, Mapping)
                else item
                for item in result["invariants"]
            ]
    elif consultation_type == "figure_plan" and isinstance(result.get("figure_plan"), Mapping):
        plan = dict(result["figure_plan"])
        for key in ("nodes", "edges"):
            rows = plan.get(key)
            if not isinstance(rows, list):
                continue
            for row_index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    continue
                row_copy = dict(row)
                row_copy["refs"] = resolve_ref_list(row.get("refs"), "R", "refs")
                if key == "edges":
                    row_copy["direction_refs"] = resolve_ref_list(
                        row.get("direction_refs"), "R", "direction_refs"
                    )
                rows[row_index] = row_copy
        result["figure_plan"] = plan
    return result, warnings


def _mark_typed_partial(result: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    result = dict(result)
    all_warnings = list(dict.fromkeys([*result.get("warnings", ()), *warnings]))
    result["status"] = "partial"
    result["normalization_status"] = "partial"
    result["warnings"] = all_warnings
    result["limitations"] = list(result.get("limitations", ())) + [
        f"Wire recovery: {item}." for item in warnings
    ]
    return result


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """Read one object from legacy replies, including a single object in surrounding prose."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw.strip()
    if "```" in candidate:
        fences = list(re.finditer(r"```([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", candidate, re.S))
        if len(fences) != 1 or candidate.count("```") != 2:
            return None
        language = fences[0].group(1).casefold()
        if language not in {"", "json"}:
            return None
        candidate = fences[0].group(2).strip()
    else:
        extracted = _single_json_object(candidate)
        if extracted is not None:
            candidate = extracted
    try:
        parsed = json.loads(
            candidate,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _single_json_object(text: str) -> str | None:
    """Extract exactly one balanced object while respecting quoted braces."""
    objects: list[tuple[int, int]] = []
    depth = 0
    in_string = False
    escaped = False
    start: int | None = None
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                object_end = index + 1
                objects.append((start, object_end))
                if len(objects) > 1:
                    return None
                candidate = text[start:object_end]
                try:
                    decoded = json.loads(
                        candidate,
                        object_pairs_hook=_unique_object_pairs,
                        parse_constant=_reject_json_constant,
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
                if not isinstance(decoded, dict):
                    return None
                start = None
    return text[objects[0][0] : objects[0][1]] if len(objects) == 1 else None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Typed Bridge response contains a duplicate key")
        value[key] = item
    return value


def _parse_typed_advisor_wire(
    raw: str,
    *,
    consultation_type: str,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
    measurement_sha256_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    document = parse_advisor_wire(raw)
    aliases = advisor_wire_aliases(prompt_data)
    warnings: list[str] = []
    fields = document.fields

    def values(key: str) -> tuple[str, ...]:
        return fields.get(key.casefold().replace("-", "_"), ())

    def warn(code: str) -> None:
        if code not in warnings:
            warnings.append(code)

    def text(value: str, maximum: int, code: str) -> str | None:
        del maximum
        if not value.strip():
            warn(code)
            return None
        cleaned = safe_text(value, len(value) + 1)
        if cleaned != value:
            warn("secret_detected" if _has_redaction_marker(cleaned) else code)
            return None
        return value

    if not document.has_header:
        warn("wire_header_missing")
    if document.ignored_prose:
        warn("wire_prose_ignored")
    raw_type = next(iter(values("type")), "")
    if not raw_type:
        warn("type_missing")
    elif raw_type.casefold() != consultation_type.casefold():
        warn("type_mismatch")
    status_line = next(iter(values("status")), "")
    status_match = re.match(
        r"(?i)^\s*(refused|succeeded|partial)(?:\s*\|\s*reason\s*=\s*(.*))?", status_line
    )
    if not status_line:
        warn("status_missing")
    elif status_match is None:
        warn("status_unrecognized")
    elif status_match.group(1).casefold() == "refused":
        reason = (status_match.group(2) or "Bridge a refusé la proposition.").strip()
        safe_reason = text(reason, 400, "refusal_reason_invalid")
        return {
            "schema": ADVISOR_RESPONSE_SCHEMA_V3,
            "consultation_type": consultation_type,
            "status": "refused",
            "refusal_reason": safe_reason or "Bridge a refusé la proposition.",
            "untrusted": True,
            "usable": False,
        }
    elif status_match.group(1).casefold() == "partial":
        warn("bridge_reported_partial")

    def mapped_refs(value: str, prefix: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        tokens = [part for part in re.split(r"[,;\s]+", value.strip()) if part]
        for token in tokens:
            normalized = token.strip("[](){}<>").upper()
            is_evidence = re.fullmatch(r"E[1-9][0-9]*", normalized) is not None
            if re.fullmatch(rf"{prefix}[1-9][0-9]*", normalized) is None and not is_evidence:
                warn("reference_alias_invalid")
                continue
            if is_evidence:
                item = aliases["evidence"].get(normalized)
            else:
                alias_map = (
                    aliases["refs"]
                    if prefix == "R"
                    else aliases["measurements"]
                    if prefix == "M"
                    else aliases["facts"]
                )
                item = alias_map.get(normalized)
            if item is None:
                warn("reference_alias_unknown")
                continue
            if prefix == "R":
                identifier = item.get("id")
                kind = item.get("kind")
                if isinstance(item.get("measurement_id"), str):
                    identifier, kind = item.get("evidence_id"), "observation"
                elif isinstance(item.get("fact_id"), str):
                    identifier, kind = item.get("evidence_id"), "observation"
                if isinstance(identifier, str) and isinstance(kind, str):
                    result.append({"kind": kind, "id": identifier})
            elif prefix == "M":
                if isinstance(item.get("measurement_id"), str) and is_evidence:
                    result.append(
                        {
                            "measurement_id": str(item["measurement_id"]),
                            "sha256": str(item.get("sha256", "")),
                        }
                    )
                    continue
                measurement = dict(item)
                measurement_id = measurement.get("measurement_id")
                digest = (measurement_sha256_overrides or {}).get(str(measurement_id))
                if digest is None:
                    digest = hashlib.sha256(
                        json.dumps(
                            measurement,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                result.append({"measurement_id": str(measurement_id), "sha256": digest})
            else:
                fact_id = item.get("fact_id")
                if isinstance(fact_id, str):
                    result.append({"fact_id": fact_id})
        deduplicated: list[dict[str, str]] = []
        for item in result:
            if item not in deduplicated:
                deduplicated.append(item)
        if len(tokens) != len(deduplicated):
            warn("reference_list_partial")
        return deduplicated

    def split_parts(value: str) -> list[str]:
        return [part.strip() for part in value.split("|")]

    def attr_map(parts: list[str]) -> tuple[list[str], dict[str, str]]:
        positional: list[str] = []
        attrs: dict[str, str] = {}
        for part in parts:
            match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*$", part)
            if match:
                attrs[match.group(1).casefold().replace("-", "_")] = match.group(2)
            else:
                positional.append(part)
        return positional, attrs

    def limitations() -> list[str]:
        result: list[str] = []
        for value in values("limitation"):
            if value.strip().casefold() in {"none", "aucune", "aucun"}:
                continue
            item = text(value, 500, "limitation_invalid")
            if item:
                if _REFERENCE_ALIAS_IN_PROSE.search(item):
                    warn("source_alias_in_prose")
                result.append(item)
        return result

    common = {
        "schema": ADVISOR_RESPONSE_SCHEMA_V3,
        "consultation_type": consultation_type,
        "untrusted": True,
    }

    if consultation_type == "review_text":
        requested_units = prompt_data.get("units")
        if not isinstance(requested_units, list):
            legacy = prompt_data.get("review_text")
            requested_units = (
                [{"unit_id": "U1", **dict(legacy)}] if isinstance(legacy, Mapping) else []
            )
        unit_ids = {
            str(item.get("unit_id", item.get("id")))
            for item in requested_units
            if isinstance(item, Mapping)
        }
        review_active_unit: str | None = None
        review_grouped: dict[str, list[str]] = {}
        review_refs: dict[str, list[dict[str, str]]] = {}
        review_unit_warnings: dict[str, list[str]] = {}

        def warn_review_unit(unit_id: str | None, code: str) -> None:
            warn(code)
            if unit_id is not None:
                local = review_unit_warnings.setdefault(unit_id, [])
                if code not in local:
                    local.append(code)

        for key, value in document.records:
            if key == "unit":
                candidate = value.strip().upper()
                if candidate not in unit_ids:
                    warn("unit_id_unknown")
                    review_active_unit = None
                else:
                    review_active_unit = candidate
                    review_grouped.setdefault(candidate, [])
            elif key == "remark":
                if review_active_unit is None:
                    warn("remark_unit_missing")
                    continue
                remark = text(value, len(value), "remark_invalid")
                if remark is None:
                    warn_review_unit(review_active_unit, "remark_invalid")
                    continue
                if _UUID.search(remark):
                    warn_review_unit(review_active_unit, "remark_contains_identifier")
                    continue
                if _REFERENCE_ALIAS_IN_PROSE.search(remark):
                    warn_review_unit(review_active_unit, "source_alias_in_prose")
                review_grouped[review_active_unit].append(remark)
            elif key == "refs" and review_active_unit is not None:
                refs = mapped_refs(value, "R")
                review_refs.setdefault(review_active_unit, []).extend(refs)
        for unit_id in unit_ids:
            if not review_grouped.get(unit_id):
                warn_review_unit(unit_id, "review_unit_missing")
        reviews = [
            {
                "unit_id": unit_id,
                "remarks": remarks,
                "basis_refs": [
                    {"kind": kind, "id": identifier}
                    for kind, identifier in dict.fromkeys(
                        (item["kind"], item["id"])
                        for item in review_refs.get(unit_id, ())
                        if isinstance(item.get("kind"), str) and isinstance(item.get("id"), str)
                    )
                ],
                "warnings": review_unit_warnings.get(unit_id, []),
                "usable": not any(
                    _warning_is_blocking(item) for item in review_unit_warnings.get(unit_id, [])
                ),
            }
            for unit_id, remarks in review_grouped.items()
            if remarks
        ]
        limits = limitations()
        if warnings:
            limits = limits + [f"Wire recovery: {item}." for item in warnings]
        return {
            **common,
            "status": "partial" if _review_warnings_require_partial(warnings) else "succeeded",
            "language": "fr",
            "reviews": reviews,
            "limitations": limits,
            "normalization_status": (
                "partial" if _review_warnings_require_partial(warnings) else "complete"
            ),
            "warnings": warnings,
            "usable": any(item["usable"] for item in reviews),
        }

    if consultation_type == "draft_text":
        assertions = aliases["assertions"]
        unit_rows = prompt_data.get("units")
        units_by_id = (
            {
                str(item.get("id")): item
                for item in unit_rows
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
            if isinstance(unit_rows, list)
            else {}
        )
        targets = (
            {
                unit_id: (
                    item.get("block_type"),
                    item.get("form"),
                    item.get("anchor"),
                )
                for unit_id, item in units_by_id.items()
            }
            if units_by_id
            else {
                f"legacy-{index}": (
                    item.get("block_type"),
                    item.get("form"),
                    item.get("anchor"),
                )
                for index, item in enumerate(prompt_data.get("assertions", ()))
                if isinstance(item, Mapping)
            }
        )
        target_by_value = {target: unit_id for unit_id, target in targets.items()}
        active_unit: str | None = None
        active_target: tuple[Any, Any, Any] | None = None
        grouped: dict[str, list[dict[str, Any]]] = {}
        unit_warnings: dict[str, list[str]] = {}

        def warn_draft_unit(unit_id: str | None, code: str) -> None:
            warn(code)
            if unit_id is not None:
                local = unit_warnings.setdefault(unit_id, [])
                if code not in local:
                    local.append(code)

        for key, value in document.records:
            if key == "unit":
                unit_id = value.strip().upper()
                if unit_id not in units_by_id:
                    warn("unit_id_unknown")
                    active_unit = None
                    active_target = None
                else:
                    active_unit = unit_id
                    active_target = targets[unit_id]
                    grouped.setdefault(unit_id, [])
            elif key == "block":
                if units_by_id:
                    warn("unit_id_required")
                    active_unit = None
                    active_target = None
                    continue
                parts = split_parts(value)
                if len(parts) < 2:
                    warn("block_target_invalid")
                    active_unit = None
                    active_target = None
                    continue
                anchor = (
                    None if len(parts) < 3 or parts[2].strip() in {"", "-", "none"} else parts[2]
                )
                target = (parts[0].casefold(), parts[1].casefold(), anchor)
                matching_unit = target_by_value.get(target)
                if matching_unit is None:
                    warn("block_target_unknown")
                    active_unit = None
                    active_target = None
                else:
                    active_unit = matching_unit
                    active_target = target
                    grouped.setdefault(matching_unit, [])
            elif key == "s":
                if active_target is None:
                    if not units_by_id and targets:
                        active_unit = next(iter(targets))
                        active_target = targets[active_unit]
                        grouped.setdefault(active_unit, [])
                        warn_draft_unit(active_unit, "block_target_defaulted")
                    else:
                        warn_draft_unit(active_unit, "unit_target_missing")
                        continue
                match = re.match(r"^(.*?)\s*\[([^\]]*)\]\s*$", value)
                if match is None:
                    warn_draft_unit(active_unit, "sentence_references_missing")
                    continue
                sentence = text(match.group(1).strip(), 2400, "sentence_invalid")
                sentence_refs: list[str] = []
                invalid_refs = False
                for token in re.split(r"[,;\s]+", match.group(2).strip()):
                    if not token:
                        continue
                    assertion = assertions.get(token.upper())
                    if assertion is None:
                        warn_draft_unit(active_unit, "assertion_alias_unknown")
                        invalid_refs = True
                        continue
                    assertion_id = assertion.get("id")
                    assertion_unit = assertion.get("unit_id")
                    allowed_for_unit = (
                        assertion_unit == active_unit if units_by_id else assertion_unit is None
                    )
                    if (
                        isinstance(assertion_id, str)
                        and allowed_for_unit
                        and assertion_id not in sentence_refs
                    ):
                        sentence_refs.append(assertion_id)
                    elif not allowed_for_unit:
                        warn_draft_unit(active_unit, "assertion_wrong_unit")
                        invalid_refs = True
                if not sentence_refs:
                    warn_draft_unit(active_unit, "sentence_without_valid_assertion")
                    continue
                if invalid_refs:
                    continue
                if sentence is not None and _REFERENCE_ALIAS_IN_PROSE.search(sentence):
                    warn_draft_unit(active_unit, "source_alias_in_prose")
                if sentence is None or _UUID.search(sentence) or not _is_one_sentence(sentence):
                    warn_draft_unit(active_unit, "sentence_invalid")
                    continue
                draft_unit_id = active_unit
                if draft_unit_id is None:
                    warn_draft_unit(active_unit, "unit_target_missing")
                    continue
                grouped[draft_unit_id].append({"text": sentence, "assertion_ids": sentence_refs})
        blocks = [
            {
                **({"unit_id": unit_id} if units_by_id else {}),
                "block_type": targets[unit_id][0],
                "form": targets[unit_id][1],
                "anchor": targets[unit_id][2],
                "sentences": sentences,
                "warnings": unit_warnings.get(unit_id, []),
                "usable": bool(sentences)
                and not any(_warning_is_blocking(item) for item in unit_warnings.get(unit_id, [])),
            }
            for unit_id, sentences in grouped.items()
            if sentences
        ]
        if not blocks:
            warn("draft_sentences_missing")
        if units_by_id and {item["unit_id"] for item in blocks} != set(units_by_id):
            warn("draft_unit_missing")
        limits = limitations()
        if warnings:
            limits = limits + [f"Wire recovery: {item}." for item in warnings]
        return {
            **common,
            "status": "partial" if warnings else "succeeded",
            "language": "fr",
            "blocks": blocks,
            "limitations": limits,
            "normalization_status": "partial" if warnings else "complete",
            "warnings": warnings,
            "usable": any(item["usable"] for item in blocks),
        }

    if consultation_type in {"yara_draft", "suricata_draft"}:
        expected_language = "yara" if consultation_type == "yara_draft" else "suricata"
        source = document.source
        if document.source_count > 1:
            warn("raw_source_multiple_blocks")
            source = None
        elif source is None:
            warn("raw_source_block_missing")
        elif document.source_language != expected_language:
            warn("raw_source_language_mismatch")
            source = None
        if source is not None:
            if safe_text(source, len(source) + 1).strip() != source.strip():
                warn("raw_source_contains_filtered_content")
                source = None
            elif consultation_type == "yara_draft" and _YARA_RULE_START.search(source) is None:
                warn("yara_rule_header_missing")
                source = None
            elif (
                consultation_type == "suricata_draft"
                and re.search(r"(?im)^\s*(?:alert|drop|pass)\b", source) is None
            ):
                warn("suricata_rule_header_missing")
                source = None
        source_hash = (
            hashlib.sha256(source.encode("utf-8")).hexdigest() if source is not None else None
        )

        if consultation_type == "yara_draft":
            motifs: list[dict[str, Any]] = []
            for value in values("motif"):
                parts = split_parts(value)
                name = parts[0] if parts else ""
                raw_refs = next(
                    (
                        part.split("=", 1)[1]
                        for part in parts[1:]
                        if part.casefold().startswith("measurements=")
                    ),
                    "",
                )
                measurement_alias_refs: list[dict[str, str]] = (
                    mapped_refs(raw_refs, "M") if raw_refs else []
                )
                if not name.startswith("$") or not measurement_alias_refs:
                    warn("motif_incomplete")
                    continue
                motifs.append({"name": name, "measurement_refs": measurement_alias_refs})
            if not motifs:
                warn("motifs_missing")
            yara_invariants = [
                item
                for value in values("invariant")
                if value.strip().casefold() not in {"none", "aucune", "aucun"}
                and (item := text(value, 500, "invariant_invalid"))
            ]
            if not values("invariant"):
                warn("invariants_missing")
            limits = limitations()
            if source is None or not motifs:
                return _partial_typed_wire(
                    common,
                    warnings,
                    limitations=limits,
                    rule_source=source,
                    rule_source_sha256=source_hash,
                    motifs=motifs,
                    invariants=yara_invariants,
                )
            limits = limits + [f"Wire recovery: {item}." for item in warnings]
            return {
                **common,
                "status": "partial" if warnings else "succeeded",
                "rule_source": source,
                "rule_source_sha256": source_hash,
                "motifs": motifs,
                "invariants": yara_invariants,
                "limitations": limits,
                "normalization_status": "partial" if warnings else "complete",
                "warnings": warnings,
                "usable": True,
            }

        fact_refs: list[str] = []
        for value in values("fact"):
            for ref in mapped_refs(value, "F"):
                if ref["fact_id"] not in fact_refs:
                    fact_refs.append(ref["fact_id"])
        suricata_invariants: list[dict[str, Any]] = []
        for value in values("invariant"):
            parts, attrs = attr_map(split_parts(value))
            statement = parts[0] if parts else ""
            invariant_fact_refs: list[dict[str, str]] = mapped_refs(attrs.get("facts", ""), "F")
            if not invariant_fact_refs and len(parts) > 1:
                invariant_fact_refs = mapped_refs(parts[1], "F")
            clean_statement = (
                None
                if statement.strip().casefold() in {"none", "aucune", "aucun"}
                else text(statement, 500, "invariant_invalid")
            )
            if clean_statement and invariant_fact_refs:
                suricata_invariants.append(
                    {
                        "statement": clean_statement,
                        "fact_refs": [ref["fact_id"] for ref in invariant_fact_refs],
                    }
                )
            else:
                warn("invariant_incomplete")
        if not fact_refs:
            fact_refs = list(
                dict.fromkeys(ref for item in suricata_invariants for ref in item["fact_refs"])
            )
        if not fact_refs:
            warn("fact_references_missing")
        limits = limitations()
        if source is None or not fact_refs or not suricata_invariants:
            return _partial_typed_wire(
                common,
                warnings,
                limitations=limits,
                rule_source=source,
                rule_source_sha256=source_hash,
                fact_refs=fact_refs,
                invariants=suricata_invariants,
            )
        limits = limits + [f"Wire recovery: {item}." for item in warnings]
        return {
            **common,
            "status": "partial" if warnings else "succeeded",
            "rule_source": source,
            "rule_source_sha256": source_hash,
            "fact_refs": fact_refs,
            "invariants": suricata_invariants,
            "limitations": limits,
            "normalization_status": "partial" if warnings else "complete",
            "warnings": warnings,
            "usable": True,
        }

    if consultation_type == "figure_plan":
        title = text(next(iter(values("title")), ""), 180, "title_missing_or_invalid")
        if title and _REFERENCE_ALIAS_IN_PROSE.search(title):
            warn("source_alias_in_prose")
        legend_line = next(iter(values("legend")), "")
        legend_parts = split_parts(legend_line)
        evidence_level = legend_parts[0].casefold() if len(legend_parts) >= 2 else ""
        legend_text = (
            text(legend_parts[1], 240, "legend_missing_or_invalid")
            if len(legend_parts) >= 2
            else None
        )
        if len(legend_parts) < 2:
            warn("legend_missing_or_invalid")
        elif legend_text and _REFERENCE_ALIAS_IN_PROSE.search(legend_text):
            warn("source_alias_in_prose")
        nodes: list[dict[str, Any]] = []
        node_roles: dict[str, str] = {}
        for value in values("node"):
            parts, attrs = attr_map(split_parts(value))
            if len(parts) < 3:
                warn("node_incomplete")
                continue
            node_id, label, role = parts[:3]
            node_id = node_id.casefold()
            if _REFERENCE_ALIAS_IN_PROSE.search(label):
                warn("source_alias_in_prose")
            node_refs = mapped_refs(attrs.get("refs", parts[3] if len(parts) > 3 else ""), "R")
            if not node_refs:
                warn("node_references_missing")
                continue
            nodes.append({"id": node_id, "label": label, "refs": node_refs})
            node_roles[node_id] = role
        edges: list[dict[str, Any]] = []
        for index, value in enumerate(values("edge"), start=1):
            parts, attrs = attr_map(split_parts(value))
            if len(parts) < 2:
                warn("edge_incomplete")
                continue
            endpoint = re.match(
                r"^\s*([a-z][a-z0-9_-]{0,63})\s*->\s*([a-z][a-z0-9_-]{0,63})\s*$", parts[0], re.I
            )
            if endpoint is None:
                warn("edge_endpoints_invalid")
                continue
            raw_refs = attrs.get("refs", parts[2] if len(parts) > 2 else "")
            edge_refs = mapped_refs(raw_refs, "R")
            if not edge_refs:
                warn("edge_references_missing")
                continue
            direction = attrs.get("direction", "").casefold() or None
            if _REFERENCE_ALIAS_IN_PROSE.search(parts[1]):
                warn("source_alias_in_prose")
            direction_refs = mapped_refs(attrs.get("direction_refs", ""), "R") if direction else []
            if direction and not direction_refs:
                warn("edge_direction_evidence_missing")
                direction = None
            edges.append(
                {
                    "id": f"edge-{index}",
                    "source": endpoint.group(1),
                    "target": endpoint.group(2),
                    "label": parts[1],
                    "refs": edge_refs,
                    "direction": direction,
                    "direction_refs": direction_refs if direction else [],
                }
            )
        plan_payload = {
            "schema": "advisor-figure-plan.v1",
            "title": title,
            "legend": {"evidence_level": evidence_level, "text": legend_text},
            "nodes": nodes,
            "edges": edges,
        }
        plan: dict[str, Any] | None = None
        if title and legend_text and nodes:
            try:
                typed_plan = AdvisorFigurePlanV1.from_payload(plan_payload)
                allowed = {
                    (str(item.get("kind")), str(item.get("id")))
                    for item in prompt_data.get("transmitted_refs", ())
                    if isinstance(item, Mapping)
                }
                typed_plan.require_transmitted_refs(allowed)
                plan = typed_plan.to_payload()
            except (TypeError, ValueError):
                warn("figure_plan_invalid")
        limits = limitations()
        if plan is None:
            return _partial_typed_wire(
                common, warnings, limitations=limits, figure_plan=plan_payload
            )
        limits = limits + [f"Wire recovery: {item}." for item in warnings]
        result = {
            **common,
            "status": "partial" if warnings else "succeeded",
            "figure_plan": plan,
            "limitations": limits,
            "normalization_status": "partial" if warnings else "complete",
            "warnings": warnings,
            "usable": "source_alias_in_prose" not in warnings,
        }
        if node_roles:
            result["node_roles"] = node_roles
        return result

    return None


def _partial_typed_wire(
    common: Mapping[str, Any], warnings: list[str], *, limitations: list[str], **fields: Any
) -> dict[str, Any]:
    visible = list(dict.fromkeys(warnings or ["required_fields_missing"]))
    visible_limits = limitations + [f"Wire recovery: {item}." for item in visible]
    return {
        **common,
        **fields,
        "status": "partial",
        "limitations": visible_limits,
        "normalization_status": "partial",
        "warnings": visible,
        "usable": False,
    }


def _parse_review_text(
    parsed: Mapping[str, Any],
    *,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
) -> dict[str, Any] | None:
    if parsed.get("status") == "refused":
        if set(parsed) != {"schema", "consultation_type", "status", "refusal_reason", "untrusted"}:
            return None
        reason = _strict_text(parsed.get("refusal_reason"), 400, safe_text)
        return _typed_refusal(parsed, reason) if reason else None
    required_keys = {
        "schema",
        "consultation_type",
        "status",
        "language",
        "reviews",
        "untrusted",
    }
    if not required_keys.issubset(parsed) or set(parsed) - required_keys - {
        "limitations",
        "warnings",
        "normalization_status",
    }:
        return None
    if parsed.get("status") not in {"succeeded", "partial"} or parsed.get("language") != "fr":
        return None
    warnings: list[str] = []

    def warn(code: str, unit_id: str | None = None) -> None:
        if code not in warnings:
            warnings.append(code)
        if unit_id is not None:
            local = unit_warnings.setdefault(unit_id, [])
            if code not in local:
                local.append(code)

    raw_warnings = parsed.get("warnings", [])
    if isinstance(raw_warnings, list):
        for warning in raw_warnings:
            if isinstance(warning, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", warning):
                warn(warning)
            else:
                warn("review_warning_invalid")
    elif raw_warnings:
        warn("review_warning_invalid")
    if parsed.get("status") == "partial":
        warn("bridge_reported_partial")
    if parsed.get("normalization_status") not in {None, "complete", "partial"}:
        warn("review_normalization_status_invalid")
    if "limitations" not in parsed:
        warn("limitations_missing")
    requested = prompt_data.get("units")
    if isinstance(requested, list):
        unit_ids = {
            str(item.get("unit_id", item.get("id")))
            for item in requested
            if isinstance(item, Mapping)
        }
    else:
        review = prompt_data.get("review_text")
        unit_ids = {"U1"} if isinstance(review, Mapping) else set()
    raw_reviews = parsed.get("reviews")
    if not isinstance(raw_reviews, list):
        return None
    seen: set[str] = set()
    reviews: list[dict[str, Any]] = []
    unit_warnings: dict[str, list[str]] = {}
    aliases = advisor_wire_aliases(prompt_data)
    allowed_refs = {
        (str(item.get("kind")), str(item.get("id")))
        for item in prompt_data.get("transmitted_refs", ())
        if isinstance(item, Mapping)
    }
    allowed_refs.update(
        (str(item.get("kind")), str(item.get("id")))
        for item in prompt_data.get("dossier_manifest", ())
        if isinstance(item, Mapping)
        and isinstance(item.get("kind"), str)
        and isinstance(item.get("id"), str)
    )
    allowed_refs.update(
        (str(item.get("kind")), str(item.get("id")))
        for item in aliases["evidence"].values()
        if isinstance(item.get("kind"), str) and isinstance(item.get("id"), str)
    )
    for item in raw_reviews:
        if (
            not isinstance(item, Mapping)
            or set(item) - {"unit_id", "remarks", "basis_refs", "assignment"}
            or not {"unit_id", "remarks"}.issubset(item)
        ):
            warn("review_item_invalid")
            continue
        unit_id = item.get("unit_id")
        remarks = item.get("remarks")
        if not isinstance(unit_id, str) or unit_id not in unit_ids:
            warn("review_unit_unknown")
            continue
        if unit_id in seen:
            warn("review_unit_duplicate", unit_id)
            continue
        seen.add(unit_id)
        if not isinstance(remarks, list) or not remarks:
            warn("review_remarks_invalid", unit_id)
            continue
        raw_basis_refs = item.get("basis_refs", [])
        basis_refs: list[dict[str, str]] = []
        if not isinstance(raw_basis_refs, list):
            warn("review_refs_invalid", unit_id)
        else:
            for ref in raw_basis_refs:
                if (
                    not isinstance(ref, Mapping)
                    or set(ref) != {"kind", "id"}
                    or not isinstance(ref.get("kind"), str)
                    or not isinstance(ref.get("id"), str)
                    or (str(ref.get("kind")), str(ref.get("id"))) not in allowed_refs
                ):
                    warn("review_refs_invalid", unit_id)
                    continue
                clean_ref = {"kind": str(ref["kind"]), "id": str(ref["id"])}
                if clean_ref not in basis_refs:
                    basis_refs.append(clean_ref)
        clean_remarks: list[str] = []
        for raw_remark in remarks:
            remark = _strict_text(raw_remark, 1200, safe_text)
            if not remark:
                warn(
                    "secret_detected"
                    if _text_was_redacted(raw_remark, 1200, safe_text)
                    else "remark_invalid",
                    unit_id,
                )
                continue
            if _UUID.search(remark):
                warn("remark_contains_identifier", unit_id)
                continue
            if _REFERENCE_ALIAS_IN_PROSE.search(remark):
                warn("source_alias_in_prose", unit_id)
            clean_remarks.append(remark)
        if clean_remarks:
            normalized_review = {
                "unit_id": unit_id,
                "remarks": clean_remarks,
                "basis_refs": basis_refs,
                "warnings": unit_warnings.get(unit_id, []),
                "usable": not any(
                    _warning_is_blocking(item) for item in unit_warnings.get(unit_id, [])
                ),
            }
            assignment = item.get("assignment")
            if isinstance(assignment, str):
                normalized_review["assignment"] = assignment
            reviews.append(normalized_review)
        else:
            warn("review_unit_missing", unit_id)
    for unit_id in unit_ids - seen:
        warn("review_unit_missing", unit_id)
    raw_limitations = parsed.get("limitations", [])
    limitations = _strict_text_list(raw_limitations, safe_text)
    if limitations is None:
        limitations = []
        warn(
            "secret_detected"
            if isinstance(raw_limitations, list)
            and any(_text_was_redacted(item, 500, safe_text) for item in raw_limitations)
            else "review_limitations_invalid"
        )
    clean_limitations: list[str] = []
    for limitation in limitations:
        if _REFERENCE_ALIAS_IN_PROSE.search(limitation):
            warn("source_alias_in_prose")
        clean_limitations.append(limitation)
    return {
        **_typed_base(parsed),
        "status": (
            "partial"
            if parsed.get("status") == "partial" or _review_warnings_require_partial(warnings)
            else "succeeded"
        ),
        "reviews": reviews,
        "limitations": clean_limitations,
        "warnings": warnings,
        "normalization_status": (
            "partial"
            if _review_warnings_require_partial(warnings)
            else parsed.get("normalization_status", "complete")
        ),
        "usable": any(item["usable"] for item in reviews),
    }


def _parse_draft_text(
    parsed: Mapping[str, Any],
    *,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
) -> dict[str, Any] | None:
    if parsed.get("status") == "refused":
        if set(parsed) != {"schema", "consultation_type", "status", "refusal_reason", "untrusted"}:
            return None
        reason = _strict_text(parsed.get("refusal_reason"), 400, safe_text)
        return _typed_refusal(parsed, reason) if reason else None
    required_keys = {
        "schema",
        "consultation_type",
        "status",
        "language",
        "blocks",
        "untrusted",
    }
    if not required_keys.issubset(parsed) or set(parsed) - required_keys - {
        "limitations",
        "warnings",
        "normalization_status",
    }:
        return None
    if parsed.get("status") not in {"succeeded", "partial"} or parsed.get("language") != "fr":
        return None
    warnings: list[str] = []
    unit_warnings: dict[str, list[str]] = {}

    def warn(code: str, unit_id: str | None = None) -> None:
        if code not in warnings:
            warnings.append(code)
        if unit_id is not None:
            local = unit_warnings.setdefault(unit_id, [])
            if code not in local:
                local.append(code)

    raw_warnings = parsed.get("warnings", [])
    if isinstance(raw_warnings, list):
        for warning in raw_warnings:
            if isinstance(warning, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", warning):
                warn(warning)
            else:
                warn("draft_warning_invalid")
    elif raw_warnings:
        warn("draft_warning_invalid")
    if parsed.get("status") == "partial":
        warn("bridge_reported_partial")
    if "limitations" not in parsed:
        warn("limitations_missing")
    if parsed.get("normalization_status") not in {None, "complete", "partial"}:
        warn("draft_normalization_status_invalid")
    unit_rows = prompt_data.get("units")
    units_by_id = (
        {
            str(item.get("id")): item
            for item in unit_rows
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        if isinstance(unit_rows, list)
        else {}
    )
    targets = {
        (item.get("block_type"), item.get("form"), item.get("anchor"))
        for item in (units_by_id.values() if units_by_id else prompt_data.get("assertions", ()))
        if isinstance(item, Mapping)
    }
    allowed_assertions = {
        item.get("id") for item in prompt_data.get("assertions", ()) if isinstance(item, Mapping)
    }
    transmitted_refs = {
        str(ref.get("id"))
        for item in prompt_data.get("assertions", ())
        if isinstance(item, Mapping)
        for ref in item.get("refs", ())
        if isinstance(ref, Mapping)
    }
    blocks = parsed.get("blocks")
    if not isinstance(blocks, list):
        return None
    clean_blocks: list[dict[str, Any]] = []
    seen_targets: set[tuple[Any, Any, Any]] = set()
    seen_units: set[str] = set()
    for block in blocks:
        allowed_block_keys = {"block_type", "form", "anchor", "sentences"}
        if not isinstance(block, Mapping) or frozenset(block) not in {
            frozenset(allowed_block_keys),
            frozenset(allowed_block_keys | {"unit_id"}),
            frozenset(allowed_block_keys | {"_parser_warnings"}),
            frozenset(allowed_block_keys | {"unit_id", "_parser_warnings"}),
        }:
            warn("draft_block_invalid")
            continue
        unit_id = block.get("unit_id")
        error_unit_id = unit_id if isinstance(unit_id, str) else "__single__"
        for local_warning in block.get("_parser_warnings", ()):
            if isinstance(local_warning, str) and re.fullmatch(
                r"[a-z][a-z0-9_]{0,63}", local_warning
            ):
                warn(local_warning, error_unit_id)
        block_type, form, anchor = block.get("block_type"), block.get("form"), block.get("anchor")
        target = (block_type, form, anchor)
        unit_target: Mapping[str, Any] | None = None
        if units_by_id:
            if not isinstance(unit_id, str) or unit_id not in units_by_id:
                warn("draft_unit_unknown")
                continue
            if unit_id in seen_units:
                warn("draft_unit_duplicate")
                continue
            unit_target = units_by_id[unit_id]
            if (
                unit_target.get("block_type") != block_type
                or unit_target.get("form") != form
                or unit_target.get("anchor") != anchor
            ):
                warn("draft_unit_target_mismatch", error_unit_id)
                continue
        elif unit_id is not None:
            warn("draft_unit_unexpected", error_unit_id)
            continue
        if (
            block_type not in _BLOCK_TYPES
            or form not in {"body", "analyst_note"}
            or target not in targets
            or target in seen_targets
            or (
                form == "analyst_note"
                and (not isinstance(anchor, str) or _ANCHOR.fullmatch(anchor) is None)
            )
            or (form == "body" and anchor is not None)
        ):
            warn("draft_block_target_invalid", error_unit_id)
            continue
        sentences = block.get("sentences")
        if not isinstance(sentences, list):
            warn("draft_sentence_list_invalid", error_unit_id)
            continue
        clean_sentences: list[dict[str, Any]] = []
        unit_assertion_ids = {
            item.get("id")
            for item in prompt_data.get("assertions", ())
            if isinstance(item, Mapping) and (not units_by_id or item.get("unit_id") == unit_id)
        }
        for sentence in sentences:
            if not isinstance(sentence, Mapping) or set(sentence) != {"text", "assertion_ids"}:
                warn("draft_sentence_invalid", error_unit_id)
                continue
            text_value = _strict_text(sentence.get("text"), 2400, safe_text)
            assertion_ids = sentence.get("assertion_ids")
            if not text_value and _text_was_redacted(sentence.get("text"), 2400, safe_text):
                warn("secret_detected", error_unit_id)
            if (
                not text_value
                or not _is_one_sentence(text_value)
                or not isinstance(assertion_ids, list)
                or not assertion_ids
                or not all(isinstance(item, str) for item in assertion_ids)
                or len(assertion_ids) != len(set(assertion_ids))
                or any(
                    item not in allowed_assertions or item not in unit_assertion_ids
                    for item in assertion_ids
                )
            ):
                warn("draft_sentence_assertion_invalid", error_unit_id)
                continue
            if _REFERENCE_ALIAS_IN_PROSE.search(text_value):
                warn("source_alias_in_prose", error_unit_id)
            if any(ref not in transmitted_refs for ref in _UUID.findall(text_value)):
                warn("draft_sentence_reference_invalid", error_unit_id)
                continue
            clean_sentences.append({"text": text_value, "assertion_ids": assertion_ids})
        if not clean_sentences:
            warn("draft_unit_without_valid_sentences", error_unit_id)
            continue
        seen_targets.add(target)
        if isinstance(unit_id, str):
            seen_units.add(unit_id)
        clean_block = {
            "block_type": block_type,
            "form": form,
            "anchor": anchor,
            "sentences": clean_sentences,
        }
        if unit_id is not None:
            clean_block["unit_id"] = unit_id
        clean_block["warnings"] = list(unit_warnings.get(error_unit_id, ()))
        clean_block["usable"] = not any(
            _warning_is_blocking(item) for item in unit_warnings.get(error_unit_id, ())
        )
        clean_blocks.append(clean_block)
    for clean_block in clean_blocks:
        error_unit_id = str(clean_block.get("unit_id", "__single__"))
        local_warnings = list(unit_warnings.get(error_unit_id, ()))
        clean_block["warnings"] = local_warnings
        clean_block["usable"] = bool(clean_block.get("sentences")) and not any(
            _warning_is_blocking(item) for item in local_warnings
        )
    if units_by_id and seen_units != set(units_by_id):
        warn("draft_unit_missing")
    elif not units_by_id and seen_targets != targets:
        warn("draft_block_missing")
    limitations = _strict_text_list(parsed.get("limitations", []), safe_text)
    if limitations is None:
        limitations = []
        raw_limitations = parsed.get("limitations", [])
        warn(
            "secret_detected"
            if isinstance(raw_limitations, list)
            and any(_text_was_redacted(item, 500, safe_text) for item in raw_limitations)
            else "draft_limitations_invalid"
        )
    else:
        clean_limitations: list[str] = []
        for limitation in limitations:
            if _REFERENCE_ALIAS_IN_PROSE.search(limitation):
                warn("source_alias_in_prose")
            clean_limitations.append(limitation)
        limitations = clean_limitations
    result = {
        **_typed_base(parsed),
        "status": "partial" if parsed.get("status") == "partial" or warnings else "succeeded",
        "blocks": clean_blocks,
        "limitations": limitations,
        "usable": any(item.get("usable") is True for item in clean_blocks),
        "warnings": warnings,
        "normalization_status": (
            "partial" if warnings else parsed.get("normalization_status", "complete")
        ),
    }
    return result


def _parse_yara_draft(
    parsed: Mapping[str, Any],
    *,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
    measurement_sha256_overrides: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    if parsed.get("status") == "refused":
        if set(parsed) != {"schema", "consultation_type", "status", "refusal_reason", "untrusted"}:
            return None
        reason = _strict_text(parsed.get("refusal_reason"), 400, safe_text)
        return _typed_refusal(parsed, reason) if reason else None
    if (
        set(parsed)
        != {
            "schema",
            "consultation_type",
            "status",
            "rule_source",
            "rule_source_sha256",
            "motifs",
            "invariants",
            "limitations",
            "untrusted",
        }
        or parsed.get("status") != "succeeded"
    ):
        return None
    source = parsed.get("rule_source")
    source_hash = parsed.get("rule_source_sha256")
    if (
        not isinstance(source, str)
        or not source.strip()
        or _YARA_RULE_START.search(source) is None
        or not isinstance(source_hash, str)
        or not _SHA256.fullmatch(source_hash)
        or hashlib.sha256(source.encode("utf-8")).hexdigest() != source_hash
        or safe_text(source, len(source) + 1).strip() != source.strip()
    ):
        return None
    allowed_measurements = {
        item.get("measurement_id"): (measurement_sha256_overrides or {}).get(
            str(item.get("measurement_id")),
            hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        )
        for item in prompt_data.get("measurements", ())
        if isinstance(item, Mapping)
    }
    motifs = parsed.get("motifs")
    if not isinstance(motifs, list) or not motifs:
        return None
    clean_motifs = []
    seen_names: set[str] = set()
    for motif in motifs:
        if not isinstance(motif, Mapping) or set(motif) != {"name", "measurement_refs"}:
            return None
        name = _strict_text(motif.get("name"), 64, safe_text)
        raw_refs = motif.get("measurement_refs")
        if (
            not name
            or not name.startswith("$")
            or name in seen_names
            or not isinstance(raw_refs, list)
            or not raw_refs
        ):
            return None
        refs: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for raw_ref in raw_refs:
            if not isinstance(raw_ref, Mapping) or set(raw_ref) != {"measurement_id", "sha256"}:
                return None
            measurement_id, measurement_sha = raw_ref["measurement_id"], raw_ref["sha256"]
            if (
                not isinstance(measurement_id, str)
                or measurement_id in seen_ids
                or measurement_id not in allowed_measurements
                or measurement_sha != allowed_measurements[measurement_id]
            ):
                return None
            seen_ids.add(measurement_id)
            refs.append({"measurement_id": measurement_id, "sha256": measurement_sha})
        seen_names.add(name)
        clean_motifs.append({"name": name, "measurement_refs": refs})
    invariants = _strict_text_list(parsed.get("invariants"), safe_text)
    limitations = _strict_text_list(parsed.get("limitations"), safe_text)
    if invariants is None or limitations is None:
        return None
    return {
        **_typed_base(parsed),
        "rule_source": source,
        "rule_source_sha256": source_hash,
        "motifs": clean_motifs,
        "invariants": invariants,
        "limitations": limitations,
        "usable": True,
    }


def _parse_suricata_draft(
    parsed: Mapping[str, Any],
    *,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
) -> dict[str, Any] | None:
    if parsed.get("status") == "refused":
        if set(parsed) != {"schema", "consultation_type", "status", "refusal_reason", "untrusted"}:
            return None
        reason = _strict_text(parsed.get("refusal_reason"), 400, safe_text)
        return _typed_refusal(parsed, reason) if reason else None
    if (
        set(parsed)
        != {
            "schema",
            "consultation_type",
            "status",
            "rule_source",
            "rule_source_sha256",
            "fact_refs",
            "invariants",
            "limitations",
            "untrusted",
        }
        or parsed.get("status") != "succeeded"
    ):
        return None
    source = parsed.get("rule_source")
    source_hash = parsed.get("rule_source_sha256")
    if (
        not isinstance(source, str)
        or not source.strip()
        or "alert " not in source
        or not isinstance(source_hash, str)
        or not _SHA256.fullmatch(source_hash)
        or hashlib.sha256(source.encode("utf-8")).hexdigest() != source_hash
        or safe_text(source, len(source) + 1).strip() != source.strip()
    ):
        return None
    allowed_facts = {
        item.get("fact_id")
        for item in prompt_data.get("network_facts", ())
        if isinstance(item, Mapping)
    }
    fact_refs = parsed.get("fact_refs")
    if (
        not isinstance(fact_refs, list)
        or not fact_refs
        or len(fact_refs) != len(set(fact_refs))
        or any(item not in allowed_facts for item in fact_refs)
    ):
        return None
    invariants = parsed.get("invariants")
    if not isinstance(invariants, list) or not invariants:
        return None
    clean_invariants = []
    for item in invariants:
        if not isinstance(item, Mapping) or set(item) != {"statement", "fact_refs"}:
            return None
        statement = _strict_text(item.get("statement"), 500, safe_text)
        refs = item.get("fact_refs")
        if (
            not statement
            or not isinstance(refs, list)
            or not refs
            or any(ref not in allowed_facts for ref in refs)
        ):
            return None
        clean_invariants.append({"statement": statement, "fact_refs": refs})
    limitations = _strict_text_list(parsed.get("limitations"), safe_text)
    if limitations is None:
        return None
    return {
        **_typed_base(parsed),
        "rule_source": source,
        "rule_source_sha256": source_hash,
        "fact_refs": fact_refs,
        "invariants": clean_invariants,
        "limitations": limitations,
        "usable": True,
    }


def _parse_figure_plan(
    parsed: Mapping[str, Any],
    *,
    prompt_data: Mapping[str, Any],
    safe_text: Callable[[str, int], str],
) -> dict[str, Any] | None:
    required_keys = {
        "schema",
        "consultation_type",
        "status",
        "figure_plan",
        "limitations",
        "untrusted",
    }
    if not required_keys.issubset(parsed) or set(parsed) - required_keys - {
        "warnings",
        "normalization_status",
        "node_roles",
    }:
        return None
    if parsed.get("status") not in {"succeeded", "partial"}:
        return None
    try:
        allowed = {
            (str(item.get("kind")), str(item.get("id")))
            for item in prompt_data.get("transmitted_refs", ())
            if isinstance(item, Mapping)
        }
        figure_payload = parsed.get("figure_plan")
        if isinstance(figure_payload, Mapping):
            figure_payload = dict(figure_payload)
            for collection_name in ("nodes", "edges"):
                collection = figure_payload.get(collection_name)
                if not isinstance(collection, list):
                    continue
                normalized_collection = []
                for item in collection:
                    if not isinstance(item, Mapping):
                        normalized_collection.append(item)
                        continue
                    normalized_item = dict(item)
                    refs = normalized_item.get("refs")
                    if isinstance(refs, list):
                        normalized_refs = []
                        for reference in refs:
                            if (
                                isinstance(reference, Mapping)
                                and reference.get("kind") == "article"
                                and isinstance(reference.get("id"), str)
                                and reference["id"].startswith("article:")
                            ):
                                article_sha = reference["id"].partition(":")[2]
                                if ("article", article_sha) in allowed:
                                    reference = {**dict(reference), "id": article_sha}
                            normalized_refs.append(reference)
                        normalized_item["refs"] = normalized_refs
                    if collection_name == "edges":
                        direction_refs = normalized_item.get("direction_refs")
                        if isinstance(direction_refs, list):
                            normalized_direction_refs = []
                            for reference in direction_refs:
                                if (
                                    isinstance(reference, Mapping)
                                    and reference.get("kind") == "article"
                                    and isinstance(reference.get("id"), str)
                                    and reference["id"].startswith("article:")
                                ):
                                    article_sha = reference["id"].partition(":")[2]
                                    if ("article", article_sha) in allowed:
                                        reference = {**dict(reference), "id": article_sha}
                                normalized_direction_refs.append(reference)
                            normalized_item["direction_refs"] = normalized_direction_refs
                    normalized_collection.append(normalized_item)
                figure_payload[collection_name] = normalized_collection
        plan = AdvisorFigurePlanV1.from_payload(figure_payload)
        plan.require_transmitted_refs(allowed)
    except (TypeError, ValueError):
        return None
    payload = plan.to_payload()
    text_values = [payload["title"], payload["legend"]["text"]]
    text_values.extend(item["label"] for item in payload["nodes"])
    text_values.extend(item["label"] for item in payload["edges"])
    for text_value in text_values:
        if safe_text(text_value, len(text_value) + 1) != text_value:
            return None
    warnings: list[str] = []
    raw_warnings = parsed.get("warnings", [])
    if isinstance(raw_warnings, list):
        warnings.extend(
            item
            for item in raw_warnings
            if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
        )
        if len(warnings) < len(raw_warnings):
            warnings.append("figure_warning_invalid")
    elif raw_warnings:
        warnings.append("figure_warning_invalid")
    prose_alias = any(_REFERENCE_ALIAS_IN_PROSE.search(text_value) for text_value in text_values)
    if prose_alias and "source_alias_in_prose" not in warnings:
        warnings.append("source_alias_in_prose")
    limitations = _strict_text_list(parsed.get("limitations"), safe_text)
    if limitations is None:
        limitations = []
        warnings.append("figure_limitations_invalid")
    result = {
        **_typed_base(parsed),
        "status": "partial" if parsed.get("status") == "partial" or warnings else "succeeded",
        "figure_plan": payload,
        "limitations": limitations,
        "usable": not prose_alias,
    }
    if warnings:
        result["warnings"] = list(dict.fromkeys(warnings))
        result["normalization_status"] = "partial"
    elif parsed.get("normalization_status"):
        result["normalization_status"] = parsed["normalization_status"]
    raw_node_roles = parsed.get("node_roles")
    if raw_node_roles is not None:
        node_ids = {item["id"] for item in payload["nodes"]}
        if isinstance(raw_node_roles, Mapping) and all(
            isinstance(key, str)
            and key in node_ids
            and isinstance(value, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value)
            for key, value in raw_node_roles.items()
        ):
            result["node_roles"] = dict(raw_node_roles)
        else:
            result["status"] = "partial"
            result["normalization_status"] = "partial"
            result["warnings"] = list(
                dict.fromkeys([*result.get("warnings", ()), "figure_node_roles_invalid"])
            )
            result["usable"] = False
    return result


def _typed_base(parsed: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": parsed["schema"],
        "consultation_type": parsed["consultation_type"],
        "status": parsed["status"],
        "untrusted": True,
    }


def _typed_refusal(parsed: Mapping[str, Any], reason: str | None) -> dict[str, Any] | None:
    if not reason:
        return None
    return {
        **_typed_base(parsed),
        "refusal_reason": reason,
        "usable": False,
    }


def _strict_text(value: Any, max_chars: int, safe_text: Callable[[str, int], str]) -> str | None:
    del max_chars
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = safe_text(value, len(value) + 1)
    return value if cleaned == value else None


def _has_redaction_marker(value: object) -> bool:
    return isinstance(value, str) and ("[REDACTED]" in value or "[SIGNED URL REDACTED]" in value)


def _text_was_redacted(value: object, max_chars: int, safe_text: Callable[[str, int], str]) -> bool:
    del max_chars
    return isinstance(value, str) and _has_redaction_marker(safe_text(value, len(value) + 1))


def _strict_text_list(value: Any, safe_text: Callable[[str, int], str]) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        text_value = _strict_text(item, 500, safe_text)
        if text_value is None:
            return None
        result.append(text_value)
    return result


def _is_one_sentence(value: str) -> bool:
    stripped = value.strip()
    if not stripped or stripped[-1] not in ".!?…":
        return False
    # The model emits sentence-shaped array items, so a second sentence delimiter
    # followed by a capital is treated as an accidental multi-sentence paragraph.
    return re.search(r"[.!?…][\"'»)]?\s+[A-ZÀ-Þ0-9]", stripped[:-1]) is None


__all__ = [
    "ADVISOR_RESPONSE_SCHEMA_V3",
    "extract_json_object",
    "parse_typed_advisor_response",
]
