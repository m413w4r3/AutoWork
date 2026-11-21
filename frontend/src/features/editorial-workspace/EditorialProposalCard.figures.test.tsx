import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditorialProposalRead } from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";

const SUBJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const OCCURRENCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const PROPOSAL_ID = "proposal-figures";

const proposal: EditorialProposalRead = {
  id: PROPOSAL_ID,
  subject_id: SUBJECT_ID,
  production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  occurrence_id: OCCURRENCE_ID,
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "create_source_figure",
  base_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  base_artifact_version: 3,
  base_canonical_sha256: "a".repeat(64),
  base_text: null,
  patch_sha256: "b".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "create_source_figure",
    target: {},
    target_id: "target-1",
    occurrence_id: OCCURRENCE_ID,
    before_hash: "c".repeat(64),
    rationale: "Figures archivées ajoutées manuellement.",
    enrichment_patch: {
      manual_source_figures: [
        {
          figure_id: "11111111-1111-4111-8111-111111111111",
          caption: "Vue des échanges",
          order: 0,
          sha256: "e".repeat(64),
          mime_type: "image/png",
          byte_size: 4096,
          width: 1280,
          height: 720,
          source_document_id: "99999999-9999-4999-8999-999999999999",
          source_url: "https://source.example/report",
          provenance: "Figure archivée et vérifiée dans le rapport.",
        },
        {
          figure_id: "22222222-2222-4222-8222-222222222222",
          caption: "Vue de l’infrastructure",
          order: 1,
          sha256: "f".repeat(64),
          mime_type: "image/png",
          byte_size: 2048,
          width: 800,
          height: 450,
          source_document_id: "99999999-9999-4999-8999-999999999999",
          source_url: "https://source.example/report",
          provenance: "Figure archivée et vérifiée dans le rapport.",
        },
      ],
    },
  },
  diagnostics: {},
  created_at: "2026-11-21T08:00:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("EditorialProposalCard source figures", () => {
  it("compares each figure with readable provenance and folded archive identifiers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify(proposal), {
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    );
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <EditorialProposalCard
          subjectId={SUBJECT_ID}
          occurrenceId={OCCURRENCE_ID}
          proposalId={PROPOSAL_ID}
          anchorText="Paragraphe d’ancrage synthétique."
        />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Comparaison de l’insertion de figures",
      }),
    ).toBeVisible();
    expect(screen.getByText("Vue des échanges")).toBeVisible();
    expect(screen.getByText("Vue de l’infrastructure")).toBeVisible();
    expect(
      screen.getAllByText("Figure archivée et vérifiée dans le rapport."),
    ).toHaveLength(2);
    expect(screen.getByText(/1280 × 720 px · image\/png/)).toBeVisible();
    expect(
      screen.getByRole("img", { name: "Vue des échanges" }),
    ).toHaveAttribute(
      "src",
      "/api/subjects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/editor/figures/11111111-1111-4111-8111-111111111111/thumbnail",
    );
    expect(screen.queryByText("e".repeat(64))).toBeNull();

    await userEvent.click(screen.getByText("Détails de la proposition"));
    const patchDetails = screen.getByLabelText("Patch éditorial typé");
    expect(patchDetails).toHaveTextContent("e".repeat(64));
    expect(patchDetails).toHaveTextContent(
      "11111111-1111-4111-8111-111111111111",
    );
  });
});
