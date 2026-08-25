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
    conversation_id: "c-1",
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
      screen.getByRole("link", { name: "Voir la conversation" }),
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
                detail: "Qualification de 83 IOC",
              },
            },
          }),
        ),
      ),
    );

    renderProduction();

    expect(
      await screen.findByText(/Qualification de 83 IOC/),
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
    expect(
      screen.queryByRole("button", { name: "Relancer la production" }),
    ).toBeNull();
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

  it("arrête polling seulement quand run terminal", () => {
    expect(shouldPollProduction("queued")).toBe(true);
    expect(shouldPollProduction("running")).toBe(true);
    expect(shouldPollProduction("needs_review")).toBe(false);
    expect(shouldPollProduction("failed")).toBe(false);
    expect(shouldPollProduction("ready")).toBe(false);
  });
});
