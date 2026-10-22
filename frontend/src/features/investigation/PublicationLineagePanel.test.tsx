import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PublicationLineagePanel } from "./PublicationLineagePanel";

afterEach(() => vi.unstubAllGlobals());

describe("PublicationLineagePanel", () => {
  it("compare le PDF publié au brouillon et construit des liens épinglés", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.endsWith("/production/artifacts/publication")) {
        return Promise.resolve(
          Response.json({
            artifact_id: "artifact-1",
            production_run_id: "run-1",
            stage: "publication",
            version: 3,
            input_hash: "d".repeat(64),
            canonical_sha256: "a".repeat(64),
            status: "verified",
            metadata: {},
            canonical_content: null,
          }),
        );
      }
      if (url.includes("/publication/preview?")) {
        return Promise.resolve(
          Response.json({
            status: "READY",
            artifact_id: "artifact-1",
            artifact_version: 3,
            artifact_input_hash: "d".repeat(64),
            current_artifact_id: "artifact-1",
            current_artifact_version: 3,
            render_id: "render-1",
            render_identity: "e".repeat(64),
            render_disposition: "ACCEPTED_VERSION",
            published_edition_version: 1,
            error_code: null,
            error_message: null,
            pdf_url:
              "/api/subjects/subject-1/publication/preview/artifact-1/pdf?render_identity=" +
              "e".repeat(64),
          }),
        );
      }
      if (url.endsWith("/api/investigations/investigation-1/opinion")) {
        return Promise.resolve(
          Response.json({
            draft: {
              id: "opinion-1",
              version: 27,
              status: "draft",
              content_sha256: "f".repeat(64),
              blocks: [],
              unresolved_points: [],
              created_at: "2026-10-20T09:00:00Z",
            },
            history: [],
            citation_violations: [],
            renderable_blocks: [],
            provenance: {
              investigation_id: "investigation-1",
              synthesis_artifact_id: "synthesis-1",
              synthesis_sha256: "1".repeat(64),
              base_publication_artifact_id: "artifact-1",
              base_publication_sha256: "a".repeat(64),
            },
          }),
        );
      }
      return Promise.resolve(Response.json({}, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });

    render(
      <QueryClientProvider client={client}>
        <PublicationLineagePanel
          investigationId="investigation-1"
          subjectId="subject-1"
        />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByRole("heading", { name: "Publié" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText(/Publication de base/)).toHaveLength(2);
    expect(screen.getByText(/artefact v3/)).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: "Ouvrir le PDF publié v3" }),
    ).toHaveAttribute(
      "href",
      "/api/subjects/subject-1/publication/preview/artifact-1/pdf?render_identity=" +
        "e".repeat(64),
    );
    expect(
      screen.getByRole("link", { name: "Prévisualiser le brouillon v27" }),
    ).toHaveAttribute(
      "href",
      "/api/opinions/opinion-1/preview.pdf?version=27&opinion_sha256=" +
        "f".repeat(64) +
        "&base_publication_sha256=" +
        "a".repeat(64),
    );
  });
});
