/** Libellés français et formatage borné de la Vue d’ensemble M24. */

import type {
  BootstrapCoverageKind,
  BootstrapPlanState,
  BootstrapReason,
  BootstrapRouteStatus,
  BootstrapSnapshotIocV1,
} from "../api/bootstrap";
import type { BudgetDimension } from "../api/types";
import type { WorkspaceAvailabilityCode } from "../api/workspace";
import type { WorkspaceAgentStatus } from "../api/workspaceAgent";

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

export function bounded<T>(
  items: readonly T[],
  max = 10,
): { visible: readonly T[]; hidden: number } {
  return {
    visible: items.slice(0, max),
    hidden: Math.max(items.length - max, 0),
  };
}
