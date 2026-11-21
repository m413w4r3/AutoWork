import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  EditorialManualEditResult,
  EditorialWorkspaceSnapshot,
} from "../../api/editorialWorkspace";
import { EditorialWorkspacePage } from "./EditorialWorkspacePage";

vi.mock("./EditorialConversation", () => ({
  EditorialConversation: () => null,
}));
vi.mock("./EditorialServerDraftStatus", () => ({
  EditorialServerDraftStatus: () => null,
}));

const subjectId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const blockId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function createSnapshot(
  text: string,
  version: number,
  impactStatus: EditorialWorkspaceSnapshot["impact_status"],
): EditorialWorkspaceSnapshot {
  return {
    subject_id: subjectId,
    production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
    publication_kind: "article",
    synthesis_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    synthesis_version: version,
    synthesis_canonical_sha256: version === 1 ? "a".repeat(64) : "b".repeat(64),
    enrichment_artifact_id: null,
    enrichment_version: null,
    publication_artifact_id: null,
    publication_version: null,
    pipeline_generation: 1,
    blocks: [
      {
        block_id: blockId,
        kind: "paragraph",
        parent_block_id: null,
        position: 0,
        text,
        evidence_refs: [],
        chart: null,
        revision_target: { block_id: blockId, legacy_target_id: null },
        before_hash: "e".repeat(64),
        allowed_operations: ["rewrite_paragraph"],
        revision_status: "ready",
      },
    ],
    enrichments: [],
    document_outline: [],
    capabilities: {
      can_discuss: false,
      can_preview_revision: false,
      manual_edit: true,
      can_add_enrichment: false,
      can_preview_pdf: false,
      can_regenerate_pdf: false,
    },
    preview_status: "READY",
    impact_status: impactStatus,
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
    snapshot_id:
      version === 1
        ? "ffffffff-ffff-4fff-8fff-ffffffffffff"
        : "11111111-1111-4111-8111-111111111111",
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EditorialWorkspacePage manual editing", () => {
  it("sends the displayed snapshot identity and refreshes the canonical text and impact", async () => {
    const user = userEvent.setup();
    let currentSnapshot = createSnapshot(
      "Synthetic original paragraph.",
      1,
      "none",
    );
    const manualRequests: { url: string; body: Record<string, unknown> }[] = [];
    const editResult: EditorialManualEditResult = {
      id: "22222222-2222-4222-8222-222222222222",
      state: "accepted",
      state_version: 2,
      artifact_id: "33333333-3333-4333-8333-333333333333",
      artifact_version: 2,
      snapshot_id: "11111111-1111-4111-8111-111111111111",
      impact_id: "44444444-4444-4444-8444-444444444444",
      impact_state: "pending",
    };
    const jsonResponse = (value: unknown, status = 200) =>
      Promise.resolve(
        new Response(JSON.stringify(value), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((input, init) => {
        const url =
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url;
        if (url.endsWith("/editor/snapshot")) {
          return jsonResponse(currentSnapshot);
        }
        if (url.endsWith("/manual-edit") && init?.method === "POST") {
          if (typeof init.body !== "string") {
            throw new Error("Expected a JSON request body");
          }
          manualRequests.push({
            url,
            body: JSON.parse(init.body) as Record<string, unknown>,
          });
          currentSnapshot = createSnapshot(
            "Synthetic manually edited paragraph.",
            2,
            "pending",
          );
          return jsonResponse(editResult);
        }
        return jsonResponse({ detail: { code: "draft_not_found" } }, 404);
      }),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <EditorialWorkspacePage subjectId={subjectId} />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(
        document.querySelector(
          `.editorial-document__block[data-block-id="${blockId}"]`,
        ),
      ).toBeInTheDocument(),
    );
    const documentBlock = document.querySelector<HTMLElement>(
      `.editorial-document__block[data-block-id="${blockId}"]`,
    );
    if (!documentBlock)
      throw new Error("Expected the synthetic paragraph block");
    await user.click(documentBlock);
    await user.click(screen.getByRole("button", { name: "Modifier" }));
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    const editor = screen.getByRole("textbox", { name: "Texte du bloc" });
    await user.clear(editor);
    await user.type(editor, "Synthetic manually edited paragraph.");
    await user.click(screen.getByRole("button", { name: "Enregistrer" }));

    expect(
      (await screen.findAllByText("Synthetic manually edited paragraph."))
        .length,
    ).toBeGreaterThan(0);
    expect(await screen.findByText("Impact en attente")).toBeVisible();
    const confirmation = await screen.findByText(
      "Modification manuelle enregistrée (version 2).",
    );
    expect(confirmation).toHaveAttribute("data-proposal-id", editResult.id);
    expect(manualRequests).toHaveLength(1);
    expect(manualRequests[0]?.url).toContain(
      `/subjects/${subjectId}/editor/blocks/${blockId}/manual-edit`,
    );
    expect(manualRequests[0]?.body).toEqual({
      snapshot_id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
      operation: "rewrite_paragraph",
      text: "Synthetic manually edited paragraph.",
    });
    expect(documentBlock).toHaveAttribute("data-selected", "true");
    await waitFor(() => expect(document.activeElement).toBe(documentBlock));
    expect(
      screen.queryByRole("status", { name: /Brouillon local/ }),
    ).not.toBeInTheDocument();
  });
});
