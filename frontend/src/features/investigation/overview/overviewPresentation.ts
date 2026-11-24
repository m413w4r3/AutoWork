/** Libellés français et formatage borné de la Vue d’ensemble M24. */

import type {
  BootstrapBudgetLimitV1,
  BootstrapEnvelopeProposalV1,
  BootstrapExternalModelProposalV1,
  BootstrapCoverageKind,
  BootstrapPlanState,
  BootstrapReason,
  BootstrapRouteStatus,
  BootstrapSnapshotIocV1,
} from "../api/bootstrap";
import type { AgentSessionView } from "../api/agentSessions";
import type { NoteProposalViewV1 } from "../api/opinionProposals";
import type { TaskView } from "../api/tasks";
import type { BudgetDimension } from "../api/types";
import type {
  WorkspaceAvailabilityCode,
  WorkspaceConflictViewV1,
  WorkspaceStatusV1,
} from "../api/workspace";
import type { WorkspaceAgentStatus } from "../api/workspaceAgent";
import type { EnvelopeAccessKind } from "./envelopeAccess";

export const PLAN_STATE_LABELS: Record<BootstrapPlanState, string> = {
  empty: "Inventaire vide",
  awaiting_g2: "En attente de la décision G2",
  ready: "Prêt",
  blocked: "Bloqué",
  unavailable: "Indisponible",
};

export const REASON_LABELS: Record<BootstrapReason, string> = {
  pending: "En attente",
  available: "Disponible",
  downloaded: "Téléchargé",
  reused: "Réutilisé",
  empty: "Vide",
  not_found: "Introuvable",
  provider_denied: "Refusé par le fournisseur",
  route_unavailable: "Route indisponible",
  quota: "Quota atteint",
  failed: "Échec",
  partial: "Partiel",
  needs_review: "À réviser",
  policy_denied: "Refusé par la politique",
  capability_absent: "Capacité absente",
  provider_not_allowed: "Fournisseur non autorisé",
  acquisition_not_allowed: "Acquisition non autorisée",
  disclosure_bounds: "Limites de divulgation",
  budget_exceeded: "Budget dépassé",
  awaiting_g2: "En attente G2",
  evidence_unavailable: "Preuve indisponible",
  not_attempted: "Non tenté",
  stopped: "Arrêté",
  invocation_failed: "Invocation échouée",
};

export const ROUTE_STATUS_LABELS: Record<BootstrapRouteStatus, string> = {
  configured: "Configurée",
  unverified: "Non vérifiée",
  unavailable: "Indisponible",
};

export const COVERAGE_KIND_LABELS: Record<BootstrapCoverageKind, string> = {
  capability_absent: "Capacité absente",
  capability_shared: "Capacité partagée",
  route_unavailable: "Route indisponible",
  route_unverified: "Route non vérifiée",
  deferred: "Reporté",
  limit: "Limite",
};

export const BUDGET_DIMENSION_LABELS: Record<BudgetDimension, string> = {
  bridge_turns: "Tours Bridge",
  vt_units: "Unités VirusTotal",
  shodan_credits: "Crédits Shodan",
  downloads: "Téléchargements",
  tool_seconds: "Secondes d’outil",
  context_tokens: "Tokens de contexte",
  context_bytes: "Octets de contexte",
};

export const BOOTSTRAP_LIMIT_SOURCE_LABELS: Record<
  BootstrapBudgetLimitV1["source"],
  string
> = {
  inventory: "Inventaire",
  settings_cap: "Plafond de configuration",
  settings_default: "Valeur par défaut de la configuration",
};

export const EXTERNAL_MODEL_POLICY_SOURCE_LABELS: Record<
  BootstrapExternalModelProposalV1["policy_source"],
  string
> = {
  article_policy: "Politique de l’article",
};

export const BOOTSTRAP_PROVIDER_LABELS: Readonly<Record<string, string>> = {
  vt: "VirusTotal",
  bridge: "Bridge",
  shodan: "Shodan",
};

