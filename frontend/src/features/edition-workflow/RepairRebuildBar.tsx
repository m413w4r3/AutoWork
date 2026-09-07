import type { EditionRepairArticle } from "../../api/publication";
import {
  repairPlanActionLabel,
  repairPlanCostLabel,
  repairPlanSubtitle,
} from "./repairExecutionPlan";

function isApplicable(article: EditionRepairArticle): boolean {
  return (
    article.execution_plan.ready_to_apply &&
    article.execution_plan.impact_kind !== "no_deliverable_change" &&
    (article.has_pending_projection ||
      article.resolved_since_last_build_count > 0)
  );
}

function articleActionLabel(article: EditionRepairArticle): string {
  const count = article.resolved_since_last_build_count;
  if (count > 1) {
    return `Appliquer ${count} réparations — ${repairPlanCostLabel(article.execution_plan)}`;
  }
  return repairPlanActionLabel(article.execution_plan);
}

export function RepairRebuildBar({
  articles,
  titles,
  pendingSubjects,
  readOnly,
  onRebuild,
  onRebuildAll,
}: {
  articles: EditionRepairArticle[];
  titles: ReadonlyMap<string, string>;
  pendingSubjects: ReadonlySet<string>;
  readOnly: boolean;
  onRebuild: (subjectId: string) => void;
  onRebuildAll: () => void;
}) {
  const rebuildArticles = articles.filter(isApplicable);
  if (readOnly || rebuildArticles.length === 0) return null;

  const pendingCount = rebuildArticles.filter((article) =>
    pendingSubjects.has(article.subject_id),
  ).length;
  const noModelCount = rebuildArticles.filter(
    (article) => !article.execution_plan.model_call_required,
  ).length;
  const synthesisCount = rebuildArticles.filter(
    (article) => article.execution_plan.impact_kind === "narrative",
  ).length;
  const sourceCount = rebuildArticles.filter(
    (article) => article.execution_plan.impact_kind === "source_corpus",
  ).length;
  const summary = [
    noModelCount > 0 ? `${noModelCount} sans appel modèle` : null,
    synthesisCount > 0
      ? `${synthesisCount} synthèse${synthesisCount > 1 ? "s" : ""}`
      : null,
    sourceCount > 0
      ? `${sourceCount} réintégration${sourceCount > 1 ? "s" : ""} de source`
      : null,
  ].filter((value): value is string => value !== null);

  return (
    <aside className="repair-rebuild-bar" aria-live="polite">
      <div>
        <strong>
          {rebuildArticles.length} article
          {rebuildArticles.length > 1 ? "s ont" : " a"} des réparations à
          appliquer.
        </strong>
        {summary.length > 0 ? <p>{summary.join(" · ")}</p> : null}
        {pendingCount > 0 ? (
          <p>
            {pendingCount} application{pendingCount > 1 ? "s" : ""} en cours…
          </p>
        ) : null}
      </div>
      <ul>
        {rebuildArticles.map((article) => (
          <li key={article.subject_id}>
            <span>
              {titles.get(article.subject_id) ?? article.subject_id} ·{" "}
              {repairPlanActionLabel(article.execution_plan)}
              <small>{repairPlanSubtitle(article.execution_plan)}</small>
            </span>
            <button
              className="button button--secondary"
              type="button"
              disabled={pendingSubjects.has(article.subject_id)}
              onClick={() => onRebuild(article.subject_id)}
            >
              {pendingSubjects.has(article.subject_id)
                ? "Application…"
                : articleActionLabel(article)}
            </button>
          </li>
        ))}
      </ul>
      <button
        className="button"
        type="button"
        disabled={pendingCount > 0}
        onClick={onRebuildAll}
      >
        {pendingCount > 0
          ? "Applications en cours…"
          : `Appliquer ${rebuildArticles.length} article${rebuildArticles.length > 1 ? "s" : ""}`}
      </button>
    </aside>
  );
}
