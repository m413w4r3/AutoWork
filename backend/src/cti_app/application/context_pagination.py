"""Canonical keyset cursors and omission proofs for operator context pages."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum, StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContextOmissionReason(StrEnum):
    BUDGET = "budget"
    PAGINATION = "pagination"
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
    context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", exclude=True)
    cursor: str | None = Field(default=None, max_length=1_024, exclude=True)
    pivot: dict[str, str] | None = Field(default=None, exclude=True)
    descending: bool = Field(default=False, exclude=True)
    limit: int | None = Field(default=None, ge=1, le=1_000, exclude=True)
    has_more: bool = Field(default=False, exclude=True)
    cursor_authentication: Literal["unauthenticated_position"] | None = None

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
    cursor: str | None = Field(default=None, max_length=1_024)
    pivot: dict[str, str] | None = None
    descending: bool = False
    limit: int = Field(default=1, ge=1, le=1_000)
    context_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    omission_manifest: tuple[ContextOmissionManifestEntryV1, ...] = ()
    source_bounded: bool = True
    unbounded_reason: str | None = Field(default=None, max_length=256)
    cursor_authentication: Literal["unauthenticated_position"] = "unauthenticated_position"


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
    """Encode an unauthenticated position; authorization comes from the caller."""
    if not section or len(section) > 64 or not 1 <= len(sort_key) <= 256:
        raise ValueError("Context keyset cursor is invalid")
    try:
        datetime.fromisoformat(sort_key)
    except ValueError:
        raise ValueError("Context keyset cursor is invalid") from None
    if not _is_sha256(previous_page_sha256):
        raise ValueError("Context keyset cursor is invalid")
    if context_sha256 is not None and not _is_sha256(context_sha256):
        raise ValueError("Context keyset cursor is invalid")
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
    encoded = _encode_cursor(payload)
    if len(encoded) > 1_024:
        raise ValueError("Context keyset cursor is invalid")
    return encoded


def decode_keyset_cursor(
    cursor: str | None,
    *,
    section: str,
    investigation_id: UUID,
    descending: bool | None = None,
    context_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Decode a position-only cursor and bind it to the requested read scope.

    This cursor is deliberately not authenticated. A caller can move its pivot
    inside the already-authorized section; the page CAS records that pivot.
    """
    if cursor is None:
        return None
    payload = _decode_cursor(cursor)
    if (
        payload.get("kind") != "keyset"
        or payload.get("section") != section
        or payload.get("investigation_id") != str(investigation_id)
        or not isinstance(payload.get("sort_key"), str)
        or not 1 <= len(payload["sort_key"]) <= 256
        or any(ord(character) < 32 for character in payload["sort_key"])
        or not isinstance(payload.get("item_id"), str)
        or not isinstance(payload.get("previous_page_sha256"), str)
        or not _is_sha256(payload["previous_page_sha256"])
        or not isinstance(payload.get("descending"), bool)
        or set(payload) != _keyset_cursor_fields(has_context="context_sha256" in payload)
        or (descending is not None and payload["descending"] is not descending)
        or (context_sha256 is not None and payload.get("context_sha256") != context_sha256)
    ):
        raise ValueError("Context keyset cursor is invalid")
    try:
        item_id = UUID(payload["item_id"])
        datetime.fromisoformat(payload["sort_key"])
    except (TypeError, ValueError):
        raise ValueError("Context keyset cursor is invalid") from None
    if str(item_id) != payload["item_id"]:
        raise ValueError("Context keyset cursor is invalid")
    if "context_sha256" in payload and not _is_sha256(payload["context_sha256"]):
        raise ValueError("Context keyset cursor is invalid")
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
        or set(payload)
        != {
            "v",
            "kind",
            "section",
            "investigation_id",
            "context_sha256",
            "offset",
            "previous_page_sha256",
        }
    ):
        raise ValueError("Context offset cursor is invalid")
    return payload


