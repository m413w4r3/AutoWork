import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Edition } from "../api/editions";
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
  target_articles: 3,
  previous_edition_id: null,
  source_profile: "default",
  status: "assembling",
  version: 2,
  progress_percent: 90,
  allowed_transitions: ["review", "published", "archived"],
  created_at: "2026-08-29T10:00:00Z",
  updated_at: "2026-08-29T10:00:00Z",
};

const release: EditionReleaseResponse = {
  edition_id: EDITION_ID,
  edition_status: "assembling",
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

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

function renderPage(edition: Edition, currentRelease: EditionReleaseResponse) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      if (urlOf(input).endsWith(`/api/editions/${EDITION_ID}/release`)) {
        return Promise.resolve(Response.json(currentRelease));
      }
      return Promise.resolve(Response.json(edition));
    }),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <EditionDetailPage editionId={EDITION_ID} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("EditionDetailPage archivage", () => {
  it("n’affiche jamais Archiver pour une édition ASSEMBLING", async () => {
    renderPage(baseEdition, release);

    expect(
      await screen.findByRole("heading", { name: "Manifest figé" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Archiver l’édition" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Supprimer définitivement/ }),
    ).not.toBeInTheDocument();
  });

  it("conserve l’archivage pour une édition PUBLISHED", async () => {
    renderPage(
      { ...baseEdition, status: "published", progress_percent: 100 },
      { ...release, edition_status: "published", assembly_status: "succeeded" },
    );

    expect(
      await screen.findByRole("heading", { name: "Bulletin publié" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Archiver l’édition" }),
    ).toBeInTheDocument();
  });
});
