/**
 * Subject Production Component
 * Displays production status and controls for a subject
 */

import { useQuery, useMutation } from "@tanstack/react-query";
import {
  getSubjectProduction,
  retryReferences,
  retrySynthesis,
  cancelSubjectProduction,
  startSubjectProduction,
} from "../api/production";
import type { StageStatus } from "../api/production";
import { ProductionStageCard } from "./ProductionStageCard";
import { BriefDraftEditor } from "./BriefDraftEditor";

interface SubjectProductionProps {
  subjectId: string;
  onClose?: () => void;
}

const STATUS_LABELS: Record<string, string> = {
  queued: "en attente",
  running: "en cours",
  ready: "prête",
  needs_review: "à vérifier",
  failed: "en échec",
  cancelled: "annulée",
};

function stageDetail(
  stage: string,
  entry: StageStatus | undefined,
): string | undefined {
  if (!entry) return undefined;
  if (stage === "sources" && entry.archived_sources !== undefined) {
    return `${entry.archived_sources} archivée(s)`;
  }
  return undefined;
}

export function SubjectProduction({
  subjectId,
  onClose,
}: SubjectProductionProps) {
  // Fetch production status
  const {
    data: status,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ["production", subjectId],
    queryFn: () => getSubjectProduction(subjectId),
    // Only poll while a run is actually in flight; a finished or absent run
    // has nothing left to watch.
    refetchInterval: (query) => {
      const runStatus = query.state.data?.status;
      return runStatus === "queued" || runStatus === "running" ? 2000 : false;
    },
  });

  // Mutations
  const retryReferencesMutation = useMutation({
    mutationFn: () => retryReferences(subjectId),
    onSuccess: () => refetch(),
  });

  const retrySynthesisMutation = useMutation({
    mutationFn: () => retrySynthesis(subjectId),
    onSuccess: () => refetch(),
  });

  const cancelMutation = useMutation({
    mutationFn: () => cancelSubjectProduction(subjectId),
    onSuccess: () => {
      void refetch();
      setTimeout(() => onClose?.(), 1000);
    },
  });

  const startMutation = useMutation({
    mutationFn: () => startSubjectProduction(subjectId),
    onSuccess: () => void refetch(),
  });

  if (isLoading)
    return <p role="status">Chargement de l’état de production…</p>;
  if (error)
    return (
      <p className="error-message" role="alert">
        L’état de production est inaccessible : {String(error)}
      </p>
    );

  // No run yet, or the last one ended without a brief: either way the only
  // way forward is to (re)start production, so offer it.
  const restartable =
    !status || status.status === "cancelled" || status.status === "failed";
  const previousError = status?.stages?.[status.current_stage]?.error_code;

  if (restartable) {
    return (
      <section className="production-panel">
        <h2>Production de la brève</h2>
        {status ? (
          <p className="production-counters">
            La production précédente s’est terminée en{" "}
            <strong>{STATUS_LABELS[status.status] ?? status.status}</strong>
            {previousError ? ` (${previousError})` : ""}.
          </p>
        ) : null}
        <p>
          Les sources seront collectées, puis ChatGPT effectuera la recherche
          des références, l’extraction CTI et la synthèse.
        </p>
        {startMutation.error ? (
          <p className="error-message" role="alert">
            {String(startMutation.error)}
          </p>
        ) : null}
        <button
          className="button"
          disabled={startMutation.isPending}
          onClick={() => startMutation.mutate()}
        >
          {startMutation.isPending
            ? "Démarrage…"
            : status
              ? "Relancer la production"
              : "Produire cette brève"}
        </button>
      </section>
    );
  }

  const stageList = [
    "sources",
    "references",
    "extraction",
    "synthesis",
    "assembly",
  ];
  // Warnings are recoveries the parser made: worth showing, never blocking.
  const warnings = status.warnings ?? [];

  // Tolerate a response without per-stage detail rather than crashing the page.
  const stages: Partial<Record<string, StageStatus>> = status.stages ?? {};
  const completedStages = stageList.filter(
    (stage) => stages[stage]?.status === "succeeded",
  ).length;

  return (
    <section className="production-panel">
      <div className="production-panel__heading">
        <div>
          <p className="eyebrow">{status.editorial_type}</p>
          <h2>{status.title}</h2>
        </div>
        <span className={`badge production-status is-${status.status}`}>
          {STATUS_LABELS[status.status] ?? status.status}
        </span>
      </div>

      <p className="production-counters">
        Progression : <strong>{completedStages}</strong> / {stageList.length}{" "}
        étapes
      </p>
      <progress max={stageList.length} value={completedStages}>
        {completedStages}/{stageList.length}
      </progress>

      <ol className="production-stage-list">
        {stageList.map((stage, i) => (
          <ProductionStageCard
            key={stage}
            stage={stage}
            status={stages[stage]?.status || "pending"}
            stageNumber={i + 1}
            isActive={status.current_stage === stage}
            detail={stageDetail(stage, stages[stage])}
          />
        ))}
      </ol>

      <nav className="production-links" aria-label="Détail des étapes">
        <a href={`/subjects/${subjectId}#sources`}>Voir les sources</a>
        <a href={`/subjects/${subjectId}/production/artifacts/references`}>
          Voir les références
        </a>
        <a href={`/subjects/${subjectId}/production/artifacts/extraction`}>
          Voir l’extraction
        </a>
        <a href={`/subjects/${subjectId}/production/artifacts/synthesis`}>
          Voir la synthèse
        </a>
        {status.conversation_id ? (
          <a href={`/subjects/${subjectId}#conversations`}>
            Voir la conversation
          </a>
        ) : null}
        {status.status === "ready" ? (
          <a href={`/subjects/${subjectId}/production/artifacts/brief`}>
            Aperçu
          </a>
        ) : null}
      </nav>

      {warnings.length > 0 && (
        <div className="verification-warning" role="note">
          <strong>Avertissements de lecture</strong>
          <ul>
            {warnings.map((warning: string) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {(status.status === "needs_review" || status.status === "failed") && (
        <p className="error-message" role="alert">
          {status.status === "needs_review" ? "Revue nécessaire" : "Échec"} —{" "}
          {stages[status.current_stage]?.error_code ?? "inconnu"} :{" "}
          {stages[status.current_stage]?.error_message ?? ""}
        </p>
      )}

      {status.status === "ready" && (
        <BriefDraftEditor
          subjectId={subjectId}
          isAvailable={true}
          onClose={undefined}
        />
      )}

      <div className="production-actions">
        {status.status === "ready" && (
          <>
            <button
              className="button"
              onClick={() => retryReferencesMutation.mutate()}
              disabled={retryReferencesMutation.isPending}
            >
              {retryReferencesMutation.isPending
                ? "Relance…"
                : "Relancer les références"}
            </button>
            <button
              className="button button--secondary"
              onClick={() => retrySynthesisMutation.mutate()}
              disabled={retrySynthesisMutation.isPending}
            >
              {retrySynthesisMutation.isPending
                ? "Relance…"
                : "Relancer la synthèse"}
            </button>
          </>
        )}

        {status.status === "running" && (
          <button
            className="button button--danger"
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
          >
            {cancelMutation.isPending ? "Annulation…" : "Annuler"}
          </button>
        )}

        {onClose && (
          <button className="button button--secondary" onClick={onClose}>
            Fermer
          </button>
        )}
      </div>

      <div className="production-meta">
        <p>Identifiant du run : {status.run_id}</p>
        <p>Créé : {new Date(status.created_at).toLocaleString()}</p>
        {status.started_at && (
          <p>Démarré : {new Date(status.started_at).toLocaleString()}</p>
        )}
        {status.finished_at && (
          <p>Terminé : {new Date(status.finished_at).toLocaleString()}</p>
        )}
      </div>
    </section>
  );
}
