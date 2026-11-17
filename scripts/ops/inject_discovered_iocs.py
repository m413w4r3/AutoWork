#!/usr/bin/env python3
"""Inject CSV-discovered IOC groups into edition render data (stdlib only, offline)."""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_TYPE_LABELS = {
    "file": ("Fichiers", "hash"),
    "ip": ("IPv4", "ip"),
    "url": ("URLs", "url"),
    "domain": ("Domaines", "domain"),
}
_TYPE_ORDER = tuple(_TYPE_LABELS)
_HASH_VALUE = re.compile(
    r"^(?:(?:md5|sha-?1|sha-?256)[:=])?(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$",
    re.IGNORECASE,
)
_HASH_PREFIX = re.compile(r"^(?:md5|sha-?1|sha-?256)[:=]", re.IGNORECASE)
_ARTIFACT_TYPE_TO_CSV_TYPE = {
    "hash": "file",
    "ip": "ip",
    "url": "url",
    "domain": "domain",
    "filename": "file",
    "filepath": "file",
}
_HEADING_TO_CSV_TYPE = {
    "fichiers": "file",
    "ipv4": "ip",
    "urls": "url",
    "domaines": "domain",
}
_DEFAULT_ITEM_TEMPLATE: dict[str, Any] = {
    "artifact_type": "",
    "type_label": "",
    "value": {"text": "", "break_chunks": [], "break_after": []},
    "source_refs": [],
    "evidence": [],
    "context": "",
    "draft_refs": [],
    "qualification": "",
    "relationship_status_label": "",
    "indicator_status_label": "",
}


class InjectionError(ValueError):
    """Edition render data or IOC CSV does not match the expected delivery shape."""


def _read_new_rows(path: Path) -> list[tuple[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header: list[str] | None = None
        for row in reader:
            normalized = [cell.strip().casefold() for cell in row]
            if len(normalized) == 3 and set(normalized) == {"isnew", "type", "value"}:
                header = normalized
                break
        if header is None:
            raise InjectionError(f"{path}: expected an isNew,type,value header")
        indexes = {name: header.index(name) for name in ("isnew", "type", "value")}
        rows: list[tuple[str, str]] = []
        for row_number, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) != 3:
                raise InjectionError(f"{path}: unexpected row shape at data row {row_number}")
            is_new = row[indexes["isnew"]].strip()
            if is_new not in {"0", "1"}:
                raise InjectionError(f"{path}: isNew must be 0 or 1 at data row {row_number}")
            if is_new != "1":
                continue
            ioc_type = row[indexes["type"]].strip().casefold()
            value = row[indexes["value"]].strip()
            if ioc_type not in _TYPE_LABELS:
                raise InjectionError(f"{path}: unsupported IOC type at data row {row_number}")
            if not value:
                raise InjectionError(f"{path}: new IOC value is empty at data row {row_number}")
            rows.append((ioc_type, value))
        return rows


def _ioc_identity(ioc_type: str, value: str) -> tuple[str, str]:
    normalized = value.strip()
    if ioc_type == "file":
        normalized = _HASH_PREFIX.sub("", normalized).casefold()
    elif ioc_type == "domain":
        normalized = normalized.replace("[.]", ".").rstrip(".").casefold()
    elif ioc_type == "ip":
        candidate = normalized.replace("[.]", ".")
        try:
            normalized = ipaddress.ip_address(candidate).compressed.casefold()
        except ValueError:
            normalized = candidate.casefold()
    elif ioc_type == "url":
        candidate = normalized.replace("[.]", ".")
        parts = urlsplit(candidate)
        if parts.scheme and parts.netloc:
            normalized = (
                parts.scheme.casefold()
                + "://"
                + parts.netloc.casefold()
                + parts.path
                + (f"?{parts.query}" if parts.query else "")
                + (f"#{parts.fragment}" if parts.fragment else "")
            )
        else:
            normalized = candidate
    return ioc_type, normalized


def _value_text(item: dict[str, Any], *, path: Path, field: str) -> str:
    value = item.get("value")
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        raise InjectionError(f"{path}: expected {field} item values with a text field")
    return value["text"]


def _group_type(group: dict[str, Any]) -> str | None:
    heading = group.get("heading")
    if isinstance(heading, str):
        mapped = _HEADING_TO_CSV_TYPE.get(heading.strip().casefold())
        if mapped is not None:
            return mapped
    for item in group.get("items", []):
        if isinstance(item, dict):
            artifact_type = item.get("artifact_type")
            if isinstance(artifact_type, str):
                mapped = _ARTIFACT_TYPE_TO_CSV_TYPE.get(artifact_type.casefold())
                if mapped is not None:
                    return mapped
    return None


def _ioc_keys(groups: list[dict[str, Any]], *, path: Path, field: str) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            raise InjectionError(f"{path}: expected {field} groups with item lists")
        ioc_type = _group_type(group)
        if ioc_type is None:
            continue
        for item in group["items"]:
            if not isinstance(item, dict):
                raise InjectionError(f"{path}: expected {field} items to be objects")
            value = _value_text(item, path=path, field=field)
            keys.add(_ioc_identity(ioc_type, value))
    return keys


def _item_template(source_groups: list[dict[str, Any]]) -> dict[str, Any]:
    for group in source_groups:
        for item in group.get("items", []):
            if isinstance(item, dict):
                return item
    return _DEFAULT_ITEM_TEMPLATE


def _empty_like(value: Any) -> Any:
    if isinstance(value, list):
        return []
    if isinstance(value, dict):
        return {}
    if isinstance(value, str):
        return ""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0
    return None


def _display_value(value: str) -> dict[str, Any]:
    if _HASH_VALUE.fullmatch(value):
        chunks = [value[start : start + 8] for start in range(0, len(value), 8)]
        return {
            "text": value,
            "break_chunks": chunks,
            "break_after": [True] * max(0, len(chunks) - 1),
        }
    return {"text": value}


def _new_item(
    template: dict[str, Any], *, ioc_type: str, value: str
) -> dict[str, Any]:
    heading, artifact_type = _TYPE_LABELS[ioc_type]
    item = {key: _empty_like(template_value) for key, template_value in template.items()}
    item.update(
        {
            "artifact_type": artifact_type,
            "type_label": heading,
            "value": _display_value(value),
        }
    )
    return item


def _validate_ioc_section(section: dict[str, Any], *, path: Path) -> None:
    for key in ("source_indicators", "original_indicators"):
        groups = section.get(key)
        if not isinstance(groups, list):
            raise InjectionError(f"{path}: IOC section must contain a {key} list")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("items"), list):
                raise InjectionError(f"{path}: {key} must contain groups with item lists")
            heading = group.get("heading")
            if not isinstance(heading, str) or not heading.strip():
                raise InjectionError(f"{path}: {key} group heading must be non-empty text")
            for item in group["items"]:
                if not isinstance(item, dict):
                    raise InjectionError(f"{path}: {key} group items must be objects")
                _value_text(item, path=path, field=key)


