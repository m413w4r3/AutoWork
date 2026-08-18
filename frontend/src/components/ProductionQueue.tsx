/**
 * Production Queue Component
 *
 * Controls for the edition's batch production. The list of producible subjects
 * lives in the editorial board's "Prêts à traiter" section — this component
 * only acts on that selection, so a subject is never listed twice.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  getEditionBriefProduction,
  startEditionBriefProduction,
} from "../api/production";

export interface ProductionQueueBrief {
  subjectId: string;
  title: string;
}

interface ProductionQueueProps {
  editionId: string;
  /** Selected briefs of the edition, i.e. what is eligible for production. */
  briefs?: ProductionQueueBrief[];
  /** Subjects ticked in the editorial board. */
  selectedSubjects?: string[];
  onProduced?: () => void;
}

const ITEM_STATUS_LABELS: Record<string, string> = {
  queued: "En attente",
  running: "En cours",
  ready: "Prête",
  needs_review: "À vérifier",
  failed: "En échec",
  cancelled: "Annulée",
};

export function ProductionQueue({
  editionId,
  briefs = [],
  selectedSubjects = [],
  onProduced,
}: ProductionQueueProps) {
  const {
    data: batch,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ["batch", editionId],
    queryFn: () => getEditionBriefProduction(editionId),
    // Only poll while a batch is actually in flight. With no batch there is
    // nothing to watch, and polling then would make every editorial board
    // page chatter against the API.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 2000 : false;
    },
  });

  const start = useMutation({
    mutationFn: (subjectIds?: string[]) =>
      startEditionBriefProduction(editionId, subjectIds),
    onSuccess: () => {
      onProduced?.();
      void refetch();
    },
  });

  if (isLoading) return <p role="status">Chargement du lot de production…</p>;
  if (error)
    return (
      <p role="alert" className="error-message">
        Le lot de production est inaccessible : {String(error)}
      </p>
    );

  // No batch yet: this is the entry point, not an error.
  if (!batch) {
    if (briefs.length === 0) {
      return (
        <section className="production-panel">
          <p className="empty-state">
            Aucune brève sélectionnée n’est prête à être produite.
          </p>
        </section>
      );
    }

    return (
      <section className="production-panel" aria-labelledby="production-start">
        <div className="production-panel__heading">
          <div>
            <p className="eyebrow">Production</p>
            <h2 id="production-start">
              {briefs.length} brève{briefs.length > 1 ? "s" : ""} prête
              {briefs.length > 1 ? "s" : ""}
            </h2>
          </div>
          <p className="production-hint">
            Cochez des sujets dans « Prêts à traiter » pour n’en produire qu’une
            partie.
          </p>
        </div>

        {start.error ? (
          <p role="alert" className="error-message">
            {String(start.error)}
          </p>
        ) : null}

        <div className="production-actions">
          <button
            className="button"
            disabled={start.isPending}
            onClick={() => start.mutate(undefined)}
          >
            {start.isPending
              ? "Démarrage…"
              : `Traiter les ${briefs.length} brèves`}
          </button>
          <button
            className="button button--secondary"
            disabled={start.isPending || selectedSubjects.length === 0}
            onClick={() => start.mutate(selectedSubjects)}
          >
            {`Traiter les ${selectedSubjects.length} sélectionnées`}
          </button>
        </div>
      </section>
    );
  }

  const totalItems = batch.items;
  const processedItems = batch.completed + batch.needs_review + batch.failed;
  const progressPercent =
    totalItems > 0 ? Math.round((processedItems / totalItems) * 100) : 0;

  return (
    <section className="production-panel" aria-labelledby="production-batch">
      <div className="production-panel__heading">
        <div>
          <p className="eyebrow">Production</p>
          <h2 id="production-batch">
            {processedItems} / {totalItems} brèves traitées
          </h2>
        </div>
        <span className="badge">{batch.status.replace(/_/g, " ")}</span>
      </div>

      <progress max={100} value={progressPercent}>
        {progressPercent}%
      </progress>

      <p className="production-counters">
        <strong>{batch.completed}</strong> prêtes ·{" "}
        <strong>{batch.needs_review}</strong> à vérifier ·{" "}
        <strong>{batch.failed}</strong> en échec
        {batch.cancelled ? (
          <>
            {" "}
            · <strong>{batch.cancelled}</strong> annulées
          </>
        ) : null}
      </p>

      {batch.item_details.length > 0 && (
        <ol className="production-item-list" aria-label="File de production">
          {batch.item_details.map((item) => (
            <li key={item.run_id}>
              <span className="production-item__position">
                {item.position}/{totalItems}
              </span>
              <a href={`/subjects/${item.subject_id}`}>{item.title}</a>
              <span className={`production-item__status is-${item.status}`}>
                {ITEM_STATUS_LABELS[item.status] ?? item.status}
                {item.status === "running" ? ` · ${item.current_stage}` : ""}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
