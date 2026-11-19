from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cti_app.application import production_synthesis_revision as synthesis_revision
from cti_app.application.editorial_workspace.registry import (
    project_blocks,
    resolve_workspace_target,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.editorial_workspace import (
    bootstrap_block_registry,
    canonical_content_sha256,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionConflictError,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)


def _paragraph(text: str) -> dict[str, object]:
    return {"text": text, "evidence_refs": []}


def _synthesis(paragraphs: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "schema_version": "2",
        "title": "Synthetic title",
        "lead": [_paragraph("Synthetic lead")],
        "sections": [
            {
                "kind": "narrative",
                "heading": "Synthetic heading",
                "paragraphs": list(paragraphs),
            }
        ],
        "timeline": [],
        "source_notes": [],
    }


def test_synthesis_facade_characterization_keeps_legacy_target_and_cas() -> None:
    content = _synthesis((_paragraph("unique synthetic paragraph"),))
    catalog = synthesis_revision.build_targeted_revision_target_catalog("synthesis", content)
    legacy_paragraph = next(
        item for item in catalog if item["label"].endswith("unique synthetic paragraph")
    )
    expected_legacy_target_id = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(legacy_paragraph["target"])
    ).hexdigest()
    assert legacy_paragraph["id"] == expected_legacy_target_id

    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256=canonical_content_sha256(content),
        source_stage="synthesis",
        canonical_content=content,
    )
    blocks, _enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=content,
        enrichment=None,
    )
    block = next(
        item
        for item in blocks
        if item["kind"] == "paragraph" and item["text"] == "unique synthetic paragraph"
    )
    assert block["revision_target"]["legacy_target_id"] == expected_legacy_target_id

    raw_target = legacy_paragraph["target"]
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind(raw_target["kind"]),
        stable_ids=tuple(raw_target.get("stable_ids", [])),
        parent_id=raw_target.get("parent_id"),
        anchor_id=raw_target.get("anchor_id"),
        insert_after=raw_target.get("insert_after", False),
    )
    operation = TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    patch = synthesis_revision._WirePatch(
        target="legacy-target-must-not-bypass-cas",
        action=operation,
        base_artifact_hash="0" * 64,
        before_hash="0" * 64,
        after="synthetic replacement",
        rationale="synthetic characterization",
    )
    with pytest.raises(TargetedEditorialRevisionConflictError) as error:
        synthesis_revision._apply_wire_patch(
            base_synthesis=None,  # type: ignore[arg-type]
            base_payload=content,
            target=target,
            operation=operation,
            patch=patch,
            evidence_pack=None,  # type: ignore[arg-type]
            extraction=None,  # type: ignore[arg-type]
            snapshot=None,  # type: ignore[arg-type]
        )
    assert str(error.value) == "The targeted element changed"

    with pytest.raises(synthesis_revision.TargetedSynthesisRevisionValidationError) as error:
        synthesis_revision._parse_wire_patch(
            "\n".join(
                (
                    "@@TARGET@@",
                    "target",
                    "@@ACTION@@",
                    "rewrite_paragraph",
                    "@@BASE_ARTIFACT_HASH@@",
                    "not-a-sha256",
                    "@@BEFORE_HASH@@",
                    "0" * 64,
                    "@@AFTER@@",
                    "replacement",
                    "@@RATIONALE@@",
                    "synthetic rationale",
                )
            )
        )
    assert error.value.reason_code == "revision_wire_hash_invalid"

    from cti_app.application.editorial_synthesis import patch_validation, targets

    assert synthesis_revision._resolve_target is targets.resolve_target
    assert synthesis_revision.build_targeted_revision_target_catalog is (
        targets.build_targeted_revision_target_catalog
    )
    assert synthesis_revision._parse_wire_patch is patch_validation.parse_wire_patch
    assert synthesis_revision._WirePatch is patch_validation.WirePatch


def test_synthesis_target_resolution_uses_registry_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_synthesis import targets

    content = _synthesis((_paragraph("same paragraph"), _paragraph("same paragraph")))
    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256=canonical_content_sha256(content),
        source_stage="synthesis",
        canonical_content=content,
    )
    paragraphs = [entry for entry in entries if entry.kind == "paragraph"]
    resolved_inputs: list[dict[str, object]] = []
    resolve_target = targets.resolve_target

    def record_target_resolution(payload, target, operation):
        resolved_inputs.append(payload)
        return resolve_target(payload, target, operation)

    monkeypatch.setattr(targets, "resolve_target", record_target_resolution)

    first = resolve_workspace_target(
        entry=paragraphs[0],
        canonical_content=content,
        operation="rewrite_paragraph",
    )
    second = resolve_workspace_target(
        entry=paragraphs[1],
        canonical_content=content,
        operation="rewrite_paragraph",
    )

    assert first.block_id == paragraphs[0].block_id
    assert second.block_id == paragraphs[1].block_id
    assert first.block_id != second.block_id
    assert first.path == "synthesis/sections/0/paragraphs/0"
    assert second.path == "synthesis/sections/0/paragraphs/1"
    assert first.before_hash == second.before_hash
    assert len(resolved_inputs) == 2
    assert all(len(payload["sections"][0]["paragraphs"]) == 1 for payload in resolved_inputs)
