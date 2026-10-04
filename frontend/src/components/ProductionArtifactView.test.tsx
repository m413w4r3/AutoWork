import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
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
    | "editorial_enrichment"
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

function revisionBaseArtifact(version = 2) {
  return {
    artifact_id: "editorial-enrichment-base",
    stage: "editorial_enrichment",
    version,
    status: "verified",
    input_hash: "a".repeat(64),
    canonical_sha256: "b".repeat(64),
    metadata: {},
    canonical_content: {
      tables: [
        {
          key: "tools_table",
          title: "Outils observés",
          rows: [{ cells: ["ExampleRAT", "malware"] }],
        },
      ],
      diagrams: [{ key: "infection_chain", title: "Séquence observée" }],
      source_figures: [],
    },
  };
}

function revisionResponse(outcome: "revised" | "needs_new_evidence") {
  return {
    outcome,
    request_identity: "revision-request-identity",
    artifact_id: "editorial-enrichment-revision",
    artifact_version: 3,
    artifact_input_hash: "a".repeat(64),
    artifact_canonical_sha256: "c".repeat(64),
    publication_artifact_id: "publication-revision",
    publication_artifact_version: 2,
    previous_publication_artifact_id: "publication-previous",
    revision: {
      request_identity: "revision-request-identity",
      outcome,
      element_kind: "table",
      element_key: "tools_table",
      action: "improve_table",
      instruction: "Clarifier la table sans ajouter de faits.",
      base_artifact_id: "editorial-enrichment-base",
      base_version: 2,
      element_evidence_handles: ["E001"],
      admitted_evidence_handles: ["E001", "E002"],
      element_before: { title: "Outils observés" },
      element_after: { title: "Outils documentés" },
      validator_results: [
        { validator: "l8_enrichment_grounding", status: "passed" },
      ],
      validator_rejections: [],
      parse_identity: "parse-identity",
      previous_publication_artifact_id: "publication-previous",
      resource_need:
        outcome === "needs_new_evidence"
          ? {
              key: "N001",
              kind: "TECHNICAL_ANALYSIS",
              reason: "Le détail demandé est absent des preuves admises.",
              query_hint: "ExampleRAT détail protocole",
            }
          : null,
    },
  };
}