def canonical_page_sha256(
    *,
    section: str,
    investigation_id: UUID,
    items: Any,
    previous_page_sha256: str,
    context_sha256: str | None = None,
    cursor: str | None = None,
    pivot: Mapping[str, Any] | None = None,
    descending: bool = False,
    limit: int = 1,
    total: int = 0,
    has_more: bool = False,
    omission_manifest: Any = (),
) -> str:
    if not _is_sha256(previous_page_sha256):
        raise ValueError("Previous page SHA-256 is invalid")
    if context_sha256 is not None and not _is_sha256(context_sha256):
        raise ValueError("Context SHA-256 is invalid")
    if limit < 1 or total < 0:
        raise ValueError("Context page bounds are invalid")
    payload = {
        "schema": "investigation-context-page.v1",
        "section": section,
        "investigation_id": str(investigation_id),
        "context_sha256": context_sha256,
        "cursor": cursor,
        "pivot": _canonicalize(pivot),
        "descending": descending,
        "limit": limit,
        "total": total,
        "has_more": has_more,
        "previous_page_sha256": previous_page_sha256,
        "items": _canonicalize(items),
        "omission_manifest": _canonical_omissions(omission_manifest),
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
    context_sha256: str | None = None,
    cursor: str | None = None,
    pivot: Mapping[str, Any] | None = None,
    descending: bool = False,
    limit: int = 1,
    total: int = 0,
    has_more: bool = False,
    omission_manifest: Any = (),
    next_cursor: str | None = None,
) -> bool:
    if next_cursor is not None:
        try:
            next_payload = _decode_cursor(next_cursor)
        except ValueError:
            return False
        if next_payload.get("previous_page_sha256") != claimed_sha256:
            return False
        if next_payload.get("kind") == "keyset":
            try:
                decode_keyset_cursor(
                    next_cursor,
                    section=section,
                    investigation_id=investigation_id,
                    descending=descending,
                    context_sha256=context_sha256,
                )
            except ValueError:
                return False
            if keyset_pivot(next_payload) != (dict(pivot) if pivot is not None else None):
                return False
        elif next_payload.get("kind") == "offset":
            try:
                decode_offset_cursor(
                    next_cursor,
                    section=section,
                    investigation_id=investigation_id,
                    context_sha256=context_sha256 or "",
                )
            except ValueError:
                return False
            expected_offset_pivot = {
                "kind": "offset",
                "section": section,
                "investigation_id": str(investigation_id),
                "context_sha256": context_sha256,
                "offset": str(next_payload.get("offset")),
            }
            if dict(pivot or {}) != expected_offset_pivot:
                return False
        else:
            return False
    if claimed_sha256 != canonical_page_sha256(
        section=section,
        investigation_id=investigation_id,
        items=items,
        previous_page_sha256=previous_page_sha256,
        context_sha256=context_sha256,
        cursor=cursor,
        pivot=pivot,
        descending=descending,
        limit=limit,
        total=total,
        has_more=has_more,
        omission_manifest=omission_manifest,
    ):
        return False

    omission_entries = _manifest_entries(omission_manifest)
    expected_source_ref = source_reference(
        section=section,
        investigation_id=investigation_id,
        cursor=cursor,
        page_sha256=claimed_sha256,
        limit=limit,
    )
    expected_omitted = max(0, total - len(items))
    expected_page = {
        "section": section,
        "total": total,
        "selected": len(items),
        "omitted": expected_omitted,
        "source_bounded": None,
        "source_ref": expected_source_ref,
        "page_sha256": claimed_sha256,
        "omission_reasons": (
            {ContextOmissionReason.PAGINATION: expected_omitted} if expected_omitted else {}
        ),
        "unbounded_reason": None,
        "context_sha256": context_sha256,
        "cursor": cursor,
        "pivot": dict(pivot) if pivot is not None else None,
        "descending": descending,
        "limit": limit,
        "has_more": has_more,
        "cursor_authentication": "unauthenticated_position",
    }
    for entry in omission_entries:
        entry_authentication = _field(entry, "cursor_authentication")
        entry_pivot = _field(entry, "pivot")
        is_keyset_manifest = (
            entry_authentication == "unauthenticated_position"
            or (isinstance(entry_pivot, Mapping) and entry_pivot.get("kind") == "keyset")
            or (pivot is not None and pivot.get("kind") == "keyset")
        )
        if not is_keyset_manifest:
            continue
        # Boundedness is itself included in the page digest. Preserve the value
        # from the entry here while checking every field that describes the page.
        expected_page["source_bounded"] = _field(entry, "source_bounded")
        expected_page["unbounded_reason"] = _field(entry, "unbounded_reason")
        if not verify_manifest_entry(
            entry,
            expected_page,
            section=section,
            investigation_id=investigation_id,
            expected_source_ref=expected_source_ref,
        ):
            return False
    return True


