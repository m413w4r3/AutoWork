from __future__ import annotations

import hashlib

import pytest

from cti_app.application.editorial_enrichment_revision import (
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
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from tests.test_production_editorial_enrichment_application import (
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
