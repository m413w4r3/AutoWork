from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from cti_app.domain.discovery import canonicalize_http_url

NORMALIZATION_VERSION = "discovery-json-v1"
_FENCE = re.compile(r"^\s*```(?:json|markdown)?\s*\n(?P<body>.*)\n```\s*$", re.DOTALL)
_JSON_KEY = re.compile(r'(?P<key>"(?:[^"\\]|\\.)*")(?P<space>\s*:)')
_MARKDOWN_LINK = re.compile(r"^\[(?P<label>[^\]]+)\]\((?P<url>https?://[^\s()]+)\)$")
_EXPLICIT_ABSENCE = {"absent", "aucun", "aucune", "none", "null", "n/a", "unknown"}
_NULLABLE_NAMES = {"published_at", "event_date", "citation", "excerpt"}


class JsonEnvelopeError(ValueError):
    def __init__(self, message: str, *, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.line = line
        self.column = column


@dataclass(frozen=True, slots=True)
class NormalizedModelOutput:
    raw_text: str
    normalized_text: str
    value: dict[str, Any]
    version: str
    raw_sha256: str
    normalized_sha256: str
    transformations: tuple[str, ...]


def normalize_discovery_output(raw_text: str) -> NormalizedModelOutput:
    transformations: list[str] = []
    candidate = raw_text.strip()
    fence = _FENCE.fullmatch(candidate)
    if fence:
        candidate = fence.group("body").strip()
        transformations.append("remove_markdown_fence")

    key_fixed = _JSON_KEY.sub(_normalize_key_match, candidate)
    if key_fixed != candidate:
        candidate = key_fixed
        transformations.append("unescape_underscore_in_keys")

    value, extracted = _decode_unique_object(candidate)
    if extracted:
        transformations.append("extract_unique_json_object")
    if not isinstance(value, dict):
        raise JsonEnvelopeError("La sortie structurée doit être un objet JSON.")

    _normalize_values(value, transformations)
    _normalize_queries(value, transformations)
    _deduplicate_urls(value, transformations)
    normalized_text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return NormalizedModelOutput(
        raw_text=raw_text,
        normalized_text=normalized_text,
        value=value,
        version=NORMALIZATION_VERSION,
        raw_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized_text.encode()).hexdigest(),
        transformations=tuple(dict.fromkeys(transformations)),
    )


def _normalize_key_match(match: re.Match[str]) -> str:
    key = match.group("key")
    return key.replace(r"\_", "_") + match.group("space")


def _decode_unique_object(text: str) -> tuple[Any, bool]:
    try:
        return json.loads(text), False
    except json.JSONDecodeError as original:
        decoder = json.JSONDecoder()
        objects: list[tuple[int, int, Any]] = []
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                value, end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                objects.append((index, index + end, value))
        outermost = [
            candidate
            for candidate in objects
            if not any(
                other[0] < candidate[0] and candidate[1] <= other[1] for other in objects
            )
        ]
        if len(outermost) == 1:
            return outermost[0][2], True
        raise JsonEnvelopeError(
            "La sortie ne contient pas un objet JSON unique.",
            line=original.lineno,
            column=original.colno,
        ) from original


def _normalize_values(value: Any, transformations: list[str], *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        for child_key, child in list(value.items()):
            value[child_key] = _normalize_values(child, transformations, key=child_key)
        return value
    if isinstance(value, list):
        for index, child in enumerate(value):
            value[index] = _normalize_values(child, transformations, key=key)
        return value
    if not isinstance(value, str):
        return value
    if key in _NULLABLE_NAMES and value.strip().casefold() in _EXPLICIT_ABSENCE:
        transformations.append("explicit_absence_to_null")
        return None
    link = _MARKDOWN_LINK.fullmatch(value.strip())
    if link:
        destination = link.group("url")
        canonicalize_http_url(destination)
        transformations.append("unwrap_unambiguous_markdown_url")
        return destination
    return value


def _normalize_queries(value: dict[str, Any], transformations: list[str]) -> None:
    queries = value.get("queries")
    if not isinstance(queries, list):
        return
    filtered = [query for query in queries if not isinstance(query, str) or query.strip()]
    if filtered != queries:
        value["queries"] = filtered
        transformations.append("remove_empty_queries")


def _deduplicate_urls(value: dict[str, Any], transformations: list[str]) -> None:
    changed = False
    citations = value.get("citations")
    if isinstance(citations, list):
        value["citations"], deduped = _deduplicate_items(citations)
        changed = changed or deduped
    topics = value.get("topics")
    if isinstance(topics, list):
        for topic in topics:
            if not isinstance(topic, dict) or not isinstance(topic.get("sources"), list):
                continue
            topic["sources"], deduped = _deduplicate_items(topic["sources"])
            changed = changed or deduped
    if changed:
        transformations.append("deduplicate_canonical_urls")


def _deduplicate_items(items: list[Any]) -> tuple[list[Any], bool]:
    seen: set[str] = set()
    result: list[Any] = []
    changed = False
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            result.append(item)
            continue
        try:
            canonical = canonicalize_http_url(item["url"])
        except ValueError:
            result.append(item)
            continue
        if canonical in seen:
            changed = True
            continue
        seen.add(canonical)
        result.append(item)
    return result, changed
