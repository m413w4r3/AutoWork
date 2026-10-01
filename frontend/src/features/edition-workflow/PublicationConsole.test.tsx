import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { EditionReleaseResponse } from "../../api/publication";
import { PublicationConsole } from "./PublicationConsole";
import { publicationPollingInterval } from "./publicationPolling";

const EDITION_ID = "edition-1";

const baseRelease: EditionReleaseResponse = {
  edition_id: EDITION_ID,
  edition_state: "open",
  manifest_id: "manifest-1",
  manifest_sha256: "a".repeat(64),
  release_id: null,
  json_available: false,
  render_id: null,
  render_status: "none",
  render_error_code: null,
  render_error_message: null,
  can_retry_render: false,
  pdf_available: false,
  published_at: null,
  assembly_job_id: "job-1",
  assembly_status: "queued",
  assembly_error_code: null,
  assembly_error_message: null,
  can_retry_assembly: false,
};

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function renderConsole(
  release: EditionReleaseResponse | null = baseRelease,
  readOnly = false,
) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") {
      return Promise.resolve(
        Response.json({ edition_id: EDITION_ID, edition_state: "open" }),
      );
    }
    if (release === null) {
      return Promise.resolve(
        Response.json(
          { detail: "Edition release not available" },
          { status: 404 },
        ),
      );
    }
    return Promise.resolve(Response.json(release));
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <PublicationConsole editionId={EDITION_ID} readOnly={readOnly} />
    </QueryClientProvider>,
  );
  return { client, fetchMock };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("PublicationConsole", () => {
  it.each(["queued", "running"] as const)(
    "%s conserve un polling de 2 secondes",
    (status) => {
      expect(
        publicationPollingInterval({
          ...baseRelease,
          assembly_status: status,
        }),
      ).toBe(2_000);
    },
  );

  it.each(["queued", "running"] as const)(
    "poll le rendu PDF quand son job est %s",
    (render_status) => {
      expect(
        publicationPollingInterval({
          ...baseRelease,
          release_id: "release-1",
          render_status,
        }),
      ).toBe(2_000);
    },
  );

  it.each([
    ["queued", "Assemblage en cours"],
    ["running", "Assemblage en cours"],
    ["succeeded", "Assemblage terminé"],
  ] as const)("%s n’affiche pas de retry", async (assembly_status, label) => {
    renderConsole({
      ...baseRelease,
      assembly_status,
    });

    expect(await screen.findByText(label)).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("cesse le polling pour les états terminaux", () => {
    for (const assembly_status of [
      "failed",
      "cancelled",
      "waiting_human",
      "succeeded",
    ] as const) {
      expect(
        publicationPollingInterval({
          ...baseRelease,
          assembly_status,
        }),
      ).toBe(false);
    }
    expect(
      publicationPollingInterval({
        ...baseRelease,
        edition_state: "open",
        release_id: "release-1",
        assembly_status: "succeeded",
      }),
    ).toBe(false);
    expect(publicationPollingInterval(undefined)).toBe(false);
  });

  it("cesse le polling quand aucun job ne peut être relancé", () => {
    expect(
      publicationPollingInterval({
        ...baseRelease,
        assembly_status: null,
        assembly_job_id: null,
        can_retry_assembly: true,
      }),
    ).toBe(false);
    expect(
      publicationPollingInterval({
        ...baseRelease,
        manifest_id: null,
        assembly_status: "running",
      }),
    ).toBe(false);
  });

  it("suit can_retry_assembly même si le statut du job est actif", () => {
    expect(
      publicationPollingInterval({
        ...baseRelease,
        assembly_status: "running",
        can_retry_assembly: true,
      }),
    ).toBe(false);
  });

  it("affiche le retry quand aucun job d’assemblage n’existe", async () => {
    const { fetchMock } = renderConsole({
      ...baseRelease,
      assembly_job_id: null,
      assembly_status: null,
      can_retry_assembly: true,
    });
    const user = userEvent.setup();

    expect(
      await screen.findByText("L'assemblage n'a pas pu être démarré."),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Relancer l'assemblage" }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            init?.method === "POST" &&
            urlOf(input) === "/api/editions/edition-1/publication/accept",
        ),
      ).toBe(true),
    );
    expect(
      fetchMock.mock.calls.find(([, init]) => init?.method === "POST")?.[1]
        ?.body,
    ).toBeUndefined();
  });

  it("affiche le retry pour un assemblage annulé", async () => {
    renderConsole({
      ...baseRelease,
      assembly_status: "cancelled",
      can_retry_assembly: true,
    });

    expect(
      await screen.findByText("L'assemblage a été annulé."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Relancer l'assemblage" }),
    ).toBeInTheDocument();
  });

  it("affiche l’intervention requise sans retry par défaut", async () => {
    renderConsole({
      ...baseRelease,
      assembly_status: "waiting_human",
      can_retry_assembly: false,
    });

    expect(
      await screen.findByText(
        "Intervention requise pour poursuivre l'assemblage.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("affiche une erreur publique, les diagnostics et le retry d’assemblage", async () => {
    const { fetchMock } = renderConsole({
      ...baseRelease,
      assembly_status: "failed",
      assembly_error_code: "edition_assembly_failed",
      assembly_error_message: "Le document n’a pas pu être assemblé.",
      can_retry_assembly: true,
    });
    const user = userEvent.setup();

    expect(
      await screen.findByText("L'assemblage a échoué."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Le document n’a pas pu être assemblé."),
    ).toBeInTheDocument();
    expect(screen.getByText("edition_assembly_failed")).not.toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Relancer l'assemblage" }),
    );
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            init?.method === "POST" &&
            urlOf(input) === "/api/editions/edition-1/publication/accept",
        ),
      ).toBe(true),
    );
    expect(
      fetchMock.mock.calls.find(([, init]) => init?.method === "POST")?.[1]
        ?.body,
    ).toBeUndefined();
  });

  it("affiche le lien PDF et sépare assemblage canonique et rendu", async () => {
    const release = {
      ...baseRelease,
      edition_state: "open" as const,
      assembly_status: "succeeded" as const,
      release_id: "release-1",
      json_available: true,
      render_id: "render-1",
      render_status: "succeeded" as const,
      pdf_available: true,
      published_at: "2026-08-29T10:00:00Z",
    };
    const { fetchMock } = renderConsole(release);
    expect(
      await screen.findByRole("heading", { name: "Assemblage canonique" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Release assemblé")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Rendu PDF" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Bulletin publié")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Télécharger le PDF" });
    expect(link).toHaveAttribute("href", "/api/editions/edition-1/release/pdf");
    expect(link).toHaveAttribute("download");
    expect(
      fetchMock.mock.calls.every(([input]) => !urlOf(input).endsWith("/pdf")),
    ).toBe(true);
  });

  it("relance uniquement le rendu PDF après un échec", async () => {
    const { fetchMock } = renderConsole({
      ...baseRelease,
      release_id: "release-1",
      json_available: true,
      assembly_status: "succeeded",
      render_status: "failed",
      render_id: "render-1",
      render_error_code: "typst_compile_failed",
      render_error_message: "Le PDF ne peut pas être compilé.",
      can_retry_render: true,
    });
    const user = userEvent.setup();

    expect(await screen.findByText("typst_compile_failed")).toBeInTheDocument();
    expect(
      screen.getByText("Le PDF ne peut pas être compilé."),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Relancer le rendu" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            init?.method === "POST" &&
            urlOf(input) === "/api/editions/edition-1/release/render",
        ),
      ).toBe(true),
    );
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          init?.method === "POST" &&
          urlOf(input) === "/api/editions/edition-1/publication/accept",
      ),
    ).toBe(false);
  });

  it("ne répercute pas automatiquement une publication dans Edition", async () => {
    const release = {
      ...baseRelease,
      edition_state: "open" as const,
      release_id: "release-1",
      assembly_status: "succeeded" as const,
      json_available: true,
      render_status: "succeeded" as const,
      pdf_available: true,
    };
    const { client } = renderConsole(release);
    const invalidate = vi.spyOn(client, "invalidateQueries");

    expect(await screen.findByText("Bulletin publié")).toBeInTheDocument();
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("propose l’archive des règles à côté du bulletin, sans fetcher le ZIP", async () => {
    const release = {
      ...baseRelease,
      edition_state: "open" as const,
      assembly_status: "succeeded" as const,
      release_id: "release-1",
      json_available: true,
      published_at: "2026-08-29T10:00:00Z",
    };
    const { fetchMock } = renderConsole(release);
    const link = await screen.findByRole("link", {
      name: "Télécharger les règles (ZIP)",
    });
    expect(link).toHaveAttribute(
      "href",
      "/api/editions/edition-1/release/rules",
    );
    expect(link).toHaveAttribute("download");
    expect(
      fetchMock.mock.calls.every(([input]) => !urlOf(input).endsWith("/rules")),
    ).toBe(true);
  });

  it("conserve les téléchargements en mode ARCHIVED et n’affiche pas de commande", async () => {
    const release = {
      ...baseRelease,
      release_id: "release-1",
      json_available: true,
      render_status: "succeeded" as const,
      pdf_available: true,
    };
    renderConsole(release, true);
    expect(
      await screen.findByRole("link", {
        name: "Télécharger le PDF",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Télécharger les règles (ZIP)" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("affiche un état archivé simple si le release est absent", async () => {
    renderConsole(null, true);
    expect(
      await screen.findByRole("heading", { name: "Édition archivée" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Télécharger le PDF" }),
    ).not.toBeInTheDocument();
  });
});
