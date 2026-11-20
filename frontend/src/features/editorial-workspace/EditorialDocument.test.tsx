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
});
