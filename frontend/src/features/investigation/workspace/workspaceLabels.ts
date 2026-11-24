/**
 * French vocabulary and small formatters of the « Workspace et notes » view.
 *
 * The maps transcribe the closed unions of the frozen M24 API contract; a
 * human note path is only ever `notes/humaines/**.md`, and no host filesystem
 * path is handled by this module (the browser only reads the API).
 */

import type {
  ConflictChoice,
  ExportAction,
  ExportSource,
  InstructionState,
  NoteAnnotationKind,
  NoteContributionMethod,
  NoteContributionObjectType,
  NoteIocType,
  ToolInvocationStatus,
  WorkContributionCategory,
  WorkContributionImportStatus,
  WorkspaceAgentStatus,
  WorkspaceAvailabilityCode,
  WorkspaceImportStatus,
  WorkspaceOrigin,
  WorkspacePolicyClass,
} from "../api";

/** Agent availability as such: the workspace state has its own labels. */
export const AGENT_STATUS_LABELS: Record<WorkspaceAgentStatus, string> = {
  available: "Agent workspace disponible",
  unavailable: "Agent workspace indisponible",
  unconfigured: "Agent workspace non configuré",
};

export const AVAILABILITY_LABELS: Record<WorkspaceAvailabilityCode, string> = {
  available: "Agent disponible",
  unconfigured: "Agent non configuré",
  unavailable: "Agent indisponible",
  identity_conflict: "Conflit d’identité du workspace",
  not_bound: "Workspace non lié",
};

export const WORKSPACE_STATE_LABELS: Record<"bound" | "unbound", string> = {
  bound: "Workspace lié",
  unbound: "Aucun workspace lié",
};

export const POLICY_LABELS: Record<WorkspacePolicyClass, string> = {
  human_editable: "Modifiable",
  agent_owned: "Note d’agent",
  generated_immutable: "Généré · lecture seule",
  read_only: "Lecture seule",
  agent_internal: "Interne à l’agent",
};

export const ORIGIN_LABELS: Record<WorkspaceOrigin, string> = {
  filesystem: "Fichier local",
  ui: "Interface",
  agent: "Agent",
  system: "Système",
};

export const IMPORT_STATUS_LABELS: Record<WorkspaceImportStatus, string> = {
  received: "Reçu du workspace",
  applied: "Importé",
  ignored: "Ignoré",
  rejected: "Refusé",
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

export const CONFLICT_CHOICE_LABELS: Record<ConflictChoice, string> = {
  keep_current: "Conserver la version locale",
  keep_proposed: "Conserver la version proposée",
  merged_text: "Fusionner dans un texte",
};

export const EXPORT_ACTION_LABELS: Record<ExportAction, string> = {
  exported: "Exporté",
  already_exported: "Déjà exporté",
  preserved: "Préservé",
  conflict: "Conflit",
};

export const EXPORT_SOURCE_LABELS: Record<ExportSource, string> = {
  plan: "Plan",
  vt: "Rapports VT",
  results: "Résultats",
};

export const ANNOTATION_KIND_LABELS: Record<NoteAnnotationKind, string> = {
  link_task: "Lien vers une tâche",
  link_ioc: "Lien vers un IOC",
  link_sample: "Lien vers un échantillon",
  mark_useful: "Marquée utile",
  unmark_useful: "Marque « utile » retirée",
  attribute_author: "Auteur attribué",
  dismiss: "Écartée",
};

export const IOC_TYPE_LABELS: Record<NoteIocType, string> = {
  md5: "MD5",
  sha1: "SHA-1",
  sha256: "SHA-256",
  ipv4: "Adresse IPv4",
  domain: "Domaine",
  url: "URL",
};

export const NOTE_IOC_TYPES: readonly NoteIocType[] = [
  "md5",
  "sha1",
  "sha256",
  "ipv4",
  "domain",
  "url",
];

export const CONTRIBUTION_OBJECT_TYPES: readonly NoteContributionObjectType[] =
  ["sample", "file", "host", "ip", "domain", "url"];

export const CONTRIBUTION_METHODS: readonly NoteContributionMethod[] = [
  "hash",
  "static_analysis",
  "string_extraction",
  "yara_scan",
  "dynamic_behaviour",
  "network_capture_analysis",
  "dns_query",
  "http_request",
  "negative_check",
];

export const CONTRIBUTION_OBJECT_LABELS: Record<
  NoteContributionObjectType,
  string
> = {
  sample: "Échantillon",
  file: "Fichier",
  host: "Hôte",
  ip: "Adresse IP",
  domain: "Domaine",
  url: "URL",
};

export const CONTRIBUTION_METHOD_LABELS: Record<
  NoteContributionMethod,
  string
> = {
  hash: "Hachage",
  static_analysis: "Analyse statique",
  string_extraction: "Extraction de chaînes",
  yara_scan: "Analyse YARA",
  dynamic_behaviour: "Comportement dynamique",
  network_capture_analysis: "Analyse de capture réseau",
  dns_query: "Requête DNS",
  http_request: "Requête HTTP",
  negative_check: "Vérification négative",
};

export const INSTRUCTION_STATE_LABELS: Record<InstructionState, string> = {
  queued: "En file (agent occupé)",
  delivering: "Livraison en cours",
  delivered: "Transmise",
  acknowledged: "Acquittée",
  delivery_unknown: "Livraison inconnue",
  cancelled: "Annulée",
};

export const INVOCATION_STATUS_LABELS: Record<ToolInvocationStatus, string> = {
  pending: "En attente",
  running: "En cours",
  succeeded: "Réussi",
  empty: "Vide",
  unavailable: "Indisponible",
  policy_denied: "Refusé par la politique",
  needs_review: "Revue requise",
  failed: "Échec",
  partial: "Partiel",
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

export function formatShortDate(value: string): string {
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return "Date inconnue";
  return new Intl.DateTimeFormat("fr-FR", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(timestamp);
}

export function humanNoteLabelFromPath(path: string): string {
  const date = path.match(/(?:^|[-_])(20\d{2}-\d{2}-\d{2})(?:T|[-_.]|$)/)?.[1];
  return date === undefined
    ? "Note humaine"
    : `Note humaine · ${formatShortDate(`${date}T00:00:00Z`)}`;
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} o`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} Kio`;
  return `${(size / (1024 * 1024)).toFixed(1)} Mio`;
}

/** Single implementation shared with the samples lot. */
export { workshopErrorText } from "../shell/stateText";

/** The only path family the UI writes: a human Markdown note. */
export function isHumanNotePath(path: string): boolean {
  return (
    path.startsWith("notes/humaines/") &&
    path.endsWith(".md") &&
    !path.includes("..") &&
    !path.includes("\\") &&
    !path.startsWith("/")
  );
}
