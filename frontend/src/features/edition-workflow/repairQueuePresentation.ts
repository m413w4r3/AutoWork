import type {
  EditionRepairItem,
  ProductionRepairAction,
  ProductionRepairIssueKind,
} from "../../api/publication";

export type RepairQueueFilter =
  "all" | "sources" | "ioc" | "rules" | "other" | "resolved" | "blocking";

export const REPAIR_QUEUE_FILTERS: ReadonlyArray<
  readonly [RepairQueueFilter, string]
> = [
  ["all", "Tous à traiter"],
  ["sources", "Sources"],
  ["ioc", "IOC"],
  ["rules", "Règles"],
  ["other", "Autres"],
  ["resolved", "Résolus"],
];

export function repairKindLabel(item: EditionRepairItem): string {
  if (item.kind === "supplemental_source_unarchived") return "Source";
  if (item.kind === "rejected_rule") return "Règle";
  return item.is_publication_ioc ? "IOC" : "Autre perte";
}

export function repairActionLabel(action: ProductionRepairAction): string {
  if (action === "continue_without_source") return "Continué sans source";
  if (action === "replace") return "Valeur corrigée";
  return action === "include" ? "Inclus" : "Exclu";
}

/**
 * Decisions are revisable, so an arbitrated issue still offers the answers it
 * does not currently hold. The backend remains the authority: it refuses a
 * revision whose fence is stale.
 */
export function alternativeRepairActions(
  kind: ProductionRepairIssueKind,
  currentAction: ProductionRepairAction | null,
  resolved = false,
): ProductionRepairAction[] {
  if (kind === "supplemental_source_unarchived") {
    // A source is either still missing — and then waivable — or already
    // settled: an archived source owes a rebuild, never a new arbitration.
    return currentAction || resolved ? [] : ["continue_without_source"];
  }
  const actions: ProductionRepairAction[] = ["include", "exclude"];
  if (kind === "rejected_indicator") actions.push("replace");
  return actions.filter(
    (action) => action !== currentAction || action === "replace",
  );
}

/**
 * The queue is deliberately bounded and never opens an archive, so an empty
 * preview on a pre-evidence-pack rejection means "not read yet", not "lost".
 */
export function repairPreviewFallback(item: EditionRepairItem): string {
  return item.legacy_evidence
    ? "Valeur historique — ouvrir pour tentative de récupération"
    : "Valeur non conservée";
}

export function repairStatusLabel(item: EditionRepairItem): string {
  if (item.application_state === "unbuildable") {
    return "Décision inapplicable";
  }
  if (item.rebuild_required && item.resolved) return "À reconstruire";
  if (!item.resolved) {
    if (item.repair_state === "collection_missing")
      return "Source non attachée";
    return item.kind === "supplemental_source_unarchived"
      ? "Source à fournir"
      : "À arbitrer";
  }
  return item.effective_action
    ? repairActionLabel(item.effective_action)
    : "Arbitré";
}

export function repairReasonLabel(reasonCode: string): string {
  switch (reasonCode) {
    case "source_evidence_not_text_verifiable":
      return "La valeur n'a pas pu être vérifiée dans le texte archivé.";
    case "source_evidence_missing":
      return "La valeur n'est pas présente dans la représentation locale de la source.";
    case "source_rule_evidence_missing":
      return "Le corps exact de la règle n'a pas été retrouvé dans la source archivée.";
    case "supplemental_source_unarchived":
      return "Le collecteur n'a pas pu archiver la source proposée.";
    default:
      return "Le gate de production n'a pas pu vérifier cet élément.";
  }
}

export function repairIssueMatchesFilter(
  item: EditionRepairItem,
  filter: RepairQueueFilter,
  blockingSubjectIds: ReadonlySet<string>,
): boolean {
  if (filter === "resolved") return item.resolved;
  if (filter === "blocking") {
    return !item.resolved && blockingSubjectIds.has(item.subject_id);
  }
  if (item.resolved) return false;
  switch (filter) {
    case "sources":
      return item.kind === "supplemental_source_unarchived";
    case "ioc":
      return item.kind === "rejected_indicator" && item.is_publication_ioc;
    case "rules":
      return item.kind === "rejected_rule";
    case "other":
      return item.kind === "rejected_indicator" && !item.is_publication_ioc;
    case "all":
      return true;
  }
}
