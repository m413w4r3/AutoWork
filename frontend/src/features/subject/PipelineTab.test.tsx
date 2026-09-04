import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PipelineTab } from "./PipelineTab";

const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const EDITION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SOURCE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const BLOCKED_URL = "https://blocked.example/report";

const production = {
  subject_id: SUBJECT_ID,
  edition_id: EDITION_ID,
  title: "Sujet bloqué",
  status: "needs_review",
  current_stage: "sources",
  progress_current: 0,
  progress_total: 5,
  references_conversation_id: null,
  synthesis_conversation_id: null,
  run_id: "run-1",
  pipeline_generation: 1,
  created_at: "2026-09-04T10:00:00Z",
  started_at: "2026-09-04T10:00:00Z",
  finished_at: "2026-09-04T10:01:00Z",
  error_code: "source_collection_no_success",
  error_message: "No source was archived",
  error_details: null,
  recovery_disposition: "manual_only",
  warnings: [],
  stages: {
    sources: {
      status: "needs_review",
      version: null,
      error_code: "source_collection_no_success",
      error_message: "No source was archived",
    },
  },
};

const workbench = {
  subject_id: SUBJECT_ID,
  sources: [
    {
      id: SOURCE_ID,
      requested_url: BLOCKED_URL,
      state: "blocked",
      title: "Rapport bloqué",
    },
    {
      id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
      requested_url: "https://ok.example/report",
      state: "completed",
      title: "Rapport archivé",
    },
  ],
  claims: [],
  indicators: [],
};

function renderPipeline() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <PipelineTab subjectId={SUBJECT_ID} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("PipelineTab source replacement", () => {
  it("affiche les états, remplace une source puis propose la relance", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.includes("/workbench")) {
        return Promise.resolve(Response.json(workbench));
      }
      if (init?.method === "PATCH") {
        return Promise.resolve(
          Response.json({
            source: { url: "https://mirror.example/report" },
            updated_subject_ids: [SUBJECT_ID],
          }),
        );
      }
      if (init?.method === "POST") {
        return Promise.resolve(
          Response.json({ run_id: "run-2", replaced_run_id: "run-1" }),
        );
      }
      return Promise.resolve(Response.json(production));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPipeline();

    expect(
      await screen.findByRole("heading", {
        name: "Remplacer une source inaccessible",
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Rapport bloqué")).toBeInTheDocument();
    expect(screen.getByText("État : blocked")).toBeInTheDocument();
    expect(screen.getByText("État : completed")).toBeInTheDocument();

    await user.type(
      screen.getByLabelText("Nouvelle URL"),
      "https://mirror.example/report",
    );
    await user.click(screen.getByRole("button", { name: "Remplacer" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/editions/${EDITION_ID}/discovery/candidates/${SUBJECT_ID}/sources/replacement`,
        expect.objectContaining({
          method: "PATCH",
          body: JSON.stringify({
            replaced_canonical_url: BLOCKED_URL,
            url: "https://mirror.example/report",
          }),
        }),
      ),
    );
    expect(
      await screen.findByRole("button", { name: "Relancer la production" }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Relancer la production" }),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/production/subjects/${SUBJECT_ID}/production/restart-with-new-sources`,
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("ne l’affiche pas pour un autre code d’échec", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          ...production,
          error_code: "q2_source_coverage_failed",
          stages: {
            sources: {
              ...production.stages.sources,
              error_code: "q2_source_coverage_failed",
            },
          },
        }),
      ),
    );
    renderPipeline();

    await screen.findByRole("heading", { name: "Sujet bloqué" });
    expect(
      screen.queryByRole("heading", {
        name: "Remplacer une source inaccessible",
      }),
    ).not.toBeInTheDocument();
  });
});
