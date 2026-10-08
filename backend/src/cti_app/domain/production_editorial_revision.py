"""Typed contract shared by targeted editorial revision operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.production import ProductionStage
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind


class TargetedEditorialRevisionOperation(StrEnum):
    REWRITE_TITLE = "rewrite_title"
    REWRITE_LEAD = "rewrite_lead"
    REWRITE_PARAGRAPH = "rewrite_paragraph"
    INSERT_PARAGRAPH = "insert_paragraph"
    DELETE_PARAGRAPH = "delete_paragraph"
    REWRITE_SECTION = "rewrite_section"
    INSERT_SECTION = "insert_section"
    DELETE_SECTION = "delete_section"
    MERGE_SECTIONS = "merge_sections"
    SPLIT_SECTION = "split_section"
    RENAME_SECTION = "rename_section"
    CREATE_TABLE = "create_table"
    REWRITE_TABLE = "rewrite_table"
    DELETE_TABLE = "delete_table"
    CREATE_DIAGRAM = "create_diagram"
    REWRITE_DIAGRAM = "rewrite_diagram"
    DELETE_DIAGRAM = "delete_diagram"
    CREATE_CHART = "create_chart"
    REWRITE_CHART = "rewrite_chart"
    DELETE_CHART = "delete_chart"
    CREATE_SOURCE_FIGURE = "create_source_figure"
    REWRITE_SOURCE_FIGURE = "rewrite_source_figure"
    REPLACE_SOURCE_FIGURE = "replace_source_figure"
    DELETE_SOURCE_FIGURE = "delete_source_figure"


class TargetedEditorialRevisionMode(StrEnum):
    EXISTING_EVIDENCE_ONLY = "existing_evidence_only"
    ALLOW_SOURCE_EXTENSION = "allow_source_extension"


class TargetedEditorialRevisionTargetKind(StrEnum):
    TITLE = "title"
    LEAD = "lead"
    LEAD_PARAGRAPH = "lead_paragraph"
    SECTIONS = "sections"
    SECTION = "section"
    SECTION_PARAGRAPH = "section_paragraph"
    ENRICHMENT_ELEMENT = "enrichment_element"


class TargetedEditorialRevisionPlacementKind(StrEnum):
    AFTER_LEAD = "after_lead"
    AFTER_SECTION = "after_section"
    AFTER_TIMELINE = "after_timeline"


@dataclass(frozen=True, slots=True)
class TargetedEditorialRevisionPlacement:
    kind: TargetedEditorialRevisionPlacementKind
    section_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TargetedEditorialRevisionPlacementKind):
            raise ValueError("targeted_editorial_revision_placement_kind_invalid")
        if self.kind is TargetedEditorialRevisionPlacementKind.AFTER_SECTION:
            if self.section_id is None or _SHA256.fullmatch(self.section_id) is None:
                raise ValueError("targeted_editorial_revision_placement_section_required")
        elif self.section_id is not None:
            raise ValueError("targeted_editorial_revision_placement_section_invalid")

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "section_id": self.section_id}


class TargetedEditorialRevisionRefusalCode(StrEnum):
    REVISION_NOT_APPLICABLE = "revision_not_applicable"
    SOURCE_EXTENSION_NOT_AVAILABLE_YET = "source_extension_not_available_yet"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TargetedEditorialRevisionConflictError(RuntimeError):
    code = "targeted_editorial_revision_conflict"


@dataclass(frozen=True, slots=True)
class TargetedEditorialRevisionTarget:
    """A target uses content hashes for stable elements and anchors."""

    kind: TargetedEditorialRevisionTargetKind
    stable_ids: tuple[str, ...] = ()
    parent_id: str | None = None
    anchor_id: str | None = None
    insert_after: bool = False
    element_kind: EditorialEnrichmentElementKind | None = None
    element_key: str | None = None
    placement: TargetedEditorialRevisionPlacement | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TargetedEditorialRevisionTargetKind):
            raise ValueError("targeted_editorial_revision_target_kind_invalid")
        if not isinstance(self.stable_ids, tuple) or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in self.stable_ids
        ):
            raise ValueError("targeted_editorial_revision_target_id_invalid")
        if len(self.stable_ids) != len(set(self.stable_ids)):
            raise ValueError("targeted_editorial_revision_target_duplicate_id")
        if self.insert_after and self.anchor_id is None:
            raise ValueError("targeted_editorial_revision_anchor_required")
        if self.anchor_id is not None and self.kind not in {
            TargetedEditorialRevisionTargetKind.LEAD,
            TargetedEditorialRevisionTargetKind.SECTIONS,
            TargetedEditorialRevisionTargetKind.SECTION,
        }:
            raise ValueError("targeted_editorial_revision_anchor_invalid")
        required_ids = {
            TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH: 1,
            TargetedEditorialRevisionTargetKind.SECTION: 1,
            TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH: 1,
        }
        expected = required_ids.get(self.kind)
        if expected is not None and len(self.stable_ids) != expected:
            raise ValueError("targeted_editorial_revision_target_id_count_invalid")
        if self.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH:
            if self.parent_id is None:
                raise ValueError("targeted_editorial_revision_parent_id_required")
        elif self.parent_id is not None:
            raise ValueError("targeted_editorial_revision_parent_id_invalid")
        if (
            self.kind
            in {
                TargetedEditorialRevisionTargetKind.TITLE,
                TargetedEditorialRevisionTargetKind.LEAD,
            }
            and self.stable_ids
        ):
            raise ValueError("targeted_editorial_revision_target_id_invalid")
        if self.kind is TargetedEditorialRevisionTargetKind.SECTIONS and len(
            self.stable_ids
        ) not in {
            0,
            2,
        }:
            raise ValueError("targeted_editorial_revision_target_id_count_invalid")
        for value in (self.parent_id, self.anchor_id):
            if value is not None and (
                not isinstance(value, str) or _SHA256.fullmatch(value) is None
            ):
                raise ValueError("targeted_editorial_revision_target_id_invalid")
        if type(self.insert_after) is not bool:
            raise ValueError("targeted_editorial_revision_insert_after_invalid")
        if self.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            if (
                not isinstance(self.element_kind, EditorialEnrichmentElementKind)
                or self.stable_ids
                or self.parent_id is not None
                or self.anchor_id is not None
                or self.insert_after
            ):
                raise ValueError("targeted_editorial_revision_enrichment_target_invalid")
            if self.element_key is not None and (
                not self.element_key.strip() or len(self.element_key) > 128
            ):
                raise ValueError("targeted_editorial_revision_enrichment_key_invalid")
        elif (
            self.element_kind is not None
            or self.element_key is not None
            or self.placement is not None
        ):
            raise ValueError("targeted_editorial_revision_enrichment_target_invalid")

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "stable_ids": list(self.stable_ids),
            "parent_id": self.parent_id,
            "anchor_id": self.anchor_id,
            "insert_after": self.insert_after,
        }
        if self.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            payload.update(
                {
                    "element_kind": (
                        self.element_kind.value if self.element_kind is not None else None
                    ),
                    "element_key": self.element_key,
                    "placement": self.placement.to_json() if self.placement is not None else None,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class TargetedEditorialRevisionRequest:
    subject_id: UUID
    base_artifact_id: UUID
    base_version: int
    base_input_hash: str
    base_canonical_sha256: str
    target: TargetedEditorialRevisionTarget
    operation: TargetedEditorialRevisionOperation
    instruction: str
    mode: TargetedEditorialRevisionMode
    requester: str


@dataclass(frozen=True, slots=True)
class TargetedEditorialRevisionRefusal:
    code: TargetedEditorialRevisionRefusalCode
    recommended_restart_stage: ProductionStage | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "recommended_restart_stage": (
                self.recommended_restart_stage.value
                if self.recommended_restart_stage is not None
                else None
            ),
            "reason": self.reason,
        }
