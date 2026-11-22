/**
 * French vocabulary of the « Échantillons et résultats » cockpit view.
 *
 * Every map below transcribes one closed union of the frozen M24 API contract
 * (`../api`), so the backend value is always rendered with its canonical label
 * instead of leaking a raw enum to the analyst.
 */

import type {
  BootstrapCoverageKind,
  BootstrapIocKind,
  BootstrapIocOrigin,
  BootstrapPlanState,
  BootstrapReason,
  BootstrapRouteStatus,
  SampleOrigin,
  SampleQualification,
  SampleState,
  ToolInvocationStatus,
  WorkContributionCategory,
  WorkContributionImportStatus,
} from "../api";

export const IOC_KIND_LABELS: Record<BootstrapIocKind, string> = {
  sample: "Échantillon (hash)",
  ipv4: "Adresse IP",
  domain: "Domaine",
  url: "URL",
};

export const IOC_ORIGIN_LABELS: Record<BootstrapIocOrigin, string> = {
  seed: "Seed — déjà dans l’article",
  discovered: "Découvert — pivot à qualifier",
};

export const PLAN_STATE_LABELS: Record<BootstrapPlanState, string> = {
  empty: "Aucune opération planifiée",
  awaiting_g2: "En attente de l’enveloppe G2",
  ready: "Prêt à collecter",
  blocked: "Bloqué",
  unavailable: "Indisponible",
};

export const PLAN_STATE_REASONS: Record<BootstrapPlanState, string> = {
  empty: "Aucune opération VT n’est planifiée pour l’instant.",
  awaiting_g2:
    "Collecte en attente de l’approbation humaine de l’enveloppe G2.",
  ready: "",
  blocked:
    "Collecte bloquée : les capacités ou routes requises ne sont pas disponibles.",
  unavailable: "Collecte VT indisponible sur cet hôte.",
};

export const ROUTE_STATUS_LABELS: Record<BootstrapRouteStatus, string> = {
  configured: "Route configurée",
  unverified: "Route non vérifiée",
  unavailable: "Route indisponible",
};

export const COVERAGE_KIND_LABELS: Record<BootstrapCoverageKind, string> = {
  capability_absent: "Capacité absente",
  capability_shared: "Capacité partagée",
  route_unavailable: "Route indisponible",
  route_unverified: "Route non vérifiée",
  deferred: "Reporté",
  limit: "Limite",
};

export const REASON_LABELS: Record<BootstrapReason, string> = {
  pending: "En attente",
  available: "Disponible",
  downloaded: "Téléchargé",
  reused: "Réutilisé",
  empty: "Aucun résultat",
  not_found: "Introuvable",
  provider_denied: "Refusé par le fournisseur",
  route_unavailable: "Route indisponible",
  quota: "Quota atteint",
  failed: "Échec",
  partial: "Partiel",
  needs_review: "Revue requise",
  policy_denied: "Refusé par la politique",
  capability_absent: "Capacité absente",
  provider_not_allowed: "Fournisseur non autorisé",
  acquisition_not_allowed: "Acquisition non autorisée",
  disclosure_bounds: "Limites de divulgation",
  budget_exceeded: "Budget dépassé",
  awaiting_g2: "En attente de l’enveloppe G2",
  evidence_unavailable: "Preuve indisponible",
  not_attempted: "Non tenté",
  stopped: "Arrêté",
  invocation_failed: "Invocation en échec",
};

export const INVOCATION_STATUS_LABELS: Record<ToolInvocationStatus, string> = {
  pending: "En attente",
  running: "En cours",
  succeeded: "Réussi",
  empty: "Vide — aucun résultat",
  unavailable: "Indisponible",
  policy_denied: "Refusé par la politique",
  needs_review: "Revue humaine requise",
  failed: "Échec",
  partial: "Partiel",
};

export const SAMPLE_ORIGIN_LABELS: Record<SampleOrigin, string> = {
  seed: "Seed (article)",
  vt_pivot: "Pivot VT",
  relationship: "Relation VT",
  behavior: "Comportement",
  manual: "Manuel",
};

export const SAMPLE_QUALIFICATION_LABELS: Record<SampleQualification, string> =
  {
    candidate: "Candidat — décision G3 humaine requise",
    related: "Lié",
    confirmed: "Confirmé par décision humaine",
    rejected: "Rejeté par décision humaine",
    uncertain: "Incertain",
  };

export const SAMPLE_STATE_LABELS: Record<SampleState, string> = {
  quarantined: "En quarantaine",
  review_candidate: "Candidat à revue",
  validated: "Validé",
  rejected: "Rejeté",
};

export const DOWNLOAD_STATUS_LABELS: Record<string, string> = {
  downloaded: "Téléchargé et haché vérifié",
  reused: "Réutilisé depuis la quarantaine",
  not_attempted: "Téléchargement non tenté",
  failed: "Téléchargement en échec",
};

export const VT_TOOL_LABELS: Record<string, string> = {
  "vt.file_report": "Rapport fichier",
  "vt.object_report": "Rapport IP / domaine / URL",
  "vt.relationships": "Relations",
  "vt.behaviour": "Comportement (sandbox)",
};

export const CONTRIBUTION_CATEGORY_LABELS: Record<
  WorkContributionCategory,
  string
> = {
  work_contribution: "Note de travail",
  observation: "Observation",
  finding: "Finding",
};

export const CONTRIBUTION_IMPORT_LABELS: Record<
  WorkContributionImportStatus,
  string
> = {
  imported: "Importé",
  rejected: "Refusé",
  skipped: "Ignoré",
};

export function labelOf(
  labels: Readonly<Record<string, string>>,
  value: string,
): string {
  return labels[value] ?? value.replaceAll("_", " ");
}

/** Deterministic, locale-independent rendering of an ISO-8601 timestamp. */
export function formatTimestamp(value: string): string {
  return value.replace("T", " ").replace(/\.\d+/, "").replace(/Z$/, " UTC");
}

/** Single implementation shared with the workspace lot. */
export { workshopErrorText } from "../shell/stateText";

/** Human reading of one VT operation, including partial and negative results. */
export function operationOutcomeLabel(status: ToolInvocationStatus): string {
  switch (status) {
    case "empty":
      return "Résultat négatif — aucun champ observé";
    case "partial":
      return "Résultat partiel — couverture incomplète";
    case "unavailable":
      return "Outil indisponible";
    case "policy_denied":
      return "Refusé par la politique G2";
    case "needs_review":
      return "Revue humaine requise";
    case "failed":
      return "Échec de la collecte";
    case "pending":
    case "running":
    case "succeeded":
      return INVOCATION_STATUS_LABELS[status];
  }
}
