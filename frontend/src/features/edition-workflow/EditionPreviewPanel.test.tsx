import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { EditionPreviewPanel } from "./EditionPreviewPanel";

afterEach(() => vi.unstubAllGlobals());

const HASH = "a".repeat(64);

type PreviewPublication = {
  position: number;
  subject_id: string;
  document: object;
};

type PreviewClassification = {
  position: number;
  subject_id: string;
  kind: "article" | "brief";
  number: string;
  anchor: string;
};

function briefDocument(title: string, subjectId = "subject-brief") {
  return {
    schema_version: "7",
    publication_kind: "brief",
    routing_decision_hash: "b".repeat(64),
    routing_policy_version: "routing-policy-v1",
    base_publication: {
      schema_version: "6",
      subject_id: subjectId,
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

function articleDocument(title: string, subjectId = "subject-article") {
  return {
    ...briefDocument(title, subjectId),
    publication_kind: "article",
    base_publication: {
      ...briefDocument(title, subjectId).base_publication,
      subject_id: subjectId,
    },
  };
}

function legacyDocument(title: string, subjectId: string) {
  return {
    ...briefDocument(title, subjectId).base_publication,
    schema_version: "6",
  };
}

function renderPreview(
  publications: PreviewPublication[],
  classifications: PreviewClassification[],
) {
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
        classifications,
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
          publications,
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
}

it("uses backend groups, numbers, and anchors for mixed editions", async () => {
  renderPreview(
    [
      {
        position: 2,
        subject_id: "subject-brief",
        document: briefDocument("Chronologie courte"),
      },
      {
        position: 3,
        subject_id: "subject-article-2",
        document: articleDocument("Deuxième analyse", "subject-article-2"),
      },
      {
        position: 1,
        subject_id: "subject-article-1",
        document: articleDocument("Analyse détaillée", "subject-article-1"),
      },
    ],
    [
      {
        position: 1,
        subject_id: "subject-article-1",
        kind: "article",
        number: "01",
        anchor: "article01",
      },
      {
        position: 3,
        subject_id: "subject-article-2",
        kind: "article",
        number: "02",
        anchor: "article02",
      },
      {
        position: 2,
        subject_id: "subject-brief",
        kind: "brief",
        number: "03",
        anchor: "breve03",
      },
    ],
  );

  const articleGroup = await screen.findByRole("region", { name: "ARTICLES" });
  const briefGroup = screen.getByRole("region", { name: "BRÈVES" });
  expect(
    within(articleGroup).getByRole("heading", { name: "Article 01" }),
  ).toBeInTheDocument();
  expect(
    within(articleGroup).getByRole("heading", { name: "Article 02" }),
  ).toBeInTheDocument();
  expect(
    within(briefGroup).getByRole("heading", { name: "Brève 03" }),
  ).toBeInTheDocument();
  expect(document.getElementById("article01")).toBeInTheDocument();
  expect(document.getElementById("article02")).toBeInTheDocument();
  expect(document.getElementById("breve03")).toBeInTheDocument();
  expect(within(articleGroup).getAllByText("Format : Article")).toHaveLength(2);
  expect(within(briefGroup).getByText("Format : Brève")).toBeInTheDocument();
});

it("numbers articles and briefs from their respective backend groups", async () => {
  renderPreview(
    [
      {
        position: 2,
        subject_id: "article-2",
        document: articleDocument("Article 2", "article-2"),
      },
      {
        position: 1,
        subject_id: "article-1",
        document: articleDocument("Article 1", "article-1"),
      },
    ],
    [
      {
        position: 1,
        subject_id: "article-1",
        kind: "article",
        number: "01",
        anchor: "article01",
      },
      {
        position: 2,
        subject_id: "article-2",
        kind: "article",
        number: "02",
        anchor: "article02",
      },
    ],
  );

  const articleGroup = await screen.findByRole("region", { name: "ARTICLES" });
  expect(
    within(articleGroup).getByRole("heading", { name: "Article 01" }),
  ).toBeInTheDocument();
  expect(
    within(articleGroup).getByRole("heading", { name: "Article 02" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("region", { name: "BRÈVES" }),
  ).not.toBeInTheDocument();
});

it("numbers a briefs-only edition from the backend classification", async () => {
  renderPreview(
    [
      {
        position: 2,
        subject_id: "brief-2",
        document: briefDocument("Brève 2", "brief-2"),
      },
      {
        position: 1,
        subject_id: "brief-1",
        document: briefDocument("Brève 1", "brief-1"),
      },
    ],
    [
      {
        position: 1,
        subject_id: "brief-1",
        kind: "brief",
        number: "01",
        anchor: "breve01",
      },
      {
        position: 2,
        subject_id: "brief-2",
        kind: "brief",
        number: "02",
        anchor: "breve02",
      },
    ],
  );

  const briefGroup = await screen.findByRole("region", { name: "BRÈVES" });
  expect(
    within(briefGroup).getByRole("heading", { name: "Brève 01" }),
  ).toBeInTheDocument();
  expect(
    within(briefGroup).getByRole("heading", { name: "Brève 02" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("region", { name: "ARTICLES" }),
  ).not.toBeInTheDocument();
});

it("uses backend legacy classification for documents without an explicit kind", async () => {
  renderPreview(
    [
      {
        position: 1,
        subject_id: "legacy-brief",
        document: legacyDocument("Brève historique", "legacy-brief"),
      },
    ],
    [
      {
        position: 1,
        subject_id: "legacy-brief",
        kind: "brief",
        number: "01",
        anchor: "breve01",
      },
    ],
  );

  const briefGroup = await screen.findByRole("region", { name: "BRÈVES" });
  expect(
    within(briefGroup).getByRole("heading", { name: "Brève 01" }),
  ).toBeInTheDocument();
  expect(document.getElementById("breve01")).toBeInTheDocument();
  expect(within(briefGroup).getByText("Format : Brève")).toBeInTheDocument();
  expect(
    screen.queryByRole("region", { name: "ARTICLES" }),
  ).not.toBeInTheDocument();
});
