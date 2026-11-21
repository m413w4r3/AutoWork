import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import {
  type PublicationDocumentV4,
  type PublicationDocumentV5,
  type PublicationDocumentV6,
  type PublicationDocumentV7,
} from "../../api/production";
import {
  ProductionArtifactView,
  PublicationDocumentView,
} from "../../components/ProductionArtifactView";

afterEach(() => vi.unstubAllGlobals());

const SOURCE_ID = "source-sample-1";
const SUBJECT_ID = "subject-sample-1";
const SOURCE_URL = "https://sample.invalid/report";

const evidenceRef = {
  source_document_id: SOURCE_ID,
  kind: "fact",
  evidence_key: "sample-evidence-1",
} as const;

const baseDocument: PublicationDocumentV4 = {
  schema_version: "4",
  subject_id: SUBJECT_ID,
  publication_language: "fr",
  title: "Dossier Sample Orchard",
  lead: [
    {
      text: "Le groupe Sample Orchard est documenté.",
      evidence_refs: [evidenceRef],
    },
  ],
  sections: [
    {
      kind: "analysis",
      heading: "Analyse",
      paragraphs: [
        {
          text: "Le groupe Sample Orchard est documenté.",
          evidence_refs: [evidenceRef],
        },
        {
          text: "Une seconde observation complète le dossier.",
          evidence_refs: [evidenceRef],
        },
      ],
    },
  ],
  timeline: [
    {
      event_date: "2026-10-12",
      date_text: null,
      text: "Une activité est observée le 12 octobre.",
      evidence_refs: [evidenceRef],
    },
  ],
  indicators: [
    {
      artifact_type: "domain",
      indicators: [
        {
          value: "sample.invalid",
          normalized_value: "sample.invalid",
          artifact_type: "domain",
          source_document_ids: [SOURCE_ID],
        },
      ],
    },
  ],
  sources: [
    {
      source_document_id: SOURCE_ID,
      canonical_url: SOURCE_URL,
      title: "Sample report",
      publisher: "Sample Publisher",
      published_at: "2026-10-12T00:00:00Z",
      tier: "core",
      kind: "publication",
      role: "primary",
    },
  ],
  uncertainties: [
    { text: "Une donnée reste incertaine.", source_document_ids: [SOURCE_ID] },
  ],
  tables: [
    {
      key: "sample-table",
      kind: "summary",
      title: "Éléments observés",
      caption: "Résumé de la source",
      columns: [{ key: "element", label: "Élément" }],
      rows: [{ cells: ["Échantillon"], evidence_refs: [evidenceRef] }],
      placement: { kind: "after_timeline", section_index: null },
    },
  ],
  diagrams: [
    {
      key: "sample-diagram",
      kind: "flow",
      title: "Chaîne résumée",
      caption: "Schéma de synthèse",
      direction: "LR",
      nodes: [],
      edges: [],
      groups: [],
      placement: { kind: "after_lead", section_index: null },
      asset_id: "diagram-asset",
    },
  ],
  figures: [
    {
      key: "sample-figure",
      asset_id: "figure-asset",
      sha256: "a".repeat(64),
      mime_type: "image/png",
      byte_size: 128,
      source_document_id: SOURCE_ID,
      source_url: SOURCE_URL,
      caption: "Figure archivée de la source",
      provenance: "Figure du rapport sample",
      locator: {
        page: 2,
        section: "Annexe",
        figure_label: "Figure 1",
        original_asset_url: null,
      },
      placement: { kind: "after_section", section_index: 0 },
    },
  ],
};

