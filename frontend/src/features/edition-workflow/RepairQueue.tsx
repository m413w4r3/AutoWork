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
  return (["include", "exclude"] as const).filter(
    (action) => action !== currentAction,
  );
}

export function repairStatusLabel(item: EditionRepairItem): string {
  if (item.recommended_stage === "revise_decision") {
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

export function RepairQueue({
  items,
  filter,
  search,
  selectedKey,
  selectedKeys,
  blockingSubjectIds,
  onFilterChange,
  onSearchChange,
  onSelect,
  onToggleSelection,
  selectable = true,
}: {
  items: EditionRepairItem[];
  filter: RepairQueueFilter;
  search: string;
  selectedKey: string | null;
  selectedKeys: ReadonlySet<string>;
  blockingSubjectIds: ReadonlySet<string>;
  onFilterChange: (filter: RepairQueueFilter) => void;
  onSearchChange: (value: string) => void;
  onSelect: (item: EditionRepairItem) => void;
  onToggleSelection: (item: EditionRepairItem, selected: boolean) => void;
  /**
   * False in a historical review: the queue stays fully readable but offers no
   * bulk selection, since no arbitration can follow it.
   */
  selectable?: boolean;
}) {
  const normalizedSearch = search.trim().toLocaleLowerCase("fr-FR");
  const visibleItems = items.filter((item) => {
    if (!repairIssueMatchesFilter(item, filter, blockingSubjectIds)) {
      return false;
    }
    if (!normalizedSearch) return true;
    return [
      item.article_title,
      item.source_id,
      item.source_title,
      item.artifact_type,
      item.preview,
      item.reason_code,
      repairReasonLabel(item.reason_code),
    ]
      .filter((value): value is string => Boolean(value))
      .some((value) =>
        value.toLocaleLowerCase("fr-FR").includes(normalizedSearch),
      );
  });

  const groups = new Map<string, EditionRepairItem[]>();
  for (const item of visibleItems) {
    const current = groups.get(item.subject_id) ?? [];
    current.push(item);
    groups.set(item.subject_id, current);
  }

  return (
    <section className="repair-queue" aria-labelledby="repair-queue-heading">
      <div className="repair-queue__heading">
        <div>
          <p className="eyebrow">Décisions éditoriales</p>
          <h3 id="repair-queue-heading">File de réparation</h3>
        </div>
        <span className="repair-queue__count" aria-live="polite">
          {visibleItems.length} affiché{visibleItems.length > 1 ? "s" : ""}
        </span>
      </div>

      <div
        className="repair-queue__filters"
        role="toolbar"
        aria-label="Filtres de réparation"
      >
        {REPAIR_QUEUE_FILTERS.map(([value, label]) => (
          <button
            key={value}
            className="button button--secondary"
            type="button"
            aria-pressed={filter === value}
            onClick={() => onFilterChange(value)}
          >
            {label}
          </button>
        ))}
      </div>
      <label className="repair-queue__search">
        Rechercher dans les éléments chargés
        <input
          type="search"
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder="Article, source, valeur, motif…"
        />
      </label>

      {groups.size > 0 ? (
        <ul className="repair-queue__articles">
          {Array.from(groups.entries()).map(([subjectId, articleItems]) => (
            <li key={subjectId} className="repair-queue__article">
              <h4>
                <span>{articleItems[0]?.position}</span>{" "}
                {articleItems[0]?.article_title}
              </h4>
              <ul className="repair-queue__issues">
                {articleItems.map((item) => (
                  <li key={item.repair_key}>
                    <div
                      className={`repair-issue-row${selectedKey === item.repair_key ? " is-selected" : ""}`}
                    >
                      {selectable ? (
                        <input
                          type="checkbox"
                          aria-label={`Sélectionner ${repairKindLabel(item)} ${item.preview || item.repair_key}`}
                          checked={selectedKeys.has(item.repair_key)}
                          onChange={(event) =>
                            onToggleSelection(item, event.target.checked)
                          }
                        />
                      ) : null}
                      <button
                        className="repair-issue-row__button"
                        type="button"
                        aria-current={
                          selectedKey === item.repair_key ? "true" : undefined
                        }
                        onClick={() => onSelect(item)}
                      >
                        <span className="repair-issue-row__meta">
                          <span>
                            {item.source_id ??
                              item.source_title ??
                              "Source inconnue"}
                          </span>
                          <strong>{repairKindLabel(item)}</strong>
                          <span className="repair-issue-row__status">
                            {repairStatusLabel(item)}
                          </span>
                        </span>
                        <code className="repair-issue-row__preview">
                          {item.preview || "Valeur non conservée"}
                        </code>
                        <span className="repair-issue-row__reason">
                          {repairReasonLabel(item.reason_code)}
                        </span>
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      ) : (
        <p className="empty-state">Aucun élément dans ce filtre.</p>
      )}
    </section>
  );
}
