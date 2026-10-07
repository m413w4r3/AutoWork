import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ExtractionProgress } from "../api/production";
import { ExtractionProgressView } from "./ExtractionProgress";

type ProgressSource = ExtractionProgress["sources"][number];

function progress(sources: ProgressSource[]): ExtractionProgress {
  return {
    total_sources: sources.length,
    completed_sources: sources.filter(
      (source) =>
        source.status === "cached" ||
        source.status === "reused" ||
        source.status === "succeeded",
    ).length,
    full_total: sources.filter((source) => source.profile === "full").length,
    full_completed: 0,
    ioc_rules_total: sources.filter((source) => source.profile === "ioc_rules")
      .length,
    ioc_rules_completed: 0,
    cache_hits: 0,
    model_calls: 1,
    skipped_sources: 0,
    confirmed_iocs: 0,
    contextual_iocs: 0,
    rules_total: 0,
    yara_rules: 0,
    sigma_rules: 0,
    suricata_rules: 0,
    snort_rules: 0,
    sources,
  };
}

function source(
  url: string,
  extra: Partial<ProgressSource> = {},
): ProgressSource {
  return {
    source_id: url,
    title: null,
    canonical_url: url,
    tier: "core",
    profile: "full",
    status: "pending",
    reuse_state: null,
    ioc_count: 0,
    rule_count: 0,
    ...extra,
  };
}

function sourceRow(text: string): HTMLElement {
  const list = screen.getByLabelText("Sources de l’extraction");
  const row = within(list)
    .getAllByRole("listitem")
    .find((item) => item.textContent?.includes(text));
  if (!row) throw new Error(`no row for ${text}`);
  return row;
}

describe("ExtractionProgressView", () => {
  it("montre le tier, le profil et le verdict canonique de chaque source", () => {
    render(
      <ExtractionProgressView
        progress={progress([
          source("https://core.example/a", {
            title: "Rapport CORE",
            status: "succeeded",
            reuse_state: "fresh",
          }),
          source("https://support.example/b", {
            tier: "supporting",
            profile: "ioc_rules",
            status: "reused",
            reuse_state: "duplicate_content",
            scope: {
              kind: "case",
              case_id: "GTG-30004",
              kept_sections: 1,
              total_sections: 38,
              kept_chars: 8361,
              total_chars: 258860,
            },
          }),
          source("https://tech.example/c", {
            tier: "technical",
            profile: null,
            status: "omitted",
          }),
        ])}
      />,
    );

    expect(sourceRow("Rapport CORE")).toHaveTextContent("CORE · FULL");
    expect(sourceRow("Rapport CORE")).toHaveTextContent("Terminé");
    expect(sourceRow("https://support.example/b")).toHaveTextContent(
      "Complémentaire · IOC uniquement",
    );
    expect(sourceRow("https://support.example/b")).toHaveTextContent(
      "contenu identique à une autre source",
    );
    expect(sourceRow("https://support.example/b")).toHaveTextContent(
      "Cas GTG-30004 · 1/38 sections · 8361/258860 caractères",
    );
    expect(sourceRow("https://support.example/b")).toHaveTextContent(
      "Réutilisé",
    );
    // A source the corpus left out keeps its line, without any profile.
    expect(sourceRow("https://tech.example/c")).toHaveTextContent(
      "Non éligible dans le corpus",
    );
    expect(sourceRow("https://tech.example/c")).not.toHaveTextContent("FULL");
  });

  it("résume le coût modèle et les résultats réutilisés", () => {
    render(
      <ExtractionProgressView
        progress={{
          ...progress([source("https://core.example/a", { status: "cached" })]),
          cache_hits: 1,
          model_calls: 0,
        }}
      />,
    );

    expect(
      screen.getByText("Résultats existants : 1 · Appels modèle : 0"),
    ).toBeInTheDocument();
  });
});