function stubRevisionFetch(
  revision: ReturnType<typeof revisionResponse> | null,
  revisionStatus = 200,
  errorDetail: Record<string, unknown> = {
    code: "editorial_enrichment_stale_base",
    message: "L'artifact de base a changé.",
  },
) {
  const currentArtifact = revisionBaseArtifact();
  const fetchMock = vi.fn(
    (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = urlOf(input);
      if (url.endsWith("/production/artifacts/editorial_enrichment")) {
        return Promise.resolve(Response.json(currentArtifact));
      }
      if (
        url.includes("/production/artifacts/editorial_enrichment/revisions")
      ) {
        if (revisionStatus !== 200) {
          return Promise.resolve(
            new Response(JSON.stringify({ detail: errorDetail }), {
              status: revisionStatus,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        expect(init?.method).toBe("POST");
        if (revision?.outcome === "revised") {
          currentArtifact.artifact_id = revision.artifact_id;
          currentArtifact.version = revision.artifact_version;
          currentArtifact.canonical_sha256 = revision.artifact_canonical_sha256;
        }
        return Promise.resolve(Response.json(revision));
      }
      if (url.includes("/production/artifacts/editorial_enrichment/")) {
        return Promise.resolve(
          Response.json({
            ...revisionBaseArtifact(),
            artifact_id: "editorial-enrichment-base",
            canonical_content: { history_label: "Ancienne version conservée" },
          }),
        );
      }
      if (url.includes("/publication/preview")) {
        return Promise.resolve(
          Response.json({
            status: "FAILED",
            artifact_id: new URL(url, "http://localhost").searchParams.get(
              "artifact_id",
            ),
            artifact_version: 1,
            error_code: "preview_not_ready",
            pdf_url: null,
          }),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function clickFirstRevisionButton() {
  const [button] = screen.getAllByRole("button", {
    name: "Réviser cet élément",
  });
  if (!button) throw new Error("Revision action button is missing");
  fireEvent.click(button);
}

it("submits a targeted element revision and shows its comparison and previous version", async () => {
  const fetchMock = stubRevisionFetch(revisionResponse("revised"));
  renderArtifact("editorial_enrichment");

  expect(
    await screen.findByRole("heading", { name: "Outils observés" }),
  ).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Instruction pour Outils observés"), {
    target: { value: "Clarifier la table sans ajouter de faits." },
  });
  clickFirstRevisionButton();

  expect(await screen.findByText("Nouvelle version 3")).toBeInTheDocument();
  expect(
    await screen.findByText(/Ancienne version conservée/),
  ).toBeInTheDocument();
  expect(
    screen.getByText("PDF de la publication précédente"),
  ).toBeInTheDocument();
  expect(
    screen.getByText("PDF de la nouvelle publication 2"),
  ).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.some(
      ([input, init]) =>
        urlOf(input).includes("/editorial_enrichment/revisions") &&
        init?.method === "POST",
    ),
  ).toBe(true);
});

it("explains when a revision needs new evidence", async () => {
  stubRevisionFetch(revisionResponse("needs_new_evidence"));
  renderArtifact("editorial_enrichment");

  await screen.findAllByRole("button", { name: "Réviser cet élément" });
  clickFirstRevisionButton();

  expect(
    await screen.findByText(/Relancez la collecte et la production/),
  ).toBeInTheDocument();
  expect(screen.getByText(/ExampleRAT détail protocole/)).toBeInTheDocument();
});

it("shows a clear stale-base conflict when the server returns 409", async () => {
  stubRevisionFetch(null, 409);
  renderArtifact("editorial_enrichment");

  await screen.findAllByRole("button", { name: "Réviser cet élément" });
  clickFirstRevisionButton();

  expect(
    await screen.findByText(
      "Cette base est obsolète. Rechargez l’enrichissement et réessayez.",
    ),
  ).toBeInTheDocument();
});

it("surfaces validator rejections returned for an invalid revision", async () => {
  stubRevisionFetch(null, 422, {
    code: "editorial_enrichment_revision_rejected",
    rejections: [
      { block_id: "tools_table", reason_code: "unsupported_evidence_claim" },
    ],
  });
  renderArtifact("editorial_enrichment");

  await screen.findAllByRole("button", { name: "Réviser cet élément" });
  clickFirstRevisionButton();

  expect(
    await screen.findByText("tools_table : unsupported_evidence_claim"),
  ).toBeInTheDocument();
});

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

it("ne rend pas une publication dont le schema n'est ni V4 ni V5", async () => {
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

it("ne répète pas dans les sections un paragraphe déjà affiché dans le lead", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "publication-duplicate-lead",
        stage: "publication",
        version: 1,
        status: "verified",
        metadata: {},
        canonical_content: {
          schema_version: "4",
          subject_id: SYNTHESIS_SUBJECT_ID,
          publication_language: "fr",
          title: "Publication avec lead répété",
          lead: [
            {
              text: "Straße assessment.",
              evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
            },
          ],
          sections: [
            {
              kind: "overview",
              heading: "",
              paragraphs: [
                {
                  text: "  STRASSE   ASSESSMENT.  ",
                  evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                },
                {
                  text: "Unique section detail.",
                  evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
                },
              ],
            },
          ],
          timeline: [],
          indicators: [],
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
          ],
          uncertainties: [],
          tables: [],
          diagrams: [],
          figures: [],
        },
      }),
    ),
  );

  renderArtifact("publication");

  expect(await screen.findByText("Straße assessment.")).toBeInTheDocument();
  expect(screen.getAllByText("Straße assessment.")).toHaveLength(1);
  expect(screen.getByText("Unique section detail.")).toBeInTheDocument();
});

it("affiche la publication V4 et ses enrichissements", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      if (urlOf(input).includes("/editorial_enrichment")) {
        return Promise.resolve(
          Response.json({
            artifact_id: "editorial-enrichment-v4",
            stage: "editorial_enrichment",
            version: 2,
            status: "verified",
            metadata: {},
            canonical_content: {
              schema_version: "3",
              warnings: ["figure_candidate_unavailable"],
              resource_needs: [
                {
                  key: "need-command-telemetry",
                  kind: "MEDIA",
                  reason: "Illustration de télémétrie absente.",
                },
              ],
            },
          }),
        );
      }
      return Promise.resolve(
        Response.json({
          artifact_id: "publication-v4",
          stage: "publication",
          version: 1,
          status: "verified",
          metadata: {
            input_artifacts: {
              editorial_enrichment_artifact_id: "editorial-enrichment-v4",
            },
            diagnostics: {
              warnings_by_stage: {
                synthesis: ["synthesis_output_invalid"],
              },
              synthesis: {
                rejected_blocks: [
                  { block_id: "B003", reason_code: "unsupported_claim" },
                ],
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
      );
    }),
  );

  renderArtifact("publication");

  expect(
    await screen.findByRole("heading", { name: "Article canonique" }),
  ).toBeInTheDocument();
  const referencesHeading = screen.getByRole("heading", {
    name: "RÉFÉRENCES",
  });
  expect(screen.getAllByRole("heading", { name: "RÉFÉRENCES" })).toHaveLength(
    1,
  );
  expect(
    screen.queryByRole("heading", { name: "Chronologie" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "Sources complémentaires" }),
  ).not.toBeInTheDocument();
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
    await within(diagnosticsPanel).findByText(/need-command-telemetry/),
  ).toBeInTheDocument();
  expect(diagnosticsPanel).toHaveTextContent("B003");
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
  expect(screen.getByText("1 septembre 2026 :")).toBeInTheDocument();
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
    screen.getByRole("img", { name: "Chaîne d’infection" }),
  ).toHaveAttribute(
    "src",
    "/api/subjects/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/publication/assets/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  );
  expect(
    screen.getByRole("img", { name: "Architecture source." }),
  ).toHaveAttribute(
    "src",
    "/api/subjects/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/publication/assets/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  );
  const diagram = screen.getByRole("img", { name: "Chaîne d’infection" });
  expect(diagram.closest("article")).not.toHaveTextContent(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  );
  expect(diagnosticsPanel.querySelector("pre")).toHaveTextContent(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  );
  expect(diagnosticsPanel.querySelector("pre")).toHaveTextContent(
    "Figure 1 from the vendor report.",
  );
  expect(diagnosticsPanel.querySelector("pre")).toHaveTextContent(
    "https://vendor.example/figure.png",
  );
  expect(screen.getAllByRole("link", { name: "Rapport" })[0]).toHaveAttribute(
    "href",
    VENDOR_URL,
  );
  expect(
    screen.getAllByRole("link", { name: "IOC source" })[0],
  ).toHaveAttribute("href", IOC_URL);
  const [lineageButton] = screen.getAllByRole("button", {
    name: "Voir les sources et preuves",
  });
  if (!lineageButton) throw new Error("Aucun bouton de sources et preuves.");
  fireEvent.click(lineageButton);
  const lineagePanel = screen.getByRole("complementary", {
    name: "Sources et preuves du passage",
  });
  expect(
    within(lineagePanel).getByRole("link", { name: "Rapport" }),
  ).toHaveAttribute("href", VENDOR_URL);
  expect(lineagePanel).toHaveTextContent(EVIDENCE_KEY);
});

it("affiche le PDF compilé avec son artifact, son rendu et son statut", async () => {
  const artifactId = "55555555-5555-4555-8555-555555555555";
  const renderIdentity = "e".repeat(64);
  const originalCreateObjectURL = Object.getOwnPropertyDescriptor(
    URL,
    "createObjectURL",
  );
  const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(
    URL,
    "revokeObjectURL",
  );
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn().mockReturnValue("blob:publication-preview"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  let unmount: (() => void) | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/publication/preview?")) {
        return Promise.resolve(
          Response.json({
            status: "READY",
            artifact_id: artifactId,
            artifact_version: 8,
            artifact_input_hash: "a".repeat(64),
            current_artifact_id: artifactId,
            current_artifact_version: 8,
            render_id: "66666666-6666-4666-8666-666666666666",
            render_identity: renderIdentity,
            render_disposition: "ACCEPTED_VERSION",
            published_edition_version: 12,
            error_code: null,
            error_message: null,
            pdf_url: "/api/subjects/subject-1/publication/preview/pdf",
          }),
        );
      }
      if (url.endsWith("/publication/preview/pdf")) {
        return Promise.resolve(
          new Response(new Uint8Array([37, 80, 68, 70]), {
            headers: { "Content-Type": "application/pdf" },
          }),
        );
      }
      return Promise.resolve(
        Response.json({
          artifact_id: artifactId,
          stage: "publication",
          version: 8,
          status: "verified",
          metadata: {},
          canonical_content: {
            schema_version: "4",
            subject_id: SYNTHESIS_SUBJECT_ID,
            publication_language: "fr",
            title: "Publication PDF",
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
      );
    }),
  );

  try {
    unmount = renderArtifact("publication").unmount;

    const viewer = await screen.findByTestId("publication-pdf-viewer");
    expect(viewer).toHaveAttribute("type", "application/pdf");
    expect(await screen.findByText("PDF prêt")).toBeInTheDocument();
    expect(screen.getByText(renderIdentity)).toBeInTheDocument();
    expect(
      screen.getByText(/Version acceptée de l’édition 12/),
    ).toBeInTheDocument();
  } finally {
    unmount?.();
    if (originalCreateObjectURL) {
      Object.defineProperty(URL, "createObjectURL", originalCreateObjectURL);
    }
    if (originalRevokeObjectURL) {
      Object.defineProperty(URL, "revokeObjectURL", originalRevokeObjectURL);
    }
  }
});

it("signale un PDF obsolète sans l’afficher avec la nouvelle proposition", async () => {
  const currentId = "77777777-7777-4777-8777-777777777777";
  const oldId = "88888888-8888-4888-8888-888888888888";
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/publication/preview?")) {
        return Promise.resolve(
          Response.json({
            status: "STALE",
            artifact_id: oldId,
            artifact_version: 2,
            artifact_input_hash: "a".repeat(64),
            current_artifact_id: currentId,
            current_artifact_version: 3,
            render_id: "99999999-9999-4999-8999-999999999999",
            render_identity: "d".repeat(64),
            render_disposition: "EXPLICIT_RENDER",
            published_edition_version: null,
            error_code: "publication_preview_stale",
            error_message: "A newer artifact is current.",
            pdf_url: null,
          }),
        );
      }
      return Promise.resolve(
        Response.json({
          artifact_id: currentId,
          stage: "publication",
          version: 3,
          status: "verified",
          metadata: {},
          canonical_content: {
            schema_version: "4",
            subject_id: SYNTHESIS_SUBJECT_ID,
            publication_language: "fr",
            title: "Proposition récente",
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
      );
    }),
  );

  renderArtifact("publication");

  expect(await screen.findByText("Obsolète")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(
    "n’est pas affiché avec cette proposition",
  );
  expect(
    screen.queryByTestId("publication-pdf-viewer"),
  ).not.toBeInTheDocument();
});

it("applique les rôles typographiques sémantiques d’un document V5", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        artifact_id: "publication-v5",
        stage: "publication",
        version: 5,
        status: "verified",
        metadata: {},
        canonical_content: {
          schema_version: "5",
          subject_id: SYNTHESIS_SUBJECT_ID,
          publication_language: "fr",
          title: "APT Fjord utilise PowerShell",
          lead: [
            {
              text: "APT Fjord lance PowerShell et déclenche un beacon.",
              evidence_refs: [evidenceRef(VENDOR_DOCUMENT_ID, "fact")],
            },
          ],
          sections: [],
          timeline: [],
          indicators: [],
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
          ],
          uncertainties: [],
          tables: [],
          diagrams: [],
          figures: [],
          rich_text: {
            schema_version: "1",
            policy_version: "semantic-annotation-policy-v1",
            paragraphs: [
              {
                anchor: "title",
                spans: [
                  { role: "actor", text: "APT Fjord" },
                  { role: "text", text: " utilise PowerShell" },
                ],
              },
              {
                anchor: "lead:0001",
                spans: [
                  { role: "actor", text: "APT Fjord" },
                  { role: "text", text: " lance " },
                  { role: "command", text: "PowerShell" },
                  { role: "text", text: " et déclenche un " },
                  { role: "english_term", text: "beacon" },
                  { role: "text", text: "." },
                ],
              },
            ],
          },
        },
      }),
    ),
  );

  renderArtifact("publication");

  expect(
    await screen.findAllByText("APT Fjord", { selector: "strong" }),
  ).toHaveLength(2);
  expect(
    screen.getByText("PowerShell", { selector: "code" }),
  ).toBeInTheDocument();
  expect(screen.getByText("beacon", { selector: "em" })).toBeInTheDocument();
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
