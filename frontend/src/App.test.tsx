import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { Edition } from "./api/editions";

const iranEdition: Edition = {
  id: "30e5b0b8-2dba-48c3-81ca-9eaed5c22c62",
  country: "Iran",
  country_code: "IR",
  period_start: "2026-07-01",
  period_end: "2026-07-31",
  tlp: "AMBER",
  languages: ["fr", "en", "fa"],
  target_major_articles: 2,
  target_briefs: 6,
  previous_edition_id: null,
  source_profile: "iran-default",
  status: "draft",
  version: 1,
  progress_percent: 0,
  allowed_transitions: ["discovery", "archived"],
  created_at: "2026-08-08T00:00:00Z",
  updated_at: "2026-08-08T00:00:00Z",
};

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/editions");
});

describe("App éditions", () => {
  it("affiche la liste avec badges, progression et lien détail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json({
          items: [iranEdition],
          total: 1,
          page: 1,
          page_size: 20,
        }),
      ),
    );
    renderApp();

    expect(
      await screen.findByRole("heading", { name: "Iran" }),
    ).toBeInTheDocument();
    expect(screen.getByText("TLP:AMBER")).toBeInTheDocument();
    expect(
      screen.getByText("Brouillon", { selector: "span" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveValue(0);
  });

  it("présente un état vide exploitable", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          Response.json({ items: [], total: 0, page: 1, page_size: 20 }),
        ),
    );
    renderApp();

    expect(
      await screen.findByRole("heading", { name: "Aucune édition" }),
    ).toBeInTheDocument();
  });

  it("crée une édition Iran et n’affiche que les transitions autorisées", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url === "/api/editions" && init?.method === "POST") {
        return Response.json(iranEdition, { status: 201 });
      }
      if (url.endsWith(iranEdition.id)) return Response.json(iranEdition);
      return Response.json({ items: [], total: 0, page: 1, page_size: 20 });
    });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/editions/new");
    const user = userEvent.setup();
    renderApp();

    await user.type(screen.getByLabelText("Pays"), "Iran");
    await user.type(screen.getByLabelText("Code pays"), "IR");
    await user.type(screen.getByLabelText("Période"), "2026-07");
    await user.clear(screen.getByLabelText("Langues"));
    await user.type(screen.getByLabelText("Langues"), "fr,en,fa");
    await user.clear(screen.getByLabelText("Profil de sources"));
    await user.type(screen.getByLabelText("Profil de sources"), "iran-default");
    await user.click(screen.getByRole("button", { name: "Créer l’édition" }));

    expect(
      await screen.findByRole("heading", { name: "Iran" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Passer à « Découverte »" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Passer à « Archivée »" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Publiée/ }),
    ).not.toBeInTheDocument();
    const createCall = fetchMock.mock.calls.find(
      ([url, init]) => url === "/api/editions" && init?.method === "POST",
    );
    const requestBody = createCall?.[1]?.body;
    expect(
      JSON.parse(typeof requestBody === "string" ? requestBody : "{}"),
    ).toMatchObject({
      country: "Iran",
      country_code: "IR",
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      languages: ["fr", "en", "fa"],
      previous_edition_id: null,
    });
  });

  it("rend les erreurs API accessibles", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          {
            detail: {
              code: "storage_error",
              message: "Service indisponible.",
            },
          },
          { status: 503 },
        ),
      ),
    );
    renderApp();

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Service indisponible.",
    );
  });
});
