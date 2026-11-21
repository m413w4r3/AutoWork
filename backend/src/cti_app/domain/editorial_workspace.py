"""Pure identity and snapshot rules for the editorial workspace."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5


class EditorialBlockKind(StrEnum):
    TITLE = "title"
    LEAD_PARAGRAPH = "lead_paragraph"
    SECTION_HEADING = "section_heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    DIAGRAM = "diagram"
    CHART = "chart"
    FIGURE = "figure"


class EditorialTransition(StrEnum):
    BOOTSTRAP = "bootstrap"
    REWRITE = "rewrite"
    MOVE = "move"
    INSERT = "insert"
    DELETE = "delete"


class EditorialRevisionStatus(StrEnum):
    READY = "ready"
    NEEDS_REVIEW = "needs_review"


class EditorialSourceStage(StrEnum):
    SYNTHESIS = "synthesis"
    EDITORIAL_ENRICHMENT = "editorial_enrichment"


class EditorialWorkspaceError(RuntimeError):
    code = "editorial_workspace_error"


class EditorialIdentityAmbiguousError(EditorialWorkspaceError):
    code = "editorial_identity_ambiguous"


class EditorialBlockMissingError(EditorialWorkspaceError):
    code = "editorial_block_missing"


class EditorialRegistryConflictError(EditorialWorkspaceError):
    code = "editorial_registry_conflict"


class EditorialReadOnlyError(EditorialWorkspaceError):
    code = "editorial_read_only"

    def __init__(self, reason: str) -> None:
        super().__init__(self.code)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class BlockRegistryEntry:
    block_id: UUID
    kind: EditorialBlockKind
    parent_block_id: UUID | None
    position: int
    content_sha256: str
    source_stage: EditorialSourceStage
    structural_pointer: str
    transition: EditorialTransition = EditorialTransition.BOOTSTRAP
    revision_status: EditorialRevisionStatus = EditorialRevisionStatus.READY

    def __post_init__(self) -> None:
        if not isinstance(self.kind, EditorialBlockKind):
            raise ValueError("editorial_registry_kind_invalid")
        if not isinstance(self.transition, EditorialTransition):
            raise ValueError("editorial_registry_transition_invalid")
        if not isinstance(self.revision_status, EditorialRevisionStatus):
            raise ValueError("editorial_registry_status_invalid")
        if not isinstance(self.source_stage, EditorialSourceStage):
            raise ValueError("editorial_registry_stage_invalid")
        if self.position < 0:
            raise ValueError("editorial_registry_position_invalid")
        _validate_sha256(self.content_sha256, "content_sha256")
        if not self.structural_pointer.startswith("/"):
            raise ValueError("editorial_registry_pointer_invalid")

    def to_json(self) -> dict[str, object]:
        return {
            "block_id": str(self.block_id),
            "kind": self.kind.value,
            "parent_block_id": str(self.parent_block_id) if self.parent_block_id else None,
            "position": self.position,
            "content_sha256": self.content_sha256,
            "source_stage": self.source_stage.value,
            "structural_pointer": self.structural_pointer,
            "transition": self.transition.value,
            "revision_status": self.revision_status.value,
        }


@dataclass(frozen=True, slots=True)
class AdvancedBlockRegistry:
    active: tuple[BlockRegistryEntry, ...]
    retired_block_ids: frozenset[UUID]


def _validate_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{field} must be lowercase SHA-256")


def validate_block_registry(
    entries: Sequence[BlockRegistryEntry], retired_block_ids: Sequence[UUID] = ()
) -> None:
    ids = [entry.block_id for entry in entries]
    retired = set(retired_block_ids)
    if len(ids) != len(set(ids)) or set(ids) & retired:
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    active_ids = set(ids)
    if any(
        entry.parent_block_id is not None and entry.parent_block_id not in active_ids
        for entry in entries
    ):
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    pointers = [(entry.source_stage, entry.structural_pointer) for entry in entries]
    if len(pointers) != len(set(pointers)):
        raise EditorialRegistryConflictError("editorial_registry_conflict")


def canonical_content_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def workspace_pin_hash(pins: Mapping[str, object]) -> str:
    normalized = {
        key: str(value) if isinstance(value, UUID) else value for key, value in pins.items()
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def bootstrap_block_registry(
    *,
    artifact_id: UUID,
    canonical_sha256: str,
    source_stage: str | EditorialSourceStage,
    canonical_content: Mapping[str, Any],
) -> tuple[BlockRegistryEntry, ...]:
    """Assign stable IDs to every physical occurrence without persisting prose."""
    _validate_sha256(canonical_sha256, "canonical_sha256")
    try:
        typed_source_stage = EditorialSourceStage(source_stage)
    except ValueError as exc:
        raise ValueError("editorial_registry_stage_invalid") from exc
    entries: list[BlockRegistryEntry] = []

    def append(
        kind: EditorialBlockKind,
        value: Any,
        pointer: str,
        position: int,
        parent_block_id: UUID | None = None,
    ) -> UUID:
        block_id = uuid5(
            NAMESPACE_URL,
            f"autowork:editorial-workspace:{artifact_id}:{canonical_sha256}:{kind}:{pointer}",
        )
        entries.append(
            BlockRegistryEntry(
                block_id=block_id,
                kind=kind,
                parent_block_id=parent_block_id,
                position=position,
                content_sha256=canonical_content_sha256(value),
                source_stage=typed_source_stage,
                structural_pointer=pointer,
            )
        )
        return block_id

    if typed_source_stage is EditorialSourceStage.SYNTHESIS:
        if "title" in canonical_content:
            append(EditorialBlockKind.TITLE, canonical_content["title"], "/title", 0)
        lead = canonical_content.get("lead")
        if isinstance(lead, list):
            for index, paragraph in enumerate(lead):
                if isinstance(paragraph, Mapping):
                    append(EditorialBlockKind.LEAD_PARAGRAPH, paragraph, f"/lead/{index}", index)
        sections = canonical_content.get("sections")
        if isinstance(sections, list):
            for section_index, section in enumerate(sections):
                if not isinstance(section, Mapping):
                    continue
                section_pointer = f"/sections/{section_index}"
                heading_value = section.get("heading", "")
                heading_id = append(
                    EditorialBlockKind.SECTION_HEADING,
                    heading_value,
                    f"{section_pointer}/heading",
                    section_index,
                )
                paragraphs = section.get("paragraphs")
                if isinstance(paragraphs, list):
                    for paragraph_index, paragraph in enumerate(paragraphs):
                        if isinstance(paragraph, Mapping):
                            append(
                                EditorialBlockKind.PARAGRAPH,
                                paragraph,
                                f"{section_pointer}/paragraphs/{paragraph_index}",
                                paragraph_index,
                                heading_id,
                            )
        validate_block_registry(entries)
        return tuple(entries)

    if typed_source_stage is not EditorialSourceStage.EDITORIAL_ENRICHMENT:
        raise ValueError("editorial_registry_stage_invalid")

    family_kinds: tuple[tuple[str, EditorialBlockKind], ...] = (
        ("tables", EditorialBlockKind.TABLE),
        ("diagrams", EditorialBlockKind.DIAGRAM),
        ("charts", EditorialBlockKind.CHART),
        ("source_figures", EditorialBlockKind.FIGURE),
    )
    for family, kind in family_kinds:
        raw_items = canonical_content.get(family, [])
        if not isinstance(raw_items, list):
            continue
        for index, item in enumerate(raw_items):
            if isinstance(item, Mapping):
                append(kind, item, f"/{family}/{index}", index)
    validate_block_registry(entries)
    return tuple(entries)


def advance_block_registry(
    *,
    base_registry: Sequence[BlockRegistryEntry],
    artifact_id: UUID,
    canonical_sha256: str,
    source_stage: str | EditorialSourceStage,
    canonical_content: Mapping[str, Any],
    lineage: Sequence[Mapping[str, object]],
    retired_block_ids: Sequence[UUID] = (),
) -> AdvancedBlockRegistry:
    """Build a next registry from explicit lineage; never match by text or order."""
    if len(retired_block_ids) != len(set(retired_block_ids)):
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    candidates = bootstrap_block_registry(
        artifact_id=artifact_id,
        canonical_sha256=canonical_sha256,
        source_stage=source_stage,
        canonical_content=canonical_content,
    )
    candidate_by_pointer = {item.structural_pointer: item for item in candidates}
    if len(candidate_by_pointer) != len(candidates):
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    base_by_id = {item.block_id: item for item in base_registry}
    if len(base_by_id) != len(base_registry):
        raise EditorialRegistryConflictError("editorial_registry_conflict")

    lineage_by_id: dict[UUID, tuple[str, str | None]] = {}
    insertion_by_pointer: dict[str, UUID | None] = {}
    for item in lineage:
        transition = str(item.get("transition", ""))
        pointer_value = item.get("pointer")
        pointer = str(pointer_value) if pointer_value is not None else None
        if transition == "insert":
            if (
                pointer is None
                or pointer not in candidate_by_pointer
                or pointer in insertion_by_pointer
            ):
                raise EditorialRegistryConflictError("editorial_registry_conflict")
            try:
                requested_id = UUID(str(item["block_id"])) if item.get("block_id") else None
            except (TypeError, ValueError) as exc:
                raise EditorialRegistryConflictError("editorial_registry_conflict") from exc
            if requested_id is not None and requested_id in base_by_id:
                raise EditorialRegistryConflictError("editorial_registry_conflict")
            insertion_by_pointer[pointer] = requested_id
            continue
        try:
            block_id = UUID(str(item["block_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise EditorialRegistryConflictError("editorial_registry_conflict") from exc
        if block_id not in base_by_id or block_id in lineage_by_id:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        if transition not in {"rewrite", "move", "delete"}:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        if (transition == "delete") != (pointer is None):
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        if pointer is not None and pointer not in candidate_by_pointer:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        lineage_by_id[block_id] = (transition, pointer)

    if set(lineage_by_id) != set(base_by_id):
        raise EditorialIdentityAmbiguousError("editorial_identity_ambiguous")

    candidate_to_preserved: dict[UUID, UUID] = {}
    active: list[BlockRegistryEntry] = []
    retired = set(retired_block_ids)
    claimed_pointers: set[str] = set()
    for block_id, (transition, pointer) in lineage_by_id.items():
        if transition == "delete":
            retired.add(block_id)
            continue
        assert pointer is not None
        if pointer in claimed_pointers:
            raise EditorialIdentityAmbiguousError("editorial_identity_ambiguous")
        claimed_pointers.add(pointer)
        candidate = candidate_by_pointer[pointer]
        prior = base_by_id[block_id]
        if prior.kind != candidate.kind:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        candidate_to_preserved[candidate.block_id] = block_id
        active.append(
            replace(
                candidate,
                block_id=block_id,
                transition=EditorialTransition(transition),
            )
        )

    for pointer, requested_id in insertion_by_pointer.items():
        if pointer in claimed_pointers:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        claimed_pointers.add(pointer)
        candidate = candidate_by_pointer[pointer]
        block_id = requested_id or candidate.block_id
        if block_id in retired or block_id in {item.block_id for item in active}:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        candidate_to_preserved[candidate.block_id] = block_id
        active.append(replace(candidate, block_id=block_id, transition=EditorialTransition.INSERT))

    if claimed_pointers != set(candidate_by_pointer):
        raise EditorialRegistryConflictError("editorial_registry_conflict")

    remapped: list[BlockRegistryEntry] = []
    for entry in active:
        remapped.append(
            replace(
                entry,
                parent_block_id=(
                    candidate_to_preserved.get(entry.parent_block_id, entry.parent_block_id)
                    if entry.parent_block_id is not None
                    else None
                ),
            )
        )
    ids = [item.block_id for item in remapped]
    if len(ids) != len(set(ids)) or set(ids) & retired:
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    validate_block_registry(remapped, tuple(retired))
    return AdvancedBlockRegistry(tuple(remapped), frozenset(retired))


def reconcile_exact_block_registry(
    previous_registry: Sequence[BlockRegistryEntry],
    current_registry: Sequence[BlockRegistryEntry],
) -> tuple[BlockRegistryEntry, ...]:
    """Keep only unique, exact, parent-safe, order-preserving legacy identities."""
    if (
        {entry.source_stage for entry in previous_registry}
        != {entry.source_stage for entry in current_registry}
        and previous_registry
        and current_registry
    ):
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    if any(
        entry.revision_status is not EditorialRevisionStatus.READY for entry in previous_registry
    ):
        previous_registry = tuple(
            entry
            for entry in previous_registry
            if entry.revision_status is EditorialRevisionStatus.READY
        )
    if not current_registry:
        return ()

    previous_order = {entry.block_id: index for index, entry in enumerate(previous_registry)}
    current_order = {entry.block_id: index for index, entry in enumerate(current_registry)}
    previous_by_parent: dict[UUID | None, list[BlockRegistryEntry]] = {}
    current_by_parent: dict[UUID | None, list[BlockRegistryEntry]] = {}
    for entry in previous_registry:
        previous_by_parent.setdefault(entry.parent_block_id, []).append(entry)
    for entry in current_registry:
        current_by_parent.setdefault(entry.parent_block_id, []).append(entry)

    old_parent_by_current: dict[UUID | None, UUID | None] = {None: None}
    matched: dict[UUID, BlockRegistryEntry] = {}
    pending_parents: list[UUID | None] = [None]
    examined_parents: set[UUID | None] = set()

    while pending_parents:
        current_parent = pending_parents.pop(0)
        if current_parent in examined_parents:
            continue
        examined_parents.add(current_parent)
        old_parent = old_parent_by_current[current_parent]
        old_children = previous_by_parent.get(old_parent, [])
        new_children = current_by_parent.get(current_parent, [])
        old_by_exact_key: dict[tuple[EditorialBlockKind, str], list[BlockRegistryEntry]] = {}
        new_by_exact_key: dict[tuple[EditorialBlockKind, str], list[BlockRegistryEntry]] = {}
        for entry in old_children:
            old_by_exact_key.setdefault((entry.kind, entry.content_sha256), []).append(entry)
        for entry in new_children:
            new_by_exact_key.setdefault((entry.kind, entry.content_sha256), []).append(entry)

        candidates: list[tuple[BlockRegistryEntry, BlockRegistryEntry]] = []
        for key, old_matches in old_by_exact_key.items():
            new_matches = new_by_exact_key.get(key, [])
            if len(old_matches) == 1 and len(new_matches) == 1:
                candidates.append((old_matches[0], new_matches[0]))

        ambiguous_ids: set[UUID] = set()
        for index, (old_entry, new_entry) in enumerate(candidates):
            for other_old, other_new in candidates[index + 1 :]:
                old_before = previous_order[old_entry.block_id] < previous_order[other_old.block_id]
                new_before = current_order[new_entry.block_id] < current_order[other_new.block_id]
                if old_before != new_before:
                    ambiguous_ids.update((old_entry.block_id, other_old.block_id))

        for old_entry, new_entry in candidates:
            if old_entry.block_id in ambiguous_ids:
                continue
            matched[new_entry.block_id] = old_entry
            old_parent_by_current[new_entry.block_id] = old_entry.block_id
            pending_parents.append(new_entry.block_id)

    reconciled: list[BlockRegistryEntry] = []
    for entry in current_registry:
        prior = matched.get(entry.block_id)
        parent_id = (
            old_parent_by_current.get(entry.parent_block_id, entry.parent_block_id)
            if entry.parent_block_id is not None
            else None
        )
        if prior is None:
            reconciled.append(
                replace(
                    entry,
                    parent_block_id=parent_id,
                    revision_status=EditorialRevisionStatus.NEEDS_REVIEW,
                )
            )
            continue
        reconciled.append(
            replace(
                entry,
                block_id=prior.block_id,
                parent_block_id=parent_id,
                transition=(
                    EditorialTransition.MOVE
                    if prior.structural_pointer != entry.structural_pointer
                    else EditorialTransition.BOOTSTRAP
                ),
                revision_status=EditorialRevisionStatus.READY,
            )
        )

    validate_block_registry(reconciled)
    return tuple(reconciled)
