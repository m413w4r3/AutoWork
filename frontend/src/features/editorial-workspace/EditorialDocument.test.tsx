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
});
