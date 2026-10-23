"""Small, tolerant text wire format used for Bridge adviser replies.

The format deliberately stays line-oriented.  It is a prompt convention, not a
provider-enforced schema; callers validate every value against the transmitted
evidence before exposing it to consumers.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cti_app.application.advisor_dossier import build_advisor_dossier
from cti_app.application.production_parsers import sanitize_bridge_output_text

ADVISOR_WIRE_VERSION = "advisor-wire.v1"
_HEADER = re.compile(r"(?im)^\s*(?:>\s*)*(?:[-*+]\s*)?(?:FORMAT\s*:\s*)?advisor-wire\.v1\s*$")
_FIELD = re.compile(r"^\s*(?:[-*+]\s*)?([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")
_RECORD = re.compile(r"^\s*(?:[-*+]\s*)?(NODE|EDGE)\s+(.+?)\s*$", re.I)


@dataclass(frozen=True, slots=True)
class AdvisorWireDocument:
    has_header: bool
    fields: Mapping[str, tuple[str, ...]]
    records: tuple[tuple[str, str], ...]
    source: str | None
    source_language: str | None
    source_count: int
    ignored_prose: bool = False


@dataclass(frozen=True, slots=True)
class _RawSourceBlock:
    language: str | None
    source: str | None
    start: int
    end: int


def parse_advisor_wire(raw: str) -> AdvisorWireDocument:
    """Read wire fields and one complete, unambiguous raw source block."""
    text = _sanitize_wire_text(raw if isinstance(raw, str) else "")
    header = _HEADER.search(text)
    region = text[header.end() :] if header else text
    source_blocks = _raw_source_blocks(region)
    source, language, source_count = _source_result(source_blocks)
    fields: dict[str, list[str]] = defaultdict(list)
    records: list[tuple[str, str]] = []
    ignored_prose = False
    position = 0
    for raw_line in region.splitlines(keepends=True):
        line_start = position
        position += len(raw_line)
        if any(block.start <= line_start < block.end for block in source_blocks):
            continue
        line = raw_line.lstrip()
        line = re.sub(r"^\s*>\s?", "", line)
        record_match = _RECORD.match(line)
        if record_match is not None:
            key, value = record_match.groups()
            normalized = key.casefold()
            fields[normalized].append(value.strip())
            records.append((normalized, value.strip()))
            continue
        match = _FIELD.match(line)
        if match is None:
            ignored_prose = ignored_prose or bool(line.strip())
            continue
        key, value = match.groups()
        normalized = key.casefold().replace("-", "_")
        if normalized in {"format", "end", "end_advisor"}:
            continue
        fields[normalized].append(value.strip())
        records.append((normalized, value.strip()))
    return AdvisorWireDocument(
        has_header=header is not None,
        fields={key: tuple(values) for key, values in fields.items()},
        records=tuple(records),
        source=source,
        source_language=language,
        source_count=source_count,
        ignored_prose=ignored_prose,
    )


def advisor_wire_aliases(prompt_data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Assign stable short aliases to refs, measurements, facts and assertions."""
    refs: list[tuple[str, str, str | None]] = []

    ref_indexes: dict[tuple[str, str], int] = {}

    def add(kind: object, identifier: object, sha256: object = None) -> None:
        if not isinstance(kind, str) or not isinstance(identifier, str) or not identifier:
            return
        key = (kind, identifier)
        digest = sha256 if isinstance(sha256, str) else None
        index = ref_indexes.get(key)
        if index is None:
            ref_indexes[key] = len(refs)
            refs.append((kind, identifier, digest))
        elif refs[index][2] is None and digest is not None:
            refs[index] = (kind, identifier, digest)

    for item in prompt_data.get("transmitted_refs", ()):
        if isinstance(item, Mapping):
            add(item.get("kind"), item.get("id"), item.get("sha256"))
    for unit in prompt_data.get("units", ()):
        if not isinstance(unit, Mapping):
            continue
        for key, kind in (("evidence_refs", "observation"), ("finding_refs", "finding")):
            for item in unit.get(key, ()):
                if isinstance(item, str):
                    add(kind, item)
    review_text = prompt_data.get("review_text")
    if isinstance(review_text, Mapping):
        for key, kind in (("evidence_refs", "observation"), ("finding_refs", "finding")):
            for item in review_text.get(key, ()):
                if isinstance(item, str):
                    add(kind, item)
    for item in prompt_data.get("evidence", ()):
        if isinstance(item, Mapping):
            add("observation", item.get("evidence_id"), item.get("sha256"))
    for item in prompt_data.get("findings", ()):
        if isinstance(item, Mapping):
            add("finding", item.get("finding_id"), item.get("sha256"))
    for item in prompt_data.get("assertions", ()):
        if not isinstance(item, Mapping):
            continue
        for ref in item.get("refs", ()):
            if isinstance(ref, Mapping):
                add(ref.get("kind"), ref.get("id"), ref.get("sha256"))
    for key, kind in (("evidence_refs", "observation"), ("observation_refs", "observation")):
        for item in prompt_data.get(key, ()):
            if isinstance(item, str):
                add(kind, item)
    article_ref = prompt_data.get("article_source_ref")
    if isinstance(article_ref, str):
        article_id = (
            article_ref.partition(":")[2] if article_ref.startswith("article:") else article_ref
        )
        transmitted_article_ids = {
            str(item.get("id"))
            for item in prompt_data.get("transmitted_refs", ())
            if isinstance(item, Mapping) and item.get("kind") == "article"
        }
        if article_id not in transmitted_article_ids:
            add("article", article_ref)

    raw_manifest = prompt_data.get("dossier_manifest")
    if isinstance(raw_manifest, (list, tuple)):
        refs = []
        ref_indexes = {}
        for item in prompt_data.get("transmitted_refs", ()):
            if isinstance(item, Mapping):
                add(item.get("kind"), item.get("id"), item.get("sha256"))
        for item in raw_manifest:
            if not isinstance(item, Mapping):
                continue
            manifest_kind = item.get("kind")
            identifier = item.get("id")
            digest = item.get("sha256")
            if item.get("origin") == "versioned_review_target":
                continue
            if manifest_kind in {"article", "observation", "finding", "opinion"}:
                if (
                    manifest_kind == "article"
                    and isinstance(identifier, str)
                    and not identifier.startswith("article:")
                ):
                    identifier = f"article:{identifier}"
                add(manifest_kind, identifier, digest)
            elif manifest_kind in {"measurement", "fact"}:
                add("observation", item.get("evidence_id"), item.get("evidence_sha256"))

    ref_aliases = {
        f"R{index}": {"kind": kind, "id": identifier, **({"sha256": sha} if sha else {})}
        for index, (kind, identifier, sha) in enumerate(refs, start=1)
    }
    measurement_rows = [
        item
        for item in prompt_data.get("measurements", ())
        if isinstance(item, Mapping) and isinstance(item.get("measurement_id"), str)
    ]
    measurement_aliases = {
        f"M{index}": item for index, item in enumerate(measurement_rows, start=1)
    }
    fact_rows = [
        item
        for item in prompt_data.get("network_facts", ())
        if isinstance(item, Mapping) and isinstance(item.get("fact_id"), str)
    ]
    fact_aliases = {f"F{index}": item for index, item in enumerate(fact_rows, start=1)}
    assertion_rows = [
        item
        for item in prompt_data.get("assertions", ())
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    ]
    assertion_aliases = {f"A{index}": item for index, item in enumerate(assertion_rows, start=1)}
    dossier = build_advisor_dossier(prompt_data)
    raw_manifest = prompt_data.get("dossier_manifest")
    manifest = (
        [dict(item) for item in raw_manifest if isinstance(item, Mapping)]
        if isinstance(raw_manifest, (list, tuple))
        else [dict(item) for item in dossier.manifest]
    )
    if isinstance(raw_manifest, (list, tuple)):
        selected_measurement_ids = {
            str(item.get("measurement_id"))
            for item in manifest
            if isinstance(item.get("measurement_id"), str)
        }
        selected_fact_ids = {
            str(item.get("fact_id")) for item in manifest if isinstance(item.get("fact_id"), str)
        }
        measurement_rows = [
            item
            for item in measurement_rows
            if item.get("measurement_id") in selected_measurement_ids
        ]
        measurement_aliases = {
            f"M{index}": item for index, item in enumerate(measurement_rows, start=1)
        }
        fact_rows = [item for item in fact_rows if item.get("fact_id") in selected_fact_ids]
        fact_aliases = {f"F{index}": item for index, item in enumerate(fact_rows, start=1)}
    evidence_aliases = {}
    for item in manifest:
        alias = item.get("alias")
        if not isinstance(alias, str):
            continue
        if item.get("origin") == "versioned_review_target":
            continue
        evidence = dict(item)
        if evidence.get("kind") == "article" and isinstance(evidence.get("id"), str):
            identifier = evidence["id"]
            if not identifier.startswith("article:"):
                evidence["id"] = f"article:{identifier}"
        elif evidence.get("kind") == "opinion" and isinstance(evidence.get("source_ref"), str):
            evidence["id"] = evidence["source_ref"]
        evidence_aliases[alias] = evidence
    return {
        "refs": ref_aliases,
        "measurements": measurement_aliases,
        "facts": fact_aliases,
        "assertions": assertion_aliases,
        "evidence": evidence_aliases,
    }


