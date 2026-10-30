import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { InvestigationActivityPage } from "../../api/investigationOperator";
import { InvestigationActivityCockpit } from "./InvestigationPanels";

const INVESTIGATION_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";

function makePage(
  overrides: Partial<InvestigationActivityPage> = {},
): InvestigationActivityPage {
  return {
    schema_version: "investigation-activity.v1",
    investigation: {
      id: INVESTIGATION_ID,
      subject_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
      production_run_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
      synthesis_artifact_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
      synthesis_sha256: "a".repeat(64),
      status: "running",
      version: 7,
      stop_reason: null,
      execution_mode: "operator_led",
    },
    operator: {
      principal: null,
      model: null,
      effort: null,
      active_subagent_count: null,
      metadata_source: "unavailable_in_autowork",
    },
    working_state: {
      version: 3,
      turn_index: 4,
      stop_reason: null,
      hypothesis_count: 2,
      open_hypothesis_count: 1,
    },
    last_activity_at: null,
    last_activity_source: null,
    active_backend_job_count: 1,
    budget: null,
    timeline: [],
    next_cursor: null,
    has_more: false,
    blockers: [],
    counts: { inconclusive_findings: 0 },
    generated_at: "2026-10-30T12:00:00Z",
    ...overrides,
  };
}

function renderCockpit(page: InvestigationActivityPage) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(Response.json(page)),
  );
  return render(
    <QueryClientProvider client={queryClient}>
      <InvestigationActivityCockpit
        investigationId={INVESTIGATION_ID}
        onOpenJournal={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("InvestigationActivityCockpit", () => {
  it(
    "shows unknown activity without inferring that the agent stopped",
    async () => {
      renderCockpit(makePage());

      expect(
        await screen.findByRole("heading", { name: "Investigation en cours" }),
      ).toBeTruthy();
      expect(screen.getByText("Inconnu — non mesurable")).toBeTruthy();
      expect(
        screen.getByText(/Cela ne permet pas de conclure que l’agent est arrêté/),
      ).toBeTruthy();
      expect(screen.getAllByText("Non déterminée").length).toBeGreaterThan(0);
      expect(screen.getByText("9. Note de l’analyste")).toBeTruthy();
    },
  );

  it(
    "reports persisted activity as partial, not as a completed stage",
    async () => {
      const page = makePage({
        timeline: [
          {
            id: "ffffffff-ffff-4fff-8fff-ffffffffffff",
            version: 2,
            tool_name: "sample.analyze",
            schema_version: "sample-analysis.v1",
            purpose_excerpt: "Analyser un échantillon",
            status: "succeeded",
            created_at: "2026-10-30T11:00:00Z",
            updated_at: "2026-10-30T11:01:00Z",
            duration_seconds: null,
            duration_quality: "unknown",
            reserved_budget: { tool_seconds: 10 },
            consumed_budget: { tool_seconds: 4 },
            repeated_input_count_in_page: 1,
            observation_refs: ["11111111-1111-4111-8111-111111111111"],
            observations: [
              {
                id: "11111111-1111-4111-8111-111111111111",
                kind: "binary_analysis",
                sha256: "b".repeat(64),
                source: "local-analysis",
                source_version: "2",
                observed_at: "2026-10-30T11:01:00Z",
              },
            ],
          },
        ],
      });
      renderCockpit(page);

      expect(await screen.findByText("2. Analyse statique")).toBeTruthy();
      expect(
        screen.getAllByText("Partielle · clôture inconnue").length,
      ).toBeGreaterThan(0);
      expect(screen.queryByText("Terminée")).toBeNull();
      expect(
        screen.getByText(/Aucune mesure fiable n’est persistée/),
      ).toBeTruthy();
    },
  );

  it("presents an inconclusive finding as insufficient evidence, not a bug", async () => {
    const page = makePage({
      blockers: [
        {
          kind: "evidence_insufficient",
          code: "inconclusive_findings",
          invocation_id: null,
          external_effect_state: null,
          message: "1 constat reste sans preuve suffisante; ce n’est pas un bug.",
          next_action: "Clarifier l’hypothèse ou rechercher une preuve admissible supplémentaire.",
        },
      ],
    });
    renderCockpit(page);

    expect(await screen.findByText("Preuve insuffisante")).toBeTruthy();
    expect(screen.getByText(/n’est pas un bug/)).toBeTruthy();
  });
});
