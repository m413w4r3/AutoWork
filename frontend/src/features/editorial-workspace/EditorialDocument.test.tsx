import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { EditorialDocument } from "./EditorialDocument";
import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: "subject-1",
  production_run_id: "run-1",
  publication_kind: "article",
  synthesis_artifact_id: "synthesis-1",
  synthesis_version: 1,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [
    {
      block_id: "first-occurrence",
      kind: "paragraph",
      parent_block_id: null,
      position: 0,
      text: "Repeated synthetic text",
      evidence_refs: [],
      chart: null,
      revision_target: {
        block_id: "first-occurrence",
        legacy_target_id: null,
      },
      before_hash: "b".repeat(64),
      allowed_operations: [],
      revision_status: "ready",
    },
    {
      block_id: "second-occurrence",
      kind: "paragraph",
      parent_block_id: null,
      position: 1,
      text: "Repeated synthetic text",
      evidence_refs: [],
      chart: null,
      revision_target: {
        block_id: "second-occurrence",
        legacy_target_id: null,
      },
      before_hash: "c".repeat(64),
      allowed_operations: [],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: true,
    can_preview_revision: false,
    manual_edit: true,
    can_add_enrichment: false,
    can_preview_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  snapshot_id: "snapshot-1",
};

describe("EditorialDocument", () => {
  it("test_click_selects_server_block_id", () => {
    const onSelectBlock = vi.fn<(blockId: string) => void>();
    render(
      <EditorialDocument
        snapshot={snapshot}
        selectedBlockId={null}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={onSelectBlock}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
      />,
    );

    const paragraphs = screen.getAllByText("Repeated synthetic text");
    const firstParagraph = paragraphs[0];
    const secondParagraph = paragraphs[1];
    if (!firstParagraph || !secondParagraph) {
      throw new Error("Expected two equal-text paragraph occurrences");
    }
    fireEvent.click(firstParagraph);
    fireEvent.click(secondParagraph);

    expect(onSelectBlock.mock.calls.map(([blockId]) => blockId)).toEqual([
      "first-occurrence",
      "second-occurrence",
    ]);
  });

  it("selects the focused occurrence with Enter and Space", () => {
    const onSelectBlock = vi.fn<(blockId: string) => void>();
    const { container } = render(
      <EditorialDocument
        snapshot={snapshot}
        selectedBlockId={null}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={onSelectBlock}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
      />,
    );

    const [firstBlock, secondBlock] = container.querySelectorAll<HTMLElement>(
      ".editorial-document__block",
    );
    if (!firstBlock || !secondBlock) {
      throw new Error("Expected two synthetic paragraph occurrences");
    }

    expect(firstBlock).toHaveAttribute("tabindex", "0");
    firstBlock.focus();
    fireEvent.keyDown(firstBlock, { key: "Enter" });
    fireEvent.keyDown(secondBlock, { key: " " });

    expect(onSelectBlock.mock.calls.map(([blockId]) => blockId)).toEqual([
      "first-occurrence",
      "second-occurrence",
    ]);
  });

  it("renders an inserted table in the exact order supplied by the snapshot", () => {
    const anchorBlock = snapshot.blocks[0];
    const followingBlock = snapshot.blocks[1];
    if (!anchorBlock || !followingBlock) {
      throw new Error("Expected the synthetic paragraph blocks");
    }
    const insertedTable = {
      block_id: "inserted-table-occurrence",
      kind: "table" as const,
      parent_block_id: null,
      position: 1,
      text: "Résumé vérifiable",
      evidence_refs: [],
      chart: null,
      table: {
        key: "table-synthetic",
        kind: "custom" as const,
        title: "Résumé vérifiable",
        caption: "Informations rapportées",
        columns: [
          { key: "item", label: "Élément" },
          { key: "value", label: "Valeur" },
        ],
        rows: [
          {
            cells: ["Échantillon", "Confirmé"],
            evidence_refs: [
              {
                source_document_id: "source-1",
                kind: "fact" as const,
                evidence_key: "d".repeat(64),
              },
            ],
          },
        ],
        evidence_refs: [],
        anchor_block_id: anchorBlock.block_id,
        placement: "after_paragraph" as const,
      },
      revision_target: {
        block_id: "inserted-table-occurrence",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready" as const,
    };
    const orderedSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      blocks: [anchorBlock, insertedTable, followingBlock],
    };

    const { container } = render(
      <EditorialDocument
        snapshot={orderedSnapshot}
        selectedBlockId={null}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={() => undefined}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
      />,
    );

    const orderedIds = Array.from(
      container.querySelectorAll<HTMLElement>(".editorial-document__block"),
    ).map((block) => block.dataset.blockId);
    expect(orderedIds).toEqual([
      anchorBlock.block_id,
      insertedTable.block_id,
      followingBlock.block_id,
    ]);
    expect(
      screen.getByRole("table", { name: "Résumé vérifiable" }),
    ).toBeVisible();
    expect(screen.getByText("Preuves de la ligne 1")).toBeVisible();
    fireEvent.click(screen.getByText("Preuves de la ligne 1"));
    expect(screen.getByText("d".repeat(64))).toBeVisible();
  });

  it("renders a chart block at the position supplied by the snapshot", () => {
    const anchorBlock = snapshot.blocks[0];
    const followingBlock = snapshot.blocks[1];
    if (!anchorBlock || !followingBlock) {
      throw new Error("Expected the synthetic paragraph blocks");
    }
    const insertedChart = {
      block_id: "inserted-chart-occurrence",
      kind: "chart" as const,
      parent_block_id: null,
      position: 1,
      text: "Valeurs documentées",
      evidence_refs: [],
      chart: {
        key: "chart-synthetic",
        kind: "bar" as const,
        title: "Valeurs documentées",
        caption: null,
        legend: ["Série synthétique"],
        category_axis_label: "Catégorie",
        value_axis_label: "Valeur",
        unit: "unités",
        points: [
          {
            label: "Premier",
            value: "4",
            evidence_refs: [
              {
                source_document_id: "11111111-1111-4111-8111-111111111111",
                kind: "fact" as const,
                evidence_key: "d".repeat(64),
              },
            ],
          },
          { label: "Second", value: "8", evidence_refs: [] },
        ],
        anchor_block_id: anchorBlock.block_id,
      },
      revision_target: {
        block_id: "inserted-chart-occurrence",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready" as const,
    };
    const orderedSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      blocks: [anchorBlock, insertedChart, followingBlock],
    };

    const { container } = render(
      <EditorialDocument
        snapshot={orderedSnapshot}
        selectedBlockId={null}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={() => undefined}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
      />,
    );

    const orderedIds = Array.from(
      container.querySelectorAll<HTMLElement>(".editorial-document__block"),
    ).map((block) => block.dataset.blockId);
    expect(orderedIds).toEqual([
      anchorBlock.block_id,
      insertedChart.block_id,
      followingBlock.block_id,
    ]);
    expect(
      screen.getByRole("figure", { name: "Graphique : Valeurs documentées" }),
    ).toBeVisible();
    expect(screen.getByText("Série synthétique")).toBeVisible();
    expect(screen.getByText("8 unités")).toBeVisible();
    const documentChart = screen.getByRole("figure", {
      name: "Graphique : Valeurs documentées",
    });
    const documentBars = Array.from(
      documentChart.querySelectorAll<HTMLElement>(".editorial-bar-chart__bar"),
    );
    expect(documentBars.map((bar) => bar.style.left)).toEqual(["0%", "0%"]);
    expect(documentBars.map((bar) => bar.style.width)).toEqual(["50%", "100%"]);
    expect(
      Array.from(
        documentChart.querySelectorAll<HTMLElement>(
          ".editorial-bar-chart__zero",
        ),
      ).map((axis) => axis.style.left),
    ).toEqual(["0%", "0%"]);
    fireEvent.click(screen.getByText(/Sources du point 1/));
    expect(screen.getByText(/d{64}/)).toBeVisible();
  });

  it("renders included figures in the exact snapshot order with local images and provenance", () => {
    const anchorBlock = snapshot.blocks[0];
    const followingBlock = snapshot.blocks[1];
    if (!anchorBlock || !followingBlock) {
      throw new Error("Expected the synthetic paragraph blocks");
    }
    const figureBlock = {
      block_id: "inserted-figure-occurrence",
      kind: "figure" as const,
      parent_block_id: null,
      position: 1,
      text: "Schéma réseau annoté",
      evidence_refs: [],
      chart: null,
      figure: {
        key: "adopted_source_figure_99999999999949998999999999999999",
        catalog_figure_id: "11111111-1111-4111-8111-111111111111",
        caption: "Schéma réseau annoté",
        source_url: "https://source.example/report",
        source: "Rapport synthétique",
        provenance: "Figure archivée, vérifiée et conservée depuis le rapport.",
        sha256: "f".repeat(64),
        mime_type: "image/png",
        byte_size: 1024,
        width: 800,
        height: 450,
        status: "included" as const,
        order: 0,
        anchor_block_id: anchorBlock.block_id,
        placement: "after_paragraph" as const,
        page: 4,
        section: "Infrastructure",
      },
      revision_target: {
        block_id: "inserted-figure-occurrence",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready" as const,
    };
    const orderedSnapshot: EditorialWorkspaceSnapshot = {
      ...snapshot,
      blocks: [anchorBlock, figureBlock, followingBlock],
    };

    const { container } = render(
      <EditorialDocument
        snapshot={orderedSnapshot}
        selectedBlockId={null}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={() => undefined}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
      />,
    );

    const orderedIds = Array.from(
      container.querySelectorAll<HTMLElement>(".editorial-document__block"),
    ).map((block) => block.dataset.blockId);
    expect(orderedIds).toEqual([
      anchorBlock.block_id,
      figureBlock.block_id,
      followingBlock.block_id,
    ]);
    expect(
      screen.getByRole("figure", { name: "Schéma réseau annoté" }),
    ).toBeVisible();
    expect(
      screen.getByRole("img", { name: "Schéma réseau annoté" }),
    ).toHaveAttribute(
      "src",
      "/api/subjects/subject-1/editor/figures/11111111-1111-4111-8111-111111111111/thumbnail",
    );
    expect(screen.getByText("Rapport synthétique")).toBeVisible();
    expect(
      screen.getByText(
        "Figure archivée, vérifiée et conservée depuis le rapport.",
      ),
    ).toBeVisible();
    expect(screen.getByText("800 × 450 px · image/png")).toBeVisible();
  });

  it("shows the caption and provenance when the catalog preview is missing", () => {
    const anchorBlock = snapshot.blocks[0];
    const followingBlock = snapshot.blocks[1];
    if (!anchorBlock || !followingBlock) {
      throw new Error("Expected the synthetic paragraph blocks");
    }
    const unavailableFigure = {
      block_id: "unavailable-figure-occurrence",
      kind: "figure" as const,
      parent_block_id: null,
      position: 1,
      text: "Figure sans aperçu",
      evidence_refs: [],
      chart: null,
      figure: {
        key: "adopted_source_figure_88888888888848888888888888888888",
        catalog_figure_id: null,
        caption: "Figure sans aperçu",
        source_url: "https://source.example/report",
        source: "Rapport synthétique",
        provenance: "Figure archivée, vérifiée et conservée depuis le rapport.",
        sha256: "f".repeat(64),
        mime_type: "image/png",
        byte_size: 1024,
        width: 800,
        height: 450,
        status: "included" as const,
        order: 0,
        anchor_block_id: anchorBlock.block_id,
        placement: "after_paragraph" as const,
        page: 4,
        section: "Infrastructure",
      },
      revision_target: {
        block_id: "unavailable-figure-occurrence",
        legacy_target_id: null,
      },
      before_hash: null,
      allowed_operations: [],
      revision_status: "ready" as const,
    };
    const { container } = render(
      <EditorialDocument
        snapshot={{
          ...snapshot,
          blocks: [anchorBlock, unavailableFigure, followingBlock],
        }}
        selectedBlockId={null}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={() => undefined}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
      />,
    );

    expect(screen.getByText("Aperçu indisponible")).toBeVisible();
    expect(screen.getByText("Figure sans aperçu")).toBeVisible();
    expect(
      screen.getByText(
        "Figure archivée, vérifiée et conservée depuis le rapport.",
      ),
    ).toBeVisible();
    expect(
      container.querySelector(".editorial-document__source-figure img"),
    ).toBeNull();
  });
});
