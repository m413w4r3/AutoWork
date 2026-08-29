import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../api/editions";
import {
  excludeReviewItem,
  includeReviewItem,
  retryProductionRun,
  type ReviewItem,
} from "../../api/publication";
import { Link } from "../../routing";

const STALE_MESSAGE =
  "Cet article a changé depuis son ouverture. La revue a été rechargée.";

function isStaleReviewError(error: unknown): boolean {
  return error instanceof ApiError && error.code === "review_item_stale";
}

function hasDocument(item: ReviewItem): boolean {
  return (
    item.document_artifact_id !== null &&
    item.document_artifact_version !== null &&
    item.document_input_hash !== null
  );
}

function retryItem(item: ReviewItem) {
  if (!item.can_retry || item.retry_stage === null) {
    return Promise.reject(
      new Error("Cette tentative ne peut pas être relancée."),
    );
  }
  return retryProductionRun(item.run_id, item.retry_stage);
}

function invalidateReview(
  queryClient: ReturnType<typeof useQueryClient>,
  editionId: string,
) {
  void queryClient.invalidateQueries({
    queryKey: ["edition-review", editionId],
  });
}

function invalidateAfterRetry(
  queryClient: ReturnType<typeof useQueryClient>,
  editionId: string,
  subjectId: string,
) {
  void queryClient.invalidateQueries({
    queryKey: ["edition-review", editionId],
  });
  void queryClient.invalidateQueries({ queryKey: ["batch", editionId] });
  void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
  if (queryClient.getQueryState(["subject-production", subjectId])) {
    void queryClient.invalidateQueries({
      queryKey: ["subject-production", subjectId],
    });
  }
  void queryClient.invalidateQueries({
    queryKey: ["subject-content", subjectId],
  });
  void queryClient.invalidateQueries({
    queryKey: ["subject-indicators", subjectId],
  });
}

