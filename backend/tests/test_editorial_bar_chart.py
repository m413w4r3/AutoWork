from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest

from cti_app.domain.production_editorial_enrichment import (
    ChartKind,
    ChartPointV1,
    ChartSpecV1,
    EditorialAnalyticPurposeV1,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure import analytic_chart_renderer


def _reference() -> ExtractionEvidenceRefV1:
    return ExtractionEvidenceRefV1(
        source_document_id=uuid4(),
        kind=EvidenceKind.FACT,
        evidence_key=hashlib.sha256(b"synthetic bar-chart evidence").hexdigest(),
    )


def _bar_chart(
    *,
    values: tuple[str, ...] = ("12", "18"),
    evidence_refs: tuple[ExtractionEvidenceRefV1, ...] | None = None,
) -> ChartSpecV1:
    anchor = uuid4()
    refs = evidence_refs if evidence_refs is not None else tuple(_reference() for _ in values)
    purpose = EditorialAnalyticPurposeV1(
        question="How many synthetic endpoints were documented?",
        available_data="Exact endpoint counts in the admitted evidence.",
        comprehension_gain="Compare the documented counts.",
        scope="Two synthetic source records.",
        evidence_refs=refs,
        knowledge_limits="No extrapolation beyond the cited values.",
        placement_reason="After the selected paragraph.",
    )
    return ChartSpecV1(
        key="synthetic_endpoint_counts",
        kind=ChartKind.BAR,
        title="Documented endpoint counts",
        caption="Synthetic evidence only.",
        placement=EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=anchor,
        ),
        purpose=purpose,
        points=tuple(
            ChartPointV1(
                label=f"Source {index}",
                date=None,
                series="Endpoints",
                value=Decimal(value),
                evidence_refs=(refs[min(index - 1, len(refs) - 1)],),
            )
            for index, value in enumerate(values, start=1)
        ),
        category_axis_label="Source record",
        value_axis_label="Documented count",
        unit="endpoints",
    )


def test_bar_data_requires_finite_evidence_backed_values() -> None:
    chart = _bar_chart()

    with pytest.raises(ValueError, match="finite"):
        _bar_chart(values=("NaN", "18"))

    from cti_app.application.editorial_proposals import _validate_bar_chart_evidence

    evidence_entries = {
        chart.points[0].evidence_refs[0]: {
            "value": "12",
            "evidence_quote": "The synthetic report documents 12 endpoints.",
        },
        chart.points[1].evidence_refs[0]: {
            "value": "18",
            "evidence_quote": "The synthetic report documents 18 endpoints.",
        },
    }
    admitted_refs = frozenset(evidence_entries)
    _validate_bar_chart_evidence(
        chart,
        evidence_entries=evidence_entries,
        admitted_refs=admitted_refs,
    )

    unsupported = _bar_chart(
        values=("12", "19"),
        evidence_refs=tuple(point.evidence_refs[0] for point in chart.points),
    )
    with pytest.raises(ValueError) as caught:
        _validate_bar_chart_evidence(
            unsupported,
            evidence_entries=evidence_entries,
            admitted_refs=admitted_refs,
        )
    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"
    assert getattr(caught.value, "validator_code", None) == "bar_chart_value_not_evidenced"

    substring_only_unit = replace(chart, unit="point")
    with pytest.raises(ValueError) as caught:
        _validate_bar_chart_evidence(
            substring_only_unit,
            evidence_entries=evidence_entries,
            admitted_refs=admitted_refs,
        )
    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"


def test_insufficient_data_refuses_without_invented_value() -> None:
    reference = _reference()

    with pytest.raises(ValueError) as caught:
        _bar_chart(values=("12",), evidence_refs=(reference,))

    assert getattr(caught.value, "code", None) == "bar_chart_insufficient_data"
    assert str(caught.value) == (
        "Aucun graphique quantitatif vérifiable ne peut être créé à partir des preuves actuelles. "
        "Un tableau descriptif peut être proposé à la place."
    )


def test_bar_svg_compilation_is_deterministic_and_escapes_labels() -> None:
    chart = _bar_chart()
    chart = replace(
        chart,
        points=(
            ChartPointV1(
                label="Source <A>",
                date=None,
                series="Endpoints",
                value=Decimal("12"),
                evidence_refs=chart.points[0].evidence_refs,
            ),
            chart.points[1],
        ),
    )

    first = analytic_chart_renderer.render_bar_svg(chart)
    second = analytic_chart_renderer.render_bar_svg(chart)

    assert first == second
    assert b"Source &lt;A&gt;" in first
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