def _publication_number(publication: dict[str, Any]) -> str:
    number = publication.get("number")
    if isinstance(number, int) and not isinstance(number, bool):
        number = str(number)
    if not isinstance(number, str) or not number.isdigit():
        raise InjectionError("edition render data publications require numeric numbers")
    return number.zfill(2)


def inject_discovered_iocs(data: object, ioc_csv_root: Path) -> dict[str, Any]:
    """Merge new IOC CSV rows into matching publication original_indicators groups."""
    if not isinstance(data, dict) or not isinstance(data.get("publications"), list):
        raise InjectionError("edition render data must contain a publications list")
    publications = data["publications"]
    seen_numbers: set[str] = set()
    for publication in publications:
        if not isinstance(publication, dict):
            raise InjectionError("edition render data publications must be objects")
        number = _publication_number(publication)
        if number in seen_numbers:
            raise InjectionError("edition render data publication numbers must be unique")
        seen_numbers.add(number)
        sections = publication.get("content_sections")
        if not isinstance(sections, list) or any(not isinstance(item, dict) for item in sections):
            raise InjectionError(f"publication {number} must contain content_sections objects")

        csv_path = ioc_csv_root / f"art{number}" / "ioc_pipeline_article.csv"
        if not csv_path.is_file():
            raise InjectionError(f"missing IOC CSV for publication {number}: {csv_path}")
        new_rows = _read_new_rows(csv_path)
        if not new_rows:
            continue

        ioc_sections = [section for section in sections if section.get("type") == "ioc"]
        if len(ioc_sections) != 1:
            raise InjectionError(f"publication {number} must contain exactly one IOC section")
        ioc_section = ioc_sections[0]
        _validate_ioc_section(ioc_section, path=csv_path)
        source_groups = ioc_section["source_indicators"]
        original_groups = ioc_section["original_indicators"]
        source_keys = _ioc_keys(source_groups, path=csv_path, field="source_indicators")
        original_keys = _ioc_keys(original_groups, path=csv_path, field="original_indicators")
        template = _item_template(source_groups)

        new_by_type: dict[str, list[str]] = {ioc_type: [] for ioc_type in _TYPE_ORDER}
        seen_new: set[tuple[str, str]] = set()
        for ioc_type, value in new_rows:
            identity = _ioc_identity(ioc_type, value)
            if identity in source_keys or identity in original_keys or identity in seen_new:
                continue
            seen_new.add(identity)
            new_by_type[ioc_type].append(value)

        for ioc_type in _TYPE_ORDER:
            values = new_by_type[ioc_type]
            if not values:
                continue
            heading, _artifact_type = _TYPE_LABELS[ioc_type]
            group = next(
                (item for item in original_groups if item.get("heading") == heading),
                None,
            )
            if group is None:
                group = {"heading": heading, "items": []}
                original_groups.append(group)
            for value in values:
                group["items"].append(
                    _new_item(template, ioc_type=ioc_type, value=value)
                )
            original_keys.update(_ioc_identity(ioc_type, value) for value in values)

    return data


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", required=True, type=Path, help="edition-render-data.json")
    parser.add_argument(
        "--ioc-csv-root",
        required=True,
        type=Path,
        help="directory containing artNN/ioc_pipeline_article.csv",
    )
    parser.add_argument("--output", type=Path, help="output JSON (defaults to rewriting --data)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        data = json.loads(args.data.read_text(encoding="utf-8"))
        updated = inject_discovered_iocs(data, args.ioc_csv_root)
        destination = args.output or args.data
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeError, json.JSONDecodeError, InjectionError) as exc:
        raise SystemExit(f"inject_discovered_iocs: {exc}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
