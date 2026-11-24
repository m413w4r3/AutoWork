import { describe, expect, it } from "vitest";

import type { TaskView } from "../api/tasks";
import {
  BOOTSTRAP_LIMIT_SOURCE_LABELS,
  EXTERNAL_MODEL_POLICY_SOURCE_LABELS,
  HASH_TYPE_LABELS,
  bootstrapProviderLabel,
  deriveOverviewDecisions,
  verificationLabel,
} from "./overviewPresentation";

const task = {
  id: "task-1",
  title: "Vérifier l’infrastructure",
  status: "ready",
  result_verification_status: "unverified",
  result_refs: [] as TaskView["result_refs"],
} as const;

describe("labels de source bootstrap", () => {
  it("traduit les sources et fournisseurs techniques en libellés français", () => {
    expect(BOOTSTRAP_LIMIT_SOURCE_LABELS.settings_cap).toBe(
      "Plafond de configuration",
    );
    expect(EXTERNAL_MODEL_POLICY_SOURCE_LABELS.article_policy).toBe(
      "Politique de l’article",
    );
    expect(bootstrapProviderLabel("vt")).toBe("VirusTotal");
    expect(bootstrapProviderLabel("future_provider")).toBe("Autre fournisseur");
    expect(HASH_TYPE_LABELS.sha256).toBe("SHA-256");
  });
});

describe("deriveOverviewDecisions", () => {
  it("ne dérive aucune décision quand les projections sont vides", () => {
    expect(
      deriveOverviewDecisions({
        tasks: [],
        proposals: [],
        conflicts: [],
      }),
    ).toEqual([]);
  });

  it("lie une tâche en attente au plan et à son identifiant", () => {
    const decisions = deriveOverviewDecisions({
      tasks: [{ ...task, status: "awaiting_review" }],
      workspaceViewsAvailable: true,
    });

    expect(decisions).toEqual([
      {
        key: "task:task-1",
        label: "Examiner une tâche",
        detail: "Vérifier l’infrastructure",
        href: "?vue=plan&task=task-1",
      },
    ]);
  });

  it("oriente une proposition Bridge vers la revue de note", () => {
    const decisions = deriveOverviewDecisions({
      proposals: [
        {
          id: "proposal-1",
          task_id: "task-1",
          anchor: "Résumé",
          status: "proposed",
          proposed_text: "Résumé proposé par Bridge",
        },
      ],
    });

    expect(decisions[0]).toMatchObject({
      label: "Examiner une proposition Bridge",
      href: "?vue=opinion",
    });
  });

  it("oriente un conflit workspace vers sa résolution", () => {
    const decisions = deriveOverviewDecisions({
      conflicts: [
        {
          conflict_id: "conflict-1",
          path: "notes/hypothese.md",
          task_id: "task-1",
        },
      ],
    });

    expect(decisions[0]).toMatchObject({
      label: "Résoudre un conflit de note ou fichier",
      href: "?vue=workspace",
    });
  });

  it("propose le G2 initial seulement si la proposition est approuvable", () => {
    const decisions = deriveOverviewDecisions({
      workspace: { workspace_state: "bound", workspace_available: true },
      envelopeProposal: {
        already_approved: false,
        plan_state: "ready",
        covered_operations: 2,
      },
      envelope: { approved: false, currently_authorized: false },
    });

    expect(decisions[0]).toMatchObject({
      label: "Examiner et approuver l’enveloppe G2",
      href: "#overview-g2-details",
    });
    expect(
      deriveOverviewDecisions({
        workspace: { workspace_state: "bound", workspace_available: true },
        envelopeProposal: {
          already_approved: false,
          plan_state: "awaiting_g2",
          covered_operations: 2,
        },
        envelope: { approved: true, currently_authorized: false },
      }),
    ).toEqual([]);
  });
});

describe("verificationLabel", () => {
  it("n’appelle jamais une contribution non reliée à une tâche une preuve vérifiée", () => {
    expect(verificationLabel(undefined)).toBe("Non vérifié");
  });
});
