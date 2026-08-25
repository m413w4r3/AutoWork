import { useQuery, useMutation } from "@tanstack/react-query";
import {
  getSubjectProduction,
  retryReferences,
  retrySynthesis,
  cancelSubjectProduction,
  startSubjectProduction,
  shouldPollProduction,
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

const STAGE_LABELS: Record<string, string> = {
  sources: "Sources",
  references: "Références",
  extraction: "Extraction CTI",
  synthesis: "Synthèse",
  assembly: "Brève",
};

const CONVERSATION_ERROR_CODES = new Set([
  "bridge_server_error",
  "bridge_timeout",
  "bridge_ui_timeout",
  "conversation_unavailable",
  "conversation_locator_invalid",
  "conversation_profile_mismatch",
  "conversation_busy",
]);

function stageDetail(
  stage: string,
  entry: StageStatus | undefined,
): string | undefined {
  if (!entry) return undefined;
  if (stage === "sources" && entry.archived_sources !== undefined) {
    return `${entry.archived_sources} archivée(s)`;
  }
  if (stage === "extraction") return entry.detail;
  return undefined;
}

function stageArtifactHref(subjectId: string, stage: string): string {
  return `/subjects/${subjectId}/production/artifacts/${stage}`;
}

function issueCopy(status: string, errorCode: string | null): string {
  if (errorCode === "q2_chunk_coverage_failed") {
    return "Couverture des segments incomplète — certains éléments n’ont pas été vérifiés.";
  }
  if (errorCode === "model_needs_review") {
    return "Le modèle demande une revue avant poursuite.";
  }
  if (errorCode === "synthesis_validation_failed") {
    return "Validation de synthèse échouée — consultez les détails de l’étape.";
  }
  if (errorCode && CONVERSATION_ERROR_CODES.has(errorCode)) {
    return "Intervention requise — la conversation ChatGPT n’a pas pu être finalisée.";
  }
  return status === "needs_review"
    ? "Intervention requise — vérifiez cette étape avant de poursuivre."
    : "Échec de l’étape — consultez ses détails.";
}

export function SubjectProduction({
  subjectId,
  onClose,
}: SubjectProductionProps) {
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
      return shouldPollProduction(query.state.data?.status) ? 2000 : false;
    },
  });

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

  // A failed run remains visible: its failed stage and recovery path matter.
  const restartable = !status || status.status === "cancelled";

  if (restartable) {
    return (
      <section className="production-panel">
        <h2>Production de la brève</h2>
        {status ? (
          <p className="production-counters">
            La production précédente s’est terminée en{" "}
            <strong>{STATUS_LABELS[status.status] ?? status.status}</strong>.
          </p>
        ) : null}
        <p>
          AutoWork collecte et archive les sources ; ChatGPT établit les
          références ; l’extraction technique structurée est vérifiée
          automatiquement ; ChatGPT rédige ensuite la synthèse.
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
  const issueStage = stages[status.current_stage];
  const issueCode = issueStage?.error_code ?? null;
  const showIssue =
    status.status === "needs_review" || status.status === "failed";
  const issueIsConversation =
    issueCode !== null && CONVERSATION_ERROR_CODES.has(issueCode);
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
        {status.references_conversation_id ? (
          <a href={`/subjects/${subjectId}#conversations`}>Voir la recherche</a>
        ) : null}
        {status.synthesis_conversation_id ? (
          <a href={`/subjects/${subjectId}#conversations`}>Voir la synthèse</a>
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

      {showIssue && (
        <div className="error-message" role="alert">
          <p>
            <strong>
              {STAGE_LABELS[status.current_stage] ?? status.current_stage}
            </strong>
            {" — "}
            {issueCopy(status.status, issueCode)}
          </p>
          <p>Code : {issueCode ?? "inconnu"}</p>
          {issueIsConversation &&
          (status.references_conversation_id ||
            status.synthesis_conversation_id) ? (
            <a href={`/subjects/${subjectId}#conversations`}>
              Voir la conversation
            </a>
          ) : (
            <a href={stageArtifactHref(subjectId, status.current_stage)}>
              Voir les détails de l’étape
            </a>
          )}
        </div>
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