def verify_manifest_entry(
    entry: Any,
    page: Any,
    *,
    section: str | None = None,
    investigation_id: UUID | None = None,
    expected_source_ref: str | None = None,
) -> bool:
    """Check that a manifest row describes its page and its recomputed source reference."""
    page_section = _field(page, "section", section)
    page_investigation_id = _field(page, "investigation_id", investigation_id)
    cursor = _field(page, "cursor")
    page_sha256 = _field(page, "page_sha256")
    limit = _field(page, "limit")
    if expected_source_ref is None:
        if page_section is None or page_investigation_id is None:
            return False
        try:
            expected_source_ref = source_reference(
                section=page_section,
                investigation_id=page_investigation_id,
                cursor=cursor,
                page_sha256=page_sha256,
                limit=limit,
            )
        except (TypeError, ValueError):
            return False
    page_source_ref = _field(page, "source_ref")
    if page_source_ref is not _MISSING and page_source_ref != expected_source_ref:
        return False

    page_items = _field(page, "items", ())
    total = _field(page, "total")
    selected = _field(page, "selected", len(page_items))
    omitted = _field(page, "omitted", total - selected if isinstance(total, int) else None)
    omission_reasons = _field(
        page,
        "omission_reasons",
        {ContextOmissionReason.PAGINATION: omitted} if omitted else {},
    )
    expected = {
        "section": page_section,
        "total": total,
        "selected": selected,
        "omitted": omitted,
        "source_bounded": _field(page, "source_bounded"),
        "source_ref": expected_source_ref,
        "page_sha256": page_sha256,
        "omission_reasons": omission_reasons,
        "unbounded_reason": _field(page, "unbounded_reason"),
        "context_sha256": _field(page, "context_sha256"),
        "cursor": cursor,
        "pivot": _field(page, "pivot"),
        "descending": _field(page, "descending"),
        "limit": limit,
        "has_more": _field(page, "has_more"),
        "cursor_authentication": _field(
            page, "cursor_authentication", "unauthenticated_position"
        ),
    }
    for field, expected_value in expected.items():
        actual_value = _field(entry, field)
        if actual_value is _MISSING:
            if field in {
                "section",
                "total",
                "selected",
                "omitted",
                "source_bounded",
                "source_ref",
                "page_sha256",
                "omission_reasons",
            }:
                return False
            continue
        if field == "omission_reasons":
            if _canonicalize(actual_value) != _canonicalize(expected_value):
                return False
        elif _canonicalize(actual_value) != _canonicalize(expected_value):
            return False
    return True