export function ReviewItemCard({
  editionId,
  item,
}: {
  editionId: string;
  item: ReviewItem;
}) {
  const queryClient = useQueryClient();
  const [excludeOpen, setExcludeOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [staleMessage, setStaleMessage] = useState<string | null>(null);

  const handleError = (error: unknown) => {
    if (isStaleReviewError(error)) {
      setStaleMessage(STALE_MESSAGE);
      invalidateReview(queryClient, editionId);
    }
  };

  const include = useMutation({
    mutationFn: () => includeReviewItem(editionId, item),
    retry: false,
    onSuccess: () => {
      setStaleMessage(null);
      invalidateReview(queryClient, editionId);
    },
    onError: handleError,
  });

  const exclude = useMutation({
    mutationFn: (value: string) => excludeReviewItem(editionId, item, value),
    retry: false,
    onSuccess: () => {
      setStaleMessage(null);
      invalidateReview(queryClient, editionId);
    },
    onError: handleError,
  });

  const retry = useMutation({
    mutationFn: () => retryItem(item),
    retry: false,
    onSuccess: () => {
      setStaleMessage(null);
      invalidateAfterRetry(queryClient, editionId, item.subject_id);
    },
    onError: handleError,
  });

  const isActive =
    item.run_status === "queued" || item.run_status === "running";
  const isExcluded = item.effective_decision === "exclude";
  const isReadyIncluded = item.run_status === "ready" && item.included;
  const canReinclude =
    isExcluded && item.run_status === "ready" && hasDocument(item);
  const canRetry = item.can_retry === true && item.retry_stage !== null;
  const isProblem =
    item.run_status === "failed" ||
    item.run_status === "needs_review" ||
    item.run_status === "cancelled" ||
    (item.run_status === "ready" && !item.included && !isExcluded);
  const statusLabel = isActive
    ? "Nouvelle tentative en cours"
    : isExcluded
      ? "Exclu"
      : isReadyIncluded
        ? "Prêt"
        : "À corriger";
  const actionPending =
    include.isPending || exclude.isPending || retry.isPending;
  const mutationError = include.error ?? exclude.error ?? retry.error;

  const confirmExclude = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const normalizedReason = reason.trim();
    if (!normalizedReason) return;
    exclude.mutate(normalizedReason);
  };

  return (
    <li className={`review-item-card review-item-card--${item.run_status}`}>
      <div className="review-item-card__heading">
        <div className="review-item-card__title">
          <span className="review-item-card__position">{item.position}</span>
          <Link to={`/subjects/${item.subject_id}`}>{item.title}</Link>
        </div>
        <span className={`review-item-card__status is-${item.run_status}`}>
          {statusLabel}
        </span>
      </div>

      {isProblem && item.error_message ? (
        <p className="review-item-card__message">{item.error_message}</p>
      ) : null}

      <div className="review-item-card__actions">
        <Link to={`/subjects/${item.subject_id}`}>Ouvrir</Link>
        {!isActive && isReadyIncluded ? (
          <button
            className="button button--danger"
            type="button"
            disabled={actionPending}
            onClick={() => setExcludeOpen(true)}
          >
            Exclure
          </button>
        ) : null}
        {!isActive && canReinclude ? (
          <button
            className="button"
            type="button"
            disabled={actionPending}
            onClick={() => include.mutate()}
          >
            Réinclure
          </button>
        ) : null}
        {!isActive && isProblem && !isExcluded ? (
          <>
            {canRetry ? (
              <button
                className="button button--secondary"
                type="button"
                disabled={actionPending}
                onClick={() => retry.mutate()}
              >
                {retry.isPending ? "Nouvelle tentative…" : "Réessayer"}
              </button>
            ) : null}
            <button
              className="button button--danger"
              type="button"
              disabled={actionPending}
              onClick={() => setExcludeOpen(true)}
            >
              Exclure
            </button>
          </>
        ) : null}
        {!isActive && isProblem && isExcluded && canRetry ? (
          <button
            className="button button--secondary"
            type="button"
            disabled={actionPending}
            onClick={() => retry.mutate()}
          >
            {retry.isPending ? "Nouvelle tentative…" : "Réessayer"}
          </button>
        ) : null}
      </div>

      {excludeOpen ? (
        <form className="review-item-card__exclude" onSubmit={confirmExclude}>
          <label htmlFor={`exclude-reason-${item.subject_id}`}>
            Raison de l’exclusion
          </label>
          <textarea
            id={`exclude-reason-${item.subject_id}`}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            rows={2}
          />
          <div className="review-item-card__exclude-actions">
            <button
              className="button button--danger"
              type="submit"
              disabled={!reason.trim() || actionPending}
            >
              {exclude.isPending ? "Confirmation…" : "Confirmer"}
            </button>
            <button
              className="button button--secondary"
              type="button"
              disabled={actionPending}
              onClick={() => setExcludeOpen(false)}
            >
              Annuler
            </button>
          </div>
        </form>
      ) : null}

      {staleMessage ? (
        <p className="error-message" role="alert">
          {staleMessage}
        </p>
      ) : mutationError && !isStaleReviewError(mutationError) ? (
        <p className="error-message" role="alert">
          {mutationError instanceof Error
            ? mutationError.message
            : "La revue n’a pas pu être mise à jour."}
        </p>
      ) : null}

      <details className="review-item-card__diagnostics">
        <summary>Diagnostics</summary>
        <dl>
          <dt>run_id</dt>
          <dd>{item.run_id}</dd>
          <dt>generation</dt>
          <dd>{item.pipeline_generation}</dd>
          <dt>artifact_id</dt>
          <dd>{item.document_artifact_id ?? "—"}</dd>
          <dt>artifact_version</dt>
          <dd>{item.document_artifact_version ?? "—"}</dd>
          <dt>input_hash</dt>
          <dd>{item.document_input_hash ?? "—"}</dd>
          <dt>decision_id</dt>
          <dd>{item.effective_decision_id ?? "—"}</dd>
          <dt>error_code</dt>
          <dd>{item.error_code ?? "—"}</dd>
        </dl>
      </details>
    </li>
  );
}

export { STALE_MESSAGE };
