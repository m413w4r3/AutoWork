import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ProductionArtifactView } from "./ProductionArtifactView";

afterEach(() => vi.unstubAllGlobals());

it("construit la preview de publication depuis le JSON canonique", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "publication-1",
        stage: "publication",
        version: 1,
        status: "verified",
        metadata: {},
        rendered_content: '::: {custom-style="publication"}\ncontenu\n:::',
        canonical_content: {
          schema_version: "1",
          title: "[Cavern Manticore] Un framework modulaire",
          timeline: [],
          synthesis: [
            [
              { kind: "actor", text: "Cavern Manticore", source_ids: [] },
              { kind: "text", text: " utilise ", source_ids: [] },
              { kind: "tool", text: "WinDirStat", source_ids: [] },
            ],
          ],
          indicators: [
            {
              artifact_type: "domain",
              values: [
                {
                  value: "example[.]com",
                  normalized_value: "example.com",
                  artifact_type: "domain",
                  source_ids: ["S1"],
                },
              ],
            },
          ],
          sources: [],
          uncertainties: [],
        },
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId="subject-1" stage="publication" />
    </QueryClientProvider>,
  );

  expect(
    await screen.findByRole("heading", {
      name: "[Cavern Manticore] Un framework modulaire",
    }),
  ).toBeInTheDocument();
  expect(screen.getByText("WinDirStat")).toHaveClass("semantic-tool");
  expect(screen.getByText("example.com")).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: "Télécharger le Markdown Pandoc" }),
  ).toHaveAttribute("download", "publication-pandoc.md");
  expect(screen.queryByText(/custom-style/)).not.toBeInTheDocument();
});

it("préserve la provenance visible d'un artifact réutilisé", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "synthesis-b",
        stage: "synthesis",
        version: 1,
        status: "verified",
        reused: true,
        reused_from_artifact_id: "synthesis-a",
        reused_from_created_at: "2026-08-10T10:00:00Z",
        metadata: {},
        rendered_content: "Synthèse canonique réutilisée",
        canonical_content: { body: "Synthèse canonique réutilisée" },
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId="subject-1" stage="synthesis" />
    </QueryClientProvider>,
  );

  expect(
    await screen.findByText(/Réutilisé depuis un calcul précédent/),
  ).toBeInTheDocument();
  expect(screen.getByText(/artifact source : synthesis-a/)).toBeInTheDocument();
  expect(screen.getByText(/calcul original/)).toBeInTheDocument();
  expect(screen.getByText("Synthèse canonique réutilisée")).toBeInTheDocument();
});

it("affiche toutes les sources du corpus REFERENCES V1 avec leur URL canonique", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "references-1",
        stage: "references",
        version: 1,
        status: "verified",
        metadata: {},
        rendered_content: "legacy S1 S2",
        canonical_content: {
          schema_version: 1,
          subject_id: "subject-1",
          research_date: "2026-09-25",
          production_input_hash: "a".repeat(64),
          research_status: "completed",
          warnings: [],
          sources: [
            {
              canonical_url: "https://vendor.example/core-report",
              tier: "core",
              kind: "publication",
              role: "primary",
              title: "Core report",
              publisher: "Vendor",
              published_at: "2026-09-20",
              source_collection_id: "collection-1",
              source_document_id: "document-1",
              discovery_candidate_ids: [],
              collection_state: "archived",
              content_sha256: "b".repeat(64),
              relevance_reason: null,
              proposed_by_model: false,
              eligible_for_extraction: true,
            },
            {
              canonical_url: "https://news.example/related",
              tier: "supporting",
              kind: "publication",
              role: "independent",
              title: "Related report",
              publisher: "News",
              published_at: "2026-09-21",
              source_collection_id: null,
              source_document_id: null,
              discovery_candidate_ids: [],
              collection_state: "unavailable",
              content_sha256: null,
              relevance_reason: "Corroboration",
              proposed_by_model: true,
              eligible_for_extraction: false,
            },
            {
              canonical_url: "https://research.example/ioc-feed",
              tier: "technical",
              kind: "technical_resource",
              role: "technical",
              title: "IOC feed",
              publisher: "Research Lab",
              published_at: null,
              source_collection_id: null,
              source_document_id: null,
              discovery_candidate_ids: [],
              collection_state: "blocked",
              content_sha256: null,
              relevance_reason: "Technical indicators",
              proposed_by_model: true,
              eligible_for_extraction: false,
            },
            {
              canonical_url: "https://reports.example/retry",
              tier: "supporting",
              kind: "publication",
              role: "relay",
              title: "Retryable failure report",
              publisher: "Reports",
              published_at: null,
              source_collection_id: null,
              source_document_id: null,
              discovery_candidate_ids: [],
              collection_state: "failed_retryable",
              content_sha256: null,
              relevance_reason: "Additional context",
              proposed_by_model: true,
              eligible_for_extraction: false,
            },
            {
              canonical_url: "https://reports.example/terminal",
              tier: "technical",
              kind: "technical_resource",
              role: "technical",
              title: "Terminal failure report",
              publisher: "Reports",
              published_at: null,
              source_collection_id: null,
              source_document_id: null,
              discovery_candidate_ids: [],
              collection_state: "failed_terminal",
              content_sha256: null,
              relevance_reason: "Technical details",
              proposed_by_model: true,
              eligible_for_extraction: false,
            },
          ],
        },
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const { container } = render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId="subject-1" stage="references" />
    </QueryClientProvider>,
  );

  expect(
    await screen.findByRole("heading", { name: "Corpus de références" }),
  ).toBeInTheDocument();
  for (const url of [
    "https://vendor.example/core-report",
    "https://news.example/related",
    "https://research.example/ioc-feed",
    "https://reports.example/retry",
    "https://reports.example/terminal",
  ]) {
    expect(screen.getByRole("link", { name: url })).toHaveAttribute("href", url);
  }
  expect(screen.getByText("CORE — source du sujet")).toBeInTheDocument();
  expect(
    screen.getAllByText("SUPPORTING — référence complémentaire"),
  ).toHaveLength(2);
  expect(screen.getAllByText("TECHNICAL — ressource technique")).toHaveLength(2);
  expect(screen.getByText("primary")).toBeInTheDocument();
  expect(screen.getAllByText("Publication")).toHaveLength(3);
  expect(screen.getAllByText("Ressource technique")).toHaveLength(2);
  expect(screen.getByText("Vendor")).toBeInTheDocument();
  expect(screen.getByText("2026-09-20")).toBeInTheDocument();
  expect(screen.getByText("Archivée")).toBeInTheDocument();
  expect(screen.getByText("Indisponible")).toBeInTheDocument();
  expect(screen.getByText("Bloquée")).toBeInTheDocument();
  expect(screen.getByText("Échec — nouvel essai possible")).toBeInTheDocument();
  expect(screen.getByText("Échec définitif")).toBeInTheDocument();
  expect(screen.getAllByText("Éligible à l’extraction")).toHaveLength(1);
  expect(screen.getAllByText("Non éligible à l’extraction")).toHaveLength(4);
  expect(screen.queryByText("S1", { exact: true })).not.toBeInTheDocument();
  expect(screen.queryByText("S2", { exact: true })).not.toBeInTheDocument();
  expect(container.querySelector('a[href*="#conversations"]')).toBeNull();
  expect(screen.queryByText("legacy S1 S2")).not.toBeInTheDocument();
});
