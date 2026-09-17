import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Edition, Subject } from "../api/editions";
import type { EditionReleaseResponse } from "../api/publication";
import { EditionDetailPage } from "./EditionDetailPage";

const EDITION_ID = "edition-1";

const baseEdition: Edition = {
  id: EDITION_ID,
  country: "France",
  country_code: "FR",
  period_start: "2026-08-01",
  period_end: "2026-08-31",
  tlp: "GREEN",
  languages: ["fr"],
  state: "archived",
  version: 2,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

const release: EditionReleaseResponse = {
  edition_id: EDITION_ID,
  edition_state: "archived",
  manifest_id: "manifest-1",
  manifest_sha256: "a".repeat(64),
  release_id: null,
  json_available: false,
  markdown_available: false,
  docx_available: false,
  published_at: null,
  assembly_job_id: "job-1",
  assembly_status: "queued",
  assembly_error_code: null,
  assembly_error_message: null,
  can_retry_assembly: false,
};

const subject: Subject = {
  id: "subject-1",
  edition_id: EDITION_ID,
  title: "Sujet historique",
  slug: "sujet-historique",
  tlp: "GREEN",
  version: 1,
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

// Chaque endpoint du Dashboard répond séparément : une réponse « Edition »
// servie à la liste des Subjects masquerait la vraie projection.
function renderPage(edition: Edition, currentRelease: EditionReleaseResponse) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const url = urlOf(input);
    if (url.endsWith(`/api/editions/${EDITION_ID}/release`)) {
      return Promise.resolve(Response.json(currentRelease));
    }
    if (url.endsWith(`/api/editions/${EDITION_ID}/subjects`)) {
      return Promise.resolve(Response.json([subject]));
    }
    if (url.endsWith(`/api/subjects/${subject.id}/production`)) {
      return Promise.resolve(new Response(null, { status: 404 }));
    }
    return Promise.resolve(Response.json(edition));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionDetailPage editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("EditionDetailPage archivage", () => {
  it("n’affiche jamais Archiver pour une édition archivée", async () => {
    renderPage(baseEdition, release);

    expect(
      await screen.findByRole("heading", { name: "France" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Archiver l’édition" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Supprimer définitivement/ }),
    ).not.toBeInTheDocument();
  });

  it("garde une édition archivée consultable : Dashboard, historique et navigation", async () => {
    renderPage(baseEdition, release);

    expect(await screen.findByText("Sujet historique")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Ouvrir le sujet" }),
    ).toHaveAttribute("href", `/subjects/${subject.id}`);
    expect(
      screen.getByText("Production non démarrée").parentElement,
    ).toHaveTextContent("1");
    expect(screen.getByRole("link", { name: "Découverte" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/discovery`,
    );
    expect(screen.getByRole("link", { name: "Revue" })).toHaveAttribute(
      "href",
      `/editions/${EDITION_ID}/review`,
    );
    expect(
      screen.queryByRole("button", { name: "Archiver l’édition" }),
    ).not.toBeInTheDocument();
  });

  it("rend l’outil demandé au lieu du Dashboard quand une capacité est ouverte", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.endsWith(`/api/editions/${EDITION_ID}`)) {
        return Promise.resolve(Response.json(baseEdition));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <EditionDetailPage editionId={EDITION_ID} tool="production" />
      </QueryClientProvider>,
    );

    await screen.findByRole("heading", { name: "France" });
    expect(screen.getByRole("link", { name: "Productions" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(
      screen.queryByRole("link", { name: "Ouvrir le sujet" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        urlOf(input).endsWith(`/api/editions/${EDITION_ID}/subjects`),
      ),
    ).toBe(false);
  });

  it("affiche Archiver pour une édition ouverte", async () => {
    const fetchMock = renderPage({ ...baseEdition, state: "open" }, release);

    const archiveButton = await screen.findByRole("button", {
      name: "Archiver l’édition",
    });
    expect(archiveButton).toBeInTheDocument();
    await userEvent.setup().click(archiveButton);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/editions/${EDITION_ID}/archive`,
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ version: baseEdition.version }),
        }),
      ),
    );
  });
});
