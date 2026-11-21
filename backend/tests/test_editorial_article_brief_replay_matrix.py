"""T-M8-32 replay matrix: stable ARTICLE/BRIEF editorial oracles, fully offline.

The same typed editorial operations are prepared or resolved against fixed
synthetic pins for both publication kinds. No provider, gateway or network is
created; the only publication-specific restriction is the BRIEF figure cap.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_figure_catalog import (
    EditorialFigureCatalogError,
    archive_reference_for_catalog_item,
    assert_archive_reference_matches,
    validate_figure_insertion_limit,
)
from cti_app.application.editorial_proposal_adoption import (
    _proposal_pins_match,
    _proposal_stage,
)
from cti_app.application.editorial_proposals import _manual_wire_response
from cti_app.application.editorial_synthesis.targets import resolve_target
from cti_app.application.editorial_workspace.registry import (
    resolve_occurrence,
    resolve_workspace_target,
)
from cti_app.domain.editorial_proposal import (
    ManualBarChartPatch,
    ManualPatch,
    ManualSourceFigurePatch,
    ManualSourceFigureSelection,
    ManualTablePatch,
)
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EditorialEnrichmentElementKind,
    EnrichmentPlacementKind,
)
from cti_app.domain.production_editorial_revision import TargetedEditorialRevisionOperation
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.domain.publication_kind import PublicationKind
from tests.test_editorial_bar_chart import _bar_chart, _paragraph_enrichment_proposal_fixture
from tests.test_editorial_figure_adoption import _catalog_item
from tests.test_editorial_proposal_preparation import (
    _install_real_workspace_resolver,
    _setup,
)
from tests.test_editorial_table_insertion import _table_spec


@pytest.mark.asyncio
async def test_article_brief_editorial_replay_matrix() -> None:
    """Replay typed editorial operations against stable Article and Brief pins."""
    replay_results: dict[PublicationKind, tuple[tuple[str, int, str], ...]] = {}

    for publication_kind in (PublicationKind.ARTICLE, PublicationKind.BRIEF):
        # Reuse the existing in-memory proposal service and a fixed scripted
        # Bridge turn. No live gateway or provider is created by this test.
        setup = _setup()
        preparation, entries, shared_content = _install_real_workspace_resolver(setup)
        setup.current_snapshot.publication_kind = publication_kind
        title_entry = entries[0]
        target_resolution = resolve_workspace_target(
            entry=title_entry,
            canonical_content=shared_content,
            operation=TargetedEditorialRevisionOperation.REWRITE_TITLE.value,
        )
        canonical_target = resolve_target(
            shared_content,
            target_resolution.target,
            TargetedEditorialRevisionOperation.REWRITE_TITLE,
        )
        # The registry entry and the canonical resolver agree on the exact
        # bytes a rewrite must fence before any proposal can be prepared.
        assert target_resolution.before_hash == canonical_target.before_hash
        raw_response = _manual_wire_response(
            target_path=canonical_target.path,
            operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
            base_sha256=preparation.base_canonical_sha256,
            before_hash=target_resolution.before_hash,
            after="Synthetic title revised from documented content",
        )
        preparation = replace(
            preparation,
            target=target_resolution.target,
            target_id=target_resolution.legacy_target_id,
            before_hash=target_resolution.before_hash,
            operation=TargetedEditorialRevisionOperation.REWRITE_TITLE,
            raw_response=raw_response,
            strict_wire=True,
            idempotency_key=f"article-brief-rewrite-{publication_kind.value}",
            request_sha256="4" * 64,
        )
        # The fixture's model-run reader verifies the exact stored turn digest.
        setup.preparation(raw_response)
        rewrite = await setup.service.prepare_bridge_result(preparation)
        replay = await setup.service.prepare_bridge_result(preparation)

        assert rewrite.state.value == "proposed", rewrite.diagnostics
        assert replay.id == rewrite.id
        assert replay.patch_sha256 == rewrite.patch_sha256
        assert rewrite.operation == TargetedEditorialRevisionOperation.REWRITE_TITLE.value
        assert rewrite.base_artifact_version == 4
        assert rewrite.base_pin_hash == "5" * 64
        assert rewrite.model_run_id == setup.model_run_id
        assert rewrite.model_turn_id == setup.model_turn_id
        assert setup.repository.records == [rewrite]
        assert setup.canonical_writes == []

        # Tables, bar charts and archived figures use their production domain
        # types and the same anchored enrichment base for both publication kinds.
        enrichment_results: list[tuple[str, int, str]] = []
        evidence_refs = tuple(
            ExtractionEvidenceRefV1(
                source_document_id=uuid4(),
                kind=EvidenceKind.FACT,
                evidence_key=hashlib.sha256(f"synthetic-evidence-{index}".encode()).hexdigest(),
            )
            for index in range(2)
        )

        for operation, expected_element_kind in (
            (TargetedEditorialRevisionOperation.CREATE_TABLE, EditorialEnrichmentElementKind.TABLE),
            (
                TargetedEditorialRevisionOperation.CREATE_BAR_CHART,
                EditorialEnrichmentElementKind.CHART,
            ),
            (
                TargetedEditorialRevisionOperation.CREATE_SOURCE_FIGURE,
                EditorialEnrichmentElementKind.FIGURE,
            ),
        ):
            proposal, snapshot, entry, enrichment_artifact, _enrichment, synthesis = (
                _paragraph_enrichment_proposal_fixture(operation)
            )
            snapshot = replace(snapshot, publication_kind=publication_kind)
            assert snapshot.publication_kind is publication_kind
            resolution = resolve_occurrence(snapshot, entry.block_id)
            assert resolution is not None
            assert resolution.record.publication_kind is publication_kind
            target_resolution = resolve_workspace_target(
                entry=entry,
                canonical_content=synthesis,
                operation=operation.value,
            )

            if operation is TargetedEditorialRevisionOperation.CREATE_TABLE:
                typed_patch = ManualTablePatch(
                    anchor_block_id=entry.block_id,
                    table=_table_spec(entry.block_id, (evidence_refs[0],)),
                )
            elif operation is TargetedEditorialRevisionOperation.CREATE_BAR_CHART:
                chart = _bar_chart(evidence_refs=evidence_refs)
                chart = replace(
                    chart,
                    placement=replace(
                        chart.placement,
                        kind=EnrichmentPlacementKind.AFTER_PARAGRAPH,
                        anchor_block_id=entry.block_id,
                    ),
                )
                typed_patch = ManualBarChartPatch(anchor_block_id=entry.block_id, chart=chart)
            else:
                item = _catalog_item()
                figure_reference = archive_reference_for_catalog_item(item)
                assert assert_archive_reference_matches(figure_reference, item)
                typed_patch = ManualSourceFigurePatch(
                    anchor_block_id=entry.block_id,
                    figures=(
                        ManualSourceFigureSelection(
                            figure_id=UUID(item.figure_id),
                            caption="Synthetic archived figure",
                            order=0,
                        ),
                    ),
                )

            assert isinstance(ManualPatch(typed_patch=typed_patch), ManualPatch)
            assert typed_patch.operation is operation
            assert operation.paragraph_enrichment_element_kind is expected_element_kind
            assert target_resolution.target.element_kind is expected_element_kind
            assert _proposal_pins_match(proposal, resolution)
            assert proposal.base_artifact_version == enrichment_artifact.version == 1
            assert proposal.base_pin_hash == snapshot.workspace_pin_hash == "c" * 64
            assert (
                _proposal_stage(
                    proposal,
                    {"target": target_resolution.target.to_json()},
                    resolution,
                )
                is ProductionArtifactStage.EDITORIAL_ENRICHMENT
            )
            enrichment_results.append(
                (operation.value, proposal.base_artifact_version, proposal.base_pin_hash)
            )

        replay_results[publication_kind] = (
            (
                rewrite.operation,
                rewrite.base_artifact_version,
                rewrite.base_pin_hash,
            ),
            *enrichment_results,
        )

    assert set(replay_results) == {PublicationKind.ARTICLE, PublicationKind.BRIEF}
    assert replay_results[PublicationKind.ARTICLE] == replay_results[PublicationKind.BRIEF]

    # Figure count is the only publication-specific restriction in this matrix.
    for publication_kind in (PublicationKind.ARTICLE, PublicationKind.BRIEF):
        validate_figure_insertion_limit(
            publication_kind=publication_kind,
            integrated_count=2,
            requested_count=1,
            inventory_limit=12,
        )

    validate_figure_insertion_limit(
        publication_kind=PublicationKind.ARTICLE,
        integrated_count=3,
        requested_count=1,
        inventory_limit=12,
    )
    with pytest.raises(EditorialFigureCatalogError, match="brief_figure_limit_exceeded"):
        validate_figure_insertion_limit(
            publication_kind=PublicationKind.BRIEF,
            integrated_count=3,
            requested_count=1,
            inventory_limit=12,
        )