def assert_manifest_consistency(
    *,
    section: str,
    investigation_id: UUID,
    items: Any,
    previous_page_sha256: str,
    context_sha256: str | None,
    cursor: str | None,
    pivot: Mapping[str, Any] | None,
    descending: bool,
    limit: int,
    total: int,
    has_more: bool,
    source_bounded: bool,
    omission_reasons: Mapping[ContextOmissionReason | str, int],
    unbounded_reason: str | None,
    selected: int,
    omitted: int,
    claimed_page_sha256: str,
    claimed_source_ref: str,
    manifest: Any,
    source_ref_offset: int | None = None,
    cursor_authentication: str | None = "unauthenticated_position",
) -> None:
    """Recompute and assert that a constructed page and omission row agree."""
    projected_items = tuple(items)
    if selected != len(projected_items) or selected < 0 or omitted < 0 or total < 0:
        raise ValueError("Context manifest selected and omitted counts are inconsistent")
    if selected + omitted != total:
        raise ValueError("Context manifest selected and omitted counts must equal total")
    if not isinstance(omission_reasons, Mapping) or any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in omission_reasons.values()
    ):
        raise ValueError("Context manifest omission reasons are invalid")
    if sum(omission_reasons.values()) != omitted:
        raise ValueError("Context manifest omission reasons must account for omitted items")
    if source_bounded and unbounded_reason is not None:
        raise ValueError("Bounded context sources cannot declare an unbounded reason")
    if not source_bounded and not unbounded_reason:
        raise ValueError("Unbounded context sources require an explicit reason")

    omission_summary = {
        "section": section,
        "total": total,
        "selected": selected,
        "omitted": omitted,
        "source_bounded": source_bounded,
        "omission_reasons": omission_reasons,
        "unbounded_reason": unbounded_reason,
    }
    expected_page_sha256 = canonical_page_sha256(
        section=section,
        investigation_id=investigation_id,
        items=projected_items,
        previous_page_sha256=previous_page_sha256,
        context_sha256=context_sha256,
        cursor=cursor,
        pivot=pivot,
        descending=descending,
        limit=limit,
        total=total,
        has_more=has_more,
        omission_manifest=omission_summary,
    )
    if claimed_page_sha256 != expected_page_sha256:
        raise ValueError("Context manifest page SHA-256 does not match its construction inputs")

    if source_ref_offset is None:
        expected_source_ref = source_reference(
            section=section,
            investigation_id=investigation_id,
            cursor=cursor,
            page_sha256=expected_page_sha256,
            limit=limit,
        )
    else:
        if (
            isinstance(source_ref_offset, bool)
            or source_ref_offset < 0
            or context_sha256 is None
        ):
            raise ValueError("Operator context offset reference inputs are invalid")
        expected_source_ref = (
            f"operator-context:{context_sha256}:{section}:offset={source_ref_offset}:"
            f"sha256={expected_page_sha256}"
        )
    if claimed_source_ref != expected_source_ref:
        raise ValueError("Context manifest source reference does not match its construction inputs")

    expected = {
        "section": section,
        "total": total,
        "selected": selected,
        "omitted": omitted,
        "source_bounded": source_bounded,
        "source_ref": expected_source_ref,
        "page_sha256": expected_page_sha256,
        "omission_reasons": omission_reasons,
        "unbounded_reason": unbounded_reason,
        "context_sha256": context_sha256,
        "cursor": cursor,
        "pivot": dict(pivot) if pivot is not None else None,
        "descending": descending,
        "limit": limit,
        "has_more": has_more,
        "cursor_authentication": cursor_authentication,
    }
    entries = _manifest_entries(manifest)
    if not entries:
        raise ValueError("Context omission manifest is missing")
    for entry in entries:
        for field, expected_value in expected.items():
            actual_value = _field(entry, field)
            if actual_value is _MISSING or _canonicalize(actual_value) != _canonicalize(
                expected_value
            ):
                raise ValueError(f"Context omission manifest is inconsistent: {field}")


