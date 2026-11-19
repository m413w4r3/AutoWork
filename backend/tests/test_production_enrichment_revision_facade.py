from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from cti_app.application.editorial_enrichment_revision import (
    assert_requested_placement,
    validate_editorial_enrichment_patch,
)
from cti_app.application.production_editorial_enrichment import (
    EditorialEnrichmentRevisionConflictError,
    EditorialEnrichmentWireParseResult,
)
from cti_app.application.production_enrichment_revision import (
    EditorialEnrichmentRevisionValidationError,
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_synthesis_revision import build_targeted_revision_target_catalog
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    editorial_enrichment_from_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
)
from cti_app.domain.production_synthesis import production_synthesis_to_json
from tests.test_production_editorial_enrichment_application import (
    _common_enrichment_request,
    _RecordingGateway,
    _revision_args,
    _revision_harness,
    _succeeded_text,
    _table_revision_wire,
)


@pytest.mark.asyncio
async def test_enrichment_facade_characterization_keeps_replay_and_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    assert isinstance(harness.service, ProductionEditorialEnrichmentRevisionService)
    base_bytes = await harness.world.store.read_bytes(harness.base.canonical_blob_id)

    mismatched_fence_args = _revision_args(harness)
    mismatched_fence_args["base_canonical_sha256"] = "0" * 64
    with pytest.raises(EditorialEnrichmentRevisionConflictError):
        await harness.service.revise(**mismatched_fence_args)
    assert harness.world.gateway.calls == []

    result = await harness.service.revise(**_revision_args(harness))
    replay = await harness.service.revise(**_revision_args(harness))

    assert result.artifact.id == replay.artifact.id
    assert len(harness.world.gateway.calls) == 1
    assert await harness.world.store.read_bytes(harness.base.canonical_blob_id) == base_bytes
    assert hashlib.sha256(base_bytes).hexdigest() == harness.base_sha256


@pytest.mark.asyncio
async def test_enrichment_validation_rejects_unsupported_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )

    with pytest.raises(EditorialEnrichmentRevisionValidationError) as rejected:
        validate_editorial_enrichment_patch(
            {"operation": "replace_html", "html": "<svg/>"},
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key="tools_table",
            evidence_pack=harness.evidence_pack,
            extraction=harness.world.extraction,
            synthesis=harness.world.synthesis,
            figure_catalog=(),
        )

    assert rejected.value.rejections == [
        {"block_id": "proposal", "reason_code": "revision_patch_shape_invalid"}
    ]

    with pytest.raises(EditorialEnrichmentRevisionValidationError) as missing_proposal:
        validate_editorial_enrichment_patch(
            EditorialEnrichmentWireParseResult(proposal=None, rejections=[]),
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key="tools_table",
            evidence_pack=harness.evidence_pack,
            extraction=harness.world.extraction,
            synthesis=harness.world.synthesis,
            figure_catalog=(),
        )

    assert missing_proposal.value.rejections == [
        {"block_id": "proposal", "reason_code": "revision_patch_shape_invalid"}
    ]
    assert harness.world.gateway.calls == []


@pytest.mark.asyncio
async def test_after_timeline_targeted_placement_matches_legacy_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    base = editorial_enrichment_from_json(harness.base_payload)
    expected_placement = EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_TIMELINE)
    candidate = replace(
        base,
        tables=(replace(base.tables[0], placement=expected_placement),),
    )
    targeted_request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.CREATE_TABLE,
        element_key=None,
        placement=TargetedEditorialRevisionPlacement(
            TargetedEditorialRevisionPlacementKind.AFTER_TIMELINE
        ),
    )

    assert_requested_placement(
        candidate=candidate,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        targeted_request=targeted_request,
        synthesis=harness.world.synthesis,
    )


@pytest.mark.asyncio
async def test_after_section_targeted_placement_resolves_section_id_to_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    first_section = harness.world.synthesis.sections[0]
    second_section = replace(first_section, heading="Execution details")
    synthesis = replace(
        harness.world.synthesis,
        sections=(first_section, second_section),
    )
    section_targets = build_targeted_revision_target_catalog(
        ProductionArtifactStage.SYNTHESIS.value,
        production_synthesis_to_json(synthesis),
    )
    second_section_target = next(
        item["target"] for item in section_targets if item["label"] == "Section · Execution details"
    )
    section_id = second_section_target["stable_ids"][0]

    base = editorial_enrichment_from_json(harness.base_payload)
    expected_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_SECTION,
        section_index=1,
    )
    candidate = replace(
        base,
        tables=(replace(base.tables[0], placement=expected_placement),),
    )
    targeted_request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.CREATE_TABLE,
        element_key=None,
        placement=TargetedEditorialRevisionPlacement(
            TargetedEditorialRevisionPlacementKind.AFTER_SECTION,
            section_id=section_id,
        ),
    )

    assert_requested_placement(
        candidate=candidate,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        targeted_request=targeted_request,
        synthesis=synthesis,
    )
