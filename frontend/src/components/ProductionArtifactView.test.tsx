import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { isProductionSynthesisV1 } from "../api/production";
import { ProductionArtifactView } from "./ProductionArtifactView";

afterEach(() => vi.unstubAllGlobals());

const SYNTHESIS_SUBJECT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const VENDOR_DOCUMENT_ID = "11111111-1111-4111-8111-111111111111";
const IOC_DOCUMENT_ID = "22222222-2222-4222-8222-222222222222";
const UNKNOWN_DOCUMENT_ID = "99999999-9999-4999-8999-999999999999";
const VENDOR_URL = "https://vendor.example/report";
const IOC_URL = "https://research.example/iocs";
const EVIDENCE_KEY = "f".repeat(64);

function evidenceRef(documentId: string, kind: string) {
  return {
    source_document_id: documentId,
    kind,
    evidence_key: EVIDENCE_KEY,
  };
}

function extractionSource(documentId: string, canonicalUrl: string) {
  return {
    source_document_id: documentId,
    canonical_url: canonicalUrl,
    content_sha256: "c".repeat(64),
    tier: "core",
    kind: "publication",
    role: "primary",
    profile: "full",
    checkpoint_id: "33333333-3333-4333-8333-333333333333",
    reuse_state: "fresh",
    facts: [],
    events: [],
    indicators: [],
    rules: [],
    uncertainties: [],
  };
}

function extractionArtifact(sources: unknown[]) {
  return {
    artifact_id: "extraction-1",
    stage: "extraction",
    version: 1,
    status: "verified",
    metadata: {},
    rendered_content: null,
    canonical_content: {
      schema_version: 1,
      subject_id: SYNTHESIS_SUBJECT_ID,
      production_input_hash: "a".repeat(64),
      references_corpus_hash: "b".repeat(64),
      profile_policy_version: "production-reference-tier-v1",
      sources,
      omitted_sources: [],
      warnings: [],
    },
  };
}

function synthesisArtifact(
  contentOverrides: Record<string, unknown> = {},
  artifactOverrides: Record<string, unknown> = {},
) {
  return {
    artifact_id: "synthesis-1",
    stage: "synthesis",
    version: 1,
    status: "verified",
    metadata: { mode: "fresh", language: "fr" },
    rendered_content: null,
    canonical_content: {
      schema_version: 1,
      subject_id: SYNTHESIS_SUBJECT_ID,
      production_input_hash: "a".repeat(64),
      extraction_hash: "b".repeat(64),
      publication_language: "fr",
      synthesis_policy_version: "production-synthesis-v1",
      title: "Campagne Cavern Manticore",
      lead: [
        {
          text: "Le groupe exploite une faille d’accès initial.",
          evidence_refs: [
            evidenceRef(VENDOR_DOCUMENT_ID, "fact"),
            evidenceRef(VENDOR_DOCUMENT_ID, "event"),
            evidenceRef(IOC_DOCUMENT_ID, "indicator"),
          ],
        },
      ],
      sections: [
        {
          kind: "infection_chain",
          heading: "",
          paragraphs: [
            {
              text: "Le leurre déclenche une chaîne PowerShell.",
              evidence_refs: [evidenceRef(IOC_DOCUMENT_ID, "rule")],
            },
          ],
        },
        {
          kind: "campaign",
          heading: "",
          paragraphs: [
            {
              text: "La campagne vise des organisations exposées.",
              evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
            },
          ],
        },
      ],
      timeline: [
        {
          event_date: "2026-08-20",
          date_text: null,
          text: "Début de la campagne observée.",
          evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "event")],
        },
      ],
      uncertainties: [
        {
          text: "Le domaine C2 peut être partagé.",
          source_document_ids: [IOC_DOCUMENT_ID],
        },
      ],
      warnings: ["Un fait mineur n’a pas pu être rattaché."],
      ...contentOverrides,
    },
    ...artifactOverrides,
  };
}

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

