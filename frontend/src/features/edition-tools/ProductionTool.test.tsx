import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Edition } from "../../api/editions";
import type { ProductionBoard, ProductionSubject } from "../../api/production";
import { EditionToolSurface } from "./EditionWorkspace";

const EDITION_ID = "edition-production";

const edition: Edition = {
  id: EDITION_ID,
  country: "France",
  country_code: "FR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "GREEN",
  languages: ["fr"],
  state: "open",
  version: 1,
  created_at: "2026-08-01T10:00:00Z",
  updated_at: "2026-08-01T10:00:00Z",
};

function subject(
  id: string,
  title: string,
  overrides: Partial<ProductionSubject> = {},
): ProductionSubject {
  return {
    subject_id: id,
    title,
    tlp: "GREEN",
    latest_run_id: null,
    latest_run_number: null,
    latest_status: null,
    latest_stage: null,
    active_run_id: null,
    can_start: true,
    blocking_reason: null,
    ...overrides,
  };
}

function board(subjects: ProductionSubject[]): ProductionBoard {
  return {
    edition_id: EDITION_ID,
    active_batch: null,
    subjects,
    recent_batches: [],
  };
}

interface RecordedCall {
  url: string;
  method: string;
  headers: Headers;
  body: unknown;
}

function stubFetch(
  currentBoard: () => ProductionBoard,
  onStart: (call: RecordedCall) => Response,
): RecordedCall[] {
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      const call: RecordedCall = {
        url,
        method: init?.method ?? "GET",
        headers: new Headers(init?.headers),
        body:
          typeof init?.body === "string"
            ? (JSON.parse(init.body) as unknown)
            : undefined,
      };
      calls.push(call);
      if (url === `/api/editions/${EDITION_ID}/production`) {
        return Promise.resolve(Response.json(currentBoard()));
      }
      if (url === `/api/editions/${EDITION_ID}/production/batches`) {
        return Promise.resolve(onStart(call));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
  return calls;
}

function renderProduction(value: Edition = edition) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionToolSurface edition={value} tool="production" />
    </QueryClientProvider>,
  );
}

function startButton() {
  return screen.getByRole("button", { name: "Démarrer le lot de production" });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Production surface", () => {
  it("affiche un tableau vide sans jamais lire l’API Selection", async () => {
    const calls = stubFetch(
      () => board([]),
      () => Response.json({}),
    );

    renderProduction();

    expect(
      await screen.findByText("Aucun sujet éligible pour le moment."),
    ).toBeInTheDocument();
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.every((call) => !call.url.includes("/selection"))).toBe(true);
    expect(
      calls.every(
        (call) => call.url === `/api/editions/${EDITION_ID}/production`,
      ),
    ).toBe(true);
  });

  it("démarre un sujet seul par la primitive de lot", async () => {
    const calls = stubFetch(
      () => board([subject("subject-a", "Sujet A")]),
      () => Response.json({ batch_id: "batch-1" }),
    );

    renderProduction();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Sujet A" }));
    fireEvent.click(startButton());

    await waitFor(() =>
      expect(calls.filter((call) => call.method === "POST")).toHaveLength(1),
    );
    const post = calls.find((call) => call.method === "POST");
    expect(post?.body).toEqual({ subject_ids: ["subject-a"] });
    expect(post?.headers.get("Idempotency-Key")).toBeTruthy();
    expect(calls.every((call) => !call.url.includes("/selection"))).toBe(true);
  });

  it("envoie plusieurs sujets dans l’ordre du tableau", async () => {
    const calls = stubFetch(
      () =>
        board([
          subject("subject-c", "Sujet C"),
          subject("subject-a", "Sujet A"),
          subject("subject-b", "Sujet B"),
        ]),
      () => Response.json({ batch_id: "batch-1" }),
    );

    renderProduction();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Sujet B" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Sujet C" }));
    fireEvent.click(startButton());

    await waitFor(() =>
      expect(calls.filter((call) => call.method === "POST")).toHaveLength(1),
    );
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      subject_ids: ["subject-c", "subject-b"],
    });
  });

  it("rejoue une panne transitoire avec la même clé d’idempotence", async () => {
    let attempts = 0;
    const calls = stubFetch(
      () => board([subject("subject-a", "Sujet A")]),
      () => {
        attempts += 1;
        return attempts === 1
          ? new Response(null, { status: 503 })
          : Response.json({ batch_id: "batch-1" });
      },
    );

    renderProduction();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Sujet A" }));
    fireEvent.click(startButton());

    await waitFor(
      () =>
        expect(calls.filter((call) => call.method === "POST")).toHaveLength(2),
      { timeout: 5_000 },
    );
    const keys = calls
      .filter((call) => call.method === "POST")
      .map((call) => call.headers.get("Idempotency-Key"));
    expect(new Set(keys).size).toBe(1);
  });

  it("explique un lot déjà actif sans réessayer", async () => {
    const calls = stubFetch(
      () => board([subject("subject-a", "Sujet A")]),
      () =>
        Response.json(
          { detail: { code: "production_batch_active", subject_ids: [] } },
          { status: 409 },
        ),
    );

    renderProduction();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Sujet A" }));
    fireEvent.click(startButton());

    expect(
      await screen.findByText(/Un lot de production est déjà en cours/),
    ).toBeInTheDocument();
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
  });

  it("montre la raison de blocage et la dernière production d’un sujet", async () => {
    stubFetch(
      () =>
        board([
          subject("subject-a", "Sujet A", {
            can_start: false,
            blocking_reason: "production_subject_active",
            latest_status: "running",
            active_run_id: "run-a",
          }),
        ]),
      () => Response.json({}),
    );

    renderProduction();

    expect(
      await screen.findByText("Une production de ce sujet est déjà en cours."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: "Sujet A" }),
    ).not.toBeInTheDocument();
  });

  it("reste en lecture seule pour une édition archivée", async () => {
    const calls = stubFetch(
      () =>
        board([
          subject("subject-a", "Sujet A", {
            can_start: false,
            blocking_reason: "production_edition_archived",
          }),
        ]),
      () => Response.json({}),
    );

    renderProduction({ ...edition, state: "archived" });

    expect(await screen.findByLabelText("Sujet A")).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "Démarrer le lot de production" }),
    ).not.toBeInTheDocument();
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });
});
