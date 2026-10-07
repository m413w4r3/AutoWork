import type { BatchStatus } from "../../api/production";

export function productionBatchPollingInterval(
  status: BatchStatus["status"] | undefined,
): number | false {
  if (status === "queued" || status === "running") return 2_000;
  if (status === "paused") return 5_000;
  return false;
}