const documentV5: PublicationDocumentV5 = {
  ...baseDocument,
  schema_version: "5",
  references: [
    {
      source_document_id: SOURCE_ID,
      text: "La source décrit une activité documentée.",
      evidence_refs: [evidenceRef],
    },
  ],
  original_indicators: [
    {
      artifact_type: "domain",
      indicators: [
        {
          value: "unlinked.sample.invalid",
          normalized_value: "unlinked.sample.invalid",
          artifact_type: "domain",
          source_document_ids: [SOURCE_ID],
        },
      ],
    },
  ],
  rich_text: {
    schema_version: "1",
    policy_version: "sample-policy-v1",
    paragraphs: [
      {
        anchor: "title",
        spans: [{ role: "actor", text: "Dossier Sample Orchard" }],
      },
      {
        anchor: "lead:0001",
        spans: [
          { role: "text", text: "Le groupe " },
          { role: "actor", text: "Sample Orchard" },
          { role: "text", text: " est documenté." },
        ],
      },
      {
        anchor: "section:0:paragraph:0002",
        spans: [
          {
            role: "command",
            text: "Une seconde observation complète le dossier.",
          },
        ],
      },
      {
        anchor: "timeline:0001",
        spans: [
          { role: "text", text: "Une activité est observée le 12 octobre." },
        ],
      },
      {
        anchor: "reference:0001",
        spans: [
          { role: "source", text: "La source décrit une activité documentée." },
        ],
      },
      {
        anchor: "table:sample-table:title",
        spans: [{ role: "actor", text: "Éléments observés" }],
      },
      {
        anchor: "table:sample-table:caption",
        spans: [{ role: "text", text: "Résumé de la source" }],
      },
      {
        anchor: "table:sample-table:column:0001",
        spans: [{ role: "text", text: "Élément" }],
      },
      {
        anchor: "table:sample-table:row:0001:cell:0001",
        spans: [{ role: "text", text: "Échantillon" }],
      },
      {
        anchor: "diagram:sample-diagram:title",
        spans: [{ role: "text", text: "Chaîne résumée" }],
      },
      {
        anchor: "diagram:sample-diagram:caption",
        spans: [{ role: "text", text: "Schéma de synthèse" }],
      },
      {
        anchor: "figure:sample-figure:caption",
        spans: [{ role: "text", text: "Figure archivée de la source" }],
      },
    ],
  },
};

const documentV6: PublicationDocumentV6 = {
  schema_version: "6",
  base_publication: documentV5,
  provenance: {
    base_publication_artifact_id: "publication-sample-v5",
    base_publication_sha256: "b".repeat(64),
    investigation_id: null,
    opinion_id: null,
    opinion_version: null,
    opinion_content_sha256: null,
    reviewer_id: null,
    approver_id: null,
  },
  analyst_opinion: null,
  rendered_opinion: null,
};

const documentV7: PublicationDocumentV7 = {
  schema_version: "7",
  base_publication: documentV6,
  publication_kind: "brief",
  routing_decision_hash: "c".repeat(64),
  routing_policy_version: "sample-routing-v1",
};

function renderDocumentView(
  document: PublicationDocumentV4 | PublicationDocumentV5,
) {
  return render(
    <PublicationDocumentView document={document} publicationKind={null} />,
  );
}