/** Route each artifact request to its own payload, like the real API. */
function stubProductionFetch(payloads: {
  synthesis: unknown;
  extraction?: unknown;
}) {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/production/artifacts/synthesis")) {
        return Promise.resolve(Response.json(payloads.synthesis));
      }
      if (url.includes("/production/artifacts/extraction")) {
        return Promise.resolve(
          Response.json(payloads.extraction ?? extractionArtifact([])),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
}

function renderArtifact(
  stage:
    | "references"
    | "extraction"
    | "relevance_projection"
    | "synthesis"
    | "publication",
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId="subject-1" stage={stage} />
    </QueryClientProvider>,
  );
}

it("keeps ambiguous relevance decisions visible with their reasons", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "projection-1",
        stage: "relevance_projection",
        version: 1,
        status: "verified",
        metadata: {},
        canonical_content: {
          classifications: [
            {
              classification: "indeterminate",
              reason_code: "relation_not_established",
              evidence_ref: { kind: "fact", evidence_key: "a".repeat(64) },
              supporting_evidence_refs: [],
              provenance: "deterministic_policy",
            },
          ],
        },
      }),
    ),
  );

  renderArtifact("relevance_projection");

  expect(
    await screen.findByText(/"classification": "indeterminate"/),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/"reason_code": "relation_not_established"/),
  ).toBeInTheDocument();
});

it("ne rend pas une publication dont le schema n'est pas V4", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "publication-v3",
        stage: "publication",
        version: 1,
        status: "verified",
        metadata: {},
        canonical_content: {
          schema_version: "3",
          title: "Publication legacy à ne pas afficher",
          lead: [],
          sections: [],
          timeline: [],
          indicators: [],
          sources: [],
          uncertainties: [],
        },
      }),
    ),
  );

  renderArtifact("publication");

  expect(
    await screen.findByText("ID de l'artifact : publication-v3"),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", {
      name: "Publication legacy à ne pas afficher",
    }),
  ).not.toBeInTheDocument();
});

