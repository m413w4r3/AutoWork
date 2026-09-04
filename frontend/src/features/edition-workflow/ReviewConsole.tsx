import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import {
  acceptEditionPublication,
  getEditionReview,
  type EditionRepairSummary,
} from "../../api/publication";
import { ApiError } from "../../api/editions";
import { RepairDesk } from "./RepairDesk";
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
  const queryClient = useQueryClient();
  const [repairSummary, setRepairSummary] = useState(EMPTY_REPAIR_SUMMARY);
  const [repairSummaryLoaded, setRepairSummaryLoaded] = useState(readOnly);
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
  const accept = useMutation({
    mutationFn: () => acceptEditionPublication(editionId),
    retry: false,
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["edition-release", editionId],
      });
      void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
      void queryClient.invalidateQueries({ queryKey: ["editions"] });
    },
    onError: (error) => {
      if (
        error instanceof ApiError &&
        error.code === "review_cannot_be_accepted"
      ) {
        void queryClient.invalidateQueries({
          queryKey: ["edition-review", editionId],
        });
      }
    },
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
  const canAccept =
    (readOnly || repairSummaryLoaded) &&
    currentReview.can_accept &&
    unresolvedRepairCount === 0 &&
    pendingRebuildCount === 0;

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

      <RepairDesk
        editionId={editionId}
        reviewItems={currentReview.items}
        readOnly={readOnly}
        onSummaryChange={handleRepairSummaryChange}
      />

      <section className="review-acceptance" aria-labelledby="accept-heading">
        <div>
          <h3 id="accept-heading">
            {readOnly ? "État final de la revue" : "Accepter la production"}
          </h3>
          <p>
            {readOnly
              ? "Cette revue historique est disponible en lecture seule."
              : !repairSummaryLoaded
                ? "Chargement du résumé de réparation…"
                : unresolvedRepairCount > 0
                  ? `La revue technique n’est pas terminée : ${unresolvedMessage}`
                  : pendingRebuildCount > 0
                    ? `${pendingRebuildCount} article${pendingRebuildCount > 1 ? "s" : ""} ${pendingRebuildCount > 1 ? "doivent" : "doit"} être reconstruit${pendingRebuildCount > 1 ? "s" : ""} avant finalisation.`
                    : canAccept
                      ? "L’assemblage final sera activé à l’étape de publication."
                      : "Résolvez ou excluez les articles bloquants."}
          </p>
        </div>
        {!readOnly && accept.error ? (
          <p className="error-message" role="alert">
            {accept.error instanceof ApiError &&
            accept.error.code === "review_cannot_be_accepted"
              ? "La revue ne peut plus être acceptée. Rechargez la revue."
              : accept.error instanceof Error
                ? accept.error.message
                : "La publication n’a pas pu être acceptée."}
          </p>
        ) : null}
        {!readOnly ? (
          <button
            className="button"
            type="button"
            disabled={!canAccept || accept.isPending}
            onClick={() => accept.mutate()}
          >
            {accept.isPending ? "Acceptation…" : "Accepter la production"}
          </button>
        ) : null}
      </section>
    </section>
  );
}
