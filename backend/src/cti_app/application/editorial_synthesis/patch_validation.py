"""Wire parsing and structural validation for targeted synthesis patches."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cti_app.application.editorial_synthesis.targets import (
    _canonical_bytes,
    _paragraph_id,
    _ResolvedTarget,
    _section_id,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRefusal,
    TargetedEditorialRevisionRefusalCode,
    TargetedEditorialRevisionRefusalReason,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OUTER_WIRE_BLOCK = re.compile(
    r"^\s*@@\s*(TARGET|ACTION|BASE_ARTIFACT_HASH|BEFORE_HASH|AFTER|RATIONALE|REFUSAL|END)\s*@@\s*$",
    re.IGNORECASE,
)


class TargetedSynthesisRevisionValidationError(RuntimeError):
    code = "targeted_synthesis_revision_rejected"

    def __init__(self, reason_code: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.reason_code = reason_code
        self.details = dict(details or {})
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class _WirePatch:
    target: str
    action: TargetedEditorialRevisionOperation
    base_artifact_hash: str
    before_hash: str
    after: str
    rationale: str
    refusal: TargetedEditorialRevisionRefusal | None = None


def _parse_wire_patch(raw_text: str) -> _WirePatch:
    if not isinstance(raw_text, str):
        raise TargetedSynthesisRevisionValidationError("revision_wire_invalid")
    blocks: dict[str, list[str]] = {}
    active: str | None = None
    for line in raw_text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        match = _OUTER_WIRE_BLOCK.fullmatch(line)
        if match is not None:
            name = match.group(1).upper()
            if name == "END":
                active = None
                continue
            if name in blocks:
                raise TargetedSynthesisRevisionValidationError("revision_wire_duplicate_block")
            blocks[name] = []
            active = name
            continue
        if active is not None:
            blocks[active].append(line)
        elif line.strip() and not re.fullmatch(r"\s*```(?:[A-Za-z0-9_-]+)?\s*", line):
            # Bridge preambles are tolerated; the dedicated markers remain authoritative.
            continue
    if "REFUSAL" in blocks:
        raw = "\n".join(blocks["REFUSAL"]).strip()
        values: dict[str, str] = {}
        for line in raw.splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip().casefold()] = value.strip()
        code = values.get("code", raw.splitlines()[0].strip() if raw else "")
        if code != TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE.value:
            raise TargetedSynthesisRevisionValidationError("revision_refusal_code_invalid")
        reason = values.get("reason", "").strip()
        if not reason:
            raise TargetedSynthesisRevisionValidationError("revision_refusal_reason_missing")
        return _WirePatch(
            target="",
            action=TargetedEditorialRevisionOperation.REWRITE_TITLE,
            base_artifact_hash="",
            before_hash="",
            after="",
            rationale="",
            refusal=TargetedEditorialRevisionRefusal(
                code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                recommended_restart_stage=None,
                reason=reason,
                refusal_reason=TargetedEditorialRevisionRefusalReason.MODEL_DECLINED,
                next_safe_action=(
                    "Reformulate the instruction or add supporting evidence via source extension."
                ),
            ),
        )
    required = {"TARGET", "ACTION", "BASE_ARTIFACT_HASH", "BEFORE_HASH", "AFTER", "RATIONALE"}
    if not required <= set(blocks):
        raise TargetedSynthesisRevisionValidationError("revision_wire_missing_block")
    values = {name: "\n".join(lines).strip() for name, lines in blocks.items()}
    try:
        action = TargetedEditorialRevisionOperation(values["ACTION"])
    except ValueError as exc:
        raise TargetedSynthesisRevisionValidationError("revision_wire_action_invalid") from exc
    for name in ("BASE_ARTIFACT_HASH", "BEFORE_HASH"):
        if _SHA256.fullmatch(values[name]) is None:
            raise TargetedSynthesisRevisionValidationError("revision_wire_hash_invalid")
    if not values["RATIONALE"]:
        raise TargetedSynthesisRevisionValidationError("revision_wire_rationale_missing")
    return _WirePatch(
        target=values["TARGET"],
        action=action,
        base_artifact_hash=values["BASE_ARTIFACT_HASH"],
        before_hash=values["BEFORE_HASH"],
        after=values["AFTER"],
        rationale=values["RATIONALE"],
    )


def _assert_non_target_unchanged(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
    resolved: _ResolvedTarget,
    *,
    allow_extraction_hash_change: bool = False,
) -> None:
    always_preserved: tuple[str, ...] = (
        "schema_version",
        "subject_id",
        "production_input_hash",
        "publication_language",
        "synthesis_policy_version",
        "timeline",
        "source_notes",
        "uncertainties",
        "warnings",
    )
    if not allow_extraction_hash_change:
        always_preserved = (*always_preserved[:3], "extraction_hash", *always_preserved[3:])
    if any(
        _canonical_bytes(before[key]) != _canonical_bytes(after[key]) for key in always_preserved
    ):
        raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")
    if operation is not TargetedEditorialRevisionOperation.REWRITE_TITLE:
        if _canonical_bytes(before["title"]) != _canonical_bytes(after["title"]):
            raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")

    lead_change = operation in {
        TargetedEditorialRevisionOperation.REWRITE_LEAD,
        TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
        TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
        TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
    } and target.kind in {
        TargetedEditorialRevisionTargetKind.LEAD,
        TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH,
    }
    if not lead_change:
        if _canonical_bytes(before["lead"]) != _canonical_bytes(after["lead"]):
            raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")
    elif operation is not TargetedEditorialRevisionOperation.REWRITE_LEAD:
        old_lead = before["lead"]
        new_lead = after["lead"]
        targeted_ids = set(target.stable_ids)
        old_by_id = {_paragraph_id(item): item for item in old_lead}
        new_by_id = {_paragraph_id(item): item for item in new_lead}
        if any(
            _canonical_bytes(value) != _canonical_bytes(new_by_id[identity])
            for identity, value in old_by_id.items()
            if identity not in targeted_ids and identity in new_by_id
        ) or any(
            identity not in new_by_id for identity in old_by_id if identity not in targeted_ids
        ):
            raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")

    old_sections = before["sections"]
    new_sections = after["sections"]
    old_by_id = {_section_id(item): item for item in old_sections}
    new_by_id = {_section_id(item): item for item in new_sections}
    section_ops = {
        TargetedEditorialRevisionOperation.REWRITE_SECTION,
        TargetedEditorialRevisionOperation.DELETE_SECTION,
        TargetedEditorialRevisionOperation.INSERT_SECTION,
        TargetedEditorialRevisionOperation.MERGE_SECTIONS,
        TargetedEditorialRevisionOperation.SPLIT_SECTION,
    }
    if operation in section_ops:
        changed_section_ids = set(target.stable_ids)
        if any(
            _canonical_bytes(value) != _canonical_bytes(new_by_id[identity])
            for identity, value in old_by_id.items()
            if identity not in changed_section_ids and identity in new_by_id
        ) or any(
            identity not in new_by_id
            for identity in old_by_id
            if identity not in changed_section_ids
        ):
            raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")
    elif target.kind in {
        TargetedEditorialRevisionTargetKind.SECTION,
        TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH,
    }:
        target_section_id = target.parent_id or target.stable_ids[0]
        if any(
            _canonical_bytes(value) != _canonical_bytes(new_by_id[identity])
            for identity, value in old_by_id.items()
            if identity != target_section_id and identity in new_by_id
        ) or any(
            identity not in new_by_id for identity in old_by_id if identity != target_section_id
        ):
            raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")
        section_index = (
            resolved.parent_index if resolved.parent_index is not None else resolved.indexes[0]
        )
        old_section = old_sections[section_index]
        # Single-section paragraph and heading patches preserve the section's
        # slot while its content hash changes.
        new_section_index = section_index
        if new_section_index is not None and new_section_index < len(new_sections):
            old_paragraphs = old_section["paragraphs"]
            new_paragraphs = new_sections[new_section_index]["paragraphs"]
            paragraph_ops = {
                TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
                TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
            }
            if operation in paragraph_ops:
                ignored = set(target.stable_ids)
            else:
                ignored = set()
            old_paragraphs_by_id = {_paragraph_id(value): value for value in old_paragraphs}
            new_paragraphs_by_id = {_paragraph_id(value): value for value in new_paragraphs}
            if any(
                _canonical_bytes(value) != _canonical_bytes(new_paragraphs_by_id[identity])
                for identity, value in old_paragraphs_by_id.items()
                if identity not in ignored and identity in new_paragraphs_by_id
            ) or any(
                identity not in new_paragraphs_by_id
                for identity in old_paragraphs_by_id
                if identity not in ignored
            ):
                raise TargetedSynthesisRevisionValidationError(
                    "revision_patch_changed_outside_target"
                )
    elif _canonical_bytes(old_sections) != _canonical_bytes(new_sections):
        raise TargetedSynthesisRevisionValidationError("revision_patch_changed_outside_target")


WirePatch = _WirePatch
parse_wire_patch = _parse_wire_patch
assert_non_target_unchanged = _assert_non_target_unchanged
