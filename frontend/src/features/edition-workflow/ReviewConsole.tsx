import { useQuery } from "@tanstack/react-query";

import { getEditionReview } from "../../api/publication";
import { ReviewItemCard } from "./ReviewItemCard";
import { reviewPollingInterval } from "./reviewPolling";

export function ReviewConsole({ editionId }: { editionId: string }) {
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
  const includedCount = currentReview.items.filter(
    (item) => item.included,
  ).length;
  const blockingCount = currentReview.items.filter(
    (item) => item.blocking,
  ).length;
  const excludedCount = currentReview.items.filter(
    (item) => item.effective_decision === "exclude",
  ).length;

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

      {currentReview.items.length > 0 ? (
        <ol className="review-item-list" aria-label="Articles à revoir">
          {currentReview.items.map((item) => (
            <ReviewItemCard
              key={`${item.subject_id}-${item.position}`}
              editionId={editionId}
              item={item}
            />
          ))}
        </ol>
      ) : (
        <p className="empty-state">Aucun article à revoir.</p>
      )}

      <section className="review-acceptance" aria-labelledby="accept-heading">
        <div>
          <h3 id="accept-heading">Accepter la production</h3>
          <p>
            {currentReview.can_accept
              ? "L’assemblage final sera activé à l’étape de publication."
              : "Résolvez ou excluez les articles bloquants."}
          </p>
        </div>
        <button className="button" type="button" disabled>
          Accepter la production
        </button>
      </section>
    </section>
  );
}
