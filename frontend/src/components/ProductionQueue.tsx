/**
 * Production Queue Component
 * Shows batch production status for an edition
 */

import React, { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  getEditionBriefProduction,
  startEditionBriefProduction,
  BatchStatus,
} from "@/api/production";

interface ProductionQueueProps {
  editionId: string;
}

export function ProductionQueue({ editionId }: ProductionQueueProps) {
  const [autoRefresh, setAutoRefresh] = useState(true);

  const { data: batch, isLoading, error, refetch } = useQuery({
    queryKey: ["batch", editionId],
    queryFn: () => getEditionBriefProduction(editionId),
    refetchInterval: autoRefresh ? 2000 : false,
  });

  // Auto-stop refresh when complete
  useEffect(() => {
    if (batch?.status === "completed" || batch?.status === "completed_with_issues") {
      setAutoRefresh(false);
    }
  }, [batch?.status]);

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

  if (isLoading) return <div className="p-4">Loading batch status...</div>;
  if (error) return <div className="p-4 text-red-600">Error: {String(error)}</div>;
  if (!batch) {
    return (
      <div className="p-4 border rounded-lg bg-gray-50">
        <p className="text-gray-600">No batch found</p>
        <button
          onClick={() => startEditionBriefProduction(editionId).then(() => refetch())}
          className="mt-2 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          Start Production
        </button>
      </div>
    );
  }

  const totalItems = batch.items;
  const processedItems = batch.completed + batch.needs_review + batch.failed;
  const progressPercent = totalItems > 0 ? (processedItems / totalItems) * 100 : 0;

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="border-b pb-4">
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-xl font-bold">Batch Production</h2>
            <p className="text-sm text-gray-600">
              {totalItems} briefs {batch.status === "running" ? "in progress" : batch.status}
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
          <div className="text-2xl font-bold text-gray-800">{batch.completed}</div>
          <div className="text-xs text-gray-600">Completed</div>
        </div>
        <div className="bg-yellow-50 p-3 rounded">
          <div className="text-2xl font-bold text-yellow-800">{batch.needs_review}</div>
          <div className="text-xs text-gray-600">Needs Review</div>
        </div>
        <div className="bg-red-50 p-3 rounded">
          <div className="text-2xl font-bold text-red-800">{batch.failed}</div>
          <div className="text-xs text-gray-600">Failed</div>
        </div>
        <div className="bg-blue-50 p-3 rounded">
          <div className="text-2xl font-bold text-blue-800">
            {batch.current_subject_index !== null
              ? batch.current_subject_index + 1
              : "-"}
          </div>
          <div className="text-xs text-gray-600">Current Item</div>
        </div>
      </div>

      {/* Progress Bar */}
      <div className="space-y-2">
        <div className="flex justify-between text-sm">
          <span className="font-semibold">Overall Progress</span>
          <span>
            {processedItems} / {totalItems} briefs
          </span>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-3">
          <div
            className="bg-blue-600 h-3 rounded-full transition-all"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>

      {/* Current Subject */}
      {batch.status === "running" && batch.current_subject_index !== null && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h3 className="font-semibold text-blue-900">
            Processing Item {batch.current_subject_index + 1} of {totalItems}
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
        <p>Batch ID: {batch.batch_id}</p>
        <p>Edition ID: {batch.edition_id}</p>
        <p>Profile: {batch.profile}</p>
        <p>Created: {new Date(batch.created_at).toLocaleString()}</p>
        {batch.started_at && (
          <p>Started: {new Date(batch.started_at).toLocaleString()}</p>
        )}
        {batch.finished_at && (
          <p>Finished: {new Date(batch.finished_at).toLocaleString()}</p>
        )}
      </div>
    </div>
  );
}
