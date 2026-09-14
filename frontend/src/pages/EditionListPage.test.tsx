import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Edition } from "../api/editions";
import { withProductionNotStarted } from "../test-utils/fetchStubs";
import { App } from "../App";
import { EditionListPage } from "./EditionListPage";

const emptyEditionPage = {
  items: [],
  total: 0,
  page: 1,
  page_size: 20,
};

const draftEdition: Edition = {
  id: "edition-1",
  country: "France",
  country_code: "FR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "GREEN",
  languages: ["fr"],
  target_articles: 3,
  previous_edition_id: null,
  source_profile: "default",
  status: "draft",
  version: 1,
  progress_percent: 0,
  allowed_transitions: ["discovery"],
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

type FetchHandler = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Response | Promise<Response>;

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function stubFetch(handler: (url: string) => Response | Promise<Response>) {
  const fallback: FetchHandler = (input) => handler(urlOf(input));
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    withProductionNotStarted(fallback)(input, init),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderListPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionListPage />
    </QueryClientProvider>,
  );
}

function renderApp() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  window.history.replaceState(null, "", "/editions");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("EditionListPage URL filters", () => {
  it("loads validated filters from the URL and normalizes the country code", async () => {
    window.history.replaceState(
      null,
      "",
      "/editions?country_code=fr&period=2026-08&status=production",
    );
    const fetchMock = stubFetch(() => Response.json(emptyEditionPage));

    renderListPage();

    expect(screen.getByLabelText("Code pays")).toHaveValue("FR");
    expect(screen.getByLabelText("Période")).toHaveValue("2026-08");
    expect(screen.getByLabelText("Statut")).toHaveValue("production");
    await screen.findByRole("heading", { name: "Aucune édition" });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/editions?country_code=FR&period=2026-08&status=production",
      undefined,
    );
  });

  it("ignores invalid URL filter values", async () => {
    window.history.replaceState(
      null,
      "",
      "/editions?country_code=france&period=2026-13&status=unknown",
    );
    const fetchMock = stubFetch(() => Response.json(emptyEditionPage));

    renderListPage();

    expect(screen.getByLabelText("Code pays")).toHaveValue("");
    expect(screen.getByLabelText("Période")).toHaveValue("");
    expect(screen.getByLabelText("Statut")).toHaveValue("");
    await screen.findByRole("heading", { name: "Aucune édition" });
    expect(fetchMock).toHaveBeenCalledWith("/api/editions?", undefined);
  });

  it("clears one filter in the URL while preserving the others", async () => {
    window.history.replaceState(
      null,
      "",
      "/editions?country_code=FR&period=2026-08&status=production&source=test",
    );
    const fetchMock = stubFetch(() => Response.json(emptyEditionPage));
    const replaceState = vi.spyOn(window.history, "replaceState");
    const user = userEvent.setup();

    renderListPage();
    await screen.findByRole("heading", { name: "Aucune édition" });
    await user.clear(screen.getByLabelText("Code pays"));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/editions?period=2026-08&status=production",
        undefined,
      ),
    );
    expect(replaceState).toHaveBeenCalledWith(
      window.history.state,
      "",
      "/editions?period=2026-08&status=production&source=test",
    );
    expect(
      screen.getByRole("heading", { name: "Éditions" }),
    ).toBeInTheDocument();
  });

  it("resynchronizes filters after a popstate between list URLs", async () => {
    window.history.replaceState(
      null,
      "",
      "/editions?country_code=FR&period=2026-08&status=production",
    );
    const fetchMock = stubFetch(() => Response.json(emptyEditionPage));

    renderListPage();
    await screen.findByRole("heading", { name: "Aucune édition" });
    window.history.pushState(
      null,
      "",
      "/editions?country_code=DE&period=2027-02&status=review",
    );
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() => {
      expect(screen.getByLabelText("Code pays")).toHaveValue("DE");
      expect(screen.getByLabelText("Période")).toHaveValue("2027-02");
      expect(screen.getByLabelText("Statut")).toHaveValue("review");
    });
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/editions?country_code=DE&period=2027-02&status=review",
        undefined,
      ),
    );
  });

  it("restores the filtered list after browser back from an edition", async () => {
    window.history.replaceState(
      null,
      "",
      "/editions?country_code=fr&period=2026-08&status=draft",
    );
    stubFetch((url) => {
      if (url === "/api/editions?country_code=FR&period=2026-08&status=draft") {
        return Response.json({
          items: [draftEdition],
          total: 1,
          page: 1,
          page_size: 20,
        });
      }
      return Response.json(draftEdition);
    });

    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("link", { name: "Ouvrir l’édition" });
    await user.click(screen.getByRole("link", { name: "Ouvrir l’édition" }));
    window.history.back();

    await waitFor(() => {
      expect(window.location.pathname).toBe("/editions");
      expect(window.location.search).toBe(
        "?country_code=fr&period=2026-08&status=draft",
      );
      expect(screen.getByLabelText("Code pays")).toHaveValue("FR");
      expect(screen.getByLabelText("Période")).toHaveValue("2026-08");
      expect(screen.getByLabelText("Statut")).toHaveValue("draft");
    });
  });
});
