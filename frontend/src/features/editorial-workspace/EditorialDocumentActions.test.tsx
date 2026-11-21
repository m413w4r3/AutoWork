import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { EditorialDocument } from "./EditorialDocument";

const paragraphId = "paragraph-occurrence";
const headingId = "heading-occurrence";
const titleId = "title-occurrence";

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: "subject-actions",
  production_run_id: "run-actions",
  publication_kind: "article",
  synthesis_artifact_id: "synthesis-actions",
  synthesis_version: 1,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [
    {
      block_id: titleId,
      kind: "title",
      parent_block_id: null,
      position: 0,
      text: "Synthetic title",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: titleId, legacy_target_id: null },
      before_hash: "b".repeat(64),
      allowed_operations: ["rewrite_title"],
      revision_status: "ready",
    },
    {
      block_id: headingId,
      kind: "section_heading",
      parent_block_id: null,
      position: 1,
      text: "Synthetic section",
      evidence_refs: [],
      chart: null,
      revision_target: { block_id: headingId, legacy_target_id: null },
      before_hash: "c".repeat(64),
      allowed_operations: ["rewrite_section", "rename_section"],
      revision_status: "ready",
    },
    {
      block_id: paragraphId,
      kind: "paragraph",
      parent_block_id: headingId,
      position: 2,
      text: "Synthetic paragraph body.",
      evidence_refs: [{ label: "Synthetic source" }],
      chart: null,
      revision_target: { block_id: paragraphId, legacy_target_id: null },
      before_hash: "d".repeat(64),
      allowed_operations: ["rewrite_paragraph", "insert_after", "create_table"],
      revision_status: "ready",
    },
  ],
  enrichments: [],
  document_outline: [],
  capabilities: {
    can_discuss: true,
    can_preview_revision: false,
    manual_edit: true,
    can_add_enrichment: true,
    can_preview_pdf: false,
    can_regenerate_pdf: false,
  },
  preview_status: "READY",
  impact_status: "none",
  pdf_status: "CURRENT",
  pdf_artifact_id: null,
  pdf_artifact_version: null,
  pdf_artifact_input_hash: null,
  pdf_artifact_canonical_sha256: null,
  pdf_render_id: null,
  pdf_render_identity: null,
  pdf_error_code: null,
  pdf_error_message: null,
  pdf_url: null,
  snapshot_id: "snapshot-actions",
};

function renderDocument({
  selectedBlockId = null,
  readOnly = false,
}: {
  selectedBlockId?: string | null;
  readOnly?: boolean;
} = {}) {
  const onSelectBlock = vi.fn<(blockId: string) => void>();
  const onStartEditing = vi.fn<(blockId: string) => void>();
  const onDiscuss = vi.fn<(blockId: string) => void>();
  const onOpenSources = vi.fn<(blockId: string) => void>();
  const onOpenHistory = vi.fn<(blockId: string) => void>();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditorialDocument
        snapshot={snapshot}
        selectedBlockId={selectedBlockId}
        drafts={{}}
        staleDraftIds={new Set()}
        editingBlockId={null}
        onSelectBlock={onSelectBlock}
        onDraftChange={() => undefined}
        onResumeDraft={() => undefined}
        onCancelDraft={() => undefined}
        readOnly={readOnly}
        onStartEditing={onStartEditing}
        onDiscuss={onDiscuss}
        onOpenSources={onOpenSources}
        onOpenHistory={onOpenHistory}
      />
    </QueryClientProvider>,
  );
  return {
    onSelectBlock,
    onStartEditing,
    onDiscuss,
    onOpenSources,
    onOpenHistory,
  };
}

function blockElement(blockId: string): HTMLElement {
  const element = document.querySelector<HTMLElement>(
    `.editorial-document__block[data-block-id="${blockId}"]`,
  );
  if (!element) throw new Error(`Missing block ${blockId}`);
  return element;
}

describe("EditorialDocument block action bar", () => {
  it("shows the light action bar for the selected block with real actions only", () => {
    const callbacks = renderDocument({ selectedBlockId: paragraphId });
    const bar = screen.getByRole("toolbar", {
      name: /Actions du bloc/i,
    });
    expect(within(bar).getByRole("button", { name: "Modifier" })).toBeVisible();
    expect(within(bar).getByRole("button", { name: "Discuter" })).toBeVisible();
    expect(
      within(bar).getByRole("button", { name: "Ajouter après" }),
    ).toBeVisible();
    expect(within(bar).getByRole("button", { name: "Sources" })).toBeVisible();
    expect(within(bar).getByText("···")).toBeVisible();

    fireEvent.click(within(bar).getByRole("button", { name: "Modifier" }));
    expect(callbacks.onStartEditing).toHaveBeenCalledWith(paragraphId);
    fireEvent.click(within(bar).getByRole("button", { name: "Discuter" }));
    expect(callbacks.onDiscuss).toHaveBeenCalledWith(paragraphId);
    fireEvent.click(within(bar).getByRole("button", { name: "Sources" }));
    expect(callbacks.onOpenSources).toHaveBeenCalledWith(paragraphId);
  });

  it("moves the single action bar to the hovered block without selecting it", () => {
    const callbacks = renderDocument();
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
    fireEvent.mouseEnter(blockElement(paragraphId));
    const bar = screen.getByRole("toolbar", { name: /Actions du bloc/i });
    expect(within(bar).getByRole("button", { name: "Modifier" })).toBeVisible();
    expect(callbacks.onSelectBlock).not.toHaveBeenCalled();
    fireEvent.mouseLeave(blockElement(paragraphId));
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("hides the direct edit for a section heading instead of showing a dead button", () => {
    renderDocument({ selectedBlockId: headingId });
    const bar = screen.getByRole("toolbar", { name: /Actions du bloc/i });
    expect(
      within(bar).queryByRole("button", { name: "Modifier" }),
    ).not.toBeInTheDocument();
    expect(
      within(bar).queryByRole("button", { name: "Ajouter après" }),
    ).not.toBeInTheDocument();
    expect(
      within(bar).queryByRole("button", { name: "Discuter" }),
    ).toBeVisible();
  });

  it("offers no write action in a read-only workspace", () => {
    renderDocument({ selectedBlockId: paragraphId, readOnly: true });
    const bar = screen.queryByRole("toolbar");
    if (bar) {
      expect(
        within(bar).queryByRole("button", { name: "Modifier" }),
      ).not.toBeInTheDocument();
      expect(
        within(bar).queryByRole("button", { name: "Discuter" }),
      ).not.toBeInTheDocument();
      expect(
        within(bar).queryByRole("button", { name: "Ajouter après" }),
      ).not.toBeInTheDocument();
      expect(within(bar).queryByText("···")).not.toBeInTheDocument();
    }
  });

  it("opens a real overflow menu and keeps the selected block stable", () => {
    const callbacks = renderDocument({ selectedBlockId: paragraphId });
    fireEvent.click(screen.getByText("···"));
    fireEvent.click(screen.getByRole("button", { name: "Historique" }));
    expect(callbacks.onOpenHistory).toHaveBeenCalledWith(paragraphId);
    expect(blockElement(paragraphId)).toHaveAttribute("data-selected", "true");
  });
});