_MISSING = object()


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _manifest_entries(value: Any) -> tuple[Any, ...]:
    if isinstance(value, ContextOmissionManifestEntryV1 | Mapping):
        return (value,)
    if isinstance(value, tuple | list):
        return tuple(value)
    return ()


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
    context_sha256: str | None = None,
    source_bounded: bool = True,
    unbounded_reason: str | None = None,
) -> KeysetPageV1[T]:
    has_more = len(items) > limit
    selected = items[:limit]
    previous_page_sha256 = (
        decode_keyset_cursor(
            cursor,
            section=section,
            investigation_id=investigation_id,
            descending=descending,
            context_sha256=context_sha256,
        )["previous_page_sha256"]
        if cursor is not None
        else "0" * 64
    )
    pivot = None
    if selected:
        last_sort_key, last_item_id = position(selected[-1])
        pivot = {
            "kind": "keyset",
            "section": section,
            "investigation_id": str(investigation_id),
            "sort_key": _sort_key_text(last_sort_key),
            "item_id": str(last_item_id),
            "descending": "true" if descending else "false",
        }
        if context_sha256 is not None:
            pivot["context_sha256"] = context_sha256
    omitted = max(0, total - len(selected))
    omission_reasons = {ContextOmissionReason.PAGINATION: omitted} if omitted else {}
    omission_summary = {
        "section": section,
        "total": total,
        "selected": len(selected),
        "omitted": omitted,
        "source_bounded": source_bounded,
        "omission_reasons": omission_reasons,
        "unbounded_reason": unbounded_reason,
    }
    page_sha256 = canonical_page_sha256(
        section=section,
        investigation_id=investigation_id,
        items=selected,
        previous_page_sha256=previous_page_sha256,
        context_sha256=context_sha256,
        cursor=cursor,
        pivot=pivot,
        descending=descending,
        limit=limit,
        total=total,
        has_more=has_more,
        omission_manifest=omission_summary,
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
            context_sha256=context_sha256,
        )
    source_ref = source_reference(
        section=section,
        investigation_id=investigation_id,
        cursor=cursor,
        page_sha256=page_sha256,
        limit=limit,
    )
    page = KeysetPageV1(
        items=selected,
        total=total,
        has_more=has_more,
        next_cursor=next_cursor,
        page_sha256=page_sha256,
        source_ref=source_ref,
        cursor=cursor,
        pivot=pivot,
        descending=descending,
        limit=limit,
        context_sha256=context_sha256,
        omission_manifest=(
            ContextOmissionManifestEntryV1(
                section=section,
                total=total,
                selected=len(selected),
                omitted=omitted,
                source_bounded=source_bounded,
                source_ref=source_ref,
                page_sha256=page_sha256,
                omission_reasons=omission_reasons,
                unbounded_reason=unbounded_reason,
                context_sha256=context_sha256,
                cursor=cursor,
                pivot=pivot,
                descending=descending,
                limit=limit,
                has_more=has_more,
                cursor_authentication="unauthenticated_position",
            ),
        ),
        source_bounded=source_bounded,
        unbounded_reason=unbounded_reason,
    )
    assert_manifest_consistency(
        section=section,
        investigation_id=investigation_id,
        items=page.items,
        previous_page_sha256=previous_page_sha256,
        context_sha256=page.context_sha256,
        cursor=page.cursor,
        pivot=page.pivot,
        descending=page.descending,
        limit=page.limit,
        total=page.total,
        has_more=page.has_more,
        source_bounded=page.source_bounded,
        omission_reasons=omission_reasons,
        unbounded_reason=page.unbounded_reason,
        selected=len(page.items),
        omitted=page.total - len(page.items),
        claimed_page_sha256=page.page_sha256,
        claimed_source_ref=page.source_ref,
        manifest=page.omission_manifest,
    )
    return page


def keyset_pivot(payload: Mapping[str, Any]) -> dict[str, str]:
    """Return the complete position/scope fields, excluding the chained page SHA."""
    result = {
        field: str(payload[field])
        for field in (
            "kind",
            "section",
            "investigation_id",
            "sort_key",
            "item_id",
        )
    }
    result["descending"] = "true" if payload["descending"] else "false"
    if "context_sha256" in payload:
        result["context_sha256"] = str(payload["context_sha256"])
    return result


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
        or isinstance(payload.get("v"), bool)
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


def _keyset_cursor_fields(*, has_context: bool) -> set[str]:
    fields = {
        "v",
        "kind",
        "section",
        "investigation_id",
        "sort_key",
        "item_id",
        "previous_page_sha256",
        "descending",
    }
    if has_context:
        fields.add("context_sha256")
    return fields


def _canonical_omissions(value: Any) -> Any:
    """Hash omission counts and reasons, excluding the self-referential page proof."""
    if isinstance(value, ContextOmissionManifestEntryV1):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        values = (value,)
    elif isinstance(value, tuple | list):
        values = value
    else:
        return _canonicalize(value)
    result = []
    for item in values:
        if isinstance(item, ContextOmissionManifestEntryV1):
            item = item.model_dump(mode="python")
        if not isinstance(item, Mapping):
            result.append(_canonicalize(item))
            continue
        result.append(
            _canonicalize(
                {
                    key: item[key]
                    for key in (
                        "section",
                        "total",
                        "selected",
                        "omitted",
                        "source_bounded",
                        "omission_reasons",
                        "unbounded_reason",
                    )
                    if key in item
                }
            )
        )
    return result


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