export function bootstrapProviderLabel(provider: string): string {
  return BOOTSTRAP_PROVIDER_LABELS[provider] ?? "Autre fournisseur";
}

export const AGENT_STATUS_LABELS: Record<WorkspaceAgentStatus, string> = {
  available: "Disponible",
  unavailable: "Indisponible",
  unconfigured: "Non configuré",
};

export const WORKSPACE_AVAILABILITY_LABELS: Record<
  WorkspaceAvailabilityCode,
  string
> = {
  available: "Disponible",
  unconfigured: "Non configuré",
  unavailable: "Indisponible",
  identity_conflict: "Conflit d’identité",
  not_bound: "Non lié",
};

export const IOC_KIND_LABELS: Record<BootstrapSnapshotIocV1["kind"], string> = {
  sample: "Échantillon",
  ipv4: "Adresse IPv4",
  domain: "Domaine",
  url: "URL",
};

export const HASH_TYPE_LABELS: Record<
  NonNullable<BootstrapSnapshotIocV1["hash_type"]>,
  string
> = {
  md5: "MD5",
  sha1: "SHA-1",
  sha256: "SHA-256",
};

export interface OverviewDecision {
  key: string;
  label: string;
  detail: string;
  href: string | null;
}

export interface OverviewDecisionInput {
  tasks?: readonly Pick<
    TaskView,
    "id" | "title" | "status" | "result_verification_status" | "result_refs"
  >[];
  proposals?: readonly Pick<
    NoteProposalViewV1,
    "id" | "task_id" | "anchor" | "status" | "proposed_text"
  >[];
  conflicts?: readonly Pick<
    WorkspaceConflictViewV1,
    "conflict_id" | "path" | "task_id"
  >[];
  workspace?: Pick<
    WorkspaceStatusV1,
    "workspace_state" | "workspace_available"
  > | null;
  envelopeProposal?: Pick<
    BootstrapEnvelopeProposalV1,
    "already_approved" | "plan_state" | "covered_operations"
  > | null;
  envelopeAccessKind?: EnvelopeAccessKind | null;
  workspaceViewsAvailable?: boolean;
}

export function bootstrapEnvelopeBlockedReason(
  proposal: Pick<
    BootstrapEnvelopeProposalV1,
    "already_approved" | "plan_state" | "covered_operations"
  > | null,
  workspaceReady: boolean,
  workspaceBlockedReason: string | null,
): string | null {
  if (!proposal || proposal.already_approved) return null;
  if (!workspaceReady)
    return (
      workspaceBlockedReason ??
      "Le workspace local doit être disponible avant de décider l’enveloppe G2."
    );
  if (
    proposal.plan_state === "blocked" ||
    proposal.plan_state === "unavailable"
  )
    return "Le plan de bootstrap est bloqué ou indisponible : l’enveloppe ne peut pas être approuvée dans cet état.";
  if (proposal.covered_operations === 0)
    return "Aucune opération n’est couverte par cette proposition : vérifiez l’inventaire et les capacités locales.";
  return null;
}

/**
 * Decision candidates come only from loaded canonical projections. Unknown
 * projections produce no action; each href points at a view that owns the
 * corresponding human decision.
 */
