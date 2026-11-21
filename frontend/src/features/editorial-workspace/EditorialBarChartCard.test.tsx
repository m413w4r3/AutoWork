import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  EditorialEvidenceReference,
  EditorialProposalRead,
} from "../../api/editorialWorkspace";
import { EditorialProposalCard } from "./EditorialProposalCard";

const anchorId = "22222222-2222-4222-8222-222222222222";
const evidence: EditorialEvidenceReference = {
  source_document_id: "11111111-1111-4111-8111-111111111111",
  kind: "fact",
  evidence_key: "a".repeat(64),
};

const proposal: EditorialProposalRead = {
  id: "44444444-4444-4444-8444-444444444444",
  subject_id: "subject-1",
  production_run_id: "run-1",
  occurrence_id: anchorId,
  thread_id: null,
  origin: "manual_patch",
  state: "proposed",
  state_version: 1,
  operation: "create_bar_chart",
  base_artifact_id: "enrichment-1",
  base_artifact_version: 1,
  base_canonical_sha256: "d".repeat(64),
  base_text: null,
  patch_sha256: "e".repeat(64),
  patch: {
    schema_version: "editorial-patch-v1",
    operation: "create_bar_chart",
    target: { kind: "enrichment_element" },
    target_id: "target-chart-1",
    occurrence_id: anchorId,
    before_hash: "f".repeat(64),
    rationale: "Insertion structurée depuis les valeurs prouvées.",
    enrichment_patch: {
      charts: [
        {
          key: "chart-synthetic",
          kind: "bar",
          title: "Valeurs documentées",
          caption: null,
          category_axis_label: "Catégorie",
          value_axis_label: "Valeur",
          unit: "unités",
          points: [
            {
              label: "Premier",
              series: "Série",
              value: "4",
              evidence_handles: ["fact-1"],
            },
            {
              label: "Second",
              series: "Série",
              value: "8",
              evidence_handles: ["fact-2"],
            },
          ],
          placement: { kind: "after_paragraph", anchor_block_id: anchorId },
          purpose: { question: "Comparer les valeurs documentées." },
        },
      ],
    },
    evidence_refs: [evidence],
  },
  diagnostics: {},
  created_at: "2026-11-21T12:00:00Z",
};

function requestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

describe("EditorialProposalCard chart comparison", () => {
  it("shows the bars, labels, unit and consultable evidence before accepting", async () => {
    const fetchMock = vi.fn<typeof fetch>((input, init) => {
      const url = requestUrl(input);
      if (
        url.endsWith(`/proposals/${proposal.id}`) &&
        init?.method !== "POST"
      ) {
        return Promise.resolve(
          new Response(JSON.stringify(proposal), { status: 200 }),
        );
      }
      throw new Error(`Unexpected request ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <EditorialProposalCard
          subjectId="subject-1"
          occurrenceId={anchorId}
          proposalId={proposal.id}
          anchorText="Paragraphe d’ancrage."
        />
      </QueryClientProvider>,
    );

    const comparison = await screen.findByRole("region", {
      name: "Comparaison de l’insertion de graphique",
    });
    expect(within(comparison).getByText("Paragraphe d’ancrage.")).toBeVisible();
    expect(within(comparison).getByText("Valeurs documentées")).toBeVisible();
    expect(within(comparison).getByText("Après ce paragraphe")).toBeVisible();
    expect(within(comparison).getByText("Premier")).toBeVisible();
    expect(within(comparison).getByText("8 unités")).toBeVisible();
    expect(within(comparison).getByText("Série")).toBeVisible();
    const card = screen.getByRole("article", {
      name: "Proposition éditoriale",
    });
    expect(
      within(card).getByRole("button", { name: "Accepter" }),
    ).toBeEnabled();
    expect(within(card).getByRole("button", { name: "Rejeter" })).toBeEnabled();

    const evidenceDetails = within(comparison)
      .getByText("Preuves associées")
      .closest("details");
    expect(evidenceDetails).not.toHaveAttribute("open");
    fireEvent.click(within(comparison).getByText("Preuves associées"));
    expect(
      within(evidenceDetails as HTMLElement).getByText(
        "Source 11111111-1111-4111-8111-111111111111 · fact",
      ),
    ).toBeVisible();
    expect(
      within(evidenceDetails as HTMLElement).getByText(evidence.evidence_key),
    ).toBeVisible();
  });
});
