import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProductionQueue } from "./ProductionQueue";

const EDITION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";

const BRIEFS = [
  { subjectId: "s-1", title: "TAG-182 / MarkiRAT" },
  { subjectId: "s-2", title: "Cavern Manticore" },
  { subjectId: "s-3", title: "GigaWiper" },
];

function renderQueue(selectedSubjects: string[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ProductionQueue
        editionId={EDITION_ID}
        briefs={BRIEFS}
        selectedSubjects={selectedSubjects}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("ProductionQueue", () => {
  it("propose de traiter toutes les brèves quand aucun lot n'existe", async () => {
    // No batch yet is the normal entry state, not an error.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    );

    renderQueue();

    expect(
      await screen.findByRole("button", { name: "Traiter les 3 brèves" }),
    ).toBeInTheDocument();
  });

  it("n'envoie que les sujets cochés dans le board", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(Response.json({}, { status: 200 }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderQueue(["s-2"]);

    await user.click(
      await screen.findByRole("button", {
        name: "Traiter les 1 sélectionnées",
      }),
    );

    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(post).toBeDefined();
    expect(
      JSON.parse(typeof post?.[1]?.body === "string" ? post[1].body : "{}"),
    ).toEqual({ subject_ids: ["s-2"] });
  });

  it("affiche la file sujet par sujet avec son état", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          batch_id: "b-1",
          edition_id: EDITION_ID,
          profile: "brief_auto",
          status: "running",
          items: 3,
          completed: 1,
          needs_review: 0,
          failed: 0,
          cancelled: 0,
          current_subject_index: 1,
          created_at: "2026-08-10T10:00:00Z",
          started_at: "2026-08-10T10:00:00Z",
          finished_at: null,
          item_details: [
            {
              position: 1,
              subject_id: "s-1",
              title: "TAG-182 / MarkiRAT",
              run_id: "r-1",
              status: "ready",
              current_stage: "assembly",
            },
            {
              position: 2,
              subject_id: "s-2",
              title: "Cavern Manticore",
              run_id: "r-2",
              status: "running",
              current_stage: "references",
            },
            {
              position: 3,
              subject_id: "s-3",
              title: "GigaWiper",
              run_id: "r-3",
              status: "queued",
              current_stage: "sources",
            },
          ],
        }),
      ),
    );

    renderQueue();

    expect(await screen.findByText("TAG-182 / MarkiRAT")).toBeInTheDocument();
    expect(screen.getByText("Prête")).toBeInTheDocument();
    expect(screen.getByText("En cours · references")).toBeInTheDocument();
    expect(screen.getByText("1/3")).toBeInTheDocument();
    expect(screen.getByText("3/3")).toBeInTheDocument();
  });
});