it("affiche la publication V4 et ses enrichissements", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        Response.json({
          artifact_id: "publication-v4",
          stage: "publication",
          version: 1,
          status: "verified",
          metadata: {
            diagnostics: {
              warnings_by_stage: {
                synthesis: ["synthesis_output_invalid"],
              },
            },
          },
          canonical_content: {
            schema_version: "4",
            subject_id: SYNTHESIS_SUBJECT_ID,
            publication_language: "fr",
            title: "Article canonique",
            lead: [
              {
                text: "Lead sourcé.",
                evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
              },
            ],
            sections: [
              {
                kind: "overview",
                heading: "",
                paragraphs: [
                  {
                    text: "Paragraphe sourcé.",
                    evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                  },
                ],
              },
            ],
            timeline: [
              {
                event_date: "2026-09-01",
                date_text: null,
                text: "Événement.",
                evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "event")],
              },
            ],
            indicators: [
              {
                artifact_type: "domain",
                indicators: [
                  {
                    value: "evil.example",
                    normalized_value: "evil.example",
                    artifact_type: "domain",
                    source_document_ids: [IOC_DOCUMENT_ID],
                  },
                ],
              },
            ],
            sources: [
              {
                source_document_id: VENDOR_DOCUMENT_ID,
                canonical_url: VENDOR_URL,
                title: "Rapport",
                publisher: "Vendor",
                published_at: null,
                tier: "core",
                kind: "publication",
                role: "primary",
              },
              {
                source_document_id: IOC_DOCUMENT_ID,
                canonical_url: IOC_URL,
                title: "IOC source",
                publisher: null,
                published_at: null,
                tier: "technical",
                kind: "publication",
                role: "primary",
              },
            ],
            uncertainties: [],
            tables: [
              {
                key: "commands",
                kind: "commands",
                title: "Commandes observées",
                caption: "Commandes issues du rapport.",
                columns: [
                  { key: "command", label: "Commande" },
                  { key: "purpose", label: "Usage" },
                ],
                rows: [
                  {
                    cells: ["powershell", "Exécution"],
                    evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                  },
                ],
                placement: { kind: "after_lead", section_index: null },
              },
            ],
            diagrams: [
              {
                key: "infection_chain",
                kind: "infection_chain",
                title: "Chaîne d’infection",
                caption: null,
                direction: "left_to_right",
                nodes: [
                  {
                    node_id: "loader",
                    label: "Loader",
                    evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                  },
                  {
                    node_id: "payload",
                    label: "Payload",
                    evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                  },
                ],
                edges: [
                  {
                    source_node_id: "loader",
                    target_node_id: "payload",
                    label: "loads",
                    evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                  },
                ],
                groups: [],
                placement: { kind: "end", section_index: null },
                asset_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
              },
            ],
            figures: [
              {
                key: "source_figure_01",
                asset_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                sha256: "a".repeat(64),
                mime_type: "image/png",
                byte_size: 128,
                source_document_id: VENDOR_DOCUMENT_ID,
                source_url: "https://vendor.example/figure.png",
                caption: "Architecture source.",
                provenance: "Figure 1 from the vendor report.",
                locator: {
                  page: 4,
                  section: "Network",
                  figure_label: "Figure 1",
                  original_asset_url: "https://vendor.example/figure.png",
                },
                placement: { kind: "after_lead", section_index: null },
              },
            ],
          },
        }),
      ),
    ),
  );

  renderArtifact("publication");

  expect(
    await screen.findByRole("heading", { name: "Article canonique" }),
  ).toBeInTheDocument();
  const referencesHeading = screen.getByRole("heading", {
    name: "RÉFÉRENCES",
  });
  const synthesisHeading = screen.getByRole("heading", {
    name: "SYNTHÈSE",
  });
  const annexHeading = screen.getByRole("heading", {
    name: "ANNEXE TECHNIQUE — INDICATEURS",
  });
  expect(
    referencesHeading.compareDocumentPosition(synthesisHeading) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    synthesisHeading.compareDocumentPosition(annexHeading) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  const diagnosticsPanel = screen.getByRole("region", {
    name: "Diagnostics de publication",
  });
  expect(diagnosticsPanel).toHaveTextContent("synthesis_output_invalid");
  expect(
    diagnosticsPanel.compareDocumentPosition(referencesHeading) &
      Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(screen.getByText("Lead sourcé.")).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Contexte" }),
  ).not.toBeInTheDocument();
  expect(screen.getByText("Paragraphe sourcé.")).toBeInTheDocument();
  expect(screen.getByText("Événement.")).toBeInTheDocument();
  expect(screen.getByText("evil.example")).toBeInTheDocument();
  expect(screen.queryByText("Attribution incertaine.")).not.toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "Commandes observées" }),
  ).toBeInTheDocument();
  expect(screen.getByText("powershell")).toBeInTheDocument();
  expect(screen.getByText("1 preuves")).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "Chaîne d’infection" }),
  ).toBeInTheDocument();
  expect(
    screen.getByText("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Figure 1 from the vendor report."),
  ).toBeInTheDocument();
  expect(
    screen.getByText("https://vendor.example/figure.png"),
  ).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: "Rapport" })[0]).toHaveAttribute(
    "href",
    VENDOR_URL,
  );
  expect(
    screen.getAllByRole("link", { name: "IOC source" })[0],
  ).toHaveAttribute("href", IOC_URL);
});

