import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  EditorialProposalOperation,
  EditorialVisualRevisionPatch,
  EditorialVisualRevisionProposalPatch,
} from "../../api/editorialWorkspace";
import { VisualProposalDiff } from "./VisualProposalDiff";
import { ANCHOR_ONE_ID, chartBlock, diagramBlock } from "./visualTestFixtures";

function operationFor(
  visualPatch: EditorialVisualRevisionPatch,
): EditorialProposalOperation {
  switch (visualPatch.kind) {
    case "revise_chart":
      return "rewrite_chart";
    case "revise_diagram":
      return "rewrite_diagram";
    case "remove_enrichment_element":
      return "delete_chart";
    default:
      return visualPatch.kind;
  }
}

function proposalPatch(
  visualPatch: EditorialVisualRevisionPatch,
): EditorialVisualRevisionProposalPatch {
  return {
    schema_version: "editorial-enrichment-visual-patch-v1",
    operation: operationFor(visualPatch),
    target: {},
    target_id: "target",
    occurrence_id: "occurrence",
    before_hash: "a".repeat(64),
    visual_patch: visualPatch,
    rationale: "Justification synthétique.",
  };
}

const chart = chartBlock();

const revisedChart: EditorialVisualRevisionPatch = {
  kind: "revise_chart",
  element_key: "chart-a",
  chart: {
    key: "chart-a",
    kind: "bar",
    title: "Titre corrigé du graphique",
    caption: "Légende corrigée",
    placement: { kind: "after_paragraph", section_index: null },
    purpose: {
      question: "Question synthétique ?",
      available_data: "Données synthétiques disponibles.",
      comprehension_gain: "Compréhension du volume observé.",
      scope: "Périmètre synthétique.",
      evidence_refs: [],
      knowledge_limits: "Limites synthétiques.",
      placement_reason: "Emplacement synthétique.",
    },
    category_axis_label: "Catégories",
    value_axis_label: "Valeurs",
    unit: "événements",
    points: [
      {
        label: "Catégorie 1",
        series: "Série A",
        value: "4",
        evidence_refs: [
          {
            source_document_id: "source-1",
            kind: "fact",
            evidence_key: "fact-1",
          },
        ],
      },
      {
        label: "Catégorie 2",
        series: "Série A",
        value: "1",
        evidence_refs: [],
      },
    ],
  },
};

function anchorLabel(blockId: string): string | null {
  return blockId === ANCHOR_ONE_ID ? "Paragraphe un" : null;
}

