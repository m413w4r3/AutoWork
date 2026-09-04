import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "../../api/editions";
import {
  cancelProductionRun,
  excludeReviewItem,
  includeReviewItem,
  retryProductionRun,
  type ReviewItem,
} from "../../api/publication";
import { Link } from "../../routing";
import { ReconciliationPanel } from "./ReconciliationPanel";
import type { RepairQueueFilter } from "./RepairQueue";

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
  if (item.requires_reconciliation) {
    return Promise.reject(
      new Error(
        "La réponse ChatGPT doit d’abord être récupérée ou abandonnée.",
      ),
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
  void queryClient.invalidateQueries({
    queryKey: ["edition-repair", editionId],
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
  void queryClient.invalidateQueries({
    queryKey: ["edition-repair", editionId],
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

function invalidateAfterCancel(
  queryClient: ReturnType<typeof useQueryClient>,
  editionId: string,
  subjectId: string,
) {
  void queryClient.invalidateQueries({
    queryKey: ["edition-review", editionId],
  });
  void queryClient.invalidateQueries({
    queryKey: ["edition-repair", editionId],
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
  readOnly = false,
  onRepairFilter,
}: {
  editionId: string;
  item: ReviewItem;
  readOnly?: boolean;
  onRepairFilter?: (filter: RepairQueueFilter, subjectId: string) => void;
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

  const cancel = useMutation({
    mutationFn: () => cancelProductionRun(item.run_id),
    retry: false,
    onSuccess: () => {
      setStaleMessage(null);
      invalidateAfterCancel(queryClient, editionId, item.subject_id);
    },
    onError: handleError,
  });

  const isActive =
    item.run_status === "queued" || item.run_status === "running";
  const isExcluded = item.effective_decision === "exclude";
  const isReadyIncluded = item.run_status === "ready" && item.included;
  const canReinclude =
    isExcluded && item.run_status === "ready" && hasDocument(item);
  // An ambiguous ChatGPT submission has its own recovery use case: replaying
  // the stage would duplicate or drop an answer the provider may already hold.
  const needsReconciliation =
    item.requires_reconciliation === true && item.reconciliation !== null;
  const canRetry =
    item.can_retry === true &&
    item.retry_stage !== null &&
    !needsReconciliation;
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
    include.isPending ||
    exclude.isPending ||
    retry.isPending ||
    cancel.isPending;
  const mutationError =
    include.error ?? exclude.error ?? retry.error ?? cancel.error;
  const hasLossSignals =
    item.rejected_indicator_count > 0 ||
    item.rejected_rule_count > 0 ||
    item.published_rule_count > 0;

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

      {item.effective_decision ? (
        <p className="review-item-card__decision">
          Décision finale :{" "}
          {item.effective_decision === "include" ? "inclure" : "exclure"}
        </p>
      ) : null}

      {hasLossSignals ? (
        <p
          className="review-item-card__loss-signals"
          aria-label="Signalement des pertes"
        >
          {item.rejected_rule_count > 0 ? (
            <button
              className="review-loss-badge review-loss-badge--alert"
              type="button"
              onClick={() => onRepairFilter?.("rules", item.subject_id)}
              disabled={!onRepairFilter}
            >
              {item.rejected_rule_count} règle(s) de détection à arbitrer
            </button>
          ) : null}
          {item.rejected_indicator_count > 0 ? (
            <button
              className="review-loss-badge review-loss-badge--warning"
              type="button"
              onClick={() => onRepairFilter?.("ioc", item.subject_id)}
              disabled={!onRepairFilter}
            >
              {item.rejected_indicator_count} indicateur(s) à arbitrer
            </button>
          ) : null}
          {item.published_rule_count > 0 ? (
            <span className="review-loss-badge review-loss-badge--neutral">
              {item.published_rule_count} règle(s) de détection publiée(s)
            </span>
          ) : null}
        </p>
      ) : null}

      {isProblem && item.error_message ? (
        <p className="review-item-card__message">{item.error_message}</p>
      ) : null}

      <div className="review-item-card__actions">
        <Link to={`/subjects/${item.subject_id}`}>Ouvrir</Link>
        {!readOnly && isActive ? (
          <button
            className="button button--danger"
            type="button"
            disabled={actionPending}
            onClick={() => cancel.mutate()}
          >
            {cancel.isPending ? "Arrêt…" : "Arrêter cette tentative"}
          </button>
        ) : null}
        {!readOnly && !isActive && isReadyIncluded ? (
          <button
            className="button button--danger"
            type="button"
            disabled={actionPending}
            onClick={() => setExcludeOpen(true)}
          >
            Exclure
          </button>
        ) : null}
        {!readOnly && !isActive && canReinclude ? (
          <button
            className="button"
            type="button"
            disabled={actionPending}
            onClick={() => include.mutate()}
          >
            Réinclure
          </button>
        ) : null}
        {!readOnly && !isActive && isProblem && !isExcluded ? (
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
        {!readOnly && !isActive && isProblem && isExcluded && canRetry ? (
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

      {!readOnly && !isActive && needsReconciliation && item.reconciliation ? (
        <ReconciliationPanel
          runId={item.run_id}
          reconciliation={item.reconciliation}
          onRecovered={() =>
            invalidateAfterRetry(queryClient, editionId, item.subject_id)
          }
        />
      ) : null}

      {!readOnly && excludeOpen ? (
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

      {!readOnly && staleMessage ? (
        <p className="error-message" role="alert">
          {staleMessage}
        </p>
      ) : !readOnly && mutationError && !isStaleReviewError(mutationError) ? (
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
