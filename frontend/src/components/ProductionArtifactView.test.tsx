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
              role: "unknown",
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
              role: "unknown",
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
    expect(screen.getByRole("link", { name: url })).toHaveAttribute(
      "href",
      url,
    );
  }
  expect(screen.getByText("Core")).toBeInTheDocument();
  expect(screen.getAllByText("Référence complémentaire")).toHaveLength(2);
  expect(
    screen.getByRole("heading", { name: "Core report" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Primaire")).toBeInTheDocument();
  expect(screen.getAllByText("Inconnu")).toHaveLength(2);
  expect(screen.getAllByText("Publication")).toHaveLength(3);
  // Two TECHNICAL tiers plus two technical_resource kinds.
  expect(screen.getAllByText("Ressource technique")).toHaveLength(4);
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

it("rend l'extraction canonique V1 structurée avec ses preuves et omissions", async () => {
  const fullDocumentId = "11111111-1111-4111-8111-111111111111";
  const iocDocumentId = "22222222-2222-4222-8222-222222222222";
  const evidence = (quote: string, ...documents: string[]) => ({
    context: "",
    evidence_quote: quote,
    evidence_basis: "source_verified",
    source_document_ids: documents,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "extraction-1",
        stage: "extraction",
        version: 1,
        status: "verified",
        metadata: {},
        rendered_content: null,
        canonical_content: {
          schema_version: 1,
          subject_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          production_input_hash: "a".repeat(64),
          references_corpus_hash: "b".repeat(64),
          profile_policy_version: "production-reference-tier-v1",
          sources: [
            {
              source_document_id: fullDocumentId,
              canonical_url: "https://vendor.example/report",
              content_sha256: "c".repeat(64),
              tier: "core",
              kind: "publication",
              role: "primary",
              profile: "full",
              checkpoint_id: "33333333-3333-4333-8333-333333333333",
              reuse_state: "fresh",
              facts: [
                {
                  category: "actors",
                  value: "Cavern Manticore",
                  attack_id: null,
                  ...evidence(
                    "Cavern Manticore launched the campaign.",
                    fullDocumentId,
                    iocDocumentId,
                  ),
                },
              ],
              events: [
                {
                  event_date: "2026-08-20",
                  date_text: null,
                  text: "The campaign began.",
                  ...evidence(
                    "The campaign began on 20 August 2026.",
                    fullDocumentId,
                  ),
                },
              ],
              indicators: [],
              rules: [],
              uncertainties: [],
            },
            {
              source_document_id: iocDocumentId,
              canonical_url: "https://research.example/iocs",
              content_sha256: "d".repeat(64),
              tier: "technical",
              kind: "technical_resource",
              role: "unknown",
              profile: "ioc_rules",
              checkpoint_id: "44444444-4444-4444-8444-444444444444",
              reuse_state: "reused",
              facts: [],
              events: [],
              indicators: [
                {
                  value: "c2.example",
                  artifact_type: "domain",
                  indicator_status: "confirmed_ioc",
                  ...evidence("C2 domain: c2.example", iocDocumentId),
                },
              ],
              rules: [
                {
                  rule_type: "sigma",
                  name: "Suspicious process launch",
                  body: "title: Suspicious process launch\nlogsource:\n  product: windows",
                  sha256: "e".repeat(64),
                  ...evidence(
                    "title: Suspicious process launch",
                    iocDocumentId,
                  ),
                },
              ],
              uncertainties: [
                "Le domaine peut être partagé avec un autre outil.",
              ],
            },
          ],
          omitted_sources: [
            {
              canonical_url: "https://reports.example/unavailable",
              tier: "supporting",
              collection_state: "unavailable",
              reason: "reference_not_eligible",
              error_code: null,
            },
            {
              canonical_url: "https://reports.example/unreadable",
              tier: "supporting",
              collection_state: "archived",
              reason: "source_extraction_failed",
              error_code: "extraction_source_text_unreadable",
            },
          ],
          warnings: ["Une source complémentaire a été omise."],
        },
      }),
    ),
  );
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const { container } = render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId="subject-1" stage="extraction" />
    </QueryClientProvider>,
  );

  for (const heading of [
    "Sources FULL",
    "Sources IOC_RULES",
    "Sources réutilisées",
    "Sources omises / en erreur",
    "Chronologie",
    "Faits",
    "IOC / artefacts",
    "Règles publiées",
    "Incertitudes",
    "Warnings",
  ]) {
    expect(
      await screen.findByRole("heading", { name: heading }),
    ).toBeInTheDocument();
  }
  expect(screen.getByText("Calculée")).toBeInTheDocument();
  expect(screen.getAllByText(/Réutilisée/)).toHaveLength(2);
  expect(screen.getByText("The campaign began.")).toBeInTheDocument();
  expect(
    screen.getByText("Cavern Manticore", { exact: true }),
  ).toBeInTheDocument();
  expect(screen.getByText("c2.example")).toBeInTheDocument();
  expect(container.querySelector("pre")).toHaveTextContent(
    "title: Suspicious process launch",
  );
  expect(
    screen.getByText("Le domaine peut être partagé avec un autre outil."),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Une source complémentaire a été omise."),
  ).toBeInTheDocument();
  // A fact published by two documents names both of them.
  const factProvenance = screen
    .getByText("Cavern Manticore", { exact: true })
    .closest("li")
    ?.querySelector(".extraction-provenance");
  expect(factProvenance).toHaveTextContent("https://vendor.example/report");
  expect(factProvenance).toHaveTextContent("https://research.example/iocs");
  expect(
    screen.getByText("extraction_source_text_unreadable"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/schema_version/)).not.toBeInTheDocument();
});
