/**
 * Subject Production Component
 * Displays production status and controls for a subject
 */

import React, { useEffect, useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import {
  getSubjectProduction,
  retryReferences,
  retrySynthesis,
  cancelSubjectProduction,
  ProductionStatus,
} from "@/api/production";
import { ProductionStageCard } from "./ProductionStageCard";

interface SubjectProductionProps {
  subjectId: string;
  onClose?: () => void;
}

export function SubjectProduction({
  subjectId,
  onClose,
}: SubjectProductionProps) {
  const [autoRefresh, setAutoRefresh] = useState(true);

  // Fetch production status
  const { data: status, isLoading, error, refetch } = useQuery({
    queryKey: ["production", subjectId],
    queryFn: () => getSubjectProduction(subjectId),
    refetchInterval: autoRefresh && status?.status === "running" ? 2000 : false,
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
      refetch();
      setTimeout(() => onClose?.(), 1000);
    },
  });

  // Auto-stop refresh when complete
  useEffect(() => {
    if (status?.status === "ready" || status?.status === "needs_review" || status?.status === "failed") {
      setAutoRefresh(false);
    }
  }, [status?.status]);

  if (isLoading) return <div className="p-4">Loading production status...</div>;
  if (error) return <div className="p-4 text-red-600">Error: {String(error)}</div>;
  if (!status) return <div className="p-4">No production data</div>;

  const stageList = [
    "sources",
    "references",
    "extraction",
    "synthesis",
    "assembly",
  ];
  const completedStages = stageList.filter(
    (stage) => status.stages[stage]?.status === "verified"
  ).length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="border-b pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold">{status.title}</h1>
            <p className="text-sm text-gray-600">
              Type: {status.editorial_type}
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
          <span className="font-semibold">Progress</span>
          <span>{completedStages} / 5 stages</span>
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
            status={status.stages[stage]?.status || "pending"}
            stageNumber={i + 1}
            isActive={status.current_stage === stage}
          />
        ))}
      </div>

      {/* Current Stage Details */}
      {status.stages[status.current_stage] && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h3 className="font-semibold text-blue-900">
            Current: {status.current_stage.toUpperCase()}
          </h3>
          <p className="text-sm text-blue-700 mt-2">
            {status.stages[status.current_stage]?.error_message ||
              "Processing..."}
          </p>
        </div>
      )}

      {/* Error Display */}
      {status.status === "needs_review" && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4">
          <h3 className="font-semibold text-yellow-900">Review Needed</h3>
          <p className="text-sm text-yellow-700 mt-2">
            Code: {status.stages[status.current_stage]?.error_code || "unknown"}
          </p>
          <p className="text-sm text-yellow-700 mt-1">
            {status.stages[status.current_stage]?.error_message}
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
                ? "Retrying..."
                : "Retry References"}
            </button>
            <button
              onClick={() => retrySynthesisMutation.mutate()}
              disabled={retrySynthesisMutation.isPending}
              className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {retrySynthesisMutation.isPending
                ? "Retrying..."
                : "Retry Synthesis"}
            </button>
          </>
        )}

        {status.status === "running" && (
          <button
            onClick={() => cancelMutation.mutate()}
            disabled={cancelMutation.isPending}
            className="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
          >
            {cancelMutation.isPending ? "Cancelling..." : "Cancel"}
          </button>
        )}

        {onClose && (
          <button
            onClick={onClose}
            className="px-4 py-2 bg-gray-300 text-gray-800 rounded hover:bg-gray-400 ml-auto"
          >
            Close
          </button>
        )}
      </div>

      {/* Metadata */}
      <div className="text-xs text-gray-500 border-t pt-4">
        <p>Run ID: {status.run_id}</p>
        <p>Created: {new Date(status.created_at).toLocaleString()}</p>
        {status.started_at && (
          <p>Started: {new Date(status.started_at).toLocaleString()}</p>
        )}
        {status.finished_at && (
          <p>Finished: {new Date(status.finished_at).toLocaleString()}</p>
        )}
      </div>
    </div>
  );
}
