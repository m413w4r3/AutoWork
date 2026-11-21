"""Registry serialization, pinned projection, and block target resolution."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

from cti_app.application.editorial_synthesis import targets as synthesis_targets
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialBlockMissingError,
    EditorialIdentityAmbiguousError,
    EditorialRegistryConflictError,
    EditorialRevisionStatus,
    EditorialSourceStage,
    EditorialTransition,
    EditorialWorkspaceError,
    canonical_content_sha256,
    validate_block_registry,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EnrichmentTableKind,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.publication_kind import PublicationKind

# Compatibility seam for callers of the former canonical resolver.
build_targeted_revision_target_catalog = synthesis_targets.build_targeted_revision_target_catalog


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshotRecord:
    snapshot_id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_version: int
    synthesis_input_hash: str
    synthesis_canonical_sha256: str
    enrichment_artifact_id: UUID | None
    enrichment_version: int | None
    enrichment_input_hash: str | None
    enrichment_canonical_sha256: str | None
    publication_artifact_id: UUID | None
    publication_version: int | None
    publication_input_hash: str | None
    publication_canonical_sha256: str | None
    pipeline_generation: int
    routing_decision_id: UUID | None
    routing_decision_hash: str | None
    publication_kind: PublicationKind | None
    workspace_pin_hash: str
    registry_hash: str
    block_registry: tuple[BlockRegistryEntry, ...]
    retired_block_ids: tuple[UUID, ...] = ()
    parent_snapshot_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceOccurrenceResolution:
    record: WorkspaceSnapshotRecord
    entry: BlockRegistryEntry | None
    binding_state: Literal["valid", "stale", "ambiguous"]
    artifact_id: UUID | None
    artifact_version: int | None
    canonical_sha256: str | None


@dataclass(frozen=True, slots=True)
class ResolvedWorkspaceTarget:
    block_id: UUID
    path: str
    before_hash: str
    operation: str
    target: TargetedEditorialRevisionTarget
    legacy_target_id: str | None


@dataclass(frozen=True, slots=True)
class ResolvedParagraphAnchor:
    block_id: UUID
    structural_pointer: str
    canonical_position: int
    paragraph_kind: Literal["lead", "section"]
    section_index: int | None
    paragraph_index: int


def resolve_paragraph_anchor(
    record: WorkspaceSnapshotRecord,
    block_id: UUID,
    *,
    synthesis: Mapping[str, Any] | None = None,
) -> ResolvedParagraphAnchor:
    """Resolve a registered paragraph occurrence against one exact workspace pin."""
    occurrence = resolve_occurrence(record, block_id)
    entry = occurrence.entry
    if (
        entry is None
        or occurrence.binding_state != "valid"
        or entry.source_stage is not EditorialSourceStage.SYNTHESIS
    ):
        raise EditorialWorkspaceError("editorial_anchor_stale")

    tokens = _pointer_tokens(entry.structural_pointer)
    try:
        if entry.kind.value == "lead_paragraph" and len(tokens) == 2 and tokens[0] == "lead":
            paragraph_index = int(tokens[1])
            paragraph_kind: Literal["lead", "section"] = "lead"
            section_index = None
        elif (
            entry.kind.value == "paragraph"
            and len(tokens) == 4
            and tokens[0] == "sections"
            and tokens[2] == "paragraphs"
        ):
            section_index = int(tokens[1])
            paragraph_index = int(tokens[3])
            paragraph_kind = "section"
        else:
            raise ValueError("invalid paragraph pointer")
        if paragraph_index < 0 or (section_index is not None and section_index < 0):
            raise ValueError("invalid paragraph pointer")
        if synthesis is not None:
            value = read_pointer(synthesis, entry.structural_pointer)
            if canonical_content_sha256(value) != entry.content_sha256:
                raise ValueError("paragraph content does not match the pinned registry")
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise EditorialWorkspaceError("editorial_anchor_stale") from exc

    return ResolvedParagraphAnchor(
        block_id=entry.block_id,
        structural_pointer=entry.structural_pointer,
        canonical_position=entry.position,
        paragraph_kind=paragraph_kind,
        section_index=section_index,
        paragraph_index=paragraph_index,
    )


def resolve_canonical_target(
    payload: Mapping[str, Any],
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
) -> synthesis_targets.ResolvedTarget:
    return synthesis_targets.resolve_target(payload, target, operation)


def resolve_occurrence(
    record: WorkspaceSnapshotRecord, occurrence_id: UUID
) -> WorkspaceOccurrenceResolution:
    """Resolve current ownership and continuity for one registry occurrence."""
    entry = next((item for item in record.block_registry if item.block_id == occurrence_id), None)
    if entry is None:
        return WorkspaceOccurrenceResolution(
            record=record,
            entry=None,
            binding_state="stale",
            artifact_id=None,
            artifact_version=None,
            canonical_sha256=None,
        )

    if entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT:
        artifact_id = record.enrichment_artifact_id
        artifact_version = record.enrichment_version
        canonical_sha256 = record.enrichment_canonical_sha256
    else:
        artifact_id = record.synthesis_artifact_id
        artifact_version = record.synthesis_version
        canonical_sha256 = record.synthesis_canonical_sha256

    if artifact_id is None or artifact_version is None or canonical_sha256 is None:
        binding_state: Literal["valid", "stale", "ambiguous"] = "stale"
    elif entry.revision_status is EditorialRevisionStatus.READY:
        binding_state = "valid"
    elif entry.revision_status is EditorialRevisionStatus.NEEDS_REVIEW:
        binding_state = "ambiguous"
    else:
        binding_state = "stale"
    return WorkspaceOccurrenceResolution(
        record=record,
        entry=entry,
        binding_state=binding_state,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        canonical_sha256=canonical_sha256,
    )


def serialize_registry(entries: Sequence[BlockRegistryEntry]) -> list[dict[str, object]]:
    return [entry.to_json() for entry in entries]


def registry_hash(entries: Sequence[BlockRegistryEntry]) -> str:
    return canonical_content_sha256(serialize_registry(entries))


def registry_from_json(raw: Sequence[Mapping[str, Any]]) -> tuple[BlockRegistryEntry, ...]:
    entries: list[BlockRegistryEntry] = []
    for item in raw:
        try:
            entries.append(
                BlockRegistryEntry(
                    block_id=UUID(str(item["block_id"])),
                    kind=EditorialBlockKind(str(item["kind"])),
                    parent_block_id=(
                        UUID(str(item["parent_block_id"]))
                        if item.get("parent_block_id") is not None
                        else None
                    ),
                    position=int(item["position"]),
                    content_sha256=str(item["content_sha256"]),
                    source_stage=EditorialSourceStage(str(item["source_stage"])),
                    structural_pointer=str(item["structural_pointer"]),
                    transition=EditorialTransition(str(item.get("transition", "bootstrap"))),
                    revision_status=EditorialRevisionStatus(
                        str(item.get("revision_status", "ready"))
                    ),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise EditorialRegistryConflictError("editorial_registry_conflict") from exc
    ids = [entry.block_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    validate_block_registry(entries)
    return tuple(entries)


def _pointer_tokens(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise EditorialRegistryConflictError("editorial_registry_conflict")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def read_pointer(content: Any, pointer: str) -> Any:
    current = content
    for token in _pointer_tokens(pointer):
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, Mapping):
            current = current[token]
        else:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
    return current


def _copy_with_selected_occurrence(
    content: Mapping[str, Any], entry: BlockRegistryEntry
) -> dict[str, Any]:
    copied = cast(dict[str, Any], json.loads(json.dumps(content)))
    tokens = _pointer_tokens(entry.structural_pointer)
    if entry.kind == "title":
        return copied
    if tokens[0] == "lead":
        selected_index = int(tokens[1])
        copied["lead"] = [copied["lead"][selected_index]]
        return copied
    if tokens[0] != "sections":
        return copied
    section_index = int(tokens[1])
    selected_section = copied["sections"][section_index]
    copied["sections"] = [selected_section]
    if entry.kind == "paragraph":
        paragraph_index = int(tokens[3])
        selected_section["paragraphs"] = [selected_section["paragraphs"][paragraph_index]]
    return copied


def _target_for_entry(
    entry: BlockRegistryEntry,
    canonical_content: Mapping[str, Any],
) -> tuple[TargetedEditorialRevisionTarget, tuple[str, ...], str | None]:
    reduced = _copy_with_selected_occurrence(canonical_content, entry)
    catalog = build_targeted_revision_target_catalog(
        ProductionArtifactStage.SYNTHESIS.value,
        reduced,
    )
    matching: list[dict[str, Any]] = []
    expected_kind = {
        "title": TargetedEditorialRevisionTargetKind.TITLE.value,
        "lead_paragraph": TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH.value,
        "section_heading": TargetedEditorialRevisionTargetKind.SECTION.value,
        "paragraph": TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH.value,
    }.get(entry.kind)
    for option in catalog:
        raw_target = option.get("target")
        if not isinstance(raw_target, Mapping) or raw_target.get("kind") != expected_kind:
            continue
        stable_ids = raw_target.get("stable_ids", [])
        if entry.kind in {"title", "section_heading"} or (
            isinstance(stable_ids, list) and entry.content_sha256 in stable_ids
        ):
            matching.append(option)
    if len(matching) != 1:
        raise EditorialIdentityAmbiguousError("editorial_identity_ambiguous")
    option = matching[0]
    target_data = cast(Mapping[str, Any], option["target"])
    resolved_target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind(str(target_data["kind"])),
        stable_ids=tuple(str(item) for item in target_data.get("stable_ids", [])),
        parent_id=(str(target_data["parent_id"]) if target_data.get("parent_id") else None),
        anchor_id=(str(target_data["anchor_id"]) if target_data.get("anchor_id") else None),
        insert_after=bool(target_data.get("insert_after", False)),
    )
    operations = tuple(str(item) for item in option.get("operations", []))
    legacy_matches = [
        candidate
        for candidate in build_targeted_revision_target_catalog(
            ProductionArtifactStage.SYNTHESIS.value,
            canonical_content,
        )
        if _is_legacy_catalog_target_for_entry(
            candidate.get("target"), entry, canonical_content, expected_kind
        )
    ]
    legacy_target_id: str | None = None
    if len(legacy_matches) == 1:
        raw_legacy_target_id = legacy_matches[0].get("id")
        if isinstance(raw_legacy_target_id, str):
            legacy_target_id = raw_legacy_target_id
    return resolved_target, operations, legacy_target_id


def _is_legacy_catalog_target_for_entry(
    raw_target: object,
    entry: BlockRegistryEntry,
    canonical_content: Mapping[str, Any],
    expected_kind: str | None,
) -> bool:
    if expected_kind is None or not isinstance(raw_target, Mapping):
        return False
    if raw_target.get("kind") != expected_kind:
        return False

    stable_ids = raw_target.get("stable_ids", [])
    parent_id = raw_target.get("parent_id")
    tokens = _pointer_tokens(entry.structural_pointer)
    if entry.kind is EditorialBlockKind.TITLE:
        expected_stable_ids: list[str] = []
        expected_parent_id = None
    elif entry.kind is EditorialBlockKind.LEAD_PARAGRAPH:
        expected_stable_ids = [entry.content_sha256]
        expected_parent_id = None
    elif entry.kind is EditorialBlockKind.SECTION_HEADING:
        section = read_pointer(canonical_content, f"/sections/{tokens[1]}")
        expected_stable_ids = [canonical_content_sha256(section)]
        expected_parent_id = None
    elif entry.kind is EditorialBlockKind.PARAGRAPH:
        section = read_pointer(canonical_content, f"/sections/{tokens[1]}")
        expected_stable_ids = [entry.content_sha256]
        expected_parent_id = canonical_content_sha256(section)
    else:
        return False

    return stable_ids == expected_stable_ids and parent_id == expected_parent_id


def allowed_operations(
    entry: BlockRegistryEntry,
    canonical_content: Mapping[str, Any],
    *,
    allow_table_insertion: bool = False,
    allow_bar_chart_insertion: bool = False,
) -> tuple[str, ...]:
    if entry.source_stage != "synthesis":
        return ()
    _, operations, _legacy_target_id = _target_for_entry(entry, canonical_content)
    if allow_table_insertion and entry.kind in {
        EditorialBlockKind.LEAD_PARAGRAPH,
        EditorialBlockKind.PARAGRAPH,
    }:
        operations = (*operations, TargetedEditorialRevisionOperation.CREATE_TABLE.value)
    if allow_bar_chart_insertion and entry.kind in {
        EditorialBlockKind.LEAD_PARAGRAPH,
        EditorialBlockKind.PARAGRAPH,
    }:
        operations = (*operations, TargetedEditorialRevisionOperation.CREATE_BAR_CHART.value)
    return operations


def revision_target_path(entry: BlockRegistryEntry) -> str:
    """Resolve the canonical revision path for one registered synthesis block."""
    return f"synthesis{entry.structural_pointer}"


def table_insertion_preparation_ready(
    *,
    record: WorkspaceSnapshotRecord,
    entry: BlockRegistryEntry,
    synthesis_artifact: ProductionArtifact | None,
    synthesis_read_sha256: str | None,
    enrichment_artifact: ProductionArtifact | None,
    enrichment_read_sha256: str | None,
    anchor_registered: bool,
) -> bool:
    """Return whether a table can be prepared against these verified pins."""
    if (
        not anchor_registered
        or entry not in record.block_registry
        or entry.source_stage is not EditorialSourceStage.SYNTHESIS
        or entry.kind not in {EditorialBlockKind.PARAGRAPH, EditorialBlockKind.LEAD_PARAGRAPH}
        or entry.revision_status is not EditorialRevisionStatus.READY
        or synthesis_artifact is None
        or synthesis_artifact.stage is not ProductionArtifactStage.SYNTHESIS
        or synthesis_artifact.status is not ProductionArtifactStatus.VERIFIED
        or synthesis_artifact.canonical_blob_id is None
        or synthesis_artifact.id != record.synthesis_artifact_id
        or synthesis_artifact.version != record.synthesis_version
        or synthesis_artifact.input_hash != record.synthesis_input_hash
        or synthesis_artifact.production_run_id != record.production_run_id
        or synthesis_artifact.subject_id != record.subject_id
        or synthesis_read_sha256 != record.synthesis_canonical_sha256
        or enrichment_artifact is None
        or enrichment_artifact.stage is not ProductionArtifactStage.EDITORIAL_ENRICHMENT
        or enrichment_artifact.status is not ProductionArtifactStatus.VERIFIED
        or enrichment_artifact.canonical_blob_id is None
        or enrichment_artifact.id != record.enrichment_artifact_id
        or enrichment_artifact.version != record.enrichment_version
        or enrichment_artifact.input_hash != record.enrichment_input_hash
        or enrichment_artifact.production_run_id != record.production_run_id
        or enrichment_artifact.subject_id != record.subject_id
        or enrichment_read_sha256 != record.enrichment_canonical_sha256
    ):
        return False
    return True


def bar_chart_insertion_preparation_ready(**kwargs: Any) -> bool:
    """A bar chart shares the same verified anchor and enrichment pins as table insertion."""
    return table_insertion_preparation_ready(**kwargs)


def resolve_workspace_target(
    *,
    entry: BlockRegistryEntry,
    canonical_content: Mapping[str, Any],
    operation: str,
) -> ResolvedWorkspaceTarget:
    if entry.revision_status is EditorialRevisionStatus.NEEDS_REVIEW:
        raise EditorialIdentityAmbiguousError("editorial_identity_ambiguous")
    if entry.source_stage is not EditorialSourceStage.SYNTHESIS:
        raise EditorialBlockMissingError("editorial_block_missing")
    try:
        typed_operation = TargetedEditorialRevisionOperation(operation)
        target, operations, legacy_target_id = _target_for_entry(entry, canonical_content)
        reduced_content = _copy_with_selected_occurrence(canonical_content, entry)
    except (TypeError, ValueError) as exc:
        raise EditorialBlockMissingError("editorial_block_missing") from exc
    if operation not in operations:
        if typed_operation in {
            TargetedEditorialRevisionOperation.CREATE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
        } and entry.kind in {
            EditorialBlockKind.LEAD_PARAGRAPH,
            EditorialBlockKind.PARAGRAPH,
        }:
            operations = (*operations, typed_operation.value)
        else:
            raise EditorialBlockMissingError("editorial_block_missing")
    if typed_operation in {
        TargetedEditorialRevisionOperation.CREATE_TABLE,
        TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
    }:
        table_target = TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
            element_kind=(
                EditorialEnrichmentElementKind.TABLE
                if typed_operation is TargetedEditorialRevisionOperation.CREATE_TABLE
                else EditorialEnrichmentElementKind.CHART
            ),
            placement=TargetedEditorialRevisionPlacement(
                kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=entry.block_id,
            ),
        )
        return ResolvedWorkspaceTarget(
            entry.block_id,
            revision_target_path(entry),
            entry.content_sha256,
            operation,
            table_target,
            canonical_content_sha256(table_target.to_json()),
        )
    try:
        resolved = resolve_canonical_target(reduced_content, target, typed_operation)
    except ValueError as exc:
        if "ambiguous" in str(exc):
            raise EditorialIdentityAmbiguousError("editorial_identity_ambiguous") from exc
        raise EditorialBlockMissingError("editorial_block_missing") from exc
    path = revision_target_path(entry)
    return ResolvedWorkspaceTarget(
        entry.block_id,
        path,
        resolved.before_hash,
        operation,
        target,
        legacy_target_id,
    )


def project_blocks(
    *,
    entries: Sequence[BlockRegistryEntry],
    synthesis: Mapping[str, Any],
    enrichment: Mapping[str, Any] | None,
    record: WorkspaceSnapshotRecord | None = None,
    synthesis_artifact: ProductionArtifact | None = None,
    synthesis_read_sha256: str | None = None,
    enrichment_artifact: ProductionArtifact | None = None,
    enrichment_read_sha256: str | None = None,
    needs_review_ids: frozenset[UUID] = frozenset(),
    allow_operations: bool = True,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    blocks: list[dict[str, object]] = []
    enrichments: list[dict[str, object]] = []
    outline: list[dict[str, object]] = []
    heading_children: dict[UUID, list[UUID]] = {}
    for entry in entries:
        binding_state: str | None = None
        if record is not None:
            resolved = resolve_occurrence(record, entry.block_id)
            if resolved.entry is None:
                raise EditorialRegistryConflictError("editorial_registry_conflict")
            entry = resolved.entry
            binding_state = resolved.binding_state
        source = synthesis if entry.source_stage is EditorialSourceStage.SYNTHESIS else enrichment
        if source is None:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        value = read_pointer(source, entry.structural_pointer)
        if canonical_content_sha256(value) != entry.content_sha256:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        if entry.kind in {"title", "section_heading"}:
            text_value = str(value)
        elif isinstance(value, Mapping):
            text_value = str(
                value.get("text")
                or value.get("caption")
                or value.get("title")
                or value.get("label")
                or value.get("name")
                or entry.kind.replace("_", " ").title()
            )
        else:
            text_value = str(value)
        evidence_refs = _project_evidence_refs(value)
        structured_table = (
            _project_structured_table(value)
            if entry.kind is EditorialBlockKind.TABLE
            and entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
            else None
        )
        structured_chart = (
            _project_structured_chart(value)
            if entry.kind is EditorialBlockKind.CHART
            and entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
            else None
        )
        operations = (
            allowed_operations(
                entry,
                synthesis,
                allow_table_insertion=(
                    record is not None
                    and table_insertion_preparation_ready(
                        record=record,
                        entry=entry,
                        synthesis_artifact=synthesis_artifact,
                        synthesis_read_sha256=synthesis_read_sha256,
                        enrichment_artifact=enrichment_artifact,
                        enrichment_read_sha256=enrichment_read_sha256,
                        anchor_registered=True,
                    )
                ),
                allow_bar_chart_insertion=(
                    record is not None
                    and bar_chart_insertion_preparation_ready(
                        record=record,
                        entry=entry,
                        synthesis_artifact=synthesis_artifact,
                        synthesis_read_sha256=synthesis_read_sha256,
                        enrichment_artifact=enrichment_artifact,
                        enrichment_read_sha256=enrichment_read_sha256,
                        anchor_registered=True,
                    )
                ),
            )
            if allow_operations and entry.revision_status is EditorialRevisionStatus.READY
            else ()
        )
        legacy_target_id: str | None = None
        if entry.source_stage is EditorialSourceStage.SYNTHESIS:
            try:
                _target, _operations, legacy_target_id = _target_for_entry(entry, synthesis)
            except EditorialIdentityAmbiguousError:
                legacy_target_id = None
        before_hash = entry.content_sha256
        if operations:
            before_hash = resolve_workspace_target(
                entry=entry,
                canonical_content=synthesis,
                operation=operations[0],
            ).before_hash
        block: dict[str, object] = {
            "block_id": str(entry.block_id),
            "kind": entry.kind,
            "parent_block_id": (
                str(entry.parent_block_id) if entry.parent_block_id is not None else None
            ),
            "position": entry.position,
            "text": text_value,
            "evidence_refs": evidence_refs,
            "table": structured_table,
            "chart": structured_chart,
            "revision_target": {
                "block_id": str(entry.block_id),
                "legacy_target_id": legacy_target_id,
            },
            "before_hash": before_hash,
            "allowed_operations": list(operations),
            "revision_status": (
                "needs_review"
                if binding_state == "ambiguous"
                or entry.revision_status is EditorialRevisionStatus.NEEDS_REVIEW
                or entry.block_id in needs_review_ids
                else "stale"
                if binding_state == "stale"
                else "ready"
            ),
        }
        blocks.append(block)
        if entry.kind == "section_heading":
            outline.append(
                {
                    "block_id": str(entry.block_id),
                    "label": text_value,
                    "children": [],
                }
            )
        elif entry.parent_block_id is not None:
            heading_children.setdefault(entry.parent_block_id, []).append(entry.block_id)
        if entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT:
            enrichments.append(block)
    for node in outline:
        heading_id = UUID(str(node["block_id"]))
        node["children"] = [str(item) for item in heading_children.get(heading_id, [])]
    synthesis_entries = [entry for entry in entries if entry.source_stage == "synthesis"]
    enrichment_entries = [
        entry for entry in entries if entry.source_stage == "editorial_enrichment"
    ]
    block_by_id = {UUID(str(block["block_id"])): block for block in blocks}
    synthesis_blocks = [block_by_id[entry.block_id] for entry in synthesis_entries]
    enrichment_blocks_by_id = {UUID(str(block["block_id"])): block for block in enrichments}
    insertions: dict[int, list[tuple[int, int, str, dict[str, object]]]] = {}
    stale_enrichments: list[tuple[int, str, dict[str, object]]] = []
    section_count = len(synthesis.get("sections", []))
    for entry in enrichment_entries:
        if enrichment is None:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        item = read_pointer(enrichment, entry.structural_pointer)
        if not isinstance(item, Mapping):
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        placement = item.get("placement")
        if not isinstance(placement, Mapping) or not isinstance(placement.get("kind"), str):
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        placement_kind = str(placement["kind"])
        section_index: int | None = None
        stale_anchor = False
        if placement_kind == "after_lead":
            boundary = next(
                (
                    index
                    for index, candidate in enumerate(synthesis_entries)
                    if candidate.kind == "section_heading"
                ),
                len(synthesis_entries),
            )
            placement_rank = 0
        elif placement_kind == "after_section":
            try:
                section_index = int(placement["section_index"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EditorialRegistryConflictError("editorial_registry_conflict") from exc
            section_prefix = f"/sections/{section_index}/"
            matches = [
                index
                for index, candidate in enumerate(synthesis_entries)
                if candidate.structural_pointer.startswith(section_prefix)
            ]
            if not matches:
                raise EditorialWorkspaceError("editorial_anchor_stale")
            boundary = max(matches) + 1
            placement_rank = section_index + 1
        elif placement_kind == "after_paragraph":
            try:
                anchor_block_id = UUID(str(placement["anchor_block_id"]))
                resolved_anchor = (
                    resolve_paragraph_anchor(record, anchor_block_id, synthesis=synthesis)
                    if record is not None
                    else None
                )
            except (KeyError, TypeError, ValueError, EditorialWorkspaceError):
                resolved_anchor = None
            anchor_index = (
                next(
                    (
                        index
                        for index, synthesis_entry in enumerate(synthesis_entries)
                        if synthesis_entry.block_id == resolved_anchor.block_id
                    ),
                    None,
                )
                if resolved_anchor is not None
                else None
            )
            if anchor_index is None:
                stale_anchor = True
            else:
                boundary = anchor_index + 1
                placement_rank = 0
        elif placement_kind in {"after_timeline", "end"}:
            boundary = len(synthesis_entries)
            placement_rank = section_count + (1 if placement_kind == "after_timeline" else 2)
        else:
            raise EditorialRegistryConflictError("editorial_registry_conflict")
        element_key = str(item.get("element_key") or item.get("key") or entry.structural_pointer)
        enrichment_block = enrichment_blocks_by_id[entry.block_id]
        if stale_anchor:
            enrichment_block["revision_status"] = "stale"
            stale_enrichments.append((entry.position, element_key, enrichment_block))
            continue
        insertions.setdefault(boundary, []).append(
            (
                placement_rank,
                entry.position,
                element_key,
                enrichment_block,
            )
        )

    ordered_blocks: list[dict[str, object]] = []
    ordered_enrichments: list[dict[str, object]] = []
    for boundary in range(len(synthesis_blocks) + 1):
        attached = sorted(insertions.get(boundary, []), key=lambda item: item[:3])
        for _rank, _position, _key, block in attached:
            ordered_blocks.append(block)
            ordered_enrichments.append(block)
        if boundary < len(synthesis_blocks):
            ordered_blocks.append(synthesis_blocks[boundary])
    ordered_enrichments.extend(
        block for _position, _element_key, block in sorted(stale_enrichments)
    )
    blocks = ordered_blocks
    enrichments = ordered_enrichments
    return blocks, enrichments, outline


def _project_structured_table(value: Any) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    title = value.get("title")
    columns = value.get("columns")
    rows = value.get("rows")
    placement = value.get("placement")
    key = value.get("key")
    table_kind = value.get("kind")
    if (
        not isinstance(key, str)
        or not isinstance(table_kind, str)
        or table_kind not in {kind.value for kind in EnrichmentTableKind}
        or not isinstance(title, str)
        or not isinstance(columns, list)
        or not isinstance(rows, list)
        or not isinstance(placement, Mapping)
        or not isinstance(placement.get("kind"), str)
    ):
        return None
    placement_kind = placement["kind"]
    if placement_kind not in {
        "after_paragraph",
        "after_lead",
        "after_timeline",
        "after_section",
        "end",
    }:
        return None
    projected_columns = [
        {"key": column["key"], "label": column["label"]}
        for column in columns
        if isinstance(column, Mapping)
        and isinstance(column.get("key"), str)
        and isinstance(column.get("label"), str)
    ]
    projected_rows = [
        {
            "cells": list(row["cells"]),
            "evidence_refs": _project_evidence_refs(row),
        }
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("cells"), list)
        and all(isinstance(cell, str) for cell in row["cells"])
    ]
    if len(projected_columns) != len(columns) or len(projected_rows) != len(rows):
        return None
    anchor_block_id = placement.get("anchor_block_id")
    if anchor_block_id is not None and not isinstance(anchor_block_id, str):
        anchor_block_id = None
    if placement_kind == "after_paragraph" and anchor_block_id is None:
        return None
    return {
        "key": key,
        "kind": table_kind,
        "title": title,
        "caption": value.get("caption") if isinstance(value.get("caption"), str) else None,
        "columns": projected_columns,
        "rows": projected_rows,
        "evidence_refs": _project_evidence_refs(value),
        "anchor_block_id": anchor_block_id,
        "placement": placement_kind,
    }


def _project_structured_chart(value: Any) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("kind") != "bar":
        return None
    points = value.get("points")
    placement = value.get("placement")
    if not isinstance(points, list) or not isinstance(placement, Mapping):
        return None
    projected_points: list[dict[str, object]] = []
    for point in points:
        if not isinstance(point, Mapping) or not isinstance(point.get("value"), str):
            return None
        refs = _project_evidence_refs(point)
        if not refs or not isinstance(point.get("label"), str):
            return None
        projected_points.append(
            {
                "label": point["label"],
                "value": point["value"],
                "evidence_refs": refs,
            }
        )
    return {
        "key": value.get("key"),
        "kind": "bar",
        "title": value.get("title"),
        "caption": value.get("caption"),
        "legend": list(
            dict.fromkeys(point.get("series") for point in points if isinstance(point, Mapping))
        ),
        "category_axis_label": value.get("category_axis_label"),
        "value_axis_label": value.get("value_axis_label"),
        "unit": value.get("unit"),
        "points": projected_points,
        "anchor_block_id": placement.get("anchor_block_id"),
    }


def _project_evidence_refs(value: Any) -> list[dict[str, object]]:
    references: dict[str, dict[str, object]] = {}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            raw_refs = item.get("evidence_refs")
            if isinstance(raw_refs, list):
                for reference in raw_refs:
                    if isinstance(reference, Mapping):
                        normalized = {str(key): value for key, value in reference.items()}
                        key = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
                        references[key] = normalized
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return [references[key] for key in sorted(references)]
