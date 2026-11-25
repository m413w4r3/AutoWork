import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  ProductionRoutingProjection,
  ProductionRoutingSource,
} from "../../api/production";
import { ProductionRoutingDetails } from "./ProductionRoutingDetails";

function routing(
  overrides: Partial<ProductionRoutingProjection> = {},
): ProductionRoutingProjection {
  return {
    status: "decided",
    publication_kind: "article",
    reason_code: "subject_ioc_confirmed",
    policy_version: "routing-policy-v1",
    confirmed_ioc_count: 2,
    contextual_ioc_count: 0,
    ambiguous_ioc_count: 0,
    extraction_verification_state: "verified",
    sources: [],
    ...overrides,
  };
}

function routingSource(
  overrides: Partial<ProductionRoutingSource> = {},
): ProductionRoutingSource {
  return {
    source_id: "source-1",
    source_url: "https://source.example/report",
    failure_code: "critical_source_inaccessible",
    tier: "core",
    criticality: "critical",
    criticality_reason: "required_source_missing",
    policy_version: "source-policy-v1",
    editorial_role: "primary",
    acquisition_method: "http",
    collection_state: "blocked",
    error_code: "http_403",
    remaining_control_code: "human_review_required",
    human_waiver_path: "not_consumed_by_routing",
    ...overrides,
  };
}

