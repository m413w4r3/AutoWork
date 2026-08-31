import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { shouldPollProduction } from "../api/production";
import { SubjectProduction } from "./SubjectProduction";

const SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function renderProduction() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SubjectProduction subjectId={SUBJECT_ID} />
    </QueryClientProvider>,
  );
}

function status(
  runStatus: "queued" | "running" | "ready" | "needs_review" | "failed",
  currentStage = "extraction",
) {
  return {
    subject_id: SUBJECT_ID,
    title: "TAG-182 et MarkiRAT",
    status: runStatus,
    current_stage: currentStage,
    progress_current: 2,
    progress_total: 5,
    references_conversation_id: "c-1",
    synthesis_conversation_id: null,
    run_id: "r-1",
    pipeline_generation: 3,
    created_at: "2026-08-10T10:00:00Z",
    started_at: "2026-08-10T10:00:00Z",
    finished_at: runStatus === "running" ? null : "2026-08-10T10:00:00Z",
    warnings: [],
    stages: {
      sources: {
        status: "succeeded",
        version: null,
        error_code: null,
        error_message: null,
      },
      references: {
        status: "succeeded",
        version: null,
        error_code: null,
        error_message: null,
      },
      extraction: {
        status:
          runStatus === "failed"
            ? "failed"
            : runStatus === "needs_review"
              ? "needs_review"
              : "succeeded",
        version: null,
        error_code:
          runStatus === "failed"
            ? "extraction_failed"
            : runStatus === "needs_review"
              ? "needs_review_code"
              : null,
        error_message: null,
      },
      synthesis: {
        status: "pending",
        version: null,
        error_code: null,
        error_message: null,
      },
      assembly: {
        status: "pending",
        version: null,
        error_code: null,
        error_message: null,
      },
    },
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("SubjectProduction retry from stage", () => {
  it.each(["failed", "needs_review"] as const)(
    "%s extraction affiche l’action prioritaire et appelle le retry générique",
    async (runStatus) => {
      const fetchMock = vi.fn(
        (_input: RequestInfo | URL, init?: RequestInit) =>
          init?.method === "POST"
            ? Promise.resolve(Response.json({ status: "running" }))
            : Promise.resolve(Response.json(status(runStatus))),
      );
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();
      renderProduction();
      expect(await screen.findByRole("alert")).toHaveTextContent("Extraction");
      expect(
        screen.getByRole("button", { name: "Relancer cette étape" }),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/toutes les étapes suivantes seront recalculées/),
      ).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", { name: "Relancer cette étape" }),
      );
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          `/api/subjects/${SUBJECT_ID}/production/retry`,
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({ stage: "extraction" }),
          }),
        ),
      );
    },
  );

  it("READY affiche un menu avec les cinq étapes", async () => {
    const readyBase = status("ready", "assembly");
    const ready = {
      ...readyBase,
      stages: Object.fromEntries(
        Object.entries(readyBase.stages).map(([key, value]) => [
          key,
          { ...value, status: "succeeded" },
        ]),
      ),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json(ready)));
    renderProduction();
    const select = await screen.findByRole("combobox", {
      name: "Relancer depuis une étape",
    });
    expect(select).toHaveValue("");
    expect(screen.getAllByRole("option")).toHaveLength(6);
  });

  it("RUNNING n’affiche aucune action retry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(status("running"))),
    );
    renderProduction();
    await screen.findByText("TAG-182 et MarkiRAT");
    expect(screen.queryByText("Relancer depuis…")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Relancer cette étape" }),
    ).toBeNull();
  });

  it("RUNNING annule la tentative exacte exposée par le statut", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST"
        ? Promise.resolve(
            Response.json({
              action: "cancel",
              run_id: "r-1",
              status: "cancelled",
            }),
          )
        : Promise.resolve(Response.json(status("running"))),
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProduction();
    await screen.findByText("TAG-182 et MarkiRAT");
    await user.click(screen.getByRole("button", { name: "Annuler" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/production/runs/r-1/cancel",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("après une relance refetch le nouveau run et reprend le polling", async () => {
    let reads = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST")
        return Promise.resolve(
          Response.json({ status: "running", pipeline_generation: 4 }),
        );
      reads += 1;
      return Promise.resolve(
        Response.json(
          reads === 1
            ? status("ready", "assembly")
            : { ...status("running"), pipeline_generation: 4 },
        ),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProduction();
    await user.selectOptions(
      await screen.findByRole("combobox", {
        name: "Relancer depuis une étape",
      }),
      "extraction",
    );
    await waitFor(() =>
      expect(screen.getByText("en cours")).toBeInTheDocument(),
    );
    expect(shouldPollProduction("running")).toBe(true);
  });
});

it("polls only queued and running", () => {
  expect(shouldPollProduction("queued")).toBe(true);
  expect(shouldPollProduction("running")).toBe(true);
  expect(shouldPollProduction("ready")).toBe(false);
  expect(shouldPollProduction("failed")).toBe(false);
});
