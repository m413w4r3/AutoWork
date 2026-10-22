from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from cti_app.application.figure_plan_projection import (
    FigurePlanEdgeV1,
    FigurePlanNodeV1,
    FigurePlanV1,
    FigurePlanValidationError,
    project_figure_plan,
)
from cti_app.domain.production_editorial_enrichment import (
    DiagramNodeRole,
    DiagramRelationDirection,
    DiagramRelationType,
    EnrichmentDiagramDirection,
    EnrichmentDiagramKind,
)
from cti_app.domain.production_synthesis import EvidenceKind, ExtractionEvidenceRefV1
from cti_app.infrastructure.d2_diagram_compiler import encode_d2_source

_REF_A = "e22d1be7-740c-4946-a8fa-9657a3c364e1"
_REF_B = "70930c38-799b-43d9-a840-9c6381dbcc67"


class _VerifiedReferences:
    def __init__(self) -> None:
        self.references = {
            _REF_A: ExtractionEvidenceRefV1(
                source_document_id=UUID("e22d1be7-740c-4946-a8fa-9657a3c364e1"),
                kind=EvidenceKind.FACT,
                evidence_key="a" * 64,
            ),
            _REF_B: ExtractionEvidenceRefV1(
                source_document_id=UUID("70930c38-799b-43d9-a840-9c6381dbcc67"),
                kind=EvidenceKind.EVENT,
                evidence_key="b" * 64,
            ),
        }

    def resolve_verified(self, reference: str) -> ExtractionEvidenceRefV1 | None:
        return self.references.get(reference)


def _fixture(
    *, direction: DiagramRelationDirection | None = None, refs: tuple[str, ...] = (_REF_A, _REF_B)
) -> FigurePlanV1:
    return FigurePlanV1(
        source="bridge",
        source_ref="bridge-consult-73",
        title="Infrastructure documentée",
        legend="Niveau de preuve : ARTICLE_DOCUMENTED (non observé). Pas de trames ni d'offsets.",
        kind=EnrichmentDiagramKind.INFRASTRUCTURE,
        direction=EnrichmentDiagramDirection.TOP_TO_BOTTOM,
        refs=refs,
        nodes=(
            FigurePlanNodeV1("client", "Client Chisel", DiagramNodeRole.MALWARE_TOOL, (_REF_A,)),
            FigurePlanNodeV1("relay", "Relais SSH", DiagramNodeRole.INFRASTRUCTURE, (_REF_B,)),
        ),
        edges=(
            FigurePlanEdgeV1(
                "client",
                "relay",
                "tunnel demandé",
                DiagramRelationType.FACTUAL,
                (_REF_A, _REF_B),
                direction,
            ),
        ),
    )


def test_figure_plan_projects_verified_refs_to_deterministic_boxed_d2() -> None:
    resolver = _VerifiedReferences()
    first = project_figure_plan(_fixture(), evidence_resolver=resolver)
    second = project_figure_plan(_fixture(), evidence_resolver=resolver)
    source = encode_d2_source(first).decode("utf-8")

    assert first.key == second.key
    assert source == encode_d2_source(second).decode("utf-8")
    assert source.count("shape: rectangle") == 2
    assert "n001 -- n002" in source
    assert "n001 -> n002" not in source
    assert "e22d1be7-740c-4946-a8fa-9657a3c364e1" not in source
    assert "a" * 64 not in source
    assert first.nodes[0].evidence_refs == (resolver.references[_REF_A],)


def test_figure_plan_adds_arrow_only_when_direction_is_explicit() -> None:
    diagram = project_figure_plan(
        _fixture(direction=DiagramRelationDirection.DIRECTED),
        evidence_resolver=_VerifiedReferences(),
    )

    source = encode_d2_source(diagram).decode("utf-8")
    assert "n001 -> n002" in source


def test_figure_plan_rejects_unverified_refs_and_identifiers_in_labels() -> None:
    with pytest.raises(FigurePlanValidationError, match="reference_unverified"):
        project_figure_plan(
            _fixture(refs=(_REF_A, _REF_B, "missing-ref")),
            evidence_resolver=_VerifiedReferences(),
        )

    with pytest.raises(FigurePlanValidationError, match="contains_identifier"):
        FigurePlanNodeV1(
            "node",
            "Artifact " + "c" * 40,
            DiagramNodeRole.DATA_ARTIFACT,
            (_REF_A,),
        )


def test_figure_plan_rejects_proof_level_repeated_in_nodes() -> None:
    with pytest.raises(FigurePlanValidationError, match="repeats_proof_level"):
        FigurePlanNodeV1(
            "client",
            "ARTICLE_DOCUMENTED client",
            DiagramNodeRole.MALWARE_TOOL,
            (_REF_A,),
        )

    plan = _fixture()
    with pytest.raises(FigurePlanValidationError, match="repeats_proof_level"):
        replace(plan, title="ARTICLE_DOCUMENTED infrastructure")
    with pytest.raises(FigurePlanValidationError, match="proof_legend_invalid"):
        replace(
            plan,
            legend=(
                "Niveau de preuve : ARTICLE_DOCUMENTED (non observé); "
                "ARTICLE_DOCUMENTED ne signifie pas flux observé."
            ),
        )
    with pytest.raises(FigurePlanValidationError, match="legend_uncertainty_missing"):
        replace(plan, legend="Niveau de preuve : ARTICLE_DOCUMENTED.")

    with pytest.raises(FigurePlanValidationError, match="repeats_proof_level"):
        FigurePlanNodeV1(
            "client",
            "Client documenté, non observé",
            DiagramNodeRole.ACTOR,
            (_REF_A,),
        )
