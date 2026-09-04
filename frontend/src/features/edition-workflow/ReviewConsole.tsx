import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  acceptEditionPublication,
  getEditionReview,
} from "../../api/publication";
import { ApiError } from "../../api/editions";
import { ReviewItemCard } from "./ReviewItemCard";
import { reviewPollingInterval } from "./reviewPolling";

export function ReviewConsole({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
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
  const includedCount = currentReview.items.filter(
    (item) => item.included,
  ).length;
  const blockingCount = currentReview.items.filter(
    (item) => item.blocking,
  ).length;
  const excludedCount = currentReview.items.filter(
    (item) => item.effective_decision === "exclude",
  ).length;
  // Excluded articles are intentionally omitted: this guardrail describes
  // what remains in the edition's publication scope.
  const publicationScopeItems = currentReview.items.filter(
    (item) => item.effective_decision !== "exclude",
  );
  const rejectedIndicatorCount = publicationScopeItems.reduce(
    (total, item) => total + item.rejected_indicator_count,
    0,
  );
  const rejectedRuleCount = publicationScopeItems.reduce(
    (total, item) => total + item.rejected_rule_count,
    0,
  );
  const publishedRuleCount = publicationScopeItems.reduce(
    (total, item) => total + item.published_rule_count,
    0,
  );

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

      <div className="review-summary" aria-label="Résumé de la revue">
        <strong>{includedCount} inclus</strong>
        <strong>{blockingCount} à corriger</strong>
        <strong>{excludedCount} exclus</strong>
      </div>
      <p className="review-summary__losses">
        Sur l’ensemble de l’édition : {rejectedIndicatorCount} indicateurs
        écartés, {rejectedRuleCount} règles de détection perdues,{" "}
        {publishedRuleCount} règles publiées.
      </p>

      {currentReview.items.length > 0 ? (
        <ol className="review-item-list" aria-label="Articles à revoir">
          {currentReview.items.map((item) => (
            <ReviewItemCard
              key={`${item.subject_id}-${item.position}`}
              editionId={editionId}
              item={item}
              readOnly={readOnly}
            />
          ))}
        </ol>
      ) : (
        <p className="empty-state">Aucun article à revoir.</p>
      )}

      <section className="review-acceptance" aria-labelledby="accept-heading">
        <div>
          <h3 id="accept-heading">
            {readOnly ? "État final de la revue" : "Accepter la production"}
          </h3>
          <p>
            {readOnly
              ? "Cette revue historique est disponible en lecture seule."
              : currentReview.can_accept
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
            disabled={!currentReview.can_accept || accept.isPending}
            onClick={() => accept.mutate()}
          >
            {accept.isPending ? "Acceptation…" : "Accepter la production"}
          </button>
        ) : null}
      </section>
    </section>
  );
}