it("affiche une publication V4 sans enrichissement comme une publication narrative", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "publication-v4-empty",
        stage: "publication",
        version: 1,
        status: "verified",
        metadata: {},
        canonical_content: {
          schema_version: "4",
          subject_id: SYNTHESIS_SUBJECT_ID,
          publication_language: "fr",
          title: "Publication V4 minimale",
          lead: [],
          sections: [],
          timeline: [],
          indicators: [],
          sources: [],
          uncertainties: [],
          tables: [],
          diagrams: [],
          figures: [],
        },
      }),
    ),
  );

  renderArtifact("publication");

  expect(
    await screen.findByRole("heading", { name: "Publication V4 minimale" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Tableaux" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Diagrammes" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Figures source" }),
  ).not.toBeInTheDocument();
});

it("préserve la provenance visible d'un artifact réutilisé", async () => {
  stubProductionFetch({
    synthesis: synthesisArtifact(
      { title: "Synthèse canonique réutilisée" },
      {
        artifact_id: "synthesis-b",
        reused: true,
        reused_from_artifact_id: "synthesis-a",
        reused_from_created_at: "2026-08-10T10:00:00Z",
        metadata: { mode: "reuse_exact", language: "fr" },
      },
    ),
  });

  renderArtifact("synthesis");

  expect(
    await screen.findByText(/Réutilisé depuis un calcul précédent/),
  ).toBeInTheDocument();
  expect(screen.getByText(/artifact source : synthesis-a/)).toBeInTheDocument();
  expect(screen.getByText(/calcul original/)).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "Synthèse canonique réutilisée" }),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Réutilisation d’une synthèse identique"),
  ).toBeInTheDocument();
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

it("rend la synthèse canonique V1 et la provenance exacte de ses évidences", async () => {
  stubProductionFetch({
    synthesis: synthesisArtifact(
      {},
      {
        rendered_content:
          "# Titre Markdown non canonique\n\nNe pas interpréter.",
      },
    ),
    extraction: extractionArtifact([
      extractionSource(VENDOR_DOCUMENT_ID, VENDOR_URL),
      extractionSource(IOC_DOCUMENT_ID, IOC_URL),
    ]),
  });

  const { container } = renderArtifact("synthesis");

  expect(
    await screen.findByRole("heading", { name: "Campagne Cavern Manticore" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Langue de publication")).toBeInTheDocument();
  expect(screen.getByText("fr")).toBeInTheDocument();
  expect(screen.getByText("Mode de génération")).toBeInTheDocument();
  expect(screen.getByText("Rédaction initiale")).toBeInTheDocument();
  expect(
    screen.getByText("Le groupe exploite une faille d’accès initial."),
  ).toBeInTheDocument();
  expect(screen.getByText("3 preuves")).toBeInTheDocument();
  // Two refs on the same document: one link, not two.
  const leadParagraph = screen
    .getByText("Le groupe exploite une faille d’accès initial.")
    .closest(".synthesis-paragraph");
  expect(leadParagraph).not.toBeNull();
  expect(
    within(leadParagraph as HTMLElement).getAllByRole("link", {
      name: VENDOR_URL,
    }),
  ).toHaveLength(1);
  expect(
    within(leadParagraph as HTMLElement).getByRole("link", { name: IOC_URL }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: /Progression de l’attaque/ }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: /Contexte de la campagne/ }),
  ).not.toBeInTheDocument();
  expect(screen.getByText(/20 août 2026/)).toBeInTheDocument();
  expect(
    screen.getByText("Début de la campagne observée."),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Le domaine C2 peut être partagé."),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Un fait mineur n’a pas pu être rattaché."),
  ).toBeInTheDocument();
  // Le Markdown rendu reste une projection secondaire, jamais la source.
  expect(
    screen.queryByRole("heading", { name: /Markdown non canonique/ }),
  ).toBeNull();
  expect(screen.getByText(/Ne pas interpréter\./)).toBeInTheDocument();
  // L'identité interne d'évidence n'est pas un handle prompt exposé.
  expect(container.textContent).not.toContain(EVIDENCE_KEY);
  expect(container.querySelector('a[href*="#conversations"]')).toBeNull();
});

