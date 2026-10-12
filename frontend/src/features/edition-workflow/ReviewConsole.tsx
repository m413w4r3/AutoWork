import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import {
  getEditionReview,
  type EditionRepairSummary,
} from "../../api/publication";
import { RepairDesk } from "./RepairDesk";
import { EditionPreviewPanel } from "./EditionPreviewPanel";
import { reviewPollingInterval } from "./reviewPolling";

const EMPTY_REPAIR_SUMMARY: EditionRepairSummary = {
  unresolved_total: 0,
  sources_to_supply: 0,
  rejected_iocs_to_review: 0,
  rejected_rules_to_review: 0,
  rejected_other_artifacts: 0,
  articles_with_repairs: 0,
  articles_needing_rebuild: 0,
};

export function ReviewConsole({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const [repairSummary, setRepairSummary] = useState(EMPTY_REPAIR_SUMMARY);
  const [repairSummaryLoaded, setRepairSummaryLoaded] = useState(readOnly);
  const [tab, setTab] = useState<"review" | "preview">("review");
  const handleRepairSummaryChange = useCallback(
    (summary: EditionRepairSummary) => {
      setRepairSummary(summary);
      setRepairSummaryLoaded(true);
    },
    [],
  );
  const review = useQuery({
    queryKey: ["edition-review", editionId],
    queryFn: () => getEditionReview(editionId),
    refetchInterval: (query) => reviewPollingInterval(query.state.data),
  });
  if (review.isPending) return <p role="status">Chargement de la revue…</p>;
  if (review.isError) {
    return (
      <p className="error-message" role="alert">
        La revue de publication est inaccessible : {String(review.error)}
      </p>
    );
  }
  if (!review.data) return null;

  const currentReview = review.data;
  const unresolvedRepairCount =
    currentReview.unresolved_repair_count ?? repairSummary.unresolved_total;
  // The backend owns the rebuild debt (LOT 24): an archived source waiting for
  // its REFERENCES reconciliation keeps blocking sign-off across a refresh.
  const pendingRebuildCount = Math.max(
    currentReview.pending_rebuild_count ?? 0,
    repairSummary.articles_needing_rebuild,
  );
  const repairSummaryEntries: ReadonlyArray<readonly [number, string, string]> =
    [
      [repairSummary.rejected_iocs_to_review, "IOC", "IOC"],
      [repairSummary.rejected_rules_to_review, "règle", "règles"],
      [repairSummary.sources_to_supply, "source", "sources"],
      [repairSummary.rejected_other_artifacts, "autre perte", "autres pertes"],
    ];
  const repairSummaryParts = repairSummaryEntries
    .filter(([count]) => count > 0)
    .map(
      ([count, singular, plural]) =>
        `${count} ${count === 1 ? singular : plural}`,
    );
  const unresolvedMessage =
    repairSummaryParts.length > 0
      ? `${repairSummaryParts.join(", ")} restent à arbitrer.`
      : `${unresolvedRepairCount} élément${unresolvedRepairCount > 1 ? "s" : ""} reste${unresolvedRepairCount > 1 ? "nt" : ""} à arbitrer.`;
  return (
    <section
      className="review-console"
      aria-labelledby="publication-review-heading"
    >
      <div className="review-console__heading">
        <div>
          <p className="eyebrow">Revue</p>
          <h2 id="publication-review-heading">Revue de publication</h2>
        </div>
      </div>

      <nav className="workbench-tabs" aria-label="Outils de l’édition">
        <button
          type="button"
          aria-pressed={tab === "review"}
          onClick={() => setTab("review")}
        >
          Revue
        </button>
        <button
          type="button"
          aria-pressed={tab === "preview"}
          onClick={() => setTab("preview")}
        >
          Prévisualisation
        </button>
      </nav>

      {tab === "preview" ? (
        <EditionPreviewPanel editionId={editionId} />
      ) : (
        <>
          <RepairDesk
            editionId={editionId}
            reviewItems={currentReview.items}
            readOnly={readOnly}
            onSummaryChange={handleRepairSummaryChange}
          />

          <section
            className="review-acceptance"
            aria-labelledby="accept-heading"
          >
            <div>
              <h3 id="accept-heading">
                {readOnly ? "État de la revue" : "Validation éditoriale"}
              </h3>
              <p>
                {readOnly
                  ? "Cette revue historique est disponible en lecture seule."
                  : !repairSummaryLoaded
                    ? "Chargement du résumé de réparation…"
                    : unresolvedRepairCount > 0
                      ? `La revue technique n’est pas terminée : ${unresolvedMessage}`
                      : pendingRebuildCount > 0
                        ? `${pendingRebuildCount} article${pendingRebuildCount > 1 ? "s" : ""} ${pendingRebuildCount > 1 ? "doivent" : "doit"} être reconstruit${pendingRebuildCount > 1 ? "s" : ""} avant l’acceptation.`
                        : currentReview.can_accept
                          ? "La validation technique est prête. L’acceptation finale se fait depuis Composition avec confirmation de version."
                          : "Résolvez les diagnostics bloquants avant l’acceptation."}
              </p>
            </div>
            {!readOnly ? (
              <a className="button" href={`/editions/${editionId}/composition`}>
                Ouvrir la composition
              </a>
            ) : null}
          </section>
        </>
      )}
    </section>
  );
}
