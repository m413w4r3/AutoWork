import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import type { Edition } from "../../api/editions";
import { EditionWorkflow } from "./EditionWorkflow";

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

const edition: Edition = {
  id: "edition-1",
  country: "Iran",
  country_code: "IR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "AMBER",
  languages: ["fr"],
  target_major_articles: 2,
  target_briefs: 3,
  previous_edition_id: null,
  source_profile: "default",
  status: "selection",
  version: 3,
  progress_percent: 30,
  allowed_transitions: ["production", "archived"],
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

const board = {
  groups: [
    {
      id: "group-1",
      edition_id: edition.id,
      title: "Campagne A",
      outcome: "new_subject" as const,
      status: "selected" as const,
      editorial_type: "brief" as const,
      subject_id: "subject-1",
      candidates: [],
      score: {
        impact: 0,
        novelty: 0,
        technical_depth: 0,
        hunting_potential: 0,
        actionability: 0,
        source_quality: 0,
        total: 0,
        justifications: {},
      },
      source_relationship_status: "verified" as const,
      needs_source_verification: false,
      needs_source_expansion: false,
      grouping_confidence: "high" as const,
      grouping_justification: "",
      historical_comparison: null,
      version: 1,
    },
  ],
  selected_briefs: 1,
  selected_major: 0,
  ignored: 0,
  undecided: 0,
  target_briefs: 3,
  target_major: 2,
  automatic_selection: false as const,
};

const batch = {
  batch_id: "batch-1",
  edition_id: edition.id,
  profile: "brief_auto",
  status: "queued" as const,
  phase: "initial" as const,
  next_dispatch_at: null,
  items: 1,
  completed: 0,
  needs_review: 0,
  failed: 0,
  cancelled: 0,
  item_details: [],
  created_at: "2026-08-29T10:00:00Z",
  started_at: null,
  finished_at: null,
};

afterEach(() => vi.unstubAllGlobals());

it("lance tous les sujets éligibles et invalide Edition après le POST", async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = urlOf(input);
    if (url.includes("/editorial-groups")) return Response.json(board);
    if (init?.method === "POST") return Response.json(batch);
    throw new Error(`Unexpected GET ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const user = userEvent.setup();

  render(
    <QueryClientProvider client={client}>
      <EditionWorkflow edition={edition} />
    </QueryClientProvider>,
  );

  const start = await screen.findByRole("button", {
    name: "Lancer la production de 1 article",
  });
  expect(
    screen.queryByRole("button", { name: /traiter/i }),
  ).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await user.click(start);

  await vi.waitFor(() => {
    expect(invalidate).toHaveBeenCalledWith({
      queryKey: ["edition", edition.id],
    });
  });
  const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
  expect(post?.[1]?.body).toBe(JSON.stringify({ subject_ids: null }));
});