describe("VisualProposalDiff", () => {
  it("compares a chart revision field by field with values and evidence", () => {
    render(
      <VisualProposalDiff
        patch={proposalPatch(revisedChart)}
        currentElement={chart}
        currentGroup={[]}
        anchorLabel={anchorLabel}
        warnings={[]}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Comparaison du graphique" }),
    ).toBeVisible();
    expect(screen.getByText("Titre actuel du graphique")).toBeVisible();
    expect(screen.getByText("Titre corrigé du graphique")).toBeVisible();
    expect(screen.getAllByText("Modifié").length).toBeGreaterThan(0);

    const points = screen.getByRole("list", {
      name: "Valeurs du graphique proposé",
    });
    expect(within(points).getByText("Catégorie 1")).toBeVisible();
    expect(within(points).getByText("Catégorie 2")).toBeVisible();
    expect(within(points).getByText("Ajouté")).toBeVisible();
    expect(within(points).getByText("Modifié")).toBeVisible();
    expect(screen.getByText("Preuves utilisées")).toBeVisible();
  });

  it("renders a caption diff and its warnings", () => {
    render(
      <VisualProposalDiff
        patch={proposalPatch({
          kind: "update_visual_caption",
          element_kind: "chart",
          element_key: "chart-a",
          caption: "Légende proposée",
        })}
        currentElement={chart}
        currentGroup={[]}
        anchorLabel={anchorLabel}
        warnings={["La légende sera revérifiée à l’adoption."]}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Comparaison éditoriale" }),
    ).toBeVisible();
    expect(screen.getByText("Légende actuelle")).toBeVisible();
    expect(screen.getByText("Légende proposée")).toBeVisible();
    expect(
      screen.getByText("La légende sera revérifiée à l’adoption."),
    ).toBeVisible();
  });

  it("compares diagram nodes and relations", () => {
    render(
      <VisualProposalDiff
        patch={proposalPatch({
          kind: "revise_diagram",
          element_key: "diagram-a",
          diagram: {
            key: "diagram-a",
            kind: "network_flow",
            title: "Titre du diagramme",
            caption: "Légende du diagramme",
            direction: "left_to_right",
            placement: { kind: "after_paragraph", section_index: null },
            nodes: [
              {
                node_id: "n1",
                label: "Nœud 1 corrigé",
                evidence_refs: [
                  {
                    source_document_id: "source-1",
                    kind: "fact",
                    evidence_key: "fact-1",
                  },
                ],
              },
              { node_id: "n3", label: "Nœud 3" },
            ],
            edges: [
              {
                source_node_id: "n1",
                target_node_id: "n3",
                label: "appelle",
                relation_type: "factual",
                evidence_refs: [
                  {
                    source_document_id: "source-1",
                    kind: "fact",
                    evidence_key: "fact-1",
                  },
                ],
              },
            ],
            groups: [],
          },
        })}
        currentElement={diagramBlock()}
        currentGroup={[]}
        anchorLabel={anchorLabel}
        warnings={[]}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Comparaison du diagramme" }),
    ).toBeVisible();
    expect(screen.getByText("Nœud 1 corrigé")).toBeVisible();
    expect(screen.getByText("Nœud 3")).toBeVisible();
    expect(screen.getByText("Nœud 2")).toBeVisible();
    expect(screen.getAllByText("Ajouté").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Retiré").length).toBeGreaterThan(0);
    expect(screen.getByText("Preuves du diagramme")).toBeVisible();
    expect(screen.getByText("fact-1")).toBeInTheDocument();
  });

  it("shows current and proposed anchors for a move and flags an unknown anchor", () => {
    const { rerender } = render(
      <VisualProposalDiff
        patch={proposalPatch({
          kind: "move_enrichment_element",
          element_kind: "chart",
          element_key: "chart-a",
          anchor_block_id: "anchor-two",
        })}
        currentElement={chart}
        currentGroup={[]}
        anchorLabel={(blockId) =>
          blockId === ANCHOR_ONE_ID
            ? "Paragraphe un"
            : blockId === "anchor-two"
              ? "Paragraphe deux"
              : null
        }
        warnings={[]}
      />,
    );
    expect(screen.getByText("Paragraphe un")).toBeVisible();
    expect(screen.getByText("Paragraphe deux")).toBeVisible();

    rerender(
      <VisualProposalDiff
        patch={proposalPatch({
          kind: "move_enrichment_element",
          element_kind: "chart",
          element_key: "chart-a",
          anchor_block_id: "anchor-missing",
        })}
        currentElement={chart}
        currentGroup={[]}
        anchorLabel={anchorLabel}
        warnings={[
          "Le paragraphe d’ancrage proposé n’est plus présent dans cette version. La proposition ne peut pas être acceptée en l’état.",
        ]}
      />,
    );
    expect(
      screen.getByText("Paragraphe d’ancrage introuvable dans cette version"),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Le paragraphe d’ancrage proposé n’est plus présent dans cette version. La proposition ne peut pas être acceptée en l’état.",
      ),
    ).toBeVisible();
  });

  it("lists current and proposed order for a reorder", () => {
    render(
      <VisualProposalDiff
        patch={proposalPatch({
          kind: "reorder_enrichment_elements",
          anchor_block_id: ANCHOR_ONE_ID,
          order: [{ kind: "chart", key: "chart-a" }],
        })}
        currentElement={chart}
        currentGroup={[chart]}
        anchorLabel={anchorLabel}
        warnings={[]}
      />,
    );
    expect(screen.getByRole("region", { name: "Ordre actuel" })).toBeVisible();
    expect(screen.getByRole("region", { name: "Ordre proposé" })).toBeVisible();
    expect(
      screen.getAllByText("Titre actuel du graphique").length,
    ).toBeGreaterThan(1);
  });

  it("explains removal keeps the archived source", () => {
    render(
      <VisualProposalDiff
        patch={proposalPatch({
          kind: "remove_enrichment_element",
          element_kind: "chart",
          element_key: "chart-a",
        })}
        currentElement={chart}
        currentGroup={[]}
        anchorLabel={anchorLabel}
        warnings={[]}
      />,
    );
    expect(
      screen.getByText(
        "Retirer ce visuel du document ? Il restera dans les sources archivées et dans l’historique.",
      ),
    ).toBeVisible();
  });
});
