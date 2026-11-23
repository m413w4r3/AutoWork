/**
 * French labels and formatters shared by the plan and task views.
 *
 * Every map is a closed `Record` on the API union, so a new backend value
 * fails the type check instead of rendering a raw snake_case token.
 */

import type {
  AgentSessionState,
  BlockedReason,
  HandoffFreshnessState,
  InstructionState,
  OperatorAdapterKind,
  ResultVerificationStatus,
  TaskActorKind,
  TaskEventType,
  TaskPriority,
  TaskStatus,
} from "../api/types";
import type { TimelineActorKind, TimelineItemType } from "../api/tasks";

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  proposed: "Proposée",
  ready: "Prête",
  assigned: "Attribuée",
  running: "En cours",
  awaiting_input: "Attente d’instruction",
  awaiting_review: "En revue",
  blocked: "Bloquée",
  completed: "Terminée",
  cancelled: "Annulée",
  failed: "Échouée",
};

export const TASK_PRIORITY_LABELS: Record<TaskPriority, string> = {
  critical: "Critique",
  high: "Haute",
  normal: "Normale",
  low: "Basse",
};

export const VERIFICATION_LABELS: Record<ResultVerificationStatus, string> = {
  unverified: "Non vérifié",
  pending_review: "En attente de revue",
  accepted: "Accepté",
  rejected: "Rejeté",
};

export const BLOCKED_REASON_LABELS: Record<BlockedReason, string> = {
  dependency: "Dépendance non satisfaite",
  capability: "Capacité indisponible",
  approval: "Approbation requise",
  session_lost: "Session perdue",
  lease_expired: "Bail expiré",
  input_unavailable: "Entrée indisponible",
  external: "Cause externe",
};

export const TASK_ACTOR_LABELS: Record<TaskActorKind, string> = {
  human: "Humain",
  operator: "Opérateur",
  system: "Système",
};

export const SESSION_STATE_LABELS: Record<AgentSessionState, string> = {
  provisioning: "Provisionnement",
  ready: "Prête",
  running: "En cours",
  awaiting_instruction: "Attente d’instruction",
  interrupt_requested: "Interruption demandée",
  stopped: "Arrêtée",
  lost: "Perdue",
  reconciling: "Réconciliation",
  closed: "Fermée",
  failed: "Échouée",
};

/** Full instruction vocabulary of the session summary (B6). */
export const INSTRUCTION_JOURNAL_STATE_LABELS: Record<
  InstructionState,
  string
> = {
  queued: "En file (agent occupé)",
  delivering: "Livraison en cours",
  delivered: "Transmise",
  acknowledged: "Acquittée",
  delivery_unknown: "Livraison inconnue",
  cancelled: "Annulée",
};

export const HANDOFF_FRESHNESS_LABELS: Record<HandoffFreshnessState, string> = {
  fresh: "À jour",
  stale: "Périmée",
};

export const ADAPTER_LABELS: Record<OperatorAdapterKind, string> = {
  codex_cli: "Codex CLI",
  claude_code: "Claude Code",
};

/** Closed target union of an analyst decision, translated for the UI. */
export const DECISION_TARGET_LABELS: Record<string, string> = {
  member: "membre",
  feature: "fonctionnalité",
  tool: "outil",
  invariant: "invariant",
  pivot: "pivot",
  corpus: "corpus",
  detection: "détection",
  note: "note",
  plan_version: "version de plan",
  task: "tâche",
  workspace_conflict: "conflit de workspace",
};

const ACTOR_ID_PREFIXES: ReadonlySet<string> = new Set([
  "human",
  "operator",
  "system",
  "source",
]);

/**
 * Actor ids are persisted as `kind:value` (`operator:codex_cli`,
 * `human:alice`). The UI already shows the actor kind, so strip the redundant
 * prefix and translate a known adapter value; other principals stay readable.
 */
export function actorRefLabel(actorId: string): string {
  const separator = actorId.indexOf(":");
  if (separator === -1) return actorId;
  const prefix = actorId.slice(0, separator);
  const value = actorId.slice(separator + 1);
  if (!ACTOR_ID_PREFIXES.has(prefix) || value === "") return actorId;
  if (prefix === "operator" && value in ADAPTER_LABELS) {
    return ADAPTER_LABELS[value as OperatorAdapterKind];
  }
  return value;
}

export const TASK_EVENT_LABELS: Record<TaskEventType, string> = {
  task_created: "Tâche créée",
  status_changed: "Statut modifié",
  assigned: "Attribution",
  lease_acquired: "Bail acquis",
  lease_renewed: "Bail renouvelé",
  lease_released: "Bail libéré",
  lease_expired: "Bail expiré",
  result_submitted: "Résultat soumis",
  verification_recorded: "Vérification enregistrée",
  session_started: "Session démarrée",
  session_state_changed: "État de session modifié",
  handoff_created: "Passation créée",
  contribution_imported: "Contribution importée",
};

export const TIMELINE_TYPE_LABELS: Record<TimelineItemType, string> = {
  task_event: "Événement de tâche",
  decision: "Décision humaine",
  observation: "Observation",
  bridge_consultation: "Consultation Bridge",
  tool_invocation: "Invocation d’outil",
  plan_version: "Version de plan",
  handoff: "Passation",
  contribution: "Contribution",
  agent_session: "Session d’agent",
  instruction: "Instruction humaine",
};

export const TIMELINE_ACTOR_LABELS: Record<TimelineActorKind, string> = {
  human: "Humain",
  operator: "Opérateur",
  system: "Système",
  source: "Source",
};

export function formatTimestamp(value: string | null): string {
  if (value === null || value === "") return "Non renseigné";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

/** Human duration between two ISO instants; `null` end means "still running". */
export function formatDuration(start: string, end: string | null): string {
  const from = new Date(start).getTime();
  const to = end === null ? Date.now() : new Date(end).getTime();
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) {
    return "Non renseignée";
  }
  const minutes = Math.floor((to - from) / 60_000);
  if (minutes < 1) return "moins d’une minute";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

export function shortSha(value: string | null): string {
  if (value === null || value === "") return "Aucun";
  return value.length > 16 ? `${value.slice(0, 12)}…` : value;
}

/** Budget amounts are non-negative integers; `null` means "unbounded". */
export function formatBudgetAmount(value: number | null): string {
  if (value === null) return "illimité";
  return new Intl.NumberFormat("fr-FR").format(value);
}

export function orPlaceholder(value: string): string {
  return value.trim() === "" ? "Non renseigné" : value;
}

export function listOrPlaceholder(values: readonly string[]): string {
  return values.length === 0 ? "Non renseigné" : values.join(", ");
}
