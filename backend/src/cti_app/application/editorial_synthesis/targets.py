"""Stable synthesis targets and occurrence-aware canonical resolution."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)


@dataclass(frozen=True, slots=True)
class _ResolvedTarget:
    path: str
    before_hash: str
    indexes: tuple[int, ...] = ()
    parent_index: int | None = None
    anchor_index: int | None = None


def _canonical_bytes(value: Any) -> bytes:
    return ProductionArtifactStore.canonical_json_bytes(value)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _paragraph_id(payload: Mapping[str, Any]) -> str:
    return _sha256(payload)


def _section_id(payload: Mapping[str, Any]) -> str:
    return _sha256(payload)


def _enrichment_section_anchor_id(payload: Mapping[str, Any]) -> str:
    """Identify a synthesis section for enrichment without binding its heading.

    Section IDs used by targeted editorial requests intentionally include the
    heading. Enrichment placements need a separate identity: changing a title
    must not invalidate evidence anchored to the section's paragraphs.
    """
    anchor_payload = dict(payload)
    anchor_payload.pop("heading", None)
    return _sha256(anchor_payload)


def build_targeted_revision_target_catalog(
    stage: str, canonical_content: Any
) -> list[dict[str, Any]]:
    """Expose stable, server-derived targets and valid operations for the UI."""
    if not isinstance(canonical_content, Mapping):
        return []

    def option(
        *,
        label: str,
        target: TargetedEditorialRevisionTarget,
        operations: Sequence[TargetedEditorialRevisionOperation],
        summary: str,
    ) -> dict[str, Any]:
        payload = target.to_json()
        return {
            "id": _sha256(payload),
            "label": label,
            "summary": summary,
            "target": payload,
            "operations": [item.value for item in operations],
        }

    if stage == ProductionArtifactStage.SYNTHESIS.value:
        lead = canonical_content.get("lead")
        sections = canonical_content.get("sections")
        if not isinstance(lead, list) or not isinstance(sections, list):
            return []
        options = [
            option(
                label="Titre",
                target=TargetedEditorialRevisionTarget(
                    kind=TargetedEditorialRevisionTargetKind.TITLE
                ),
                operations=(TargetedEditorialRevisionOperation.REWRITE_TITLE,),
                summary=str(canonical_content.get("title", "")),
            ),
            option(
                label="Chapeau",
                target=TargetedEditorialRevisionTarget(
                    kind=TargetedEditorialRevisionTargetKind.LEAD
                ),
                operations=(
                    TargetedEditorialRevisionOperation.REWRITE_LEAD,
                    TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
                ),
                summary=" ".join(
                    str(item.get("text", "")) for item in lead if isinstance(item, Mapping)
                ),
            ),
            option(
                label="Sections · créer une section",
                target=TargetedEditorialRevisionTarget(
                    kind=TargetedEditorialRevisionTargetKind.SECTIONS
                ),
                operations=(TargetedEditorialRevisionOperation.INSERT_SECTION,),
                summary=f"{len(sections)} section(s)",
            ),
        ]
        raw_section_ids = [
            _section_id(section) for section in sections if isinstance(section, Mapping)
        ]
        unique_section_ids = {
            identity for identity, count in Counter(raw_section_ids).items() if count == 1
        }
        section_ids_by_index: dict[int, str] = {}
        for section_index, section in enumerate(sections):
            if not isinstance(section, Mapping):
                continue
            section_id = _section_id(section)
            if section_id not in unique_section_ids:
                continue
            section_ids_by_index[section_index] = section_id
            heading = str(section.get("heading", "Section"))
            paragraphs = section.get("paragraphs")
            if not isinstance(paragraphs, list):
                paragraphs = []
            paragraph_ids = [
                _paragraph_id(paragraph)
                for paragraph in paragraphs
                if isinstance(paragraph, Mapping)
            ]
            unique_paragraph_ids = {
                identity for identity, count in Counter(paragraph_ids).items() if count == 1
            }
            options.append(
                option(
                    label=f"Section · {heading}",
                    target=TargetedEditorialRevisionTarget(
                        kind=TargetedEditorialRevisionTargetKind.SECTION,
                        stable_ids=(section_id,),
                    ),
                    operations=(
                        TargetedEditorialRevisionOperation.REWRITE_SECTION,
                        TargetedEditorialRevisionOperation.DELETE_SECTION,
                        TargetedEditorialRevisionOperation.SPLIT_SECTION,
                        TargetedEditorialRevisionOperation.RENAME_SECTION,
                        TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
                    ),
                    summary=" ".join(
                        str(item.get("text", ""))
                        for item in paragraphs
                        if isinstance(item, Mapping)
                    ),
                )
            )
            for paragraph in paragraphs:
                if not isinstance(paragraph, Mapping):
                    continue
                paragraph_id = _paragraph_id(paragraph)
                if paragraph_id not in unique_paragraph_ids:
                    continue
                text = str(paragraph.get("text", ""))
                options.append(
                    option(
                        label=f"Paragraphe · {heading} · {text[:100]}",
                        target=TargetedEditorialRevisionTarget(
                            kind=TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH,
                            stable_ids=(paragraph_id,),
                            parent_id=section_id,
                        ),
                        operations=(
                            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                            TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
                        ),
                        summary=text,
                    )
                )
        lead_target = TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH
        lead_ids = [
            _paragraph_id(paragraph) for paragraph in lead if isinstance(paragraph, Mapping)
        ]
        unique_lead_ids = {identity for identity, count in Counter(lead_ids).items() if count == 1}
        for paragraph in lead:
            if not isinstance(paragraph, Mapping):
                continue
            if _paragraph_id(paragraph) not in unique_lead_ids:
                continue
            text = str(paragraph.get("text", ""))
            options.append(
                option(
                    label=f"Paragraphe du chapeau · {text[:100]}",
                    target=TargetedEditorialRevisionTarget(
                        kind=lead_target,
                        stable_ids=(_paragraph_id(paragraph),),
                    ),
                    operations=(
                        TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                        TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
                    ),
                    summary=text,
                )
            )
        for index in range(len(sections) - 1):
            first = sections[index]
            second = sections[index + 1]
            if (
                not isinstance(first, Mapping)
                or not isinstance(second, Mapping)
                or index not in section_ids_by_index
                or index + 1 not in section_ids_by_index
            ):
                continue
            options.append(
                option(
                    label=(
                        "Fusionner · "
                        f"{first.get('heading', 'Section')} + "
                        f"{second.get('heading', 'Section')}"
                    ),
                    target=TargetedEditorialRevisionTarget(
                        kind=TargetedEditorialRevisionTargetKind.SECTIONS,
                        stable_ids=(section_ids_by_index[index], section_ids_by_index[index + 1]),
                    ),
                    operations=(TargetedEditorialRevisionOperation.MERGE_SECTIONS,),
                    summary="Deux sections adjacentes",
                )
            )
        return options

    if stage != ProductionArtifactStage.EDITORIAL_ENRICHMENT.value:
        return []

    family_specs = (
        ("tables", EditorialEnrichmentElementKind.TABLE, "Tableau"),
        ("diagrams", EditorialEnrichmentElementKind.DIAGRAM, "Diagramme"),
        ("charts", EditorialEnrichmentElementKind.CHART, "Graphique"),
        ("source_figures", EditorialEnrichmentElementKind.FIGURE, "Figure source"),
    )
    options = []
    operation_by_kind = {
        EditorialEnrichmentElementKind.TABLE: (
            TargetedEditorialRevisionOperation.REWRITE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_TABLE,
        ),
        EditorialEnrichmentElementKind.DIAGRAM: (
            TargetedEditorialRevisionOperation.REWRITE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
        ),
        EditorialEnrichmentElementKind.CHART: (
            TargetedEditorialRevisionOperation.REWRITE_CHART,
            TargetedEditorialRevisionOperation.DELETE_CHART,
        ),
        EditorialEnrichmentElementKind.FIGURE: (
            TargetedEditorialRevisionOperation.REWRITE_SOURCE_FIGURE,
            TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        ),
    }
    create_operation_by_kind = {
        EditorialEnrichmentElementKind.TABLE: TargetedEditorialRevisionOperation.CREATE_TABLE,
        EditorialEnrichmentElementKind.DIAGRAM: TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
        EditorialEnrichmentElementKind.CHART: TargetedEditorialRevisionOperation.CREATE_CHART,
    }
    for field_name, element_kind, family_label in family_specs:
        raw_elements = canonical_content.get(field_name)
        elements = raw_elements if isinstance(raw_elements, list) else []
        for element in elements:
            if not isinstance(element, Mapping) or not isinstance(element.get("key"), str):
                continue
            element_key = str(element["key"])
            summary = str(element.get("title") or element.get("caption") or element_key)
            options.append(
                option(
                    label=f"{family_label} · {summary}",
                    target=TargetedEditorialRevisionTarget(
                        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
                        element_kind=element_kind,
                        element_key=element_key,
                    ),
                    operations=operation_by_kind[element_kind],
                    summary=summary,
                )
            )
        if element_kind in create_operation_by_kind:
            options.append(
                option(
                    label=f"{family_label} · créer",
                    target=TargetedEditorialRevisionTarget(
                        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
                        element_kind=element_kind,
                        placement=TargetedEditorialRevisionPlacement(
                            kind=TargetedEditorialRevisionPlacementKind.AFTER_LEAD
                        ),
                    ),
                    operations=(create_operation_by_kind[element_kind],),
                    summary="Nouvel élément placé après le chapeau",
                )
            )
    return options


def _indexed_unique(
    values: Sequence[Mapping[str, Any]], key_fn: Any, *, label: str
) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, value in enumerate(values):
        key = key_fn(value)
        if key in result:
            raise ValueError(f"targeted_synthesis_revision_ambiguous_{label}")
        result[key] = index
    return result


def _target_path(
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation | None = None,
) -> str:
    if target.kind is TargetedEditorialRevisionTargetKind.TITLE:
        return "synthesis.title"
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD:
        if operation is TargetedEditorialRevisionOperation.INSERT_PARAGRAPH and target.anchor_id:
            relation = "after" if target.insert_after else "before"
            return f"synthesis.lead.{relation}[{target.anchor_id}]"
        return "synthesis.lead"
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
        return f"synthesis.lead[{target.stable_ids[0]}]"
    if target.kind is TargetedEditorialRevisionTargetKind.SECTIONS:
        if target.stable_ids:
            return f"synthesis.sections[{','.join(target.stable_ids)}]"
        if target.anchor_id is not None:
            relation = "after" if target.insert_after else "before"
            return f"synthesis.sections.{relation}[{target.anchor_id}]"
        return "synthesis.sections"
    if target.kind is TargetedEditorialRevisionTargetKind.SECTION:
        if operation is TargetedEditorialRevisionOperation.INSERT_PARAGRAPH:
            suffix = (
                f".paragraph.{('after' if target.insert_after else 'before')}[{target.anchor_id}]"
                if target.anchor_id is not None
                else ".paragraphs"
            )
            return f"synthesis.section[{target.stable_ids[0]}]{suffix}"
        return f"synthesis.section[{target.stable_ids[0]}]"
    assert target.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH
    return f"synthesis.section[{target.parent_id}].paragraph[{target.stable_ids[0]}]"


def _resolve_target(
    payload: Mapping[str, Any],
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
) -> _ResolvedTarget:
    sections = payload.get("sections")
    lead = payload.get("lead")
    if not isinstance(sections, list) or not isinstance(lead, list):
        raise ValueError("targeted_synthesis_revision_base_invalid")
    section_ids = _indexed_unique(sections, _section_id, label="section")
    path = _target_path(target, operation)

    def before_hash(value: Any) -> str:
        return _sha256(value)

    if target.kind is TargetedEditorialRevisionTargetKind.TITLE:
        if (
            operation is not TargetedEditorialRevisionOperation.REWRITE_TITLE
            or target.stable_ids
            or target.anchor_id is not None
            or target.insert_after
        ):
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        return _ResolvedTarget(path, before_hash(payload.get("title")))
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD:
        if (
            operation
            not in {
                TargetedEditorialRevisionOperation.REWRITE_LEAD,
                TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
            }
            or target.stable_ids
            or (
                operation is TargetedEditorialRevisionOperation.REWRITE_LEAD
                and (target.anchor_id is not None or target.insert_after)
            )
        ):
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        anchor_index = None
        if target.anchor_id is not None:
            lead_ids = _indexed_unique(lead, _paragraph_id, label="lead_paragraph")
            if target.anchor_id not in lead_ids:
                raise ValueError("targeted_synthesis_revision_target_missing")
            anchor_index = lead_ids[target.anchor_id]
        return _ResolvedTarget(path, before_hash(lead), anchor_index=anchor_index)
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
        if (
            operation
            not in {
                TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
            }
            or len(target.stable_ids) != 1
            or target.parent_id is not None
        ):
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        lead_ids = _indexed_unique(lead, _paragraph_id, label="lead_paragraph")
        index = lead_ids.get(target.stable_ids[0])
        if index is None:
            raise ValueError("targeted_synthesis_revision_target_missing")
        return _ResolvedTarget(path, before_hash(lead[index]), indexes=(index,))
    if target.kind is TargetedEditorialRevisionTargetKind.SECTIONS:
        if operation not in {
            TargetedEditorialRevisionOperation.INSERT_SECTION,
            TargetedEditorialRevisionOperation.MERGE_SECTIONS,
        }:
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        if operation is TargetedEditorialRevisionOperation.MERGE_SECTIONS:
            if len(target.stable_ids) != 2 or target.anchor_id is not None or target.insert_after:
                raise ValueError("targeted_synthesis_revision_merge_requires_two_sections")
            try:
                indexes = tuple(section_ids[item] for item in target.stable_ids)
            except KeyError as exc:
                raise ValueError("targeted_synthesis_revision_target_missing") from exc
            if abs(indexes[0] - indexes[1]) != 1:
                raise ValueError("targeted_synthesis_revision_merge_requires_adjacent_sections")
            selected = [sections[index] for index in sorted(indexes)]
            return _ResolvedTarget(path, before_hash(selected), indexes=tuple(sorted(indexes)))
        if target.stable_ids:
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        anchor_index = None
        if target.anchor_id is not None:
            if target.anchor_id not in section_ids:
                raise ValueError("targeted_synthesis_revision_target_missing")
            anchor_index = section_ids[target.anchor_id]
        return _ResolvedTarget(path, before_hash(sections), anchor_index=anchor_index)
    if target.kind is TargetedEditorialRevisionTargetKind.SECTION:
        if len(target.stable_ids) != 1 or target.parent_id is not None:
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        if operation is TargetedEditorialRevisionOperation.INSERT_PARAGRAPH:
            section_index = section_ids.get(target.stable_ids[0])
            if section_index is None:
                raise ValueError("targeted_synthesis_revision_target_missing")
            paragraphs = sections[section_index].get("paragraphs")
            if not isinstance(paragraphs, list):
                raise ValueError("targeted_synthesis_revision_base_invalid")
            anchor_index = None
            if target.anchor_id is not None:
                paragraph_ids = _indexed_unique(
                    paragraphs, _paragraph_id, label="section_paragraph"
                )
                if target.anchor_id not in paragraph_ids:
                    raise ValueError("targeted_synthesis_revision_target_missing")
                anchor_index = paragraph_ids[target.anchor_id]
            return _ResolvedTarget(
                path,
                before_hash(paragraphs),
                parent_index=section_index,
                anchor_index=anchor_index,
            )
        if operation not in {
            TargetedEditorialRevisionOperation.REWRITE_SECTION,
            TargetedEditorialRevisionOperation.DELETE_SECTION,
            TargetedEditorialRevisionOperation.SPLIT_SECTION,
            TargetedEditorialRevisionOperation.RENAME_SECTION,
        }:
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        if target.anchor_id is not None or target.insert_after:
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        section_index = section_ids.get(target.stable_ids[0])
        if section_index is None:
            raise ValueError("targeted_synthesis_revision_target_missing")
        return _ResolvedTarget(path, before_hash(sections[section_index]), indexes=(section_index,))
    if target.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH:
        if (
            operation
            not in {
                TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
                TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
            }
            or len(target.stable_ids) != 1
            or target.parent_id is None
        ):
            raise ValueError("targeted_synthesis_revision_target_action_mismatch")
        section_index = section_ids.get(target.parent_id)
        if section_index is None:
            raise ValueError("targeted_synthesis_revision_target_missing")
        paragraphs = sections[section_index].get("paragraphs")
        if not isinstance(paragraphs, list):
            raise ValueError("targeted_synthesis_revision_base_invalid")
        paragraph_ids = _indexed_unique(paragraphs, _paragraph_id, label="section_paragraph")
        paragraph_index = paragraph_ids.get(target.stable_ids[0])
        if paragraph_index is None:
            raise ValueError("targeted_synthesis_revision_target_missing")
        return _ResolvedTarget(
            path,
            before_hash(paragraphs[paragraph_index]),
            indexes=(paragraph_index,),
            parent_index=section_index,
        )
    raise ValueError("targeted_synthesis_revision_target_kind_invalid")


# Public names for the editor's shared target-resolution seam.
ResolvedTarget = _ResolvedTarget
resolve_target = _resolve_target
