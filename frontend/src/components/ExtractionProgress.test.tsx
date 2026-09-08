import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ExtractionProgress } from "../api/production";
import { ExtractionProgressView } from "./ExtractionProgress";

function progress(
  sources: ExtractionProgress["sources"],
  overrides: Partial<ExtractionProgress> = {},
): ExtractionProgress {
  return {
    total_sources: sources.length,
    completed_sources: 0,
    full_total: sources.filter((source) => source.profile === "full").length,
    full_completed: 0,
    ioc_rules_total: sources.filter((source) => source.profile === "ioc_rules")
      .length,
    ioc_rules_completed: 0,
    cache_hits: 0,
    model_calls: 0,
    confirmed_iocs: 0,
    contextual_iocs: 0,
    rules_total: 0,
    yara_rules: 0,
    sigma_rules: 0,
    suricata_rules: 0,
    snort_rules: 0,
    active_source_id: null,
    active_source_title: null,
    active_profile: null,
    sources,
    ...overrides,
  };
}

function source(
  source_id: string,
  extra: Partial<ExtractionProgress["sources"][number]> = {},
): ExtractionProgress["sources"][number] {
  return {
    source_id,
    title: `Titre ${source_id}`,
    profile: "ioc_rules",
    status: "pending",
    ioc_count: 0,
    rule_count: 0,
    ...extra,
  };
}

function sourceRow(source_id: string): HTMLElement {
  const list = screen.getByLabelText("Sources de l’extraction");
  const row = within(list)
    .getAllByRole("listitem")
    .find((item) => item.textContent?.includes(source_id));
  if (!row) throw new Error(`no row for ${source_id}`);
  return row;
}

describe("ExtractionProgressView", () => {
  it("explique pourquoi chaque source est lue ou réutilisée", () => {
    render(
      <ExtractionProgressView
        progress={progress(
          [
            source("S1", {
              status: "pending",
              plan_disposition: "extract_individual",
              plan_reason: "no_checkpoint",
            }),
            source("S2", {
              status: "cached",
              plan_disposition: "reused",
              plan_reason: "reusable_checkpoint",
            }),
            source("S3", {
              status: "pending",
              plan_disposition: "extract_batched",
              plan_reason: "source_content_changed",
            }),
          ],
          { planned_model_calls: 2, planned_reuses: 1 },
        )}
      />,
    );

    expect(sourceRow("S1")).toHaveTextContent("aucun résultat réutilisable");
    expect(sourceRow("S2")).toHaveTextContent("résultat existant réutilisé");
    expect(sourceRow("S3")).toHaveTextContent("contenu réarchivé différent");
    // Le coût prévu est lisible avant que le moindre appel soit émis.
    expect(
      screen.getByText(/Plan : 2 appels prévus · 1 source réutilisée/),
    ).toBeInTheDocument();
  });

  it("nomme la source primaire d’un doublon de contenu", () => {
    render(
      <ExtractionProgressView
        progress={progress([
          source("S4", {
            status: "cached",
            plan_disposition: "content_duplicate",
            plan_reason: "same_content_as_primary_source",
            plan_primary_source_id: "S1",
          }),
        ])}
      />,
    );

    expect(sourceRow("S4")).toHaveTextContent(
      "contenu identique à une autre source (S1)",
    );
  });

  it("reste inchangé quand le backend n’a pas encore publié de plan", () => {
    render(
      <ExtractionProgressView
        progress={progress([source("S1", { status: "pending" })])}
      />,
    );

    expect(sourceRow("S1")).toHaveTextContent("En attente");
    expect(screen.queryByText(/Plan :/)).not.toBeInTheDocument();
  });
});
