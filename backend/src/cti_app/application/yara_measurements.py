"""Deterministic byte decoding shared by local YARA provenance checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


def measurement_value_bytes(row: Mapping[str, Any] | None) -> bytes | None:
    if row is None:
        return None
    value = row.get("value")
    kind = row.get("kind")
    encoding = row.get("encoding")
    if not isinstance(value, str) or not isinstance(kind, str) or not isinstance(encoding, str):
        return None
    try:
        if kind == "hex" or encoding == "raw":
            compact = "".join(value.split())
            if not compact or len(compact) % 2 or re.fullmatch(r"[0-9a-fA-F]+", compact) is None:
                return None
            measured = bytes.fromhex(compact)
        elif kind == "wide" or encoding in {"wide", "utf16le"}:
            measured = value.encode("utf-16le", errors="strict")
        elif encoding == "ascii":
            measured = value.encode("ascii", errors="strict")
        elif encoding in {"utf8", "utf-8"}:
            measured = value.encode("utf-8", errors="strict")
        else:
            return None
    except (UnicodeEncodeError, ValueError):
        return None
    return measured if len(measured) == row.get("length") else None


def yara_string_names(source: str) -> tuple[str, ...]:
    strings_match = re.search(r"(?is)\bstrings\s*:(.*?)\bcondition\s*:", source)
    if strings_match is None:
        return ()
    strings_section = _strip_yara_comments(strings_match.group(1))
    return tuple(dict.fromkeys(re.findall(r"(\$[A-Za-z_][A-Za-z0-9_]*)\s*=", strings_section)))


def exact_yara_string_bytes(source: str, identifier: str) -> bytes | None:
    strings_match = re.search(r"(?is)\bstrings\s*:(.*?)\bcondition\s*:", source)
    if strings_match is None:
        return None
    strings_section = _strip_yara_comments(strings_match.group(1))
    declaration = re.compile(
        rf'(?s){re.escape(identifier)}\s*=\s*(\{{.*?\}}|"(?:\\.|[^"\\])*"|/(?:\\.|[^/\\])*/)([^\r\n]*)'
    )
    matches = list(declaration.finditer(strings_section))
    if len(matches) != 1:
        return None
    pattern, raw_modifiers = matches[0].groups()
    modifiers = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", raw_modifiers, flags=re.S)
    modifier_set = set(modifiers.split())
    if pattern.startswith("{"):
        body = re.sub(r"/\*.*?\*/|//[^\r\n]*", "", pattern[1:-1], flags=re.S)
        byte_pairs = body.split()
        if not byte_pairs or any(
            re.fullmatch(r"[0-9a-fA-F]{2}", item) is None for item in byte_pairs
        ):
            return None
        if modifier_set:
            return None
        return bytes.fromhex("".join(byte_pairs))
    if not pattern.startswith('"') or modifier_set - {"ascii", "wide", "fullword"}:
        return None
    decoded = _decode_yara_string_literal(pattern)
    if decoded is None or {"ascii", "wide"}.issubset(modifier_set):
        return None
    text, raw_bytes = decoded
    if "wide" in modifier_set:
        return text.encode("utf-16le", errors="strict")
    return raw_bytes


def _strip_yara_comments(source: str) -> str:
    output = list(source)
    state = "plain"
    index = 0
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if state == "plain":
            if character == "/" and following == "*":
                output[index] = output[index + 1] = " "
                index += 2
                state = "block_comment"
                continue
            if character == "/" and following == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "line_comment"
                continue
            if character == '"':
                state = "quoted_string"
            elif character == "/":
                state = "regex"
        elif state == "block_comment":
            if character == "*" and following == "/":
                output[index] = output[index + 1] = " "
                index += 2
                state = "plain"
                continue
            if character != "\n":
                output[index] = " "
        elif state == "line_comment":
            if character == "\n":
                state = "plain"
            else:
                output[index] = " "
        elif state in {"quoted_string", "regex"}:
            delimiter = '"' if state == "quoted_string" else "/"
            if character == "\\":
                index += 2
                continue
            if character == delimiter:
                state = "plain"
        index += 1
    return "".join(output)


def _decode_yara_string_literal(literal: str) -> tuple[str, bytes] | None:
    if len(literal) < 2 or literal[0] != '"' or literal[-1] != '"':
        return None
    text: list[str] = []
    index = 1
    while index < len(literal) - 1:
        character = literal[index]
        index += 1
        if character != "\\":
            text.append(character)
            continue
        if index >= len(literal) - 1:
            return None
        escape = literal[index]
        index += 1
        escaped = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}.get(escape)
        if escaped is not None:
            text.append(escaped)
            continue
        if escape == "x" and index + 2 <= len(literal) - 1:
            digits = literal[index : index + 2]
            if re.fullmatch(r"[0-9a-fA-F]{2}", digits) is None:
                return None
            text.append(chr(int(digits, 16)))
            index += 2
            continue
        return None
    decoded = "".join(text)
    try:
        return decoded, decoded.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
