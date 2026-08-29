import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  getEditionProduction,
  type BatchItemDetail,
  type BatchStatus,
  type ProductionBatchPhase,
  type SubjectProductionStage,
  type SubjectProductionStatus,
} from "../../api/production";
import { Link } from "../../routing";

const STATUS_LABELS: Record<SubjectProductionStatus, string> = {
  queued: "En attente",
  running: "En cours",
  ready: "Prêt",
  needs_review: "À vérifier",
  failed: "Échec",
  cancelled: "Annulé",
};

const STAGE_LABELS: Record<SubjectProductionStage, string> = {
  sources: "Sources",
  references: "Références",
  extraction: "Extraction",
  synthesis: "Synthèse",
  analyst_research: "Recherche analyste",
  analyst_note: "Note analyste",
  assembly: "Assemblage",
};

const PHASE_LABELS: Record<ProductionBatchPhase, string> = {
  initial: "Production initiale",
  recovery: "Récupération automatique",
  review: "Finalisation",
};

const TERMINAL_BATCH_STATUSES = new Set([
  "completed",
  "completed_with_issues",
  "cancelled",
]);

function formatCountdown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function useDispatchCountdown(nextDispatchAt: string | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  const dispatchTime = nextDispatchAt ? Date.parse(nextDispatchAt) : NaN;

  useEffect(() => {
    if (!Number.isFinite(dispatchTime) || dispatchTime <= Date.now()) {
      setNow(Date.now());
      return undefined;
    }
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [dispatchTime]);

  if (!Number.isFinite(dispatchTime) || dispatchTime <= now) return null;
  return Math.ceil((dispatchTime - now) / 1_000);
}

function processedCount(batch: BatchStatus): number {
  return batch.completed + batch.needs_review + batch.failed + batch.cancelled;
}

function BatchCounters({ batch }: { batch: BatchStatus }) {
  return (
    <div className="production-counters" aria-label="Compteurs de production">
      <span>
        <strong>{batch.completed}</strong> prêts
      </span>
      <span>
        <strong>{batch.needs_review}</strong> à vérifier
      </span>
      <span>
        <strong>{batch.failed}</strong> échecs
      </span>
      {batch.cancelled > 0 ? (
        <span>
          <strong>{batch.cancelled}</strong> annulés
        </span>
      ) : null}
    </div>
  );
}

function ItemError({ item }: { item: BatchItemDetail }) {
  if (
    !item.error_message ||
    (item.status !== "failed" && item.status !== "needs_review")
  ) {
    return null;
  }
  return (
    <div className="production-item__error">
      <span>{item.error_message}</span>
      {item.error_code ? <small>Code : {item.error_code}</small> : null}
    </div>
  );
}

function ProductionItem({
  item,
  total,
}: {
  item: BatchItemDetail;
  total: number;
}) {
  const isActive = item.status === "running";
  return (
    <li className={`production-item production-item--${item.status}`}>
      <div className="production-item__main">
        <span className="production-item__position">
          {item.position}/{total}
        </span>
        <Link to={`/subjects/${item.subject_id}`}>{item.title}</Link>
        <span className={`production-item__status is-${item.status}`}>
          {STATUS_LABELS[item.status]}
        </span>
      </div>
      <div className="production-item__details">
        {isActive ? (
          <span>Étape : {STAGE_LABELS[item.current_stage]}</span>
        ) : null}
        {item.auto_recovery_count > 0 ? (
          <span>
            {item.auto_recovery_count} récupération automatique
            {item.auto_recovery_count > 1 ? "s" : ""}
          </span>
        ) : null}
        <span>Génération {item.pipeline_generation}</span>
      </div>
      <ItemError item={item} />
    </li>
  );
}

export function ProductionConsole({ editionId }: { editionId: string }) {
  const queryClient = useQueryClient();
  const editionInvalidated = useRef(false);
  const batch = useQuery({
    queryKey: ["batch", editionId],
    queryFn: () => getEditionProduction(editionId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "queued" || status === "running" ? 2_000 : false;
    },
  });

  useEffect(() => {
    if (
      batch.data &&
      TERMINAL_BATCH_STATUSES.has(batch.data.status) &&
      !editionInvalidated.current
    ) {
      editionInvalidated.current = true;
      void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
    }
  }, [batch.data, editionId, queryClient]);

  const countdown = useDispatchCountdown(batch.data?.next_dispatch_at ?? null);

  if (batch.isPending) return <p role="status">Chargement de la production…</p>;
  if (batch.isError) {
    return (
      <p className="error-message" role="alert">
        La supervision de production est inaccessible : {String(batch.error)}
      </p>
    );
  }
  if (!batch.data) {
    return (
      <section className="production-panel">
        <p className="empty-state">Aucun lot de production n’est disponible.</p>
      </section>
    );
  }

  const currentBatch = batch.data;
  const processed = processedCount(currentBatch);
  const progress =
    currentBatch.items > 0
      ? Math.round((processed / currentBatch.items) * 100)
      : 0;

  return (
    <section
      className="production-panel production-console"
      aria-labelledby="production-console-heading"
    >
      <div className="production-panel__heading">
        <div>
          <p className="eyebrow">Production</p>
          <h2 id="production-console-heading">
            {processed} / {currentBatch.items} articles traités
          </h2>
        </div>
        <div className="production-phase" data-phase={currentBatch.phase}>
          <span>Phase courante</span>
          <strong>{PHASE_LABELS[currentBatch.phase]}</strong>
        </div>
      </div>
      {currentBatch.phase === "recovery" ? (
        <p className="production-recovery-note" role="status">
          Une récupération automatique est en cours. La production reprend son
          cours sans intervention.
        </p>
      ) : null}
      {countdown !== null ? (
        <p className="production-next-dispatch">
          Prochain article dans {formatCountdown(countdown)}
        </p>
      ) : null}
      <progress max={100} value={progress}>
        {progress} %
      </progress>
      <BatchCounters batch={currentBatch} />
      {currentBatch.item_details.length > 0 ? (
        <ol className="production-item-list" aria-label="Suivi des articles">
          {currentBatch.item_details.map((item) => (
            <ProductionItem
              key={item.run_id}
              item={item}
              total={currentBatch.items}
            />
          ))}
        </ol>
      ) : null}
    </section>
  );
}
