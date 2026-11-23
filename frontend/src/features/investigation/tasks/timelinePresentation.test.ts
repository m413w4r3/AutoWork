import { describe, expect, it } from "vitest";

import type { TimelineItemV1 } from "../api/tasks";
import { timelineSummaryLabel } from "./timelinePresentation";

function item(overrides: Partial<TimelineItemV1>): TimelineItemV1 {
  return {
    item_type: "task_event",
    object_id: "11111111-1111-4111-8111-111111111111",
    occurred_at: "2026-11-22T10:00:00Z",
    task_id: null,
    actor_kind: "system",
    actor_id: null,
    summary: "",
    evidence: false,
    refs: {},
    linked_event_ids: [],
    linked_observation_ids: [],
    ...overrides,
  };
}

describe("timelineSummaryLabel", () => {
  it("traduit les événements de tâche et leur transition d’état", () => {
    expect(
      timelineSummaryLabel(item({ summary: "status_changed:ready->running" })),
    ).toBe("Statut modifié · Prête → En cours");
    expect(timelineSummaryLabel(item({ summary: "task_created:-->-" }))).toBe(
      "Tâche créée",
    );
  });

  it("traduit une décision humaine et sa cible", () => {
    expect(
      timelineSummaryLabel(
        item({ item_type: "decision", summary: "pivot_approve:pivot" }),
      ),
    ).toBe("Approbation d’un pivot · cible : pivot");
  });

  it("traduit une observation canonique et son type d’objet", () => {
    expect(
      timelineSummaryLabel(
        item({
          item_type: "observation",
          summary: "vt_file_report:file",
          evidence: true,
        }),
      ),
    ).toBe("Rapport VT fichier · objet : fichier");
    expect(
      timelineSummaryLabel(
        item({
          item_type: "observation",
          summary: "other:investigation_bootstrap_snapshot",
        }),
      ),
    ).toBe("Autre observation · objet : instantané de démarrage de l’enquête");
  });

  it("traduit les invocations d’outil et les consultations Bridge", () => {
    expect(
      timelineSummaryLabel(
        item({
          item_type: "tool_invocation",
          summary: "vt.download_sample:needs_review",
        }),
      ),
    ).toBe("Téléchargement d’un échantillon VT · Revue humaine requise");
    expect(
      timelineSummaryLabel(
        item({
          item_type: "bridge_consultation",
          summary: "advisor.consult:succeeded",
        }),
      ),
    ).toBe("Consultation Bridge · Réussi");
  });

  it("traduit les versions de plan, passations et contributions", () => {
    expect(
      timelineSummaryLabel(
        item({
          item_type: "plan_version",
          summary: "plan_version:2:partially_parsed",
        }),
      ),
    ).toBe("Version de plan 2 · partiellement analysé");
    expect(
      timelineSummaryLabel(
        item({ item_type: "handoff", summary: "handoff:v2:human->operator" }),
      ),
    ).toBe("Passation v2 · Humain → Opérateur");
    expect(
      timelineSummaryLabel(
        item({
          item_type: "contribution",
          summary: "contribution:finding:imported",
        }),
      ),
    ).toBe("Finding importé");
  });

  it("traduit les sessions d’agent et les instructions humaines", () => {
    expect(
      timelineSummaryLabel(
        item({
          item_type: "agent_session",
          summary: "agent_session:codex_cli:awaiting_instruction",
        }),
      ),
    ).toBe("Session Codex CLI · Attente d’instruction");
    expect(
      timelineSummaryLabel(
        item({ item_type: "instruction", summary: "instruction:queued" }),
      ),
    ).toBe("Instruction humaine · En file");
  });

  it("laisse le jeton brut intact quand il ne peut pas être traduit", () => {
    expect(
      timelineSummaryLabel(item({ summary: "future_event:whatever" })),
    ).toBe("future_event:whatever");
    expect(
      timelineSummaryLabel(
        item({ item_type: "tool_invocation", summary: "future.tool:unknown" }),
      ),
    ).toBe("future.tool:unknown");
  });
});
