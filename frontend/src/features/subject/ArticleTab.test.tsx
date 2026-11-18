import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import type { SubjectContentResponse } from "../../api/subjectContent";
import type { PublicationDocumentV4 } from "../../api/production";
import { ArticleTab } from "./ArticleTab";

afterEach(() => vi.unstubAllGlobals());

const publication: PublicationDocumentV4 = {
  schema_version: "4",
  subject_id: "subject-sample-1",
  publication_language: "fr",
  title: "Dossier Sample Orchard",
  lead: [],
  sections: [],
  timeline: [],
  indicators: [
    {
      artifact_type: "domain",
      indicators: [
        {
          value: "sample.invalid",
          normalized_value: "sample.invalid",
          artifact_type: "domain",
          source_document_ids: [],
        },
      ],
    },
  ],
  sources: [],
  uncertainties: [],
  tables: [],
  diagrams: [],
  figures: [],
};

function contentResponse(
  publicationKind: "article" | "brief" | null,
): SubjectContentResponse {
  return {
    subject_id: "subject-sample-1",
    run_id: "run-sample-1",
    pipeline_generation: 1,
    artifact_id: "artifact-sample-1",
    artifact_version: 1,
    artifact_input_hash: "a".repeat(64),
    status: "verified",
    schema_version: "4",
    canonical_content: publication,
    publication_kind: publicationKind,
  };
}

function renderArticleTab(publicationKind: "article" | "brief" | null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(Response.json(contentResponse(publicationKind))),
    ),
  );
  return render(
    <QueryClientProvider client={client}>
      <ArticleTab subjectId="subject-sample-1" onOpenPipeline={vi.fn()} />
    </QueryClientProvider>,
  );
}

it("test_article_tab_uses_explicit_kind", async () => {
  renderArticleTab("brief");

  expect(await screen.findByText("Brève")).toBeInTheDocument();
  expect(screen.queryByText("Article")).not.toBeInTheDocument();
  expect(screen.queryByText(/ANNEXE TECHNIQUE/)).not.toBeInTheDocument();
});

it("test_missing_legacy_kind_is_read_only", async () => {
  renderArticleTab(null);

  expect(
    await screen.findByText("Type de publication indéterminé. Lecture seule."),
  ).toBeInTheDocument();
  expect(screen.queryByText("Article")).not.toBeInTheDocument();
  expect(screen.queryByText("Brève")).not.toBeInTheDocument();
  expect(screen.getByText(/ANNEXE TECHNIQUE/)).toBeInTheDocument();
});
