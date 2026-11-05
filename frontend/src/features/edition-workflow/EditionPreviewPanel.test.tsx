import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { EditionPreviewPanel } from "./EditionPreviewPanel";

afterEach(() => vi.unstubAllGlobals());

const HASH = "a".repeat(64);

function briefDocument(title: string) {
  return {
    schema_version: "7",
    publication_kind: "brief",
    routing_decision_hash: "b".repeat(64),
    routing_policy_version: "routing-policy-v1",
    base_publication: {
      schema_version: "6",
      subject_id: "subject-brief",
      publication_language: "fr",
      title,
      lead: [],
      sections: [],
      timeline: [],
      indicators: [],
      sources: [],
      uncertainties: [],
      tables: [],
      diagrams: [],
      figures: [],
      references: [],
      original_indicators: [],
      rich_text: {
        schema_version: "1",
        policy_version: "semantic-policy-v1",
        paragraphs: [],
      },
    },
  };
}

function articleDocument(title: string) {
  return {
    schema_version: "7",
    publication_kind: "article",
    routing_decision_hash: "c".repeat(64),
    routing_policy_version: "routing-policy-v1",
    base_publication: {
      ...briefDocument(title).base_publication,
      subject_id: "subject-article",
    },
  };
}

it("groups mixed publications and keeps backend positions continuous", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        edition_id: "edition-1",
        edition_version: 4,
        composition_id: "composition-1",
        composition_version: 2,
        preview_input_hash: HASH,
        artifacts: [],
        document: {
          schema_version: "2",
          edition: {
            id: "edition-1",
            country: "France",
            country_code: "FR",
            period_start: "2026-10-01",
            period_end: "2026-10-31",
            tlp: "GREEN",
            languages: ["fr"],
            state: "OPEN",
            version: 4,
            created_at: "2026-10-01T00:00:00Z",
            updated_at: "2026-10-01T00:00:00Z",
          },
          publications: [
            {
              position: 2,
              subject_id: "subject-brief",
              document: briefDocument("Chronologie courte"),
            },
            {
              position: 1,
              subject_id: "subject-article",
              document: articleDocument("Analyse détaillée"),
            },
          ],
        },
        partial: false,
        missing_subject_ids: [],
        deferred_subject_ids: [],
        stale: false,
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  render(
    <QueryClientProvider client={client}>
      <EditionPreviewPanel editionId="edition-1" compositionPreview />
    </QueryClientProvider>,
  );

  const articleGroup = await screen.findByRole("region", { name: "ARTICLES" });
  const briefGroup = screen.getByRole("region", { name: "BRÈVES" });
  expect(
    within(articleGroup).getByRole("heading", { name: "Article 01" }),
  ).toBeInTheDocument();
  expect(
    within(briefGroup).getByRole("heading", { name: "Brève 02" }),
  ).toBeInTheDocument();
  expect(within(articleGroup).getByText("Format : Article")).toBeInTheDocument();
  expect(within(briefGroup).getByText("Format : Brève")).toBeInTheDocument();
});
