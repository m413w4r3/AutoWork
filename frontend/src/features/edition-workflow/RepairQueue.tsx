import type { EditionRepairItem } from "../../api/publication";
import {
  repairPlanActionLabel,
  repairPlanCostLabel,
} from "./repairExecutionPlan";
import {
  REPAIR_QUEUE_FILTERS,
  repairIssueMatchesFilter,
  repairKindLabel,
  repairPreviewFallback,
  repairReasonLabel,
  repairStatusLabel,
  type RepairQueueFilter,
} from "./repairQueuePresentation";

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
                          {item.execution_plan.impact_kind !==
                          "no_deliverable_change" ? (
                            <span className="repair-issue-row__status">
                              {repairPlanActionLabel(item.execution_plan)} —{" "}
                              {repairPlanCostLabel(item.execution_plan)}
                            </span>
                          ) : item.execution_plan.ready_to_apply ? (
                            <span className="repair-issue-row__status">
                              Décision appliquée — aucun contenu à reconstruire
                            </span>
                          ) : null}
                        </span>
                        <code className="repair-issue-row__preview">
                          {item.preview || repairPreviewFallback(item)}
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
