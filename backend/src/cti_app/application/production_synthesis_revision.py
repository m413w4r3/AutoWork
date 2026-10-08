"""Bounded, evidence-only revisions of an existing synthesis artifact."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.model_gateway import (
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_editorial_enrichment import (
    build_editorial_enrichment_evidence_pack,
    canonical_synthesis_hash,
    compute_editorial_enrichment_input_hash,
    editorial_enrichment_evidence_pack_hash,
    semantic_annotation_anchor_texts,
    validate_editorial_enrichment,
)
from cti_app.application.production_prompts import (
    SYNTHESIS_EDITORIAL_CONTRACT_V5,
    SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION,
    SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION,
)
from cti_app.application.production_references import production_reference_corpus_from_json
from cti_app.application.production_source_additions import (
    SourceAdditionConflictError,
    SourceAdditionError,
    SourceAdditionPromotionBlockedError,
    SourceAdditionPromotionResult,
    SourceAdditionService,
)
from cti_app.application.production_stages import EditorialEnrichmentService, SynthesisService
from cti_app.application.production_synthesis import (
    ProductionSynthesisService,
    SynthesisEvidencePackV1,
    SynthesisProposalControlError,
    SynthesisProposalV1,
    build_synthesis_evidence_pack,
    build_synthesis_model_request,
    canonical_extraction_hash,
    draft_synthesis_proposal,
    parse_synthesis_proposal_wire,
    synthesis_access_policy_hash,
    synthesis_evidence_pack_hash,
    synthesis_input_hash,
    validate_synthesis_proposal,
)
from cti_app.application.publication_assembly import PublicationAssemblyService
from cti_app.application.publication_rendering import PublicationRenderService
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionInputSnapshot,
    ProductionRun,
    ProductionStage,
    SynthesisMode,
)
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EditorialEnrichmentV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import (
    EvidenceUseScope,
    TargetedEditorialRevisionConflictError,
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionRefusal,
    TargetedEditorialRevisionRefusalCode,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import (
    ProductionExtractionOmissionReason,
    ProductionExtractionV1,
    is_production_extraction_hash,
    production_extraction_from_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionSourceCriticality,
    production_source_criticality,
)
from cti_app.domain.production_synthesis import (
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    is_valid_editorial_title,
    production_synthesis_from_json,
    production_synthesis_to_json,
    validate_synthesis_lineage,
    validate_synthesis_section_headings,
)

TARGETED_SYNTHESIS_REVISION_PARSER_VERSION = "targeted-synthesis-patch-parser-v3-heading-after-text"
TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION = "targeted-synthesis-patch-validator-v3-no-change"
MAX_TARGETED_REVISION_INSTRUCTION_LENGTH = 2000
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
class TargetedSynthesisRevisionPreview:
    base_artifact_id: UUID
    base_version: int
    base_input_hash: str
    base_canonical_sha256: str
    target_path: str
    stages_preserved: tuple[str, ...]
    stages_invalidated: tuple[str, ...]
    stages_conditionally_rebased: tuple[str, ...]
    source_extension: bool
    expected_model_calls: int
    enrichment_rebase: str
    before_hash: str | None = None
    source_additions: tuple[Mapping[str, Any], ...] = ()
    source_additions_hash: str | None = None
    source_addition_ready: bool | None = None
    source_addition_blockers: tuple[Mapping[str, Any], ...] = ()
    expected_model_calls_by_stage: Mapping[str, Mapping[str, int]] | None = None
    stages_rebased: tuple[str, ...] = ()
    historical_artifacts_preserved: bool = True
    refusal: TargetedEditorialRevisionRefusal | None = None


@dataclass(frozen=True, slots=True)
class TargetedSynthesisRevisionResult:
    artifact: ProductionArtifact | None
    publication_artifact: ProductionArtifact | None
    revision: dict[str, Any] | None
    refusal: TargetedEditorialRevisionRefusal | None = None


@dataclass(frozen=True, slots=True)
class _WirePatch:
    target: str
    action: TargetedEditorialRevisionOperation
    base_artifact_hash: str
    before_hash: str
    after: str
    rationale: str
    refusal: TargetedEditorialRevisionRefusal | None = None


@dataclass(frozen=True, slots=True)
class _RevisionContext:
    run: ProductionRun
    snapshot: ProductionInputSnapshot
    base_artifact: ProductionArtifact
    base_payload: dict[str, Any]
    references_artifact: ProductionArtifact
    extraction_artifact: ProductionArtifact
    projection_artifact: ProductionArtifact | None
    publication_artifact: ProductionArtifact | None


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


def _render_current_synthesis(payload: Mapping[str, Any]) -> str:
    lines = [f"TITLE [{_target_hash(payload.get('title'))}]: {payload.get('title', '')}", "LEAD:"]
    for paragraph in payload.get("lead", []):
        identity = _paragraph_id(paragraph)
        lines.append(f"  PARAGRAPH [{identity}]: {paragraph.get('text', '')}")
    lines.append("SECTIONS:")
    for section in payload.get("sections", []):
        identity = _section_id(section)
        heading = section.get("heading", "")
        lines.append(f"  SECTION [{identity}] kind={section.get('kind')} heading={heading}")
        for paragraph in section.get("paragraphs", []):
            lines.append(f"    PARAGRAPH [{_paragraph_id(paragraph)}]: {paragraph.get('text', '')}")
    return "\n".join(lines)


def _target_hash(value: Any) -> str:
    return _sha256(value)


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
        from cti_app.domain.production import ProductionStage

        stage_value = values.get("recommended_restart_stage", "").upper()
        try:
            stage = next((item for item in ProductionStage if item.name == stage_value), None)
            if stage is None and stage_value:
                stage = ProductionStage(stage_value.lower())
        except ValueError as exc:
            raise TargetedSynthesisRevisionValidationError(
                "revision_refusal_restart_stage_invalid"
            ) from exc
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
                recommended_restart_stage=stage,
                reason=reason,
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


def _extract_after_text(raw: str, name: str) -> str:
    marker = re.compile(
        rf"(?is)@@\s*{re.escape(name)}\s*@@\s*(.*?)\s*@@\s*END\s+{re.escape(name)}\s*@@"
    )
    match = marker.search(raw)
    if match is not None:
        return match.group(1).strip()
    inline = re.compile(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.*?)\s*$")
    match = inline.search(raw)
    if match is not None:
        return match.group(1).strip()
    if name in {"TITLE", "HEADING"} and raw.strip() and "@@" not in raw:
        return raw.strip()
    return ""


def _parse_after_claims(
    raw: str,
    *,
    evidence_pack: SynthesisEvidencePackV1,
    extraction: ProductionExtractionV1,
    unchanged_lead_wire: str = "",
    source_addition_document_ids: frozenset[UUID] = frozenset(),
    require_claim_scopes: bool = False,
) -> tuple[SynthesisProposalV1, tuple[SynthesisParagraphV1, ...], tuple[SynthesisSectionV1, ...]]:
    synthetic_lead = not re.search(r"(?im)^\s*@@\s*LEAD\s*@@", raw)
    parse_input = f"{unchanged_lead_wire}\n{raw}" if synthetic_lead else raw
    parsed = parse_synthesis_proposal_wire(parse_input)
    if parsed.proposal is None or parsed.error_code is not None or parsed.rejections:
        raise TargetedSynthesisRevisionValidationError(
            "revision_after_claims_invalid",
            details={
                "parse_error": parsed.error_code,
                "rejections": [item.reason_code for item in parsed.rejections],
            },
        )
    if parsed.proposal.source_notes:
        raise TargetedSynthesisRevisionValidationError("revision_patch_cannot_change_source_notes")
    if parsed.proposal.title is not None:
        raise TargetedSynthesisRevisionValidationError("revision_patch_cannot_change_title_here")
    _assert_claim_handles_and_relations_supported(
        parsed.proposal,
        evidence_pack,
        check_lead=not synthetic_lead,
        extraction=extraction,
        source_addition_document_ids=source_addition_document_ids,
        require_claim_scopes=require_claim_scopes,
    )
    try:
        lead, sections = validate_synthesis_proposal(parsed.proposal, evidence_pack, extraction)
    except SynthesisProposalControlError as exc:
        raise TargetedSynthesisRevisionValidationError(
            exc.code.value,
            details={"reason": exc.reason},
        ) from exc
    return parsed.proposal, (() if synthetic_lead else lead), sections


_GROUNDING_STOP_WORDS = frozenset(
    (
        "a an and are as at be been being by can could did do does for from had has have he her "
        "hers him his i in is it its may might must of on or our she should that the their them "
        "then there these they this those to was we were what when where which who will with would "
        "you your le la les un une des de du et est sont dans par pour sur cette ce ces son sa ses "
        "qui "
        "que aux au en comme leur leurs chaque selon"
    ).split()
)


def _grounding_token(value: str) -> str:
    token = unicodedata.normalize("NFKC", value).casefold().replace("\u2019", "'").strip("'-")
    if token.endswith(("ies", "ied")) and len(token) > 5:
        return f"{token[:-3]}y"
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    if token.endswith("es") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and len(token) > 4:
        return token[:-1]
    return token


_GROUNDING_WORD = re.compile(r"[\w-]+", flags=re.UNICODE)
_GROUNDING_SCOPE_MARKERS = frozenset(
    """
    all every each entire whole only worldwide globally tout toute tous toutes chaque jamais
    toujours seulement uniquement mondialement partout
    """.split()
)
_DISTINCTIVE_LITERAL = re.compile(
    r"(?<![\w])(?:"
    r"--[A-Za-z0-9][A-Za-z0-9_-]*|"
    r"(?:https?://|www\.)[^\s,;)>\]]+|"
    r"(?:[A-Za-z]:)?[\\/](?:[\w.-]+[\\/])*[\w.-]+|"
    r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+(?:/[^\s,;)>\]]*)?|"
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|"
    r"(?:\d{1,3}\.){3}\d{1,3}|"
    r"[A-Fa-f0-9]{32,64}|"
    r"(?:[A-Fa-f0-9]{0,4}:){2,7}[A-Fa-f0-9]{0,4}|"
    r"[A-Za-z0-9_-]*\d[A-Za-z0-9_-]*"
    r")(?![\w])",
    flags=re.IGNORECASE | re.UNICODE,
)


def _grounding_match_token(value: str) -> str:
    """Normalize a distinctive token for case, accent, and common plural matches."""
    token = unicodedata.normalize("NFKC", value).casefold().replace("\u2019", "'").strip()
    is_digest = re.fullmatch(r"[a-f0-9]{32,64}", token) is not None
    if (
        not is_digest
        and not any(character.isdigit() for character in token)
        and not any(character in token for character in "._/\\")
        and not token.startswith("-")
    ):
        token = _grounding_token(token)
    decomposed = unicodedata.normalize("NFKD", token)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _is_sentence_initial(text: str, start: int) -> bool:
    prefix = text[:start]
    return re.search(r"(?:^|[.!?])\s*[^\w\s]*\s*$", prefix) is not None


def _distinctive_grounding_terms(text: str) -> tuple[str, ...]:
    """Return only claim tokens that can introduce a specific, verifiable fact.

    Numbers, identifiers, technical literals, IOC-like values, CamelCase names,
    acronyms, and non-initial capitalized names are checked against cited
    evidence. Absolute quantifiers and scope modifiers are checked because they
    change the factual reach of a claim. Ordinary editorial prose remains free
    to paraphrase.
    """
    terms: dict[str, str] = {}

    def add(value: str, *, force: bool = False) -> None:
        cleaned = value.strip(".,:;!?()[]{}\"'").strip("\u201c\u201d\u2018\u2019")
        if not cleaned:
            return
        key = _grounding_match_token(cleaned)
        if key and (force or key not in _GROUNDING_STOP_WORDS):
            terms.setdefault(key, cleaned)

    for match in _DISTINCTIVE_LITERAL.finditer(text):
        add(match.group())

    words = list(_GROUNDING_WORD.finditer(text))
    current_run: list[re.Match[str]] = []
    for index, match in enumerate(words):
        value = match.group()
        letters = [character for character in value if character.isalpha()]
        is_camel_case = any(left.islower() and right.isupper() for left, right in pairwise(value))
        is_acronym = len(letters) > 1 and value.isupper()
        is_title_case = bool(letters) and value[0].isupper() and value[1:].islower()

        scope_marker = unicodedata.normalize("NFKC", value).casefold()
        next_word = words[index + 1].group().casefold() if index < len(words) - 1 else ""
        if scope_marker in _GROUNDING_SCOPE_MARKERS and not (
            scope_marker == "tout" and next_word == "en"
        ):
            add(value, force=True)
        if any(character.isdigit() for character in value) or is_camel_case or is_acronym:
            add(value)

        if not is_title_case:
            if len(current_run) > 1:
                add(" ".join(part.group() for part in current_run))
            current_run = []
        else:
            current_run.append(match)
        next_is_title_case = index < len(words) - 1 and (
            words[index + 1].group()[0].isupper() and words[index + 1].group()[1:].islower()
        )
        if current_run and not next_is_title_case:
            if len(current_run) > 1:
                add(" ".join(part.group() for part in current_run))
            current_run = []

        if is_title_case and not _is_sentence_initial(text, match.start()):
            add(value)

    return tuple(terms[key] for key in sorted(terms))


def _unsupported_distinctive_claim_terms(claim_text: str, evidence_text: str) -> tuple[str, ...]:
    """Find claim-specific names and literals absent from its cited evidence."""
    supported = {
        _grounding_match_token(term) for term in _distinctive_grounding_terms(evidence_text)
    }
    unsupported: dict[str, str] = {}
    for term in _distinctive_grounding_terms(claim_text):
        key = _grounding_match_token(term)
        if key not in supported:
            unsupported.setdefault(key, term)
    return tuple(unsupported[key] for key in sorted(unsupported))


def _bounded_claim_grounding_details(details: Mapping[str, Any]) -> dict[str, Any]:
    """Expose only a bounded claim identifier and distinctive unsupported terms."""
    safe: dict[str, Any] = {}
    claim_code = details.get("claim_code")
    if isinstance(claim_code, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,40}", claim_code):
        safe["claim_code"] = claim_code
    unsupported_terms = details.get("unsupported_terms")
    if isinstance(unsupported_terms, (list, tuple)):
        terms = [
            term
            for term in unsupported_terms
            if isinstance(term, str)
            and 0 < len(term) <= 80
            and not any(ord(character) < 32 for character in term)
        ][:12]
        if terms:
            safe["unsupported_terms"] = terms
    return safe


def _normalized_editorial_title_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _editorial_title_group_aliases(group: str) -> tuple[str, ...]:
    raw_aliases = group.split("/")
    aliases = tuple(_normalized_editorial_title_text(alias) for alias in raw_aliases)
    return aliases if aliases and all(aliases) else ()


def _editorial_title_group_is_anchored(group: str, context: Sequence[str]) -> bool:
    aliases = _editorial_title_group_aliases(group)
    if not aliases:
        return False

    normalized_context = _normalized_editorial_title_text(" ".join(context))
    context_tokens = {
        _grounding_token(token)
        for token in re.findall(
            r"[\w-]+",
            unicodedata.normalize("NFKC", " ".join(context)),
            flags=re.UNICODE,
        )
    }
    for alias in aliases:
        significant_tokens = {
            _grounding_token(token)
            for token in re.findall(r"[\w-]+", alias, flags=re.UNICODE)
            if len(token) >= 4 and _grounding_token(token) not in _GROUNDING_STOP_WORDS
        }
        exact_alias_is_anchored = (
            re.search(
                rf"(?<![\w-]){re.escape(alias)}(?![\w-])",
                normalized_context,
                flags=re.UNICODE,
            )
            is not None
        )
        if not exact_alias_is_anchored and (
            not significant_tokens or not significant_tokens.issubset(context_tokens)
        ):
            return False
    return True


def _editorial_title_group_from_title(title: str) -> str | None:
    match = re.match(r"^\[([^\]]+)\] ", title)
    return match.group(1) if match is not None else None


def _publisher_title_aliases(
    snapshot: ProductionInputSnapshot,
    extraction: ProductionExtractionV1,
) -> frozenset[str]:
    publishers: set[str] = set()
    for source in snapshot.core_sources:
        if source.publisher.strip():
            publishers.update(_editorial_title_group_aliases(source.publisher))
    publishers.update(
        _normalized_editorial_title_text(hostname)
        for source in extraction.sources
        if (hostname := (urlsplit(source.canonical_url).hostname or "").removeprefix("www."))
    )
    return frozenset(publishers)


def _group_matches_publisher(group: str, publishers: frozenset[str]) -> bool:
    return any(alias in publishers for alias in _editorial_title_group_aliases(group))


def _has_anchored_actor(
    *,
    base_synthesis: ProductionSynthesisV1,
    extraction: ProductionExtractionV1,
    snapshot: ProductionInputSnapshot,
    context: Sequence[str],
    publishers: frozenset[str],
) -> bool:
    actor_candidates = [snapshot.actor_or_campaign]
    actor_candidates.extend(
        fact.value
        for source in extraction.sources
        for fact in source.facts
        if fact.category in {"actors", "campaigns"}
    )
    actor_candidates.extend(
        group
        for group in (
            _editorial_title_group_from_title(base_synthesis.title),
            _editorial_title_group_from_title(snapshot.subject_title),
        )
        if group is not None
    )
    unestablished_labels = {
        "unknown",
        "unknown actor",
        "unattributed",
        "unattributed actor",
        "inconnu",
        "acteur inconnu",
        "non attribue",
        "non identifie",
        "non attribué",
        "non identifié",
    }
    for candidate in actor_candidates:
        if not candidate.strip():
            continue
        aliases = _editorial_title_group_aliases(candidate)
        actor_aliases = tuple(
            alias
            for alias in aliases
            if alias not in unestablished_labels and alias not in publishers
        )
        if any(_editorial_title_group_is_anchored(alias, context) for alias in actor_aliases):
            return True
    return False


def _editorial_title_context(
    *,
    base_synthesis: ProductionSynthesisV1,
    evidence_pack: SynthesisEvidencePackV1,
    extraction: ProductionExtractionV1,
    snapshot: ProductionInputSnapshot,
) -> tuple[str, ...]:
    context = [base_synthesis.title, snapshot.subject_title, snapshot.actor_or_campaign]
    context.extend(paragraph.text for paragraph in base_synthesis.lead)
    context.extend(
        paragraph.text for section in base_synthesis.sections for paragraph in section.paragraphs
    )
    evidence_records = (
        *evidence_pack.narrative_evidence,
        *evidence_pack.technical_evidence,
        *evidence_pack.reserve_evidence,
    )
    omitted_fields = {"handle", "source_label", "source_role", "editorial_role"}
    context.extend(
        value
        for record in evidence_records
        for key, value in record.items()
        if key not in omitted_fields and isinstance(value, str)
    )
    context.extend(source.publisher for source in snapshot.core_sources if source.publisher.strip())
    for source in extraction.sources:
        hostname = (urlsplit(source.canonical_url).hostname or "").removeprefix("www.")
        if hostname:
            context.append(hostname)
        for fact in source.facts:
            if fact.category in {"actors", "campaigns"}:
                context.extend((fact.value, fact.context, fact.evidence_quote))
    return tuple(context)


def _assert_claim_handles_and_relations_supported(
    proposal: SynthesisProposalV1,
    evidence_pack: SynthesisEvidencePackV1,
    *,
    check_lead: bool = True,
    extraction: ProductionExtractionV1 | None = None,
    source_addition_document_ids: frozenset[UUID] = frozenset(),
    require_claim_scopes: bool = False,
) -> None:
    records = {
        str(record["handle"]): record
        for record in (
            *evidence_pack.narrative_evidence,
            *evidence_pack.technical_evidence,
            *evidence_pack.reserve_evidence,
        )
    }
    claims = [
        *(proposal.lead if check_lead else ()),
        *(claim for section in proposal.sections for claim in section.claims),
    ]
    source_by_id = (
        {source.source_document_id: source for source in extraction.sources}
        if extraction is not None
        else {}
    )
    for claim_index, claim in enumerate(claims, start=1):
        if any(handle not in evidence_pack._handle_to_ref for handle in claim.evidence_handles):
            raise TargetedSynthesisRevisionValidationError("synthesis_unknown_evidence")
        if require_claim_scopes:
            try:
                claim_scope = EvidenceUseScope(claim.claim_scope or "")
            except ValueError as exc:
                raise TargetedSynthesisRevisionValidationError(
                    "revision_claim_scope_missing_or_invalid"
                ) from exc
            referenced_ids = {
                evidence_pack.resolve_handle(handle).source_document_id
                for handle in claim.evidence_handles
            }
            context_ids = {
                source_id
                for source_id in referenced_ids
                if source_id in source_addition_document_ids
                and source_by_id.get(source_id) is not None
                and source_by_id[source_id].editorial_role is ProductionEditorialRole.CONTEXT
            }
            subject_scoped_ids = {
                evidence_pack.resolve_handle(handle).source_document_id
                for handle in claim.evidence_handles
                if handle in evidence_pack._subject_scoped_handles
                and evidence_pack.resolve_handle(handle).source_document_id
                not in source_addition_document_ids
            }
            if claim_scope is EvidenceUseScope.TOOL_CONTEXT and (
                not context_ids or referenced_ids != context_ids
            ):
                raise TargetedSynthesisRevisionValidationError(
                    "revision_tool_context_scope_mismatch"
                )
            if claim_scope is EvidenceUseScope.ACTOR_ACTIVITY:
                if context_ids:
                    raise TargetedSynthesisRevisionValidationError(
                        "revision_tool_context_cannot_support_actor_activity"
                    )
                if not subject_scoped_ids:
                    raise TargetedSynthesisRevisionValidationError(
                        "revision_actor_activity_requires_subject_evidence"
                    )
        evidence_text = " ".join(
            str(value)
            for handle in claim.evidence_handles
            for key, value in records[handle].items()
            if key not in {"handle", "source_label", "source_role", "editorial_role"}
            and isinstance(value, str)
        )
        unsupported = _unsupported_distinctive_claim_terms(claim.text, evidence_text)
        if unsupported:
            claim_code = claim._block_id or f"C{claim_index:03d}"
            raise TargetedSynthesisRevisionValidationError(
                "synthesis_relation_not_evidenced",
                details={
                    "claim_code": claim_code[:40],
                    "unsupported_terms": list(unsupported[:12]),
                },
            )


def _unchanged_lead_wire(
    synthesis: ProductionSynthesisV1, evidence_pack: SynthesisEvidencePackV1
) -> str:
    paragraph = next(
        (
            item
            for item in synthesis.lead
            if all(evidence_pack.handle_for(ref) for ref in item.evidence_refs)
        ),
        None,
    )
    if paragraph is None:
        raise TargetedSynthesisRevisionValidationError(
            "revision_cannot_validate_patch_without_admitted_lead"
        )
    handles = [evidence_pack.handle_for(ref) for ref in paragraph.evidence_refs]
    return (
        "@@LEAD@@\n@@CLAIM PRESERVED_LEAD@@\n"
        f"EVIDENCE: {', '.join(item for item in handles if item is not None)}\n"
        f"TEXT: {paragraph.text}\n"
    )


def _require_empty_after(raw: str) -> None:
    normalized = raw.strip().casefold()
    if normalized not in {"empty", "@@empty@@"}:
        raise TargetedSynthesisRevisionValidationError("revision_delete_requires_empty_marker")


def _require_one_claim(
    proposal: SynthesisProposalV1,
    lead: tuple[SynthesisParagraphV1, ...],
    sections: tuple[SynthesisSectionV1, ...],
    *,
    in_section: bool,
    section_kind: SynthesisSectionKind | None = None,
) -> SynthesisParagraphV1:
    if in_section:
        if lead or len(sections) != 1 or len(sections[0].paragraphs) != 1:
            raise TargetedSynthesisRevisionValidationError("revision_requires_one_section_claim")
        section = sections[0]
        if section.kind is not section_kind or section.heading:
            raise TargetedSynthesisRevisionValidationError("revision_paragraph_scope_changed")
        return section.paragraphs[0]
    if len(lead) != 1 or sections:
        raise TargetedSynthesisRevisionValidationError("revision_requires_one_lead_claim")
    return lead[0]


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


def _apply_wire_patch(
    *,
    base_synthesis: ProductionSynthesisV1,
    base_payload: dict[str, Any],
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
    patch: _WirePatch,
    evidence_pack: SynthesisEvidencePackV1,
    extraction: ProductionExtractionV1,
    snapshot: ProductionInputSnapshot,
    source_addition_document_ids: frozenset[UUID] = frozenset(),
    require_claim_scopes: bool = False,
) -> tuple[ProductionSynthesisV1, dict[str, Any], str]:
    resolved = _resolve_target(base_payload, target, operation)
    if patch.before_hash != resolved.before_hash:
        raise TargetedEditorialRevisionConflictError("The targeted element changed")
    if patch.target != resolved.path or patch.action is not operation:
        raise TargetedSynthesisRevisionValidationError("revision_patch_target_action_mismatch")

    title = base_synthesis.title
    lead = list(base_synthesis.lead)
    sections = list(base_synthesis.sections)
    proposal: SynthesisProposalV1 | None = None
    parsed_lead: tuple[SynthesisParagraphV1, ...] = ()
    parsed_sections: tuple[SynthesisSectionV1, ...] = ()
    raw_after = patch.after

    if operation is TargetedEditorialRevisionOperation.REWRITE_TITLE:
        title = _extract_after_text(raw_after, "TITLE")
        if not is_valid_editorial_title(title):
            raise TargetedSynthesisRevisionValidationError("synthesis_title_invalid")
        group = _editorial_title_group_from_title(title)
        assert group is not None
        title_context = _editorial_title_context(
            base_synthesis=base_synthesis,
            evidence_pack=evidence_pack,
            extraction=extraction,
            snapshot=snapshot,
        )
        publishers = _publisher_title_aliases(snapshot, extraction)
        if _group_matches_publisher(group, publishers) and _has_anchored_actor(
            base_synthesis=base_synthesis,
            extraction=extraction,
            snapshot=snapshot,
            context=title_context,
            publishers=publishers,
        ):
            raise TargetedSynthesisRevisionValidationError("synthesis_title_publisher_as_actor")
        if not _editorial_title_group_is_anchored(group, title_context):
            raise TargetedSynthesisRevisionValidationError("synthesis_title_actor_ungrounded")
    elif operation is TargetedEditorialRevisionOperation.RENAME_SECTION:
        heading = _extract_after_text(raw_after, "HEADING")
        if not heading.strip():
            raise TargetedSynthesisRevisionValidationError("revision_after_heading_missing")
        section_index = resolved.indexes[0]
        current = sections[section_index]
        try:
            validate_synthesis_section_headings(
                ((heading, (paragraph.text for paragraph in current.paragraphs)),)
            )
        except ValueError as exc:
            raise TargetedSynthesisRevisionValidationError(
                "synthesis_section_heading_invalid"
            ) from exc
        sections[section_index] = replace(current, heading=heading)
    elif operation in {
        TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
        TargetedEditorialRevisionOperation.DELETE_SECTION,
    }:
        _require_empty_after(raw_after)
        if operation is TargetedEditorialRevisionOperation.DELETE_PARAGRAPH:
            if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
                lead.pop(resolved.indexes[0])
            else:
                parent_index = resolved.parent_index
                assert parent_index is not None
                paragraphs = list(sections[parent_index].paragraphs)
                paragraphs.pop(resolved.indexes[0])
                if not paragraphs:
                    raise TargetedSynthesisRevisionValidationError(
                        "revision_cannot_leave_empty_section"
                    )
                sections[parent_index] = replace(
                    sections[parent_index], paragraphs=tuple(paragraphs)
                )
        else:
            sections.pop(resolved.indexes[0])
    else:
        proposal, parsed_lead, parsed_sections = _parse_after_claims(
            raw_after,
            evidence_pack=evidence_pack,
            extraction=extraction,
            unchanged_lead_wire=_unchanged_lead_wire(base_synthesis, evidence_pack),
            source_addition_document_ids=source_addition_document_ids,
            require_claim_scopes=require_claim_scopes,
        )
        if operation is TargetedEditorialRevisionOperation.REWRITE_LEAD:
            if not parsed_lead or parsed_sections:
                raise TargetedSynthesisRevisionValidationError(
                    "revision_rewrite_lead_scope_invalid"
                )
            lead = list(parsed_lead)
        elif operation in {
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
            TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
        }:
            in_section = target.kind in {
                TargetedEditorialRevisionTargetKind.SECTION,
                TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH,
            }
            current_kind = None
            if target.kind in {
                TargetedEditorialRevisionTargetKind.SECTION,
                TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH,
            }:
                parent_index = resolved.parent_index
                assert parent_index is not None
                current_kind = sections[parent_index].kind
            paragraph = _require_one_claim(
                proposal,
                parsed_lead,
                parsed_sections,
                in_section=in_section,
                section_kind=current_kind,
            )
            if operation is TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH:
                if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
                    lead[resolved.indexes[0]] = paragraph
                else:
                    assert resolved.parent_index is not None
                    paragraphs = list(sections[resolved.parent_index].paragraphs)
                    paragraphs[resolved.indexes[0]] = paragraph
                    sections[resolved.parent_index] = replace(
                        sections[resolved.parent_index], paragraphs=tuple(paragraphs)
                    )
            elif target.kind is TargetedEditorialRevisionTargetKind.LEAD:
                index = resolved.anchor_index
                if index is not None and target.insert_after:
                    index += 1
                lead.insert(len(lead) if index is None else index, paragraph)
            else:
                assert resolved.parent_index is not None
                paragraphs = list(sections[resolved.parent_index].paragraphs)
                index = resolved.anchor_index
                if index is not None and target.insert_after:
                    index += 1
                paragraphs.insert(len(paragraphs) if index is None else index, paragraph)
                sections[resolved.parent_index] = replace(
                    sections[resolved.parent_index], paragraphs=tuple(paragraphs)
                )
        elif operation in {
            TargetedEditorialRevisionOperation.REWRITE_SECTION,
            TargetedEditorialRevisionOperation.INSERT_SECTION,
            TargetedEditorialRevisionOperation.MERGE_SECTIONS,
        }:
            if parsed_lead or len(parsed_sections) != 1:
                raise TargetedSynthesisRevisionValidationError("revision_requires_one_section")
            candidate = parsed_sections[0]
            if operation is TargetedEditorialRevisionOperation.REWRITE_SECTION:
                old = sections[resolved.indexes[0]]
                if candidate.kind is not old.kind:
                    raise TargetedSynthesisRevisionValidationError("revision_section_kind_changed")
                sections[resolved.indexes[0]] = candidate
            elif operation is TargetedEditorialRevisionOperation.INSERT_SECTION:
                index = resolved.anchor_index
                if index is not None and target.insert_after:
                    index += 1
                sections.insert(len(sections) if index is None else index, candidate)
            else:
                first, second = resolved.indexes
                if (
                    candidate.kind is not sections[first].kind
                    or candidate.kind is not sections[second].kind
                ):
                    raise TargetedSynthesisRevisionValidationError("revision_section_kind_changed")
                sections[first : second + 1] = [candidate]
        elif operation is TargetedEditorialRevisionOperation.SPLIT_SECTION:
            old = sections[resolved.indexes[0]]
            if (
                parsed_lead
                or len(parsed_sections) != 2
                or any(item.kind is not old.kind for item in parsed_sections)
            ):
                raise TargetedSynthesisRevisionValidationError(
                    "revision_split_requires_two_sections"
                )
            sections[resolved.indexes[0] : resolved.indexes[0] + 1] = list(parsed_sections)
        else:
            raise TargetedSynthesisRevisionValidationError("revision_operation_not_supported")

    revised = replace(base_synthesis, title=title, lead=tuple(lead), sections=tuple(sections))
    if not is_production_extraction_hash(extraction, base_synthesis.extraction_hash):
        raise TargetedSynthesisRevisionValidationError("synthesis_extraction_lineage_invalid")
    try:
        validate_synthesis_lineage(revised, snapshot, base_synthesis.extraction_hash)
    except (ValueError, AttributeError) as exc:
        raise TargetedSynthesisRevisionValidationError("synthesis_lineage_invalid") from exc
    revised_payload = production_synthesis_to_json(revised)
    _assert_non_target_unchanged(base_payload, revised_payload, target, operation, resolved)
    if _canonical_bytes(base_payload) == _canonical_bytes(revised_payload):
        raise TargetedSynthesisRevisionValidationError("revision_no_change")
    return revised, revised_payload, resolved.before_hash


class ProductionSynthesisRevisionService:
    """Apply one evidence-bounded patch to the current canonical synthesis."""

    def __init__(
        self,
        *,
        synthesis_service: ProductionSynthesisService,
        synthesis_persistence: SynthesisService,
        enrichment_persistence: EditorialEnrichmentService,
        publication_render_service: PublicationRenderService,
        enrichment_revision_service: Any | None = None,
        source_addition_service: SourceAdditionService | None = None,
        resource_search_enabled: bool = False,
    ) -> None:
        self._synthesis = synthesis_service
        self._synthesis_persistence = synthesis_persistence
        self._enrichment_persistence = enrichment_persistence
        self._publication_render_service = publication_render_service
        self._enrichment_revision_service = enrichment_revision_service
        self._source_addition_service = source_addition_service
        self._resource_search_enabled = resource_search_enabled

    async def preview(
        self, request: TargetedEditorialRevisionRequest
    ) -> TargetedSynthesisRevisionPreview:
        self._validate_request(request)
        if request.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            if self._enrichment_revision_service is None:
                raise TargetedSynthesisRevisionValidationError(
                    "targeted_enrichment_revision_unavailable"
                )
            return cast(
                TargetedSynthesisRevisionPreview,
                await self._enrichment_revision_service.preview_targeted(request),
            )
        context = await self._load_context(request)
        resolved = _resolve_target(context.base_payload, request.target, request.operation)
        if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
            addition_preview = None
            preview_refusal = None
            if self._source_addition_service is None or request.source_addition_request_id is None:
                preview_refusal = TargetedEditorialRevisionRefusal(
                    code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason="A source addition request is required before extension preview.",
                )
            else:
                addition_preview = await self._source_addition_service.preview(
                    production_run_id=context.run.id,
                    request_id=request.source_addition_request_id,
                )
                if request.before_hash is not None and request.before_hash != resolved.before_hash:
                    preview_refusal = TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                        recommended_restart_stage=ProductionStage.SYNTHESIS,
                        reason="The target changed after the preview fences were captured.",
                    )
                elif (
                    request.source_additions_hash is not None
                    and request.source_additions_hash != addition_preview.source_additions_hash
                ):
                    preview_refusal = TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason="The source addition manifest changed after preview.",
                    )
            calls_by_stage = (
                {key: dict(value) for key, value in addition_preview.model_calls.items()}
                if addition_preview is not None
                else {}
            )
            calls = (
                int(calls_by_stage.get("addition_extraction", {}).get("remaining", 0))
                if addition_preview is not None
                else 0
            )
            if addition_preview is not None and addition_preview.ready and not preview_refusal:
                effective_request = replace(
                    request,
                    source_additions_hash=addition_preview.source_additions_hash,
                    before_hash=resolved.before_hash,
                )
                identity = _sha256(self._request_identity_payload(effective_request))
                model_run_id = uuid5(NAMESPACE_URL, f"targeted-synthesis-model-run:{identity}")
                existing_run = await self._synthesis._model_gateway.get_run(model_run_id)
                revision_calls = 0 if existing_run is not None else 1
                calls_by_stage["synthesis_revision"] = {
                    "made": 0,
                    "remaining": revision_calls,
                }
                calls += revision_calls
            else:
                calls_by_stage["synthesis_revision"] = {"made": 0, "remaining": 0}
            if addition_preview is not None and addition_preview.blockers:
                preview_refusal = TargetedEditorialRevisionRefusal(
                    code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason=(
                        "Une source critique est bloquée; la promotion attend sa réparation "
                        "ou une décision D effective."
                    ),
                )
            elif (
                addition_preview is not None and not addition_preview.ready and not preview_refusal
            ):
                preview_refusal = TargetedEditorialRevisionRefusal(
                    code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason=(
                        "Aucune addition READY n'est disponible; terminez l'archivage "
                        "et l'extraction."
                    ),
                )
            return TargetedSynthesisRevisionPreview(
                base_artifact_id=context.base_artifact.id,
                base_version=context.base_artifact.version,
                base_input_hash=context.base_artifact.input_hash,
                base_canonical_sha256=request.base_canonical_sha256,
                target_path=resolved.path,
                stages_preserved=(
                    "production_input_snapshot",
                    "old_source_checkpoints",
                    "historical_references_extraction_projection",
                ),
                stages_invalidated=(ProductionArtifactStage.PUBLICATION.value,),
                stages_conditionally_rebased=(ProductionArtifactStage.EDITORIAL_ENRICHMENT.value,),
                source_extension=True,
                expected_model_calls=calls,
                enrichment_rebase=(
                    "revalidate_anchors_and_evidence; adopt if valid, otherwise stale"
                ),
                before_hash=resolved.before_hash,
                source_additions=(
                    addition_preview.source_additions if addition_preview is not None else ()
                ),
                source_additions_hash=(
                    addition_preview.source_additions_hash if addition_preview is not None else None
                ),
                source_addition_ready=(
                    addition_preview.ready if addition_preview is not None else False
                ),
                source_addition_blockers=(
                    tuple(
                        {
                            "source_url": blocker.source_url,
                            "repair_key": blocker.repair_key,
                            "criticality": blocker.criticality.value,
                            "criticality_reason": blocker.criticality_reason.value,
                            "failure_code": blocker.failure_code,
                            "reason": blocker.reason,
                        }
                        for blocker in addition_preview.blockers
                    )
                    if addition_preview is not None
                    else ()
                ),
                expected_model_calls_by_stage=calls_by_stage,
                stages_rebased=(
                    ProductionArtifactStage.REFERENCES.value,
                    ProductionArtifactStage.EXTRACTION.value,
                    ProductionArtifactStage.RELEVANCE_PROJECTION.value,
                )
                if addition_preview is not None and addition_preview.projected_artifacts
                else (),
                historical_artifacts_preserved=True,
                refusal=preview_refusal,
            )
        else:
            extraction, _ = await self._synthesis._load_extraction(
                context.run, context.snapshot, context.extraction_artifact
            )
            if await self._critical_extraction_failures(context, extraction):
                calls = 0
            else:
                access_policy = await self._synthesis._load_access_policy(
                    context.snapshot, extraction
                )
                if access_policy.do_not_submit or not access_policy.external_llm_allowed:
                    calls = 0
                else:
                    identity = _sha256(self._request_identity_payload(request))
                    model_run_id = uuid5(NAMESPACE_URL, f"targeted-synthesis-model-run:{identity}")
                    existing_run = await self._synthesis._model_gateway.get_run(model_run_id)
                    calls = 0 if existing_run is not None else 1
        return TargetedSynthesisRevisionPreview(
            base_artifact_id=context.base_artifact.id,
            base_version=context.base_artifact.version,
            base_input_hash=context.base_artifact.input_hash,
            base_canonical_sha256=request.base_canonical_sha256,
            target_path=resolved.path,
            stages_preserved=(
                ProductionArtifactStage.REFERENCES.value,
                ProductionArtifactStage.EXTRACTION.value,
                ProductionArtifactStage.RELEVANCE_PROJECTION.value,
            ),
            stages_invalidated=(ProductionArtifactStage.PUBLICATION.value,),
            stages_conditionally_rebased=(ProductionArtifactStage.EDITORIAL_ENRICHMENT.value,),
            source_extension=False,
            expected_model_calls=calls,
            enrichment_rebase="revalidate_anchors_and_evidence; adopt if valid, otherwise stale",
            before_hash=resolved.before_hash,
        )

    async def _critical_extraction_failures(
        self, context: _RevisionContext, extraction: ProductionExtractionV1
    ) -> tuple[Any, ...]:
        references_blob_id = context.references_artifact.canonical_blob_id
        if references_blob_id is None:
            raise TargetedSynthesisRevisionValidationError("references_artifact_blob_missing")
        references_payload = await self._synthesis._artifact_store.read_json(references_blob_id)
        reference_corpus = production_reference_corpus_from_json(references_payload)
        return _critical_extraction_failures(extraction, reference_corpus)

    async def revise(
        self, request: TargetedEditorialRevisionRequest
    ) -> TargetedSynthesisRevisionResult:
        self._validate_request(request)
        if request.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            if self._enrichment_revision_service is None:
                raise TargetedSynthesisRevisionValidationError(
                    "targeted_enrichment_revision_unavailable"
                )
            return cast(
                TargetedSynthesisRevisionResult,
                await self._enrichment_revision_service.revise_targeted(request),
            )
        identity_payload = self._request_identity_payload(request)
        request_identity = _sha256(identity_payload)
        revision_id = uuid5(NAMESPACE_URL, f"targeted-synthesis-revision:{request_identity}")
        prior = await self._existing_result(request.subject_id, revision_id)
        if prior is not None:
            return prior

        promotion: SourceAdditionPromotionResult | None = None
        if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION and (
            self._source_addition_service is None
            or request.source_addition_request_id is None
            or request.source_additions_hash is None
            or request.before_hash is None
        ):
            return TargetedSynthesisRevisionResult(
                artifact=None,
                publication_artifact=None,
                revision=None,
                refusal=TargetedEditorialRevisionRefusal(
                    code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                    recommended_restart_stage=ProductionStage.REFERENCES,
                    reason=(
                        "Source extension requires a READY archived and extracted addition "
                        "with matching preview fences."
                    ),
                ),
            )

        async with self._synthesis._uow_factory() as uow:
            if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
                assert self._source_addition_service is not None
                assert request.source_addition_request_id is not None
                current_run = await uow.production_runs.get_current_for_subject(request.subject_id)
                if current_run is None:
                    raise TargetedEditorialRevisionConflictError(
                        "The requested production run is no longer current"
                    )
                try:
                    promotion = await self._source_addition_service.promote(
                        uow,
                        production_run_id=current_run.id,
                        request_id=request.source_addition_request_id,
                    )
                except SourceAdditionPromotionBlockedError as exc:
                    blocked = "; ".join(
                        f"{item.source_url}: {item.reason}" for item in exc.blockers
                    )
                    return TargetedSynthesisRevisionResult(
                        artifact=None,
                        publication_artifact=None,
                        revision=None,
                        refusal=TargetedEditorialRevisionRefusal(
                            code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                            recommended_restart_stage=ProductionStage.REFERENCES,
                            reason=(
                                "Une source critique du corpus reste bloquée; réparez-la ou "
                                "enregistrez une décision D effective pour cette génération. "
                                f"{blocked}"
                            ),
                        ),
                    )
                except SourceAdditionConflictError:
                    raise
                except SourceAdditionError as exc:
                    return TargetedSynthesisRevisionResult(
                        artifact=None,
                        publication_artifact=None,
                        revision=None,
                        refusal=TargetedEditorialRevisionRefusal(
                            code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                            recommended_restart_stage=ProductionStage.REFERENCES,
                            reason=(
                                "Aucune addition READY n'est disponible pour ce manifeste; "
                                f"reprenez l'acquisition et l'extraction ({exc})."
                            ),
                        ),
                    )
            try:
                result = await self.revise_in_uow(
                    uow,
                    request,
                    source_addition_promotion=promotion,
                )
            except TargetedSynthesisRevisionValidationError as exc:
                deterministic_refusals = {
                    "revision_claim_scope_missing_or_invalid",
                    "revision_tool_context_scope_mismatch",
                    "revision_tool_context_cannot_support_actor_activity",
                    "revision_actor_activity_requires_subject_evidence",
                    "source_extension_added_evidence_not_used",
                    "synthesis_unknown_evidence",
                    "synthesis_relation_not_evidenced",
                    "revision_after_claims_invalid",
                    "revision_patch_changed_outside_target",
                    "revision_patch_target_action_mismatch",
                    "synthesis_extraction_lineage_invalid",
                    "synthesis_lineage_invalid",
                    "synthesis_section_heading_invalid",
                    "revision_requires_one_section",
                    "revision_requires_one_section_claim",
                }
                if (
                    request.mode is not TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
                    or exc.reason_code not in deterministic_refusals
                ):
                    raise
                restart_stage = (
                    ProductionStage.EXTRACTION
                    if "extraction" in exc.reason_code or "lineage" in exc.reason_code
                    else ProductionStage.SYNTHESIS
                )
                refusal_details = _bounded_claim_grounding_details(exc.details)
                return TargetedSynthesisRevisionResult(
                    artifact=None,
                    publication_artifact=None,
                    revision=None,
                    refusal=TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                        recommended_restart_stage=restart_stage,
                        reason=(
                            "Le patch de révision n'est pas étayé par les preuves autorisées. "
                            f"Détail: {exc.reason_code}."
                        ),
                        details=refusal_details,
                    ),
                )
            if result.refusal is None:
                await uow.commit()

        if result.publication_artifact is not None:
            await self._publication_render_service.render_preview(result.publication_artifact.id)
        return result

    async def revise_in_uow(
        self,
        uow: Any,
        request: TargetedEditorialRevisionRequest,
        *,
        source_addition_promotion: SourceAdditionPromotionResult | None = None,
    ) -> TargetedSynthesisRevisionResult:
        try:
            result = await self._revise_in_uow_impl(
                uow,
                request,
                source_addition_promotion=source_addition_promotion,
            )
        except BaseException:
            rollback = getattr(uow, "rollback", None)
            if source_addition_promotion is not None and callable(rollback):
                await rollback()
            raise
        if result.refusal is not None and source_addition_promotion is not None:
            rollback = getattr(uow, "rollback", None)
            if callable(rollback):
                await rollback()
        return result

    async def _revise_in_uow_impl(
        self,
        uow: Any,
        request: TargetedEditorialRevisionRequest,
        *,
        source_addition_promotion: SourceAdditionPromotionResult | None = None,
    ) -> TargetedSynthesisRevisionResult:
        """Revise within a caller-owned transaction, optionally after promotion."""
        self._validate_request(request)
        if request.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            raise TargetedSynthesisRevisionValidationError(
                "targeted_enrichment_revision_requires_its_own_unit_of_work"
            )
        if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
            if source_addition_promotion is None:
                return TargetedSynthesisRevisionResult(
                    artifact=None,
                    publication_artifact=None,
                    revision=None,
                    refusal=TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason="A source addition must be promoted in this transaction first.",
                    ),
                )
            if source_addition_promotion.source_additions_hash != request.source_additions_hash:
                return TargetedSynthesisRevisionResult(
                    artifact=None,
                    publication_artifact=None,
                    revision=None,
                    refusal=TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason="The ready source addition manifest changed after preview.",
                    ),
                )

        identity_payload = self._request_identity_payload(request)
        request_identity = _sha256(identity_payload)
        revision_id = uuid5(NAMESPACE_URL, f"targeted-synthesis-revision:{request_identity}")
        existing_artifact = await uow.production_artifacts.get(revision_id)
        if existing_artifact is not None:
            if (
                existing_artifact.subject_id != request.subject_id
                or existing_artifact.stage is not ProductionArtifactStage.SYNTHESIS
            ):
                raise TargetedEditorialRevisionConflictError(
                    "The deterministic revision identity belongs to another artifact"
                )
            revision = existing_artifact.metadata.get("revision")
            if not isinstance(revision, dict):
                raise ValueError("targeted_synthesis_revision_result_corrupt")
            publication = next(
                (
                    item
                    for item in await uow.production_artifacts.list_for_run(
                        existing_artifact.production_run_id
                    )
                    if item.stage is ProductionArtifactStage.PUBLICATION
                    and isinstance(item.metadata.get("input_artifacts"), dict)
                    and item.metadata["input_artifacts"].get("synthesis_artifact_id")
                    == str(existing_artifact.id)
                ),
                None,
            )
            return TargetedSynthesisRevisionResult(
                artifact=existing_artifact,
                publication_artifact=publication,
                revision=revision,
            )

        context = await self._load_context(request, uow=uow)
        resolved = _resolve_target(context.base_payload, request.target, request.operation)
        if request.before_hash is not None and request.before_hash != resolved.before_hash:
            raise TargetedEditorialRevisionConflictError("The targeted element changed")
        base_blob_id = context.base_artifact.canonical_blob_id
        if base_blob_id is None:
            raise TargetedSynthesisRevisionValidationError("synthesis_artifact_blob_missing")
        base_bytes = await self._synthesis._artifact_store.read_bytes(base_blob_id)
        base_hash = hashlib.sha256(base_bytes).hexdigest()
        if base_hash != request.base_canonical_sha256:
            raise TargetedEditorialRevisionConflictError(
                "The synthesis revision base content hash changed"
            )
        base_synthesis = production_synthesis_from_json(context.base_payload)
        source_addition_document_ids: frozenset[UUID] = frozenset()
        source_additions: tuple[Mapping[str, Any], ...] = ()
        base_projection_hash: str | None = None
        if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
            promotion = source_addition_promotion
            if promotion is None or promotion.promotion_plan is None:
                return TargetedSynthesisRevisionResult(
                    artifact=None,
                    publication_artifact=None,
                    revision=None,
                    refusal=TargetedEditorialRevisionRefusal(
                        code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                        recommended_restart_stage=ProductionStage.REFERENCES,
                        reason="The source addition promotion has no verifiable base plan.",
                    ),
                )
            plan = promotion.promotion_plan
            if (
                plan.production_run_id != context.run.id
                or plan.subject_id != context.run.subject_id
                or plan.pipeline_generation != context.run.pipeline_generation
                or promotion.request_id != request.source_addition_request_id
                or promotion.source_additions_hash != request.source_additions_hash
                or len(promotion.artifacts) != 3
            ):
                raise TargetedEditorialRevisionConflictError(
                    "The source addition promotion does not match the requested run or manifest"
                )
            fences_by_stage = {item.stage: item for item in plan.base_artifacts}
            if set(fences_by_stage) != {
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.RELEVANCE_PROJECTION,
            }:
                raise TargetedEditorialRevisionConflictError(
                    "The source addition base fences are incomplete"
                )
            for stage, fence in fences_by_stage.items():
                fenced_artifact = await uow.production_artifacts.get(fence.artifact_id)
                if (
                    fenced_artifact is None
                    or fenced_artifact.production_run_id != context.run.id
                    or fenced_artifact.stage is not stage
                    or fenced_artifact.version != fence.version
                    or fenced_artifact.input_hash != fence.input_hash
                    or fenced_artifact.status is not ProductionArtifactStatus.VERIFIED
                    or fenced_artifact.canonical_blob_id is None
                ):
                    raise TargetedEditorialRevisionConflictError(
                        "A source addition base artifact changed"
                    )
                fenced_bytes = await self._synthesis._artifact_store.read_bytes(
                    fenced_artifact.canonical_blob_id
                )
                if hashlib.sha256(fenced_bytes).hexdigest() != fence.canonical_sha256:
                    raise TargetedEditorialRevisionConflictError(
                        "A source addition base artifact hash changed"
                    )
                if stage is ProductionArtifactStage.EXTRACTION:
                    base_extraction_payload = json.loads(fenced_bytes)
                    if not isinstance(base_extraction_payload, dict):
                        raise TargetedSynthesisRevisionValidationError(
                            "source_addition_base_extraction_invalid"
                        )
                    base_extraction = production_extraction_from_json(base_extraction_payload)
                    if not is_production_extraction_hash(
                        base_extraction, base_synthesis.extraction_hash
                    ):
                        raise TargetedEditorialRevisionConflictError(
                            "The synthesis base does not reference its fenced extraction"
                        )
                elif stage is ProductionArtifactStage.RELEVANCE_PROJECTION:
                    projection_payload = json.loads(fenced_bytes)
                    base_projection_hash = (
                        str(projection_payload.get("projection_hash"))
                        if isinstance(projection_payload, dict)
                        else None
                    )
            if (
                context.base_artifact.metadata.get("relevance_projection_hash")
                != base_projection_hash
            ):
                raise TargetedEditorialRevisionConflictError(
                    "The synthesis base does not reference its fenced relevance projection"
                )
            promoted_by_stage = {item.stage: item for item in promotion.artifacts}
            current_by_stage = {
                ProductionArtifactStage.REFERENCES: context.references_artifact,
                ProductionArtifactStage.EXTRACTION: context.extraction_artifact,
                ProductionArtifactStage.RELEVANCE_PROJECTION: context.projection_artifact,
            }
            projected_by_stage = {item.stage: item for item in plan.projected_artifacts}
            for stage, current_artifact in current_by_stage.items():
                promoted_artifact = promoted_by_stage.get(stage)
                projected = projected_by_stage.get(stage)
                if (
                    current_artifact is None
                    or promoted_artifact is None
                    or projected is None
                    or current_artifact.id != promoted_artifact.id
                    or current_artifact.id != projected.artifact_id
                    or current_artifact.version != projected.version
                    or current_artifact.input_hash != projected.input_hash
                    or current_artifact.canonical_blob_id is None
                ):
                    raise TargetedEditorialRevisionConflictError(
                        "The promoted source addition artifacts are no longer current"
                    )
                current_bytes = await self._synthesis._artifact_store.read_bytes(
                    current_artifact.canonical_blob_id
                )
                if hashlib.sha256(current_bytes).hexdigest() != projected.canonical_sha256:
                    raise TargetedEditorialRevisionConflictError(
                        "A promoted source addition artifact hash changed"
                    )
                if current_artifact.metadata.get("source_additions_hash") != (
                    promotion.source_additions_hash
                ):
                    raise TargetedEditorialRevisionConflictError(
                        "A promoted artifact has a different source addition manifest"
                    )
            raw_manifest = context.references_artifact.metadata.get("source_additions")
            if (
                not isinstance(raw_manifest, list)
                or context.references_artifact.metadata.get("source_additions_hash")
                != promotion.source_additions_hash
            ):
                raise TargetedEditorialRevisionConflictError(
                    "The promoted reference manifest is missing or inconsistent"
                )
            source_additions = tuple(
                dict(item) for item in raw_manifest if isinstance(item, Mapping)
            )
            try:
                source_addition_document_ids = frozenset(
                    UUID(str(item["source_document_id"])) for item in source_additions
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise TargetedSynthesisRevisionValidationError(
                    "source_addition_manifest_invalid"
                ) from exc
            if not source_addition_document_ids or len(source_additions) != len(raw_manifest):
                raise TargetedSynthesisRevisionValidationError("source_addition_manifest_invalid")
        extraction, extraction_hash = await self._synthesis._load_extraction(
            context.run, context.snapshot, context.extraction_artifact
        )
        if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
            base_synthesis_for_apply = replace(
                base_synthesis, extraction_hash=canonical_extraction_hash(extraction)
            )
            base_payload_for_apply = production_synthesis_to_json(base_synthesis_for_apply)
        else:
            base_synthesis_for_apply = base_synthesis
            base_payload_for_apply = context.base_payload
            if not is_production_extraction_hash(extraction, base_synthesis.extraction_hash):
                raise TargetedEditorialRevisionConflictError(
                    "The synthesis base does not reference its current extraction artifact"
                )
        if (
            request.mode is not TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
            and await self._critical_extraction_failures(context, extraction)
        ):
            return TargetedSynthesisRevisionResult(
                artifact=None,
                publication_artifact=None,
                revision=None,
                refusal=TargetedEditorialRevisionRefusal(
                    code=TargetedEditorialRevisionRefusalCode.REVISION_NOT_APPLICABLE,
                    recommended_restart_stage=ProductionStage.EXTRACTION,
                    reason=(
                        "A critical source has a source_extraction_failed issue; repair the "
                        "source evidence before revising the synthesis."
                    ),
                ),
            )
        projection = (
            await self._synthesis._load_projection(
                context.run,
                context.snapshot,
                extraction,
                extraction_hash,
                context.projection_artifact,
            )
            if context.projection_artifact is not None
            else None
        )
        access_policy = await self._synthesis._load_access_policy(context.snapshot, extraction)
        policy_hash = synthesis_access_policy_hash(access_policy)
        evidence_pack = build_synthesis_evidence_pack(context.snapshot, extraction, projection)
        evidence_pack_hash = synthesis_evidence_pack_hash(evidence_pack)
        revision_input_hash = (
            synthesis_input_hash(context.snapshot, extraction, evidence_pack, policy_hash)
            if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
            else context.base_artifact.input_hash
        )
        expected_base_projection_hash = (
            base_projection_hash
            if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
            else (projection.projection_hash if projection is not None else None)
        )
        if (
            base_synthesis.production_input_hash != context.snapshot.input_hash
            or context.base_artifact.metadata.get("relevance_projection_hash")
            != expected_base_projection_hash
        ):
            raise TargetedEditorialRevisionConflictError(
                "The frozen synthesis inputs no longer match their verified artifacts"
            )
        if access_policy.do_not_submit or not access_policy.external_llm_allowed:
            raise TargetedSynthesisRevisionValidationError("synthesis_revision_policy_blocked")

        base_request = build_synthesis_model_request(
            context.run,
            context.snapshot,
            extraction,
            evidence_pack,
            access_policy,
            SynthesisMode.FRESH,
        )
        prompt = self._revision_prompt(
            request=request,
            target_path=resolved.path,
            base_hash=base_hash,
            before_hash=resolved.before_hash,
            current_payload=base_payload_for_apply,
            evidence_pack=evidence_pack,
        )
        model_run_id = uuid5(NAMESPACE_URL, f"targeted-synthesis-model-run:{request_identity}")
        model_request = replace(
            base_request,
            text=prompt,
            prompt_template_id="production-synthesis-targeted-revision",
            prompt_template_version=SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION,
            evidence_pack_hash=evidence_pack_hash,
            metadata={
                **base_request.metadata,
                "targeted_revision_request_identity": request_identity,
                "targeted_revision_contract_version": SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION,
                "targeted_revision_parser_version": TARGETED_SYNTHESIS_REVISION_PARSER_VERSION,
                "targeted_revision_validator_version": (
                    TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION
                ),
                "targeted_revision_source_additions_hash": request.source_additions_hash,
                "targeted_revision_base_artifact_fences": (
                    [
                        {
                            "stage": item.stage.value,
                            "artifact_id": str(item.artifact_id),
                            "version": item.version,
                            "input_hash": item.input_hash,
                            "canonical_sha256": item.canonical_sha256,
                        }
                        for item in source_addition_promotion.promotion_plan.base_artifacts
                    ]
                    if source_addition_promotion is not None
                    and source_addition_promotion.promotion_plan is not None
                    else []
                ),
            },
            run_id=model_run_id,
        )
        try:
            execution, archive_error = await self._synthesis._verified_existing_execution(
                model_request
            )
            if archive_error is not None:
                raise TargetedSynthesisRevisionValidationError(
                    "revision_existing_execution_unverified", details=archive_error
                )
            existing_execution = execution is not None
            if execution is None:
                execution = await draft_synthesis_proposal(
                    self._synthesis._model_gateway, model_request
                )
        except ModelSubmissionReconciliationRequiredError as exc:
            raise TargetedSynthesisRevisionValidationError(
                "synthesis_revision_reconciliation_required",
                details={"model_run_id": str(exc.model_run_id or model_run_id), **exc.details},
            ) from exc

        if execution.run.status.value != "succeeded":
            raise TargetedSynthesisRevisionValidationError(
                "synthesis_revision_model_run_not_succeeded",
                details={
                    "model_run_id": str(execution.run.id),
                    "status": execution.run.status.value,
                },
            )
        raw_text, raw_error = await self._synthesis._verified_raw_text(
            execution.run,
            expected_text=execution.output_text if not existing_execution else None,
        )
        if raw_error is not None or raw_text is None:
            raise TargetedSynthesisRevisionValidationError(
                "synthesis_revision_response_unverified", details=raw_error
            )
        wire_patch = _parse_wire_patch(raw_text)
        if wire_patch.refusal is not None:
            return TargetedSynthesisRevisionResult(
                artifact=None,
                publication_artifact=None,
                revision=None,
                refusal=wire_patch.refusal,
            )
        if wire_patch.base_artifact_hash != base_hash:
            raise TargetedEditorialRevisionConflictError("The model patch references another base")
        revised, revised_payload, before_hash = _apply_wire_patch(
            base_synthesis=base_synthesis_for_apply,
            base_payload=base_payload_for_apply,
            target=request.target,
            operation=request.operation,
            patch=wire_patch,
            evidence_pack=evidence_pack,
            extraction=extraction,
            snapshot=context.snapshot,
            source_addition_document_ids=source_addition_document_ids,
            require_claim_scopes=(
                request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
            ),
        )
        if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION:
            _assert_non_target_unchanged(
                context.base_payload,
                revised_payload,
                request.target,
                request.operation,
                resolved,
                allow_extraction_hash_change=True,
            )
            introduced_documents = {
                UUID(str(reference["source_document_id"]))
                for reference in self._revision_evidence_refs(context.base_payload, revised_payload)
            }
            if not introduced_documents.intersection(source_addition_document_ids):
                raise TargetedSynthesisRevisionValidationError(
                    "source_extension_added_evidence_not_used"
                )
        revision_data = {
            "revision_id": str(revision_id),
            "request_identity": request_identity,
            "base_artifact_id": str(context.base_artifact.id),
            "base_artifact_hash": base_hash,
            "target": request.target.to_json(),
            "target_path": resolved.path,
            "operation": request.operation.value,
            "request": request.instruction.strip(),
            "mode": request.mode.value,
            "actor": request.requester,
            "model_run_id": str(execution.run.id),
            "prompt_version": SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION,
            "contract_version": SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION,
            "parser_version": TARGETED_SYNTHESIS_REVISION_PARSER_VERSION,
            "validator_version": TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION,
            "evidence_refs": self._revision_evidence_refs(context.base_payload, revised_payload),
            "evidence_handles": sorted(evidence_pack._handle_to_ref),
            "claim_scopes": (
                re.findall(r"(?im)^\s*CLAIM_SCOPE\s*:\s*([a-z_]+)\s*$", wire_patch.after)
                if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
                else []
            ),
            "source_additions": [dict(item) for item in source_additions],
            "source_additions_hash": request.source_additions_hash,
            "before_hash": before_hash,
            "after_hash": _sha256(revised_payload),
            "rationale": wire_patch.rationale,
            "created_at": datetime.now(UTC).isoformat(),
            "result_artifact_id": str(revision_id),
            "previous_publication_artifact_id": (
                str(context.publication_artifact.id) if context.publication_artifact else None
            ),
        }
        provenance = self._paragraph_provenance(
            context.base_artifact,
            context.base_payload,
            revised_payload,
            revision_id,
            source_addition_document_ids=source_addition_document_ids,
        )
        artifact = await self._synthesis_persistence.store_synthesis_result(
            run_id=context.run.id,
            subject_id=request.subject_id,
            input_hash=revision_input_hash,
            synthesis=revised,
            extraction=extraction,
            raw_result=raw_text,
            model_run_id=execution.run.id,
            mode=SynthesisMode.FRESH,
            model_policy_version=str(
                context.base_artifact.metadata.get("model_policy_version", "")
            ),
            routing_policy_version=str(
                context.base_artifact.metadata.get("routing_policy_version", "")
            ),
            projection_hash=projection.projection_hash if projection is not None else None,
            metadata_extra={
                "revision": revision_data,
                "paragraph_provenance": provenance,
                "revision_contract_version": SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION,
            },
            artifact_id=revision_id,
            expected_current_artifact_id=context.base_artifact.id,
            expected_current_input_hash=context.base_artifact.input_hash,
            expected_current_canonical_sha256=base_hash,
            stale_stages={ProductionArtifactStage.PUBLICATION.value},
            uow=uow,
        )

        enrichment_artifact, enrichment = await self._rebase_enrichment_if_valid(
            context=context,
            extraction=extraction,
            projection=projection,
            revised_synthesis=revised,
            synthesis_artifact=artifact,
            access_policy_hash=policy_hash,
            uow=uow,
        )
        publication = None
        if enrichment_artifact is not None and enrichment is not None:
            publication = await self._assemble_and_render(
                context=context,
                extraction=extraction,
                projection=projection,
                synthesis=revised,
                synthesis_artifact=artifact,
                enrichment_artifact=enrichment_artifact,
                enrichment=enrichment,
                uow=uow,
            )
        revision_data["publication_artifact_id"] = (
            str(publication.id) if publication is not None else None
        )
        revision_data["enrichment_artifact_id"] = (
            str(enrichment_artifact.id) if enrichment_artifact is not None else None
        )
        # Artifact metadata is immutable after storage; its result IDs are
        # discoverable through the publication's input_artifacts and rebase record.
        return TargetedSynthesisRevisionResult(
            artifact=artifact,
            publication_artifact=publication,
            revision=revision_data,
        )

    async def lineage(self, artifact_id: UUID) -> dict[str, Any]:
        enrichment = False
        async with self._synthesis._uow_factory() as uow:
            artifact = await uow.production_artifacts.get(artifact_id)
            if artifact is None:
                raise ValueError("targeted_synthesis_revision_artifact_not_found")
            if artifact.stage is ProductionArtifactStage.EDITORIAL_ENRICHMENT:
                enrichment = True
            elif artifact.stage is not ProductionArtifactStage.SYNTHESIS:
                raise ValueError("targeted_synthesis_revision_artifact_not_synthesis")
            else:
                run_id = artifact.production_run_id
                chain: list[dict[str, Any]] = []
                cursor: ProductionArtifact | None = artifact
                visited: set[UUID] = set()
                while cursor is not None and cursor.id not in visited:
                    visited.add(cursor.id)
                    revision = cursor.metadata.get("revision")
                    if not isinstance(revision, dict):
                        break
                    chain.append(dict(revision))
                    base_id = revision.get("base_artifact_id")
                    try:
                        cursor = await uow.production_artifacts.get(UUID(str(base_id)))
                    except (ValueError, TypeError, AttributeError):
                        break
                    if cursor is None or cursor.stage is not ProductionArtifactStage.SYNTHESIS:
                        break
                result = {
                    "artifact_id": str(artifact_id),
                    "production_run_id": str(run_id),
                    "revisions": chain,
                    "paragraph_provenance": artifact.metadata.get("paragraph_provenance", {}),
                    "element_provenance": {},
                }
        if enrichment:
            if self._enrichment_revision_service is None:
                raise ValueError("targeted_enrichment_revision_unavailable")
            return cast(
                dict[str, Any],
                await self._enrichment_revision_service.lineage_targeted(artifact_id),
            )
        return result

    @staticmethod
    def _validate_request(request: TargetedEditorialRevisionRequest) -> None:
        if not isinstance(request, TargetedEditorialRevisionRequest):
            raise ValueError("targeted_synthesis_revision_request_invalid")
        if not isinstance(request.operation, TargetedEditorialRevisionOperation):
            raise ValueError("targeted_synthesis_revision_operation_invalid")
        if not isinstance(request.mode, TargetedEditorialRevisionMode):
            raise ValueError("targeted_synthesis_revision_mode_invalid")
        enrichment_operations = {
            TargetedEditorialRevisionOperation.CREATE_TABLE,
            TargetedEditorialRevisionOperation.REWRITE_TABLE,
            TargetedEditorialRevisionOperation.DELETE_TABLE,
            TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
            TargetedEditorialRevisionOperation.REWRITE_DIAGRAM,
            TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
            TargetedEditorialRevisionOperation.CREATE_CHART,
            TargetedEditorialRevisionOperation.REWRITE_CHART,
            TargetedEditorialRevisionOperation.DELETE_CHART,
            TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE,
            TargetedEditorialRevisionOperation.REWRITE_SOURCE_FIGURE,
            TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        }
        if request.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
            kind = request.target.element_kind
            operation = request.operation
            operation_kind = {
                TargetedEditorialRevisionOperation.CREATE_TABLE: "table",
                TargetedEditorialRevisionOperation.REWRITE_TABLE: "table",
                TargetedEditorialRevisionOperation.DELETE_TABLE: "table",
                TargetedEditorialRevisionOperation.CREATE_DIAGRAM: "diagram",
                TargetedEditorialRevisionOperation.REWRITE_DIAGRAM: "diagram",
                TargetedEditorialRevisionOperation.DELETE_DIAGRAM: "diagram",
                TargetedEditorialRevisionOperation.CREATE_CHART: "chart",
                TargetedEditorialRevisionOperation.REWRITE_CHART: "chart",
                TargetedEditorialRevisionOperation.DELETE_CHART: "chart",
                TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE: "figure",
                TargetedEditorialRevisionOperation.REWRITE_SOURCE_FIGURE: "figure",
                TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE: "figure",
            }.get(operation)
            if operation_kind is None or kind is None or kind.value != operation_kind:
                raise ValueError("targeted_enrichment_revision_target_operation_mismatch")
            is_create = operation in {
                TargetedEditorialRevisionOperation.CREATE_TABLE,
                TargetedEditorialRevisionOperation.CREATE_DIAGRAM,
                TargetedEditorialRevisionOperation.CREATE_CHART,
            }
            if is_create:
                if request.target.element_key is not None or request.target.placement is None:
                    raise ValueError("targeted_enrichment_revision_create_target_invalid")
            elif request.target.element_key is None or request.target.placement is not None:
                raise ValueError("targeted_enrichment_revision_element_target_invalid")
        elif request.operation in enrichment_operations:
            raise ValueError("targeted_editorial_revision_target_operation_mismatch")
        instruction = request.instruction.strip()
        if not instruction or len(instruction) > MAX_TARGETED_REVISION_INSTRUCTION_LENGTH:
            raise ValueError("targeted_synthesis_revision_instruction_invalid")
        if not request.requester.strip() or len(request.requester) > 256:
            raise ValueError("targeted_synthesis_revision_requester_invalid")
        if request.base_version < 1:
            raise ValueError("targeted_synthesis_revision_base_version_invalid")
        if (
            _SHA256.fullmatch(request.base_input_hash) is None
            or _SHA256.fullmatch(request.base_canonical_sha256) is None
            or (
                request.source_additions_hash is not None
                and _SHA256.fullmatch(request.source_additions_hash) is None
            )
            or (request.before_hash is not None and _SHA256.fullmatch(request.before_hash) is None)
        ):
            raise ValueError("targeted_synthesis_revision_base_hash_invalid")

    @staticmethod
    def _request_identity_payload(request: TargetedEditorialRevisionRequest) -> dict[str, Any]:
        return {
            "subject_id": str(request.subject_id),
            "base_artifact_id": str(request.base_artifact_id),
            "base_version": request.base_version,
            "base_input_hash": request.base_input_hash,
            "base_canonical_sha256": request.base_canonical_sha256,
            "target": request.target.to_json(),
            "operation": request.operation.value,
            "instruction": request.instruction.strip(),
            "mode": request.mode.value,
            "requester": request.requester,
            "source_addition_request_id": (
                str(request.source_addition_request_id)
                if request.source_addition_request_id is not None
                else None
            ),
            "source_additions_hash": request.source_additions_hash,
            "before_hash": request.before_hash,
            "prompt_version": SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION,
            "contract_version": SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION,
            "parser_version": TARGETED_SYNTHESIS_REVISION_PARSER_VERSION,
            "validator_version": TARGETED_SYNTHESIS_REVISION_VALIDATOR_VERSION,
        }

    async def _existing_result(
        self, subject_id: UUID, revision_id: UUID
    ) -> TargetedSynthesisRevisionResult | None:
        async with self._synthesis._uow_factory() as uow:
            artifact = await uow.production_artifacts.get(revision_id)
            if artifact is None:
                return None
            if (
                artifact.subject_id != subject_id
                or artifact.stage is not ProductionArtifactStage.SYNTHESIS
            ):
                raise TargetedEditorialRevisionConflictError(
                    "The deterministic revision identity belongs to another artifact"
                )
            revision = artifact.metadata.get("revision")
            if not isinstance(revision, dict):
                raise ValueError("targeted_synthesis_revision_result_corrupt")
            publication = next(
                (
                    item
                    for item in await uow.production_artifacts.list_for_run(
                        artifact.production_run_id
                    )
                    if item.stage is ProductionArtifactStage.PUBLICATION
                    and isinstance(item.metadata.get("input_artifacts"), dict)
                    and item.metadata["input_artifacts"].get("synthesis_artifact_id")
                    == str(artifact.id)
                ),
                None,
            )
        return TargetedSynthesisRevisionResult(
            artifact=artifact,
            publication_artifact=publication,
            revision=revision,
        )

    async def _load_context(
        self, request: TargetedEditorialRevisionRequest, *, uow: Any | None = None
    ) -> _RevisionContext:
        if uow is None:
            async with self._synthesis._uow_factory() as active_uow:
                return await self._load_context(request, uow=active_uow)
        run = await uow.production_runs.get_current_for_subject(request.subject_id)
        base = await uow.production_artifacts.get(request.base_artifact_id)
        if run is None or base is None or base.production_run_id != run.id:
            raise TargetedEditorialRevisionConflictError(
                "The requested synthesis base is no longer current"
            )
        current = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.SYNTHESIS.value
        )
        if (
            current is None
            or current.id != base.id
            or base.version != request.base_version
            or base.input_hash != request.base_input_hash
            or base.status is not ProductionArtifactStatus.VERIFIED
            or base.canonical_blob_id is None
        ):
            raise TargetedEditorialRevisionConflictError("The requested synthesis base is stale")
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
        references = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.REFERENCES.value
        )
        extraction = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.EXTRACTION.value
        )
        projection = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.RELEVANCE_PROJECTION.value
        )
        publication = await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.PUBLICATION.value
        )
        if snapshot is None or references is None or extraction is None:
            raise ValueError("targeted_synthesis_revision_inputs_missing")
        for item in (references, extraction, base):
            if (
                item.status is not ProductionArtifactStatus.VERIFIED
                or item.canonical_blob_id is None
            ):
                raise ValueError("targeted_synthesis_revision_inputs_missing")
        if projection is not None and (
            projection.status is not ProductionArtifactStatus.VERIFIED
            or projection.canonical_blob_id is None
        ):
            raise ValueError("targeted_synthesis_revision_projection_invalid")
        base_bytes = await self._synthesis._artifact_store.read_bytes(base.canonical_blob_id)
        if hashlib.sha256(base_bytes).hexdigest() != request.base_canonical_sha256:
            raise TargetedEditorialRevisionConflictError(
                "The requested synthesis base content hash changed"
            )
        payload = json.loads(base_bytes)
        if not isinstance(payload, dict):
            raise ValueError("targeted_synthesis_revision_base_invalid")
        return _RevisionContext(
            run=run,
            snapshot=snapshot,
            base_artifact=base,
            base_payload=payload,
            references_artifact=references,
            extraction_artifact=extraction,
            projection_artifact=projection,
            publication_artifact=publication,
        )

    @staticmethod
    def _revision_prompt(
        *,
        request: TargetedEditorialRevisionRequest,
        target_path: str,
        base_hash: str,
        before_hash: str,
        current_payload: Mapping[str, Any],
        evidence_pack: SynthesisEvidencePackV1,
    ) -> str:
        authorized_evidence = [
            *evidence_pack.narrative_evidence,
            *evidence_pack.technical_evidence,
            *evidence_pack.reserve_evidence,
        ]
        operation = request.operation
        if operation is TargetedEditorialRevisionOperation.REWRITE_TITLE:
            after_format = "AFTER format: one line containing only the replacement title."
        elif operation is TargetedEditorialRevisionOperation.RENAME_SECTION:
            after_format = (
                "AFTER format: one non-empty line containing only the replacement section "
                "heading. Do not include claims or a section wrapper. Generic CTI terms such "
                "as C2, S3, IPv4/IPv6, x64/x86, SHA-1/SHA-256/MD5, HTTP/2, OAuth2, and Web3 "
                "may describe the claims without appearing literally in them; C&C and 2FA/MFA "
                "are also generic terms. Keep numbers, "
                "indicators, and case-specific product, actor, campaign, or family names "
                "grounded in the section claims."
            )
        elif operation in {
            TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
            TargetedEditorialRevisionOperation.DELETE_SECTION,
        }:
            after_format = "AFTER format: the exact literal EMPTY."
        elif operation is TargetedEditorialRevisionOperation.REWRITE_LEAD:
            after_format = (
                "AFTER format: a LEAD block containing one or more CLAIM blocks. Each CLAIM "
                "must contain EVIDENCE: with admitted handles and TEXT: with the prose. Claim "
                "IDs are optional."
            )
        elif operation in {
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
            TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
        }:
            lead_target = request.target.kind in {
                TargetedEditorialRevisionTargetKind.LEAD,
                TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH,
            }
            group = "LEAD" if lead_target else "SECTION using the current section kind"
            after_format = (
                f"AFTER format: exactly one CLAIM block inside a {group} block. The CLAIM "
                "must contain EVIDENCE: with admitted handles and TEXT: with the prose. Claim "
                "IDs are optional."
            )
        elif operation is TargetedEditorialRevisionOperation.SPLIT_SECTION:
            after_format = (
                "AFTER format: exactly two SECTION blocks, each using the current section kind "
                "and containing one or more CLAIM blocks. Each CLAIM must contain EVIDENCE: "
                "with admitted handles and TEXT: with the prose. Claim IDs are optional."
            )
        elif operation is TargetedEditorialRevisionOperation.REWRITE_SECTION:
            after_format = (
                "AFTER format: exactly one SECTION block using the target section's current "
                "kind and containing one or more CLAIM blocks. HEADING is optional. Each CLAIM "
                "must contain EVIDENCE: with admitted handles and TEXT: with the prose. Claim "
                "IDs are optional."
            )
        elif operation is TargetedEditorialRevisionOperation.MERGE_SECTIONS:
            after_format = (
                "AFTER format: exactly one SECTION block using the shared current kind of the "
                "target sections and containing one or more CLAIM blocks. HEADING is optional. "
                "Each CLAIM must contain EVIDENCE: with admitted handles and TEXT: with the "
                "prose. Claim IDs are optional."
            )
        else:
            after_format = (
                "AFTER format: exactly one SECTION block whose kind fits the inserted section's "
                "claims and containing one or more CLAIM blocks. HEADING is optional. Each CLAIM "
                "must contain EVIDENCE: with admitted handles and TEXT: with the prose. Claim "
                "IDs are optional."
            )
        claim_scope_example = (
            "CLAIM_SCOPE: tool_context\n"
            if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
            else ""
        )
        claim_example = (
            "@@CLAIM C001@@\n"
            f"{claim_scope_example}EVIDENCE: E001\nTEXT: Replacement prose.\n@@END CLAIM@@"
        )
        if operation is TargetedEditorialRevisionOperation.REWRITE_TITLE:
            after_example = "A concise evidence-grounded title"
        elif operation is TargetedEditorialRevisionOperation.RENAME_SECTION:
            after_example = "A concise evidence-grounded heading"
        elif operation in {
            TargetedEditorialRevisionOperation.DELETE_PARAGRAPH,
            TargetedEditorialRevisionOperation.DELETE_SECTION,
        }:
            after_example = "EMPTY"
        elif operation is TargetedEditorialRevisionOperation.REWRITE_LEAD:
            after_example = f"@@LEAD@@\n{claim_example}\n@@END LEAD@@"
        elif operation in {
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
            TargetedEditorialRevisionOperation.INSERT_PARAGRAPH,
        }:
            lead_target = request.target.kind in {
                TargetedEditorialRevisionTargetKind.LEAD,
                TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH,
            }
            if lead_target:
                after_example = f"@@LEAD@@\n{claim_example}\n@@END LEAD@@"
            else:
                after_example = f"@@SECTION overview S001@@\n{claim_example}\n@@END SECTION@@"
        elif operation is TargetedEditorialRevisionOperation.SPLIT_SECTION:
            after_example = (
                f"@@SECTION overview S001@@\n{claim_example}\n@@END SECTION@@\n"
                f"@@SECTION overview S002@@\n{claim_example.replace('C001', 'C002')}\n"
                "@@END SECTION@@"
            )
        else:
            after_example = f"@@SECTION overview S001@@\n{claim_example}\n@@END SECTION@@"
        return "\n".join(
            (
                "TASK: Apply exactly one targeted synthesis revision patch.",
                f"Prompt version: {SYNTHESIS_TARGETED_REVISION_PROMPT_VERSION}",
                f"Contract version: {SYNTHESIS_TARGETED_REVISION_CONTRACT_VERSION}",
                f"TARGET: {target_path}",
                f"ACTION: {request.operation.value}",
                f"BASE_ARTIFACT_HASH: {base_hash}",
                f"BEFORE_HASH: {before_hash}",
                f"MODE: {request.mode.value}",
                f"INSTRUCTION: {request.instruction.strip()}",
                "",
                "CURRENT SYNTHESIS (IDs are content hashes):",
                _render_current_synthesis(current_payload),
                "",
                "AUTHORIZED EVIDENCE HANDLES:",
                json.dumps(authorized_evidence, ensure_ascii=False, sort_keys=True, default=str),
                "",
                SYNTHESIS_EDITORIAL_CONTRACT_V5,
                "",
                "CONSERVATION RULES:",
                (
                    "Do not modify any content outside TARGET. Do not alter IOC, sources, "
                    "timeline, "
                    "source notes, uncertainties, or attribution."
                ),
                "Use only the evidence handles listed above. Do not add sources or relations.",
                (
                    "For allow_source_extension, label every claim with CLAIM_SCOPE: "
                    "tool_context or actor_activity. Use the new CONTEXT source only for "
                    "generic tool descriptions. It does not prove that the subject used the "
                    "tool or performed any operation. Claims about subject activity require "
                    "a CORE, subject-scoped evidence handle and must not cite a TOOL_CONTEXT "
                    "handle. Never infer intrusion, execution, persistence, or C2."
                    if request.mode is TargetedEditorialRevisionMode.ALLOW_SOURCE_EXTENSION
                    else ""
                ),
                (
                    "For a change that needs new sources, repair of extraction, a scope change, "
                    "or "
                    "QA bypass, return REFUSAL revision_not_applicable with restart stage "
                    "and reason."
                ),
                "Return only the wire envelope. " + after_format,
                "",
                "WIRE:",
                "@@TARGET@@",
                target_path,
                "@@ACTION@@",
                request.operation.value,
                "@@BASE_ARTIFACT_HASH@@",
                base_hash,
                "@@BEFORE_HASH@@",
                before_hash,
                "@@AFTER@@",
                after_example,
                "@@RATIONALE@@",
                "Short evidence-based reason.",
                "@@END@@",
            )
        )

    @staticmethod
    def _target_evidence_refs(
        payload: Mapping[str, Any], target: TargetedEditorialRevisionTarget
    ) -> list[dict[str, str]]:
        selected: list[Mapping[str, Any]] = []
        if target.kind is TargetedEditorialRevisionTargetKind.TITLE:
            return []
        if target.kind in {
            TargetedEditorialRevisionTargetKind.LEAD,
            TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH,
        }:
            paragraphs = payload["lead"]
            if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
                paragraphs = [
                    item for item in paragraphs if _paragraph_id(item) == target.stable_ids[0]
                ]
            selected.extend(paragraphs)
        else:
            sections = payload["sections"]
            if target.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH:
                sections = [item for item in sections if _section_id(item) == target.parent_id]
            elif target.kind is TargetedEditorialRevisionTargetKind.SECTION:
                sections = [
                    item for item in sections if _section_id(item) in set(target.stable_ids)
                ]
            for section in sections:
                paragraphs = section.get("paragraphs", [])
                if target.kind is TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH:
                    paragraphs = [
                        item for item in paragraphs if _paragraph_id(item) in set(target.stable_ids)
                    ]
                selected.extend(paragraphs)
        refs = {
            _canonical_bytes(ref): ref
            for paragraph in selected
            for ref in paragraph.get("evidence_refs", [])
        }
        return [refs[key] for key in sorted(refs)]

    @staticmethod
    def _revision_evidence_refs(
        before: Mapping[str, Any], after: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        old_counts: dict[bytes, int] = {}
        for paragraph in (
            *before.get("lead", []),
            *(
                item
                for section in before.get("sections", [])
                for item in section.get("paragraphs", [])
            ),
        ):
            encoded = _canonical_bytes(paragraph)
            old_counts[encoded] = old_counts.get(encoded, 0) + 1
        refs: dict[bytes, dict[str, str]] = {}
        for paragraph in (
            *after.get("lead", []),
            *(
                item
                for section in after.get("sections", [])
                for item in section.get("paragraphs", [])
            ),
        ):
            encoded = _canonical_bytes(paragraph)
            if old_counts.get(encoded, 0):
                old_counts[encoded] -= 1
                continue
            for reference in paragraph.get("evidence_refs", []):
                refs[_canonical_bytes(reference)] = reference
        return [refs[key] for key in sorted(refs)]

    @staticmethod
    def _paragraph_provenance(
        base_artifact: ProductionArtifact,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        revision_id: UUID,
        *,
        source_addition_document_ids: frozenset[UUID] = frozenset(),
    ) -> dict[str, Any]:
        old = base_artifact.metadata.get("paragraph_provenance")
        provenance = dict(old) if isinstance(old, dict) else {}
        old_ids = {
            _paragraph_id(item)
            for item in (
                *before.get("lead", []),
                *(
                    paragraph
                    for section in before.get("sections", [])
                    for paragraph in section.get("paragraphs", [])
                ),
            )
        }
        new_paragraphs = (
            *after.get("lead", []),
            *(
                paragraph
                for section in after.get("sections", [])
                for paragraph in section.get("paragraphs", [])
            ),
        )
        for paragraph in new_paragraphs:
            identity = _paragraph_id(paragraph)
            if identity not in provenance:
                if identity in old_ids:
                    provenance[identity] = {
                        "origin": "initial" if old is None else "inherited",
                        "artifact_id": str(base_artifact.id),
                        "revision_id": None,
                    }
                else:
                    source_document_ids = sorted(
                        {
                            str(reference["source_document_id"])
                            for reference in paragraph.get("evidence_refs", [])
                            if isinstance(reference, Mapping)
                            and reference.get("source_document_id") is not None
                            and UUID(str(reference["source_document_id"]))
                            in source_addition_document_ids
                        }
                    )
                    provenance[identity] = {
                        "origin": "revision",
                        "artifact_id": str(revision_id),
                        "revision_id": str(revision_id),
                        **(
                            {"source_document_ids": source_document_ids}
                            if source_document_ids
                            else {}
                        ),
                    }
        return {
            key: provenance[key]
            for key in sorted(provenance)
            if key in {_paragraph_id(item) for item in new_paragraphs}
        }

    async def _rebase_enrichment_if_valid(
        self,
        *,
        context: _RevisionContext,
        extraction: ProductionExtractionV1,
        projection: Any,
        revised_synthesis: ProductionSynthesisV1,
        synthesis_artifact: ProductionArtifact,
        access_policy_hash: str,
        uow: Any | None = None,
    ) -> tuple[ProductionArtifact | None, EditorialEnrichmentV1 | None]:
        if uow is None:
            async with self._synthesis._uow_factory() as active_uow:
                result = await self._rebase_enrichment_if_valid(
                    context=context,
                    extraction=extraction,
                    projection=projection,
                    revised_synthesis=revised_synthesis,
                    synthesis_artifact=synthesis_artifact,
                    access_policy_hash=access_policy_hash,
                    uow=active_uow,
                )
                await active_uow.commit()
                return result
        enrichment_artifact = await uow.production_artifacts.get_current(
            context.run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
        if (
            enrichment_artifact is None
            or enrichment_artifact.status is not ProductionArtifactStatus.VERIFIED
            or enrichment_artifact.canonical_blob_id is None
        ):
            return None, None
        current_bytes = await self._synthesis._artifact_store.read_bytes(
            enrichment_artifact.canonical_blob_id
        )
        current_sha = hashlib.sha256(current_bytes).hexdigest()
        try:
            raw_payload = json.loads(current_bytes)
            enrichment = editorial_enrichment_from_json(raw_payload)
            base_synthesis = production_synthesis_from_json(context.base_payload)
            raw_before_sections = context.base_payload.get("sections")
            before_sections = (
                raw_before_sections
                if isinstance(raw_before_sections, list)
                else production_synthesis_to_json(base_synthesis)["sections"]
            )
            after_sections = production_synthesis_to_json(revised_synthesis)["sections"]
            after_anchor_indexes: dict[str, list[int]] = {}
            after_legacy_indexes: dict[str, list[int]] = {}
            for index, section in enumerate(after_sections):
                if isinstance(section, Mapping):
                    # The heading-insensitive anchor is the current format.
                    after_anchor_indexes.setdefault(
                        _enrichment_section_anchor_id(section), []
                    ).append(index)
                    # Keep the full canonical ID as a compatibility path for
                    # old artifacts and unchanged legacy sections.
                    after_legacy_indexes.setdefault(_section_id(section), []).append(index)
            after_section_indexes = {
                identity: indexes[0]
                for identities in (after_anchor_indexes, after_legacy_indexes)
                for identity, indexes in identities.items()
                if len(indexes) == 1
            }
            stale_blocks: list[dict[str, str]] = []

            def rebase_placements(kind: str, items: Sequence[Any]) -> tuple[Any, ...]:
                kept: list[Any] = []
                for item in items:
                    placement = item.placement
                    if placement.kind.value == "after_section":
                        index = placement.section_index
                        if index is None or index >= len(before_sections):
                            stale_blocks.append(
                                {
                                    "kind": kind,
                                    "key": item.key,
                                    "reason": "anchor_missing",
                                    "source_artifact_id": str(enrichment_artifact.id),
                                }
                            )
                            continue
                        before_section = before_sections[index]
                        section_identity = _enrichment_section_anchor_id(before_section)
                        new_index = after_section_indexes.get(section_identity)
                        if new_index is None:
                            # Read placements from artifacts created before
                            # heading-insensitive anchors existed.
                            new_index = after_section_indexes.get(_section_id(before_section))
                        if new_index is None:
                            stale_blocks.append(
                                {
                                    "kind": kind,
                                    "key": item.key,
                                    "reason": "section_anchor_changed",
                                    "source_artifact_id": str(enrichment_artifact.id),
                                }
                            )
                            continue
                        if new_index != index:
                            item = replace(
                                item,
                                placement=replace(placement, section_index=new_index),
                            )
                    kept.append(item)
                return tuple(kept)

            tables = rebase_placements("table", enrichment.tables)
            diagrams = rebase_placements("diagram", enrichment.diagrams)
            charts = rebase_placements("chart", enrichment.charts)
            source_figures = rebase_placements("source_figure", enrichment.source_figures)
            kept_figure_keys = {item.key for item in source_figures}
            removed_figure_ids = {
                item.resolved_figure.figure_id
                for item in enrichment.source_figures
                if item.key not in kept_figure_keys and item.resolved_figure is not None
            }
            figure_decisions = tuple(
                item
                for item in enrichment.figure_decisions
                if item.figure_id not in removed_figure_ids
            )
            provisional = replace(
                enrichment,
                extraction_hash=canonical_extraction_hash(extraction),
                synthesis_hash=canonical_synthesis_hash(revised_synthesis),
                tables=tables,
                diagrams=diagrams,
                charts=charts,
                source_figures=source_figures,
                figure_decisions=figure_decisions,
                annotations=(),
            )
            before_anchors = semantic_annotation_anchor_texts(base_synthesis, enrichment)
            after_anchors = semantic_annotation_anchor_texts(revised_synthesis, provisional)
            annotations = []
            for annotation in enrichment.annotations:
                before_text = before_anchors.get(annotation.paragraph_anchor)
                after_text = after_anchors.get(annotation.paragraph_anchor)
                if (
                    before_text is None
                    or before_text != after_text
                    or after_text is None
                    or annotation.text not in after_text
                ):
                    stale_blocks.append(
                        {
                            "kind": "annotation",
                            "key": annotation.paragraph_anchor,
                            "reason": "paragraph_anchor_changed",
                            "source_artifact_id": str(enrichment_artifact.id),
                        }
                    )
                    continue
                annotations.append(annotation)
            rebased = replace(provisional, annotations=tuple(annotations))
            validate_editorial_enrichment(
                rebased, extraction=extraction, synthesis=revised_synthesis
            )
            evidence_pack = build_editorial_enrichment_evidence_pack(
                context.snapshot, extraction, revised_synthesis, projection
            )
            new_evidence_hash = editorial_enrichment_evidence_pack_hash(evidence_pack)
            projection_hash = projection.projection_hash if projection is not None else None
            inventory_hash = enrichment_artifact.metadata.get("source_figure_inventory_hash")
            new_input_hash = compute_editorial_enrichment_input_hash(
                extraction=extraction,
                synthesis=revised_synthesis,
                evidence_pack_hash=new_evidence_hash,
                access_policy_hash=access_policy_hash,
                projection_hash=projection_hash,
                source_figure_inventory_hash=(
                    str(inventory_hash) if inventory_hash is not None else None
                ),
                resource_search_enabled=self._resource_search_enabled,
            )
        except (ValueError, TypeError, KeyError):
            await uow.production_artifacts.mark_stages_stale(
                context.run.id, {ProductionArtifactStage.EDITORIAL_ENRICHMENT.value}
            )
            return None, None

        rebased_artifact = await self._enrichment_persistence.store_editorial_enrichment_result(
            run_id=context.run.id,
            subject_id=context.snapshot.subject_id,
            input_hash=new_input_hash,
            enrichment=rebased,
            extraction=extraction,
            synthesis=revised_synthesis,
            raw_result=None,
            model_run_id=enrichment_artifact.model_run_id,
            evidence_pack_hash=new_evidence_hash,
            access_policy_hash=access_policy_hash,
            model_policy_version=str(enrichment_artifact.metadata.get("model_policy_version", "")),
            routing_policy_version=str(
                enrichment_artifact.metadata.get("routing_policy_version", "")
            ),
            projection_hash=projection_hash,
            source_figure_inventory_hash=(
                str(inventory_hash) if inventory_hash is not None else None
            ),
            metadata_extra={
                "revision_rebase": {
                    "from_artifact_id": str(enrichment_artifact.id),
                    "synthesis_artifact_id": str(synthesis_artifact.id),
                    "evidence_pack_hash": new_evidence_hash,
                    "anchor_validation": "passed",
                    "model_call_count": 0,
                    "stale_blocks": stale_blocks,
                    "adopted_blocks": {
                        "tables": [item.key for item in rebased.tables],
                        "diagrams": [item.key for item in rebased.diagrams],
                        "charts": [item.key for item in rebased.charts],
                        "source_figures": [item.key for item in rebased.source_figures],
                        "annotations": [item.paragraph_anchor for item in rebased.annotations],
                    },
                }
            },
            expected_current_artifact_id=enrichment_artifact.id,
            expected_current_input_hash=enrichment_artifact.input_hash,
            expected_current_canonical_sha256=current_sha,
            canonical_payload=editorial_enrichment_to_json(rebased),
            resource_search_enabled=self._resource_search_enabled,
            uow=uow,
        )
        return rebased_artifact, rebased

    async def _assemble_and_render(
        self,
        *,
        context: _RevisionContext,
        extraction: ProductionExtractionV1,
        projection: Any,
        synthesis: ProductionSynthesisV1,
        synthesis_artifact: ProductionArtifact,
        enrichment_artifact: ProductionArtifact,
        enrichment: EditorialEnrichmentV1,
        uow: Any,
    ) -> ProductionArtifact:
        references_blob_id = context.references_artifact.canonical_blob_id
        if references_blob_id is None:
            raise TargetedSynthesisRevisionValidationError("references_artifact_blob_missing")
        references_payload = await self._synthesis._artifact_store.read_json(references_blob_id)
        references = production_reference_corpus_from_json(references_payload)
        publication = await PublicationAssemblyService(
            self._synthesis._artifact_store, uow.production_artifacts
        ).assemble_publication(
            run=context.run,
            snapshot=context.snapshot,
            references=references,
            extraction=extraction,
            relevance_projection=projection,
            synthesis=synthesis,
            editorial_enrichment=enrichment,
            metadata_extra={
                "input_artifacts": {
                    "references_artifact_id": str(context.references_artifact.id),
                    "extraction_artifact_id": str(context.extraction_artifact.id),
                    "relevance_projection_artifact_id": (
                        str(context.projection_artifact.id)
                        if context.projection_artifact is not None
                        else None
                    ),
                    "synthesis_artifact_id": str(synthesis_artifact.id),
                    "editorial_enrichment_artifact_id": str(enrichment_artifact.id),
                },
                "editorial_revision_artifact_id": str(synthesis_artifact.id),
            },
        )
        return publication


def _target_value_for_display(
    payload: Mapping[str, Any], target: TargetedEditorialRevisionTarget
) -> Any:
    if target.kind is TargetedEditorialRevisionTargetKind.TITLE:
        return payload.get("title")
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD:
        return payload.get("lead")
    if target.kind is TargetedEditorialRevisionTargetKind.SECTIONS:
        return payload.get("sections")
    if target.kind is TargetedEditorialRevisionTargetKind.LEAD_PARAGRAPH:
        return next(item for item in payload["lead"] if _paragraph_id(item) == target.stable_ids[0])
    if target.kind is TargetedEditorialRevisionTargetKind.SECTION:
        return next(
            item for item in payload["sections"] if _section_id(item) == target.stable_ids[0]
        )
    section = next(item for item in payload["sections"] if _section_id(item) == target.parent_id)
    return next(
        item for item in section["paragraphs"] if _paragraph_id(item) == target.stable_ids[0]
    )


def _critical_extraction_failures(
    extraction: ProductionExtractionV1,
    reference_corpus: ProductionReferenceCorpusV1,
) -> tuple[Any, ...]:
    references_by_url = {source.canonical_url: source for source in reference_corpus.sources}
    critical_failures = []
    for omission in extraction.omitted_sources:
        if omission.reason is not ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED:
            continue
        source = references_by_url.get(omission.canonical_url)
        criticality = (
            ProductionSourceCriticality.CRITICAL
            if source is None
            else production_source_criticality(source, sources=extraction.sources)[0]
        )
        if criticality is ProductionSourceCriticality.CRITICAL:
            critical_failures.append(omission)
    return tuple(critical_failures)
