import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  getEditionProduction,
  cancelProductionBatch,
  pauseProductionBatch,
  resumeProductionBatch,
  type BatchItemDetail,
  type ProductionBatch,
  type ProductionBatchSummary,
  type ProductionActivity,
  type ProductionSubject,
} from "../../api/production";
import { ExtractionProgressView } from "../../components/ExtractionProgress";
import { Link } from "../../routing";
import { productionBatchPollingInterval } from "../production/productionPolling";
import {
  PHASE_LABELS,
  STAGE_LABELS,
  STATUS_LABELS,
} from "../production/productionLabels";
import { ReconciliationPanel } from "./ReconciliationPanel";

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

function formatElapsed(seconds: number | null | undefined): string {
  const safeSeconds = Math.max(0, seconds ?? 0);
  const hours = Math.floor(safeSeconds / 3_600);
  const minutes = Math.floor((safeSeconds % 3_600) / 60);
  const remainingSeconds = safeSeconds % 60;
  if (hours > 0) return `${hours} h ${minutes} min ${remainingSeconds} s`;
  if (minutes > 0) return `${minutes} min ${remainingSeconds} s`;
  return `${remainingSeconds} s`;
}

function formatActivityTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("fr-FR", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function activityStageLabel(activity: ProductionActivity): string {
  return activity.stage ? STAGE_LABELS[activity.stage] : "Production";
}

function modelCallDetail(
  activity: ProductionActivity,
  progress: BatchItemDetail["extraction_progress"],
): string {
  let chunks = "";
  if (progress) {
    const currentSource = progress.sources.find(
      (source) => source.source_id === progress.current_source_id,
    );
    if (currentSource?.chunks_total && currentSource.chunks_total > 0) {
      const currentChunk =
        progress.current_chunk_index ?? (currentSource.chunks_done ?? 0) + 1;
      chunks = ` — tranche ${currentChunk}/${currentSource.chunks_total}${
        currentSource.chunks_total_is_estimate ? " (estimée)" : ""
      }`;
    }
  }
  return `Activité: ${activityStageLabel(activity)} — appel modèle en cours depuis ${formatElapsed(activity.since_seconds)}${chunks}`;
}

function activitySummaryText(
  subject: ProductionSubject,
  activity: ProductionActivity,
): string {
  if (activity.kind === "model_call") {
    return `${subject.title} — ${activityStageLabel(activity)}, appel modèle en cours depuis ${formatElapsed(activity.since_seconds)}`;
  }
  if (activity.kind === "reconciliation_probe") {
    return `${subject.title} — Sondage de réconciliation programmé (aucune requête modèle)`;
  }
  return `${subject.title} — Nouvel essai programmé à ${formatActivityTime(activity.started_at)}`;
}

function SystemActivitySummary({
  subjects,
}: {
  subjects: ProductionSubject[];
}) {
  const active = subjects.filter(
    (subject) =>
      subject.activity?.kind === "model_call" ||
      subject.activity?.kind === "reconciliation_probe" ||
      subject.activity?.kind === "retry_scheduled",
  );
  if (active.length === 0) return null;
  return (
    <aside
      className="production-system-activity"
      aria-labelledby="production-system-activity-heading"
    >
      <h3 id="production-system-activity-heading">Activité du système</h3>
      <ul>
        {active.map((subject) => (
          <li key={subject.subject_id}>
            {activitySummaryText(subject, subject.activity!)}
          </li>
        ))}
      </ul>
    </aside>
  );
}

function ActivityDetails({ item }: { item: BatchItemDetail }) {
  const activity = item.activity;
  if (!activity) return null;
  if (activity.kind === "model_call") {
    return (
      <p className="production-item__activity" role="status">
        {modelCallDetail(activity, item.extraction_progress)}
      </p>
    );
  }
  if (activity.kind === "deterministic_stage") {
    return (
      <p className="production-item__activity" role="status">
        Activité: {activityStageLabel(activity)} — étape en cours depuis{" "}
        {formatElapsed(activity.since_seconds)}
      </p>
    );
  }
  return null;
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

function processedCount(
  batch: ProductionBatch | ProductionBatchSummary,
): number {
  // Cancellation is a separate outcome, not produced work.  It must not
  // make a stopped batch look fully processed.
  return batch.completed + batch.needs_review + batch.failed;
}

function BatchCounters({
  batch,
}: {
  batch: ProductionBatch | ProductionBatchSummary;
}) {
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

function ReconciliationFlow({
  item,
  onRecovered,
  readOnly,
}: {
  item: BatchItemDetail;
  onRecovered: () => void;
  readOnly: boolean;
}) {
  const reconciliation = item.reconciliation;
  if (
    readOnly ||
    item.status !== "needs_review" ||
    item.error_code !== "model_submission_reconciliation_required" ||
    !reconciliation
  ) {
    return null;
  }
  return (
    <ReconciliationPanel
      runId={item.run_id}
      reconciliation={reconciliation}
      onRecovered={onRecovered}
    />
  );
}

function ProductionItem({
  item,
  total,
  dispatchPending,
  onRecovered,
  readOnly,
}: {
  item: BatchItemDetail;
  total: number;
  dispatchPending: boolean;
  onRecovered: () => void;
  readOnly: boolean;
}) {
  const isPaused = item.paused === true;
  const isActive = item.status === "running" && !dispatchPending && !isPaused;
  const isWaitingForDispatch =
    item.status === "running" && dispatchPending && !isPaused;
  const statusLabel =
    item.activity?.kind === "reconciliation_probe"
      ? "Sondage de réconciliation programmé (aucune requête modèle)"
      : item.activity?.kind === "retry_scheduled"
        ? `Nouvel essai programmé à ${formatActivityTime(item.activity.started_at)}`
        : isWaitingForDispatch
          ? "En attente du prochain article"
          : STATUS_LABELS[item.status];
  return (
    <li
      className={`production-item production-item--${item.status}${
        isPaused ? " production-item--paused" : ""
      }`}
    >
      <div className="production-item__main">
        <span className="production-item__position">
          {item.position}/{total}
        </span>
        <Link to={`/subjects/${item.subject_id}`}>{item.title}</Link>
        <span
          className={`production-item__status is-${item.status}${
            isWaitingForDispatch ? " is-dispatch-pending" : ""
          }`}
        >
          {isPaused && item.status === "running"
            ? "En pause"
            : isPaused && item.status === "queued"
              ? "En attente (lot en pause)"
              : statusLabel}
        </span>
      </div>
      <div className="production-item__details">
        {isPaused && item.status === "running" ? (
          <span>
            En pause à l’étape :{" "}
            {STAGE_LABELS[item.paused_stage ?? item.current_stage]}
          </span>
        ) : isPaused && item.status === "queued" ? (
          <span>En attente de la reprise du lot</span>
        ) : isActive ? (
          <span>Étape : {STAGE_LABELS[item.current_stage]}</span>
        ) : isWaitingForDispatch ? (
          <span>En attente du démarrage</span>
        ) : null}
        {item.auto_recovery_count > 0 ? (
          <span>
            {item.auto_recovery_count} récupération automatique
            {item.auto_recovery_count > 1 ? "s" : ""}
          </span>
        ) : null}
      </div>
      <ActivityDetails item={item} />
      {isActive &&
      item.current_stage === "extraction" &&
      item.extraction_progress ? (
        <ExtractionProgressView progress={item.extraction_progress} />
      ) : null}
      <ItemError item={item} />
      <ReconciliationFlow
        item={item}
        onRecovered={onRecovered}
        readOnly={readOnly}
      />
    </li>
  );
}

export function ProductionConsole({
  editionId,
  readOnly = false,
}: {
  editionId: string;
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const editionInvalidated = useRef(false);
  const board = useQuery({
    queryKey: ["production-board", editionId],
    queryFn: () => getEditionProduction(editionId),
    refetchInterval: (query) => {
      const data = query.state.data;
      const hasLiveActivity = data?.subjects.some(
        (subject) =>
          subject.activity?.kind === "model_call" ||
          subject.activity?.kind === "reconciliation_probe" ||
          subject.activity?.kind === "retry_scheduled",
      );
      if (hasLiveActivity) return productionBatchPollingInterval("running");
      const currentBatch = data?.active_batch ?? data?.recent_batches[0];
      const status = currentBatch?.status;
      return productionBatchPollingInterval(status);
    },
  });
  const currentBatch =
    board.data?.active_batch ?? board.data?.recent_batches[0] ?? null;
  const invalidateProductionViews = () => {
    void queryClient.invalidateQueries({
      queryKey: ["production-board", editionId],
    });
    void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
    void queryClient.invalidateQueries({
      queryKey: ["edition-review", editionId],
    });
  };
  const cancel = useMutation({
    mutationFn: () =>
      currentBatch
        ? cancelProductionBatch(editionId, currentBatch.batch_id)
        : Promise.reject(new Error("Aucun lot de production actif.")),
    retry: false,
    onSuccess: invalidateProductionViews,
  });
  const pause = useMutation({
    mutationFn: () =>
      currentBatch
        ? pauseProductionBatch(editionId, currentBatch.batch_id)
        : Promise.reject(new Error("Aucun lot de production actif.")),
    retry: false,
    onSuccess: invalidateProductionViews,
  });
  const resume = useMutation({
    mutationFn: () =>
      currentBatch
        ? resumeProductionBatch(editionId, currentBatch.batch_id)
        : Promise.reject(new Error("Aucun lot de production en pause.")),
    retry: false,
    onSuccess: invalidateProductionViews,
  });

  useEffect(() => {
    if (
      currentBatch &&
      TERMINAL_BATCH_STATUSES.has(currentBatch.status) &&
      !editionInvalidated.current
    ) {
      editionInvalidated.current = true;
      void queryClient.invalidateQueries({ queryKey: ["edition", editionId] });
    }
  }, [currentBatch, editionId, queryClient]);

  const countdown = useDispatchCountdown(
    currentBatch?.status === "paused"
      ? null
      : (currentBatch?.next_dispatch_at ?? null),
  );

  if (board.isPending) return <p role="status">Chargement de la production…</p>;
  if (board.isError) {
    return (
      <p className="error-message" role="alert">
        La supervision de production est inaccessible : {String(board.error)}
      </p>
    );
  }
  if (!currentBatch) {
    return (
      <section className="production-panel">
        <SystemActivitySummary subjects={board.data.subjects} />
        <p className="empty-state">
          {readOnly
            ? "L’historique de production n’est plus disponible pour cette édition."
            : "Aucun lot de production n’est disponible."}
        </p>
      </section>
    );
  }

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
      <SystemActivitySummary subjects={board.data.subjects} />
      <div className="production-panel__heading">
        <div>
          <p className="eyebrow">Production</p>
          <h2 id="production-console-heading">
            {processed} / {currentBatch.items} sujets traités
          </h2>
        </div>
        <div className="production-phase" data-phase={currentBatch.phase}>
          <span>Phase du lot</span>
          <strong>{PHASE_LABELS[currentBatch.phase]}</strong>
        </div>
        {!readOnly && currentBatch.status === "running" ? (
          <button
            className="button"
            type="button"
            disabled={pause.isPending}
            onClick={() => pause.mutate()}
          >
            {pause.isPending ? "Mise en pause…" : "Mettre en pause"}
          </button>
        ) : null}
        {!readOnly && currentBatch.status === "paused" ? (
          <button
            className="button"
            type="button"
            disabled={resume.isPending}
            onClick={() => resume.mutate()}
          >
            {resume.isPending ? "Reprise…" : "Reprendre"}
          </button>
        ) : null}
        {!readOnly &&
        (currentBatch.status === "queued" ||
          currentBatch.status === "running" ||
          currentBatch.status === "paused") ? (
          <button
            className="button button--danger"
            type="button"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate()}
          >
            {cancel.isPending ? "Annulation…" : "Annuler le lot de production"}
          </button>
        ) : null}
      </div>
      {currentBatch.reactivated_by_retry ? (
        <p className="production-recovery-note" role="status">
          Ce lot a été réactivé par un nouvel essai manuel. Les articles déjà
          terminés restent comptabilisés.
        </p>
      ) : null}
      {cancel.error ? (
        <p className="error-message" role="alert">
          {cancel.error instanceof Error
            ? cancel.error.message
            : "Le lot n’a pas pu être arrêté."}
        </p>
      ) : null}
      {pause.error || resume.error ? (
        <p className="error-message" role="alert">
          {pause.error instanceof Error
            ? pause.error.message
            : resume.error instanceof Error
              ? resume.error.message
              : "Le contrôle du lot de production a échoué."}
        </p>
      ) : null}
      {currentBatch.status === "paused" ? (
        <p className="production-recovery-note" role="status">
          En pause. Le prochain article et les étapes restantes reprendront dans
          leur ordre prévu.
        </p>
      ) : null}
      {currentBatch.phase === "recovery" ? (
        <p className="production-recovery-note" role="status">
          Une récupération automatique est en cours. La production reprend son
          cours sans intervention.
        </p>
      ) : null}
      {countdown !== null ? (
        <p className="production-next-dispatch">
          Démarrage du prochain article dans {formatCountdown(countdown)}
        </p>
      ) : null}
      <progress max={100} value={progress}>
        {progress} %
      </progress>
      <BatchCounters batch={currentBatch} />
      {readOnly ? (
        <p className="workflow-read-only-note" role="note">
          Historique de production : aucune action ne peut modifier cette
          édition.
        </p>
      ) : null}
      {currentBatch.item_details && currentBatch.item_details.length > 0 ? (
        <ol className="production-item-list" aria-label="Suivi des articles">
          {currentBatch.item_details.map((item) => (
            <ProductionItem
              key={item.run_id}
              item={item}
              total={currentBatch.items}
              dispatchPending={countdown !== null}
              readOnly={readOnly}
              onRecovered={() => {
                void queryClient.invalidateQueries({
                  queryKey: ["production-board", editionId],
                });
              }}
            />
          ))}
        </ol>
      ) : null}
    </section>
  );
}
