/**
 * Production Queue Component
 * Shows batch production status for an edition, or the entry point to start it.
 */

import { useState } from "react";
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
}

const ITEM_STATUS_LABELS: Record<string, string> = {
  queued: "En attente",
  running: "En cours",
  ready: "Prête",
  needs_review: "À vérifier",
  failed: "En échec",
  cancelled: "Annulée",
};

function itemStatusColor(status: string): string {
  if (status === "ready") return "text-green-700";
  if (status === "needs_review") return "text-yellow-700";
  if (status === "failed") return "text-red-700";
  if (status === "running") return "text-blue-700";
  return "text-gray-500";
}

export function ProductionQueue({
  editionId,
  briefs = [],
}: ProductionQueueProps) {
  const [selected, setSelected] = useState<string[]>([]);

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
    onSuccess: () => void refetch(),
  });

  const getStatusColor = (status: string) => {
    switch (status) {
      case "completed":
        return "bg-green-100 text-green-800";
      case "completed_with_issues":
        return "bg-yellow-100 text-yellow-800";
      case "running":
        return "bg-blue-100 text-blue-800";
      default:
        return "bg-gray-100 text-gray-800";
    }
  };

  const toggle = (subjectId: string) =>
    setSelected((current) =>
      current.includes(subjectId)
        ? current.filter((id) => id !== subjectId)
        : [...current, subjectId],
    );

  if (isLoading)
    return <div className="p-4">Chargement du lot de production…</div>;
  if (error)
    return (
      <div className="p-4 text-red-600" role="alert">
        Le lot de production est inaccessible : {String(error)}
      </div>
    );

  // No batch yet: this is the entry point, not an error.
  if (!batch) {
    if (briefs.length === 0) {
      return (
        <div className="p-4 border rounded-lg bg-gray-50">
          <p className="text-gray-600">
            Aucune brève sélectionnée n’est prête à être produite.
          </p>
        </div>
      );
    }

    return (
      <section className="production-start space-y-3 p-4 border rounded-lg bg-gray-50">
        <h2 className="text-xl font-bold">
          {briefs.length} brève{briefs.length > 1 ? "s" : ""} prête
          {briefs.length > 1 ? "s" : ""}
        </h2>
        {start.error ? (
          <p className="text-red-600 text-sm" role="alert">
            {String(start.error)}
          </p>
        ) : null}

        <button
          className="button px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          disabled={start.isPending}
          onClick={() => start.mutate(undefined)}
        >
          {start.isPending
            ? "Démarrage…"
            : `Traiter les ${briefs.length} brèves`}
        </button>

        <ul className="space-y-1">
          {briefs.map((brief) => (
            <li key={brief.subjectId}>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={selected.includes(brief.subjectId)}
                  onChange={() => toggle(brief.subjectId)}
                />
                {brief.title}
              </label>
            </li>
          ))}
        </ul>

        <button
          className="button px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
          disabled={start.isPending || selected.length === 0}
          onClick={() => start.mutate(selected)}
        >
          {`Traiter les ${selected.length} sélectionnées`}
        </button>
      </section>
    );
  }

  const totalItems = batch.items;
  const processedItems = batch.completed + batch.needs_review + batch.failed;
  const progressPercent =
    totalItems > 0 ? (processedItems / totalItems) * 100 : 0;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="border-b pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-bold">Production par lot</h2>
            <p className="text-sm text-gray-600">
              {totalItems} brèves{" "}
              {batch.status === "running" ? "en cours" : batch.status}
            </p>
          </div>
          <div
            className={`inline-block px-3 py-1 rounded text-sm font-semibold ${getStatusColor(batch.status)}`}
          >
            {batch.status.replace(/_/g, " ").toUpperCase()}
          </div>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="bg-gray-50 p-3 rounded">
          <div className="text-2xl font-bold text-gray-800">
            {batch.completed}
          </div>
          <div className="text-xs text-gray-600">Terminées</div>
        </div>
        <div className="bg-yellow-50 p-3 rounded">
          <div className="text-2xl font-bold text-yellow-800">
            {batch.needs_review}
          </div>
          <div className="text-xs text-gray-600">À revoir</div>
        </div>
        <div className="bg-red-50 p-3 rounded">
          <div className="text-2xl font-bold text-red-800">{batch.failed}</div>
          <div className="text-xs text-gray-600">
            En échec{batch.cancelled ? ` · ${batch.cancelled} annulée(s)` : ""}
          </div>
        </div>
        <div className="bg-blue-50 p-3 rounded">
          <div className="text-2xl font-bold text-blue-800">
            {batch.current_subject_index !== null
              ? batch.current_subject_index + 1
              : "-"}
          </div>
          <div className="text-xs text-gray-600">Élément courant</div>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="space-y-2">
        <div className="flex justify-between text-sm">
          <span className="font-semibold">Progression globale</span>
          <span>
            {processedItems} / {totalItems} brèves
          </span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-3">
          <div
            className="bg-blue-600 h-3 rounded-full transition-all"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>

      {/* Per-subject queue: which one is running, which are done */}
      {batch.item_details.length > 0 && (
        <ol className="space-y-1" aria-label="File de production">
          {batch.item_details.map((item) => (
            <li
              key={item.run_id}
              className="flex items-center gap-2 text-sm border-b py-1"
            >
              <span className="text-gray-500 tabular-nums">
                {item.position}/{totalItems}
              </span>
              <span className="flex-1">{item.title}</span>
              <span className={`text-xs ${itemStatusColor(item.status)}`}>
                {ITEM_STATUS_LABELS[item.status] ?? item.status}
                {item.status === "running" ? ` · ${item.current_stage}` : ""}
              </span>
            </li>
          ))}
        </ol>
      )}

      {/* Current Subject */}
      {batch.status === "running" && batch.current_subject_index !== null && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h3 className="font-semibold text-blue-900">
            Traitement de l’élément {batch.current_subject_index + 1} sur{" "}
            {totalItems}
          </h3>
          <div className="mt-2 flex space-x-1">
            <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce"></div>
            <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce delay-100"></div>
            <div className="w-2 h-2 bg-blue-500 rounded-full animate-bounce delay-200"></div>
          </div>
        </div>
      )}

      {/* Summary */}
      <div className="text-xs text-gray-500 border-t pt-4">
        <p>Identifiant du lot : {batch.batch_id}</p>
        <p>Identifiant de l’édition : {batch.edition_id}</p>
        <p>Profil : {batch.profile}</p>
        <p>Créé : {new Date(batch.created_at).toLocaleString()}</p>
        {batch.started_at && (
          <p>Démarré : {new Date(batch.started_at).toLocaleString()}</p>
        )}
        {batch.finished_at && (
          <p>Terminé : {new Date(batch.finished_at).toLocaleString()}</p>
        )}
      </div>
    </div>
  );
}