def render_advisor_wire_input(
    prompt_data: Mapping[str, Any], *, dossier_text: str | None = None
) -> str:
    """Render only selected, human-readable task facts; never serialize internal data."""
    if dossier_text is not None:
        return dossier_text
    return build_advisor_dossier(prompt_data).text


def _raw_source_block(text: str) -> tuple[str | None, str | None, int]:
    """Return a source only when exactly one complete source block was found."""
    return _source_result(_raw_source_blocks(_sanitize_wire_text(text)))


def _source_result(
    blocks: list[_RawSourceBlock],
) -> tuple[str | None, str | None, int]:
    if len(blocks) != 1:
        return None, None, len(blocks)
    block = blocks[0]
    return block.source, block.language, 1


def _sanitize_wire_text(text: str) -> str:
    cleaned = sanitize_bridge_output_text(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        line
        for line in cleaned.split("\n")
        if line.strip().casefold() not in {"copy code", "copier le code"}
    )


def _raw_source_blocks(text: str) -> list[_RawSourceBlock]:
    blocks: list[_RawSourceBlock] = []
    fenced_pattern = re.compile(
        r"(?im)^[ \t]*```[ \t]*(yara|suricata)[ \t]*\n(.*?)^[ \t]*```[ \t]*$",
        re.S,
    )
    marker_pattern = re.compile(
        r"(?im)^[ \t]*BEGIN_SOURCE(?:[ \t]+(yara|suricata))?[ \t]*\n(.*?)"
        r"^[ \t]*END_SOURCE[ \t]*$",
        re.S,
    )
    wrapped_spans: list[tuple[int, int]] = []

    for match in fenced_pattern.finditer(text):
        blocks.append(
            _RawSourceBlock(match.group(1).casefold(), match.group(2), match.start(), match.end())
        )
        wrapped_spans.append((match.start(), match.end()))
    for match in marker_pattern.finditer(text):
        blocks.append(
            _RawSourceBlock(
                (match.group(1) or "").casefold() or None,
                match.group(2),
                match.start(),
                match.end(),
            )
        )
        wrapped_spans.append((match.start(), match.end()))

    for pattern, language_group in (
        (re.compile(r"(?im)^[ \t]*```[ \t]*(yara|suricata)[ \t]*$"), 1),
        (re.compile(r"(?im)^[ \t]*BEGIN_SOURCE(?:[ \t]+(yara|suricata))?[ \t]*$"), 1),
    ):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in wrapped_spans):
                continue
            language_value = match.group(language_group) or ""
            block = _RawSourceBlock(
                language_value.casefold() or None, None, match.start(), len(text)
            )
            blocks.append(block)
            wrapped_spans.append((match.start(), len(text)))

    lines = text.splitlines(keepends=True)
    offsets: list[tuple[int, int, str]] = []
    cursor = 0
    for line in lines:
        offsets.append((cursor, cursor + len(line), line.rstrip("\n")))
        cursor += len(line)

    def covered(start: int, end: int) -> bool:
        return any(
            start < block_end and end > block_start for block_start, block_end in wrapped_spans
        )

    for index, (start, _end, raw_line) in enumerate(offsets):
        if covered(start, start + len(raw_line)):
            continue
        label = raw_line.strip().casefold()
        if label not in {"yara", "suricata"}:
            continue
        language = label
        opening_index = index + 1
        while opening_index < len(offsets) and not offsets[opening_index][2].strip():
            opening_index += 1
        if opening_index >= len(offsets):
            blocks.append(_RawSourceBlock(language, None, start, len(text)))
            wrapped_spans.append((start, len(text)))
            continue
        _opening_start, _opening_end, opening_line = offsets[opening_index]
        stripped_opening = opening_line.lstrip()
        if not stripped_opening.startswith("`") or stripped_opening.startswith("```"):
            continue
        prefix_length = len(opening_line) - len(stripped_opening)
        closing = [
            candidate
            for candidate in range(opening_index, len(offsets))
            if offsets[candidate][2].rstrip().endswith("`")
            and not offsets[candidate][2].rstrip().endswith("``")
            and not offsets[candidate][2].lstrip().startswith("```")
        ]
        if len(closing) != 1:
            blocks.append(_RawSourceBlock(language, None, start, len(text)))
            wrapped_spans.append((start, len(text)))
            continue
        closing_index = closing[0]
        _close_start, close_end, close_line = offsets[closing_index]
        opening_content = opening_line[prefix_length + 1 :]
        if closing_index == opening_index:
            if not opening_content.endswith("`"):
                blocks.append(_RawSourceBlock(language, None, start, close_end))
                wrapped_spans.append((start, close_end))
                continue
            body_lines = [opening_content[:-1]]
        else:
            close_content = close_line.rstrip()
            close_content = close_content[:-1]
            body_lines = [opening_content]
            body_lines.extend(
                line for _line_start, _line_end, line in offsets[opening_index + 1 : closing_index]
            )
            body_lines.append(close_content)
        source_text = "\n".join(body_lines).strip("\n")
        source_value: str | None = source_text if "`" not in source_text else None
        blocks.append(_RawSourceBlock(language, source_value, start, close_end))
        wrapped_spans.append((start, close_end))

    masked = _mask_source_spans(text, wrapped_spans)
    yara_declarations = list(
        re.finditer(
            r"(?im)^[ \t]*(?:(?:private|global)[ \t]+)*rule[ \t]+[A-Za-z_][A-Za-z0-9_]*"
            r"[ \t]*(?:\n[ \t]*)?\{",
            masked,
        )
    )
    yara_spans: list[tuple[int, int]] = []
    for match in yara_declarations:
        if covered(match.start(), match.end()):
            continue
        end = _balanced_yara_rule_end(text, match.end() - 1)
        if end is None:
            blocks.append(_RawSourceBlock("yara", None, match.start(), len(text)))
            yara_spans.append((match.start(), len(text)))
        else:
            blocks.append(_RawSourceBlock("yara", text[match.start() : end], match.start(), end))
            yara_spans.append((match.start(), end))
    masked = _mask_source_spans(text, [*wrapped_spans, *yara_spans])

    action_line = re.compile(r"(?i)^[ \t]*(?:alert|drop|pass)\b[^\n]*;[ \t]*$")
    incomplete_action = re.compile(r"(?i)^[ \t]*(?:alert|drop|pass)\b[^\n]*$")
    action_lines: list[tuple[int, int, str, bool]] = []
    cursor = 0
    for line in masked.splitlines(keepends=True):
        body = line.rstrip("\n")
        if action_line.fullmatch(body):
            action_lines.append((cursor, cursor + len(line), body, True))
        elif incomplete_action.fullmatch(body):
            action_lines.append((cursor, cursor + len(line), body, False))
        cursor += len(line)
    groups: list[list[tuple[int, int, str, bool]]] = []
    for item in action_lines:
        if groups:
            gap_start, gap_end = groups[-1][-1][1], item[0]
            if not text[gap_start:gap_end].strip():
                groups[-1].append(item)
                continue
        groups.append([item])
    for group in groups:
        start, end = group[0][0], group[-1][1]
        if any(not complete for _start, _end, _line, complete in group):
            blocks.append(_RawSourceBlock("suricata", None, start, end))
        else:
            blocks.append(_RawSourceBlock("suricata", text[start:end].rstrip("\n"), start, end))

    return sorted(blocks, key=lambda block: (block.start, block.end))


def _mask_source_spans(text: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return text
    characters = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(characters), end)):
            if characters[index] != "\n":
                characters[index] = " "
    return "".join(characters)


def _balanced_yara_rule_end(text: str, opening_brace: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    regex = False
    previous = ""
    index = opening_brace
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if regex:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "/":
                regex = False
            index += 1
            continue
        if char == "/" and following == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if char in {'"', "'"}:
            quote = char
            index += 1
            continue
        if char == "/" and previous == "=":
            regex = True
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
            if depth < 0:
                return None
        if not char.isspace():
            previous = char
        index += 1
    return None


__all__ = [
    "ADVISOR_WIRE_VERSION",
    "AdvisorWireDocument",
    "advisor_wire_aliases",
    "parse_advisor_wire",
    "render_advisor_wire_input",
]
