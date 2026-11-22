"""E04: equal paragraphs in one parent keep independent revision targets.

The typed revision target identifies a paragraph by content id and parent
hash. Two equal paragraphs in one section share both, so the target gains an
optional `occurrence_ordinal` (n-th equal paragraph in that parent). It is
only carried when the content id is not unique: historical targets stay
byte-identical and an ordinal-less legacy target is still refused.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.editorial_proposal_adoption_inputs import (
    _manual_text_without_evidence_patch,
)
from cti_app.application.editorial_synthesis.patch_validation import WirePatch
from cti_app.application.editorial_synthesis.targets import resolve_target
from cti_app.application.editorial_workspace.registry import (
    ResolvedWorkspaceTarget,
    allowed_operations,
    project_blocks,
    resolve_workspace_target,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_synthesis import SynthesisEvidencePackV1
from cti_app.application.production_synthesis_revision import _resolve_target
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    bootstrap_block_registry,
    canonical_content_sha256,
)
from cti_app.domain.production import ProductionInputSnapshot
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.domain.production_extraction import ProductionExtractionV1
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
)

_DUPLICATE = "Identical paragraph body."
_PARAGRAPH_KIND = TargetedEditorialRevisionTargetKind.SECTION_PARAGRAPH


def _payload(*, duplicates: bool = True) -> dict[str, Any]:
    section_paragraphs = [
        {"text": "Unique neighbour.", "evidence_refs": []},
        {"text": _DUPLICATE, "evidence_refs": []},
        (
            {"text": _DUPLICATE, "evidence_refs": []}
            if duplicates
            else {"text": "Second unique neighbour.", "evidence_refs": []}
        ),
    ]
    return {
        "schema_version": "2",
        "title": "Synthetic title",
        "lead": [{"text": "Synthetic lead", "evidence_refs": []}],
        "sections": [
            {
                "kind": "technical",
                "heading": "Synthetic heading",
                "paragraphs": section_paragraphs,
            }
        ],
        "timeline": [],
        "source_notes": [],
    }


def _paragraph_entries(
    payload: dict[str, Any],
) -> tuple[BlockRegistryEntry, ...]:
    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="9" * 64,
        source_stage="synthesis",
        canonical_content=payload,
    )
    return tuple(entry for entry in entries if entry.kind == "paragraph")


def _resolved(
    payload: dict[str, Any], index: int, operation: str = "rewrite_paragraph"
) -> ResolvedWorkspaceTarget:
    return resolve_workspace_target(
        entry=_paragraph_entries(payload)[index],
        canonical_content=payload,
        operation=operation,
    )


def _synthesis_domain(payload: dict[str, Any]) -> ProductionSynthesisV1:
    sections = payload["sections"]
    assert isinstance(sections, list)
    section = sections[0]
    assert isinstance(section, dict)
    paragraphs = section["paragraphs"]
    assert isinstance(paragraphs, list)
    lead = payload["lead"]
    assert isinstance(lead, list)
    return ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=uuid4(),
        production_input_hash="a" * 64,
        extraction_hash="b" * 64,
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title=str(payload["title"]),
        lead=tuple(
            SynthesisParagraphV1(str(item["text"]), ()) for item in lead if isinstance(item, dict)
        ),
        sections=(
            SynthesisSectionV1(
                SynthesisSectionKind.TECHNICAL,
                str(section["heading"]),
                tuple(
                    SynthesisParagraphV1(str(item["text"]), ())
                    for item in paragraphs
                    if isinstance(item, dict)
                ),
            ),
        ),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )


def _evidence_pack() -> SynthesisEvidencePackV1:
    return SynthesisEvidencePackV1(
        subject_title="Synthetic subject",
        publication_language="fr",
        discovery_summary="Synthetic summary.",
        actor_or_campaign="Synthetic actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        narrative_evidence=(),
        technical_evidence=(),
        reserve_evidence=(),
        source_note_sources=(),
        source_pair_relations=(),
        uncertainties=(),
    )


def _apply_second_paragraph(payload: dict[str, Any], index: int, text: str) -> dict[str, Any]:
    resolved = _resolved(payload, index)
    target = resolved.target
    operation = TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    canonical = resolve_target(payload, target, operation)
    patch = WirePatch(
        target=canonical.path,
        action=operation,
        base_artifact_hash="0" * 64,
        before_hash=canonical.before_hash,
        after=text,
        rationale="Synthetic occurrence oracle.",
    )
    # The applier deletes these three arguments at entry; it only needs the
    # base synthesis, payload, target and wire patch.
    _revised, revised_payload, _before_hash = _manual_text_without_evidence_patch(
        base_synthesis=_synthesis_domain(payload),
        base_payload=payload,
        target=target,
        operation=operation,
        patch=patch,
        evidence_pack=_evidence_pack(),
        extraction=object.__new__(ProductionExtractionV1),
        snapshot=object.__new__(ProductionInputSnapshot),
    )
    return revised_payload


def test_equal_paragraphs_resolve_to_distinct_occurrence_targets() -> None:
    payload = _payload()
    entries = _paragraph_entries(payload)
    first, second = (
        resolve_workspace_target(
            entry=entries[index],
            canonical_content=payload,
            operation="rewrite_paragraph",
        )
        for index in (1, 2)
    )

    assert first.target != second.target
    assert (first.target.occurrence_ordinal, second.target.occurrence_ordinal) == (0, 1)
    assert first.before_hash == second.before_hash
    assert first.path != second.path
    assert first.legacy_target_id is None and second.legacy_target_id is None
    assert first.target_id != second.target_id
    for operation in ("rewrite_paragraph", "delete_paragraph"):
        assert operation in allowed_operations(entries[1], payload)
        assert operation in allowed_operations(entries[2], payload)

    first_canonical = resolve_target(
        payload, first.target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    )
    second_canonical = resolve_target(
        payload, second.target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    )
    assert (first_canonical.parent_index, first_canonical.indexes) == (0, (1,))
    assert (second_canonical.parent_index, second_canonical.indexes) == (0, (2,))
    assert first_canonical.path != second_canonical.path
    first_delete = resolve_target(
        payload, first.target, TargetedEditorialRevisionOperation.DELETE_PARAGRAPH
    )
    second_delete = resolve_target(
        payload, second.target, TargetedEditorialRevisionOperation.DELETE_PARAGRAPH
    )
    assert first_delete.indexes == (1,) and second_delete.indexes == (2,)


def test_unique_neighbour_stays_editable_next_to_equal_paragraphs() -> None:
    # Only the equal paragraphs need an ordinal. A neighbour whose own content
    # id is unique keeps its historical ordinal-less target, so a duplicated
    # occurrence never disables the rest of its section.
    payload = _payload()
    entry = _paragraph_entries(payload)[0]
    resolved = resolve_workspace_target(
        entry=entry, canonical_content=payload, operation="rewrite_paragraph"
    )
    assert resolved.target.occurrence_ordinal is None
    assert resolved.legacy_target_id is not None
    assert resolve_target(
        payload, resolved.target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    ).indexes == (0,)

    blocks, _enrichments, _outline = project_blocks(
        entries=_paragraph_entries(payload),
        synthesis=payload,
        enrichment=None,
    )
    projected = [block for block in blocks if block["kind"] == "paragraph"]
    assert [block["allowed_operations"] for block in projected] == [
        ["rewrite_paragraph", "delete_paragraph"]
    ] * 3


def test_rewriting_the_second_equal_paragraph_changes_only_it() -> None:
    payload = _payload()
    revised = _apply_second_paragraph(payload, 2, "Identical paragraph body, revised.")

    sections = revised["sections"]
    assert isinstance(sections, list)
    paragraphs = sections[0]["paragraphs"]
    assert isinstance(paragraphs, list)
    assert paragraphs[0] == payload["sections"][0]["paragraphs"][0]
    assert paragraphs[1] == payload["sections"][0]["paragraphs"][1]
    assert paragraphs[2] == {
        "text": "Identical paragraph body, revised.",
        "evidence_refs": [],
    }
    assert revised["lead"] == payload["lead"]
    assert revised["title"] == payload["title"]

    first_revised = _apply_second_paragraph(payload, 1, "Identical paragraph body, first.")
    first_paragraphs = first_revised["sections"][0]["paragraphs"]
    assert first_paragraphs[0] == paragraphs[0]
    assert first_paragraphs[1] == {
        "text": "Identical paragraph body, first.",
        "evidence_refs": [],
    }
    assert first_paragraphs[2] == payload["sections"][0]["paragraphs"][2]


def test_legacy_target_without_ordinal_is_still_refused_on_equal_paragraphs() -> None:
    payload = _payload()
    section_hash = canonical_content_sha256(payload["sections"][0])
    paragraph_id = canonical_content_sha256(payload["sections"][0]["paragraphs"][1])
    legacy_target = TargetedEditorialRevisionTarget(
        kind=_PARAGRAPH_KIND,
        stable_ids=(paragraph_id,),
        parent_id=section_hash,
    )

    with pytest.raises(ValueError, match="ambiguous_section_paragraph"):
        resolve_target(payload, legacy_target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH)
    # The legacy writers share this exact resolver: an ordinal-less target on
    # duplicated content is refused with the historical ambiguity error.
    with pytest.raises(ValueError, match="ambiguous_section_paragraph"):
        _resolve_target(
            payload, legacy_target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
        )


def test_stale_ordinal_refuses_instead_of_falling_back_on_its_sibling() -> None:
    payload = _payload()
    resolved = _resolved(payload, 2)
    target = resolved.target

    concurrently_edited = _apply_second_paragraph(payload, 2, "Edited by someone else.")
    with pytest.raises(ValueError, match="target_missing"):
        resolve_target(
            concurrently_edited,
            target,
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
        )

    sibling_deleted = {
        **payload,
        "sections": [
            {
                **payload["sections"][0],
                "paragraphs": payload["sections"][0]["paragraphs"][:2],
            }
        ],
    }
    # Ordinal 0 still resolves to the surviving equal paragraph; ordinal 1 has
    # no occurrence and refuses rather than reusing the survivor.
    assert resolve_target(
        sibling_deleted,
        _resolved(sibling_deleted, 1).target,
        TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
    ).indexes == (1,)
    with pytest.raises(ValueError, match="target_missing"):
        resolve_target(
            sibling_deleted,
            target,
            TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
        )


def test_historical_target_serialization_and_id_are_byte_identical() -> None:
    payload = _payload(duplicates=False)
    entry = _paragraph_entries(payload)[0]
    resolved = resolve_workspace_target(
        entry=entry, canonical_content=payload, operation="rewrite_paragraph"
    )
    target = resolved.target
    assert target.occurrence_ordinal is None
    historical = {
        "kind": "section_paragraph",
        "stable_ids": [target.stable_ids[0]],
        "parent_id": target.parent_id,
        "anchor_id": None,
        "insert_after": False,
    }
    assert target.to_json() == historical
    expected_id = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(historical)
    ).hexdigest()
    assert resolved.target_id == expected_id
    assert resolved.legacy_target_id == expected_id

    duplicate_payload = _payload()
    duplicate_id = canonical_content_sha256(duplicate_payload["sections"][0]["paragraphs"][1])
    ordinal_target = _resolved(duplicate_payload, 2).target
    ordinal_payload = ordinal_target.to_json()
    assert ordinal_payload["occurrence_ordinal"] == 1
    assert ordinal_payload["stable_ids"] == [duplicate_id]
    assert ordinal_payload["parent_id"] == canonical_content_sha256(
        duplicate_payload["sections"][0]
    )