describe("ProductionRoutingDetails", () => {
  it("indicates that the format is undetermined when no routing decision exists", () => {
    render(<ProductionRoutingDetails />);

    expect(screen.getByText("Format : à déterminer")).toBeInTheDocument();
  });

  it.each([
    ["article", "Article"],
    ["brief", "Brève"],
  ] as const)("shows a decided %s format", (publicationKind, label) => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          publication_kind: publicationKind,
          sources: [
            {
              source_id: "source-1",
              source_url: "https://source.example/report",
              failure_code: "critical_source_incomplete",
              tier: "core",
              criticality: "critical",
              criticality_reason: "required_source_missing",
              policy_version: "source-policy-v1",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText(`Format : ${label}`)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Détail diagnostic"));
    expect(screen.getByText("subject_ioc_confirmed")).toBeInTheDocument();
    expect(screen.getByText("routing-policy-v1")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "source.example/report" }),
    ).toHaveAttribute("href", "https://source.example/report");
  });

  it("marks a route that needs review without predicting a publication kind", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "incomplete_extraction",
          confirmed_ioc_count: 0,
        })}
      />,
    );

    expect(
      screen.getByText("Format : Qualification à revoir"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByText("Détail diagnostic"));
    expect(screen.getByText("incomplete_extraction")).toBeInTheDocument();
    expect(screen.getByText("0")).toBeInTheDocument();
  });

  it("explique une source critique inaccessible sans exposer sa query", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "critical_source_inaccessible",
          sources: [
            routingSource({
              source_url: "https://source.example/report?token=secret",
            }),
          ],
        })}
      />,
    );

    const review = within(screen.getByRole("status"));
    expect(review.getByText(/source\.example\/report/)).toBeInTheDocument();
    expect(
      review.getByText(/CORE.*PRIMARY|PRIMARY.*CORE/i),
    ).toBeInTheDocument();
    expect(
      review.getByText(/bloquée par le site distant \(403\)/i),
    ).toBeInTheDocument();
    expect(
      review.getByText(/réacquérir ou remplacer la source/i),
    ).toBeInTheDocument();
    expect(
      review.getByText(/routage.*extraction.*étapes suivantes/i),
    ).toBeInTheDocument();
    expect(
      review.getByText(/sources archivées.*empreintes.*historique/i),
    ).toBeInTheDocument();
    expect(review.queryByText(/token=secret/)).toBeNull();
    expect(review.queryByRole("button")).toBeNull();
    expect(review.getByRole("link")).toHaveAttribute(
      "href",
      "https://source.example/report",
    );

    fireEvent.click(screen.getByText("Détail diagnostic"));
    expect(
      screen.getAllByText("critical_source_inaccessible").length,
    ).toBeGreaterThan(0);
    expect(screen.getByText("http_403")).toBeInTheDocument();
  });

  it("explique une source critique archivée mais illisible", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "critical_source_unreadable",
          sources: [
            routingSource({
              failure_code: "critical_source_unreadable",
              collection_state: "archived",
              error_code: "archive_unreadable",
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText(/archivée mais illisible/i)).toBeInTheDocument();
    expect(
      screen.getByText(/réacquérir ou remplacer la source/i),
    ).toBeInTheDocument();
  });

  it("demande de vérifier la preuve d’une provenance alternative", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "critical_source_alternative_provenance",
          sources: [
            routingSource({
              failure_code: "alternative_acquisition_provenance",
              acquisition_method: "manual_upload",
              collection_state: "archived",
            }),
          ],
        })}
      />,
    );

    expect(
      screen.getByText(/provenance alternative à vérifier/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/examiner le justificatif.*preuve d’acquisition/i),
    ).toBeInTheDocument();
  });

  it("liste les sources CORE/PRIMARY sans date vérifiée pour la chronologie", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "brief_evidence_chronology_missing",
          sources: [
            routingSource({
              source_url: "https://one.example/report",
              failure_code: "brief_chronology_source_undated",
            }),
            routingSource({
              source_id: "source-2",
              source_url: "https://two.example/report",
              failure_code: "brief_chronology_source_undated",
            }),
            routingSource({
              source_id: "unrelated-source",
              source_url: "https://unrelated.example/report",
              failure_code: "source_event_not_relevant",
              tier: "secondary",
              criticality: "non_critical",
              editorial_role: "contextual",
              remaining_control_code: "no_routing_block",
            }),
          ],
        })}
      />,
    );

    const review = within(screen.getByRole("status"));
    expect(
      review.getByText(/aucune date vérifiée n’est disponible/i),
    ).toBeInTheDocument();
    expect(review.getByText(/one\.example\/report/)).toBeInTheDocument();
    expect(review.getByText(/two\.example\/report/)).toBeInTheDocument();
    expect(review.queryByText(/unrelated\.example\/report/)).toBeNull();
    expect(
      screen.getByText(/acquisition datée est nécessaire/i),
    ).toBeInTheDocument();
    expect(review.queryByText(/brief_chronology_source_undated/)).toBeNull();
    fireEvent.click(screen.getByText("Détail diagnostic"));
    expect(screen.getAllByText("brief_chronology_source_undated")).toHaveLength(
      2,
    );
  });

  it("n’invente pas de source bloquante lorsque les événements ne donnent pas de chronologie admissible", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "brief_evidence_chronology_missing",
          sources: [
            routingSource({
              source_id: "dated-but-ineligible",
              source_url: "https://dated-but-ineligible.example/report",
              failure_code: "source_event_not_relevant",
            }),
          ],
        })}
      />,
    );

    const review = within(screen.getByRole("status"));
    expect(
      review.getByText(/événements extraits.*chronologie datée admissible/i),
    ).toBeInTheDocument();
    expect(
      review.getByText(
        /date vérifiée peut exister.*événement.*pas admissible pour la brève/i,
      ),
    ).toBeInTheDocument();
    expect(
      review.getByText(
        /acquisition de source datée.*date de publication admissible/i,
      ),
    ).toBeInTheDocument();
    expect(
      review.queryByText(/dated-but-ineligible\.example\/report/),
    ).toBeNull();
    expect(
      review.queryByText(/aucune date vérifiée n’est disponible/i),
    ).toBeNull();
  });

  it("conserve le diagnostic des sources pour les autres motifs de routage", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "critical_source_inaccessible",
          sources: [
            routingSource({
              source_url: "https://critical.example/report",
            }),
          ],
        })}
      />,
    );

    const review = within(screen.getByRole("status"));
    expect(review.getByText(/critical\.example\/report/)).toBeInTheDocument();
    expect(
      review.getByText(/bloquée par le site distant \(403\)/i),
    ).toBeInTheDocument();
  });

  it("garde un libellé lisible pour une ancienne source critique manquante", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "critical_source_missing",
        })}
      />,
    );

    expect(
      screen.getByText(
        /source critique manquante ou non lisible \(décision antérieure\)/i,
      ),
    ).toBeInTheDocument();
  });

  it("présente les pertes non critiques comme des avertissements", () => {
    render(
      <ProductionRoutingDetails
        routing={routing({
          status: "needs_review",
          publication_kind: null,
          reason_code: "critical_source_inaccessible",
          sources: [
            routingSource(),
            routingSource({
              source_id: "secondary-1",
              source_url: "https://secondary.example/report",
              failure_code: "secondary_source_unreadable",
              tier: "secondary",
              criticality: "non_critical",
              editorial_role: "contextual",
              remaining_control_code: "no_routing_block",
              collection_state: "archived",
              error_code: "archive_unreadable",
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText(/avertissement/i)).toBeInTheDocument();
    expect(
      within(screen.getByRole("status")).getByText(
        /secondary\.example\/report/,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/continuer sans source/i)).toBeNull();
  });
});