it("affiche une date explicite quand l’événement n’en fournit aucune", async () => {
  stubProductionFetch({
    synthesis: synthesisArtifact({
      sections: [],
      timeline: [
        {
          event_date: null,
          date_text: null,
          text: "Événement sans date précise.",
          evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "event")],
        },
      ],
      uncertainties: [],
      warnings: [],
    }),
  });

  renderArtifact("synthesis");

  expect(await screen.findByText(/Date non précisée/)).toBeInTheDocument();
  expect(screen.getByText("Événement sans date précise.")).toBeInTheDocument();
});

it("conserve une source d’évidence inconnue sans la substituer", async () => {
  stubProductionFetch({
    synthesis: synthesisArtifact({
      lead: [
        {
          text: "Un paragraphe cite trois évidences.",
          evidence_refs: [
            evidenceRef(VENDOR_DOCUMENT_ID, "fact"),
            evidenceRef(VENDOR_DOCUMENT_ID, "event"),
            evidenceRef(UNKNOWN_DOCUMENT_ID, "rule"),
          ],
        },
      ],
      sections: [],
      timeline: [],
      uncertainties: [],
      warnings: [],
    }),
    extraction: extractionArtifact([
      extractionSource(VENDOR_DOCUMENT_ID, VENDOR_URL),
    ]),
  });

  renderArtifact("synthesis");

  expect(await screen.findByText("3 preuves")).toBeInTheDocument();
  expect(screen.getAllByRole("link", { name: VENDOR_URL })).toHaveLength(1);
  const unknown = screen.getByText(
    `Document source indisponible : ${UNKNOWN_DOCUMENT_ID}`,
  );
  expect(unknown).toHaveAttribute(
    "data-source-document-id",
    UNKNOWN_DOCUMENT_ID,
  );
  expect(unknown).toHaveAttribute("title", UNKNOWN_DOCUMENT_ID);
  expect(screen.queryByRole("link", { name: IOC_URL })).toBeNull();
  expect(
    screen.getByText("Aucun événement de chronologie."),
  ).toBeInTheDocument();
  expect(screen.getByText("Aucune incertitude signalée.")).toBeInTheDocument();
  expect(screen.getByText("Aucun warning.")).toBeInTheDocument();
});

it("rend la synthèse canonique même quand l’extraction n’est pas résolue", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/production/artifacts/synthesis")) {
        return Promise.resolve(Response.json(synthesisArtifact()));
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );

  renderArtifact("synthesis");

  expect(
    await screen.findByRole("heading", { name: "Campagne Cavern Manticore" }),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/Extraction canonique indisponible/),
  ).toBeInTheDocument();
  expect(
    screen.getAllByText(`Document source indisponible : ${VENDOR_DOCUMENT_ID}`)
      .length,
  ).toBeGreaterThan(0);
  expect(screen.getByText("3 preuves")).toBeInTheDocument();
});

it("décode strictement la charge canonique de synthèse V1", () => {
  const canonical = synthesisArtifact().canonical_content;
  expect(isProductionSynthesisV1(canonical)).toBe(true);
  expect(
    isProductionSynthesisV1({
      ...canonical,
      prompt_handles: ["handle-1"],
    }),
  ).toBe(false);
  const missingTitle: Record<string, unknown> = { ...canonical };
  delete missingTitle.title;
  expect(isProductionSynthesisV1(missingTitle)).toBe(false);
  expect(
    isProductionSynthesisV1({
      ...canonical,
      lead: [{ text: "Sans évidence.", evidence_refs: [] }],
    }),
  ).toBe(false);
  expect(
    isProductionSynthesisV1({
      ...canonical,
      sections: [{ kind: "h1", heading: "Titre", paragraphs: [] }],
    }),
  ).toBe(false);
  expect(
    isProductionSynthesisV1({
      ...canonical,
      timeline: [
        {
          event_date: "2026-13-45",
          date_text: null,
          text: "Date invalide.",
          evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "event")],
        },
      ],
    }),
  ).toBe(false);
});