it("explains a historical publication is read only and returns to the current version", () => {
  render(
    <PublicationDocumentView
      document={baseDocument}
      publicationKind="article"
      readOnlyReason="historical_version"
      currentVersionHref={`/subjects/${SUBJECT_ID}/editor`}
    />,
  );

  expect(screen.getByText("Version historique — lecture seule")).toBeVisible();
  expect(screen.getByText("Raison : version historique.")).toBeVisible();
  expect(
    screen.getByRole("link", { name: "Revenir à la version courante" }),
  ).toHaveAttribute("href", `/subjects/${SUBJECT_ID}/editor`);
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it("does not label a current read-only publication as historical", () => {
  render(
    <PublicationDocumentView
      document={baseDocument}
      publicationKind={null}
      readOnlyReason="publication_kind_unknown"
      currentVersionHref={`/subjects/${SUBJECT_ID}/editor`}
    />,
  );

  expect(screen.queryByText("Version historique — lecture seule")).toBeNull();
  expect(screen.getByText("Publication en lecture seule")).toBeVisible();
  expect(screen.getByText(/type de publication inconnu/i)).toBeVisible();
  expect(
    screen.queryByRole("link", { name: "Revenir à la version courante" }),
  ).toBeNull();
});

function renderArtifactView(
  canonicalContent: unknown,
  publicationKind: "article" | "brief" | null = null,
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL) => {
      const url =
        typeof input === "string"
          ? input
          : input instanceof URL
            ? input.href
            : input.url;
      if (url.includes("/production/artifacts/publication")) {
        return Promise.resolve(
          Response.json({
            artifact_id: "publication-sample-v7",
            stage: "publication",
            version: 7,
            status: "verified",
            publication_kind: publicationKind,
            metadata: {},
            canonical_content: canonicalContent,
          }),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    }),
  );
  return render(
    <QueryClientProvider client={client}>
      <ProductionArtifactView subjectId={SUBJECT_ID} stage="publication" />
    </QueryClientProvider>,
  );
}

it("test_v8_brief_read_fixture_renders_in_the_historical_view", async () => {
  renderArtifactView(
    {
      schema_version: "8",
      base_publication: documentV7,
      article_format: null,
    },
    "brief",
  );

  expect(await screen.findByText("Dossier Sample Orchard")).toBeInTheDocument();
  expect(screen.getByText("Format : Brève")).toBeInTheDocument();
  expect(screen.queryByText(/ANNEXE TECHNIQUE/)).not.toBeInTheDocument();
});

function publicationLayout(container: HTMLElement): HTMLElement {
  const layout = container.querySelector<HTMLElement>(
    ".publication-preview__layout",
  );
  if (!layout) throw new Error("Publication document layout was not rendered");
  return layout;
}

it("caractérise le rendu du document publié V4", () => {
  const { container } = renderDocumentView(baseDocument);
  expect(publicationLayout(container)).toMatchInlineSnapshot(`
    <div
      class="publication-preview__layout"
    >
      <article
        class="publication-preview"
      >
        <h3>
          Dossier Sample Orchard
        </h3>
        <section
          aria-label="RÉFÉRENCES"
        >
          <h4>
            RÉFÉRENCES
          </h4>
          <div
            class="publication-preview__passage"
          >
            <p>
              <strong>
                12 octobre 2026
                 : 
              </strong>
              Une activité est observée le 12 octobre.
              <span
                class="publication-preview__provenance"
              >
                <span>
                   
                  <a
                    href="https://sample.invalid/report"
                    rel="noreferrer"
                    target="_blank"
                  >
                    Sample report
                  </a>
                </span>
              </span>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <section>
            <h4>
              Tableaux
            </h4>
            <article>
              <h5>
                Éléments observés
              </h5>
              <p>
                Résumé de la source
              </p>
              <table>
                <thead>
                  <tr>
                    <th>
                      Élément
                    </th>
                    <th>
                      Preuves
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>
                      Échantillon
                    </td>
                    <td>
                      1
                       preuves
                    </td>
                  </tr>
                </tbody>
              </table>
            </article>
          </section>
        </section>
        <section
          aria-label="SYNTHÈSE"
        >
          <h4>
            SYNTHÈSE
          </h4>
          <div
            class="publication-preview__passage"
          >
            <p>
              Le groupe Sample Orchard est documenté.
              <span
                class="publication-preview__provenance"
              >
                <span>
                   
                  <a
                    href="https://sample.invalid/report"
                    rel="noreferrer"
                    target="_blank"
                  >
                    Sample report
                  </a>
                </span>
              </span>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <section>
            <h4>
              Diagrammes
            </h4>
            <article>
              <h5>
                Chaîne résumée
              </h5>
              <figure>
                <img
                  alt="Chaîne résumée"
                  loading="lazy"
                  src="/api/subjects/subject-sample-1/publication/assets/diagram-asset"
                />
                <figcaption>
                  Schéma de synthèse
                </figcaption>
              </figure>
            </article>
          </section>
          <div>
            <h5>
              Analyse
            </h5>
            <div
              class="publication-preview__passage"
            >
              <p>
                Une seconde observation complète le dossier.
                <span
                  class="publication-preview__provenance"
                >
                  <span>
                     
                    <a
                      href="https://sample.invalid/report"
                      rel="noreferrer"
                      target="_blank"
                    >
                      Sample report
                    </a>
                  </span>
                </span>
              </p>
              <button
                class="publication-preview__lineage-trigger"
                type="button"
              >
                Voir les sources et preuves
              </button>
            </div>
            <section>
              <h4>
                Figures source
              </h4>
              <article>
                <figure>
                  <img
                    alt="Figure archivée de la source"
                    loading="lazy"
                    src="/api/subjects/subject-sample-1/publication/assets/figure-asset"
                  />
                  <figcaption>
                    Figure archivée de la source
                  </figcaption>
                </figure>
              </article>
            </section>
          </div>
        </section>
        <section
          aria-label="ANNEXE TECHNIQUE — INDICATEURS"
        >
          <h4>
            ANNEXE TECHNIQUE — INDICATEURS
          </h4>
          <div>
            <h5>
              Noms de domaine
            </h5>
            <ul>
              <li>
                <code>
                  sample.invalid
                </code>
                <span
                  class="publication-preview__provenance"
                >
                  <span>
                     
                    <a
                      href="https://sample.invalid/report"
                      rel="noreferrer"
                      target="_blank"
                    >
                      Sample report
                    </a>
                  </span>
                </span>
              </li>
            </ul>
          </div>
        </section>
      </article>
    </div>
  `);
});

it("caractérise le rendu du document publié V5 et du panneau de preuves", () => {
  const { container } = renderDocumentView(documentV5);
  expect(publicationLayout(container)).toMatchInlineSnapshot(`
    <div
      class="publication-preview__layout"
    >
      <article
        class="publication-preview"
      >
        <h3>
          <strong
            class="semantic-role semantic-role--actor"
          >
            Dossier Sample Orchard
          </strong>
        </h3>
        <section
          aria-label="RÉFÉRENCES"
        >
          <h4>
            RÉFÉRENCES
          </h4>
          <div
            class="publication-preview__passage"
          >
            <p>
              <strong>
                12 octobre 2026
                 : 
              </strong>
              <span>
                Une activité est observée le 12 octobre.
              </span>
              <span
                class="publication-preview__provenance"
              >
                <span>
                   
                  <a
                    href="https://sample.invalid/report"
                    rel="noreferrer"
                    target="_blank"
                  >
                    Sample report
                  </a>
                </span>
              </span>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <div
            class="publication-preview__passage"
          >
            <p>
              <strong>
                12 octobre 2026
                 
                :
                 
              </strong>
              <span
                class="semantic-role semantic-role--source"
              >
                La source décrit une activité documentée.
              </span>
               
              <a
                href="https://sample.invalid/report"
                rel="noreferrer"
                target="_blank"
              >
                Sample report
              </a>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <section>
            <h4>
              Tableaux
            </h4>
            <article>
              <h5>
                <strong
                  class="semantic-role semantic-role--actor"
                >
                  Éléments observés
                </strong>
              </h5>
              <p>
                <span>
                  Résumé de la source
                </span>
              </p>
              <table>
                <thead>
                  <tr>
                    <th>
                      <span>
                        Élément
                      </span>
                    </th>
                    <th>
                      Preuves
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>
                      <span>
                        Échantillon
                      </span>
                    </td>
                    <td>
                      1
                       preuves
                    </td>
                  </tr>
                </tbody>
              </table>
            </article>
          </section>
        </section>
        <section
          aria-label="SYNTHÈSE"
        >
          <h4>
            SYNTHÈSE
          </h4>
          <div
            class="publication-preview__passage"
          >
            <p>
              <span>
                Le groupe 
              </span>
              <strong
                class="semantic-role semantic-role--actor"
              >
                Sample Orchard
              </strong>
              <span>
                 est documenté.
              </span>
              <span
                class="publication-preview__provenance"
              >
                <span>
                   
                  <a
                    href="https://sample.invalid/report"
                    rel="noreferrer"
                    target="_blank"
                  >
                    Sample report
                  </a>
                </span>
              </span>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <section>
            <h4>
              Diagrammes
            </h4>
            <article>
              <h5>
                <span>
                  Chaîne résumée
                </span>
              </h5>
              <figure>
                <img
                  alt="Chaîne résumée"
                  loading="lazy"
                  src="/api/subjects/subject-sample-1/publication/assets/diagram-asset"
                />
                <figcaption>
                  <span>
                    Schéma de synthèse
                  </span>
                </figcaption>
              </figure>
            </article>
          </section>
          <div>
            <h5>
              Analyse
            </h5>
            <div
              class="publication-preview__passage"
            >
              <p>
                <code
                  class="semantic-role semantic-role--command"
                >
                  Une seconde observation complète le dossier.
                </code>
                <span
                  class="publication-preview__provenance"
                >
                  <span>
                     
                    <a
                      href="https://sample.invalid/report"
                      rel="noreferrer"
                      target="_blank"
                    >
                      Sample report
                    </a>
                  </span>
                </span>
              </p>
              <button
                class="publication-preview__lineage-trigger"
                type="button"
              >
                Voir les sources et preuves
              </button>
            </div>
            <section>
              <h4>
                Figures source
              </h4>
              <article>
                <figure>
                  <img
                    alt="Figure archivée de la source"
                    loading="lazy"
                    src="/api/subjects/subject-sample-1/publication/assets/figure-asset"
                  />
                  <figcaption>
                    <span>
                      Figure archivée de la source
                    </span>
                  </figcaption>
                </figure>
              </article>
            </section>
          </div>
        </section>
        <section
          aria-label="ANNEXE TECHNIQUE — IOC (1)"
        >
          <h4>
            ANNEXE TECHNIQUE — IOC (1)
          </h4>
          <div>
            <h5>
              Noms de domaine
               (1)
            </h5>
            <ul>
              <li>
                <code>
                  sample.invalid
                </code>
                <span
                  class="publication-preview__provenance"
                >
                  <span>
                     
                    <a
                      href="https://sample.invalid/report"
                      rel="noreferrer"
                      target="_blank"
                    >
                      Sample report
                    </a>
                  </span>
                </span>
              </li>
            </ul>
          </div>
          <div>
            <h5>
              IOC originaux (
              1
              )
            </h5>
            <p>
              Le lien avec le sujet n’est pas démontré. Sources :
               
              Sample Publisher
              .
            </p>
            <div>
              <h6>
                Noms de domaine
                 (
                1
                )
              </h6>
              <ul>
                <li>
                  <code>
                    unlinked.sample.invalid
                  </code>
                  <span
                    class="publication-preview__provenance"
                  >
                    <span>
                       
                      <a
                        href="https://sample.invalid/report"
                        rel="noreferrer"
                        target="_blank"
                      >
                        Sample report
                      </a>
                    </span>
                  </span>
                </li>
              </ul>
            </div>
          </div>
        </section>
      </article>
    </div>
  `);
});

it("caractérise le panneau de provenance et lineage du document V5", () => {
  const { container } = renderDocumentView(documentV5);
  const [lineageTrigger] = screen.getAllByRole("button", {
    name: "Voir les sources et preuves",
  });
  if (!lineageTrigger) throw new Error("No provenance button was rendered");
  fireEvent.click(lineageTrigger);
  expect(container.querySelector(".publication-lineage-panel"))
    .toMatchInlineSnapshot(`
    <aside
      aria-label="Sources et preuves du passage"
      class="publication-lineage-panel"
    >
      <div
        class="publication-lineage-panel__header"
      >
        <h4>
          Sources et preuves
        </h4>
        <button
          aria-label="Fermer le panneau des preuves"
          class="button button--secondary"
          type="button"
        >
          Fermer
        </button>
      </div>
      <blockquote>
        Une activité est observée le 12 octobre.
      </blockquote>
      <ul>
        <li>
          <p>
            <a
              href="https://sample.invalid/report"
              rel="noreferrer"
              target="_blank"
            >
              Sample report
            </a>
             · 
            fact
          </p>
          <code>
            sample-evidence-1
          </code>
        </li>
      </ul>
    </aside>
  `);
});

it("conserve le rendu du document V5 sous l'enveloppe V6", async () => {
  const directView = renderDocumentView(documentV5);
  const expectedMarkup = publicationLayout(directView.container).outerHTML;
  directView.unmount();

  const { container } = renderArtifactView(documentV6);
  await waitFor(() =>
    expect(publicationLayout(container).outerHTML).toBe(expectedMarkup),
  );
});

it("caractérise le rendu V7 d'une brève sans annexe IOC", async () => {
  const { container } = renderArtifactView(documentV7, "brief");
  await screen.findByText("Format : Brève");
  expect(publicationLayout(container)).toMatchInlineSnapshot(`
    <div
      class="publication-preview__layout"
    >
      <article
        class="publication-preview"
      >
        <p
          class="publication-preview__kind"
        >
          Format : 
          Brève
        </p>
        <h3>
          <strong
            class="semantic-role semantic-role--actor"
          >
            Dossier Sample Orchard
          </strong>
        </h3>
        <section
          aria-label="RÉFÉRENCES"
        >
          <h4>
            RÉFÉRENCES
          </h4>
          <div
            class="publication-preview__passage"
          >
            <p>
              <strong>
                12 octobre 2026
                 : 
              </strong>
              <span>
                Une activité est observée le 12 octobre.
              </span>
              <span
                class="publication-preview__provenance"
              >
                <span>
                   
                  <a
                    href="https://sample.invalid/report"
                    rel="noreferrer"
                    target="_blank"
                  >
                    Sample report
                  </a>
                </span>
              </span>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <div
            class="publication-preview__passage"
          >
            <p>
              <strong>
                12 octobre 2026
                 
                :
                 
              </strong>
              <span
                class="semantic-role semantic-role--source"
              >
                La source décrit une activité documentée.
              </span>
               
              <a
                href="https://sample.invalid/report"
                rel="noreferrer"
                target="_blank"
              >
                Sample report
              </a>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <section>
            <h4>
              Tableaux
            </h4>
            <article>
              <h5>
                <strong
                  class="semantic-role semantic-role--actor"
                >
                  Éléments observés
                </strong>
              </h5>
              <p>
                <span>
                  Résumé de la source
                </span>
              </p>
              <table>
                <thead>
                  <tr>
                    <th>
                      <span>
                        Élément
                      </span>
                    </th>
                    <th>
                      Preuves
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>
                      <span>
                        Échantillon
                      </span>
                    </td>
                    <td>
                      1
                       preuves
                    </td>
                  </tr>
                </tbody>
              </table>
            </article>
          </section>
        </section>
        <section
          aria-label="SYNTHÈSE"
        >
          <h4>
            SYNTHÈSE
          </h4>
          <div
            class="publication-preview__passage"
          >
            <p>
              <span>
                Le groupe 
              </span>
              <strong
                class="semantic-role semantic-role--actor"
              >
                Sample Orchard
              </strong>
              <span>
                 est documenté.
              </span>
              <span
                class="publication-preview__provenance"
              >
                <span>
                   
                  <a
                    href="https://sample.invalid/report"
                    rel="noreferrer"
                    target="_blank"
                  >
                    Sample report
                  </a>
                </span>
              </span>
            </p>
            <button
              class="publication-preview__lineage-trigger"
              type="button"
            >
              Voir les sources et preuves
            </button>
          </div>
          <section>
            <h4>
              Diagrammes
            </h4>
            <article>
              <h5>
                <span>
                  Chaîne résumée
                </span>
              </h5>
              <figure>
                <img
                  alt="Chaîne résumée"
                  loading="lazy"
                  src="/api/subjects/subject-sample-1/publication/assets/diagram-asset"
                />
                <figcaption>
                  <span>
                    Schéma de synthèse
                  </span>
                </figcaption>
              </figure>
            </article>
          </section>
          <div>
            <h5>
              Analyse
            </h5>
            <div
              class="publication-preview__passage"
            >
              <p>
                <code
                  class="semantic-role semantic-role--command"
                >
                  Une seconde observation complète le dossier.
                </code>
                <span
                  class="publication-preview__provenance"
                >
                  <span>
                     
                    <a
                      href="https://sample.invalid/report"
                      rel="noreferrer"
                      target="_blank"
                    >
                      Sample report
                    </a>
                  </span>
                </span>
              </p>
              <button
                class="publication-preview__lineage-trigger"
                type="button"
              >
                Voir les sources et preuves
              </button>
            </div>
            <section>
              <h4>
                Figures source
              </h4>
              <article>
                <figure>
                  <img
                    alt="Figure archivée de la source"
                    loading="lazy"
                    src="/api/subjects/subject-sample-1/publication/assets/figure-asset"
                  />
                  <figcaption>
                    <span>
                      Figure archivée de la source
                    </span>
                  </figcaption>
                </figure>
              </article>
            </section>
          </div>
        </section>
      </article>
    </div>
  `);
  expect(
    publicationLayout(container).querySelector(
      '[aria-label^="ANNEXE TECHNIQUE"]',
    ),
  ).toBeNull();
});
