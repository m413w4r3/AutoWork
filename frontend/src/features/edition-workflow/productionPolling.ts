import type { BatchStatus } from "../../api/production";

export function productionBatchPollingInterval(
  status: BatchStatus["status"] | undefined,
): number | false {
  return status === "queued" || status === "running" ? 2_000 : false;
}
