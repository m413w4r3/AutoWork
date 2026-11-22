import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { EditorialWorkspaceSnapshot } from "../../api/editorialWorkspace";
import { EditorialInspector } from "./EditorialInspector";
import type { InspectorTab } from "./EditorialInspector";

vi.mock("./EditorialConversation", () => ({
  EditorialConversation: () => (
    <div data-testid="conversation-panel">Conversation</div>
  ),
}));

vi.mock("./VisualInsertionComposer", () => ({
  VisualInsertionComposer: () => <button type="button">Ajouter après</button>,
}));

vi.mock("./EditorialHistory", () => ({
  EditorialHistory: () => (
    <div data-testid="history-panel">Historique des propositions</div>
  ),
}));

const paragraphId = "paragraph-tab";
const headingId = "heading-tab";

function block(
  blockId: string,
  kind: "paragraph" | "section_heading",
  text: string,
  allowedOperations: string[],
) {
  return {
    block_id: blockId,
    kind,
    parent_block_id: null,
    position: 0,
    text,
    evidence_refs: [{ label: "Synthetic source" }],
    chart: null,
    revision_target: { block_id: blockId, legacy_target_id: null },
    before_hash: "a".repeat(64),
    allowed_operations: allowedOperations,
    revision_status: "ready" as const,
  };
}

const snapshot: EditorialWorkspaceSnapshot = {
  subject_id: "subject-tabs",
  production_run_id: "run-tabs",
  publication_kind: "article",
  synthesis_artifact_id: "synthesis-tabs",
  synthesis_version: 1,
  synthesis_canonical_sha256: "a".repeat(64),
  enrichment_artifact_id: null,
  enrichment_version: null,
  publication_artifact_id: null,
  publication_version: null,
  pipeline_generation: 1,
  blocks: [
    block(paragraphId, "paragraph", "Synthetic paragraph.", [
      "rewrite_paragraph",
      "insert_after",
      "create_table",
    ]),
    block(headingId, "section_heading", "Synthetic heading.", [
      "rewrite_section",
      "rename_section",
    ]),
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
  snapshot_id: "snapshot-tabs",
};

function InspectorHarness({
  selectedBlockId = paragraphId,
  readOnly = false,
}: {
  selectedBlockId?: string | null;
  readOnly?: boolean;
}) {
  const [tab, setTab] = useState<InspectorTab>("discussion");
  const selected = snapshot.blocks.find(
    (candidate) => candidate.block_id === selectedBlockId,
  );
  const onStartEditing = vi.fn();
  return (
    <>
      <output data-testid="active-tab">{tab}</output>
      <EditorialInspector
        subjectId={snapshot.subject_id}
        snapshot={snapshot}
        block={selected ?? null}
        sectionTitle="Synthetic section"
        readOnly={readOnly}
        isEditing={false}
        localDraftText=""
        onLocalDraftChange={vi.fn()}
        onOpenFigureGallery={vi.fn()}
        onStartEditing={onStartEditing}
        onRefresh={vi.fn()}
        onBackToDocument={vi.fn()}
        onChangeTarget={vi.fn()}
        activeTab={tab}
        onTabChange={setTab}
        onOpenHistoryThread={vi.fn()}
      />
    </>
  );
}

function renderInspector(
  props: {
    selectedBlockId?: string | null;
    readOnly?: boolean;
  } = {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <InspectorHarness {...props} />
    </QueryClientProvider>,
  );
}

describe("EditorialInspector tabs", () => {
  it("renders the five contract tabs and defaults to the Bridge discussion", () => {
    renderInspector();
    expect(screen.getAllByRole("tab")).toHaveLength(5);
    for (const name of [
      "Discussion Bridge",
      "Modifier",
      "Visuels",
      "Sources",
      "Historique",
    ]) {
      expect(screen.getByRole("tab", { name })).toBeVisible();
    }
    expect(
      screen.getByRole("tab", { name: "Discussion Bridge" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("conversation-panel")).toBeVisible();
    expect(screen.queryByTestId("history-panel")).not.toBeInTheDocument();
  });

  it("switches panels without unmounting the discussion draft state", () => {
    renderInspector();
    fireEvent.click(screen.getByRole("tab", { name: "Sources" }));
    expect(screen.getByTestId("active-tab")).toHaveTextContent("sources");
    expect(
      screen.getByRole("heading", { name: "Sources et preuves" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Discussion Bridge" }));
    expect(screen.getByTestId("conversation-panel")).toBeVisible();
  });

  it("offers the inline edit from the Modifier tab and never a dead heading action", () => {
    const { rerender } = renderInspector();
    fireEvent.click(screen.getByRole("tab", { name: "Modifier" }));
    expect(
      screen.getByRole("button", { name: "Modifier le texte" }),
    ).toBeVisible();

    rerender(
      <QueryClientProvider
        client={
          new QueryClient({
            defaultOptions: { queries: { retry: false, gcTime: 0 } },
          })
        }
      >
        <InspectorHarness selectedBlockId={headingId} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Modifier" }));
    expect(
      screen.queryByRole("button", { name: "Modifier le texte" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/aucune opération canonique dédiée/i),
    ).toBeVisible();
  });

  it("keeps the composer reachable on every tab through the tab list", () => {
    renderInspector();
    const tablist = screen.getByRole("tablist", {
      name: "Panneaux de l’inspecteur",
    });
    expect(
      within(tablist).getByRole("tab", { name: "Discussion Bridge" }),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "Visuels" }));
    expect(screen.getByRole("button", { name: "Ajouter après" })).toBeVisible();
    expect(
      within(tablist).getByRole("tab", { name: "Discussion Bridge" }),
    ).toBeVisible();
  });

  it("supports ArrowRight/Home/End keyboard tab navigation", () => {
    renderInspector();
    const discussionTab = screen.getByRole("tab", {
      name: "Discussion Bridge",
    });
    discussionTab.focus();
    fireEvent.keyDown(discussionTab, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Modifier" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "Modifier" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.keyDown(screen.getByRole("tab", { name: "Modifier" }), {
      key: "End",
    });
    expect(screen.getByRole("tab", { name: "Historique" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Historique" }), {
      key: "Home",
    });
    expect(discussionTab).toHaveFocus();
  });

  it("explains the read-only state in the Modifier tab with no edit button", () => {
    renderInspector({ readOnly: true });
    fireEvent.click(screen.getByRole("tab", { name: "Modifier" }));
    expect(
      screen.queryByRole("button", { name: "Modifier le texte" }),
    ).toBeNull();
    expect(
      screen.getByText(/aucune modification canonique n’est possible/i),
    ).toBeVisible();
  });

  it("exposes the proposal history inline from the Historique tab", () => {
    renderInspector();
    fireEvent.click(screen.getByRole("tab", { name: "Historique" }));
    expect(screen.getByTestId("history-panel")).toBeVisible();
  });
});
