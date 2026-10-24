"""Canonical keyset cursors and omission proofs for operator context pages."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextOmissionReason(StrEnum):
    BUDGET = "budget"
    SENSITIVITY = "sensitivity"
    NOT_SELECTED = "not_selected"
    TOMBSTONE = "tombstone"
    SOURCE_NOT_PAGINATED = "source_non_paginée"


class ContextOmissionManifestEntryV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    section: str = Field(min_length=1, max_length=64)
    total: int = Field(ge=0)
    selected: int = Field(ge=0)
    omitted: int = Field(ge=0)
    source_bounded: bool
    source_ref: str = Field(min_length=1, max_length=2_048)
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    omission_reasons: dict[ContextOmissionReason, int] = Field(default_factory=dict)
    unbounded_reason: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_counts(self) -> ContextOmissionManifestEntryV1:
        if self.selected + self.omitted != self.total:
            raise ValueError("Selected and omitted counts must equal total")
        if sum(self.omission_reasons.values()) != self.omitted:
            raise ValueError("Omission reasons must account for every omitted item")
        if self.source_bounded and self.unbounded_reason is not None:
            raise ValueError("Bounded sources cannot declare an unbounded reason")
        if not self.source_bounded and not self.unbounded_reason:
            raise ValueError("Unbounded sources require an explicit reason")
        return self


class KeysetPageV1[T](BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, arbitrary_types_allowed=True
    )

    items: tuple[T, ...]
    total: int = Field(ge=0)
    has_more: bool
    next_cursor: str | None = Field(default=None, max_length=1_024)
    page_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=1, max_length=2_048)


def encode_keyset_cursor(
    *,
    section: str,
    investigation_id: UUID,
    sort_key: str,
    item_id: UUID,
    previous_page_sha256: str,
    descending: bool = False,
    context_sha256: str | None = None,
) -> str:
    payload = {
        "v": 1,
        "kind": "keyset",
        "section": section,
        "investigation_id": str(investigation_id),
        "sort_key": sort_key,
        "item_id": str(item_id),
        "previous_page_sha256": previous_page_sha256,
        "descending": descending,
    }
    if context_sha256 is not None:
        payload["context_sha256"] = context_sha256
    return _encode_cursor(payload)


def decode_keyset_cursor(
    cursor: str | None,
    *,
    section: str,
    investigation_id: UUID,
    descending: bool | None = None,
    context_sha256: str | None = None,
) -> dict[str, Any] | None:
    if cursor is None:
        return None
    payload = _decode_cursor(cursor)
    if (
        payload.get("kind") != "keyset"
        or payload.get("section") != section
        or payload.get("investigation_id") != str(investigation_id)
        or not isinstance(payload.get("sort_key"), str)
        or not isinstance(payload.get("item_id"), str)
        or not isinstance(payload.get("previous_page_sha256"), str)
        or not _is_sha256(payload["previous_page_sha256"])
        or not isinstance(payload.get("descending"), bool)
        or (descending is not None and payload["descending"] is not descending)
        or (
            context_sha256 is not None
            and payload.get("context_sha256") != context_sha256
        )
    ):
        raise ValueError("Context keyset cursor is invalid")
    try:
        UUID(payload["item_id"])
    except ValueError:
        raise ValueError("Context keyset cursor is invalid") from None
    return payload


def encode_offset_cursor(
    *,
    section: str,
    investigation_id: UUID,
    context_sha256: str,
    offset: int,
    previous_page_sha256: str,
) -> str:
    return _encode_cursor(
        {
            "v": 1,
            "kind": "offset",
            "section": section,
            "investigation_id": str(investigation_id),
            "context_sha256": context_sha256,
            "offset": offset,
            "previous_page_sha256": previous_page_sha256,
        }
    )


def decode_offset_cursor(
    cursor: str, *, section: str, investigation_id: UUID, context_sha256: str
) -> dict[str, Any]:
    payload = _decode_cursor(cursor)
    if (
        payload.get("kind") != "offset"
        or payload.get("section") != section
        or payload.get("investigation_id") != str(investigation_id)
        or payload.get("context_sha256") != context_sha256
        or isinstance(payload.get("offset"), bool)
        or not isinstance(payload.get("offset"), int)
        or payload["offset"] < 0
        or not isinstance(payload.get("previous_page_sha256"), str)
        or not _is_sha256(payload["previous_page_sha256"])
    ):
        raise ValueError("Context offset cursor is invalid")
    return payload


def canonical_page_sha256(
    *,
    section: str,
    investigation_id: UUID,
    items: Any,
    previous_page_sha256: str,
) -> str:
    payload = {
        "schema": "investigation-context-page.v1",
        "section": section,
        "investigation_id": str(investigation_id),
        "previous_page_sha256": previous_page_sha256,
        "items": _canonicalize(items),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def verify_page_sha256(
    *,
    claimed_sha256: str,
    section: str,
    investigation_id: UUID,
    items: Any,
    previous_page_sha256: str,
) -> bool:
    return claimed_sha256 == canonical_page_sha256(
        section=section,
        investigation_id=investigation_id,
        items=items,
        previous_page_sha256=previous_page_sha256,
    )


def source_reference(
    *, section: str, investigation_id: UUID, cursor: str | None, page_sha256: str, limit: int
) -> str:
    start = "start" if cursor is None else f"cursor={cursor}"
    return (
        f"investigation:{investigation_id}:{section}:start={start}:limit={limit}:"
        f"sha256={page_sha256}"
    )


def make_keyset_page[T](
    *,
    section: str,
    investigation_id: UUID,
    items: tuple[T, ...],
    total: int,
    limit: int,
    cursor: str | None,
    position: Any,
    descending: bool = False,
) -> KeysetPageV1[T]:
    has_more = len(items) > limit
    selected = items[:limit]
    previous_page_sha256 = (
        decode_keyset_cursor(
            cursor,
            section=section,
            investigation_id=investigation_id,
            descending=descending,
        )[
            "previous_page_sha256"
        ]
        if cursor is not None
        else "0" * 64
    )
    page_sha256 = canonical_page_sha256(
        section=section,
        investigation_id=investigation_id,
        items=selected,
        previous_page_sha256=previous_page_sha256,
    )
    next_cursor = None
    if has_more and selected:
        last_sort_key, last_item_id = position(selected[-1])
        next_cursor = encode_keyset_cursor(
            section=section,
            investigation_id=investigation_id,
            sort_key=_sort_key_text(last_sort_key),
            item_id=last_item_id,
            previous_page_sha256=page_sha256,
            descending=descending,
        )
    return KeysetPageV1(
        items=selected,
        total=total,
        has_more=has_more,
        next_cursor=next_cursor,
        page_sha256=page_sha256,
        source_ref=source_reference(
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            page_sha256=page_sha256,
            limit=limit,
        ),
    )


def _sort_key_text(sort_key: Any) -> str:
    return sort_key.isoformat() if isinstance(sort_key, datetime | date) else str(sort_key)


def _encode_cursor(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1_024:
        raise ValueError("Context cursor is invalid")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise ValueError("Context cursor is invalid") from None
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or _encode_cursor(payload) != cursor
    ):
        raise ValueError("Context cursor is invalid")
    return payload


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="python", by_alias=True))
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _canonicalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_canonicalize(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_canonicalize(item) for item in value), key=repr)
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
