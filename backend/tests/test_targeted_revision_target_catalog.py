from __future__ import annotations

import re
from typing import get_args
from uuid import uuid4

import pytest

from cti_app.api.editorial_workspace import EditorialHistoryItemView
from cti_app.application.production_synthesis_revision import (
    build_targeted_revision_target_catalog,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)
from cti_app.infrastructure.database.models.editorial_proposals import (
    EDITORIAL_PROPOSAL_OPERATIONS_SQL,
)


def test_synthesis_catalog_exposes_stable_target_ids_and_valid_operations() -> None:
    synthesis = {
        "title": "Titre initial",
        "lead": [{"text": "Ouverture", "evidence_refs": []}],
        "sections": [
            {
                "heading": "Contexte",
                "paragraphs": [{"text": "Premier fait", "evidence_refs": []}],
            },
            {
                "heading": "Activité",
                "paragraphs": [{"text": "Second fait", "evidence_refs": []}],
            },
        ],
    }

    first = build_targeted_revision_target_catalog("synthesis", synthesis)
    second = build_targeted_revision_target_catalog("synthesis", synthesis)

    assert first == second
    by_label = {item["label"]: item for item in first}
    assert by_label["Titre"]["operations"] == ["rewrite_title"]
    paragraph = next(item for item in first if item["label"].startswith("Paragraphe · Contexte"))
    assert paragraph["target"]["kind"] == "section_paragraph"
    assert len(paragraph["target"]["stable_ids"][0]) == 64
    assert paragraph["operations"] == ["rewrite_paragraph", "delete_paragraph"]
    assert any("Fusionner · Contexte + Activité" == item["label"] for item in first)


def test_enrichment_catalog_includes_tables_charts_figures_and_create_operations() -> None:
    enrichment = {
        "tables": [{"key": "table-a", "title": "Table A"}],
        "diagrams": [{"key": "diagram-a", "title": "Diagram A"}],
        "charts": [{"key": "chart-a", "title": "Chart A"}],
        "source_figures": [{"key": "figure-a", "caption": "Figure A"}],
    }

    targets = build_targeted_revision_target_catalog("editorial_enrichment", enrichment)
    by_label = {item["label"]: item for item in targets}

    assert by_label["Tableau · Table A"]["operations"] == ["rewrite_table", "delete_table"]
    assert by_label["Graphique · Chart A"]["target"]["element_kind"] == "chart"
    assert by_label["Figure source · Figure A"]["operations"] == [
        "rewrite_source_figure",
        "replace_source_figure",
        "delete_source_figure",
    ]
    create_diagram = by_label["Diagramme · créer"]
    assert create_diagram["operations"] == ["create_diagram"]
    assert create_diagram["target"]["placement"]["kind"] == "after_lead"


def test_catalog_for_an_unsupported_stage_is_empty() -> None:
    assert build_targeted_revision_target_catalog("extraction", {}) == []


def test_editorial_operation_vocabulary_matches_domain_check_and_api_literal() -> None:
    enum_values = {operation.value for operation in TargetedEditorialRevisionOperation}
    check_values = set(re.findall(r"'([a-z_]+)'", EDITORIAL_PROPOSAL_OPERATIONS_SQL))
    api_values = set(get_args(EditorialHistoryItemView.model_fields["operation"].annotation))

    assert enum_values == check_values == api_values
    assert len(enum_values) == 28
    assert {
        "update_visual_caption",
        "move_enrichment_element",
        "reorder_enrichment_elements",
    } <= enum_values


def test_enrichment_order_target_requires_a_paragraph_placement() -> None:
    anchor_block_id = uuid4()
    placement = TargetedEditorialRevisionPlacement(
        kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor_block_id,
    )

    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
        placement=placement,
    )

    payload = target.to_json()
    assert payload["kind"] == "enrichment_order"
    assert payload["element_kind"] is None
    assert payload["element_key"] is None
    assert payload["placement"]["anchor_block_id"] == str(anchor_block_id)

    with pytest.raises(ValueError, match="enrichment_order_placement_invalid"):
        TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
        )
    with pytest.raises(ValueError, match="enrichment_order_target_invalid"):
        TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
            element_key="table-a",
            placement=placement,
        )
    with pytest.raises(ValueError, match="enrichment_order_placement_invalid"):
        TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
            placement=TargetedEditorialRevisionPlacement(
                kind=TargetedEditorialRevisionPlacementKind.AFTER_LEAD
            ),
        )
