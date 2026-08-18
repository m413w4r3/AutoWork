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

interface SubjectProductionProps {
  subjectId: string;
  onClose?: () => void;
}

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
    return <div className="p-4">Chargement de l’état de production…</div>;
  if (error)
    return (
      <div className="p-4 text-red-600" role="alert">
        L’état de production est inaccessible : {String(error)}
      </div>
    );

  // No run yet: this is the entry point, not an error.
  if (!status) {
    return (
      <section className="production-start space-y-3">
        <h2 className="text-xl font-bold">Production de la brève</h2>
        <p className="text-sm text-gray-600">
          Les sources seront collectées, puis ChatGPT effectuera la recherche
          des références, l’extraction CTI et la synthèse.
        </p>
        {startMutation.error ? (
          <p className="text-red-600 text-sm" role="alert">
            {String(startMutation.error)}
          </p>
        ) : null}
        <button
          className="button px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          disabled={startMutation.isPending}
          onClick={() => startMutation.mutate()}
        >
          {startMutation.isPending ? "Démarrage…" : "Produire cette brève"}
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
    <div className="space-y-6">
      {/* Header */}
      <div className="border-b pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{status.title}</h1>
            <p className="text-sm text-gray-600">
              Type : {status.editorial_type}
            </p>
          </div>
          <div className="text-right">
            <div
              className={`inline-block px-3 py-1 rounded text-sm font-semibold ${
                status.status === "ready"
                  ? "bg-green-100 text-green-800"
                  : status.status === "needs_review"
                    ? "bg-yellow-100 text-yellow-800"
                    : status.status === "failed"
                      ? "bg-red-100 text-red-800"
                      : "bg-blue-100 text-blue-800"
              }`}
            >
              {status.status.toUpperCase()}
            </div>
          </div>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="space-y-2">
        <div className="flex justify-between text-sm">
          <span className="font-semibold">Progression</span>
          <span>{completedStages} / 5 étapes</span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-2">
          <div
            className="bg-blue-600 h-2 rounded-full transition-all"
            style={{ width: `${(completedStages / 5) * 100}%` }}
          />
        </div>
      </div>

      {/* Stage Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-4">
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
      </div>

      {/* Deep links into what each stage actually produced */}
      <nav
        className="flex gap-3 flex-wrap text-sm"
        aria-label="Détail des étapes"
      >
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

      {/* Current Stage Details */}
      {stages[status.current_stage] && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h3 className="font-semibold text-blue-900">
            En cours : {status.current_stage.toUpperCase()}
          </h3>
          <p className="text-sm text-blue-700 mt-2">
            {stages[status.current_stage]?.error_message ||
              "Traitement en cours…"}
          </p>
        </div>
      )}

      {warnings.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
          <h3 className="font-semibold text-amber-900">
            Avertissements de lecture
          </h3>
          <ul className="text-sm text-amber-800 mt-2 list-disc pl-5">
            {warnings.map((warning: string) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Error Display */}
      {status.status === "needs_review" && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
          <h3 className="font-semibold text-yellow-900">Revue nécessaire</h3>
          <p className="text-sm text-yellow-700 mt-2">
            Code : {stages[status.current_stage]?.error_code || "inconnu"}
          </p>
          <p className="text-sm text-yellow-700 mt-1">
            {stages[status.current_stage]?.error_message}
          </p>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-2 flex-wrap">
        {status.status === "ready" && (
          <>
            <button
              onClick={() => retryReferencesMutation.mutate()}
              disabled={retryReferencesMutation.isPending}
              className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {retryReferencesMutation.isPending
                ? "Relance…"
                : "Relancer les références"}
            </button>
            <button
              onClick={() => retrySynthesisMutation.mutate()}
              disabled={retrySynthesisMutation.isPending}
              className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {retrySynthesisMutation.isPending
                ? "Relance…"
                : "Relancer la synthèse"}
            </button>
          </>
        )}

        {status.status === "running" && (
          <button
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
            className="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {cancelMutation.isPending ? "Annulation…" : "Annuler"}
          </button>
        )}

        {onClose && (
          <button
            onClick={onClose}
            className="px-4 py-2 bg-gray-300 text-gray-800 rounded hover:bg-gray-400 ml-auto"
          >
            Fermer
          </button>
        )}
      </div>

      {/* Metadata */}
      <div className="text-xs text-gray-500 border-t pt-4">
        <p>Identifiant du run : {status.run_id}</p>
        <p>Créé : {new Date(status.created_at).toLocaleString()}</p>
        {status.started_at && (
          <p>Démarré : {new Date(status.started_at).toLocaleString()}</p>
        )}
        {status.finished_at && (
          <p>Terminé : {new Date(status.finished_at).toLocaleString()}</p>
        )}
      </div>
    </div>
  );
}
