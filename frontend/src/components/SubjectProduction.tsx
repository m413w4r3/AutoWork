import { useQuery, useMutation } from "@tanstack/react-query";
import {
  getSubjectProduction,
  retryProductionStage,
  startSubjectProduction,
  shouldPollProduction,
} from "../api/production";
import { cancelProductionRun } from "../api/publication";
import type { StageStatus } from "../api/production";
import { ReconciliationPanel } from "../features/edition-workflow/ReconciliationPanel";
import { ExtractionProgressView } from "./ExtractionProgress";
import { ProductionStageCard } from "./ProductionStageCard";
import { ProductionStateTransfer } from "./ProductionStateTransfer";

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
  extraction: "Extraction",
  synthesis: "Synthèse",
  assembly: "Assemblage",
};

const RETRY_STAGES = [
  "sources",
  "references",
  "extraction",
  "synthesis",
  "assembly",
] as const;
type RetryStage = (typeof RETRY_STAGES)[number];

const RETRY_DESCRIPTIONS: Record<RetryStage, string> = {
  sources: "Les sources et toutes les étapes suivantes seront recalculées.",
  references:
    "Les références et toutes les étapes suivantes seront recalculées. Les sources existantes seront conservées.",
  extraction:
    "L’extraction et toutes les étapes suivantes seront recalculées. Les références existantes seront conservées.",
  synthesis:
    "La synthèse et toutes les étapes suivantes seront recalculées. Les références existantes seront conservées.",
  assembly:
    "L’assemblage sera recalculé. Les références existantes seront conservées.",
};

