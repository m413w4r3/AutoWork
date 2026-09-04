import type { EditionRepairArticle } from "../../api/publication";

function rebuildStageLabel(stage: string): string {
  return [
    "rebuild_references",
    "references",
    "extraction",
    "apply_projection",
  ].includes(stage)
    ? "Références → Extraction"
    : "Synthèse → Assemblage";
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
  const rebuildArticles = articles.filter(
    (article) =>
      article.has_pending_projection ||
      article.resolved_since_last_build_count > 0,
  );
  if (readOnly || rebuildArticles.length === 0) return null;

  const pendingCount = rebuildArticles.filter((article) =>
    pendingSubjects.has(article.subject_id),
  ).length;

  return (
    <aside className="repair-rebuild-bar" aria-live="polite">
      <div>
        <strong>
          {rebuildArticles.length} article
          {rebuildArticles.length > 1 ? "s ont" : " a"} des réparations non
          appliquées.
        </strong>
        {pendingCount > 0 ? (
          <p>
            {pendingCount} reconstruction{pendingCount > 1 ? "s" : ""} en cours…
          </p>
        ) : null}
      </div>
      <ul>
        {rebuildArticles.map((article) => (
          <li key={article.subject_id}>
            <span>
              {titles.get(article.subject_id) ?? article.subject_id} ·{" "}
              {rebuildStageLabel(article.recommended_stage)}
            </span>
            <button
              className="button button--secondary"
              type="button"
              disabled={pendingSubjects.has(article.subject_id)}
              onClick={() => onRebuild(article.subject_id)}
            >
              {pendingSubjects.has(article.subject_id)
                ? "Reconstruction…"
                : "Reconstruire cet article"}
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
          ? "Reconstructions en cours…"
          : `Reconstruire ${rebuildArticles.length} article${rebuildArticles.length > 1 ? "s" : ""}`}
      </button>
    </aside>
  );
}

export { rebuildStageLabel };