export function deriveOverviewDecisions(
  input: OverviewDecisionInput,
): OverviewDecision[] {
  const decisions: OverviewDecision[] = [];
  const viewsAvailable = input.workspaceViewsAvailable ?? true;

  for (const task of input.tasks ?? []) {
    const needsReview = task.status === "awaiting_review";
    const needsInput = task.status === "awaiting_input";
    const needsResultCheck =
      task.result_refs.length > 0 &&
      (task.result_verification_status === "unverified" ||
        task.result_verification_status === "pending_review");
    if (!needsReview && !needsInput && !needsResultCheck) continue;
    decisions.push({
      key: `task:${task.id}`,
      label: needsInput
        ? "Répondre à une tâche"
        : needsReview
          ? "Examiner une tâche"
          : "Vérifier un résultat",
      detail: task.title,
      href: viewsAvailable
        ? `?vue=plan&task=${encodeURIComponent(task.id)}`
        : null,
    });
  }

  for (const proposal of input.proposals ?? []) {
    if (proposal.status !== "proposed" || proposal.proposed_text === null)
      continue;
    decisions.push({
      key: `proposal:${proposal.id}`,
      label: "Examiner une proposition Bridge",
      detail: proposal.anchor,
      href: viewsAvailable ? "?vue=opinion" : null,
    });
  }

  for (const conflict of input.conflicts ?? []) {
    decisions.push({
      key: `conflict:${conflict.conflict_id}`,
      label: "Résoudre un conflit de note ou fichier",
      detail: conflict.path,
      href: viewsAvailable ? "?vue=workspace" : null,
    });
  }

  const workspaceCanApprove =
    input.workspace?.workspace_state === "bound" &&
    input.workspace.workspace_available;
  const proposal = input.envelopeProposal;
  const envelopeAccessKind = input.envelopeAccessKind;
  if (
    proposal &&
    !proposal.already_approved &&
    envelopeAccessKind === "never_approved" &&
    workspaceCanApprove &&
    bootstrapEnvelopeBlockedReason(proposal, workspaceCanApprove, null) === null
  ) {
    decisions.push({
      key: "envelope:approve",
      label: "Examiner et approuver l’enveloppe G2",
      detail: `${proposal.covered_operations} opération(s) couverte(s)`,
      href: "#overview-g2-details",
    });
  } else if (envelopeAccessKind === "active") {
    decisions.push({
      key: "envelope:extend",
      label: "Étendre l’enveloppe G2",
      detail: "Ajuster les autorisations ou les budgets",
      href: "#overview-g2-details",
    });
  }

  return decisions;
}

export const ACTIVE_AGENT_SESSION_STATES: ReadonlySet<
  AgentSessionView["state"]
> = new Set([
  "provisioning",
  "ready",
  "running",
  "awaiting_instruction",
  "interrupt_requested",
  "reconciling",
]);

export function isActiveAgentSession(session: AgentSessionView): boolean {
  return ACTIVE_AGENT_SESSION_STATES.has(session.state);
}

export function isActiveTask(task: Pick<TaskView, "status">): boolean {
  return !["completed", "cancelled", "failed"].includes(task.status);
}

export function verificationLabel(
  task: Pick<TaskView, "result_verification_status"> | undefined,
): string {
  if (!task) return "Non vérifié";
  switch (task.result_verification_status) {
    case "accepted":
      return "Accepté après vérification";
    case "rejected":
      return "Rejeté après vérification";
    case "pending_review":
      return "À vérifier";
    case "unverified":
      return "Non vérifié";
  }
}

const COUNT_FORMAT = new Intl.NumberFormat("fr-FR");

export function formatCount(value: number): string {
  return COUNT_FORMAT.format(value);
}

export function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return COUNT_FORMAT.format(value);
  if (value < 1024) return `${COUNT_FORMAT.format(value)} o`;
  const units = ["Kio", "Mio", "Gio", "Tio"] as const;
  let size = value;
  let unit: string = units[0];
  for (const candidate of units) {
    size /= 1024;
    unit = candidate;
    if (size < 1024) break;
  }
  return `${new Intl.NumberFormat("fr-FR", {
    maximumFractionDigits: 1,
  }).format(size)} ${unit}`;
}

export function redactTechnicalIdentifiers(value: string): string {
  return value
    .replace(
      /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/gi,
      "identifiant masqué",
    )
    .replace(
      /\b(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})\b/gi,
      "empreinte masquée",
    );
}

export function bounded<T>(
  items: readonly T[],
  max = 10,
): { visible: readonly T[]; hidden: number } {
  return {
    visible: items.slice(0, max),
    hidden: Math.max(items.length - max, 0),
  };
}
