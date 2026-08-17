/**
 * Production Stage Card Component
 * Shows the status of a single production stage
 */

interface ProductionStageCardProps {
  stage: string;
  status: string;
  stageNumber: number;
  isActive?: boolean;
}

const STAGE_NAMES: Record<string, string> = {
  sources: "Sources",
  references: "References",
  extraction: "Extraction",
  synthesis: "Synthesis",
  assembly: "Assembly",
};

const STATUS_ICONS: Record<string, { icon: string; color: string }> = {
  pending: { icon: "○", color: "text-gray-400" },
  running: { icon: "●", color: "text-blue-500 animate-pulse" },
  succeeded: { icon: "✓", color: "text-green-500" },
  verified: { icon: "✓", color: "text-green-500" },
  needs_review: { icon: "⚠", color: "text-yellow-500" },
  failed: { icon: "✗", color: "text-red-500" },
};

export function ProductionStageCard({
  stage,
  status,
  stageNumber,
  isActive,
}: ProductionStageCardProps) {
  const statusInfo = (STATUS_ICONS[status] || STATUS_ICONS["pending"]) as {
    icon: string;
    color: string;
  };

  const bgColor = isActive
    ? "bg-blue-50 border-blue-300"
    : "bg-white border-gray-200";
  const borderColor = isActive ? "border-2" : "border";

  return (
    <div
      className={`${bgColor} ${borderColor} rounded-lg p-4 flex flex-col items-center justify-center space-y-2 transition-all`}
    >
      {/* Stage Number Circle */}
      <div className="w-10 h-10 rounded-full bg-gray-200 flex items-center justify-center font-bold text-sm">
        {stageNumber}
      </div>

      {/* Stage Name */}
      <h3 className="font-semibold text-center text-sm">
        {STAGE_NAMES[stage] || stage}
      </h3>

      {/* Status Icon */}
      <div className={`text-2xl ${statusInfo.color}`}>{statusInfo.icon}</div>

      {/* Status Label */}
      <p className="text-xs text-center text-gray-600 capitalize">
        {status === "verified" ? "completed" : status}
      </p>

      {/* Loading Indicator */}
      {status === "running" && (
        <div className="mt-2">
          <div className="flex space-x-1 justify-center">
            <div className="w-1 h-1 bg-blue-500 rounded-full animate-bounce"></div>
            <div className="w-1 h-1 bg-blue-500 rounded-full animate-bounce delay-100"></div>
            <div className="w-1 h-1 bg-blue-500 rounded-full animate-bounce delay-200"></div>
          </div>
        </div>
      )}
    </div>
  );
}