const CONVERSATION_ERROR_CODES = new Set([
  "bridge_server_error",
  "bridge_timeout",
  "bridge_ui_timeout",
  "bridge_idle_timeout",
  "bridge_total_timeout",
  "conversation_unavailable",
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
  const artifactStage = stage === "assembly" ? "publication" : stage;
  return `/subjects/${subjectId}/production/artifacts/${artifactStage}`;
}

function issueCopy(status: string, errorCode: string | null): string {
  if (errorCode === "imported_production_state") {
    return "État restauré — références, extraction et synthèse sont disponibles. L’assemblage n’a pas été rejoué.";
  }
  if (errorCode === "q2_source_coverage_failed") {
    return "Couverture des sources incomplète — certaines sources n’ont pas été analysées.";
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

  const retryStageMutation = useMutation({
    mutationFn: (stage: RetryStage) => retryProductionStage(subjectId, stage),
    onSuccess: () => refetch(),
  });

  const cancelMutation = useMutation({
    mutationFn: (runId: string) => cancelProductionRun(runId),
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
  // A run owned by an edition batch is repaired through that batch. Starting a
  // standalone run here would create an article the batch never sees, so the
  // backend refuses it and the page must not offer it either.
  const batchOwned = Boolean(status?.batch_id);

  if (restartable) {
    return (
      <section className="production-panel">
        <h2>Production de l’article</h2>
        {status ? (
          <p className="production-counters">
            La production précédente s’est terminée en{" "}
            <strong>{STATUS_LABELS[status.status] ?? status.status}</strong>.
          </p>
        ) : null}
        <p>
          AutoWork collecte et archive les sources ; ChatGPT établit les
          références et analyse chaque source technique ; AutoWork normalise et
          valide les artefacts avant la synthèse.
        </p>
        {startMutation.error ? (
          <p className="error-message" role="alert">
            {String(startMutation.error)}
          </p>
        ) : null}
        {batchOwned ? (
          <p role="note">
            Cet article appartient à une production d’édition. Une nouvelle
            production isolée ne réparerait pas l’article annulé du lot :
            reprenez-le depuis la revue de l’édition.
          </p>
        ) : (
          <button
            className="button"
            disabled={startMutation.isPending}
            onClick={() => startMutation.mutate()}
          >
            {startMutation.isPending
              ? "Démarrage…"
              : status
                ? "Relancer la production"
                : "Produire cet article"}
          </button>
        )}
        <ProductionStateTransfer
          subjectId={subjectId}
          productionStatus={status ?? null}
        />
      </section>
    );
  }

  const stageList = [...RETRY_STAGES];
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
  const issueStageIndex = RETRY_STAGES.indexOf(status.current_stage);
  const reconciliationRequired =
    status.status === "needs_review" &&
    status.error_code === "model_submission_reconciliation_required" &&
    status.reconciliation !== null &&
    status.reconciliation !== undefined;
  // An unresolved provider submission owns the only recovery gesture: the
  // backend refuses every retry until the exact answer is adopted, so no
  // generic retry control — current stage or earlier — is offered here.
  const retryStages: readonly RetryStage[] = reconciliationRequired
    ? []
    : status.status === "ready"
      ? RETRY_STAGES
      : showIssue && issueStageIndex >= 0
        ? RETRY_STAGES.slice(0, issueStageIndex + 1)
        : [];
  const issueRetryStage = showIssue ? status.current_stage : null;
  const currentStageRetryRecommended = status.recovery_disposition === "auto";

  return (
    <section className="production-panel">
      <div className="production-panel__heading">
        <div>
          <p className="eyebrow">Pipeline de production</p>
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

      {status.current_stage === "extraction" && status.extraction_progress ? (
        <ExtractionProgressView progress={status.extraction_progress} />
      ) : null}

      <ol className="production-stage-list">
        {stageList.map((stage, i) => (
          <ProductionStageCard
            key={stage}
            stage={stage}
            status={stages[stage]?.status || "pending"}
            stageNumber={i + 1}
            isActive={status.current_stage === stage}
            reused={stages[stage]?.reused}
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
          <a href={`/subjects/${subjectId}/production/artifacts/publication`}>
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
          {issueStage?.error_message ? <p>{issueStage.error_message}</p> : null}
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

      {reconciliationRequired && status.reconciliation ? (
        <ReconciliationPanel
          runId={status.run_id}
          reconciliation={status.reconciliation}
          onRecovered={() => void refetch()}
        />
      ) : null}

      {!reconciliationRequired &&
        showIssue &&
        currentStageRetryRecommended &&
        issueRetryStage &&
        retryStages.includes(issueRetryStage) && (
          <div className="production-retry-primary">
            <p>
              <strong>Relancer depuis {STAGE_LABELS[issueRetryStage]}</strong>
              <br />
              {RETRY_DESCRIPTIONS[issueRetryStage]}
            </p>
            <button
              className="button"
              onClick={() => retryStageMutation.mutate(issueRetryStage)}
              disabled={retryStageMutation.isPending}
            >
              {retryStageMutation.isPending
                ? "Relance…"
                : "Relancer cette étape"}
            </button>
          </div>
        )}

      {retryStageMutation.error ? (
        <p className="error-message" role="alert">
          La relance n’a pas pu démarrer : {String(retryStageMutation.error)}
        </p>
      ) : null}

      <div className="production-actions">
        {!reconciliationRequired && status.status === "ready" && (
          <label>
            Relancer depuis…
            <select
              aria-label="Relancer depuis une étape"
              defaultValue=""
              onChange={(event) => {
                const stage = event.target.value as RetryStage;
                if (stage) retryStageMutation.mutate(stage);
              }}
              disabled={retryStageMutation.isPending}
            >
              <option value="">Choisir une étape</option>
              {retryStages.map((stage) => (
                <option key={stage} value={stage}>
                  {STAGE_LABELS[stage]}
                </option>
              ))}
            </select>
          </label>
        )}
        {!reconciliationRequired && showIssue && retryStages.length > 1 && (
          <details>
            <summary>Relancer depuis une étape précédente</summary>
            <div>
              {retryStages
                .filter((stage) => stage !== issueRetryStage)
                .map((stage) => (
                  <button
                    key={stage}
                    className="button button--secondary"
                    onClick={() => retryStageMutation.mutate(stage)}
                    disabled={retryStageMutation.isPending}
                  >
                    Relancer depuis {STAGE_LABELS[stage]}
                  </button>
                ))}
            </div>
          </details>
        )}

        {status.status === "running" && (
          <button
            className="button button--danger"
            onClick={() => cancelMutation.mutate(status.run_id)}
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

      <ProductionStateTransfer
        subjectId={subjectId}
        productionStatus={status}
      />

      <details className="production-diagnostics">
        <summary>Diagnostic</summary>
        <div className="production-meta">
          <p>Identifiant du run : {status.run_id}</p>
          <p>Créé : {new Date(status.created_at).toLocaleString()}</p>
          {status.started_at && (
            <p>Démarré : {new Date(status.started_at).toLocaleString()}</p>
          )}
          {status.finished_at && (
            <p>Terminé : {new Date(status.finished_at).toLocaleString()}</p>
          )}
          <p>Tentative pipeline : {status.pipeline_generation}</p>
          {stageList
            .filter((stage) => stages[stage]?.reused)
            .map((stage) => {
              const entry = stages[stage];
              return (
                <p key={stage}>
                  {STAGE_LABELS[stage]} : réutilisée depuis un calcul précédent
                  {entry?.reused_from_artifact_id
                    ? ` (${entry.reused_from_artifact_id})`
                    : ""}
                  {entry?.reused_from_created_at
                    ? ` · calcul original : ${new Date(entry.reused_from_created_at).toLocaleString()}`
                    : ""}
                  {entry?.research_date
                    ? ` · research_date : ${entry.research_date}`
                    : ""}
                </p>
              );
            })}
        </div>
      </details>
    </section>
  );
}
