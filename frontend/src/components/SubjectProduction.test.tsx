import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
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

function runningStatus(overrides: Record<string, unknown> = {}) {
  return {
    subject_id: SUBJECT_ID,
    title: "TAG-182 et MarkiRAT",
    editorial_type: "brief",
    status: "running",
    current_stage: "references",
    progress_current: 1,
    progress_total: 5,
    references_conversation_id: "c-1",
    synthesis_conversation_id: null,
    run_id: "r-1",
    created_at: "2026-08-10T10:00:00Z",
    started_at: "2026-08-10T10:00:00Z",
    finished_at: null,
    warnings: [],
    stages: {
      sources: {
        status: "succeeded",
        version: null,
        error_code: null,
        error_message: null,
        archived_sources: 5,
      },
      references: {
        status: "running",
        version: null,
        error_code: null,
        error_message: null,
      },
      extraction: {
        status: "pending",
        version: null,
        error_code: null,
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
    ...overrides,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("SubjectProduction", () => {
  it("propose de produire la brève quand rien n'a été lancé", async () => {
    // A 404 means "nothing started yet", which is the entry point.
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    );

    renderProduction();

    expect(
      await screen.findByRole("button", { name: "Produire cette brève" }),
    ).toBeInTheDocument();
  });

  it("lance la production sans exiger d'edition_id", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(Response.json({ run_id: "r-1" }));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderProduction();

    await user.click(
      await screen.findByRole("button", { name: "Produire cette brève" }),
    );

    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    const body = JSON.parse(
      typeof post?.[1]?.body === "string" ? post[1].body : "{}",
    ) as Record<string, unknown>;
    expect(body).toEqual({ profile: "brief_auto" });
    expect(body).not.toHaveProperty("edition_id");
  });

  it("affiche le vrai titre, les compteurs d’étape et les liens de détail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(runningStatus())),
    );

    renderProduction();

    // The scaffold used to render the literal string "Subject Title".
    expect(
      await screen.findByRole("heading", { name: "TAG-182 et MarkiRAT" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/5 archivée\(s\)/)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Voir les références" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Voir la recherche" }),
    ).toBeInTheDocument();
  });

  it("affiche détail court pendant extraction", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          runningStatus({
            current_stage: "extraction",
            stages: {
              ...runningStatus().stages,
              references: {
                ...runningStatus().stages.references,
                status: "succeeded",
              },
              extraction: {
                status: "running",
                version: null,
                error_code: null,
                error_message: null,
                detail: "Extraction technique de 83 éléments",
              },
            },
          }),
        ),
      ),
    );

    renderProduction();

    expect(
      await screen.findByText(/Extraction technique de 83 éléments/),
    ).toBeInTheDocument();
  });

  it("présente les avertissements du parser sans bloquer", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json(
            runningStatus({ warnings: ["duplicate_source_merged"] }),
          ),
        ),
    );

    renderProduction();

    expect(
      await screen.findByText("Avertissements de lecture"),
    ).toBeInTheDocument();
    expect(screen.getByText("duplicate_source_merged")).toBeInTheDocument();
  });

  it("permet de relancer après une annulation", async () => {
    // A cancelled run used to leave the page with no way forward.
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json(runningStatus({ status: "cancelled" })),
        ),
    );

    renderProduction();

    expect(
      await screen.findByRole("button", { name: "Relancer la production" }),
    ).toBeInTheDocument();
  });

  it("affiche immédiatement échec extraction sans faux état en cours", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          runningStatus({
            status: "failed",
            current_stage: "extraction",
            stages: {
              ...runningStatus().stages,
              extraction: {
                status: "failed",
                version: null,
                error_code: "bridge_timeout",
                error_message: "timeout",
              },
            },
          }),
        ),
      ),
    );

    renderProduction();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Extraction CTI",
    );
    expect(screen.getByText(/bridge_timeout/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "la conversation ChatGPT n’a pas pu être finalisée",
    );
    // The failed stage's details stay visible next to the retry action --
    // neither replaces the other.
    expect(
      screen.getByRole("button", { name: "Relancer la production" }),
    ).toBeInTheDocument();
    expect(screen.getByText("3. Extraction CTI").closest("li")).toHaveClass(
      "is-failed",
    );
  });

  it("affiche needs_review extraction, code et détail de l’étape", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          runningStatus({
            status: "needs_review",
            current_stage: "extraction",
            stages: {
              ...runningStatus().stages,
              extraction: {
                status: "needs_review",
                version: null,
                error_code: "extraction_format_unusable",
                error_message: "internal parser detail",
              },
            },
          }),
        ),
      ),
    );

    renderProduction();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Extraction CTI");
    expect(alert).toHaveTextContent("extraction_format_unusable");
    expect(alert).not.toHaveTextContent("internal parser detail");
    expect(
      screen.getByRole("link", { name: "Voir les détails de l’étape" }),
    ).toHaveAttribute(
      "href",
      `/subjects/${SUBJECT_ID}/production/artifacts/extraction`,
    );
  });

  it("conserve affichage normal quand brève prête", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          runningStatus({
            status: "ready",
            current_stage: "assembly",
            stages: Object.fromEntries(
              Object.entries(runningStatus().stages).map(([stage, entry]) => [
                stage,
                { ...entry, status: "succeeded" },
              ]),
            ),
          }),
        ),
      ),
    );

    renderProduction();

    expect(await screen.findByRole("link", { name: "Aperçu" })).toHaveAttribute(
      "href",
      `/subjects/${SUBJECT_ID}/production/artifacts/brief`,
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("relance les références : POST réel, puis reprend le polling sur le nouveau run", async () => {
    const readyStatus = runningStatus({
      status: "ready",
      current_stage: "assembly",
      run_id: "r-1",
      stages: Object.fromEntries(
        Object.entries(runningStatus().stages).map(([stage, entry]) => [
          stage,
          { ...entry, status: "succeeded" },
        ]),
      ),
    });
    const newRunStatus = runningStatus({
      status: "queued",
      current_stage: "sources",
      run_id: "r-2",
    });
    let getCount = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        // A real action started: a real job, not a synthetic "initiated".
        return Promise.resolve(
          Response.json({
            action: "retry_references",
            run_id: "r-2",
            previous_run_id: "r-1",
            status: "queued",
            job_id: "job-sources-1",
          }),
        );
      }
      getCount += 1;
      return Promise.resolve(
        Response.json(getCount === 1 ? readyStatus : newRunStatus),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const { container } = renderProduction();

    await user.click(
      await screen.findByRole("button", { name: "Relancer les références" }),
    );

    // The panel must reflect the new run's real (polling) state, never a
    // frozen "initiated" placeholder from the POST response.
    await vi.waitFor(() => {
      expect(container.querySelector(".badge.is-queued")).not.toBeNull();
    });
    expect(shouldPollProduction("queued")).toBe(true);

    const post = fetchMock.mock.calls.find(
      ([url]) => typeof url === "string" && url.includes("/references/retry"),
    );
    expect(post?.[0]).toBe(
      `/api/subjects/${SUBJECT_ID}/production/references/retry`,
    );
  });

  it("relance la synthèse : POST réel avec generation, run inchangé", async () => {
    const readyStatus = runningStatus({
      status: "ready",
      current_stage: "assembly",
      run_id: "r-1",
      stages: Object.fromEntries(
        Object.entries(runningStatus().stages).map(([stage, entry]) => [
          stage,
          { ...entry, status: "succeeded" },
        ]),
      ),
    });
    const retriedStatus = runningStatus({
      status: "running",
      current_stage: "synthesis",
      run_id: "r-1",
      stages: {
        ...readyStatus.stages,
        synthesis: { ...readyStatus.stages.synthesis, status: "running" },
      },
    });
    let getCount = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          Response.json({
            action: "retry_synthesis",
            run_id: "r-1",
            status: "running",
            job_id: "job-synthesis-1",
            synthesis_generation: 2,
          }),
        );
      }
      getCount += 1;
      return Promise.resolve(
        Response.json(getCount === 1 ? readyStatus : retriedStatus),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const { container } = renderProduction();

    await user.click(
      await screen.findByRole("button", { name: "Relancer la synthèse" }),
    );

    await vi.waitFor(() => {
      expect(container.querySelector(".badge.is-running")).not.toBeNull();
    });

    const post = fetchMock.mock.calls.find(
      ([url]) => typeof url === "string" && url.includes("/synthesis/retry"),
    );
    expect(post?.[0]).toBe(
      `/api/subjects/${SUBJECT_ID}/production/synthesis/retry`,
    );
  });

  it("relance la production depuis un run FAILED : POST /production, refetch, polling reprend", async () => {
    const failedStatus = runningStatus({
      status: "failed",
      current_stage: "extraction",
      run_id: "r-1",
      stages: {
        ...runningStatus().stages,
        extraction: {
          status: "failed",
          version: null,
          error_code: "bridge_timeout",
          error_message: "timeout",
        },
      },
    });
    const newRunStatus = runningStatus({
      status: "running",
      current_stage: "sources",
      run_id: "r-2",
    });
    let getCount = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          Response.json({ run_id: "r-2", status: "running" }),
        );
      }
      getCount += 1;
      return Promise.resolve(
        Response.json(getCount === 1 ? failedStatus : newRunStatus),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const { container } = renderProduction();

    expect(
      await screen.findByRole("button", { name: "Relancer la production" }),
    ).toBeInTheDocument();
    // Details of the failed stage remain visible next to the retry action.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Extraction CTI",
    );

    await user.click(
      screen.getByRole("button", { name: "Relancer la production" }),
    );

    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(post?.[0]).toBe(`/api/subjects/${SUBJECT_ID}/production`);

    // A brand-new run must be reflected -- a new run_id, RUNNING, polling.
    await vi.waitFor(() => {
      expect(container.querySelector(".badge.is-running")).not.toBeNull();
    });
    expect(screen.getByText(/r-2/)).toBeInTheDocument();
    expect(shouldPollProduction("running")).toBe(true);
  });

  it("relance la production depuis un run NEEDS_REVIEW : POST /production, refetch, polling reprend", async () => {
    const needsReviewStatus = runningStatus({
      status: "needs_review",
      current_stage: "references",
      run_id: "r-1",
      stages: {
        ...runningStatus().stages,
        references: {
          status: "needs_review",
          version: null,
          error_code: "no_model_response",
          error_message: "no response",
        },
      },
    });
    const newRunStatus = runningStatus({
      status: "running",
      current_stage: "sources",
      run_id: "r-2",
    });
    let getCount = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          Response.json({ run_id: "r-2", status: "running" }),
        );
      }
      getCount += 1;
      return Promise.resolve(
        Response.json(getCount === 1 ? needsReviewStatus : newRunStatus),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const { container } = renderProduction();

    expect(
      await screen.findByRole("button", { name: "Relancer la production" }),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Relancer la production" }),
    );

    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(post?.[0]).toBe(`/api/subjects/${SUBJECT_ID}/production`);

    await vi.waitFor(() => {
      expect(container.querySelector(".badge.is-running")).not.toBeNull();
    });
    expect(screen.getByText(/r-2/)).toBeInTheDocument();
  });

  it("n'affiche aucun bouton de relance de production pour un run RUNNING", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(runningStatus())),
    );

    renderProduction();

    await screen.findByText("TAG-182 et MarkiRAT");
    expect(
      screen.queryByRole("button", { name: "Relancer la production" }),
    ).toBeNull();
  });

  it("conserve les boutons existants pour un run READY", async () => {
    const readyStatus = runningStatus({
      status: "ready",
      current_stage: "assembly",
      stages: Object.fromEntries(
        Object.entries(runningStatus().stages).map(([stage, entry]) => [
          stage,
          { ...entry, status: "succeeded" },
        ]),
      ),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(Response.json(readyStatus)),
    );

    renderProduction();

    expect(
      await screen.findByRole("button", { name: "Relancer les références" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Relancer la synthèse" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Relancer la production" }),
    ).toBeNull();
  });

  it("arrête polling seulement quand run terminal", () => {
    expect(shouldPollProduction("queued")).toBe(true);
    expect(shouldPollProduction("running")).toBe(true);
    expect(shouldPollProduction("needs_review")).toBe(false);
    expect(shouldPollProduction("failed")).toBe(false);
    expect(shouldPollProduction("ready")).toBe(false);
  });
});
