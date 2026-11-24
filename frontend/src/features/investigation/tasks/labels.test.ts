import { describe, expect, it } from "vitest";

import type { AgentSessionState, InstructionState } from "../api/types";
import {
  actorRefLabel,
  activityEventLabel,
  INSTRUCTION_JOURNAL_STATE_LABELS,
  INSTRUCTION_SUBMISSION_PENDING_LABEL,
  SESSION_STATE_LABELS,
} from "./labels";

const instructionStates: readonly [InstructionState, string][] = [
  ["queued", "En file"],
  ["delivering", "Envoi en cours"],
  ["delivered", "Transmise"],
  ["acknowledged", "Acquittée"],
  ["delivery_unknown", "Livraison incertaine"],
  ["cancelled", "Annulée"],
];

const sessionStates: readonly [AgentSessionState, string][] = [
  ["provisioning", "Provisionnement"],
  ["ready", "Prête"],
  ["running", "En cours"],
  ["awaiting_instruction", "Attente d’instruction"],
  ["interrupt_requested", "Interruption demandée"],
  ["stopped", "Session interrompue"],
  ["lost", "Session perdue"],
  ["reconciling", "Réconciliation"],
  ["closed", "Fermée"],
  ["failed", "Échouée"],
];

describe("actorRefLabel", () => {
  it("traduit un adaptateur opérateur et retire le préfixe de type", () => {
    expect(actorRefLabel("operator:codex_cli")).toBe("Codex CLI");
    expect(actorRefLabel("operator:claude_code")).toBe("Claude Code");
    expect(actorRefLabel("operator:static-token")).toBe("static-token");
    expect(actorRefLabel("human:alice")).toBe("alice");
  });

  it("laisse intact un identifiant qui n’est pas un acteur typé", () => {
    expect(actorRefLabel("analyst:test")).toBe("analyst:test");
    expect(actorRefLabel("virustotal")).toBe("virustotal");
    expect(actorRefLabel("operator:")).toBe("operator:");
  });
});

describe("activityEventLabel", () => {
  it("traduit les événements d’enquête et de tâche connus", () => {
    expect(activityEventLabel("investigation_record")).toBe(
      "Mise à jour du dossier",
    );
    expect(activityEventLabel("task_created")).toBe("Tâche créée");
  });

  it("remplace un identifiant d’événement inconnu par un libellé générique", () => {
    expect(activityEventLabel("future_investigation_event")).toBe(
      "Activité enregistrée",
    );
    expect(activityEventLabel("heartbeat")).toBe("Activité enregistrée");
    expect(activityEventLabel("")).toBe("Activité enregistrée");
    expect(activityEventLabel("Collecte VT terminée")).toBe(
      "Collecte VT terminée",
    );
  });
});

describe("agent state labels", () => {
  it.each(instructionStates)("maps instruction state %s", (state, label) => {
    expect(INSTRUCTION_JOURNAL_STATE_LABELS[state]).toBe(label);
  });

  it.each(sessionStates)("maps session state %s", (state, label) => {
    expect(SESSION_STATE_LABELS[state]).toBe(label);
  });

  it("labels the in-flight enqueue request as a local state", () => {
    expect(INSTRUCTION_SUBMISSION_PENDING_LABEL).toBe("En attente d’envoi");
  });
});
